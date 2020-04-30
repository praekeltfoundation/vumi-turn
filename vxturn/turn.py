# -*- test-case-name: vumi.transports.turn.tests.test_turn -*-
# -*- encoding: utf-8 -*-

from urllib import urlencode
from datetime import datetime
import json
import string
import warnings
import treq
from StringIO import StringIO

from twisted.web import http
from twisted.web.resource import Resource
from twisted.web.server import NOT_DONE_YET
from twisted.python import log
from twisted.internet.defer import inlineCallbacks, CancelledError
from twisted.internet.protocol import Protocol
from twisted.internet.error import ConnectingCancelledError
from twisted.web._newclient import ResponseNeverReceived


from vumi.utils import http_request_full, LogFilterSite
from vumi.transports.base import Transport
from vumi.transports.failures import TemporaryFailure, PermanentFailure
from vumi.errors import VumiError
from vumi.config import ConfigText, ConfigInt
from vumi.transports.httprpc import HttpRpcTransport


class TurnTransportConfig(HttpRpcTransport.CONFIG_CLASS):
    """Config for Turn transport."""

    outbound_url = ConfigText(
        "Url to use for outbound messages",
        required=True)

    token = ConfigText(
        "Token to use for outbound messages",
        required=True)

    to_addr = ConfigText(
        "To Address to use for inbound messages",
        required=True)

    hmac_secret = ConfigText(
        "HMAC secret used to validate incoming events and messages",
        required=True)

    outbound_request_timeout = ConfigInt(
        "Timeout duration in seconds for requests for sending messages, or "
        "null for no timeout",
        default=None)


def get_datetime(turn_timestamp):
    if turn_timestamp:
        return datetime.fromtimestamp(turn_timestamp)
    else:
        return ''


def format_msisdn_for_whatsapp(msisdn):
    return msisdn.lstrip("+")


class TurnTransport(HttpRpcTransport):
    CONFIG_CLASS = TurnTransportConfig

    def respond(self, message_id, code, body=None):
        if body is None:
            body = {}

        self.finish_request(message_id, json.dumps(body), code=code)

    def send_message(self, message):
        return treq.post(
            self.config['outbound_url'],
            self.get_send_params(message),
            headers={
                'User-Agent': ['Vumi Turn Transport'],
                'Content-Type': ['application/json'],
                'Authorization': ['Bearer {}'.format(self.config['token'])],
            },
            timeout=self.config.get('outbound_request_timeout')
        )

    def get_send_params(self, message):
        params = {
            "preview_url": False,
            "recipient_type": "individual",
            "to": format_msisdn_for_whatsapp(message['to_addr']),
            "type": "text",
            "text": {"body": message['content']}
        }

        return json.dumps(params).encode('ascii')


    @inlineCallbacks
    def handle_raw_inbound_message(self, message_id, request):
        try:
            # TODO: validate HMAC secret

            for message in request.args.get("messages", []):
                content = ''
                metadata = {}
                if message['type'] == 'text':
                    content = message["text"]["body"]
                elif message['type'] == 'location':
                    loc = message["location"]
                    metadata["location"] = 'geo:{},{}'.format(loc['latitude'], loc['longitude'])

                yield self.publish_message(
                    transport_name='turn',
                    transport_type='sms',
                    message_id=message["id"],
                    transport_metadata=metadata,
                    to_addr=format_msisdn_for_whatsapp(self.config['to_addr']),
                    from_addr=format_msisdn_for_whatsapp(message['from']),
                    content=content,
                    timestamp= get_datetime(message["timestamp"]),
                    )
                log.msg("Inbound Enqueued.")

            for event in request.args.get("statuses", []):
                if event["status"] == 'sent':
                    delivery_status = 'pending'
                elif event["status"] == 'failed':
                    delivery_status = 'failed'
                elif status in ['delivered', 'read']:
                    delivery_status = 'delivered'
                else:
                    continue

                yield self.publish_delivery_report(
                    user_message_id=event['id'],
                    delivery_status=delivery_status,
                    transport_metadata={
                        'delivery_status': event["status"],
                    },
                    to_addr=format_msisdn_for_whatsapp(event['recipient_id']),
                    timestamp=get_datetime(event['timestamp']),
                    )
                log.msg("Event Enqueued.")
        except KeyError, e:
            msg = ("Need more request keys to complete this request. \n\n"
                   "Missing request key: %s" % (e,))
            log.msg('Returning %s: %s' % (http.BAD_REQUEST, msg))

            self.respond(message_id, http.BAD_REQUEST, {"error": msg})
        except ValueError, e:
            msg = "ValueError: %s" % e
            log.msg('Returning %s: %s' % (http.BAD_REQUEST, msg))
            self.respond(message_id, http.BAD_REQUEST, {"error": msg})
        except Exception, e:
            log.err("Error processing request: %s" % (request,))
            self.respond(message_id, http.INTERNAL_SERVER_ERROR, {"error": msg})

        self.respond(message_id, http.OK, {})

    @inlineCallbacks
    def handle_outbound_message(self, message):
        """
        handle messages arriving over AMQP meant for delivery via turn
        """
        try:
            resp = yield self.send_message(message)
        except (ResponseNeverReceived, ConnectingCancelledError,
                CancelledError):
            yield self.handle_send_timeout(message)
            return

        content = yield resp.content()

        self.emit('Turn response for %s: %s, status: %s' % (
            message['message_id'], content, content))

        if resp.code == http.OK:
            yield self.handle_outbound_success(message)
        else:
            yield self.handle_outbound_fail(message, content)

    @inlineCallbacks
    def handle_send_timeout(self, message):
        self.emit('Timing out: %s' % (message,))
        yield self.publish_nack(
            user_message_id=message['message_id'],
            sent_message_id=message['message_id'],
            reason='Request timeout')

        yield self.add_status(
            component='outbound',
            status='down',
            type='request_timeout',
            message='Request timeout')

    @inlineCallbacks
    def handle_outbound_success(self, message):
        self.emit('Outbound success: %s' % (message,))
        yield self.publish_ack(
            user_message_id=message['message_id'],
            sent_message_id=message['message_id'])

        yield self.add_status(
            component='outbound',
            status='ok',
            type='request_success',
            message='Request successful')

    @inlineCallbacks
    def handle_outbound_fail(self, message, status):
        self.emit('Outbound fail: %s' % (message,))
        yield self.publish_nack(
            user_message_id=message['message_id'],
            sent_message_id=message['message_id'],
            reason=status)

        yield self.add_status(
            component='outbound',
            status='down',
            type="request_failed",
            message=status)

