# vagrantNet

**Pages over LoRa MeshCore. Minimum bandwidth. Free as in Freedom.**

vagrantNet is a small web-like/BBS-like publishing system for
[MeshCore](https://meshcore.io/) LoRa networks. A daemon server hosts pages and
files on a radio. A terminal client fetches and renders them (shipped with vagrantNet). Future support for meshCore applications pending.

It is built for sub-kilobyte functionality. Pages are a deliberately tiny
markup, compressed with a dictionary trained on that markup, and sent in
147-byte chunks. On real hardware a page arrives in a second on Zero-Hop.

Example Render:
```
═══ WELCOME TO VAGRANTNET ═══

This is a test node running the vagrantNet reference daemon.

ABOUT

vagrantNet serves pages and files over MeshCore's raw-data transport,
point-to-point, without broadcasting to a channel.

  [1] Files  (files)
  [2] Help   (help.vn)
```

## Feature-set

- **Client Ships with a TUI:** Tabs, mouse, clickable links, a `ctrl-e` command
  prompt, and a start screen on every empty tab
  [micro](https://github.com/micro-editor/micro) command compatible.
- **Cheap discovery:** Servers mark themselves in the MeshCore
  advert they already send by default. 
  Clients find them by reading the contact table their radio has already built.
- **Built acknowledging packet loss:** Per-chunk retries with a hop-scaled timeout,
  automatic reconnection on both ends, and an active heartbeat that catches a
  radio that has gone silent without disconnecting.
- **Custom compression:** A zstd dictionary trained on .vn
  pages: 45% smaller than plain zstd and 34% fewer round trips, measured over
  500 randomized pages and tool utility included for future multi-language support.
- **Hidden nodes:** A server can serve normally while not advertising so only
  users with the pKey can connect.
- **Runs unattended:** Auto radio discovery (best-effor), survives the radio being
  unplugged, and stops cleanly on SIGTERM. A systemd unit is included as an example deployment on Linux.

## Requirements

### Client & Server
- One MeshCore radio running **stock companion firmware** (no custom
  firmware needed), connected by USB serial or BLE
- Python 3.10+

## Getting started

### Build from source

```sh
git clone https://github.com/aadiaz1804/vagrantNet
cd vagrantNet
python3 -m venv .venv && .venv/bin/pip install -e .
```
### Binaries & Docker
Docker and multi-OS support WIP

### Browsing (Client)

```sh
.venv/bin/python -m vagrantnet.client.tui
```

The shell autoconnects to the last radio you used, then scans for servers.
Press `ctrl-e` to type a command. Press `ctrl-g` for full help on available commands.

Example commands:

```
connect /dev/ttyUSB0           attach to a radio (or a BLE address)
discover                       list servers already heard
server add <name> <pubkey-hex> add custom server under a short name
open <name>                    fetch <name>'s index.vn
```
### Keybinds
| key | | key | |
|---|---|---|---|
| `ctrl-e` | command | `ctrl-r` | reload |
| `ctrl-g` | help | `ctrl-c` | copy page |
| `ctrl-t` | new tab | `alt-,` / `alt-.` | prev / next tab |
| `ctrl-w` | close tab | `ctrl-q` | quit |

Click links with the mouse, or press their number. There is also a one-shot
CLI, useful for scripting operations:

```sh
python -m vagrantnet.client.client <port|ble-addr> <server-pubkey> get-page index.vn
```

### Hosting (Server)

```sh
cp config.example.json config.json   # remember to update the config
python -m vagrantnet.server.server config.json
```

Drop `.vn` files in `pages_dir` and they are served. Drop anything else in
`downloads_dir` and it shows up in the `[Files|files]` listing every page can
link to. Keep separate as `.vn` files in `downloads_dir` gets
listed but a client will try to fetch it as a page and get NOT_FOUND, since
paths ending in `.vn` are resolved against `pages_dir`.

`"target": "auto"` makes the server find its own radio by asking each USB
serial port which one answers the companion protocol. It picks the first port that answers, you can also give an explicit path if you have two radios on one host and the path does not change.

### Running it as a daemon (Server headless continuous run)

The server retries a lost radio on its own, rebuilds a link that goes silent,
and answers SIGTERM so systemd only has to start it and restart it if crashes. A unit file example ships at
[`examples/server/vagrantnet.service`](examples/server/vagrantnet.service).

```sh
sudo useradd --system --home /opt/vagrantnet --groups dialout vagrantnet
sudo mkdir -p /opt/vagrantnet && sudo chown vagrantnet: /opt/vagrantnet

sudo -u vagrantnet git clone https://github.com/aadiaz1804/vagrantNet /opt/vagrantnet
sudo -u vagrantnet python3 -m venv /opt/vagrantnet/.venv
sudo -u vagrantnet /opt/vagrantnet/.venv/bin/pip install -e /opt/vagrantnet
sudo -u vagrantnet cp /opt/vagrantnet/config.example.json /opt/vagrantnet/config.json

sudo cp /opt/vagrantnet/vagrantnet/server/vagrantnet.service /etc/systemd/system/
sudo systemctl enable --now vagrantnet
journalctl -u vagrantnet -f
```

The `dialout` group is what grants access to `/dev/tty*`; without it the
server cannot open the radio. A healthy server logs a heartbeat every 60s and
rebuilds its routing table every 15 minutes.

## Writing pages

`.vn` is deliberately small

```
  # / ## / ###   headings
  [Label|path]   link
  > text         quote line
  ---            horizontal rule
  !c <colour>    colour every following line until the next !c
  !allow <key>   restrict this page to the listed pkeys (whitelisting)
  anything else  plain paragraph text

Malformed lines (including a typo'd `!directive`) render as plain text.

`!c` colours a *block*, not a span. Done for bandwidth
`!allow` is processed server-side. It's matched against
the 6-byte pubkey prefix a request carries on the wire.

A link whose path doesn't end in `.vn` is a file clients fetch it
with GET_FILE and save it instead of rendering it. `[Files|files]` is always
available and lists whatever is in `downloads_dir`.
```

Check what a page will actually cost before publishing it:

```sh
$ python -m vagrantnet.common.page pages/index.vn
pages/index.vn: 169 bytes on the wire (compressed), 2 chunk(s), ~1.2s to fetch, 2 link(s)
```

Every chunk is another round trip keep that in mind sometimes dividing pages into links is better than cramming everything on index.vn

## How discovery works

MeshCore already floods node adverts across the mesh, and every radio stores
what it hears. vagrantNet enabled servers append `[vNet]` to their advertised name, and clients filter their local contact table for it.

**Why?:** Node's *role* on MeshCore lives in `adv_type` (1 = companion, 2 = repeater, 3 = room server), which is set by firmware. A "vagrantNet server" type would need a firmware change and an allocation upstream. The name marker is the stand-in while in alpha.

Set `"advertise_as_server": false` for a hidden node.

## Status

**Alpha.** It works (tested with 2 MeshCore radios over 2 Linux computers), end to end, on real hardware over a live ~200-node community mesh ([GOME](https://ottawamesh.ca/)) but no other large scale tests or adoption has been reached yet.

**Verified on hardware:** page and file transfers including multi-chunk with a
byte-exact checksum, discovery, reconnection after a link drop, and clean
restarts. Measured on a healthy link: **0.58s median round trip, ~0.5% packet
loss** over 195 trials.

**Multi-hop replies are still pre-alpha**. The daemon builds a routing table and
will answer along a path, this has only ever been against synthetic data. A server
more than one hop away may well be discoverable but not yet fetchable.

**Roadmap:** This was built for internal adoption but if there is interest a Wiki will be in progress with future work like a vNet enabled phone/desktop app could be added in the future, also further testing on real mesh users on other hardware and submitting vNet as a non-dev protocol to MeshCore. 

*Note: There is no authentication above what MeshCore itself provides.
`!allow` on a page is a 6-byte pubkey-prefix match against the request and it keeps a page off `ls`*

## Contact
If you'd like to help or contribute, feel free to log a bug report, fork the project or push an MR on the protocol and I will try to address it. If you'd like to reach out to me directly for any inquiries (for now) the only contact is over aadiaz1804@proton.me

## License

GPL-2.0. See [LICENSE](LICENSE).
