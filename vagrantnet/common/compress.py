"""zstd compression for page and file payloads.

   Known limitations:
       1. The dictionary is built from English-language pages ONLY
       2. Pages with non-standard formatting or file transfers may not compress well

Both ends must hold the same dictionary to talk compressed
DICT_ID travels in three spare bits of the envelope's ctrl byte
the sender compresses with a dictionary only when the
receiver has said it holds that one and a mismatch costs compression ratio.
"""
from __future__ import annotations
import functools
import logging
import pathlib

import zstandard as zstd

logger = logging.getLogger("vagrantnet.compress")
DEFAULT_LEVEL = 19  # pages are tiny and compressed once; ratio over speed
DICT_PATH = pathlib.Path(__file__).with_name("vn_dict.bin")

# Identifies vn_dict.bin on the wire
DICT_ID = 1
DICT_NONE = 0

class DictionaryMismatch(ValueError):
    pass

@functools.lru_cache(maxsize=1)
def dictionary() -> zstd.ZstdCompressionDict | None:
    """The built-in page dictionary, or None if it is missing.

    Missing is survivable -- a broken install still talks to peers that have
    no dictionary either -- so warn rather than refusing to start.
    """
    try:
        return zstd.ZstdCompressionDict(DICT_PATH.read_bytes())
    except OSError as exc:
        logger.warning("no page dictionary at %s (%s); compressing without one",
                       DICT_PATH, exc)
        return None

def active_dict_id() -> int:
    # The dictionary this install actually holds for advertising
    return DICT_ID if dictionary() is not None else DICT_NONE

def compress(data: bytes, level: int = DEFAULT_LEVEL, *,
             peer_dict_id: int | None = None) -> tuple[bytes, int]:
    # Compress for a peer
    zdict = dictionary()
    use_dict = zdict is not None and (
        peer_dict_id is None or peer_dict_id == DICT_ID
    )
    cctx = (zstd.ZstdCompressor(level=level, dict_data=zdict) if use_dict
            else zstd.ZstdCompressor(level=level))
    return cctx.compress(data), (DICT_ID if use_dict else DICT_NONE)

def decompress(data: bytes, *, dict_id: int | None = None) -> bytes:
    # Decompress a payload the sender says it built with dictionary dict_id.
    ours = active_dict_id()
    if dict_id is not None and dict_id != DICT_NONE and dict_id != ours:
        raise DictionaryMismatch(
            f"payload was compressed with dictionary {dict_id}, this build "
            f"has {'dictionary ' + str(ours) if ours else 'none'} -- both "
            "ends need the same vagrantNet version"
        )
    zdict = dictionary()
    dctx = (zstd.ZstdDecompressor(dict_data=zdict) if zdict
            else zstd.ZstdDecompressor())
    try:
        return dctx.decompress(data)
    except zstd.ZstdError as exc:
        if "dictionary" in str(exc).lower():
            raise DictionaryMismatch(
                f"cannot decompress: {exc}. Dictionary check failed"
            ) from exc
        raise
