"""vagrantNet daemon: serves pages and files over MeshCore's raw-data transport."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import zlib
from pathlib import Path

from meshcore import EventType, MeshCore
from ..common import chunking, compress, envelope, transport
from ..common.envelope import Request, Response, StatusCode, Subcommand
from ..common.safepath import PathTraversalError, resolve_within
from .config import DaemonConfig
from .store import NoTokenAvailable, Transfer, TransferStore

logger = logging.getLogger("vagrantnet.daemon")

CONNECT_MAX_ATTEMPTS = 4
CONNECT_RETRY_DELAY_SECONDS = 3.0
HEARTBEAT_INTERVAL_SECONDS = 60.0
HEARTBEAT_TIMEOUT_SECONDS = 15.0

class VagrantNetDaemon:
    def __init__(self, config: DaemonConfig):
        self.config = config
        self.mc: MeshCore | None = None
        config.pages_dir.mkdir(parents=True, exist_ok=True)
        config.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.store = TransferStore(
            ttl_seconds=config.content_token_ttl_seconds,
            max_total=config.max_in_flight_transfers_total,
            max_per_client=config.max_in_flight_transfers_per_client,
        )
        # Set by _on_disconnected once transport.py's auto_reconnect has
        # exhausted its own attempts -- run_forever() watches this to start
        # a full reconnect cycle instead of leaving the daemon silently deaf.
        self._link_down = asyncio.Event()

    # ---------------- Hosting lifecycle -----------------------------------------
    async def connect(self) -> None:
        conn = self.config.connection
        # Retry loop for the initial connect (radio busy/not powered up yet).
        last_error: Exception | None = None
        for attempt in range(1, CONNECT_MAX_ATTEMPTS + 1):
            try:
                if conn.kind == "serial":
                    self.mc = await transport.connect_serial(
                        conn.target,
                        conn.baudrate,
                        pulse_before_first_connect=attempt > 1,
                    )
                elif conn.kind == "tcp":
                    host, port_str = conn.target.split(":")
                    self.mc = await MeshCore.create_tcp(host, int(port_str))
                elif conn.kind == "ble":
                    self.mc = await transport.connect_ble(
                        conn.target,
                        force_disconnect_before_first_connect=attempt == 1,
                    )
                else:
                    raise ValueError(f"unknown connection kind: {conn.kind!r}")
                if self.mc is not None:
                    break
                last_error = None
            except (ConnectionError, OSError) as exc:
                # expected serialException if the radio is busy and reconnect fails
                last_error = exc
            logger.warning(
                "connect attempt %d/%d to %s:%s failed%s",
                attempt,
                CONNECT_MAX_ATTEMPTS,
                conn.kind,
                conn.target,
                f": {last_error}" if last_error else "",
            )
            if attempt < CONNECT_MAX_ATTEMPTS:
                await asyncio.sleep(CONNECT_RETRY_DELAY_SECONDS)
    # TODO: Check if the daemon should use the repeater or roomServer firmware instead
        if self.mc is None:
            raise RuntimeError(
                f"failed to connect to MeshCore device ({conn.kind}:{conn.target}) "
                f"after {CONNECT_MAX_ATTEMPTS} attempts... did not answer, "
                "check it's flashed with companion firmware and the port/address is correct"
            )
        self._link_down = asyncio.Event()
        self.mc.subscribe(EventType.RAW_DATA, self._on_raw_data)
        self.mc.subscribe(EventType.DISCONNECTED, self._on_disconnected)

        query_result = await self.mc.commands.send_device_query()
        if query_result.type == EventType.ERROR:
            raise RuntimeError(f"device query failed: {query_result.payload}")
        logger.info("daemon connected as %r", self.config.node_name)

    async def _on_disconnected(self, event) -> None:
        payload = event.payload or {}
        if payload.get("reason") == "manual_disconnect":
            return  # our own disconnect() call, not a real link drop
        if payload.get("reconnect_failed") or payload.get("max_attempts_exceeded"):
            logger.error(
                "link lost and auto-reconnect exhausted its attempts -- "
                "reconnecting from scratch: %s",
                payload,
            )
            self._link_down.set()
        else:
            logger.info("link lost, auto-reconnect in progress: %s", payload)

    async def _heartbeat_loop(self) -> None:
        # Active probe device link rather than only reacting to it.
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            if self.mc is None:
                return
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

    async def disconnect(self) -> None:
        if self.mc is not None:
            await self.mc.disconnect()
            self.mc = None

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            # Ensure that when daemon is killed, DTR/RTS is dropped and cleaned
            loop.add_signal_handler(sig, stop.set)

        while not stop.is_set():
            await self.connect()
            stop_task = asyncio.ensure_future(stop.wait())
            link_down_task = asyncio.ensure_future(self._link_down.wait())
            heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())
            _, pending = await asyncio.wait(
                [stop_task, link_down_task, heartbeat_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if stop.is_set():
                break
            # auto_reconnect gave up, or the heartbeat caught a silent
            await self.disconnect()

        logger.info("shutting down")
        await self.disconnect()

    # ------------------------ Inbound dispatch -----------------------------------------
    async def _on_raw_data(self, event) -> None:
        logger.info("RAW_DATA event received: %s", event.payload)
        raw_hex = event.payload.get("payload") if isinstance(event.payload, dict) else None
        if not raw_hex:
            return
        try:
            data = bytes.fromhex(raw_hex)
            req = Request.decode(data)
        except (envelope.EnvelopeError, ValueError):
            return  # not a recognized frame

        logger.debug("request from %s: %s", req.client_pubkey_prefix.hex(), req.subcommand)
        try:
            await self._handle_request(req)
        except Exception:
            logger.exception("error handling request %s", req.request_id)

    async def _handle_request(self, req: Request) -> None:
        if req.subcommand == Subcommand.CONTINUE:
            await self._handle_continue(req)
            return

        try:
            uncompressed, status = self._load_content(req)
        except PathTraversalError:
            await self._reply_error(req, StatusCode.ACCESS_DENIED)
            return

        if status != StatusCode.OK:
            await self._reply_error(req, status)
            return

        compressed = compress.compress(uncompressed)
        # only worth using the compressed form if it's actually smaller
        # TODO: consider an algorithm for compression for small pages that can lose to zstd's frame overhead
        use_compressed = len(compressed) < len(uncompressed) and req.prefer_compressed
        payload = compressed if use_compressed else uncompressed

        chunks = chunking.split(payload)
        checksum = zlib.crc32(uncompressed) if len(chunks) else None

        if len(chunks) == 1:
            await self._send_chunk(
                req,
                content_token=0,  # unused for single-chunk replies
                chunk_number=0,
                total_chunks=1,
                chunk_payload=chunks[0],
                uncompressed_size=len(uncompressed),
                compressed=use_compressed,
                is_final=True,
                checksum=checksum,
            )
            return

        # The client retries its own initial request (same request_id) if there is 0 'reply seen'
        existing = self.store.find_by_request(req.client_pubkey_prefix, req.request_id)
        if existing is not None:
            token, transfer = existing
        else:
            transfer = Transfer(
                client_pubkey_prefix=req.client_pubkey_prefix,
                request_id=req.request_id,
                chunks=chunks,
                uncompressed_size=len(uncompressed),
                compressed=use_compressed,
                checksum=checksum,
            )
            try:
                token = self.store.start(req.client_pubkey_prefix, transfer)
            except NoTokenAvailable:
                await self._reply_error(req, StatusCode.ERROR)
                return

        await self._send_chunk(
            req,
            content_token=token,
            chunk_number=0,
            total_chunks=transfer.total_chunks,
            chunk_payload=transfer.chunks[0],
            uncompressed_size=transfer.uncompressed_size,
            compressed=transfer.compressed,
            is_final=False,
            checksum=None,
        )

    async def _handle_continue(self, req: Request) -> None:
        transfer = self.store.get(req.content_token)
        if transfer is None or transfer.client_pubkey_prefix != req.client_pubkey_prefix:
            await self._reply_error(req, StatusCode.UNKNOWN_TOKEN)
            return

        n = req.chunk_number
        if n >= transfer.total_chunks:
            await self._reply_error(req, StatusCode.INVALID_REQUEST)
            return

        is_final = n == transfer.total_chunks - 1
        await self._send_chunk(
            req,
            content_token=req.content_token,
            chunk_number=n,
            total_chunks=transfer.total_chunks,
            chunk_payload=transfer.chunks[n],
            uncompressed_size=transfer.uncompressed_size,
            compressed=transfer.compressed,
            is_final=is_final,
            checksum=transfer.checksum if is_final else None,
        )
        if is_final:
            self.store.finish(req.content_token)

    # ------------ content loading (path-safe) ----------------------------------
    def _load_content(self, req: Request) -> tuple[bytes, StatusCode]:
        if req.subcommand == Subcommand.LIST_PAGES:
            listing = "# Available Pages\n\n"
            for page_file in sorted(self.config.pages_dir.glob("**/*.vn")):
                rel = page_file.relative_to(self.config.pages_dir)
                listing += f"[{page_file.stem}|{rel}]\n"
            return listing.encode("utf-8"), StatusCode.OK

        root = (
            self.config.pages_dir
            if req.subcommand == Subcommand.GET_PAGE
            else self.config.downloads_dir
        )
        try:
            file_path = resolve_within(root, req.path or "")
        except PathTraversalError:
            raise

        if not file_path.exists() or not file_path.is_file():
            return b"", StatusCode.NOT_FOUND

        return file_path.read_bytes(), StatusCode.OK

    # ----------------- outbound ------------------------------------------
    async def _reply_error(self, req: Request, status: StatusCode) -> None:
        await self._send_chunk(
            req,
            content_token=0,
            chunk_number=0,
            total_chunks=1,
            chunk_payload=b"",
            uncompressed_size=0,
            compressed=False,
            is_final=True,
            checksum=None,
            status=status,
        )

    async def _send_chunk(
        self,
        req: Request,
        *,
        content_token: int,
        chunk_number: int,
        total_chunks: int,
        chunk_payload: bytes,
        uncompressed_size: int,
        compressed: bool,
        is_final: bool,
        checksum: int | None,
        status: StatusCode = StatusCode.OK,
    ) -> None:
        assert self.mc is not None
        resp = Response(
            request_id=req.request_id,
            status=status,
            chunk_number=chunk_number,
            content_token=content_token,
            payload=chunk_payload,
            compressed=compressed,
            is_final=is_final,
            checksum=checksum,
            uncompressed_size=uncompressed_size if chunk_number == 0 else None,
            total_chunks=total_chunks if chunk_number == 0 else None,
        )

        # This is a single-hop test
        # TODO: Increase robustness of the send tuned for multiple hops, clients and repeaters.
        payload = resp.encode()
        send_result = await self.mc.commands.send_raw_data(payload)
        if send_result.type == EventType.ERROR:
            logger.warning(
                "send_raw_data failed for request %s chunk %d: %s",
                req.request_id,
                chunk_number,
                send_result.payload,
            )
        else:
            logger.info(
                "sent reply for request %s chunk %d (%d bytes, status=%s)",
                req.request_id,
                chunk_number,
                len(payload),
                status.name,
            )


async def _main(config_path: str) -> None:
    logging.basicConfig(level=logging.INFO)
    config = DaemonConfig.load(Path(config_path))
    daemon = VagrantNetDaemon(config)
    await daemon.run_forever()

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m vagrantnet.daemon.server /path/to/config.json")
        sys.exit(1)
    asyncio.run(_main(sys.argv[1]))
