import socket
import sys
import unittest
from unittest import mock

from twisted.internet import error as twisted_error
from twisted.python.failure import Failure

from syncplay import constants
from syncplay.client import _retryBackoff
from syncplay.protocols import SyncClientProtocol
from syncplay.utils import hardenTransport


def sys_platform_linux():
    return sys.platform.startswith("linux")


class FakeUI(object):
    def __init__(self):
        self.errors = []
        self.debug = []

    def showErrorMessage(self, message, criticalerror=False):
        self.errors.append(message)

    def showDebugMessage(self, message):
        self.debug.append(message)

    def showMessage(self, message, **kwargs):
        pass

    def setSSLMode(self, *args, **kwargs):
        pass


class FakeClient(object):
    def __init__(self):
        self.ui = FakeUI()
        self._running = True
        self.reconnecting = False
        self._clientSupportsTLS = True
        self._serverSupportsTLS = True
        self.onDisconnectCalls = 0
        self.destroyed = 0

    def onDisconnect(self):
        self.onDisconnectCalls += 1

    def destroyProtocol(self):
        self.destroyed += 1

    def initProtocol(self, protocol):
        pass


def makeProtocol(logged=False, running=True, reconnecting=False):
    client = FakeClient()
    client._running = running
    client.reconnecting = reconnecting
    protocol = SyncClientProtocol(client)
    protocol.logged = logged
    return protocol, client


class HardenTransportTests(unittest.TestCase):
    def test_enables_keepalive_and_nodelay(self):
        transport = mock.Mock(spec=["setTcpKeepAlive", "setTcpNoDelay"])
        hardenTransport(transport)
        transport.setTcpKeepAlive.assert_called_once_with(True)
        transport.setTcpNoDelay.assert_called_once_with(True)

    def test_swallows_transport_method_errors(self):
        transport = mock.Mock(spec=["setTcpKeepAlive", "setTcpNoDelay"])
        transport.setTcpKeepAlive.side_effect = OSError("boom")
        transport.setTcpNoDelay.side_effect = OSError("boom")
        hardenTransport(transport)  # must not raise

    def test_sets_linux_keepalive_intervals(self):
        if not sys_platform_linux():
            self.skipTest("Linux-only socket options")
        transport = mock.Mock(spec=["setTcpKeepAlive", "setTcpNoDelay", "socket"])
        transport.socket = mock.Mock()
        hardenTransport(transport)
        transport.socket.setsockopt.assert_any_call(
            socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30
        )
        transport.socket.setsockopt.assert_any_call(
            socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10
        )
        transport.socket.setsockopt.assert_any_call(
            socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 6
        )

    def test_socket_option_errors_are_swallowed(self):
        transport = mock.Mock(spec=["setTcpKeepAlive", "setTcpNoDelay", "socket"])
        transport.socket = mock.Mock()
        transport.socket.setsockopt.side_effect = OSError("denied")
        hardenTransport(transport)  # must not raise

    def test_missing_methods_are_fine(self):
        hardenTransport(object())  # must not raise


