"""vagrantNet interactive client shell """

from __future__ import annotations

import asyncio
import base64
import logging
import re
import shlex
import sys
from dataclasses import dataclass, field

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.completion import NestedCompleter
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea
from ..common import discovery
from ..common.envelope import Subcommand
from ..common.page import LINK_RE, Link, extract_links, render_ansi
from .client import VagrantNetClient, VagrantNetError
from .config import ClientConfig, DEFAULT_CONFIG_PATH

logger = logging.getLogger("vagrantnet.tui")

HISTORY_PATH = DEFAULT_CONFIG_PATH.parent / "shell_history"
_BLE_ADDRESS_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

HELP_TEXT = """\
connect <serial-port|ble-address> [pin]   connect to a radio
server add <name> <pubkey-hex>            save a server under a short name
server ls                                 list saved servers
server rm <name>                          forget a saved server
open <server> [path]                      fetch and render a page (default: index.vn)
go <n>                                    follow link <n> from the current page
back                                      return to the previous page
discover                                  list nearby vagrantNet servers (no LoRa)
ls [server]                               list a server's pages (default: current server)
get <path>                                download a file from the current server
fav add <label>                           save the current page as a favorite
fav ls                                    list favorites
fav rm <n>                                forget favorite <n>
fav <n>                                   open favorite <n>
help                                      show this text
quit / exit                               leave
"""

