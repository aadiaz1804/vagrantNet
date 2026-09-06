"""zstd compression for page and file payloads.
   Known limitations: 
       1. The dictionary is built from English-language pages ONLY
       2. Pages with non-standard formatting or file transfers may not compress well
       3. As the dictionary is needed on server/client its not possible to change without access to both devices or transmitting it
"""
# TODO: Add support for dict missmatch/negotiation, and custom dictionaries for file transfers/non-english language.
from __future__ import annotations
import functools
import logging
import pathlib

import zstandard as zstd

logger = logging.getLogger("vagrantnet.compress")
DEFAULT_LEVEL = 19  # pages are tiny and compressed once; ratio over speed
DICT_PATH = pathlib.Path(__file__).with_name("vn_dict.bin")

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

def compress(data: bytes, level: int = DEFAULT_LEVEL) -> bytes:
    zdict = dictionary()
    cctx = (zstd.ZstdCompressor(level=level, dict_data=zdict) if zdict
            else zstd.ZstdCompressor(level=level))
    return cctx.compress(data)

def decompress(data: bytes) -> bytes:
    zdict = dictionary()
    dctx = (zstd.ZstdDecompressor(dict_data=zdict) if zdict
            else zstd.ZstdDecompressor())
    return dctx.decompress(data)
