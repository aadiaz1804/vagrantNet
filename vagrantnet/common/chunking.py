"""Splits payloads into wire-sized chunks and reassembles them."""

from __future__ import annotations

from .envelope import (
    MAX_SAFE_PAYLOAD,
    RESP_FIXED_LEN,
    RESP_FIRST_EXTRA_LEN,
)

# size chunks for the worst case (a single chunk that's both first and final)
# so every chunk fits regardless of position
_CRC_LEN = 4
MAX_CHUNK_PAYLOAD = MAX_SAFE_PAYLOAD - RESP_FIXED_LEN - RESP_FIRST_EXTRA_LEN - _CRC_LEN

if MAX_CHUNK_PAYLOAD <= 0:
    raise RuntimeError("envelope header overhead leaves no room for chunk payload")

def split(data: bytes, chunk_size: int = MAX_CHUNK_PAYLOAD) -> list[bytes]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if not data:
        return [b""]  # still need one (empty) chunk to carry status/size=0
    return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]

class ReassemblyError(ValueError):
    pass

def reassemble(chunks_by_number: dict[int, bytes], total_chunks: int) -> bytes:
    missing = [n for n in range(total_chunks) if n not in chunks_by_number]
    if missing:
        raise ReassemblyError(f"missing chunks: {missing}")
    return b"".join(chunks_by_number[n] for n in range(total_chunks))
