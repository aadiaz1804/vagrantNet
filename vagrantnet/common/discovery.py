"""Finding vagrantNet servers minimizing mesh traffic
    1  companion / chat node      2  repeater      3  room server
A fourth value for "vagrantNet server" is the right long-term answer
`adv_type` is decided by which firmware is flashed. 
For now vNet server is a "companion node" that advertises as [vNet]
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from meshcore import EventType

logger = logging.getLogger("vagrantnet.discovery")

# v1 Name marker for vagrantNet servers, in 32-byte advert name field.
MARKER = "[vNet]"

ADV_TYPE_CHAT = 1
ADV_TYPE_REPEATER = 2
ADV_TYPE_ROOM = 3
ADV_TYPE_NAMES = {ADV_TYPE_CHAT: "node", ADV_TYPE_REPEATER: "repeater",
                  ADV_TYPE_ROOM: "room"}

# Advert names are a fixed 32-byte field.
MAX_ADV_NAME = 32
SCAN_TIMEOUT_SECONDS = 12.0
SCAN_QUIET_SECONDS = 2.0  # contacts scan time 

def is_marked(name: str | None) -> bool:
    return bool(name) and MARKER in name

def mark(name: str) -> str:
    # Add vNet marker
    if is_marked(name):
        return name
    room = MAX_ADV_NAME - len(MARKER) - 1
    return f"{name[:room].rstrip()} {MARKER}"

def unmark(name: str | None) -> str:
    return (name or "").replace(MARKER, "").strip()

@dataclass
class Found:
    # A node the radio has heard of that claims to serve vNet
    name: str          # display name, marker stripped
    pubkey: str
    adv_type: int
    hops: int          # out_path_len; -1 means flood-only, no direct path yet
    last_advert: int

    @property
    def kind(self) -> str:
        return ADV_TYPE_NAMES.get(self.adv_type, f"type{self.adv_type}")

    @property
    def reachable(self) -> bool:
        """A flood-only contact has no path to send a request along, and the
        daemon replies zero-hop, so anything past one hop can't answer yet."""
        return self.hops >= 0

async def scan(mc, timeout: float = SCAN_TIMEOUT_SECONDS) -> list[dict]:
    # Stream the radio's contact table
    seen: dict[str, dict] = {}
    last_arrival = time.monotonic()

    def collect(event) -> None:
        nonlocal last_arrival
        payload = event.payload or {}
        if isinstance(payload, dict) and payload.get("public_key"):
            seen[payload["public_key"]] = payload
            last_arrival = time.monotonic()

    subs = [mc.subscribe(evt, collect)
            for evt in (EventType.NEW_CONTACT, EventType.NEXT_CONTACT)]
    try:
        await mc.commands.get_contacts_async()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            if seen and time.monotonic() - last_arrival > SCAN_QUIET_SECONDS:
                break
    finally:
        for sub in subs:
            try:
                mc.unsubscribe(sub)
            except Exception:
                logger.debug("could not unsubscribe from contact stream")
    logger.info("contact scan saw %d nodes", len(seen))
    return list(seen.values())

def servers(contacts: list[dict]) -> list[Found]:
    # Pick the vagrantNet servers
    out = [
        Found(
            name=unmark(c.get("adv_name")) or "(unnamed)",
            pubkey=c.get("public_key", ""),
            adv_type=c.get("type", 0),
            hops=int(c.get("out_path_len", -1)),
            last_advert=int(c.get("last_advert") or 0),
        )
        for c in contacts
        if is_marked(c.get("adv_name"))
    ]
    # Reachable first, then fewest hops, then most recently heard.
    out.sort(key=lambda f: (not f.reachable, f.hops, -f.last_advert))
    return out

def census(contacts: list[dict]) -> dict[str, int]:
    # What the radio can hear
    counts: dict[str, int] = {}
    for c in contacts:
        kind = ADV_TYPE_NAMES.get(c.get("type"), "other")
        counts[kind] = counts.get(kind, 0) + 1
    return counts
