"""vagrantNet server: serves pages and files over MeshCore's raw-data transport."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
import zlib
from pathlib import Path

from meshcore import EventType, MeshCore
from ..common import chunking, compress, discovery, envelope, page, routing, transport
from ..common.envelope import Request, Response, StatusCode, Subcommand
from ..common.safepath import PathTraversalError, resolve_within
from . import boards
from .config import ServerConfig
from .store import NoTokenAvailable, Transfer, TransferStore

logger = logging.getLogger("vagrantnet.server")

LOG_FORMAT = "%(asctime)s.%(msecs)03d %(name)s %(levelname)s %(message)s"
LOG_DATEFMT = "%H:%M:%S"

CONNECT_MAX_ATTEMPTS = 4
CONNECT_RETRY_DELAY_SECONDS = 3.0
CONNECT_BACKOFF_MAX_SECONDS = 60.0
CONTACT_REFRESH_SECONDS = 900.0  # rebuild the routing table every 15 min
HEARTBEAT_INTERVAL_SECONDS = 60.0
HEARTBEAT_TIMEOUT_SECONDS = 15.0

class VagrantNetServer:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.mc: MeshCore | None = None
        config.pages_dir.mkdir(parents=True, exist_ok=True)
        config.downloads_dir.mkdir(parents=True, exist_ok=True)
        config.boards_dir.mkdir(parents=True, exist_ok=True)
        self.store = TransferStore(
            ttl_seconds=config.content_token_ttl_seconds,
            linger_seconds=config.content_token_linger_seconds,
            max_total=config.max_in_flight_transfers_total,
            max_per_client=config.max_in_flight_transfers_per_client,
        )
        self.uploads = boards.UploadStore()
        self._link_down = asyncio.Event()
        # pubkey hex -> contact record
        self._contacts: dict[str, dict] = {}

    # ---------------- Hosting lifecycle -----------------------------------------
    async def connect(self) -> None:
        conn = self.config.connection
        if conn.kind not in ("serial", "tcp", "ble"):
            raise ValueError(f"unknown connection kind: {conn.kind!r}")
        if conn.kind == "serial" and conn.target.strip().lower() in ("", "auto"):
            found = await transport.autodetect_serial(conn.baudrate)
            if found is None:
                raise RuntimeError(
                    "connection.target is 'auto' but no MeshCore radio answered "
                )
            conn.target = found
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
                else:
                    self.mc = await transport.connect_ble(
                        conn.target,
                        force_disconnect_before_first_connect=attempt == 1,
                    )
                if self.mc is not None:
                    break
                last_error = None
            except Exception as exc:
                # Server transport retry
                last_error = exc
            logger.warning(
                "connect attempt %d/%d to %s:%s failed%s",
                attempt,
                CONNECT_MAX_ATTEMPTS,
                conn.kind,
                conn.target,
                f": {type(last_error).__name__}: {last_error}" if last_error else "",
            )
            if attempt < CONNECT_MAX_ATTEMPTS:
                await asyncio.sleep(CONNECT_RETRY_DELAY_SECONDS)

        if self.mc is None:
            raise RuntimeError(
                f"failed to connect to MeshCore device ({conn.kind}:{conn.target}) "
                f"after {CONNECT_MAX_ATTEMPTS} attempts... did not answer, "
                "check it's flashed with companion firmware and the port/address is correct"
            )
        self._link_down = asyncio.Event()
        self.mc.subscribe(EventType.RAW_DATA, self._on_raw_data)
        self.mc.subscribe(EventType.DISCONNECTED, self._on_disconnected)
        self.mc.subscribe(EventType.ADVERTISEMENT, self._on_contact)
        self.mc.subscribe(EventType.NEW_CONTACT, self._on_contact)

        query_result = await self.mc.commands.send_device_query()
        if query_result.type == EventType.ERROR:
            raise RuntimeError(f"device query failed: {query_result.payload}")
        await self._announce_identity()
        logger.info("server connected as %r", self.config.node_name)

    async def _announce_identity(self) -> None:
        # Put the vagrantNet marker in the node's advertised name.
        if not self.config.advertise_as_server:
            return
        current = (self.mc.self_info or {}).get("name") or ""
        wanted = discovery.mark(self.config.node_name)
        if current == wanted:
            logger.info("advertising as %r already", wanted)
            return
        result = await self.mc.commands.set_name(wanted)
        if result is not None and result.type == EventType.ERROR:
            logger.warning("could not set advertised name: %s", result.payload)
            return
        logger.info("now advertising as %r (was %r)", wanted, current)
        # Flood this one after identity change
        await self.mc.commands.send_advert(flood=True)

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

    async def _on_contact(self, event) -> None:
        payload = event.payload or {}
        if not isinstance(payload, dict):
            return
        key = payload.get("public_key") or payload.get("adv_key")
        if not key:
            return
        record = dict(self._contacts.get(key) or {})
        record.update(payload)
        record["public_key"] = key
        self._contacts[key] = record

    async def _contact_refresh_loop(self) -> None:
        # Learn return paths so replies can travel more than one hop.
        while True:
            try:
                for contact in await discovery.scan(self.mc):
                    key = contact.get("public_key")
                    if key:
                        self._contacts[key] = {
                            **(self._contacts.get(key) or {}), **contact}
                routable = sum(1 for c in self._contacts.values()
                               if int(c.get("out_path_len", -1)) > 0)
                logger.info("routing table: %d contacts, %d with a multi-hop path",
                            len(self._contacts), routable)
            except Exception as exc:
                logger.warning("contact refresh failed: %s", exc)
            await asyncio.sleep(CONTACT_REFRESH_SECONDS)

    async def _learn_reply_path(self, prefix: bytes) -> None:
        # Retrace the advert we heard from this client, once, and cache it.
        want = prefix.hex().lower()
        for key, contact in self._contacts.items():
            if not key.lower().startswith(want):
                continue
            if int(contact.get("out_path_len", -1)) > 0:
                return  # already routable
            if contact.get("_advert_path_tried"):
                return
            contact["_advert_path_tried"] = True
            found = await routing.advert_path(self.mc, key)
            if found is not None:
                contact["out_path"], contact["out_path_len"] = found
                logger.info("learned a %d-hop reply path to %s from its advert",
                            found[1], want)
            return

    def _reply_path(self, prefix: bytes) -> bytes:
        # Route back to the client that sent this request.
        want = prefix.hex().lower()
        for key, contact in self._contacts.items():
            if not key.lower().startswith(want):
                continue
            hops = int(contact.get("out_path_len", -1))
            path = contact.get("out_path") or ""
            if hops > 0 and path:
                logger.debug("replying to %s via %d hop(s)", want, hops)
                return bytes.fromhex(path)
            return b""  # known, and a direct neighbour
        return b""  # never heard of them; direct is the best guess

    async def _advert_loop(self) -> None:
        # Re-flood the advert occasionally so the server stays findable.
        if not self.config.advertise_as_server or self.config.advert_interval_hours <= 0:
            return
        period = self.config.advert_interval_hours * 3600
        while True:
            await asyncio.sleep(period)
            if self.mc is None:
                return
            try:
                await self.mc.commands.send_advert(flood=True)
                logger.info("re-advertised (every %.1fh)", self.config.advert_interval_hours)
            except Exception as exc:
                logger.warning("periodic advert failed: %s", exc)

    async def _heartbeat_loop(self) -> None:
        # Active probe device link rather than only reacting to it.
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            if self.mc is None:
                return
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

    async def disconnect(self) -> None:
        if self.mc is not None:
            await self.mc.disconnect()
            self.mc = None

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            # Ensure that when server is killed, DTR/RTS is dropped and cleaned
            loop.add_signal_handler(sig, stop.set)

        backoff = CONNECT_RETRY_DELAY_SECONDS
        while not stop.is_set():
            try:
                await self.connect()
            except ValueError:
                raise  # misconfigured. Exit
            except Exception as exc:
                # A radio that's unplugged for longer than one connect cycle
                logger.error("connect cycle failed, retrying in %.0fs: %s", backoff, exc)
                await self.disconnect()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, CONNECT_BACKOFF_MAX_SECONDS)
                continue
            backoff = CONNECT_RETRY_DELAY_SECONDS
            stop_task = asyncio.ensure_future(stop.wait())
            link_down_task = asyncio.ensure_future(self._link_down.wait())
            heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())
            advert_task = asyncio.ensure_future(self._advert_loop())
            contacts_task = asyncio.ensure_future(self._contact_refresh_loop())
            _, pending = await asyncio.wait(
                [stop_task, link_down_task, heartbeat_task, advert_task,
                 contacts_task],
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
        except envelope.UnsupportedVersionError as exc:
            # A real peer on a different build. 
            logger.warning(
                "request from %s speaks protocol v%d, we speak %s -- refusing",
                exc.client_pubkey_prefix.hex() if exc.client_pubkey_prefix else "?",
                exc.version,
                sorted(envelope.SUPPORTED_VERSIONS),
            )
            await self._reply_unsupported_version(exc)
            return
        except (envelope.EnvelopeError, ValueError):
            return  # not a recognized frame

        logger.debug("request from %s: %s", req.client_pubkey_prefix.hex(), req.subcommand)
        try:
            await self._learn_reply_path(req.client_pubkey_prefix)
        except Exception:
            logger.debug("reply-path lookup failed", exc_info=True)
        try:
            await self._handle_request(req)
        except Exception:
            logger.exception("error handling request %s", req.request_id)

    async def _handle_request(self, req: Request) -> None:
        if req.subcommand == Subcommand.CONTINUE:
            await self._handle_continue(req)
            return
        if req.subcommand == Subcommand.POST:
            await self._handle_post(req)
            return

        try:
            uncompressed, status = self._load_content(req)
        except PathTraversalError:
            await self._reply_error(req, StatusCode.ACCESS_DENIED)
            return

        if status != StatusCode.OK:
            await self._reply_error(req, status)
            return

        # What costs time on this link in chunks.
        raw_chunks = chunking.split(uncompressed)
        use_compressed = False
        dict_used = envelope.DICT_NONE
        chunks = raw_chunks
        if req.prefer_compressed:
            compressed, dict_id = compress.compress(
                uncompressed, peer_dict_id=req.dict_id
            )
            comp_chunks = chunking.split(compressed)
            if len(comp_chunks) < len(raw_chunks):
                use_compressed, chunks, dict_used = True, comp_chunks, dict_id
        payload = compressed if use_compressed else uncompressed
        if (
            len(chunks) > envelope.MAX_TOTAL_CHUNKS
            or len(uncompressed) > envelope.MAX_UNCOMPRESSED_SIZE
        ):
            logger.warning(
                "refusing %r: %d bytes in %d chunks, past what the header can "
                "describe (%d bytes / %d chunks)",
                req.path,
                len(uncompressed),
                len(chunks),
                envelope.MAX_UNCOMPRESSED_SIZE,
                envelope.MAX_TOTAL_CHUNKS,
            )
            await self._reply_error(req, StatusCode.ERROR)
            return

        checksum = zlib.crc32(uncompressed)

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
                dict_id=dict_used,
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
                dict_id=dict_used,
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
            dict_id=transfer.dict_id,
        )

    async def _ack_post(self, req: Request, status: StatusCode) -> None:
        # Ack one POST chunk
        await self._send_chunk(
            req,
            content_token=0,
            chunk_number=req.chunk_number,
            total_chunks=(req.post_total_chunks or 1),
            chunk_payload=b"",
            uncompressed_size=0,
            compressed=False,
            is_final=True,
            checksum=None,
            status=status,
        )

    async def _handle_post(self, req: Request) -> None:
        if not self.config.enable_posting:
            await self._ack_post(req, StatusCode.ACCESS_DENIED)
            return

        key = (req.client_pubkey_prefix, req.request_id)
        if self.uploads.already_committed(key):
            await self._ack_post(req, StatusCode.OK)  # ack a resend, don't post twice
            return

        if req.chunk_number == 0:
            total = req.post_total_chunks or 1
            if total > envelope.MAX_TOTAL_CHUNKS:
                await self._ack_post(req, StatusCode.INVALID_REQUEST)
                return
            upload = self.uploads.begin(key, req.path or "", total,
                                        req.upload_compressed, req.dict_id,
                                        req.post_checksum or 0)
            if upload is None:
                await self._ack_post(req, StatusCode.ERROR)  # too many open
                return
        else:
            upload = self.uploads.get(key)
            if upload is None:
                await self._ack_post(req, StatusCode.UNKNOWN_TOKEN)
                return

        if not upload.accept(req.chunk_number, req.post_payload or b""):
            logger.warning("post chunk %d out of range (total %d) from %s",
                           req.chunk_number, upload.total,
                           req.client_pubkey_prefix.hex())
            await self._ack_post(req, StatusCode.INVALID_REQUEST)
            return
        if not upload.complete:
            await self._ack_post(req, StatusCode.OK)  # ack, keep going
            return

        body = upload.body()
        if upload.checksum and zlib.crc32(body) != upload.checksum:
            # Assembled wrong or arrived corrupt. Keep the upload so the
            # client can resend the bad chunk instead of starting over.
            logger.warning("post from %s failed its checksum, not storing",
                           req.client_pubkey_prefix.hex())
            await self._ack_post(req, StatusCode.INVALID_REQUEST)
            return

        self.uploads.done(key)
        if upload.compressed:
            try:
                body = compress.decompress(body, dict_id=upload.dict_id)
            except (compress.DictionaryMismatch, Exception) as exc:
                logger.warning("post from %s failed to decompress: %s",
                               req.client_pubkey_prefix.hex(), exc)
                await self._ack_post(req, StatusCode.INVALID_REQUEST)
                return

        status = self._commit_post(req, upload.dest, body)
        if status == StatusCode.OK:
            self.uploads.mark_committed(key)
        await self._ack_post(req, status)

    def _commit_post(self, req: Request, dest: str, body: bytes) -> StatusCode:
        if len(body) > boards.MAX_TEXT * 2:
            return StatusCode.INVALID_REQUEST
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return StatusCode.INVALID_REQUEST

        # nick\nsubject\ntext for a new thread, nick\ntext for a reply
        p = dest.strip("/")
        if not p.startswith(page.BOARD_PREFIX + "/"):
            return StatusCode.INVALID_REQUEST
        rest = p[len(page.BOARD_PREFIX) + 1:]
        name, _, thread_part = rest.partition("/")

        thread = None
        if thread_part:
            try:
                thread = int(thread_part)
            except ValueError:
                return StatusCode.INVALID_REQUEST

        parts = text.split("\n", 2 if thread is None else 1)
        nick = parts[0].strip() if parts else ""
        if thread is None:
            subject = parts[1].strip() if len(parts) > 1 else ""
            content = parts[2] if len(parts) > 2 else ""
        else:
            subject = ""
            content = parts[1] if len(parts) > 1 else ""
        if not content.strip():
            return StatusCode.INVALID_REQUEST

        post = boards.append_post(
            self.config.boards_dir, name, author=req.client_pubkey_prefix.hex(),
            nick=nick, text=content, thread=thread, subject=subject,
        )
        if post is None:
            return StatusCode.NOT_FOUND
        logger.info("post to %s/%s by %s seq=%d", name, thread or "new", nick, post.seq)
        return StatusCode.OK

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
            dict_id=transfer.dict_id,
        )
        if is_final:
            self.store.finish(req.content_token)

    # ------------ content loading (path-safe) ----------------------------------
    def _load_content(self, req: Request) -> tuple[bytes, StatusCode]:
        if req.subcommand == Subcommand.GET_FILE and not self.config.enable_file_transfer:
            return b"", StatusCode.ACCESS_DENIED

        if req.subcommand == Subcommand.LIST_PAGES:
            listing = "# Available Pages\n\n"
            for page_file in sorted(self.config.pages_dir.glob("**/*.vn")):
                try:
                    text = page_file.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    logger.warning("skipping %s in listing: %s", page_file, exc)
                    continue
                if not self._allowed(text, req.client_pubkey_prefix):
                    continue  # an !allow'd page doesn't announce itself either
                rel = page_file.relative_to(self.config.pages_dir)
                listing += f"[{page_file.stem}|{rel}]\n"
            return listing.encode("utf-8"), StatusCode.OK

        if req.subcommand == Subcommand.GET_PAGE and (req.path or "").strip("/") == page.FILES_LISTING_PATH:
            if not self.config.enable_file_transfer:
                return b"", StatusCode.ACCESS_DENIED
            return self._list_downloads(), StatusCode.OK

        if req.subcommand == Subcommand.GET_PAGE:
            board_page = self._load_board_page(req.path or "")
            if board_page is not None:
                return board_page

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

        if req.subcommand != Subcommand.GET_PAGE:
            return file_path.read_bytes(), StatusCode.OK

        try:
            text = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("failed to read page %s: %s", file_path, exc)
            return b"", StatusCode.ERROR
        if not self._allowed(text, req.client_pubkey_prefix):
            return b"", StatusCode.ACCESS_DENIED
        return page.strip_server_directives(text).encode("utf-8"), StatusCode.OK

    def _load_board_page(self, path: str) -> tuple[bytes, StatusCode] | None:
        """Serve b, b/<board>[:seq] and b/<board>/<thread>. None if not a board path."""
        p = path.strip("/")
        if p != page.BOARD_PREFIX and not p.startswith(page.BOARD_PREFIX + "/"):
            return None
        root = self.config.boards_dir

        rest = p[len(page.BOARD_PREFIX):].strip("/")
        if not rest:
            index = boards.render_index(boards.list_boards(root),
                                        boards.board_seqs(root), root)
            return index.encode("utf-8"), StatusCode.OK

        head, _, thread_part = rest.partition("/")
        # <board>:<seq> is catch-up, <board>@<seq> pages back through history
        name, since_part, before_part = head, "", ""
        if ":" in head:
            name, _, since_part = head.partition(":")
        elif "@" in head:
            name, _, before_part = head.partition("@")
        try:
            since = int(since_part) if since_part else 0
            before = int(before_part) if before_part else 0
        except ValueError:
            return b"", StatusCode.INVALID_REQUEST

        board = boards.load_board(root, name)
        if board is None:
            return b"", StatusCode.NOT_FOUND

        if not thread_part:
            rendered = boards.render_threads(board, since, before)
            return rendered.encode("utf-8"), StatusCode.OK

        if thread_part == "archive":
            return boards.render_archive(board).encode("utf-8"), StatusCode.OK
        if thread_part.startswith("archive/"):
            month = boards.render_month(board, thread_part[len("archive/"):])
            if month is None:
                return b"", StatusCode.NOT_FOUND
            return month.encode("utf-8"), StatusCode.OK

        try:
            thread = int(thread_part)
        except ValueError:
            return b"", StatusCode.INVALID_REQUEST
        rendered = boards.render_thread(board, thread, since)
        if rendered is None:
            return b"", StatusCode.NOT_FOUND
        return rendered.encode("utf-8"), StatusCode.OK

    def _list_downloads(self) -> bytes:
        listing = "# Files\n\n"
        for f in sorted(self.config.downloads_dir.glob("**/*")):
            if not f.is_file():
                continue
            rel = f.relative_to(self.config.downloads_dir)
            listing += f"[{rel}|{rel}]\n"
        return listing.encode("utf-8")

    def _allowed(self, page_text: str, client_prefix: bytes) -> bool:
        # Process !allow directive, if it exists
        allowed_keys: list[str] = []
        for name, args in page.directives(page_text):
            if name == "allow":
                allowed_keys.extend(args)
        if not allowed_keys:
            return True
        client_hex = client_prefix.hex()
        for key in allowed_keys:
            key_hex = key.lower().replace(":", "")
            n = min(len(key_hex), len(client_hex))
            if n and key_hex[:n] == client_hex[:n]:
                return True
        return False

    # ----------------- outbound ------------------------------------------
    async def _reply_unsupported_version(
        self, exc: envelope.UnsupportedVersionError
    ) -> None:
        # Answer a frame we could only partly parse.
        if exc.request_id is None or self.mc is None:
            return
        resp = Response(
            request_id=exc.request_id,
            status=StatusCode.UNSUPPORTED_VERSION,
            chunk_number=0,
            content_token=0,
            payload=b"",
            is_final=True,
            uncompressed_size=0,
            total_chunks=1,
        )
        path = (
            self._reply_path(exc.client_pubkey_prefix)
            if exc.client_pubkey_prefix
            else b""
        )
        try:
            await self.mc.commands.send_raw_data(resp.encode(), path=path)
        except Exception as send_exc:
            logger.warning("could not answer version mismatch: %s", send_exc)

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
        dict_id: int = envelope.DICT_NONE,
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
            dict_id=dict_id,
        )

        payload = resp.encode()
        logger.info(
            "send_raw_data: request %s chunk %d (%d bytes)",
            req.request_id,
            chunk_number,
            len(payload),
        )
        started = time.monotonic()
        send_result = await self.mc.commands.send_raw_data(
            payload, path=self._reply_path(req.client_pubkey_prefix))
        elapsed = time.monotonic() - started
        if send_result.type == EventType.ERROR:
            logger.warning(
                "send_raw_data failed for request %s chunk %d: %s",
                req.request_id,
                chunk_number,
                send_result.payload,
            )
        else:
            logger.info(
                "sent reply for request %s chunk %d (%d bytes, status=%s) in %.3fs",
                req.request_id,
                chunk_number,
                len(payload),
                status.name,
                elapsed,
            )


async def _main(config_path: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        datefmt=LOG_DATEFMT,
        force=True,
    )
    config = ServerConfig.load(Path(config_path))
    server = VagrantNetServer(config)
    await server.run_forever()

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m vagrantnet.server.server /path/to/config.json")
        sys.exit(1)
    asyncio.run(_main(sys.argv[1]))