class Shell:
    # Session model: connection, navigation, favourites.
    def __init__(self, emit=None, show_page=None) -> None:
        self.emit = emit or print
        self.show_page = show_page or self._print_page
        self.config = ClientConfig.load()
        self.client: VagrantNetClient | None = None
        self.current_server: str | None = None  # name or pubkey, as typed
        self.current_path: str | None = None
        self.current_links: list[Link] = []
        self.nav_stack: list[tuple[str, str]] = []
        self.found: list[discovery.Found] = []
        self.census: dict[str, int] = {}

    # ---------------- connection -----------------------------------------
    async def connect(self, target: str, pin: str | None = None) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None

        kind = "ble" if _BLE_ADDRESS_RE.match(target) else "serial"
        self.emit(f"connecting ({kind}) to {target}...")
        try:
            self.client = (
                await VagrantNetClient.connect_ble(target, pin=pin, quiet=True)
                if kind == "ble"
                else await VagrantNetClient.connect_serial(target, quiet=True)
            )
        except VagrantNetError as e:
            self.emit(f"connect failed: {e}")
            return
        self.config.set_last_connection(kind, target, pin)
        self.emit("connected.")

    async def autoconnect_if_known(self) -> None:
        if self.config.last_connection_target:
            self.emit(f"reconnecting to last radio: {self.config.last_connection_target}")
            await self.connect(
                self.config.last_connection_target, self.config.last_connection_pin
            )

    def _require_client(self) -> VagrantNetClient | None:
        if self.client is None:
            self.emit("not connected -- try: connect <serial-port|ble-address>")
            return None
        return self.client

    def _resolve_server(self, name_or_pubkey: str) -> str:
        return self.config.resolve_server(name_or_pubkey)

    # ---------------- page navigation -----------------------------------------
    async def open_page(self, server: str, path: str, *, push_history: bool = True) -> None:
        client = self._require_client()
        if client is None:
            return
        pubkey = self._resolve_server(server)
        try:
            body = await client.fetch(pubkey, Subcommand.GET_PAGE, path)
        except VagrantNetError as e:
            self.emit(f"fetch failed: {e}")
            return

        if push_history and self.current_server is not None and self.current_path is not None:
            self.nav_stack.append((self.current_server, self.current_path))

        text = body.decode("utf-8", errors="replace")
        self.current_server = server
        self.current_path = path
        self.current_links = extract_links(text)
        self.show_page(server, path, text, self.current_links)

    async def discover(self, announce: bool = True) -> None:
        # List vagrantNet servers the radio has already heard.
        client = self._require_client()
        if client is None:
            return
        if announce:
            self.emit("scanning the radio's contact table (no traffic sent)...")
        # Refresh the list live when a server we had not heard of advertises.
        def _live(servers):
            self.found = servers
            self.emit(f"heard a new vagrantNet server ({len(servers)} known)")
        client.on_servers_changed = _live
        self.found, self.census = await client.discover()
        seen = ", ".join(f"{n} {k}s" for k, n in sorted(self.census.items()))
        self.emit(f"heard {sum(self.census.values())} nodes ({seen})")
        if not self.found:
            self.emit("no vagrantNet servers advertising yet -- a server has to "
                      "set advertise_as_server for it to be findable")
            return
        self.emit(f"{len(self.found)} vagrantNet server(s):")
        for i, f in enumerate(self.found, start=1):
            where = f"{f.hops} hop(s)" if f.reachable else "no path yet"
            self.emit(f"  [{i}] {f.name}  ({f.kind}, {where})  {f.pubkey[:12]}...")
        self.emit("use: server add <name> <pubkey>   then: open <name>")

    def _print_page(self, server, path, text, links) -> None:
        print(render_ansi(text))
        if links:
            print()
            for i, link in enumerate(links, start=1):
                print(f"  [{i}] {link.label}  ({link.path})")

    async def go(self, n: int) -> None:
        if not (1 <= n <= len(self.current_links)):
            self.emit(f"no link {n} on this page")
            return
        if self.current_server is None:
            self.emit("no current page")
            return
        link = self.current_links[n - 1]
        await self.open_page(self.current_server, link.path)

    async def back(self) -> None:
        if not self.nav_stack:
            self.emit("nothing to go back to")
            return
        server, path = self.nav_stack.pop()
        await self.open_page(server, path, push_history=False)

    async def list_pages(self, server: str | None) -> None:
        client = self._require_client()
        if client is None:
            return
        target = server or self.current_server
        if target is None:
            self.emit("no server given and no current server -- try: ls <server>")
            return
        pubkey = self._resolve_server(target)
        try:
            body = await client.fetch(pubkey, Subcommand.LIST_PAGES, "")
        except VagrantNetError as e:
            self.emit(f"list-pages failed: {e}")
            return
        self.emit(render_ansi(body.decode("utf-8", errors="replace")))

    async def get_file(self, path: str) -> None:
        client = self._require_client()
        if client is None:
            return
        if self.current_server is None:
            self.emit("no current server -- open a page first")
            return
        pubkey = self._resolve_server(self.current_server)
        try:
            body = await client.fetch(pubkey, Subcommand.GET_FILE, path)
        except VagrantNetError as e:
            self.emit(f"get-file failed: {e}")
            return
        out_name = path.split("/")[-1] or "download.bin"
        with open(out_name, "wb") as f:
            f.write(body)
        self.emit(f"saved {len(body)} bytes to {out_name}")

    # ---------------- favorites/servers -----------------------------------------
    def fav_add(self, label: str) -> None:
        if self.current_server is None or self.current_path is None:
            self.emit("no current page to favorite")
            return
        self.config.add_favorite(self.current_server, self.current_path, label)
        self.emit(f"favorited {label!r}")

    def fav_ls(self) -> None:
        if not self.config.favorites:
            self.emit("no favorites yet -- try: fav add <label>")
            return
        for i, fav in enumerate(self.config.favorites, start=1):
            self.emit(f"  [{i}] {fav.label}  ({fav.server}:{fav.path})")

    async def fav_open(self, n: int) -> None:
        if not (1 <= n <= len(self.config.favorites)):
            self.emit(f"no favorite {n}")
            return
        fav = self.config.favorites[n - 1]
        await self.open_page(fav.server, fav.path)

    def fav_rm(self, n: int) -> None:
        fav = self.config.remove_favorite(n - 1)
        if fav is None:
            self.emit(f"no favorite {n}")
            return
        self.emit(f"forgot favorite {fav.label!r} ({fav.server}:{fav.path})")

    def server_add(self, name: str, pubkey: str) -> None:
        self.config.add_server(name, pubkey)
        self.emit(f"saved server {name!r}")

    def server_rm(self, name: str) -> None:
        pubkey = self.config.remove_server(name)
        if pubkey is None:
            self.emit(f"no saved server named {name!r}")
            return
        self.emit(f"forgot server {name!r} ({pubkey[:12]}...)")
        stale = [f.label for f in self.config.favorites if f.server == name]
        if stale:
            self.emit(f"  note: {len(stale)} favorite(s) still refer to {name!r}: "
                  f"{', '.join(stale)}")

    def server_ls(self) -> None:
        if not self.config.servers:
            self.emit("no saved servers -- try: server add <name> <pubkey-hex>")
            return
        for name, pubkey in self.config.servers.items():
            self.emit(f"  {name}  {pubkey}")

    # ---------------- command dispatch -----------------------------------------
    async def run_command(self, line: str) -> bool:
        """Returns False when the shell should exit."""
        try:
            parts = shlex.split(line)
        except ValueError as e:
            self.emit(f"parse error: {e}")
            return True
        if not parts:
            return True

        cmd, args = parts[0], parts[1:]

        if cmd in ("quit", "exit"):
            return False
        elif cmd == "help":
            self.emit(HELP_TEXT)
        elif cmd == "connect":
            if not args:
                self.emit("usage: connect <serial-port|ble-address> [pin]")
            else:
                await self.connect(args[0], args[1] if len(args) > 1 else None)
        elif cmd == "server":
            if args and args[0] == "add" and len(args) == 3:
                self.server_add(args[1], args[2])
            elif args and args[0] == "ls":
                self.server_ls()
            elif args and args[0] == "rm" and len(args) == 2:
                self.server_rm(args[1])
            else:
                self.emit("usage: server add <name> <pubkey-hex> | server ls "
                      "| server rm <name>")
        elif cmd == "open":
            if not args:
                self.emit("usage: open <server> [path]")
            else:
                await self.open_page(args[0], args[1] if len(args) > 1 else "index.vn")
        elif cmd == "go":
            if not args or not args[0].isdigit():
                self.emit("usage: go <n>")
            else:
                await self.go(int(args[0]))
        elif cmd == "back":
            await self.back()
        elif cmd == "discover":
            await self.discover()
        elif cmd == "ls":
            await self.list_pages(args[0] if args else None)
        elif cmd == "get":
            if not args:
                self.emit("usage: get <path>")
            else:
                await self.get_file(args[0])
        elif cmd == "fav":
            if args and args[0] == "add" and len(args) >= 2:
                self.fav_add(" ".join(args[1:]))
            elif args and args[0] == "ls":
                self.fav_ls()
            elif args and args[0] == "rm" and len(args) == 2 and args[1].isdigit():
                self.fav_rm(int(args[1]))
            elif args and args[0].isdigit():
                await self.fav_open(int(args[0]))
            else:
                self.emit("usage: fav add <label> | fav ls | fav rm <n> | fav <n>")
        else:
            self.emit(f"unknown command: {line!r} (try: help)")
        return True

    async def shutdown(self) -> None:
        if self.client is not None:
            await self.client.disconnect()

