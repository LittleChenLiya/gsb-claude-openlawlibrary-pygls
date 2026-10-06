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
"""Tests for the per-session position encoding snapshot negotiated during
``initialize``."""

import pytest
from lsprotocol import types

from pygls.exceptions import JsonRpcInternalError

from ..conftest import ClientServer
from ..ls_setup import CALL_TIMEOUT

DOC_URI = "file:///position-encoding.txt"
# The emoji "\U0001F60B" is 4 UTF-8 code units, 2 UTF-16 code units and
# 1 UTF-32 code unit.
DOC_TEXT = 'x="\U0001F60B"\n'


class Utf8LS(ClientServer):
    """A client that declares support for UTF-8 positions only."""

    def __init__(self):
        super().__init__()
        server = self.server

        @server.feature(types.TEXT_DOCUMENT_HOVER)
        def hover(params: types.HoverParams):
            document = server.workspace.get_text_document(params.text_document.uri)
            position = document.position_codec.position_from_client_units(
                document.lines, params.position
            )
            return types.Hover(
                contents=types.MarkupContent(
                    kind=types.MarkupKind.PlainText,
                    value=f"{position.line}:{position.character}",
                )
            )

    def initialize(self):
        response = self.client.protocol.send_request(
            types.INITIALIZE,
            types.InitializeParams(
                process_id=12345,
                root_uri="file://",
                capabilities=types.ClientCapabilities(
                    general=types.GeneralClientCapabilities(
                        position_encodings=[types.PositionEncodingKind.Utf8]
                    )
                ),
            ),
        ).result(timeout=CALL_TIMEOUT)
        assert response.capabilities is not None


@Utf8LS.decorate()
def test_negotiates_utf8(client_server):
    """The negotiated encoding is snapshotted for the session."""
    _, server = client_server

    assert server.protocol.position_encoding == types.PositionEncodingKind.Utf8
    assert server.workspace.position_encoding == types.PositionEncodingKind.Utf8
    assert (
        server.server_capabilities.position_encoding == types.PositionEncodingKind.Utf8
    )


@Utf8LS.decorate()
def test_positions_are_interpreted_as_utf8(client_server):
    """Positions from the client are decoded according to the snapshot."""
    client, _ = client_server

    client.protocol.notify(
        types.TEXT_DOCUMENT_DID_OPEN,
        types.DidOpenTextDocumentParams(
            text_document=types.TextDocumentItem(
                uri=DOC_URI,
                language_id="plaintext",
                version=1,
                text=DOC_TEXT,
            )
        ),
    )

    # UTF-8 offset 6 lies 3 bytes into the emoji; under UTF-16, offset 6
    # would be past it (x, =, ", emoji(2 units), " -> 6 units).
    response = client.protocol.send_request(
        types.TEXT_DOCUMENT_HOVER,
        types.HoverParams(
            text_document=types.TextDocumentIdentifier(uri=DOC_URI),
            position=types.Position(line=0, character=6),
        ),
    ).result(timeout=CALL_TIMEOUT)

    # In UTF-32 (server native) units the position is line 0, character 4.
    assert response.contents.value == "0:4"


@Utf8LS.decorate()
def test_snapshot_is_independent_of_client_capabilities(client_server):
    """Modifying the original declaration must not affect the session."""
    _, server = client_server

    server.protocol.client_capabilities.general.position_encodings = [
        types.PositionEncodingKind.Utf32
    ]

    assert server.protocol.position_encoding == types.PositionEncodingKind.Utf8
    assert server.workspace.position_encoding == types.PositionEncodingKind.Utf8


def test_default_encoding_is_utf16(client_server):
    """The historical default is kept when the client declares nothing."""
    _, server = client_server

    assert server.protocol.position_encoding == types.PositionEncodingKind.Utf16
    assert server.workspace.position_encoding == types.PositionEncodingKind.Utf16


class FailingLS(ClientServer):
    """A server whose ``initialize`` handler always fails."""

    def __init__(self):
        super().__init__()
        server = self.server

        @server.feature(types.INITIALIZE)
        def initialize(params):
            raise RuntimeError("initialize failed")

        @server.feature(types.TEXT_DOCUMENT_HOVER)
        def hover(params: types.HoverParams):
            # A position-dependent request: it needs the session's workspace
            # and position encoding snapshot.
            document = server.workspace.get_text_document(params.text_document.uri)
            return types.Hover(contents=document.source or "")

    def start(self):
        # Do not initialize - the test drives the requests itself.
        self.server_thread.start()
        self.client_thread.start()


def test_failed_initialize_leaves_no_ready_state():
    """A failed ``initialize`` must not leave a ready state behind and
    position-dependent requests must be rejected."""
    client_server = FailingLS()
    client_server.start()
    client, server = client_server

    try:
        with pytest.raises(JsonRpcInternalError):
            client.protocol.send_request(
                types.INITIALIZE,
                types.InitializeParams(
                    process_id=12345,
                    root_uri="file://",
                    capabilities=types.ClientCapabilities(
                        general=types.GeneralClientCapabilities(
                            position_encodings=[types.PositionEncodingKind.Utf8]
                        )
                    ),
                ),
            ).result(timeout=CALL_TIMEOUT)

        # No ready state may remain after the failed initialization.
        with pytest.raises(RuntimeError):
            server.protocol.position_encoding
        with pytest.raises(RuntimeError):
            server.protocol.workspace

        # Position-dependent requests are rejected.
        with pytest.raises(JsonRpcInternalError):
            client.protocol.send_request(
                types.TEXT_DOCUMENT_HOVER,
                types.HoverParams(
                    text_document=types.TextDocumentIdentifier(uri=DOC_URI),
                    position=types.Position(line=0, character=0),
                ),
            ).result(timeout=CALL_TIMEOUT)
    finally:
        client_server.stop()
