import json
from urllib import urlencode
from urlparse import urljoin

from twisted.web import http
from twisted.internet import reactor
from twisted.internet.task import Clock
from twisted.internet.defer import inlineCallbacks, returnValue, Deferred
from twisted.web.client import HTTPConnectionPool, readBody
from twisted.web.server import NOT_DONE_YET

import treq

from vumi.tests.helpers import VumiTestCase
from vumi.transports.httprpc.tests.helpers import HttpRpcTransportHelper
from vumi.tests.utils import LogCatcher
from vumi.tests.utils import MockHttpServer

from vxturn.turn import TurnTransport

class TestTurnTransport(VumiTestCase):
    @inlineCallbacks
    def setUp(self):
        self.clock = Clock()
        self.patch(TurnTransport, 'get_clock', lambda _: self.clock)

        self.remote_request_handler = lambda _: 'OK.1234'
        self.remote_server = MockHttpServer(self.remote_handle_request)
        yield self.remote_server.start()
        self.addCleanup(self.remote_server.stop)

        self.tx_helper = self.add_helper(
            HttpRpcTransportHelper(TurnTransport))

        connection_pool = HTTPConnectionPool(reactor, persistent=False)
        treq._utils.set_global_pool(connection_pool)

    @inlineCallbacks
    def mk_transport(self, **kw):
        config = {
            'outbound_url': urljoin(self.remote_server.url, 'v1/messages'),
            'token': 'super-secret-token',
            'to_addr': '+271234',
            'hmac_secret': 'test-hmac-secret',
            'web_port': 0,
            'web_path': '/api/v1/turn/',
            'publish_status': True,
        }
        config.update(kw)

        transport = yield self.tx_helper.get_transport(config)
        self.patch(transport, 'get_clock', lambda _: self.clock)
        returnValue(transport)

    @inlineCallbacks
    def patch_reactor_call_later(self):
        yield self.wait_for_test_setup()
        self.patch(reactor, 'callLater', self.clock.callLater)

    def wait_for_test_setup(self):
        """
        Wait for test setup to complete.
        Twisted's twisted.trial._asynctest runner calls `reactor.callLater`
        to set the test timeout *after* running the start of the test. We
        thus need to wait for this to happen *before* we patch
        `reactor.callLater`.
        """
        d = Deferred()
        reactor.callLater(0, d.callback, None)
        return d

    def capture_remote_requests(self, response='OK.1234'):
        def handler(req):
            req.payload = json.loads(req.content.read())
            reqs.append(req)
            return response

        reqs = []
        self.remote_request_handler = handler
        return reqs

    def remote_handle_request(self, req):
        return self.remote_request_handler(req)

    def get_host(self, transport):
        addr = transport.web_resource.getHost()
        return '%s:%s' % (addr.host, addr.port)

    def assert_contains_items(self, obj, items):
        for name, value in items.iteritems():
            self.assertEqual(obj[name], value)

    def assert_uri(self, actual_uri, path, params):
        actual_path, actual_params = actual_uri.split('?')
        self.assertEqual(actual_path, path)

        self.assertEqual(
            sorted(actual_params.split('&')),
            sorted(urlencode(params).split('&')))

    def assert_request_params(self, transport, req, params):
        self.assert_contains_items(req, {
            'method': 'POST',
            'path': transport.config['web_path'],
            'content': '',
            'headers': {
                'Connection': ['close'],
                'Host': [self.get_host(transport)]
            }
        })

        self.assert_uri(req['uri'], transport.config['web_path'], params)

    @inlineCallbacks
    def test_outbound_success(self):
        yield self.mk_transport()

        reqs = self.capture_remote_requests()

        self.tx_helper.clear_dispatched_statuses()
        msg = yield self.tx_helper.make_dispatch_outbound(
            from_addr='456',
            to_addr='+123',
            content='hi')

        [req] = reqs

        self.assertTrue(req.uri.startswith('/v1/messages'))
        self.assertEqual(req.method, 'POST')
        self.assertEqual(req.payload, {
            "to": "123",
            "type": "text",
            "preview_url": False,
            "recipient_type": "individual",
            "text": {"body": "hi"}
        })

        headers = req.requestHeaders
        auth = headers.getRawHeaders('Authorization')[0]
        content_type = headers.getRawHeaders('Content-Type')[0]
        user_agent = headers.getRawHeaders('User-Agent')[0]

        self.assertEqual(auth, 'Bearer super-secret-token')
        self.assertEqual(content_type, 'application/json')
        self.assertEqual(user_agent, 'Vumi Turn Transport')

        [ack] = yield self.tx_helper.wait_for_dispatched_events(1)

        self.assert_contains_items(ack, {
            'user_message_id': msg['message_id'],
            'sent_message_id': msg['message_id'],
        })

        [status] = self.tx_helper.get_dispatched_statuses()

        self.assert_contains_items(status, {
            'status': 'ok',
            'component': 'outbound',
            'type': 'request_success',
            'message': 'Request successful',
        })

    @inlineCallbacks
    def test_outbound_error(self):
        def handler(req):
            req.setResponseCode(400)
            return 'Invalid recipient type'

        yield self.mk_transport()
        self.remote_request_handler = handler

        msg = yield self.tx_helper.make_dispatch_outbound(
            from_addr='456',
            to_addr='+123',
            content='hi')

        [nack] = yield self.tx_helper.wait_for_dispatched_events(1)

        self.assert_contains_items(nack, {
            'event_type': 'nack',
            'user_message_id': msg['message_id'],
            'sent_message_id': msg['message_id'],
            'nack_reason': 'Invalid recipient type',
        })

        [status] = self.tx_helper.get_dispatched_statuses()

        self.assert_contains_items(status, {
            'status': 'down',
            'component': 'outbound',
            'type': 'request_failed',
            'message': 'Invalid recipient type',
        })

    @inlineCallbacks
    def test_outbound_timeout(self):
        self.remote_request_handler = lambda _: NOT_DONE_YET
        yield self.mk_transport(outbound_request_timeout=3)

        msg = self.tx_helper.make_outbound(
            from_addr='456',
            to_addr='+123',
            content='hi')

        yield self.patch_reactor_call_later()
        d = self.tx_helper.dispatch_outbound(msg)
        self.clock.advance(0)  # trigger initial request
        self.clock.advance(2)  # wait 2 seconds of timeout
        self.assertEqual(self.tx_helper.get_dispatched_statuses(), [])
        self.clock.advance(1)  # wait last second of timeout
        yield d

        [nack] = yield self.tx_helper.get_dispatched_events()

        self.assert_contains_items(nack, {
            'event_type': 'nack',
            'user_message_id': msg['message_id'],
            'sent_message_id': msg['message_id'],
            'nack_reason': 'Request timeout',
        })

        [status] = self.tx_helper.get_dispatched_statuses()

        self.assert_contains_items(status, {
            'status': 'down',
            'component': 'outbound',
            'type': 'request_timeout',
            'message': 'Request timeout',
        })
