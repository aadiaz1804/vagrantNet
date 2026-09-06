"""Path-traversal-safe file resolution """

from __future__ import annotations
from pathlib import Path

class PathTraversalError(ValueError):
    pass

def resolve_within(root: Path, relative: str) -> Path:
    # Resolve `relative` against `root`, or raise PathTraversalError.
    if not relative or relative.startswith(("/", "\\")):
        raise PathTraversalError(f"absolute or empty path not allowed: {relative!r}")

    root_resolved = root.resolve()
    candidate = (root / relative).resolve()

    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        raise PathTraversalError(
            f"{relative!r} resolves outside root {root_resolved}"
        ) from None

    return candidate
