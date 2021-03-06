#
#  Copyright 2015-2016 Bleemeo
#
#  bleemeo.com an infrastructure monitoring solution in the Cloud
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#

import hashlib
import json
import logging
import os
import random
import socket
import ssl
import threading
import time
import zlib

import paho.mqtt.client as mqtt
import requests
from six.moves import queue
from six.moves import urllib_parse

import bleemeo_agent
import bleemeo_agent.util


MQTT_QUEUE_MAX_SIZE = 2000


class ApiError(Exception):
    def __init__(self, response):
        super(Exception, self).__init__()
        self.response = response


def api_iterator(url, params, auth, headers=None):
    """ Call Bleemeo API on a list endpoints and return a iterator
        that request all pages
    """
    params = params.copy()
    if 'page_size' not in params:
        params['page_size'] = 100

    response = requests.get(
        url,
        params=params,
        auth=auth,
        headers=headers,
    )

    if response.status_code != 200:
        raise ApiError(response)

    data = response.json()
    if isinstance(data, list):
        # Old API without pagination
        for item in data:
            yield item

        return

    for item in data['results']:
        yield item

    while data['next']:
        response = requests.get(data['next'], auth=auth, headers=headers)

        if response.status_code != 200:
            raise ApiError(response)

        data = response.json()
        for item in data['results']:
            yield item


def convert_docker_date(input_date):
    """ Take a string representing a date using Docker inspect format and return
        None if the date is "0001-01-01T00:00:00Z"
    """
    if input_date is None:
        return None

    if input_date == '0001-01-01T00:00:00Z':
        return None
    return input_date


def get_listen_addresses(service_info):
    """ Return the listen_addresses for a service_info
    """
    try:
        address = socket.gethostbyname(service_info['address'])
    except (socket.gaierror, TypeError, KeyError):
        # gaierror => unable to resolv name
        # TypeError => service_info['address'] is None (happen when
        #              service is on a stopped container)
        # KeyError => no 'address' in service_info (happen when service
        #             is a customer defined using Nagios check).
        address = None

    extra_ports = service_info.get('extra_ports', {}).copy()
    if service_info.get('port') is not None and len(extra_ports) == 0:
        if service_info['protocol'] == socket.IPPROTO_TCP:
            extra_ports['%s/tcp' % service_info['port']] = address
        elif service_info['protocol'] == socket.IPPROTO_UDP:
            extra_ports['%s/udp' % service_info['port']] = address

    return ','.join(
        '%s:%s' % (address, port_proto)
        for (port_proto, address) in extra_ports.items()
    )


