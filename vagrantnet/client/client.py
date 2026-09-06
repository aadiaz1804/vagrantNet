"""vagrantNet client: fetches pages/files/listings from a daemon over MeshCore."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
import zlib
from dataclasses import dataclass

from meshcore import EventType, MeshCore

from ..common import chunking, compress, transport
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
# Total time willing to keep retrying one chunk before surfacing an error --
# TCP-like: link drops and lost packets are invisible retries up to this
# point, not a fixed attempt count, since real LoRa loss rates don't fit a
# "3 tries and give up" model (see NOTES.md, ~1/3 arrival rate observed).
CHUNK_RETRY_DEADLINE_SECONDS = 90.0
CHUNK_RETRY_JITTER_SECONDS = (0.5, 2.0)
CONNECT_MAX_ATTEMPTS = 4
CONNECT_RETRY_DELAY_SECONDS = 3.0
CONTACT_LOOKUP_MAX_ATTEMPTS = 8
CONTACT_LOOKUP_RETRY_DELAY_SECONDS = 3.0

_BLE_ADDRESS_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

class VagrantNetError(RuntimeError):
    pass

async def _retry(coro_fn, *, attempts: int, delay: float, what: str):
    """Bounded retry for a first connect attempt -- there's no live session
    to fall back on yet, so unlike CHUNK_RETRY_DEADLINE_SECONDS this has to
    give up eventually rather than retry indefinitely. Once connected,
    `transport.connect_serial`/`connect_ble`'s auto_reconnect takes over for
    any drop that happens afterwards."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await coro_fn()
        except (ConnectionError, OSError) as exc:
            last_error = exc
            logger.warning("%s attempt %d/%d failed: %s", what, attempt, attempts, exc)
            if attempt < attempts:
                await asyncio.sleep(delay)
    raise VagrantNetError(f"{what} failed after {attempts} attempts") from last_error

@dataclass
class _PendingFetch:
    request_id: int
    future: "asyncio.Future[Response]"

