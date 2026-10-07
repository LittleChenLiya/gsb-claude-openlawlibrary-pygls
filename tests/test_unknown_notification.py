############################################################################
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
############################################################################
"""Tests for the optional unknown-notification observer on JsonRPCProtocol."""
import io
import json

from pygls.protocol import JsonRPCProtocol, default_converter

KNOWN_REQUEST = "example/request"
UNKNOWN_NOTIFICATION = "example/unknownNotification"
UNKNOWN_REQUEST = "example/unknownRequest"


class FakeServer:
    """Minimal stand-in so error reporting does not touch a real server."""

    def __init__(self):
        self.errors = []

    def _report_server_error(self, error, source):
        self.errors.append((error, source))


def make_protocol(observer=None):
    server = FakeServer()
    protocol = JsonRPCProtocol(server, default_converter(),
                               notification_observer=observer)
    buffer = io.BytesIO()
    protocol.set_writer(buffer, include_headers=False)

    @protocol.fm.feature(KNOWN_REQUEST)
    def _(params):
        return {"echo": params}

    return protocol, buffer, server


def feed(protocol, payload: dict):
    """Deserialize and handle a single incoming frame."""
    message = json.loads(json.dumps(payload),
                         object_hook=protocol.structure_message)
    protocol.handle_message(message)


def drain(buffer) -> list:
    """Decode every JSON message written to the buffer so far."""
    data = buffer.getvalue().decode("utf-8")
    buffer.seek(0)
    buffer.truncate(0)

    decoder = json.JSONDecoder()
    messages = []
    idx = 0
    while idx < len(data):
        while idx < len(data) and data[idx].isspace():
            idx += 1
        if idx >= len(data):
            break
        message, idx = decoder.raw_decode(data, idx)
        messages.append(message)
    return messages


def test_unknown_notification_then_unknown_request_then_known_request():
    observed = []
    protocol, buffer, server = make_protocol(
        observer=lambda method, params: observed.append((method, params))
    )

    # 1. Unknown notification: observer fires, no response is produced.
    feed(protocol, {
        "jsonrpc": "2.0",
        "method": UNKNOWN_NOTIFICATION,
        "params": {"field_a": "test_a"},
    })
    assert drain(buffer) == []
    assert len(observed) == 1
    method, params = observed[0]
    assert method == UNKNOWN_NOTIFICATION
    assert params.field_a == "test_a"  # raw params, as received

    # 2. Unknown request: original error flow (method-not-found response).
    feed(protocol, {
        "jsonrpc": "2.0",
        "id": "req-unknown",
        "method": UNKNOWN_REQUEST,
        "params": None,
    })
    (error_response,) = drain(buffer)
    assert error_response["id"] == "req-unknown"
    assert error_response["error"]["code"] == -32601  # MethodNotFound
    assert len(observed) == 1  # observer is for notifications only

    # 3. Known request: handled normally.
    feed(protocol, {
        "jsonrpc": "2.0",
        "id": "req-known",
        "method": KNOWN_REQUEST,
        "params": 1,
    })
    (ok_response,) = drain(buffer)
    assert ok_response == {
        "jsonrpc": "2.0",
        "id": "req-known",
        "result": {"echo": 1},
    }


def test_throwing_observer_does_not_break_subsequent_frames():
    def bad_observer(method, params):
        raise RuntimeError("observer exploded")

    protocol, buffer, server = make_protocol(observer=bad_observer)

    feed(protocol, {
        "jsonrpc": "2.0",
        "method": UNKNOWN_NOTIFICATION,
        "params": {"x": 1},
    })
    assert drain(buffer) == []  # still no response for a notification

    # The frame after the exploding observer is processed normally.
    feed(protocol, {
        "jsonrpc": "2.0",
        "id": "after-error",
        "method": KNOWN_REQUEST,
        "params": 2,
    })
    (ok_response,) = drain(buffer)
    assert ok_response["id"] == "after-error"
    assert ok_response["result"] == {"echo": 2}


def test_default_protocol_without_observer_regression():
    """Without the new option the original behaviour is unchanged."""
    protocol, buffer, server = make_protocol()
    assert protocol._notification_observer is None

    # Unknown notification: silently ignored, nothing sent.
    feed(protocol, {
        "jsonrpc": "2.0",
        "method": UNKNOWN_NOTIFICATION,
        "params": {"field_a": "test_a"},
    })
    assert drain(buffer) == []

    # Unknown request: still the original error flow.
    feed(protocol, {
        "jsonrpc": "2.0",
        "id": "req-unknown",
        "method": UNKNOWN_REQUEST,
        "params": None,
    })
    (error_response,) = drain(buffer)
    assert error_response["error"]["code"] == -32601

    # Known request: normal response.
    feed(protocol, {
        "jsonrpc": "2.0",
        "id": "req-known",
        "method": KNOWN_REQUEST,
        "params": None,
    })
    (ok_response,) = drain(buffer)
    assert ok_response["result"] == {"echo": None}
