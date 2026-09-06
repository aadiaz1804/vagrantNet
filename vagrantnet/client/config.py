"""Client-side persisted state: known servers, favorites, last connection. Separate from daemon/config.py's ServerConfig """

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("~/.config/vagrantnet/client.json").expanduser()

@dataclass
class Favorite:
    server: str  # server name (looked up in `servers`) or a raw pubkey hex
    path: str
    label: str

@dataclass
class ClientConfig:
    path: Path = field(default_factory=lambda: DEFAULT_CONFIG_PATH)
    # radio to connect to by default -- kind is "serial" or "ble"
    last_connection_kind: str | None = None
    last_connection_target: str | None = None
    last_connection_pin: str | None = None
    servers: dict[str, str] = field(default_factory=dict)  # name -> pubkey hex
    favorites: list[Favorite] = field(default_factory=list)
    # Flood our advert on connect. Needed to reach servers more than one hop away
    flood_advert: bool = False

    @staticmethod
    def load(path: Path = DEFAULT_CONFIG_PATH) -> "ClientConfig":
        if not path.exists():
            return ClientConfig(path=path)
        raw = json.loads(path.read_text())
        conn = raw.get("last_connection") or {}
        return ClientConfig(
            path=path,
            last_connection_kind=conn.get("kind"),
            last_connection_target=conn.get("target"),
            last_connection_pin=conn.get("pin"),
            servers=dict(raw.get("servers", {})),
            favorites=[
                Favorite(server=f["server"], path=f["path"], label=f["label"])
                for f in raw.get("favorites", [])
            ],
            flood_advert=bool(raw.get("flood_advert", False)),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "last_connection": {
                "kind": self.last_connection_kind,
                "target": self.last_connection_target,
                "pin": self.last_connection_pin,
            },
            "servers": self.servers,
            "flood_advert": self.flood_advert,
            "favorites": [
                {"server": f.server, "path": f.path, "label": f.label}
                for f in self.favorites
            ],
        }
        self.path.write_text(json.dumps(raw, indent=2))

    def resolve_server(self, name_or_pubkey: str) -> str:
        """A server can be referred to by its saved name or a raw pubkey hex."""
        return self.servers.get(name_or_pubkey, name_or_pubkey)

    def set_last_connection(self, kind: str, target: str, pin: str | None = None) -> None:
        self.last_connection_kind = kind
        self.last_connection_target = target
        self.last_connection_pin = pin
        self.save()

    def add_server(self, name: str, pubkey_hex: str) -> None:
        self.servers[name] = pubkey_hex
        self.save()

    def add_favorite(self, server: str, path: str, label: str) -> None:
        self.favorites.append(Favorite(server=server, path=path, label=label))
        self.save()

    def remove_favorite(self, index: int) -> Favorite | None:
        if 0 <= index < len(self.favorites):
            fav = self.favorites.pop(index)
            self.save()
            return fav
        return None

    def remove_server(self, name: str) -> str | None:
        pubkey = self.servers.pop(name, None)
        if pubkey is not None:
            self.save()
        return pubkey
