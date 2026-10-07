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
"""Tests for the optional observer of unknown JSON-RPC notifications.

The fixtures here are intentionally in-memory only: a fake server, an
in-memory writer and messages driven directly through ``handle_message``.
No subprocesses, sockets or filesystem workspace are required.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pygls.protocol import JsonRPCProtocol, default_converter

UNKNOWN_NOTIFICATION = "example/unknownNotification"
UNKNOWN_REQUEST = "example/unknownRequest"
KNOWN_REQUEST = "example/knownRequest"
KNOWN_NOTIFICATION = "example/knownNotification"


class MemoryWriter:
    """Minimal writer: records everything the protocol sends."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def write(self, data: bytes) -> int:
        self.messages.append(json.loads(data.decode("utf-8")))
        return len(data)

    def close(self):  # pragma: no cover - not exercised by these tests
        return None


class FakeServer:
    """The small surface of ``JsonRPCServer`` that the protocol touches."""

    def __init__(self) -> None:
        self.reported_errors: list[Exception] = []

    def _report_server_error(self, error: Exception, source: Any) -> None:
        self.reported_errors.append(error)


# Shared inbox for the (decorated) known notification handler.
known_notifications: list[Any] = []


def _make_protocol(
    observer=None,
) -> tuple[JsonRPCProtocol, FakeServer, MemoryWriter]:
    server = FakeServer()
    protocol = JsonRPCProtocol(
        server,
        default_converter(),
        unknown_notification_callback=observer,
    )

    @protocol.fm.feature(KNOWN_REQUEST)
    def handle_known_request(params):
        # Deterministic payload so this fixture does not depend on how pygls'
        # fallback message types represent unregistered request params.
        return {"status": "ok"}

    @protocol.fm.feature(KNOWN_NOTIFICATION)
    def handle_known_notification(params):
        known_notifications.append(params)

    writer = MemoryWriter()
    protocol.set_writer(writer, include_headers=False)
    return protocol, server, writer


def _notification(method: str, params: Any = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params}


def _request(msg_id: Any, method: str, params: Any = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}


def _feed(protocol: JsonRPCProtocol, raw: dict[str, Any]) -> Any:
    message = protocol.structure_message(raw)
    return protocol.handle_message(message)


def test_unknown_notification_request_and_normal_request_in_order(caplog):
    """Acceptance sequence: unknown notification, unknown request, normal request."""
    seen: list[tuple[str, Any]] = []
    protocol, _server, writer = _make_protocol(
        observer=lambda method, params: seen.append((method, params))
    )
    known_notifications.clear()

    unknown_params = {"value": 42}
    request_params = {"hello": "world"}

    caplog.set_level(logging.WARNING)

    # 1. Unknown notification -> observer is called, nothing is sent back.
    _feed(protocol, _notification(UNKNOWN_NOTIFICATION, unknown_params))

    assert seen == [(UNKNOWN_NOTIFICATION, unknown_params)]
    assert writer.messages == []

    # 2. Unknown request -> existing error flow: method-not-found response.
    _feed(protocol, _request("req-1", UNKNOWN_REQUEST))

    assert len(writer.messages) == 1
    error_message = writer.messages[0]
    assert error_message["jsonrpc"] == "2.0"
    assert error_message["id"] == "req-1"
    assert error_message["error"]["code"] == -32601
    assert "result" not in error_message

    # 3. Normal request -> normal success response, unaffected by earlier frames.
    _feed(protocol, _request("req-2", KNOWN_REQUEST, request_params))

    assert len(writer.messages) == 2
    success_message = writer.messages[1]
    assert success_message == {
        "jsonrpc": "2.0",
        "id": "req-2",
        "result": {"status": "ok"},
    }

    # The observer was only ever called for the unknown notification.
    assert seen == [(UNKNOWN_NOTIFICATION, unknown_params)]


