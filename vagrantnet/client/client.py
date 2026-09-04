"""vagrantNet client: fetches pages/files/listings from a daemon over MeshCore."""

from __future__ import annotations

import asyncio
import logging
import random
import sys
import zlib
from dataclasses import dataclass

from meshcore import EventType, MeshCore

from ..common import chunking, compress
from ..common.envelope import (
    EnvelopeError,
    Request,
    Response,
    StatusCode,
    Subcommand,
)
from ..common.page import render_ansi

logger = logging.getLogger("vagrantnet.client")

CHUNK_TIMEOUT_SECONDS = 15.0
MAX_RETRIES_PER_CHUNK = 2

class VagrantNetError(RuntimeError):
    pass

@dataclass
class _PendingFetch:
    request_id: int
    future: "asyncio.Future[Response]"

class VagrantNetClient:
    def __init__(self, mc: MeshCore):
        self.mc = mc
        self._pending: dict[int, _PendingFetch] = {}
        self.mc.subscribe(EventType.RAW_DATA, self._on_raw_data)

    @classmethod
    # TODO: support BLE connections too + Support having vagrantNetClient and MeshCore cli/clients at the same time
    # (Maybe a middleware layer to avoid the /dev/ttyUSBX interface being locked by MeshCore CLI or vagrantNetClient)
    async def connect_serial(cls, port: str, baudrate: int = 115200) -> "VagrantNetClient":
        mc = await MeshCore.create_serial(port, baudrate)
        if mc is None:
            raise VagrantNetError(f"could not connect to MeshCore device at {port}")
        return cls(mc)

    async def _on_raw_data(self, event) -> None:
        raw_hex = event.payload.get("payload") if isinstance(event.payload, dict) else None
        if not raw_hex:
            return
        try:
            resp = Response.decode(bytes.fromhex(raw_hex))
        except EnvelopeError:
            return  # not a recognized frame -- ignore

        pending = self._pending.get(resp.request_id)
        if pending is not None and not pending.future.done():
            pending.future.set_result(resp)

    async def _resolve_path(self, server_pubkey_hex: str) -> bytes:
        contact = self.mc.get_contact_by_key_prefix(server_pubkey_hex)
        if contact is None:
            raise VagrantNetError(
                f"Server {server_pubkey_hex[:12]}... is not a known contact yet "
                " wait for server/repeater advert before fetching"
            )
        path_event = await self.mc.commands.get_advert_path(contact["public_key"])
        path_info = getattr(path_event, "payload", None) or {}
        if path_info.get("path_len", -1) < 0 or not path_info.get("path"):
            raise VagrantNetError(f"no known path was found for {server_pubkey_hex[:12]}")
        return bytes.fromhex(path_info["path"])

    def _own_pubkey_prefix(self) -> bytes:
        pk_hex = self.mc.self_info.get("public_key")
        if not pk_hex:
            raise VagrantNetError("user has no public_key, set-up and broadcast MeshCore first")
        return bytes.fromhex(pk_hex)[:6]

    async def _send_and_wait(
        self, server_path: bytes, req: Request
    ) -> Response:
        loop = asyncio.get_event_loop()
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES_PER_CHUNK + 1):
            fut: "asyncio.Future[Response]" = loop.create_future()
            self._pending[req.request_id] = _PendingFetch(req.request_id, fut)
            try:
                await self.mc.commands.send_raw_data(req.encode(), path=server_path)
                resp = await asyncio.wait_for(fut, timeout=CHUNK_TIMEOUT_SECONDS)
                return resp
            except asyncio.TimeoutError as e:
                last_error = e
                logger.warning(
                    "timeout waiting for chunk (request_id=%s, attempt %d/%d)",
                    req.request_id,
                    attempt + 1,
                    MAX_RETRIES_PER_CHUNK + 1,
                )
            finally:
                self._pending.pop(req.request_id, None)

        raise VagrantNetError(
            f"gave up after {MAX_RETRIES_PER_CHUNK + 1} attempts"
        ) from last_error

    async def fetch(
        self, server_pubkey_hex: str, subcommand: Subcommand, path: str = ""
    ) -> bytes:
        """Fetch a page/file/listing, following the CONTINUE chain until the
        final chunk, and return the fully reassembled, decompressed bytes."""
        server_path = await self._resolve_path(server_pubkey_hex)
        own_prefix = self._own_pubkey_prefix()

        req_id = random.randint(0, 0xFFFF)
        req = Request(
            request_id=req_id,
            client_pubkey_prefix=own_prefix,
            subcommand=subcommand,
            chunk_number=0,
            path=path,
        )
        resp = await self._send_and_wait(server_path, req)

        if resp.status != StatusCode.OK:
            raise VagrantNetError(f"server returned {resp.status.name} for {path!r}")

        chunks: dict[int, bytes] = {0: resp.payload}
        total_chunks = resp.total_chunks or 1
        uncompressed_size = resp.uncompressed_size or 0
        compressed = resp.compressed
        content_token = resp.content_token
        checksum = resp.checksum if resp.is_final else None

        chunk_n = 1
        while chunk_n < total_chunks:
            cont_req_id = random.randint(0, 0xFFFF)
            cont_req = Request(
                request_id=cont_req_id,
                client_pubkey_prefix=own_prefix,
                subcommand=Subcommand.CONTINUE,
                chunk_number=chunk_n,
                content_token=content_token,
            )
            cont_resp = await self._send_and_wait(server_path, cont_req)
            if cont_resp.status == StatusCode.UNKNOWN_TOKEN:
                raise VagrantNetError(
                    "[Server] transfer lost (restarted, or expired request) Restart the fetch from the beginning"
                )
            if cont_resp.status != StatusCode.OK:
                raise VagrantNetError(f"[Server] returned {cont_resp.status.name} mid-transfer")

            chunks[chunk_n] = cont_resp.payload
            if cont_resp.is_final:
                checksum = cont_resp.checksum
            chunk_n += 1

        body = chunking.reassemble(chunks, total_chunks)
        if compressed:
            body = compress.decompress(body)

        if checksum is not None and zlib.crc32(body) != checksum:
            raise VagrantNetError("Checksum mismatch (corrupted/invalid transfer)")

        if len(body) != uncompressed_size and uncompressed_size:
            logger.warning(
                "reassembled size %d != advertised uncompressed_size %d",
                len(body),
                uncompressed_size,
            )

        return body

async def _main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO)
    if len(argv) < 4:
        print(
            # TODO: Make this a TUI browser-style interface, with a list of known/favorite and their pages, and a way to select and fetch them.
            "usage: python -m vagrantnet.client.client <serial-port> <server-pubkey-hex> "
            "<get-page|get-file|list-pages> [path]"
        )
        sys.exit(1)

    port, server_key, cmd_str = argv[1], argv[2], argv[3]
    path = argv[4] if len(argv) > 4 else ""

    subcommand = {
        "get-page": Subcommand.GET_PAGE,
        "get-file": Subcommand.GET_FILE,
        "list-pages": Subcommand.LIST_PAGES,
    }.get(cmd_str)
    if subcommand is None:
        print(f"unknown command: {cmd_str}")
        sys.exit(1)

    client = await VagrantNetClient.connect_serial(port)
    body = await client.fetch(server_key, subcommand, path)

    if subcommand == Subcommand.GET_FILE:
        out_name = path.split("/")[-1] or "download.bin"
        with open(out_name, "wb") as f:
            f.write(body)
        print(f"saved {len(body)} bytes to {out_name}")
    else:
        print(render_ansi(body.decode("utf-8", errors="replace")))

if __name__ == "__main__":
    asyncio.run(_main(sys.argv))
