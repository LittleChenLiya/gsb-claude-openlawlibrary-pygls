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
"""This demonstrates the per-session position encoding snapshot.

During ``initialize`` pygls negotiates a position encoding with the client
(based on the client's ``general.positionEncodings`` declaration) and takes a
snapshot of the result. The snapshot is available for the remainder of the
session via ``ls.protocol.position_encoding`` and is independent of the
client capabilities object - modifying that object after initialization does
not change the session's encoding.

This server answers ``textDocument/hover`` by reporting the negotiated
encoding and the position of the request converted to the server's native
(UTF-32) units.
"""

import logging

from lsprotocol import types

from pygls.cli import start_server
from pygls.lsp.server import LanguageServer

server = LanguageServer("position-encoding-server", "v1")


@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover(ls: LanguageServer, params: types.HoverParams):
    document = ls.workspace.get_text_document(params.text_document.uri)

    # Positions are decoded using the session's encoding snapshot.
    position = document.position_codec.position_from_client_units(
        document.lines, params.position
    )

    return types.Hover(
        contents=types.MarkupContent(
            kind=types.MarkupKind.PlainText,
            value=(
                f"encoding: {ls.protocol.position_encoding}\n"
                f"server position: {position.line}:{position.character}"
            ),
        )
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    start_server(server)
