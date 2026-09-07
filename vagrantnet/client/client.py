"""vagrantNet client: fetches pages/files/listings from a server over MeshCore."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import sys
import zlib
from dataclasses import dataclass

from meshcore import EventType, MeshCore
from ..common import chunking, compress, discovery, transport
from ..common.envelope import (
    EnvelopeError,
    Request,
    Response,
    StatusCode,
    Subcommand,
)
from ..common.page import render_ansi

logger = logging.getLogger("vagrantnet.client")

# Base timeout 5s which increases by 5s per hop
CHUNK_TIMEOUT_BASE_SECONDS = 5.0
CHUNK_TIMEOUT_PER_HOP_SECONDS = 5.0
CHUNK_TIMEOUT_MAX_SECONDS = 35.0  # Max timeout
# Total time willing to keep retrying one chunk before surfacing an error
CHUNK_RETRY_DEADLINE_SECONDS = 90.0
CHUNK_RETRY_JITTER_SECONDS = (0.5, 2.0)
CONNECT_MAX_ATTEMPTS = 4
CONNECT_RETRY_DELAY_SECONDS = 3.0
CONTACT_LOOKUP_MAX_ATTEMPTS = 8
CONTACT_LOOKUP_RETRY_DELAY_SECONDS = 3.0
HEARTBEAT_INTERVAL_SECONDS = 60.0
HEARTBEAT_TIMEOUT_SECONDS = 15.0
RECONNECT_BACKOFF_MAX_SECONDS = 60.0

_BLE_ADDRESS_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

class VagrantNetError(RuntimeError):
    pass

def _chunk_timeout(hops: int) -> float:
    return min(
        CHUNK_TIMEOUT_BASE_SECONDS + CHUNK_TIMEOUT_PER_HOP_SECONDS * max(hops, 0),
        CHUNK_TIMEOUT_MAX_SECONDS,
    )

async def _retry(coro_fn, *, attempts: int, delay: float, what: str):
    # Retry for a first connect attempt
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await coro_fn(attempt)
        except VagrantNetError:
            raise  # already transmitted and done
        except Exception as exc:
            # Broad as BLE connect errors are plain Exceptions 
            last_error = exc
            logger.warning(
                "%s attempt %d/%d failed: %s: %s",
                what, attempt, attempts, type(exc).__name__, exc,
            )
            if attempt < attempts:
                await asyncio.sleep(delay)
    raise VagrantNetError(f"{what} failed after {attempts} attempts") from last_error

@dataclass
class _Dial:
    # Classes needed to reconnect after a link drop reliably
    kind: str  # "serial" or "ble"
    target: str
    baudrate: int = 115200
    pin: str | None = None
    quiet: bool = False  # suppress the meshcore library's INFO chatter
    flood_advert: bool = False

async def _dial(d: _Dial) -> MeshCore:
    async def _try(attempt: int) -> MeshCore:
        if d.kind == "ble":
            mc = await transport.connect_ble(
                d.target, pin=d.pin, quiet=d.quiet,
                force_disconnect_before_first_connect=attempt == 1,
            )
        else:
            mc = await transport.connect_serial(
                d.target, d.baudrate, quiet=d.quiet,
                pulse_before_first_connect=attempt > 1,
            )
        if mc is None:
            raise ConnectionError(f"could not connect to MeshCore device at {d.target}")
        return mc

    return await _retry(
        _try,
        attempts=CONNECT_MAX_ATTEMPTS,
        delay=CONNECT_RETRY_DELAY_SECONDS,
        what=f"connect to {d.target}",
    )

@dataclass
class _PendingFetch:
    request_id: int
    future: "asyncio.Future[Response]"

class VagrantNetClient:
    def __init__(self, mc: MeshCore, dial: _Dial | None = None):
        self.mc = mc
        self.dial = dial
        self._pending: dict[int, _PendingFetch] = {}
        self._contacts: dict[str, dict] = {}
        self.on_servers_changed = None
        self._link_down = asyncio.Event()
        self._supervisor: asyncio.Task | None = None
        self._subscribe()

    def _subscribe(self) -> None:
        self.mc.subscribe(EventType.RAW_DATA, self._on_raw_data)
        self.mc.subscribe(EventType.DISCONNECTED, self._on_disconnected)
        # Keep listening the way meshcore-cli does
        self.mc.subscribe(EventType.ADVERTISEMENT, self._on_advert)
        self.mc.subscribe(EventType.NEW_CONTACT, self._on_advert)

    async def _on_advert(self, event) -> None:
        payload = event.payload or {}
        if not isinstance(payload, dict):
            return
        # ADVERTISEMENT gets the advert and pkey
        key = payload.get("public_key") or payload.get("adv_key")
        if not key:
            return
        record = dict(self._contacts.get(key) or {})
        record.update(payload)
        record["public_key"] = key
        record.setdefault("type", payload.get("adv_type", 0))
        record.setdefault("out_path_len", -1)  # an advert carries no path
        was_server = discovery.is_marked((self._contacts.get(key) or {}).get("adv_name"))
        self._contacts[key] = record
        if discovery.is_marked(record.get("adv_name")) and not was_server:
            logger.info("heard a new vagrantNet server: %r", record.get("adv_name"))
            if self.on_servers_changed:
                self.on_servers_changed(self.known_servers())

    def known_servers(self) -> list:
        # vNet seen devices
        return discovery.servers(list(self._contacts.values()))

    @classmethod
    async def connect_serial(
        cls, port: str, baudrate: int = 115200, *, quiet: bool = False, flood_advert: bool = False
    ) -> "VagrantNetClient":
        dial = _Dial("serial", port, baudrate=baudrate, quiet=quiet,
                     flood_advert=flood_advert)
        return await cls._ready(await _dial(dial), dial)

    @classmethod
    async def connect_ble(
        cls, address: str, pin: str | None = None, *, quiet: bool = False, flood_advert: bool = False
    ) -> "VagrantNetClient":
        dial = _Dial("ble", address, pin=pin, quiet=quiet,
                     flood_advert=flood_advert)
        return await cls._ready(await _dial(dial), dial)

    @classmethod
    async def _ready(cls, mc: MeshCore, dial: _Dial | None = None) -> "VagrantNetClient":
        # No bulk contacts sync for speed, after radio is ready we only ask for the one contact we need for the server's pk.
        await mc.commands.send_device_query()
        # Announce ourselves so servers can route replies back.
        await mc.commands.send_advert(flood=dial.flood_advert if dial else False)
        client = cls(mc, dial)
        if dial is not None:
            client._supervisor = asyncio.ensure_future(client._supervise())
        return client

    async def _on_disconnected(self, event) -> None:
        payload = event.payload or {}
        if payload.get("reason") == "manual_disconnect":
            return  # our own disconnect() call, not a real link drop
        if payload.get("reconnect_failed") or payload.get("max_attempts_exceeded"):
            logger.error("connection to radio lost and auto-reconnect gave up: %s", payload)
            self._link_down.set()
        else:
            logger.info("connection to radio lost, reconnecting: %s", payload)

    async def _heartbeat_loop(self) -> None:
        # Changed the passive send and verify to an active query to the radio hardware
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            logger.info("heartbeat: querying radio")
            try:
                result = await asyncio.wait_for(
                    self.mc.commands.send_device_query(),
                    timeout=HEARTBEAT_TIMEOUT_SECONDS,
                )
                if result is None or result.type == EventType.ERROR:
                    raise ConnectionError(f"heartbeat query failed: {result}")
            except Exception as exc:
                logger.error("heartbeat failed, treating link as down: %s", exc)
                self._link_down.set()
                return

    async def _supervise(self) -> None:
        # Client-side of the run_forever() implementatinon
        backoff = CONNECT_RETRY_DELAY_SECONDS
        while True:
            heartbeat = asyncio.ensure_future(self._heartbeat_loop())
            try:
                await self._link_down.wait()
            finally:
                heartbeat.cancel()
            logger.warning("radio link down, reconnecting")
            try:
                await self._redial()
            except Exception as exc:
                logger.error("reconnect failed, retrying in %.0fs: %s", backoff, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX_SECONDS)
                continue
            backoff = CONNECT_RETRY_DELAY_SECONDS
            logger.info("radio link restored")

    async def _redial(self) -> None:
        assert self.dial is not None
        try:
            await self.mc.disconnect()
        except Exception as exc:
            # Old link is dead reset failed.
            logger.debug("teardown of the dead link failed: %s", exc)
        mc = await _dial(self.dial)
        await mc.commands.send_device_query()
        self.mc = mc
        self._link_down = asyncio.Event()
        self._subscribe()

    async def disconnect(self) -> None:
        # Stop active heartbeat then disconnect link
        if self._supervisor is not None:
            self._supervisor.cancel()
            self._supervisor = None
        await self.mc.disconnect()

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

    async def _resolve_path(self, server_pubkey_hex: str) -> tuple[bytes, int]:
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
        return (
            bytes.fromhex(contact.get("out_path") or ""),
            max(int(contact.get("out_path_len", 0)), 0),
        )

    async def discover(self, timeout: float = discovery.SCAN_TIMEOUT_SECONDS):
        # Find vagrantNet servers the radio has already heard advertise.
        contacts = await discovery.scan(self.mc, timeout=timeout)
        for c in contacts:
            key = c.get("public_key")
            if key:
                self._contacts[key] = {**(self._contacts.get(key) or {}), **c}
        return self.known_servers(), discovery.census(list(self._contacts.values()))

    def _own_pubkey_prefix(self) -> bytes:
        pk_hex = self.mc.self_info.get("public_key")
        if not pk_hex:
            raise VagrantNetError("user has no public_key, set-up and broadcast MeshCore first")
        return bytes.fromhex(pk_hex)[:6]

    async def _send_and_wait(
        self, server_path: bytes, req: Request, timeout: float
    ) -> Response:
        loop = asyncio.get_event_loop()
        last_error: Exception | None = None
        # A long path needs a long per-chunk timeout so this increases the retry 
        deadline = loop.time() + max(CHUNK_RETRY_DEADLINE_SECONDS, timeout * 3)
        attempt = 0

        while True:
            attempt += 1
            fut: "asyncio.Future[Response]" = loop.create_future()
            self._pending[req.request_id] = _PendingFetch(req.request_id, fut)
            try:
                logger.info(
                    "send_raw_data: request_id=%s attempt %d", req.request_id, attempt
                )
                await self.mc.commands.send_raw_data(req.encode(), path=server_path)
                resp = await asyncio.wait_for(fut, timeout=timeout)
                return resp
            except EnvelopeError:
                raise  # unencodable frame
            except Exception as e:
                # Retry fetch if send/recieve fails
                last_error = e
                logger.warning(
                    "chunk request_id=%s attempt %d failed (%s: %s), retrying...",
                    req.request_id,
                    attempt,
                    type(e).__name__,
                    e,
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
        # Fetch a page/file/listing, following the CONTINUE chain until the
        # final chunk, and return the fully reassembled, decompressed bytes.
        server_path, hops = await self._resolve_path(server_pubkey_hex)
        timeout = _chunk_timeout(hops)
        logger.info(
            "path to %s: %d hop(s), chunk timeout %.0fs",
            server_pubkey_hex[:12],
            hops,
            timeout,
        )
        own_prefix = self._own_pubkey_prefix()

        req_id = random.randint(0, 0xFFFF)
        req = Request(
            request_id=req_id,
            client_pubkey_prefix=own_prefix,
            subcommand=subcommand,
            chunk_number=0,
            path=path,
            dict_id=compress.active_dict_id(),
        )
        resp = await self._send_and_wait(server_path, req, timeout)

        if resp.status != StatusCode.OK:
            raise VagrantNetError(f"server returned {resp.status.name} for {path!r}")

        chunks: dict[int, bytes] = {0: resp.payload}
        total_chunks = resp.total_chunks or 1
        uncompressed_size = resp.uncompressed_size or 0
        compressed = resp.compressed
        server_dict_id = resp.dict_id
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
                dict_id=compress.active_dict_id(),
            )
            cont_resp = await self._send_and_wait(server_path, cont_req, timeout)
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
            try:
                body = compress.decompress(body, dict_id=server_dict_id)
            except compress.DictionaryMismatch as exc:
                raise VagrantNetError(str(exc)) from exc

        if checksum is not None and zlib.crc32(body) != checksum:
            raise VagrantNetError("Checksum mismatch (corrupted/invalid transfer)")

        if len(body) != uncompressed_size and uncompressed_size:
            logger.warning(
                "reassembled size %d != advertised uncompressed_size %d",
                len(body),
                uncompressed_size,
            )

        return body

LOG_FORMAT = "%(asctime)s.%(msecs)03d %(name)s %(levelname)s %(message)s"
LOG_DATEFMT = "%H:%M:%S"

async def _main(argv: list[str]) -> None:
    logging.basicConfig(
        level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATEFMT, force=True
    )

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
    if subcommand != Subcommand.LIST_PAGES and not path:
        # Catch path error
        print(f"{cmd_str} requires a path")
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
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(_main(sys.argv))
