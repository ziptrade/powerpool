import urllib3

from gevent.event import Event
from cryptokit.rpc import CoinRPCException, CoinserverRPC

from ..lib import loop, Component
from ..exceptions import RPCException


class Jobmanager(Component):
    pass


class NodeMonitorMixin(object):
    def __init__(self):
        self._down_connections = []  # list of RPC conns that are down
        self._poll_connection = None  # our currently active RPC connection
        self._live_connections = []  # list of live RPC connections
        self._connected = Event()  # An event type status flag

    def _start_monitor_nodes(self):
        for serv in self.config['coinservs']:
            conn = CoinserverRPC(
                "http://{0}:{1}@{2}:{3}/"
                .format(serv['username'],
                        serv['password'],
                        serv['address'],
                        serv['port']),
                pool_kwargs=dict(maxsize=serv.get('maxsize', 10)))
            conn.config = serv
            conn.name = "{}:{}".format(serv['address'], serv['port'])
            self._down_connections.append(conn)

    @loop(setup='_start_monitor_nodes', interval='rpc_ping_int')
    def _monitor_nodes(self):
        remlist = []
        for conn in self._down_connections:
            try:
                info = conn.getinfo()
            except (urllib3.exceptions.HTTPError, CoinRPCException, ValueError):
                self.logger.info("RPC connection {} still down!".format(conn.name))
                continue

            # check if we've got connections on the daemon
            if not info.get('connections', 5):
                self.logger.info("Connected to {}, but RPC server has no connections"
                                 .format(conn.name))
                continue

            self._live_connections.append(conn)
            remlist.append(conn)
            self.logger.info("Now connected to {} RPC Server {}."
                             .format(self.config['currency'], conn.name))

            # if this connection has a higher priority than current
            if self._poll_connection is not None:
                curr_poll = self._poll_connection.config['poll_priority']
                if conn.config['poll_priority'] > curr_poll:
                    self.logger.info("RPC connection {} has higher poll priority than "
                                     "current poll connection, switching..."
                                     .format(conn.name))
                    self._poll_connection = conn
            else:
                self._connected.set()
                self._poll_connection = conn
                self.logger.info("RPC connection {} defaulting poll connection"
                                 .format(conn.name))

        for conn in remlist:
            self._down_connections.remove(conn)

    def down_connection(self, conn):
        """ Called when a connection goes down. Removes if from the list of
        live connections and recomputes a new. """
        if not conn:
            self.logger.warn("Tried to down a NoneType connection")
            return

        if conn in self._live_connections:
            self._live_connections.remove(conn)

        if self._poll_connection is conn:
            # find the next best poll connection
            try:
                self._poll_connection = min(self._live_connections,
                                            key=lambda x: x.config['poll_priority'])
            except ValueError:
                self._poll_connection = None
                self._connected.clear()
                self.logger.error("No RPC connections available for polling!!!")
            else:
                self.logger.warn("RPC connection {} switching to poll_connection "
                                 "after {} went down!"
                                 .format(self._poll_connection.name, conn.name))

        if conn not in self._down_connections:
            self.logger.info("Server at {} now reporting down".format(conn.name))
            self._down_connections.append(conn)

    def call_rpc(self, command, *args, **kwargs):
        self._connected.wait()
        try:
            return getattr(self._poll_connection, command)(*args, **kwargs)
        except (urllib3.exceptions.HTTPError, CoinRPCException) as e:
            self.logger.warn("Unable to perform {} on RPC server. Got: {}"
                             .format(command, e))
            self.down_connection(self._poll_connection)
            raise RPCException(e)
