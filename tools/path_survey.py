"""How many nodes can the radio actually route to:

  out_path        the contact record's stored route.
  get_advert_path the route the advert itself travelled to reach us.

The fallback is send_path_discovery() one request/response per server

    python -m tools.path_survey <serial-port|ble-address>

Sends nothing. Reads the contact table the radio already built.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from meshcore import EventType
sys.path.insert(0, ".")
from vagrantnet.common import discovery  # noqa: E402
from vagrantnet.client.client import VagrantNetClient  # noqa: E402

logging.basicConfig(level=logging.WARNING)

async def survey(target: str) -> None:
    kind = "ble" if ":" in target else "serial"
    client = (
        await VagrantNetClient.connect_ble(target, quiet=True)
        if kind == "ble"
        else await VagrantNetClient.connect_serial(target, quiet=True)
    )
    try:
        contacts = await discovery.scan(client.mc)
        print(f"{len(contacts)} contacts in the radio's table\n")

        stored = advert = neither = 0
        vnet_reachable = []
        for c in contacts:
            key = c.get("public_key")
            name = (c.get("adv_name") or "?")[:24]
            out_len = int(c.get("out_path_len", -1))

            adv_len, adv_path = -1, ""
            try:
                ev = await client.mc.commands.get_advert_path(bytes.fromhex(key))
                if ev.type != EventType.ERROR:
                    adv_len = int(ev.payload.get("path_len", -1))
                    adv_path = ev.payload.get("path") or ""
            except Exception:
                pass

            if out_len >= 0:
                stored += 1
                how = f"out_path {out_len} hop"
            elif adv_len >= 0:
                advert += 1
                how = f"advert path {adv_len} hop ({adv_path[:12]})"
            else:
                neither += 1
                how = "no route"

            if discovery.is_marked(c.get("adv_name")):
                vnet_reachable.append((name, how))
            print(f"  {name:<26} {how}")

        total = len(contacts) or 1
        print(f"\n  stored out_path ..... {stored:4d}  ({stored/total*100:.0f}%)")
        print(f"  advert path only .... {advert:4d}  ({advert/total*100:.0f}%)")
        print(f"  no route at all ..... {neither:4d}  ({neither/total*100:.0f}%)")
        print(f"\n  routable today ...... {stored} of {len(contacts)}")
        print(f"  routable if we use advert paths .... {stored + advert}")
        print(f"  need path discovery ({neither} x one bounded exchange) ... {neither}")

        if vnet_reachable:
            print("\nvNet servers:")
            for name, how in vnet_reachable:
                print(f"  {name:<26} {how}")
    finally:
        await client.disconnect()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    asyncio.run(survey(sys.argv[1]))
