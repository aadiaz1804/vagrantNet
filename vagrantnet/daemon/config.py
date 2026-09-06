"""Daemon configuration, loaded from a JSON files from config.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class ConnectionConfig:
    # One of: "serial", "ble", "tcp"
    kind: str = "serial"
    # serial: device path e.g. "/dev/ttyUSB0"; ble: device address or name; tcp: "host:port"
    target: str = "/dev/ttyUSB0"
    baudrate: int = 115200  # serial only ignored on BLE and TCP

@dataclass
class DaemonConfig:
    node_name: str = "vagrantNet Node"
    pages_dir: Path = Path("~/vn/pages").expanduser()
    downloads_dir: Path = Path("~/vn/content").expanduser()
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)

    # rate limiting config caps guard against ERR_CODE_TABLE_FULL (firmware send-queue exhaustion) if clients overwhelm the LoRa link
    max_in_flight_transfers_per_client: int = 2
    max_in_flight_transfers_total: int = 8
    content_token_ttl_seconds: int = 300
    content_token_linger_seconds: int = 60 # How long a finished transfer stays fetchable

    @staticmethod
    def load(path: Path) -> "DaemonConfig":
        raw = json.loads(path.read_text())
        conn_raw = raw.get("connection", {})
        return DaemonConfig(
            node_name=raw.get("node_name", "vagrantNet Node"),
            pages_dir=Path(raw.get("pages_dir", "~/vn/pages")).expanduser(),
            downloads_dir=Path(
                raw.get("downloads_dir", "~/vn/content")
            ).expanduser(),
            connection=ConnectionConfig(
                kind=conn_raw.get("kind", "serial"),
                target=conn_raw.get("target", "/dev/ttyUSB0"),
                baudrate=conn_raw.get("baudrate", 115200),
            ),
            max_in_flight_transfers_per_client=raw.get(
                "max_in_flight_transfers_per_client", 2
            ),
            max_in_flight_transfers_total=raw.get("max_in_flight_transfers_total", 8),
            content_token_ttl_seconds=raw.get("content_token_ttl_seconds", 300),
            content_token_linger_seconds=raw.get("content_token_linger_seconds", 60),
        )
