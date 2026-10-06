#############################################################################
# Copyright(c) Open Law Library. All rights reserved.                      #
# See ThirdPartyNotices.txt in the project root for additional notices.    #
#                                                                          #
# Licensed under the Apache License, Version 2.0 (the "License")           #
# you may not use this file except in compliance with the License.         #
# You may obtain a copy of the License at                                  #
#                                                                          #
#     http://www.apache.org/licenses/LICENSE-2.0                           #
#                                                                          #
# Unless required by applicable law or agreed to in writing, software      #
# distributed under the License is distributed on an "AS IS" BASIS,        #
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. #
# See the License for the specific language governing permissions and      #
# limitations under the License.                                           #
#############################################################################
"""Regression tests for issue #621: a response arriving after its request
future was cancelled must not crash the message handler with
``InvalidStateError``."""
import json
from concurrent.futures import Future
from functools import partial

import attrs
import pytest
from lsprotocol.types import CANCEL_REQUEST, CancelNotification, CancelParams, ResponseError

from pygls.exceptions import JsonRpcRequestCancelled
from pygls.protocol import default_converter
from pygls.protocol.json_rpc import JsonRPCProtocol

EXAMPLE_REQUEST = "example/request"


@attrs.define
class ExampleParams:
    field_a: str


@attrs.define
class ExampleRequest:
    """A generic request message example, using an integer id (``0``)."""

    id: int
    jsonrpc: str = attrs.field(default="2.0")
    method: str = EXAMPLE_REQUEST
    params: ExampleParams = attrs.field(default=None)


class ExampleProtocol(JsonRPCProtocol):
    def get_message_type(self, method: str):
        if method == EXAMPLE_REQUEST:
            return ExampleRequest
        return None


class RecordingWriter:
    """A writer that records each sent message as a separate string."""

    def __init__(self):
        self.messages = []

    def write(self, data):
        self.messages.append(data.decode("utf-8"))
        return len(data)


@pytest.fixture()
def protocol() -> ExampleProtocol:
    p = ExampleProtocol(None, default_converter())
    p.set_writer(RecordingWriter(), include_headers=False)
    return p


def _sent_messages(protocol: ExampleProtocol):
    return [json.loads(m) for m in protocol.writer.messages]


def _make_controllable_future(protocol: ExampleProtocol, msg_id: int) -> Future:
    """Create a controllable future for ``msg_id``.

    The future is put in the RUNNING state so that ``cancel()`` returns
    ``False``, mimicking a request handler that is already executing and
    therefore cannot be cancelled.
    """
    future: Future = Future()
    future.set_running_or_notify_cancel()
    protocol._request_futures[msg_id] = future
    future.add_done_callback(partial(protocol._send_handler_result, msg_id=msg_id))
    return future


def _make_protocol() -> JsonRPCProtocol:
    """Build a protocol without running __init__ (no server needed):
    _handle_response only touches _request_futures."""
    protocol = JsonRPCProtocol.__new__(JsonRPCProtocol)
    protocol._request_futures = {}
    return protocol


def test_response_to_cancelled_request_future_is_ignored():
    protocol = _make_protocol()
    future: Future = Future()
    protocol._request_futures[1] = future
    # The awaiting task got cancelled (e.g. a debounced server->client request).
    assert future.cancel()

    # A late response must not raise InvalidStateError.
    protocol._handle_response(1, result={"ok": True})
    # ...and the bookkeeping entry must be cleaned up.
    assert 1 not in protocol._request_futures


def test_error_response_to_cancelled_request_future_is_ignored():
    protocol = _make_protocol()
    future: Future = Future()
    protocol._request_futures[2] = future
    assert future.cancel()

    # Error responses go through the same path via set_exception.
    protocol._handle_response(
        2, error=ResponseError(code=-32601, message="method not found")
    )
    assert 2 not in protocol._request_futures


def test_response_to_unknown_message_id_still_warns_not_raises():
    protocol = _make_protocol()
    # Existing behaviour: unknown ids are ignored (warning only).
    protocol._handle_response(99, result={"ok": True})
    assert protocol._request_futures == {}


def test_cancel_notification_arriving_before_result_publishes_one_cancel_response(
    protocol,
):
    """A cancel notification that arrives before the business result must
    publish exactly one cancel response; the late result must not be sent."""

    # A request with id 0 is in flight, but its handler hasn't finished yet.
    future = _make_controllable_future(protocol, 0)

    # The cancel notification arrives first.
    protocol._handle_cancel_notification(0)

    # Then the business result completes.
    future.set_result({"ok": True})

    messages = _sent_messages(protocol)
    assert len(messages) == 1, f"Expected one cancel response, got: {messages}"

    response = messages[0]
    assert response["id"] == 0
    assert "error" in response
    assert response["error"]["code"] == JsonRpcRequestCancelled.CODE
    assert "result" not in response


def test_normal_request_still_publishes_result_once(protocol):
    """A normal request without cancellation still publishes its result once."""

    future = _make_controllable_future(protocol, 1)

    # No cancel notification; the result completes normally.
    future.set_result({"ok": True})

    messages = _sent_messages(protocol)
    assert len(messages) == 1, f"Expected one result response, got: {messages}"

    response = messages[0]
    assert response["id"] == 1
    assert "result" in response
    assert response["result"] == {"ok": True}
    assert "error" not in response


def test_example_request_and_cancel_notification_messages_are_structured(protocol):
    """The two message examples (a request and a cancel notification) are
    correctly structured by the protocol, including an integer id of ``0``."""

    # --- Example request message ---
    request_data = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": EXAMPLE_REQUEST,
        "params": {"fieldA": "hello"},
    }
    request = json.loads(json.dumps(request_data), object_hook=protocol.structure_message)
    assert isinstance(request, ExampleRequest)
    assert request.id == 0
    assert request.method == EXAMPLE_REQUEST
    assert request.params.field_a == "hello"

    # --- Example cancel notification message ---
    cancel = CancelNotification(params=CancelParams(id=0))
    assert cancel.method == CANCEL_REQUEST
    assert cancel.params.id == 0
    assert isinstance(cancel.params.id, int)

    # Feeding the cancel notification for the in-flight request through the
    # full message handler must publish exactly one cancel response.
    future = _make_controllable_future(protocol, 0)
    protocol.handle_message(cancel)
    future.set_result({"ok": True})

    messages = _sent_messages(protocol)
    assert len(messages) == 1
    assert messages[0]["id"] == 0
    assert messages[0]["error"]["code"] == JsonRpcRequestCancelled.CODE
