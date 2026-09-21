"""The HTTP server's part of "local only": a refused request gets no answer at all.

An address outside the local network must learn nothing, not even that a web server is there: the connection is
reset, and the browser shows its own "this site can't be reached" page. The app decides (it knows the settings
and the path) and marks the request; this protocol, which holds the connection, swallows the answer the app
wrote and resets the connection instead. Under any other server (tests, `uvicorn --reload`) the app's answer, a
bare 403, goes out as is.
"""

import asyncio
import socket
import struct
from collections.abc import MutableMapping
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send
from uvicorn.protocols.http.h11_impl import H11Protocol

# The scope extension the app writes {"drop": True} into
DROP = "redirect.drop"


def request_drop(scope: Scope) -> bool:
    """Asks the server to reset this request's connection instead of answering. False where it cannot."""
    marker = scope.get("extensions", {}).get(DROP)
    if marker is None:
        return False
    marker["drop"] = True
    return True


def reset(transport: asyncio.Transport) -> None:
    """Closes with a TCP reset (SO_LINGER 0), as a port nobody listens on, rather than an orderly close."""
    sock = transport.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    transport.abort()


class DroppingH11Protocol(H11Protocol):
    """uvicorn's h11 protocol, with the DROP extension in every request's scope."""

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        super().connection_made(transport)  # type: ignore[arg-type]
        inner: ASGIApp = self.app

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            marker: MutableMapping[str, Any] = {}
            scope.setdefault("extensions", {})[DROP] = marker

            async def guarded_send(message: Message) -> None:
                if not marker.get("drop"):
                    await send(message)

            await inner(scope, receive, guarded_send)
            if marker.get("drop"):
                reset(self.transport)
                # Let the loss of the connection reach uvicorn first: it then sends nothing more (no 500)
                for _ in range(10):
                    if self.cycle is None or self.cycle.disconnected:
                        break
                    await asyncio.sleep(0)

        self.app = app
