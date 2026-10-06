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
"""Tests for the position encoding session snapshot taken during ``initialize``.

These tests use an in-memory (pipe) transport between a client and server
running in the same process.
"""

import pytest
from lsprotocol import types

from pygls.exceptions import JsonRpcInternalError
from pygls.lsp.server import LanguageServer

from ..ls_setup import CALL_TIMEOUT, ClientServer

DOC_URI = "file:///test.txt"
# 'x="😋"' - the emoji requires 4 UTF-8 code units / 2 UTF-16 code units.
DOC_TEXT = 'x="😋"\n'

# Character offset 6 in the client's encoding:
#   UTF-8  -> just before the closing quote (UTF-32 index 4)
#   UTF-16 -> just after the closing quote  (UTF-32 index 5)
HOVER_POSITION = types.Position(line=0, character=6)


def make_hover_server(server):
    @server.feature(types.TEXT_DOCUMENT_HOVER)
    def hover(ls: LanguageServer, params: types.HoverParams):
        document = ls.workspace.get_text_document(params.text_document.uri)

        # Interpret the client's position using the session's codec snapshot.
        position = ls.protocol.position_codec.position_from_client_units(
            document.lines, params.position
        )
        return types.Hover(
            contents=types.MarkupContent(
                kind=types.MarkupKind.PlainText,
                value=f"{position.line}:{position.character}",
            )
        )


def open_document(client):
    client.protocol.notify(
        types.TEXT_DOCUMENT_DID_OPEN,
        types.DidOpenTextDocumentParams(
            text_document=types.TextDocumentItem(
                uri=DOC_URI, language_id="text", version=1, text=DOC_TEXT
            )
        ),
    )


def request_hover(client):
    return client.protocol.send_request(
        types.TEXT_DOCUMENT_HOVER,
        types.HoverParams(
            text_document=types.TextDocumentIdentifier(uri=DOC_URI),
            position=HOVER_POSITION,
        ),
    ).result(timeout=CALL_TIMEOUT)


class Utf8ClientServer(ClientServer):
    """Client/server pair (in-memory transport) where the client declares
    support for UTF-8 positions only."""

    def __init__(self):
        super().__init__()
        make_hover_server(self.server)

    def initialize(self):
        initialize_params = types.InitializeParams(
            process_id=12345,
            root_uri="file://",
            capabilities=types.ClientCapabilities(
                general=types.GeneralClientCapabilities(
                    position_encodings=[types.PositionEncodingKind.Utf8]
                )
            ),
        )
        response = self.client.protocol.send_request(
            types.INITIALIZE, initialize_params
        ).result(timeout=CALL_TIMEOUT)
        assert response.capabilities is not None


class FailingInitializeClientServer(ClientServer):
    """Client/server pair (in-memory transport) where the server's
    ``initialize`` handler always fails."""

    def __init__(self):
        super().__init__()

        @self.server.feature(types.INITIALIZE)
        def initialize(params: types.InitializeParams):
            raise RuntimeError("initialize failed")

        make_hover_server(self.server)

    def initialize(self):
        with pytest.raises(JsonRpcInternalError, match="initialize failed"):
            self.client.protocol.send_request(
                types.INITIALIZE,
                types.InitializeParams(
                    process_id=12345,
                    root_uri="file://",
                    capabilities=types.ClientCapabilities(),
                ),
            ).result(timeout=CALL_TIMEOUT)


@Utf8ClientServer.decorate()
def test_utf8_only_declaration_negotiates_utf8(client_server):
    client, server = client_server

    # The negotiated encoding is reported to the client...
    assert (
        server.protocol.server_capabilities.position_encoding
        == types.PositionEncodingKind.Utf8
    )

    # ...and captured in the session snapshot used for positioning.
    assert server.protocol.position_codec.encoding == types.PositionEncodingKind.Utf8
    assert server.workspace.position_encoding == types.PositionEncodingKind.Utf8

    open_document(client)
    hover = request_hover(client)

    # Positions are interpreted as UTF-8 code units.
    assert hover.contents.value == "0:4"


@Utf8ClientServer.decorate()
def test_session_snapshot_independent_of_capabilities_objects(client_server):
    client, server = client_server

    open_document(client)
    assert request_hover(client).contents.value == "0:4"

    # Modify the original client declaration...
    general = server.protocol.client_capabilities.general
    general.position_encodings = [types.PositionEncodingKind.Utf16]

    # ...and the server's advertised capabilities object.
    server.protocol.server_capabilities.position_encoding = (
        types.PositionEncodingKind.Utf16
    )

    # The session snapshot is unaffected.
    assert server.protocol.position_codec.encoding == types.PositionEncodingKind.Utf8
    assert server.workspace.position_encoding == types.PositionEncodingKind.Utf8
    assert request_hover(client).contents.value == "0:4"


def test_default_position_encoding_is_utf16(client_server):
    """Clients that do not declare any encodings keep the UTF-16 default."""
    _, server = client_server

    assert server.protocol.position_codec.encoding == types.PositionEncodingKind.Utf16
    assert server.workspace.position_encoding == types.PositionEncodingKind.Utf16


@FailingInitializeClientServer.decorate()
def test_failed_initialize_leaves_no_ready_state(client_server):
    client, server = client_server

    # No session snapshot or workspace may survive a failed initialize.
    with pytest.raises(RuntimeError, match="has the server been initialized"):
        server.protocol.position_codec

    with pytest.raises(RuntimeError, match="has the server been initialized"):
        server.protocol.workspace

    # Requests that depend on positioning are rejected.
    with pytest.raises(JsonRpcInternalError):
        request_hover(client)