def _completer() -> NestedCompleter:
    return NestedCompleter.from_nested_dict(
        {
            "connect": None,
            "server": {"add": None, "ls": None, "rm": None},
            "open": None,
            "go": None,
            "back": None,
            "discover": None,
            "ls": None,
            "get": None,
            "fav": {"add": None, "ls": None, "rm": None},
            "help": None,
            "quit": None,
            "exit": None,
        }
    )

async def repl_main() -> None:
    """The original line-oriented shell, kept as a fallback for dumb terminals
    and for scripted use."""
    # force=True to not get library's INFO logging
    logging.basicConfig(level=logging.WARNING, force=True)
    shell = Shell()
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    session: PromptSession = PromptSession(
        history=FileHistory(str(HISTORY_PATH)),
        completer=_completer(),
    )

    print("vagrantNet shell -- type 'help' for commands, 'quit' to leave.")
    await shell.autoconnect_if_known()

    try:
        with patch_stdout():
            while True:
                try:
                    line = await session.prompt_async("vn> ")
                except (EOFError, KeyboardInterrupt):
                    break
                try:
                    if not await shell.run_command(line):
                        break
                except Exception:
                    # Catch possible bugs without killing the shell
                    logger.exception("command failed: %r", line)
                    print("Unexpected error, see log")
    finally:
        await shell.shutdown()

