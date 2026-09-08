# Container start-up: seed content once, write config from env, run the server.

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

DATA = Path(os.environ.get("VN_DATA", "/data"))
EXAMPLES = Path("/app/examples")
BLE_ADDRESS = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()

def env_bool(name: str, default: bool) -> bool:
    raw = env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")

def radio_kind(target: str) -> str:
    explicit = env("VN_RADIO_KIND").lower()
    if explicit:
        return explicit
    if BLE_ADDRESS.match(target):
        return "ble"
    if target.startswith("/dev/") or target.lower() == "auto":
        return "serial"
    if ":" in target:
        return "tcp"
    return "serial"

def seed() -> None:
    for name in ("pages", "boards"):
        dest = DATA / name
        dest.mkdir(parents=True, exist_ok=True)
        if any(dest.iterdir()):
            continue
        src = EXAMPLES / name
        if not src.is_dir():
            continue
        for item in src.iterdir():
            target = dest / item.name
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        print(f"seeded {name}/ with the examples")
    (DATA / "content").mkdir(parents=True, exist_ok=True)

def write_config() -> Path:
    target = env("VN_RADIO", "auto")
    config = {
        "node_name": env("VN_NODE_NAME", "vagrantNet Node"),
        "pages_dir": str(DATA / "pages"),
        "boards_dir": str(DATA / "boards"),
        "downloads_dir": str(DATA / "content"),
        "connection": {
            "kind": radio_kind(target),
            "target": target,
            "baudrate": int(env("VN_BAUDRATE", "115200")),
        },
        "advertise_as_server": env_bool("VN_ADVERTISE", True),
        "advert_interval_hours": float(env("VN_ADVERT_HOURS", "47")),
        "enable_posting": env_bool("VN_ENABLE_POSTING", True),
        "enable_file_transfer": env_bool("VN_ENABLE_FILE_TRANSFER", False),
    }
    path = DATA / "config.json"
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path

def drop_privileges(device: str | None) -> None:
    # Hand /data to a real user and stop being root.
    uid = int(env("VN_UID", "1000"))
    gid = int(env("VN_GID", "1000"))
    if os.geteuid() != 0:
        return  # already unprivileged, nothing to hand over

    for path in [DATA, *DATA.rglob("*")]:
        try:
            os.chown(path, uid, gid)
        except OSError as exc:
            print(f"could not chown {path}: {exc}", file=sys.stderr)

    # A serial radio is usually root:dialout, so keep that group or the
    # unprivileged process cannot open the port it was given.
    groups = []
    if device:
        try:
            groups.append(os.stat(device).st_gid)
        except OSError:
            pass
    try:
        os.setgroups(groups)
        os.setgid(gid)
        os.setuid(uid)
        print(f"running as {uid}:{gid}"
              + (f" (+group {groups[0]} for {device})" if groups else ""))
    except OSError as exc:
        print(f"could not drop privileges: {exc}", file=sys.stderr)

def main() -> int:
    seed()
    config = write_config()
    conn = json.loads(config.read_text())["connection"]
    print(f"radio: {conn['kind']} {conn['target']}", flush=True)

    device = None
    if conn["kind"] == "serial" and conn["target"] != "auto":
        device = conn["target"]
    drop_privileges(device)

    if device is not None and not Path(device).exists():
        print(f"\n{device} is not in the container.\n"
              f"Pass it through:  --device={device}\n"
              f"or set VN_RADIO to a host:port if the radio is on TCP.\n",
              file=sys.stderr)
        return 1

    os.execvp(sys.executable,
              [sys.executable, "-u", "-m", "vagrantnet.server.server", str(config)])

if __name__ == "__main__":
    raise SystemExit(main())
