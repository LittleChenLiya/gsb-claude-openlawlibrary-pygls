#############################################################################
# Copyright(c) Open Law Library. All rights reserved.                      #
# See ThirdPartyNotices.txt in the project root for additional notices.    #
#                                                                          #
# Licensed under the Apache License, Version 2.0 (the "License")           #
# you may not use this file except in compliance with the License.         #
# You may obtain a copy of the License at                                  #
#                                                                          #
#     http: // www.apache.org/licenses/LICENSE-2.0                         #
#                                                                          #
# Unless required by applicable law or agreed to in writing, software      #
# distributed under the License is distributed on an "AS IS" BASIS,        #
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. #
# See the License for the specific language governing permissions and      #
# limitations under the License.                                           #
#############################################################################
"""Targeted tests for cancellation/result ownership in ``JsonRPCProtocol``.

When a ``$/cancelRequest`` notification arrives before the handler's result is
committed, the cancellation owns the response for that request id: exactly one
cancellation response may be published and a late handler result must not be
sent. Request id ``0`` is a valid id and must be handled like any other.
"""
import json
from concurrent.futures import Future
from functools import partial
from typing import Any, List
from unittest.mock import Mock

from pygls.protocol import JsonRPCProtocol, default_converter

# Example message 1: the client asks the server to cancel the request with
# id 0 (note: 0 is a valid request id).
CANCEL_REQUEST_ID_0 = {
    "jsonrpc": "2.0",
    "method": "$/cancelRequest",
    "params": {"id": 0},
}

# Example message 2: the single cancellation response the server publishes for
# that request. A late handler result must never be sent for the same id.
CANCELLED_RESPONSE_ID_0 = {
    "jsonrpc": "2.0",
    "id": 0,
    "error": {"code": -32800, "message": 'Request with id "0" is canceled'},
}


class FakeWriter:
    """Captures every JSON message the protocol sends."""

    def __init__(self):
        self.messages: List[dict] = []

    def write(self, data: bytes):
        self.messages.append(json.loads(data.decode("utf-8")))


def _make_protocol() -> JsonRPCProtocol:
    protocol = JsonRPCProtocol(server=Mock(), converter=default_converter())
    protocol.set_writer(FakeWriter(), include_headers=False)
    return protocol


def _register_request(protocol: JsonRPCProtocol, msg_id: Any, *, running: bool):
    """Register an in-flight request backed by a controllable future.

    Mirrors what ``_execute_handler`` does, but leaves the completion of the
    future under the test's control. With ``running=True`` the future is put
    into the RUNNING state so ``cancel()`` fails (the handler is already
    executing) while ``set_result`` remains possible.
    """
    future: Future = Future()
    if running:
        future.set_running_or_notify_cancel()
    protocol._request_futures[msg_id] = future
    future.add_done_callback(
        partial(protocol._send_handler_result, msg_id=msg_id)
    )
    return future


def _cancel(protocol: JsonRPCProtocol, msg_id: Any):
    """Feed a ``$/cancelRequest`` notification through the message layer."""
    message = protocol.structure_message(
        {**CANCEL_REQUEST_ID_0, "params": {"id": msg_id}}
    )
    protocol.handle_message(message)


def test_cancel_before_result_publishes_single_cancellation_response():
    """Cancel id 0 while the handler is running, then commit the result:
    exactly one cancellation response is published, the late result is not."""
    protocol = _make_protocol()
    writer = protocol.writer

    future = _register_request(protocol, 0, running=True)

    # The cancellation arrives before the business result is committed.
    _cancel(protocol, 0)
    future.set_result({"ok": True})

    assert writer.messages == [CANCELLED_RESPONSE_ID_0]
    assert 0 not in protocol._request_futures
    assert 0 not in protocol._cancelled_requests


def test_cancel_pending_request_publishes_single_cancellation_response():
    """Cancel id 0 before the handler started: the future is cancelled and
    exactly one cancellation response is published."""
    protocol = _make_protocol()
    writer = protocol.writer

    future = _register_request(protocol, 0, running=False)

    _cancel(protocol, 0)

    assert future.cancelled()
    assert writer.messages == [CANCELLED_RESPONSE_ID_0]
    assert 0 not in protocol._cancelled_requests


def test_normal_request_publishes_result_exactly_once():
    """Without cancellation, id 0 completes with a single result response."""
    protocol = _make_protocol()
    writer = protocol.writer

    future = _register_request(protocol, 0, running=True)
    future.set_result({"ok": True})

    assert writer.messages == [{"jsonrpc": "2.0", "id": 0, "result": {"ok": True}}]


def test_cancel_unknown_id_publishes_nothing():
    """A cancellation for an id that is not in flight owns no response."""
    protocol = _make_protocol()
    writer = protocol.writer

    _cancel(protocol, 0)

    assert writer.messages == []
    assert protocol._cancelled_requests == set()
