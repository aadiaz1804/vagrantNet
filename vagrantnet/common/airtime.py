""" LoRa time-on-air calculator

A page that is cheap in bytes can still be expensive here,
because every chunk is a separate transmission and every chunk costs a
round trip.

The formula is the one in Semtech's SX127x datasheet (section 4.1.1.7).

    16 B -> 103 ms    64 B -> 236 ms    200 B -> 635 ms
    32 B -> 144 ms   120 B -> 400 ms

Defaults below are the USA/Canada recommended preset. Change PRESET if
your mesh runs other configs
"""

from __future__ import annotations

import math
from dataclasses import dataclass

@dataclass(frozen=True)
class Preset:
    name: str
    freq_mhz: float
    bandwidth_hz: int
    spreading_factor: int
    coding_rate: int  # 1 = 4/5, 2 = 4/6, 3 = 4/7, 4 = 4/8

# USA/Canada default. Ottawa (GOME) runs this.
PRESET = Preset("US/CA default", 910.525, 62_500, 7, 1)

# What one node can spend safely
MESH_DAILY_BUDGET_SECONDS = 13_000

# MeshCore wraps our envelope in its own header before it hits the air.
# Measured against the reference table
MESHCORE_HEADER_BYTES = 29
PREAMBLE_SYMBOLS = 8

def time_on_air(frame_bytes: int, preset: Preset = PRESET) -> float:
    # Seconds a frame of this size occupies the channel.
    sf = preset.spreading_factor
    symbol_seconds = (2 ** sf) / preset.bandwidth_hz
    preamble = (PREAMBLE_SYMBOLS + 4.25) * symbol_seconds

    # low-data-rate optimize kicks in when a symbol is over ~16ms
    de = 1 if symbol_seconds > 0.016 else 0
    numerator = 8 * frame_bytes - 4 * sf + 28 + 16  # 16 = CRC on, explicit header
    denominator = 4 * (sf - 2 * de)
    payload_symbols = 8 + max(
        math.ceil(numerator / denominator) * (preset.coding_rate + 4), 0
    )
    return preamble + payload_symbols * symbol_seconds

def wire_time(envelope_bytes: int, preset: Preset = PRESET) -> float:
    # Envelope airtime
    return time_on_air(envelope_bytes + MESHCORE_HEADER_BYTES, preset)

def fetch_cost(chunks: int, path_len: int = 8, preset: Preset = PRESET) -> float:
    # Whole fetch airtime Zero hop. 
    # Every extra routed hop multiplies this by roughly the hop count
    from .envelope import MAX_SAFE_PAYLOAD, REQ_FIXED_LEN

    request = wire_time(REQ_FIXED_LEN + path_len, preset)
    continues = (chunks - 1) * wire_time(REQ_FIXED_LEN + 1, preset)
    responses = chunks * wire_time(MAX_SAFE_PAYLOAD, preset)
    return request + continues + responses

def share_of_mesh_day(seconds: float) -> float:
    # Airtime as a percentage of everything the mesh has in a day.
    return seconds / MESH_DAILY_BUDGET_SECONDS * 100.0
