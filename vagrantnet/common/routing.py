"""Finding a path to a peer, cheapest source first.

    stored out_path        no radio, ~1% of contacts on the mesh
    advert path reversed   low radio, <9% (measured 2026-09-07, 199 contacts)
    path discovery         high radio, >9% one bounded exchange, then cached
"""

from __future__ import annotations
import logging

logger = logging.getLogger("vagrantnet.routing")

FLOOD = -1  # no usable path
def reverse_path(path_hex: str, hash_mode: int) -> str:
    """Turn an inbound path into the outbound path that retraces it."""
    size = max(hash_mode, 0) + 1
    try:
        raw = bytes.fromhex(path_hex or "")
    except ValueError:
        logger.warning("malformed path %r from the radio, ignoring", path_hex)
        return ""
    usable = len(raw) - (len(raw) % size)
    hops = [raw[i:i + size] for i in range(0, usable, size)]
    return b"".join(reversed(hops)).hex()

async def advert_path(mc, pubkey_hex: str) -> tuple[str, int] | None:
    from meshcore import EventType

    try:
        event = await mc.commands.get_advert_path(bytes.fromhex(pubkey_hex))
    except Exception as exc:
        logger.debug("advert path lookup failed for %s: %s", pubkey_hex[:12], exc)
        return None
    if event is None or event.type == EventType.ERROR:
        return None

    payload = event.payload or {}
    hops = int(payload.get("path_len", FLOOD))
    path = payload.get("path") or ""
    mode = int(payload.get("path_hash_mode", 0))
    if hops <= 0 or not path:
        return None  # flooded advert, or a direct neighbour with nothing to retrace

    reversed_hex = reverse_path(path, mode)
    logger.info("using advert path to %s: %d hop(s)", pubkey_hex[:12], hops)
    return reversed_hex, hops