class VagrantNetClient:
    def __init__(self, mc: MeshCore):
        self.mc = mc
        self._pending: dict[int, _PendingFetch] = {}
        self.mc.subscribe(EventType.RAW_DATA, self._on_raw_data)
        self.mc.subscribe(EventType.DISCONNECTED, self._on_disconnected)

    @classmethod
    # TODO: Support having vagrantNetClient and MeshCore cli/clients at the same time
    # (Maybe a middleware layer to avoid the /dev/ttyUSBX interface being locked by MeshCore CLI or vagrantNetClient)
    async def connect_serial(cls, port: str, baudrate: int = 115200) -> "VagrantNetClient":
        async def _try() -> MeshCore:
            mc = await transport.connect_serial(port, baudrate)
            if mc is None:
                raise ConnectionError(f"could not connect to MeshCore device at {port}")
            return mc

        mc = await _retry(
            _try,
            attempts=CONNECT_MAX_ATTEMPTS,
            delay=CONNECT_RETRY_DELAY_SECONDS,
            what=f"connect to {port}",
        )
        return await cls._ready(mc)

    @classmethod
    async def connect_ble(cls, address: str, pin: str | None = None) -> "VagrantNetClient":
        async def _try() -> MeshCore:
            mc = await transport.connect_ble(address, pin=pin)
            if mc is None:
                raise ConnectionError(f"could not connect to MeshCore device at {address}")
            return mc

        mc = await _retry(
            _try,
            attempts=CONNECT_MAX_ATTEMPTS,
            delay=CONNECT_RETRY_DELAY_SECONDS,
            what=f"connect to {address}",
        )
        return await cls._ready(mc)

    @classmethod
    async def _ready(cls, mc: MeshCore) -> "VagrantNetClient":
        # No bulk contacts sync for speed, after radio is ready we only ask for the one contact we need for the server's pk.
        await mc.commands.send_device_query()
        # Do a quick advert without flood to get near repeaters
        # TODO: Check if it's worth doing a flood advert or give an option to the end user
        await mc.commands.send_advert(flood=False)
        return cls(mc)

    async def _on_disconnected(self, event) -> None:
        # Disconnect if transport.py failed to reconnect
        payload = event.payload or {}
        if payload.get("reason") == "manual_disconnect":
            return  # our own disconnect() call, not a real link drop
        if payload.get("reconnect_failed") or payload.get("max_attempts_exceeded"):
            logger.error("connection to radio lost and auto-reconnect gave up: %s", payload)
        else:
            logger.info("connection to radio lost, reconnecting: %s", payload)

    async def _on_raw_data(self, event) -> None:
        raw_hex = event.payload.get("payload") if isinstance(event.payload, dict) else None
        if not raw_hex:
            return
        logger.info("RAW_DATA event received: %s", event.payload)
        try:
            resp = Response.decode(bytes.fromhex(raw_hex))
        except EnvelopeError as e:
            logger.warning("RAW_DATA received but failed to decode as a Response: %s (raw=%s)", e, raw_hex)
            return  # ignored bad/unexpected response

        pending = self._pending.get(resp.request_id)
        if pending is not None and not pending.future.done():
            pending.future.set_result(resp)

    async def _resolve_path(self, server_pubkey_hex: str) -> bytes:
        pubkey = bytes.fromhex(server_pubkey_hex)
        contact: dict = {}
        for attempt in range(1, CONTACT_LOOKUP_MAX_ATTEMPTS + 1):
            contact_event = await self.mc.commands.get_contact_by_key(pubkey)
            contact = getattr(contact_event, "payload", None) or {}
            if contact_event.type != EventType.ERROR and contact.get("out_path_len", -1) >= 0:
                break
            logger.warning(
                "contact/path lookup attempt %d/%d for %s came back empty: %s",
                attempt,
                CONTACT_LOOKUP_MAX_ATTEMPTS,
                server_pubkey_hex[:12],
                getattr(contact_event, "payload", None),
            )
            if attempt < CONTACT_LOOKUP_MAX_ATTEMPTS:
                await asyncio.sleep(CONTACT_LOOKUP_RETRY_DELAY_SECONDS)
        else:
            raise VagrantNetError(
                f"Server {server_pubkey_hex[:12]}... has no known path yet -- "
                "wait for a server/repeater advert before fetching"
            )
        return bytes.fromhex(contact.get("out_path") or "")

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
        deadline = loop.time() + CHUNK_RETRY_DEADLINE_SECONDS
        attempt = 0

        while True:
            attempt += 1
            fut: "asyncio.Future[Response]" = loop.create_future()
            self._pending[req.request_id] = _PendingFetch(req.request_id, fut)
            try:
                await self.mc.commands.send_raw_data(req.encode(), path=server_path)
                resp = await asyncio.wait_for(fut, timeout=CHUNK_TIMEOUT_SECONDS)
                return resp
            except (asyncio.TimeoutError, ConnectionError, OSError) as e:
                # ConnectionError/OSError catch for send_raw_data failing
                last_error = e
                logger.warning(
                    "chunk request_id=%s attempt %d failed (%s), retrying...",
                    req.request_id,
                    attempt,
                    type(e).__name__,
                )
            finally:
                self._pending.pop(req.request_id, None)

            if loop.time() >= deadline:
                raise VagrantNetError(
                    f"gave up on request_id={req.request_id} after {attempt} attempts "
                    f"over {CHUNK_RETRY_DEADLINE_SECONDS:.0f}s"
                ) from last_error
            await asyncio.sleep(random.uniform(*CHUNK_RETRY_JITTER_SECONDS))

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

    # Pull an optional BLE --pin=XXXXXX if it's the first connection
    pin: str | None = None
    positional = []
    for arg in argv[1:]:
        if arg.startswith("--pin="):
            pin = arg.split("=", 1)[1]
        else:
            positional.append(arg)

    if len(positional) < 3:
        print(
            # TODO: Make this a TUI browser-style interface, with a list of known/favorite and their pages, and a way to select and fetch them.
            "usage: python -m vagrantnet.client.client <serial-port|ble-address> "
            "<server-pubkey-hex> <get-page|get-file|list-pages> [path] [--pin=XXXXXX]"
        )
        sys.exit(1)

    port, server_key, cmd_str = positional[0], positional[1], positional[2]
    path = positional[3] if len(positional) > 3 else ""

    subcommand = {
        "get-page": Subcommand.GET_PAGE,
        "get-file": Subcommand.GET_FILE,
        "list-pages": Subcommand.LIST_PAGES,
    }.get(cmd_str)
    if subcommand is None:
        print(f"unknown command: {cmd_str}")
        sys.exit(1)

    client = (
        await VagrantNetClient.connect_ble(port, pin=pin)
        if _BLE_ADDRESS_RE.match(port)
        else await VagrantNetClient.connect_serial(port)
    )
    try:
        body = await client.fetch(server_key, subcommand, path)

        if subcommand == Subcommand.GET_FILE:
            out_name = path.split("/")[-1] or "download.bin"
            with open(out_name, "wb") as f:
                f.write(body)
            print(f"saved {len(body)} bytes to {out_name}")
        else:
            print(render_ansi(body.decode("utf-8", errors="replace")))
    finally:
        # Cleanly disconnect from the radio before exiting
        await client.mc.disconnect()

if __name__ == "__main__":
    asyncio.run(_main(sys.argv))