class ConnectionLostClassificationTests(unittest.TestCase):
    def test_connection_done_is_debug_only(self):
        protocol, client = makeProtocol(logged=True)
        protocol._classifyConnectionLoss(Failure(twisted_error.ConnectionDone()))
        self.assertEqual(client.ui.errors, [])
        self.assertTrue(any("cleanly" in line for line in client.ui.debug))

    def test_connection_lost_is_debug_only(self):
        protocol, client = makeProtocol(logged=True)
        protocol._classifyConnectionLoss(
            Failure(twisted_error.ConnectionLost("gone"))
        )
        self.assertEqual(client.ui.errors, [])
        self.assertTrue(
            any("Connection lost" in line for line in client.ui.debug)
        )

    def test_connection_aborted_is_expected_network_outcome(self):
        protocol, client = makeProtocol(logged=True)
        protocol._classifyConnectionLoss(
            Failure(twisted_error.ConnectionAborted())
        )
        self.assertEqual(client.ui.errors, [])
        self.assertTrue(
            any("Connection lost" in line for line in client.ui.debug)
        )

    def test_timeout_is_debug_only(self):
        protocol, client = makeProtocol(logged=True)
        protocol._classifyConnectionLoss(
            Failure(twisted_error.TimeoutError("slow"))
        )
        self.assertEqual(client.ui.errors, [])

    def test_unexpected_exception_shows_one_ui_error(self):
        protocol, client = makeProtocol(logged=True)
        protocol._classifyConnectionLoss(Failure(ValueError("weird")))
        self.assertEqual(len(client.ui.errors), 1)
        self.assertIn("weird", client.ui.errors[0])

    def test_tls_invalid_dns_id_disables_server_tls(self):
        protocol, client = makeProtocol(logged=True)
        protocol._classifyConnectionLoss(
            Failure(Exception("Invalid DNS-ID hostname"))
        )
        self.assertFalse(client._serverSupportsTLS)
        self.assertEqual(client.ui.errors, [])

    def test_tls_protocol_version_disables_client_tls(self):
        protocol, client = makeProtocol(logged=True)
        protocol._classifyConnectionLoss(
            Failure(Exception("tlsv1 alert protocol version"))
        )
        self.assertFalse(client._clientSupportsTLS)


class ConnectionLostFlowTests(unittest.TestCase):
    def test_unexpected_loss_of_logged_session_calls_on_disconnect(self):
        protocol, client = makeProtocol(logged=True, running=True)
        protocol.connectionLost(Failure(twisted_error.ConnectionLost("x")))
        self.assertEqual(client.onDisconnectCalls, 1)
        self.assertEqual(client.destroyed, 1)

    def test_intentional_stop_does_not_pause(self):
        protocol, client = makeProtocol(logged=True, running=False)
        protocol.connectionLost(Failure(twisted_error.ConnectionLost("x")))
        self.assertEqual(client.onDisconnectCalls, 0)

    def test_unlogged_session_does_not_pause(self):
        protocol, client = makeProtocol(logged=False, running=True)
        protocol.connectionLost(Failure(twisted_error.ConnectionLost("x")))
        self.assertEqual(client.onDisconnectCalls, 0)

    def test_manual_reconnect_does_not_pause(self):
        # manualReconnect sets reconnecting=True before stopService().
        protocol, client = makeProtocol(logged=True, running=True, reconnecting=True)
        protocol.connectionLost(Failure(twisted_error.ConnectionLost("x")))
        self.assertEqual(client.onDisconnectCalls, 0)

    def test_connection_done_with_live_session_still_notifies(self):
        # Clean close of a live session is still a loss of the session from the
        # player's perspective (pauseOnLeave); only intentional stop is gated.
        protocol, client = makeProtocol(logged=True, running=True)
        protocol.connectionLost(Failure(twisted_error.ConnectionDone()))
        self.assertEqual(client.onDisconnectCalls, 1)


class RetryBackoffTests(unittest.TestCase):
    def test_first_attempt_uses_initial_delay_times_factor(self):
        # ClientService is 1-based; first delay must be ≈ initialDelay*factor.
        expectedMin = constants.RECONNECT_INITIAL_DELAY * 2.0
        delay = _retryBackoff(1)
        self.assertGreaterEqual(delay, expectedMin)
        self.assertLess(delay, expectedMin + 1.001)  # + jitter < 1s

    def test_delay_doubles_until_cap(self):
        # Unjittered bases are initialDelay * factor**attempt = 1, 2, 4, 8…
        # plus additive jitter in [0, 1).
        for attempt, base in ((1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0)):
            delay = _retryBackoff(attempt)
            self.assertGreaterEqual(delay, base)
            self.assertLess(delay, base + 1.0)

    def test_delay_capped_at_max(self):
        for attempt in (6, 10, 100):
            self.assertLessEqual(
                _retryBackoff(attempt), constants.RECONNECT_MAX_DELAY + 1.0
            )

    def test_retry_constants_sane(self):
        self.assertEqual(constants.RECONNECT_RETRIES, 999)
        self.assertEqual(constants.RECONNECT_INITIAL_DELAY, 0.5)
        self.assertEqual(constants.RECONNECT_MAX_DELAY, 30.0)


if __name__ == "__main__":
    unittest.main()