def test_observer_exception_does_not_break_later_frames(caplog):
    """An observer raising must not prevent later frames from being handled."""
    calls = {"count": 0}

    def broken_observer(method: str, params: Any) -> None:
        calls["count"] += 1
        raise RuntimeError("observer boom")

    protocol, _server, writer = _make_protocol(observer=broken_observer)
    caplog.set_level(logging.ERROR)

    # First unknown notification: the observer explodes...
    _feed(protocol, _notification(UNKNOWN_NOTIFICATION, {"n": 1}))
    # ...but no response must be sent for a notification.
    assert writer.messages == []
    assert calls["count"] == 1
    assert any("observer raised an exception" in r.message for r in caplog.records)

    # A second unknown notification still reaches the observer.
    _feed(protocol, _notification(UNKNOWN_NOTIFICATION, {"n": 2}))
    assert calls["count"] == 2
    assert writer.messages == []

    # An unknown request afterwards still gets its method-not-found error.
    _feed(protocol, _request(1, UNKNOWN_REQUEST))
    assert len(writer.messages) == 1
    assert writer.messages[0]["id"] == 1
    assert writer.messages[0]["error"]["code"] == -32601

    # A normal request afterwards still produces its normal response.
    _feed(protocol, _request(2, KNOWN_REQUEST, {"ok": True}))
    assert writer.messages[1] == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"status": "ok"},
    }


def test_known_notifications_and_requests_do_not_invoke_observer():
    """The observer fires only for methods without a registered handler."""
    seen: list[tuple[str, Any]] = []
    protocol, _server, writer = _make_protocol(
        observer=lambda method, params: seen.append((method, params))
    )
    known_notifications.clear()

    _feed(protocol, _notification(KNOWN_NOTIFICATION, {"a": 1}))
    assert known_notifications == [{"a": 1}]
    assert seen == []

    _feed(protocol, _request("id", KNOWN_REQUEST, {"b": 2}))
    assert seen == []
    assert writer.messages == [
        {"jsonrpc": "2.0", "id": "id", "result": {"status": "ok"}}
    ]


def test_observer_receives_none_when_params_absent():
    """When the entry point is called without params, ``None`` is passed on."""
    seen: list[tuple[str, Any]] = []
    protocol, _server, writer = _make_protocol(
        observer=lambda method, params: seen.append((method, params))
    )

    # pygls' deserialization always populates ``params``; this drives the
    # notification entry point directly to cover a missing payload.
    protocol._handle_notification(UNKNOWN_NOTIFICATION, None)
    assert seen == [(UNKNOWN_NOTIFICATION, None)]
    assert writer.messages == []


def test_observer_can_be_set_after_construction_and_cleared(caplog):
    """The setter registers and removes the observer at runtime."""
    protocol, _server, writer = _make_protocol(observer=None)
    caplog.set_level(logging.WARNING)

    # No observer: default behavior is to log and ignore, nothing is sent.
    _feed(protocol, _notification(UNKNOWN_NOTIFICATION))
    assert any(
        "Ignoring notification for unknown method" in r.message for r in caplog.records
    )
    assert writer.messages == []

    seen: list[tuple[str, Any]] = []
    protocol.set_unknown_notification_callback(
        lambda method, params: seen.append((method, params))
    )
    _feed(protocol, _notification(UNKNOWN_NOTIFICATION, {"late": True}))
    assert seen == [(UNKNOWN_NOTIFICATION, {"late": True})]

    # Clearing the observer restores the default log-and-ignore behavior.
    protocol.set_unknown_notification_callback(None)
    _feed(protocol, _notification(UNKNOWN_NOTIFICATION))
    assert seen == [(UNKNOWN_NOTIFICATION, {"late": True})]


def test_default_interface_regression_no_observer():
    """Without the new option the original interface/behavior is preserved."""
    # Constructed with exactly the old two-argument signature.
    protocol = JsonRPCProtocol(None, default_converter())
    assert protocol._unknown_notification_callback is None

    protocol, server, writer = _make_protocol(observer=None)

    # Unknown notification: ignored, no outbound message.
    _feed(protocol, _notification(UNKNOWN_NOTIFICATION, {"ignored": True}))
    assert writer.messages == []

    # Unknown request: method-not-found error response, error reported.
    _feed(protocol, _request("r", UNKNOWN_REQUEST))
    assert writer.messages[0]["error"]["code"] == -32601
    assert len(server.reported_errors) == 1

    # Known request: success.
    _feed(protocol, _request("s", KNOWN_REQUEST, 1))
    assert writer.messages[1] == {
        "jsonrpc": "2.0",
        "id": "s",
        "result": {"status": "ok"},
    }