# ============================ full-screen UI ============================
# REPL is still the engine. Tabs, a Ctrl-key command prompt instead of a modal language, 
# mouse support, and a start screen goes through Shell.run_command().
VN_STYLE = Style.from_dict({
    "tabbar":          "bg:#1c1c2b",
    "tabbar.tab":      "bg:#1c1c2b #8a8aa0",
    "tabbar.active":   "bg:#3b3b58 #ffffff bold",
    "status":          "bg:#3b3b58 #d0d0e0",
    "status.key":      "bg:#3b3b58 #ffcc66 bold",
    "status.off":      "bg:#3b3b58 #ff8080 bold",
    "status.on":       "bg:#3b3b58 #86e08a bold",
    "cmd":             "bg:#101018 #ffffff",
    "cmd.prefix":      "bg:#101018 #ffcc66 bold",
    "page.h1":         "#82aaff bold",
    "page.h2":         "#82aaff bold",
    "page.h3":         "#c3e88d bold",
    "page.quote":      "#8a8aa0 italic",
    "page.rule":       "#44445a",
    "page.link":       "#c792ea underline",
    "page.linknum":    "#ffcc66 bold",
    "splash.title":    "#82aaff bold",
    "splash.dim":      "#8a8aa0",
    "splash.key":      "#ffcc66",
    "splash.text":     "#d0d0e0",
    "msg":             "#c3e88d",
    "msg.err":         "#ff8080",
})

SPLASH_COMMANDS = [
    ("open <server>",          "fetch and render index.vn"),
    ("ls [server]",            "list public server files"),
    ("server add <name> <pk>", "save a server by name"),
    ("connect <port|addr>",    "attach to a radio"),
    ("discover",               "find servers already heard (free)"),
]

KEYS_HELP = [
    ("ctrl-e", "command"), ("ctrl-g", "help"), ("ctrl-t", "new tab"),
    ("ctrl-w", "close tab"), ("alt-,/.", "prev/next tab"),
    ("ctrl-r", "reload"), ("ctrl-c", "copy page"), ("ctrl-q", "quit"),
]

