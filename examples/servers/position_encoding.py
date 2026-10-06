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
"""This server demonstrates the position encoding session snapshot.

During ``initialize``, pygls negotiates a position encoding based on the
encodings the client declares in its capabilities and stores a snapshot of it
on the protocol (``ls.protocol.position_codec``). The snapshot is independent
of the capabilities objects exchanged during initialization and is used to
interpret positions for the remainder of the session.

This server echoes the negotiated encoding and the hover position converted
to the server's internal (UTF-32) representation.
"""

import logging

from lsprotocol import types

from pygls.cli import start_server
from pygls.lsp.server import LanguageServer

server = LanguageServer("position-encoding-server", "v1")


@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover(ls: LanguageServer, params: types.HoverParams):
    document = ls.workspace.get_text_document(params.text_document.uri)

    # The position encoding snapshot negotiated for this session.
    codec = ls.protocol.position_codec
    encoding = getattr(codec.encoding, "value", codec.encoding)

    # Interpret the client's position according to the negotiated encoding.
    position = codec.position_from_client_units(document.lines, params.position)

    return types.Hover(
        contents=types.MarkupContent(
            kind=types.MarkupKind.Markdown,
            value="\n".join(
                [
                    f"- encoding: `{encoding}`",
                    f"- position: `{position}`",
                ]
            ),
        )
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    start_server(server)
