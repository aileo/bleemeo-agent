from bleemeo_agent.plugins import base
import bleemeo_agent.util


class Redis(base.PluginV1Base):

    def dependencies_present(self):
        return bleemeo_agent.util.package_installed('redis-server')

    def collectd_configure(self):
        # XXX: collectd from Ubuntu do not provide redis plugins
        return """
LoadPlugin redis
<Plugin redis>
    <Node "bleemeo">
    </Node>
</Plugin>
"""

    def list_checks(self):
        return [(
            'redis-server',
            'Check that Redis server is alive',
            (r'/usr/lib/nagios/plugins/check_tcp -H localhost -p 6379 '
                r'-Es "PING\n" -e "+PONG"'),
            6379)]