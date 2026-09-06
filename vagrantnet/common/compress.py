"""zstd wrapper for page/file payloads. Falls back to no dictionary until one is trained."""

from __future__ import annotations
from pathlib import Path

import zstandard as zstd

DEFAULT_LEVEL = 19  # max non-"ultra" level; pages are tiny, compress once, ratio > speed

def _load_dict(dict_path: Path | None) -> zstd.ZstdCompressionDict | None:
    if dict_path is None or not dict_path.exists():
        return None
    return zstd.ZstdCompressionDict(dict_path.read_bytes())

def compress(data: bytes, dict_path: Path | None = None, level: int = DEFAULT_LEVEL) -> bytes:
    zdict = _load_dict(dict_path)
    cctx = zstd.ZstdCompressor(level=level, dict_data=zdict) if zdict else zstd.ZstdCompressor(level=level)
    return cctx.compress(data)

def decompress(data: bytes, dict_path: Path | None = None) -> bytes:
    zdict = _load_dict(dict_path)
    dctx = zstd.ZstdDecompressor(dict_data=zdict) if zdict else zstd.ZstdDecompressor()
    return dctx.decompress(data)

def train_dictionary(sample_paths: list[Path], out_path: Path, dict_size: int = 4096) -> None:
    """One-off: train a dictionary from a subset of .vn pages."""
    samples = [p.read_bytes() for p in sample_paths if p.stat().st_size > 0]
    if len(samples) < 5:
        raise ValueError("need at least ~5 sample pages to train a useful dictionary")
    zdict = zstd.train_dictionary(dict_size, samples)
    out_path.write_bytes(zdict.as_bytes())