@dataclass
class Tab:
    # One open page. An empty one renders the start screen.
    server: str | None = None
    path: str | None = None
    text: str = ""
    links: list[Link] = field(default_factory=list)
    messages: list[tuple[str, str]] = field(default_factory=list)
    scroll: int = 0
    loading: bool = False
    nav: list[tuple[str, str]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.text and not self.messages and not self.loading

    @property
    def title(self) -> str:
        if self.loading:
            return "loading…"
        if not self.text:
            return "new tab"
        return f"{self.server}:{self.path}" if self.server else (self.path or "page")

class VagrantNetUI:
    def __init__(self) -> None:
        self.tabs: list[Tab] = [Tab()]
        self.active = 0
        self.shell = Shell(emit=self._emit, show_page=self._show_page)
        self.cmd = TextArea(height=1, multiline=False, style="class:cmd",
                            accept_handler=self._accept_command)
        self.cmd_open = False
        self.app: Application | None = None

    # ---------------- tab helpers -----------------------------------------
    @property
    def tab(self) -> Tab:
        return self.tabs[self.active]

    def _emit(self, text: str = "") -> None:
        style = "class:msg.err" if re.match(r"(?i)\s*(no |not |.*failed|unknown|usage)", str(text)) else "class:msg"
        for line in str(text).splitlines() or [""]:
            self.tab.messages.append((style, line))
        self._refresh()

    def _show_page(self, server, path, text, links) -> None:
        t = self.tab
        t.server, t.path, t.text, t.links = server, path, text, links
        t.messages.clear()
        t.scroll = 0
        self._refresh()

    def _refresh(self) -> None:
        if self.app is not None:
            self.app.invalidate()

    # ---------------- rendering -------------------------------------------
    def _content_height(self) -> int:
        rows = self.app.output.get_size().rows if self.app else 24
        return max(1, rows - 3 - (1 if self.cmd_open else 0))

    def _handler(self, link_idx: int | None = None):
        def handle(ev):
            if ev.event_type == MouseEventType.SCROLL_UP:
                self._scroll(-3)
            elif ev.event_type == MouseEventType.SCROLL_DOWN:
                self._scroll(3)
            elif ev.event_type == MouseEventType.MOUSE_UP and link_idx is not None:
                self._follow(link_idx)
            else:
                return NotImplemented
            return None
        return handle

    def _splash_lines(self) -> list[list]:
        h = self._handler()
        lines: list[list] = [[] for _ in range(2)]
        def row(frags): lines.append(frags)
        row([("class:splash.title", "        vagrantNet", h)])
        row([("class:splash.dim",   "        web over LoRa, batteries included", h)])
        row([])
        if not self.shell.client:
            row([("class:splash.dim", "       - not connected - ", h),
                 ("class:splash.key", "ctrl-e", h),
                 ("class:splash.dim", " then ", h),
                 ("class:splash.text", "connect <port|ble-addr>", h)])
            row([])
        for cmd, what in SPLASH_COMMANDS:
            row([("class:splash.dim", "        type  ", h),
                 ("class:splash.key", f"{cmd:<28}", h),
                 ("class:splash.text", what, h)])
        row([])
        if self.shell.found:
            row([("class:splash.dim", "        nearby servers", h)])
            for i, f in enumerate(self.shell.found[:6], start=1):
                where = f"{f.hops} hop" if f.reachable else "no path"
                row([("class:splash.dim", "          ", h),
                     ("class:page.linknum", f"{i}. ", h),
                     ("class:splash.text", f"{f.name:<22}", h),
                     ("class:splash.dim", f"{f.kind}, {where}", h)])
            row([])
        servers = list(self.shell.config.servers)
        favs = self.shell.config.favorites
        if servers:
            row([("class:splash.dim", "        saved servers  ", h),
                 ("class:splash.text", ", ".join(servers), h)])
        if favs:
            row([("class:splash.dim", "        favourites     ", h),
                 ("class:splash.text",
                  ", ".join(f"{i}:{f.label}" for i, f in enumerate(favs, 1)), h)])
        if not servers and not favs:
            row([("class:splash.dim", "        no servers saved", h)])
        row([])
        row([("class:splash.dim", "        ", h)] +
            [frag for k, w in KEYS_HELP for frag in
             (("class:splash.key", k, h), ("class:splash.dim", f" {w}   ", h))])
        return lines

    def _page_lines(self) -> list[list]:
        t = self.tab
        h = self._handler()
        by_path = {}
        for i, link in enumerate(t.links):
            by_path.setdefault(link.path, i)
        lines: list[list] = []
        for raw in t.text.splitlines():
            stripped = raw.strip()
            m = LINK_RE.match(stripped)
            if m:
                idx = by_path.get(m.group("path"), None)
                lh = self._handler(idx)
                lines.append([
                    ("class:page.linknum", f"  [{idx + 1 if idx is not None else '?'}] ", lh),
                    ("class:page.link", m.group("label"), lh),
                    ("class:splash.dim", f"  ({m.group('path')})", lh),
                ])
            elif stripped.startswith("### "):
                lines.append([("class:page.h3", stripped[4:], h)])
            elif stripped.startswith("## "):
                lines.append([("class:page.h2", stripped[3:].upper(), h)])
            elif stripped.startswith("# "):
                lines.append([("class:page.h1", f"═══ {stripped[2:].upper()} ═══", h)])
            elif stripped.startswith("> "):
                lines.append([("class:page.quote", f"  │ {stripped[2:]}", h)])
            elif stripped == "---":
                lines.append([("class:page.rule", "-" * 50, h)])
            else:
                lines.append([("", raw, h)])
        return lines

    def _lines(self) -> list[list]:
        t = self.tab
        if t.loading:
            return [[], [("class:splash.dim", f"  fetching {t.path or ''} …", self._handler())]]
        if t.empty:
            return self._splash_lines()
        lines = self._page_lines() if t.text else []
        if t.messages:
            if lines:
                lines.append([])
            lines.extend([[(style, "  " + text, self._handler())] for style, text in t.messages])
        return lines

    def _content(self):
        lines = self._lines()
        height = self._content_height()
        max_scroll = max(0, len(lines) - height)
        self.tab.scroll = max(0, min(self.tab.scroll, max_scroll))
        out = []
        for line in lines[self.tab.scroll:self.tab.scroll + height]:
            out.extend(line)
            out.append(("", "\n"))
        return out

    def _tabbar(self):
        frags = []
        for i, t in enumerate(self.tabs):
            def sel(ev, i=i):
                if ev.event_type == MouseEventType.MOUSE_UP:
                    self.active = i
                    self._refresh()
                else:
                    return NotImplemented
            style = "class:tabbar.active" if i == self.active else "class:tabbar.tab"
            frags.append((style, f" {i + 1} {t.title} ", sel))
            frags.append(("class:tabbar", " "))
        frags.append(("class:tabbar", ""))
        return frags

    def _status(self):
        t = self.tab
        conn = ("class:status.on", " connected ") if self.shell.client else \
               ("class:status.off", " offline ")
        where = f"{t.server}:{t.path}" if t.text and t.server else "no page"
        lines = len(self._lines())
        pct = 100 if lines <= self._content_height() else \
            min(100, int(100 * (t.scroll + self._content_height()) / max(1, lines)))
        return [
            conn,
            ("class:status", f" {where} "),
            ("class:status", f" {len(t.links)} links " if t.links else " "),
            ("class:status", " " * 2),
            ("class:status.key", "ctrl-e"), ("class:status", " command  "),
            ("class:status.key", "ctrl-g"), ("class:status", " help  "),
            ("class:status.key", "ctrl-q"), ("class:status", " quit  "),
            ("class:status", f" {pct}% "),
        ]

    # ---------------- actions ---------------------------------------------
    def _scroll(self, delta: int) -> None:
        self.tab.scroll = max(0, self.tab.scroll + delta)
        self._refresh()

    def _follow(self, idx: int | None) -> None:
        if idx is None or not (0 <= idx < len(self.tab.links)):
            return
        self._spawn(self.shell.go(idx + 1))

    def _spawn(self, coro) -> None:
        # Async updating tab without locking UI
        tab = self.tab
        tab.loading = True
        self._refresh()

        async def run():
            try:
                await coro
            except Exception as exc:
                logger.exception("command failed")
                if tab in self.tabs:
                    tab.messages.append(("class:msg.err", f"{type(exc).__name__}: {exc}"))
            finally:
                tab.loading = False
                self._refresh()

        asyncio.ensure_future(run())

    def _accept_command(self, buf) -> bool:
        line = buf.text.strip()
        self.cmd_open = False
        self._focus_content()
        if not line:
            return False
        if line in ("quit", "exit", "q"):
            self.app.exit()
            return False
        self._spawn(self.shell.run_command(line))
        return False

    def _focus_content(self) -> None:
        if self.app:
            self.app.layout.focus(self.content_window)

    def _new_tab(self) -> None:
        self.tabs.append(Tab())
        self.active = len(self.tabs) - 1
        self._refresh()

    def _close_tab(self) -> None:
        if len(self.tabs) == 1:
            # Last tab: empty it on the UI
            self.tabs[0] = Tab()
            self.active = 0
        else:
            self.tabs.pop(self.active)
            self.active = min(self.active, len(self.tabs) - 1)
        self._refresh()

    def _copy_page(self) -> None:
        # Ask the terminal to set the system clipboard
        t = self.tab
        payload = t.text or "\n".join(text for _, text in t.messages)
        if not payload:
            self._emit("nothing to copy")
            return
        b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        self.app.output.write_raw(f"\x1b]52;c;{b64}\x07")
        self.app.output.flush()
        self._emit(f"copied {len(payload)} bytes to the clipboard")

    def _help_tab(self) -> None:
        rows = ["# vagrantNet", "", "## Keys", ""]
        rows += [f"  {k:<10} {w}" for k, w in KEYS_HELP]
        rows += ["", "## Commands", ""]
        rows += ["  " + l for l in HELP_TEXT.splitlines()]
        tab = Tab(server=None, path="help", text="\n".join(rows))
        self.tabs.append(tab)
        self.active = len(self.tabs) - 1
        self._refresh()

    # ---------------- wiring ----------------------------------------------
    def _bindings(self) -> KeyBindings:
        kb = KeyBindings()
        insert = Condition(lambda: self.cmd_open)

        @kb.add("c-q")
        def _(event): event.app.exit()

        @kb.add("c-e", filter=~insert)
        def _(event):
            self.cmd_open = True
            self.cmd.text = ""
            event.app.layout.focus(self.cmd)
            self._refresh()

        @kb.add("escape", filter=insert, eager=True)
        def _(event):
            self.cmd_open = False
            self._focus_content()
            self._refresh()

        @kb.add("c-g", filter=~insert)
        def _(event): self._help_tab()

        @kb.add("c-t", filter=~insert)
        def _(event): self._new_tab()

        @kb.add("c-w", filter=~insert)
        def _(event): self._close_tab()

        @kb.add("c-r", filter=~insert)
        def _(event):
            t = self.tab
            if t.server and t.path:
                self._spawn(self.shell.open_page(t.server, t.path, push_history=False))

        @kb.add("c-c", filter=~insert)
        def _(event): self._copy_page()

        @kb.add("escape", ",", filter=~insert)
        def _(event):
            self.active = (self.active - 1) % len(self.tabs)
            self._refresh()

        @kb.add("escape", ".", filter=~insert)
        def _(event):
            self.active = (self.active + 1) % len(self.tabs)
            self._refresh()

        @kb.add("up", filter=~insert)
        def _(event): self._scroll(-1)

        @kb.add("down", filter=~insert)
        def _(event): self._scroll(1)

        @kb.add("pageup", filter=~insert)
        def _(event): self._scroll(-self._content_height() + 1)

        @kb.add("pagedown", filter=~insert)
        def _(event): self._scroll(self._content_height() - 1)

        @kb.add("home", filter=~insert)
        def _(event):
            self.tab.scroll = 0
            self._refresh()

        @kb.add("backspace", filter=~insert)
        def _(event): self._spawn(self.shell.back())

        for n in range(1, 10):
            @kb.add(str(n), filter=~insert)
            def _(event, n=n): self._follow(n - 1)

        return kb

    def build(self) -> Application:
        self.content_control = FormattedTextControl(
            self._content, focusable=True, show_cursor=False)
        self.content_window = Window(self.content_control, wrap_lines=False)
        layout = Layout(HSplit([
            Window(FormattedTextControl(self._tabbar), height=1, style="class:tabbar"),
            self.content_window,
            Window(FormattedTextControl(self._status), height=1, style="class:status"),
            ConditionalContainer(
                VSplit([
                    Window(FormattedTextControl([("class:cmd.prefix", " > ")]),
                           height=1, width=3, style="class:cmd"),
                    self.cmd,
                ]),
                filter=Condition(lambda: self.cmd_open),
            ),
        ]), focused_element=self.content_window)

        self.app = Application(
            layout=layout, key_bindings=self._bindings(), style=VN_STYLE,
            full_screen=True, mouse_support=True, refresh_interval=0.5,
        )
        return self.app

class _UILogHandler(logging.Handler):
    # Move log messages over UI when relevant
    def __init__(self, ui: VagrantNetUI):
        super().__init__(level=logging.WARNING)
        self.ui = ui

    def emit(self, record):
        try:
            self.ui.tab.messages.append(("class:msg.err", self.format(record)))
            self.ui._refresh()
        except Exception:
            pass

async def main() -> None:
    logging.basicConfig(level=logging.WARNING, force=True, handlers=[])
    ui = VagrantNetUI()
    handler = _UILogHandler(ui)
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    logging.getLogger().handlers = [handler]

    app = ui.build()
    if ui.shell.config.last_connection_target:
        async def boot():
            await ui.shell.autoconnect_if_known()
            if ui.shell.client:
                # Free, so run it unprompted part of TUI build
                await ui.shell.discover(announce=False)
        ui._spawn(boot())
    try:
        await app.run_async()
    finally:
        await ui.shell.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