class BleemeoConnector(threading.Thread):

    def __init__(self, core):
        super(BleemeoConnector, self).__init__()
        self.core = core

        self._metric_queue = queue.Queue()
        self.connected = False
        self._mqtt_queue_size = 0
        self._last_facts_sent = None
        self._last_discovery_sent = None
        self._last_update = None
        self.last_containers_removed = bleemeo_agent.util.get_clock()
        self.mqtt_client = mqtt.Client()

        # Lock held when modifying self.metrics_uuid or self.services_uuid and
        # when modification should not occur (e.g. during .items())
        self.metrics_lock = threading.Lock()
        self.metrics_uuid = self.core.state.get_complex_dict(
            'metrics_uuid', {}
        )
        self.services_uuid = self.core.state.get_complex_dict(
            'services_uuid', {}
        )
        self.metrics_info = {}

        # Make sure this metrics exists and try to be registered
        self.metrics_uuid.setdefault(('agent_status', None, None), None)
        self.metrics_info.setdefault(('agent_status', None, None), {})

        self._apply_upgrade()

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0 and not self.core.is_terminating.is_set():
            self.connected = True
            msg = {
                'public_ip': self.core.last_facts.get('public_ip'),
            }
            self.publish(
                'v1/agent/%s/connect' % self.agent_uuid,
                json.dumps(msg),
            )
            # FIXME: PRODUCT-137: to be removed when upstream bug is fixed
            if self.mqtt_client._ssl is not None:
                self.mqtt_client._ssl.setblocking(0)

            self.mqtt_client.subscribe(
                'v1/agent/%s/notification' % self.agent_uuid
            )
            logging.info('MQTT connection established')

    def on_disconnect(self, client, userdata, rc):
        if self.connected:
            logging.info('MQTT connection lost')
        self.connected = False

    def on_message(self, client, userdata, msg):
        notify_topic = 'v1/agent/%s/notification' % self.agent_uuid
        if msg.topic == notify_topic and len(msg.payload) < 1024 * 64:
            try:
                body = json.loads(msg.payload.decode('utf-8'))
            except Exception as exc:
                logging.info('Failed to decode message for Bleemeo: %s', exc)
                return

            if 'message_type' not in body:
                return
            if body['message_type'] == 'threshold-update':
                logging.debug('Got "threshold-update" message from Bleemeo')
                self._last_update = 0  # trigger a sync with Bleemeo

    def on_publish(self, client, userdata, mid):
        self._mqtt_queue_size -= 1
        self.core.update_last_report()

    def check_config_requirement(self):
        sleep_delay = 10
        while (self.core.config.get('bleemeo.account_id') is None
                or self.core.config.get('bleemeo.registration_key') is None):
            logging.warning(
                'bleemeo.account_id and/or '
                'bleemeo.registration_key is undefine. '
                'Please see https://docs.bleemeo.com/how-to-configure-agent')
            self.core.is_terminating.wait(sleep_delay)
            if self.core.is_terminating.is_set():
                raise StopIteration
            self.core.reload_config()
            sleep_delay = min(sleep_delay * 2, 600)

        if self.core.state.get('password') is None:
            self.core.state.set(
                'password', bleemeo_agent.util.generate_password())

    def run(self):
        self.core.add_scheduled_job(
            self._bleemeo_health_check,
            seconds=60,
        )

        if self.core.sentry_client and self.agent_uuid:
            self.core.sentry_client.site = self.agent_uuid

        try:
            self.check_config_requirement()
        except StopIteration:
            return

        self.core.add_scheduled_job(
            self._bleemeo_synchronize,
            seconds=15,
            next_run_in=4,
        )

        while not self._ready_for_mqtt():
            self.core.is_terminating.wait(1)
            if self.core.is_terminating.is_set():
                return

        self._mqtt_setup()

        while not self.core.is_terminating.is_set():
            self._loop()

        if self.connected and not self.upgrade_in_progress:
            self.publish(
                'v1/agent/%s/disconnect' % self.agent_uuid,
                json.dumps({'disconnect-cause': 'Clean shutdown'}),
                force=True
            )

        # Wait up to 5 second for MQTT queue to be empty before disconnecting
        deadline = bleemeo_agent.util.get_clock() + 5
        while (self._mqtt_queue_size > 0
                and bleemeo_agent.util.get_clock() < deadline):
            time.sleep(0.1)

        self.mqtt_client.disconnect()
        self.mqtt_client.loop_stop()

    def _apply_upgrade(self):
        # PRODUCT-279: elasticsearch_search_time was previously not associated
        # with the service elasticsearch

        with self.metrics_lock:
            for key in list(self.metrics_uuid):
                (metric_name, service, item) = key
                if (metric_name == 'elasticsearch_search_time'
                        and service is None):
                    value = self.metrics_uuid[key]
                    new_key = (metric_name, 'elasticsearch', item)
                    self.metrics_uuid.setdefault(new_key, value)
                    del self.metrics_uuid[key]
                    self.core.state.set_complex_dict(
                        'metrics_uuid', self.metrics_uuid
                    )

        # PRODUCT-537 added a "active" flag to service. It's default value is
        # True, set that default on service without active flag.
        # This will avoid updating all service when that flag goes from
        # undefine (so using default is True) to defined as True.
        # It also added a "stack" field with default value as ""
        for service_info in self.services_uuid.values():
            service_info.setdefault('active', True)
            service_info.setdefault('stack', '')

    def _ready_for_mqtt(self):
        """ Check for requirement needed before MQTT connection

            * agent must be registered
            * it need initial facts
            * "agent_status" metrics must be registered
        """
        agent_status_key = ('agent_status', None, None)
        return (
            self.agent_uuid is not None and
            self.core.last_facts and
            self.metrics_uuid.get(agent_status_key) is not None
        )

    def _bleemeo_health_check(self):
        """ Check the Bleemeo connector works correctly. Log any issue found
        """
        clock_now = bleemeo_agent.util.get_clock()

        if self.agent_uuid is None:
            logging.info('Agent not yet registered')

        if not self.connected:
            logging.info(
                'Bleemeo connection (MQTT) is currently not established'
            )

        if self._mqtt_queue_size >= MQTT_QUEUE_MAX_SIZE:
            logging.warning(
                'Sending queue to Bleemeo Cloud is full. '
                'New messages are dropped'
            )
        elif self._mqtt_queue_size > 10:
            logging.info(
                '%s messages waiting to be sent to Bleemeo Cloud',
                self._mqtt_queue_size,
            )

        if self._metric_queue.qsize() > 10:
            logging.info(
                '%s metric points blocked due to metric not yet registered',
                self._metric_queue.qsize(),
            )

        if (self.core.graphite_server.data_last_seen_at is None or
                clock_now - self.core.graphite_server.data_last_seen_at > 60):
            logging.info(
                'Issue with metrics collector: no metric received from %s',
                self.core.graphite_server.metrics_source,
            )

    def _mqtt_setup(self):
        self.mqtt_client.will_set(
            'v1/agent/%s/disconnect' % self.agent_uuid,
            json.dumps({'disconnect-cause': 'disconnect-will'}),
            1,
        )
        if hasattr(ssl, 'PROTOCOL_TLSv1_2'):
            # Need Python 3.4+ or 2.7.9+
            tls_version = ssl.PROTOCOL_TLSv1_2
        else:
            tls_version = ssl.PROTOCOL_TLSv1

        if self.core.config.get('bleemeo.mqtt.ssl', True):
            cafile = self.core.config.get(
                'bleemeo.mqtt.cafile',
                '/etc/ssl/certs/ca-certificates.crt'
            )
            if '$INSTDIR' in cafile and os.name == 'nt':
                # Under Windows, $INSTDIR is remplaced by installation
                # directory
                cafile = cafile.replace(
                    '$INSTDIR',
                    bleemeo_agent.util.windows_instdir()
                )
            self.mqtt_client.tls_set(
                cafile,
                tls_version=tls_version,
            )
            self.mqtt_client.tls_insecure_set(
                self.core.config.get('bleemeo.mqtt.ssl_insecure', False)
            )

        self.mqtt_client.on_connect = self.on_connect
        self.mqtt_client.on_disconnect = self.on_disconnect
        self.mqtt_client.on_message = self.on_message
        self.mqtt_client.on_publish = self.on_publish

        mqtt_host = self.core.config.get(
            'bleemeo.mqtt.host',
            'mqtt.bleemeo.com'
        )
        mqtt_port = self.core.config.get(
            'bleemeo.mqtt.port',
            8883,
        )

        self.mqtt_client.username_pw_set(
            self.agent_username,
            self.agent_password,
        )

        try:
            logging.debug('Connecting to MQTT broker at %s', mqtt_host)
            self.mqtt_client.connect(
                mqtt_host,
                mqtt_port,
                60,
            )
        except socket.error:
            pass

        self.mqtt_client.loop_start()

    def _loop(self):
        """ Call as long as agent is running. It's the "main" method for
            Bleemeo connector thread.
        """
        metrics = []
        repush_metric = None
        timeout = 3

        try:
            while True:
                metric = self._metric_queue.get(timeout=timeout)
                timeout = 0.3  # Long wait only for the first get
                key = (
                    metric['measurement'],
                    metric.get('service'),
                    metric.get('item')
                )
                metric_uuid = self.metrics_uuid.get(key, 'deleted')
                if metric_uuid == 'deleted':
                    continue

                if metric_uuid is None:
                    # UUID is not available now. Ignore this metric for now
                    self._metric_queue.put(metric)
                    if repush_metric is metric:
                        # It has looped, the first re-pushed metric was
                        # re-read.
                        # Sleep a short time to avoid looping for nothing
                        # and consuming all CPU
                        time.sleep(0.5)
                        break

                    if repush_metric is None:
                        repush_metric = metric

                    continue

                bleemeo_metric = metric.copy()
                bleemeo_metric['uuid'] = metric_uuid
                metrics.append(bleemeo_metric)
                if len(metrics) > 1000:
                    break
        except queue.Empty:
            pass

        if len(metrics) != 0:
            self.publish(
                'v1/agent/%s/data' % self.agent_uuid,
                json.dumps(metrics)
            )

    def publish_top_info(self, top_info):
        if self.agent_uuid is None:
            return

        if not self.connected:
            return

        self.publish(
            'v1/agent/%s/top_info' % self.agent_uuid,
            bytearray(zlib.compress(json.dumps(top_info).encode('utf8')))
        )

    def publish(self, topic, message, force=False):
        if self._mqtt_queue_size > MQTT_QUEUE_MAX_SIZE and not force:
            return

        self._mqtt_queue_size += 1
        self.mqtt_client.publish(
            topic,
            message,
            1)

    def register(self):
        """ Register the agent to Bleemeo SaaS service
        """
        base_url = self.bleemeo_base_url
        registration_url = urllib_parse.urljoin(base_url, '/v1/agent/')

        name = self.core.last_facts.get('fqdn')
        if not name:
            logging.debug('Register delayed, fact fqdn not available')
            return

        registration_key = self.core.config.get('bleemeo.registration_key')
        payload = {
            'account': self.account_id,
            'initial_password': self.core.state.get('password'),
            'display_name': name,
            'fqdn': name,
        }

        content = None
        try:
            response = requests.post(
                registration_url,
                data=json.dumps(payload),
                auth=('%s@bleemeo.com' % self.account_id, registration_key),
                headers={
                    'X-Requested-With': 'XMLHttpRequest',
                    'Content-type': 'application/json',
                    'User-Agent': self.core.http_user_agent,
                },
            )
            content = response.json()
        except requests.exceptions.RequestException:
            response = None
        except ValueError:
            pass  # unable to decode JSON

        if (response is not None
                and response.status_code == 201
                and content is not None
                and 'id' in content):
            self.core.state.set('agent_uuid', content['id'])
            logging.debug('Regisration successfull')
        elif content is not None:
            logging.info(
                'Registration failed: %s',
                content
            )
        elif response is not None:
            logging.debug(
                'Registration failed, response is not a json: %s',
                response.content[:100])
        else:
            logging.debug('Registration failed, unable to connect to API')

        if self.core.sentry_client and self.agent_uuid:
            self.core.sentry_client.site = self.agent_uuid

    def _bleemeo_synchronize(self):
        """ Synchronize object between local state and Bleemeo SaaS
        """
        if self.agent_uuid is None:
            self.register()

        if self.agent_uuid is None:
            return

        clock_now = bleemeo_agent.util.get_clock()
        if (self._last_update is None
                or clock_now - self._last_update > 60 * 60
                or self.core.last_services_autoremove >= self._last_update
                or self.last_containers_removed >= self._last_update):
            try:
                self._purge_deleted_services()
            except ApiError as exc:
                logging.info(
                    'Unable to synchronize services. API responded: %s',
                    exc.response.content,
                )
            try:
                self._retrive_threshold()
            except ApiError as exc:
                logging.info(
                    'Unable to synchronize metrics. API responded: %s',
                    exc.response.content,
                )
            self._update_tags()
            self._last_update = clock_now

        if (self._last_discovery_sent is None or
                self._last_discovery_sent < self.core.last_discovery_update):
            self._register_containers()
            self._last_discovery_sent = clock_now

        self._register_services()
        self._register_metric()

        if (self._last_facts_sent is None
                or self._last_facts_sent < self.core.last_facts_update):
            try:
                self.send_facts()
            except ApiError as exc:
                logging.info(
                    'Unable to synchronize facts. API responded: %s',
                    exc.response.content,
                )

    def _retrive_threshold(self):
        """ Retrieve threshold for all registered metrics

            Also remove from state any deleted metrics and remove it from
            core.last_metrics (in memory cache of last value)
        """
        logging.debug('Retrieving thresholds')
        thresholds = {}
        base_url = self.bleemeo_base_url
        metric_url = urllib_parse.urljoin(base_url, '/v1/metric/')
        metrics = api_iterator(
            metric_url,
            params={'agent': self.agent_uuid},
            auth=(self.agent_username, self.agent_password),
            headers={'User-Agent': self.core.http_user_agent},
        )

        metrics_registered = set()

        for data in metrics:
            metrics_registered.add(data['id'])
            item = data['item']
            if item == '':
                # API use "" for no item. Agent use None
                item = None

            thresholds[(data['label'], item)] = {
                'low_warning': data['threshold_low_warning'],
                'low_critical': data['threshold_low_critical'],
                'high_warning': data['threshold_high_warning'],
                'high_critical': data['threshold_high_critical'],
            }

        self.core.update_thresholds(thresholds)

        deleted_metrics = []
        with self.metrics_lock:
            for key in list(self.metrics_uuid.keys()):
                (metric_name, service_name, item) = key
                value = self.metrics_uuid[key]
                if value is None or value in metrics_registered:
                    continue

                del self.metrics_uuid[key]
                deleted_metrics.append((metric_name, item))

            if deleted_metrics:
                self.core.state.set_complex_dict(
                    'metrics_uuid', self.metrics_uuid
                )

        if deleted_metrics:
            self.core.purge_metrics(deleted_metrics)

    def _purge_deleted_services(self):
        """ Remove from state any deleted service on API and vice-versa

            Also remove them from discovered service.
        """
        base_url = self.bleemeo_base_url
        service_url = urllib_parse.urljoin(base_url, '/v1/service/')

        deleted_services_from_state = (
            set(self.services_uuid) - set(self.core.services)
        )
        for key in deleted_services_from_state:
            service_uuid = self.services_uuid[key]['uuid']
            response = requests.delete(
                service_url + '%s/' % service_uuid,
                auth=(self.agent_username, self.agent_password),
                headers={
                    'X-Requested-With': 'XMLHttpRequest',
                    'User-Agent': self.core.http_user_agent,
                },
            )
            if response.status_code not in (204, 404):
                logging.debug(
                    'Service deletion failed. Server response = %s',
                    response.content
                )
                continue
            del self.services_uuid[key]
            self.core.state.set_complex_dict(
                'services_uuid', self.services_uuid
            )

        services = api_iterator(
            service_url,
            params={'agent': self.agent_uuid},
            auth=(self.agent_username, self.agent_password),
            headers={'User-Agent': self.core.http_user_agent},
        )

        services_registred = set()
        for data in services:
            services_registred.add(data['id'])

        deleted_services = []
        for key in list(self.services_uuid.keys()):
            (service_name, instance) = key
            entry = self.services_uuid[key]
            if entry is None or entry['uuid'] in services_registred:
                continue

            del self.services_uuid[key]
            deleted_services.append(key)

        self.core.state.set_complex_dict(
            'services_uuid', self.services_uuid
        )

        if deleted_services:
            logging.debug(
                'API deleted the following services: %s',
                deleted_services
            )
            self.core.update_discovery(deleted_services=deleted_services)

    def _update_tags(self):
        """ Synchronize tags with Bleemeo API
        """
        current_tags = set(self.core.config.get('tags', []))
        old_tags = set(self.core.state.get('tags_uuid', {}))

        if current_tags == old_tags:
            return

        response = requests.get(
            urllib_parse.urljoin(
                self.bleemeo_base_url, '/v1/agent/%s/' % self.agent_uuid
            ),
            params={'fields': 'tags'},
            auth=(self.agent_username, self.agent_password),
            headers={
                'X-Requested-With': 'XMLHttpRequest',
                'User-Agent': self.core.http_user_agent,
            },
        )
        if response.status_code != 200:
            logging.debug(
                'Fetching current agent tags failed: %s',
                response.content
            )
            return

        current_api_tags = set(x['name'] for x in response.json()['tags'])

        deleted_tags = (old_tags - current_tags)
        tags = (current_api_tags - deleted_tags).union(current_tags)

        response = requests.patch(
            urllib_parse.urljoin(
                self.bleemeo_base_url, '/v1/agent/%s/' % self.agent_uuid
            ),
            data=json.dumps({'tags': [{'name': x} for x in tags]}),
            auth=(self.agent_username, self.agent_password),
            headers={
                'X-Requested-With': 'XMLHttpRequest',
                'Content-type': 'application/json',
                'User-Agent': self.core.http_user_agent,
            },
        )
        if response.status_code > 400:
            logging.debug(
                'Updating current agent tags failed: %s',
                response.content
            )
            return

        tags_uuid = {}
        for tag in response.json()['tags']:
            if tag['name'] in current_tags:
                tags_uuid[tag['name']] = tag['id']
        self.core.state.set('tags_uuid', tags_uuid)

    def _register_services(self):
        """ Check for any unregistered services and register them

            Also check for changed services and update them
        """
        base_url = self.bleemeo_base_url
        registration_url = urllib_parse.urljoin(base_url, '/v1/service/')

        for key, service_info in self.core.services.items():
            (service_name, instance) = key

            entry = {
                'listen_addresses':
                    get_listen_addresses(service_info),
                'label': service_name,
                'exe_path': service_info.get('exe_path', ''),
                'stack': service_info.get('stack', ''),
                'active': service_info.get('active', True),
            }
            if instance is not None:
                entry['instance'] = instance

            if key in self.services_uuid:
                entry['uuid'] = self.services_uuid[key]['uuid']
                # check for possible update
                if self.services_uuid[key] == entry:
                    continue
                method = requests.put
                service_uuid = self.services_uuid[key]['uuid']
                url = registration_url + str(service_uuid) + '/'
                expected_code = 200
            else:
                method = requests.post
                url = registration_url
                expected_code = 201

            payload = entry.copy()
            payload.update({
                'account': self.account_id,
                'agent': self.agent_uuid,
            })

            response = method(
                url,
                data=json.dumps(payload),
                auth=(self.agent_username, self.agent_password),
                headers={
                    'X-Requested-With': 'XMLHttpRequest',
                    'Content-type': 'application/json',
                    'User-Agent': self.core.http_user_agent,
                },
            )
            if response.status_code != expected_code:
                logging.debug(
                    'Service registration failed. Server response = %s',
                    response.content
                )
                continue
            entry['uuid'] = response.json()['id']
            self.services_uuid[key] = entry
            self.core.state.set_complex_dict(
                'services_uuid', self.services_uuid
            )

    def _register_metric(self):
        """ Check for any unregistered metrics and register them
        """
        base_url = self.bleemeo_base_url
        registration_url = urllib_parse.urljoin(base_url, '/v1/metric/')
        thresholds = self.core.state.get_complex_dict('thresholds', {})
        container_uuid = self.core.state.get('docker_container_uuid', {})
        registration_error = 0

        # It can't keep the lock during whole loop, because call to API is slow
        # In addition it may remove entry during the loop.
        with self.metrics_lock:
            list_metrics = list(self.metrics_uuid.items())

        # If one metric fail to register, it may block other metric that would
        # register correctly. To reduce this risk, randomize the list, so on
        # next run (every 15 seconds), the metric that failed to register may
        # no longer block other.
        random.shuffle(list_metrics)

        for metric_key, metric_uuid in list_metrics:
            if registration_error > 3:
                logging.debug('Too many registration error')
                return
            if metric_uuid is not None:
                continue

            (metric_name, service, item) = metric_key

            # Do most CPU-bound action under the lock. It avoid taking and
            # releasing the lock multiple time.
            with self.metrics_lock:
                if metric_key not in self.metrics_info:
                    # This should only occur when metric was seen on a
                    # previous run (and stored in state.json), not registered
                    # and not seen since startup.
                    #
                    # Remove the metrics from self.metrics_uuid to purge old
                    # no longer valid metric. If the metric still exists,
                    # it will be re-added to self.metrics_uuid quickly.
                    del self.metrics_uuid[metric_key]
                    self.core.state.set_complex_dict(
                        'metrics_uuid', self.metrics_uuid
                    )
                    continue

                status_of = self.metrics_info[metric_key].get('status_of')
                from_metric_key = (status_of, service, item)

                payload = {
                    'agent': self.agent_uuid,
                    'label': metric_name,
                }
                if status_of is not None:
                    if from_metric_key not in self.metrics_uuid:
                        # The status_of metric is deleted, also delete self
                        del self.metrics_uuid[metric_key]
                        del self.metrics_info[metric_key]
                        self.core.state.set_complex_dict(
                            'metrics_uuid', self.metrics_uuid
                        )
                        continue

                    payload['status_of'] = self.metrics_uuid.get(
                        from_metric_key,
                    )
                    if payload['status_of'] is None:
                        logging.debug(
                            'Metric %s is status_of unregistered metric %s',
                            metric_name,
                            status_of,
                        )
                        continue
                if self.metrics_info[metric_key].get('container') is not None:
                    container_name = self.metrics_info[metric_key]['container']
                    if container_name not in self.core.docker_containers:
                        # Container was removed, drop the metrics
                        del self.metrics_uuid[metric_key]
                        del self.metrics_info[metric_key]
                        self.core.state.set_complex_dict(
                            'metrics_uuid', self.metrics_uuid
                        )
                        continue

                    if (container_name not in container_uuid
                            or container_uuid[container_name][1] is None):
                        # Container not yet registered
                        continue
                    payload['container'] = (
                        container_uuid[container_name][1]
                    )
                if service is not None:
                    instance = self.metrics_info[metric_key]['instance']
                    key = (service, instance)
                    if key not in self.services_uuid:
                        del self.metrics_uuid[metric_key]
                        del self.metrics_info[metric_key]
                        self.core.state.set_complex_dict(
                            'metrics_uuid', self.metrics_uuid
                        )
                        continue

                    payload['service'] = self.services_uuid[key]['uuid']
                if item is None:
                    logging.debug('Registering metric %s', metric_name)
                else:
                    logging.debug(
                        'Registering metric %s, item=%s', metric_name, item
                    )
                    payload['item'] = item

            # This should not be done with self.metrics_lock. It no CPU-bound
            # and would lock other thread that need this lock.
            response = requests.post(
                registration_url,
                data=json.dumps(payload),
                auth=(self.agent_username, self.agent_password),
                headers={
                    'X-Requested-With': 'XMLHttpRequest',
                    'Content-type': 'application/json',
                    'User-Agent': self.core.http_user_agent,
                },
            )
            if 400 <= response.status_code < 500:
                logging.debug(
                    'Metric registration failed. '
                    'Server reported a client error: %s',
                    response.content
                )
                registration_error += 1
                continue
            elif response.status_code != 201:
                logging.debug(
                    'Metric registration failed. Server response = %s',
                    response.content
                )
                return
            data = response.json()

            with self.metrics_lock:
                self.metrics_uuid[metric_key] = (
                    data['id']
                )
                thresholds[(metric_name, item)] = {
                    'low_warning': data['threshold_low_warning'],
                    'low_critical': data['threshold_low_critical'],
                    'high_warning': data['threshold_high_warning'],
                    'high_critical': data['threshold_high_critical'],
                }
                logging.debug(
                    'Metric %s registered with uuid %s',
                    metric_name,
                    self.metrics_uuid[metric_key],
                )

                self.core.state.set_complex_dict(
                    'metrics_uuid', self.metrics_uuid
                )

            self.core.update_thresholds(thresholds)

    def _register_containers(self):
        registration_url = urllib_parse.urljoin(
            self.bleemeo_base_url, '/v1/container/',
        )
        container_uuid = self.core.state.get('docker_container_uuid', {})

        for name, inspect in self.core.docker_containers.items():
            new_hash = hashlib.sha1(
                json.dumps(inspect, sort_keys=True).encode('utf-8')
            ).hexdigest()
            old_hash, obj_uuid = container_uuid.get(name, (None, None))

            if old_hash == new_hash:
                continue

            if obj_uuid is None:
                method = requests.post
                url = registration_url
            else:
                method = requests.put
                url = registration_url + obj_uuid + '/'

            cmd = inspect.get('Config', {}).get('Cmd', [])
            if cmd is None:
                cmd = []

            payload = {
                'host': self.agent_uuid,
                'name': name,
                'command': ' '.join(cmd),
                'docker_status': inspect.get('State', {}).get('Status', ''),
                'docker_created_at': convert_docker_date(
                    inspect.get('Created')
                ),
                'docker_started_at': convert_docker_date(
                    inspect.get('State', {}).get('StartedAt')
                ),
                'docker_finished_at': convert_docker_date(
                    inspect.get('State', {}).get('FinishedAt')
                ),
                'docker_api_version': self.core.last_facts.get(
                    'docker_api_version', ''
                ),
                'docker_id': inspect.get('Id', ''),
                'docker_image_id': inspect.get('Image', ''),
                'docker_image_name':
                    inspect.get('Config', '').get('Image', ''),
                'docker_inspect': json.dumps(inspect),
            }

            response = method(
                url,
                data=json.dumps(payload),
                auth=(self.agent_username, self.agent_password),
                headers={
                    'X-Requested-With': 'XMLHttpRequest',
                    'Content-type': 'application/json',
                    'User-Agent': self.core.http_user_agent,
                },
            )

            if response.status_code not in (200, 201):
                logging.debug(
                    'Container registration failed. Server response = %s',
                    response.content
                )
                continue
            obj_uuid = response.json()['id']
            container_uuid[name] = (new_hash, obj_uuid)
            self.core.state.set('docker_container_uuid', container_uuid)

        deleted_containers = (
            set(container_uuid) - set(self.core.docker_containers)
        )
        for name in deleted_containers:
            (_, obj_uuid) = container_uuid[name]
            url = registration_url + obj_uuid + '/'
            response = requests.delete(
                url,
                auth=(self.agent_username, self.agent_password),
                headers={
                    'X-Requested-With': 'XMLHttpRequest',
                    'User-Agent': self.core.http_user_agent,
                },
            )
            if response.status_code not in (204, 404):
                logging.debug(
                    'Container deletion failed. Server response = %s',
                    response.content,
                )
                continue
            del container_uuid[name]
            self.core.state.set('docker_container_uuid', container_uuid)
            self.last_containers_removed = bleemeo_agent.util.get_clock()

    def emit_metric(self, metric):
        if self._metric_queue.qsize() < 100000:
            self._metric_queue.put(metric)
        metric_name = metric['measurement']
        service = metric.get('service')
        item = metric.get('item')

        with self.metrics_lock:
            key = (metric_name, service, item)
            if key not in self.metrics_info:
                self.metrics_info.setdefault(
                    key,
                    {
                        'status_of': metric.get('status_of'),
                        'instance': metric.get('instance'),
                        'container': metric.get('container'),
                    }
                )

            if key not in self.metrics_uuid:
                self.metrics_uuid.setdefault(key, None)

    def send_facts(self):
        base_url = self.bleemeo_base_url
        fact_url = urllib_parse.urljoin(base_url, '/v1/agentfact/')

        if self.core.state.get('facts_uuid') is not None:
            # facts_uuid were used in older version of Agent
            self.core.state.delete('facts_uuid')

        # Action:
        # * get list of all old facts
        # * create new updated facts
        # * delete old facts

        old_facts = api_iterator(
            fact_url,
            params={'agent': self.agent_uuid, 'page_size': 100},
            auth=(self.agent_username, self.agent_password),
            headers={
                'X-Requested-With': 'XMLHttpRequest',
                'User-Agent': self.core.http_user_agent,
            },
        )

        # Do request(s) now. New fact should not be in this list.
        old_facts = list(old_facts)

        # create new facts
        for fact_name, value in self.core.last_facts.items():
            payload = {
                'agent': self.agent_uuid,
                'key': fact_name,
                'value': str(value),
            }
            response = requests.post(
                fact_url,
                data=json.dumps(payload),
                auth=(self.agent_username, self.agent_password),
                headers={
                    'X-Requested-With': 'XMLHttpRequest',
                    'Content-type': 'application/json',
                    'User-Agent': self.core.http_user_agent,
                },
            )
            if response.status_code == 201:
                logging.debug(
                    'Send fact %s, stored with uuid %s',
                    fact_name,
                    response.json()['id'],
                )
            else:
                logging.debug(
                    'Fact registration failed. Server response = %s',
                    response.content
                )
                return

        # delete old facts
        for fact in old_facts:
            logging.debug(
                'Deleting fact %s (uuid=%s)', fact['key'], fact['id']
            )
            response = requests.delete(
                urllib_parse.urljoin(fact_url, '%s/' % fact['id']),
                auth=(self.agent_username, self.agent_password),
                headers={
                    'X-Requested-With': 'XMLHttpRequest',
                    'User-Agent': self.core.http_user_agent,
                },
            )
            if response.status_code != 204:
                logging.debug(
                    'Delete failed, excepted code=204, recveived %s',
                    response.status_code
                )
                return

        self._last_facts_sent = bleemeo_agent.util.get_clock()

    @property
    def account_id(self):
        return self.core.config.get('bleemeo.account_id')

    @property
    def agent_uuid(self):
        return self.core.state.get('agent_uuid')

    @property
    def agent_username(self):
        return '%s@bleemeo.com' % self.agent_uuid

    @property
    def agent_password(self):
        return self.core.state.get('password')

    @property
    def bleemeo_base_url(self):
        return self.core.config.get(
            'bleemeo.api_base',
            'https://api.bleemeo.com/'
        )

    @property
    def upgrade_in_progress(self):
        upgrade_file = self.core.config.get('agent.upgrade_file', 'upgrade')
        return os.path.exists(upgrade_file)
