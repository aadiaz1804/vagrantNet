"""vagrantNet wire envelope, carried as CMD_SEND_RAW_DATA payload."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

PROTO_MARKER = 0b01  # top 2 bits of ctrl byte; soft sanity check, not crypto
VERSION = 1
SUPPORTED_VERSIONS = frozenset({VERSION})

# The version field is two bits, so it can only ever say 0-3 
VERSION_EXTENDED = 0b11

MAX_SAFE_PAYLOAD = 160 # Max verified payload size for a single CMD_SEND_RAW_DATA
MAX_TOTAL_CHUNKS = 0xFF        # total_chunks is one byte
MAX_UNCOMPRESSED_SIZE = 0xFFFF  # uncompressed_size is two bytes
PUB_KEY_PREFIX_LEN = 6
PUB_KEY_SIZE = 32  # full key, used only off-wire for contact lookup

class MsgType(IntEnum):
    REQUEST = 0
    RESPONSE = 1

class Subcommand(IntEnum):
    GET_PAGE = 0
    GET_FILE = 1
    LIST_PAGES = 2
    CONTINUE = 3  # fetch next chunk of an already-started transfer, by token
    POST = 4  # one chunk of a board post, client -> server

class StatusCode(IntEnum):
    OK = 0
    NOT_FOUND = 1
    ACCESS_DENIED = 2
    ERROR = 3
    INVALID_REQUEST = 4
    UNKNOWN_TOKEN = 5  # CONTINUE referenced an expired/unknown token. Retry GET_PAGE/GET_FILE
    UNSUPPORTED_VERSION = 6  # peer speaks a protocol version we don't
    # 7 is the last value the 3-bit status field can carry.

REQ_FLAG_PREFER_COMPRESSED = 0b0001

# subcmd_flags: bits 0-2 subcommand, bit 3 prefer-compressed, bit 4 upload
# compressed (POST only), bits 5-7 attempt counter.
REQ_FLAG_UPLOAD_COMPRESSED = 0b00010000

# The mesh suppresses duplicate packets by hash to stop flood
# This counter is to make all retry's different
REQ_ATTEMPT_SHIFT = 5
REQ_ATTEMPT_MASK = 0b111

# status_flags packs StatusCode (bits 0-2) and these flags into one byte 
# flags start at bit 3 to stay clear of it.
RESP_FLAG_COMPRESSED = 0b00001000  # bit 3
RESP_FLAG_FINAL_CHUNK = 0b00010000  # bit 4
RESP_FLAG_HAS_CHECKSUM = 0b00100000  # bit 5
RESP_FLAG_IS_FIRST = 0b01000000  # bit 6 size + total_chunks
# bit 7 reserved

# ctrl byte:  MM VV T DDD
#   MM  bits 6-7  protocol marker
#   VV  bits 4-5  version
#   T   bit 3     message type
#   DDD bits 0-2  compression dictionary id (0 = none)
DICT_ID_MASK = 0b111
DICT_NONE = 0

def _pack_ctrl(msg_type: MsgType, dict_id: int = DICT_NONE) -> int:
    return (
        (PROTO_MARKER << 6)
        | ((VERSION & 0b11) << 4)
        | ((int(msg_type) & 0b1) << 3)
        | (dict_id & DICT_ID_MASK)
    )

def _unpack_ctrl(ctrl: int) -> tuple[int, int, MsgType, int]:
    marker = (ctrl >> 6) & 0b11
    version = (ctrl >> 4) & 0b11
    msg_type = MsgType((ctrl >> 3) & 0b1)
    dict_id = ctrl & DICT_ID_MASK
    return marker, version, msg_type, dict_id

class EnvelopeError(ValueError):
    pass

# Header: ctrl(1) request_id(2) client_pubkey_prefix(6).
FROZEN_HEADER_LEN = 9

class UnsupportedVersionError(EnvelopeError):
    # Peer speaks a version we don't. Carry answer.
    def __init__(self, version: int, request_id: int | None = None,
                 client_pubkey_prefix: bytes | None = None):
        self.version = version
        self.request_id = request_id
        self.client_pubkey_prefix = client_pubkey_prefix
        super().__init__(
            f"peer speaks protocol version {version}, this build speaks "
            f"{sorted(SUPPORTED_VERSIONS)}"
        )

def _check_version(version: int, data: bytes) -> None:
    if version in SUPPORTED_VERSIONS:
        return
    request_id = prefix = None
    if len(data) >= FROZEN_HEADER_LEN:
        request_id = int.from_bytes(data[1:3], "little")
        prefix = data[3:FROZEN_HEADER_LEN]
    raise UnsupportedVersionError(version, request_id, prefix)

def _check_size(out: bytes, what: str) -> bytes:
    if len(out) > MAX_SAFE_PAYLOAD:
        raise EnvelopeError(
            f"encoded {what} is {len(out)} bytes, exceeds MAX_SAFE_PAYLOAD "
            f"({MAX_SAFE_PAYLOAD})"
        )
    return out

# ------------ REQUEST FORMAT -------------------------------------------------------
# ctrl(1) request_id(2) client_pubkey_prefix(6) subcmd_flags(1) chunk_number(1)
#   then EITHER content_token(1)  [subcommand == CONTINUE]
#        OR      path (utf-8, variable)  [subcommand in GET_PAGE/GET_FILE/LIST_PAGES]
_REQ_FIXED = struct.Struct("<BH6sBB")
REQ_FIXED_LEN = _REQ_FIXED.size  # 1+2+6+1+1 = 11

@dataclass
class Request:
    request_id: int  # 0-65535
    client_pubkey_prefix: bytes  # 6 bytes
    subcommand: Subcommand
    chunk_number: int = 0  # 0-255
    path: str | None = None          # required unless subcommand == CONTINUE
    content_token: int | None = None  # required if subcommand == CONTINUE
    prefer_compressed: bool = True
    # Which compression dictionary this client has.
    dict_id: int = DICT_NONE
    # POST only: how many chunks the whole post takes, and this chunk's slice
    post_total_chunks: int | None = None
    post_payload: bytes | None = None
    post_checksum: int | None = None  # crc32 of the whole post, on chunk 0
    upload_compressed: bool = False
    attempt: int = 0  # retry counter, only so retries differ on the wire

    def encode(self) -> bytes:
        if len(self.client_pubkey_prefix) != PUB_KEY_PREFIX_LEN:
            raise EnvelopeError(
                f"client_pubkey_prefix must be {PUB_KEY_PREFIX_LEN} bytes"
            )
        flags = REQ_FLAG_PREFER_COMPRESSED if self.prefer_compressed else 0
        subcmd_flags = (int(self.subcommand) & 0b111) | ((flags & 0b1) << 3)
        if self.upload_compressed:
            subcmd_flags |= REQ_FLAG_UPLOAD_COMPRESSED
        subcmd_flags |= (self.attempt & REQ_ATTEMPT_MASK) << REQ_ATTEMPT_SHIFT

        fixed = _REQ_FIXED.pack(
            _pack_ctrl(MsgType.REQUEST, self.dict_id),
            self.request_id & 0xFFFF,
            self.client_pubkey_prefix,
            subcmd_flags,
            self.chunk_number & 0xFF,
        )

        if self.subcommand == Subcommand.CONTINUE:
            if self.content_token is None:
                raise EnvelopeError("CONTINUE requires content_token")
            tail = struct.pack("<B", self.content_token & 0xFF)
        elif self.subcommand == Subcommand.POST:
            body = self.post_payload or b""
            if self.chunk_number == 0:
                # only the first chunk names its destination
                if self.path is None or self.post_total_chunks is None:
                    raise EnvelopeError("POST chunk 0 requires path and post_total_chunks")
                dest = self.path.encode("utf-8")
                if len(dest) > 0xFF:
                    raise EnvelopeError("POST path too long")
                tail = struct.pack("<BIB", self.post_total_chunks & 0xFF,
                                   (self.post_checksum or 0) & 0xFFFFFFFF,
                                   len(dest)) + dest + body
            else:
                tail = body
        else:
            if self.path is None:
                raise EnvelopeError(f"{self.subcommand.name} requires path")
            tail = self.path.encode("utf-8")

        return _check_size(fixed + tail, "request")

    @staticmethod
    def decode(data: bytes) -> "Request":
        if len(data) < REQ_FIXED_LEN:
            raise EnvelopeError("frame too short for a request header")
        ctrl, request_id, pubkey_prefix, subcmd_flags, chunk_number = (
            _REQ_FIXED.unpack(data[:REQ_FIXED_LEN])
        )
        marker, version, msg_type, dict_id = _unpack_ctrl(ctrl)
        if marker != PROTO_MARKER or msg_type != MsgType.REQUEST:
            raise EnvelopeError("not a valid request frame")
        # Marker first, version second
        _check_version(version, data)

        subcommand = Subcommand(subcmd_flags & 0b111)
        prefer_compressed = bool((subcmd_flags >> 3) & 0b1)
        upload_compressed = bool(subcmd_flags & REQ_FLAG_UPLOAD_COMPRESSED)
        attempt = (subcmd_flags >> REQ_ATTEMPT_SHIFT) & REQ_ATTEMPT_MASK
        tail = data[REQ_FIXED_LEN:]

        if subcommand == Subcommand.POST:
            path = None
            total = None
            body = tail
            checksum = None
            if chunk_number == 0:
                if len(tail) < 6:
                    raise EnvelopeError("POST chunk 0 missing its header")
                total, checksum, path_len = struct.unpack("<BIB", tail[:6])
                if len(tail) < 6 + path_len:
                    raise EnvelopeError("POST chunk 0 truncated path")
                path = tail[6:6 + path_len].decode("utf-8", errors="strict")
                body = tail[6 + path_len:]
            return Request(
                request_id=request_id,
                client_pubkey_prefix=pubkey_prefix,
                subcommand=subcommand,
                chunk_number=chunk_number,
                path=path,
                prefer_compressed=prefer_compressed,
                dict_id=dict_id,
                post_total_chunks=total,
                post_payload=body,
                post_checksum=checksum,
                upload_compressed=upload_compressed,
                attempt=attempt,
            )

        if subcommand == Subcommand.CONTINUE:
            if len(tail) < 1:
                raise EnvelopeError("CONTINUE frame missing content_token")
            return Request(
                request_id=request_id,
                client_pubkey_prefix=pubkey_prefix,
                subcommand=subcommand,
                chunk_number=chunk_number,
                content_token=tail[0],
                prefer_compressed=prefer_compressed,
                dict_id=dict_id,
                attempt=attempt,
            )

        return Request(
            request_id=request_id,
            client_pubkey_prefix=pubkey_prefix,
            subcommand=subcommand,
            chunk_number=chunk_number,
            path=tail.decode("utf-8", errors="strict"),
            prefer_compressed=prefer_compressed,
            dict_id=dict_id,
            attempt=attempt,
        )

# ------------ RESPONSE FORMAT ------------------------------------------------
# ctrl(1) request_id(2) status_flags(1) chunk_number(1) content_token(1)
#   IF is_first: uncompressed_size(2) total_chunks(1)
#   payload(var)
#   IF final AND has_checksum: crc32(4)
_RESP_FIXED = struct.Struct("<BHBBB")
RESP_FIXED_LEN = _RESP_FIXED.size  # 1+2+1+1+1 = 6
_RESP_FIRST_EXTRA = struct.Struct("<HB")
RESP_FIRST_EXTRA_LEN = _RESP_FIRST_EXTRA.size  # 2+1 = 3


@dataclass
class Response:
    request_id: int
    status: StatusCode
    chunk_number: int
    content_token: int  # server-assigned on chunk 0, echoed on every chunk
    payload: bytes = b""
    compressed: bool = False
    is_final: bool = False
    checksum: int | None = None
    # only meaningful / transmitted when chunk_number == 0:
    uncompressed_size: int | None = None
    total_chunks: int | None = None
    # Which dictionary the payload was actually compressed with
    dict_id: int = DICT_NONE

    @property
    def is_first(self) -> bool:
        return self.chunk_number == 0

    def encode(self) -> bytes:
        status_flags = int(self.status) & 0b111
        if self.compressed:
            status_flags |= RESP_FLAG_COMPRESSED
        if self.is_final:
            status_flags |= RESP_FLAG_FINAL_CHUNK
        if self.is_final and self.checksum is not None:
            status_flags |= RESP_FLAG_HAS_CHECKSUM
        if self.is_first:
            status_flags |= RESP_FLAG_IS_FIRST
            if self.uncompressed_size is None or self.total_chunks is None:
                raise EnvelopeError(
                    "first chunk (chunk_number=0) requires uncompressed_size "
                    "and total_chunks"
                )
            # Check if chunking are within the hard limits of the payload
            if self.total_chunks > MAX_TOTAL_CHUNKS:
                raise EnvelopeError(
                    f"total_chunks {self.total_chunks} exceeds {MAX_TOTAL_CHUNKS}"
                )
            if self.uncompressed_size > MAX_UNCOMPRESSED_SIZE:
                raise EnvelopeError(
                    f"uncompressed_size {self.uncompressed_size} exceeds "
                    f"{MAX_UNCOMPRESSED_SIZE}"
                )

        fixed = _RESP_FIXED.pack(
            _pack_ctrl(MsgType.RESPONSE, self.dict_id),
            self.request_id & 0xFFFF,
            status_flags,
            self.chunk_number & 0xFF,
            self.content_token & 0xFF,
        )

        extra = b""
        if self.is_first:
            extra = _RESP_FIRST_EXTRA.pack(
                self.uncompressed_size & 0xFFFF, self.total_chunks & 0xFF
            )

        out = fixed + extra + self.payload
        if self.is_final and self.checksum is not None:
            out += struct.pack("<I", self.checksum & 0xFFFFFFFF)

        return _check_size(out, "response chunk")

    @staticmethod
    def decode(data: bytes) -> "Response":
        if len(data) < RESP_FIXED_LEN:
            raise EnvelopeError("frame too short for a response header")
        ctrl, request_id, status_flags, chunk_number, content_token = (
            _RESP_FIXED.unpack(data[:RESP_FIXED_LEN])
        )
        marker, version, msg_type, dict_id = _unpack_ctrl(ctrl)
        if marker != PROTO_MARKER or msg_type != MsgType.RESPONSE:
            raise EnvelopeError("not a valid response frame")
        _check_version(version, data)

        status = StatusCode(status_flags & 0b111)
        compressed = bool(status_flags & RESP_FLAG_COMPRESSED)
        is_final = bool(status_flags & RESP_FLAG_FINAL_CHUNK)
        has_checksum = bool(status_flags & RESP_FLAG_HAS_CHECKSUM)
        is_first = bool(status_flags & RESP_FLAG_IS_FIRST)

        rest = data[RESP_FIXED_LEN:]
        checksum = None
        if is_final and has_checksum:
            if len(rest) < 4:
                raise EnvelopeError("final chunk flagged with checksum but too short")
            checksum = struct.unpack("<I", rest[-4:])[0]
            rest = rest[:-4]

        uncompressed_size = total_chunks = None
        if is_first:
            if len(rest) < RESP_FIRST_EXTRA_LEN:
                raise EnvelopeError("first-chunk frame missing size/total_chunks")
            uncompressed_size, total_chunks = _RESP_FIRST_EXTRA.unpack(
                rest[:RESP_FIRST_EXTRA_LEN]
            )
            rest = rest[RESP_FIRST_EXTRA_LEN:]

        return Response(
            request_id=request_id,
            status=status,
            chunk_number=chunk_number,
            content_token=content_token,
            payload=rest,
            compressed=compressed,
            is_final=is_final,
            checksum=checksum,
            uncompressed_size=uncompressed_size,
            total_chunks=total_chunks,
            dict_id=dict_id,
        )
