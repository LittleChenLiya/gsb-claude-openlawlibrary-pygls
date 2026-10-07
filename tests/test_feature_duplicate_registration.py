"""Tests for rejecting duplicate LSP handler registrations.

Covers the new ``allow_override=False`` duplicate protection on dynamic
feature registration:

- builtin protocol methods and user defined methods share the same conflict
  rule,
- the default registration policy is unchanged,
- a rejected registration leaves the previous handler in place and working.
"""

from __future__ import annotations

import pytest
from lsprotocol import converters, types as lsp

from pygls.exceptions import FeatureAlreadyRegisteredError
from pygls.feature_manager import FeatureManager
from pygls.lsp.server import LanguageServer
from pygls.protocol.json_rpc import JsonRPCRequestMessage

# ---------------------------------------------------------------------------
# FeatureManager level checks
# ---------------------------------------------------------------------------


@pytest.fixture
def fm() -> FeatureManager:
    return FeatureManager(None, converters.get_converter())


def test_reject_duplicate_user_method(fm: FeatureManager):
    """Two user handlers on the same method: strict policy rejects the second."""

    @fm.feature("custom/userMethod")
    def first():
        return "first"

    with pytest.raises(FeatureAlreadyRegisteredError) as exc_info:

        @fm.feature("custom/userMethod", allow_override=False)
        def second():  # pragma: no cover - never registered
            return "second"

    assert "custom/userMethod" in repr(exc_info.value)

    # Old handler is still there and executable.
    assert fm.features["custom/userMethod"]() == "first"


def test_reject_duplicate_builtin_method(fm: FeatureManager):
    """A handler already registered for a builtin method is a conflict too."""
    fm.add_builtin_feature("builtin/method", lambda: "builtin")

    with pytest.raises(FeatureAlreadyRegisteredError):

        @fm.feature("builtin/method", allow_override=False)
        def override():  # pragma: no cover - never registered
            return "override"

    # Nothing was written to the user registry...
    assert "builtin/method" not in fm.features
    # ...and the builtin handler still runs.
    assert fm.builtin_features["builtin/method"]() == "builtin"


def test_strict_policy_allows_new_methods(fm: FeatureManager):
    """Strict mode does not affect methods without an existing handler."""

    @fm.feature("custom/newMethod", allow_override=False)
    def handler():
        return "new"

    assert fm.features["custom/newMethod"]() == "new"


def test_allow_override_true_replaces_user_handler(fm: FeatureManager):
    """``allow_override=True`` lets a later handler win."""

    @fm.feature("custom/method")
    def first():
        return "first"

    @fm.feature("custom/method", allow_override=True)
    def second():
        return "second"

    assert fm.features["custom/method"]() == "second"


def test_default_policy_unchanged(fm: FeatureManager):
    """Default behaviour: user handlers augment builtins, duplicates still error."""

    # Builtin + user: allowed by default (they cooperate at dispatch time).
    fm.add_builtin_feature(lsp.SHUTDOWN, lambda: "builtin")

    @fm.feature(lsp.SHUTDOWN)
    def user_shutdown():
        return "user"

    assert fm.features[lsp.SHUTDOWN]() == "user"

    # Two user handlers: still rejected by default.
    with pytest.raises(FeatureAlreadyRegisteredError):

        @fm.feature(lsp.SHUTDOWN)
        def user_shutdown_again():  # pragma: no cover - never registered
            return "user-again"

    # First user handler survived the failed attempt.
    assert fm.features[lsp.SHUTDOWN]() == "user"


# ---------------------------------------------------------------------------
# Short regression example on the original public interface (LanguageServer)
# ---------------------------------------------------------------------------


class _RecordingWriter:
    """Minimal in-memory writer used to observe JSON-RPC responses."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def write(self, data: bytes) -> None:
        self.messages.append(data.decode("utf-8"))

    def close(self) -> None:  # pragma: no cover - not exercised
        pass


def _request(server: LanguageServer, msg_id: int, method: str) -> None:
    server.protocol.handle_message(
        JsonRPCRequestMessage(
            id=msg_id,
            method=method,
            params=None,
            jsonrpc="2.0",
        )
    )


def test_public_api_regression_duplicate_protection():
    """End-to-end (in-process) regression on ``LanguageServer.feature``.

    * conflicts are raised for a user method and for a builtin method,
    * after each failure the old handler still serves the response,
    * the default registration behaviour is unchanged.
    """
    import json

    server = LanguageServer("dup-registration-test", "v0")
    writer = _RecordingWriter()
    server.protocol.set_writer(writer, include_headers=False)

    # 1. User method conflict ----------------------------------------------
    @server.feature("custom/echo")
    def echo(params):
        return "old"

    with pytest.raises(FeatureAlreadyRegisteredError):

        @server.feature("custom/echo", allow_override=False)
        def echo_new(params):  # pragma: no cover
            return "new"

    _request(server, 2, "custom/echo")
    response = json.loads(writer.messages[-1])
    assert response == {"jsonrpc": "2.0", "id": 2, "result": "old"}

    # 2. Default registration behaviour unchanged --------------------------
    @server.feature("custom/default")
    def default_one(params):
        return "one"

    with pytest.raises(FeatureAlreadyRegisteredError):

        @server.feature("custom/default")
        def default_two(params):  # pragma: no cover
            return "two"

    _request(server, 3, "custom/default")
    response = json.loads(writer.messages[-1])
    assert response == {"jsonrpc": "2.0", "id": 3, "result": "one"}

    # 3. Builtin method conflict (checked last: shutdown flips the protocol
    #    into its "no more requests" state) --------------------------------
    with pytest.raises(FeatureAlreadyRegisteredError):

        @server.feature(lsp.SHUTDOWN, allow_override=False)
        def strict_shutdown(params):  # pragma: no cover
            return "should-not-be-registered"

    # No user handler was registered; the builtin shutdown handler still
    # serves the request and returns ``None``.
    assert lsp.SHUTDOWN not in server.protocol.fm.features
    _request(server, 1, lsp.SHUTDOWN)
    response = json.loads(writer.messages[-1])
    assert response == {"jsonrpc": "2.0", "id": 1, "result": None}
    assert server.protocol._shutdown is True
