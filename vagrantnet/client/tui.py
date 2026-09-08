"""vagrantNet interactive client shell """

from __future__ import annotations

import asyncio
import base64
import logging
import re
import shlex
import sys
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

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
from ..common import discovery, page
from ..common.envelope import Subcommand
from ..common.page import Link, extract_links, is_page_path, parse as parse_page, render_ansi
from .client import VagrantNetClient, VagrantNetError
from .config import ClientConfig, DEFAULT_CONFIG_PATH

logger = logging.getLogger("vagrantnet.tui")

HISTORY_PATH = DEFAULT_CONFIG_PATH.parent / "shell_history"
_BLE_ADDRESS_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

DOWNLOAD_DIR = Path("downloads")  # relative
# Pseudo-path for the page listing, so it can be reloaded and gone back to.
LISTING_PATH = ":pages"

def _safe_download_path(server_path: str) -> Path | None:
    # Link is server-supplied confined to DOWNLOAD_DIR and refuse escapes
    parts = [p for p in PurePosixPath(server_path.replace("\\", "/")).parts
             if p not in ("", ".")]
    if not parts or any(p == ".." or p.startswith(".") for p in parts):
        return None
    target = DOWNLOAD_DIR.joinpath(*parts).resolve()
    try:
        target.relative_to(DOWNLOAD_DIR.resolve())
    except ValueError:
        return None
    return target

HELP_TEXT = """\
connect <serial-port|ble-address> [pin]   connect to a radio
server add <name> <pubkey-hex>            save a server under a short name
server ls                                 list saved servers
server rm <name>                          forget a saved server
open <server> [path]                      fetch and render a page (default: index.vn)
boards [server]                           open the board list
b <name>                                  enter a board (or click it in the sidebar)
home                                      leave the boards for the server's index
post <text>                               reply in the open thread
post <subject> | <text>                   start a thread in the open board
nick <name>                               override the name posts are signed with
                                          (defaults to your radio's own name)
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

# Only meaningful with a board open, so it is appended to the help then.
BOARD_HELP = """\
post <text>                               reply in the open thread
post <subject> | <text>                   start a thread in this board
b <name>                                  switch to another board
back                                      leave a thread / the board
home                                      leave the boards altogether
b/<board>:<seq>                           only what arrived after <seq>
b/<board>/<thread>                        one thread, :<seq> pages forward
b/<board>/archive                         browse the board by month
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
        # board name -> (title, last seq on the server), from the index page
        self.boards: dict[str, tuple[str, int]] = {}
        self.board_order: list[str] = []

    # ---------------- connection -----------------------------------------
    async def connect(self, target: str, pin: str | None = None) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None

        kind = "ble" if _BLE_ADDRESS_RE.match(target) else "serial"
        self.emit(f"connecting ({kind}) to {target}...")
        try:
            self.client = (
                await VagrantNetClient.connect_ble(
                    target, pin=pin, quiet=True,
                    flood_advert=self.config.flood_advert)
                if kind == "ble"
                else await VagrantNetClient.connect_serial(
                    target, quiet=True, flood_advert=self.config.flood_advert)
            )
        except VagrantNetError as e:
            self.emit(f"connect failed: {e}")
            return
        self.config.set_last_connection(kind, target, pin)
        self._adopt_radio_name()
        self.emit("connected.")

    def _adopt_radio_name(self) -> None:
        # Post under the name the radio already advertises.
        if self.config.nick or self.client is None:
            return
        name = discovery.unmark((self.client.mc.self_info or {}).get("name"))
        if name:
            self.config.set_nick(name[:24])
            self.emit(f"posting as {self.config.nick!r} (your radio's name)")

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
        if path == LISTING_PATH:
            await self.list_pages(server, push_history=push_history)
            return
        pubkey = self._resolve_server(server)

        if not is_page_path(path):
            # not a .vn page, possible file
            out_path = _safe_download_path(path)
            if out_path is None:
                self.emit(f"refusing to save unsafe path: {path!r}")
                return
            try:
                body = await client.fetch(pubkey, Subcommand.GET_FILE, path)
            except VagrantNetError as e:
                self.emit(f"download failed: {e}")
                return
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "wb") as f:
                f.write(body)
            self.emit(f"downloaded {len(body)} bytes to {out_path}")
            return

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
        self._remember_boards(text)
        board = self._board_of(path)
        if board is not None:
            high = page.seq(text)
            if high is not None:
                self.config.mark_seen(server, board, high)
                if board in self.boards:
                    self.boards[board] = (self.boards[board][0], high)
        self.show_page(server, path, text, self.current_links)

    # ---------------- boards ----------------------------------------------
    def _remember_boards(self, text: str) -> None:
        seqs = page.board_seqs(text)
        if not seqs:
            return
        titles = {l.path.split("/", 1)[1]: l.label
                  for l in extract_links(text) if l.path.startswith("b/")}
        self.boards = {n: (titles.get(n, n), s) for n, s in seqs.items()}
        self.board_order = [l.path.split("/", 1)[1] for l in extract_links(text)
                            if l.path.startswith("b/") and l.path.split("/", 1)[1] in seqs]

    def unread(self, board: str) -> int:
        if self.current_server is None or board not in self.boards:
            return 0
        return max(0, self.boards[board][1]
                   - self.config.last_seen(self.current_server, board))

    async def open_boards(self, server: str | None = None) -> None:
        target = server or self.current_server
        if target is None:
            self.emit("no server, try: open <server>")
            return
        await self.open_page(target, page.BOARD_PREFIX)

    async def enter_board(self, board: str, *, unread_only: bool = False) -> None:
        if self.current_server is None:
            self.emit("not connected to a server")
            return
        path = f"{page.BOARD_PREFIX}/{board}"
        if unread_only:
            path += f":{self.config.last_seen(self.current_server, board)}"
        await self.open_page(self.current_server, path)

    def _board_of(self, path: str | None) -> str | None:
        p = (path or "").strip("/")
        if not p.startswith(page.BOARD_PREFIX + "/"):
            return None
        head = p[len(page.BOARD_PREFIX) + 1:].partition("/")[0]
        return head.partition(":")[0].partition("@")[0] or None

    def in_boards(self, path: str | None = None) -> bool:
        # True on the board list and anywhere inside a board.
        p = ((self.current_path if path is None else path) or "").strip("/")
        return p == page.BOARD_PREFIX or p.startswith(page.BOARD_PREFIX + "/")

    async def home(self) -> None:
        # Leave the boards behind for the server's front page.
        if self.current_server is None:
            self.emit("no server, try: open <server>")
            return
        await self.open_page(self.current_server, "index.vn")

    def help_text(self) -> str:
        if not self.in_boards():
            return HELP_TEXT
        return f"{HELP_TEXT}\nin a board:\n\n{BOARD_HELP}"

    async def post(self, text: str, subject: str = "") -> None:
        """Post to the open board. Replies if a thread is open, else new thread."""
        client = self._require_client()
        if client is None:
            return
        if self.current_server is None:
            self.emit("no server")
            return
        board = self._board_of(self.current_path)
        if board is None:
            self.emit("not in a board")
            return
        if not self.config.nick:
            self.emit("set a name first:  nick <name>")
            return

        rest = (self.current_path or "").strip("/")[len(page.BOARD_PREFIX) + 1:]
        _, _, thread_part = rest.partition("/")
        thread = thread_part.partition(":")[0]
        dest = (f"{page.BOARD_PREFIX}/{board}/{thread}" if thread
                else f"{page.BOARD_PREFIX}/{board}")
        if not thread and not subject.strip():
            self.emit("a new thread needs a subject")
            return

        pubkey = self._resolve_server(self.current_server)
        try:
            await client.post(pubkey, dest, self.config.nick, text, subject)
        except VagrantNetError as e:
            self.emit(f"post failed: {e}")
            return
        self.emit("posted.")
        await self.open_page(self.current_server, self.current_path, push_history=False)

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
        unsaved = 0
        for i, f in enumerate(self.found, start=1):
            where = f"{f.hops} hop(s)" if f.reachable else "no path yet"
            saved = self.config.name_for(f.pubkey)
            if saved:
                # Show the name `open` takes, not just the advertised one.
                self.emit(f"  [{i}] {saved}  (as {f.name}, {f.kind}, {where})"
                          f"   open {saved}")
            else:
                unsaved += 1
                self.emit(f"  [{i}] {f.name}  ({f.kind}, {where})")
                self.emit(f"      server add <name> {f.pubkey}")
        if unsaved:
            self.emit("copy a line above, pick a short name, then: open <name>")

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

    async def list_pages(self, server: str | None, *, push_history: bool = True) -> None:
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
        # A listing is a .vn page, so show it as one: its links are clickable.
        text = body.decode("utf-8", errors="replace")
        if push_history and self.current_server is not None and self.current_path is not None:
            self.nav_stack.append((self.current_server, self.current_path))
        self.current_server = target
        self.current_path = LISTING_PATH
        self.current_links = extract_links(text)
        self.show_page(target, LISTING_PATH, text, self.current_links)

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
            self.emit(self.help_text())
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
        elif cmd == "boards":
            await self.open_boards(args[0] if args else None)
        elif cmd == "b":
            if not args:
                self.emit("usage: b <board>")
            else:
                await self.enter_board(args[0])
        elif cmd == "home":
            await self.home()
        elif cmd == "nick":
            if not args:
                self.emit(f"nick is {self.config.nick!r}" if self.config.nick
                          else "no nick, and the radio has no name, use: nick <name>")
            else:
                self.config.set_nick(" ".join(args)[:24])
                self.emit(f"posting as {self.config.nick!r}")
        elif cmd == "post":
            body = " ".join(args)
            subject, sep, text = body.partition("|")
            if sep:
                await self.post(text.strip(), subject.strip())
            else:
                await self.post(body.strip())
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
            "boards": None,
            "b": None,
            "home": None,
            "nick": None,
            "post": None,
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
# The toolbar's fixed cells, kept here so its width can be measured.
TOOLBAR_LABELS = " \u2190   \u21bb   \u2302   B   "

VN_STYLE = Style.from_dict({
    "tabbar":          "bg:#1c1c2b",
    "tabbar.tab":      "bg:#1c1c2b #8a8aa0",
    "tabbar.active":   "bg:#3b3b58 #ffffff bold",
    "tabbar.btn":      "bg:#2a2a3f #d0d0e0",
    "tabbar.btnoff":   "bg:#1c1c2b #45455a",
    "tabbar.close":    "bg:#3b3b58 #ff8080 bold",
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
    "page.linkfile":   "#86e08a underline",
    "page.linknum":    "#ffcc66 bold",
    "side":            "bg:#16161f",
    "side.title":      "bg:#16161f #8a8aa0 bold",
    "side.board":      "bg:#16161f #d0d0e0",
    "side.active":     "bg:#3b3b58 #ffffff bold",
    "side.unread":     "bg:#16161f #ffcc66 bold",
    "side.dim":        "bg:#16161f #55556a",
    "compose.head":    "bg:#101018 #ffcc66 bold",
    "splash.title":    "#82aaff bold",
    "splash.dim":      "#8a8aa0",
    "splash.key":      "#ffcc66",
    "splash.text":     "#d0d0e0",
    "msg":             "#c3e88d",
    "msg.err":         "#ff8080",
})

# !c <colour> names -> fg override, appended to a line's class so it composes
# with whatever heading/quote/link style that line already has.
PAGE_COLOUR_FG = {
    "dim": "#8a8aa0", "red": "#ff5f5f", "green": "#86e08a", "yellow": "#ffcc66",
    "blue": "#82aaff", "magenta": "#c792ea", "cyan": "#66d9ef", "white": "#d0d0e0",
}

SPLASH_COMMANDS = [
    ("boards <server>",        "open the message boards"),
    ("nick <name>",            "name your posts are signed with"),
    ("open <server>",          "fetch and render index.vn"),
    ("ls [server]",            "list public server files"),
    ("server add <name> <pk>", "save a server by name"),
    ("connect <port|addr>",    "attach to a radio"),
    ("discover",               "find servers already heard (free)"),
]

KEYS_HELP = [
    ("ctrl-e", "command"), ("ctrl-g", "help"), ("ctrl-t", "new tab"),
    ("ctrl-w", "close tab"), ("alt-,/.", "prev/next tab"),
    ("ctrl-b", "boards"), ("ctrl-p", "post"),
    ("ctrl-r", "reload"), ("ctrl-c", "copy page"), ("ctrl-q", "quit"),
]

# The rest of the bindings
KEYS_MORE = [
    ("1..9", "follow link 1-9 on the page"),
    ("backspace", "back to the previous page"),
    ("up/down", "scroll a line"),
    ("pgup/pgdn", "scroll a screen"),
    ("home", "back to the top"),
    ("esc", "close the command line, or cancel a post"),
]

KEYS_BOARD = [
    ("alt-1..9", "jump to board 1-9 in the sidebar"),
    ("ctrl-p", "write a post: ctrl-s sends, esc cancels"),
    ("ctrl-r", "reload the board for new posts"),
    ("ctrl-b", "out of the boards, back to the index"),
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
    loading_path: str | None = None  # what is being fetched, not what is shown
    nav: list[tuple[str, str]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.text and not self.messages and not self.loading

    @property
    def where(self) -> str:
        if not self.text:
            return "no page"
        return f"{self.server}:{self.path}" if self.server else (self.path or "page")

    @property
    def title(self) -> str:
        if self.loading:
            return f"→ {self.loading_path}" if self.loading_path else "loading…"
        if not self.text:
            return "new tab"
        return self.where

# The tab a spawned command belongs to.
_op_tab: ContextVar["Tab | None"] = ContextVar("vn_op_tab", default=None)

class VagrantNetUI:
    def __init__(self) -> None:
        self.tabs: list[Tab] = [Tab()]
        self.active = 0
        self.shell = Shell(emit=self._emit, show_page=self._show_page)
        self.cmd = TextArea(height=1, multiline=False, style="class:cmd",
                            accept_handler=self._accept_command)
        self.cmd_open = False
        self.compose = TextArea(height=6, multiline=True, style="class:cmd",
                                wrap_lines=True)
        self.compose_open = False
        self.app: Application | None = None

    # ---------------- tab helpers -----------------------------------------
    @property
    def tab(self) -> Tab:
        return self.tabs[self.active]

    def _here(self) -> str:
        # The path the active tab is showing, or heading for while it loads.
        # Tabs hold their own page; the Shell only tracks the last one fetched.
        t = self.tab
        return (t.loading_path if t.loading and t.loading_path else t.path) or ""

    def _target_tab(self) -> Tab | None:
        tab = _op_tab.get() or self.tab
        return tab if tab in self.tabs else None

    def _emit(self, text: str = "") -> None:
        tab = self._target_tab()
        if tab is None:
            return
        style = "class:msg.err" if re.match(r"(?i)\s*(no |not |.*failed|unknown|usage)", str(text)) else "class:msg"
        for line in str(text).splitlines() or [""]:
            tab.messages.append((style, line))
        self._refresh()

    def _show_page(self, server, path, text, links) -> None:
        t = self._target_tab()
        if t is None:
            return
        t.server, t.path, t.text, t.links = server, path, text, links
        t.messages.clear()
        t.scroll = 0
        self._refresh()

    def _refresh(self) -> None:
        if self.app is not None:
            self.app.invalidate()

    # ---------------- rendering -------------------------------------------
    def _tab_label(self, t: "Tab") -> str:
        # Tab labeling logic
        cols = self.app.output.get_size().columns if self.app else 80
        # Per tab, besides the title: " n " + "x " + a trailing space.
        room = (cols - len(TOOLBAR_LABELS) - 6) // max(1, len(self.tabs)) - 6
        if room < 6:
            # Too many tabs to name. The numbers still click, and the status
            # bar underneath says where the active one is.
            return ""
        room = min(28, room)
        title = t.title
        return title if len(title) <= room else title[:room - 1] + "\u2026"

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

    # ---------------- board sidebar ---------------------------------------
    def _board_handler(self, board: str):
        def handle(ev):
            if ev.event_type == MouseEventType.MOUSE_UP:
                self._spawn(self.shell.enter_board(board),
                            f"{page.BOARD_PREFIX}/{board}")
                return None
            return NotImplemented
        return handle

    def _sidebar(self) -> list:
        sh = self.shell
        out: list = [("class:side.title", " BOARDS\n")]
        if not sh.board_order:
            # Only reachable on the board list of a server that has none.
            out.append(("class:side.dim", " (none here)\n"))
            out.append(("class:side.dim", "\n ctrl-b back\n to the index\n"))
            return out

        here = sh._board_of(self._here())
        for i, name in enumerate(sh.board_order, start=1):
            title = sh.boards.get(name, (name, 0))[0]
            unread = sh.unread(name)
            style = "class:side.active" if name == here else "class:side.board"
            h = self._board_handler(name)
            out.append((style, f" {i}. {title[:13]:<13}", h))
            if unread:
                out.append(("class:side.unread", f"{unread:>3}\n", h))
            else:
                out.append((style, "   \n", h))
        out.append(("class:side.dim",
                    "\n alt-<n> jump\n ctrl-p post\n ctrl-b out\n"))
        return out

    def _compose_header(self) -> list:
        sh = self.shell
        board = sh._board_of(sh.current_path)
        rest = (sh.current_path or "").strip("/")
        in_thread = "/" in rest[len(page.BOARD_PREFIX) + 1:] if board else False
        what = "reply" if in_thread else "new thread (first line is the subject)"
        return [("class:compose.head",
                 f" {what} in {board or '?'} as {sh.config.nick or '(set a nick)'}"
                 "   ctrl-s send   esc cancel ")]

    def _open_compose(self) -> None:
        sh = self.shell
        if sh._board_of(sh.current_path) is None:
            self._emit("not in a board -- open one first")
            return
        if not sh.config.nick:
            self._emit("set a name first:  nick <name>")
            return
        self.compose.text = ""
        self.compose_open = True
        self.app.layout.focus(self.compose)
        self._refresh()

    def _send_compose(self) -> None:
        body = self.compose.text.strip()
        self.compose_open = False
        self._focus_content()
        if not body:
            return
        sh = self.shell
        rest = (sh.current_path or "").strip("/")[len(page.BOARD_PREFIX) + 1:]
        in_thread = "/" in rest
        where = sh.current_path
        if in_thread:
            self._spawn(sh.post(body), where)
        else:
            subject, _, text = body.partition("\n")
            self._spawn(sh.post(text.strip() or subject.strip(), subject.strip()), where)

    def _open_server_handler(self, name: str):
        def handle(ev):
            if ev.event_type == MouseEventType.MOUSE_UP:
                self._spawn(self.shell.open_page(name, "index.vn"), "index.vn")
                return None
            return NotImplemented
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
                saved = self.shell.config.name_for(f.pubkey)
                # Lead with the name `open` takes. Unsaved servers have none
                label = saved or f.name
                note = (f"{f.kind}, {where}" if saved
                        else f"{f.kind}, {where} -- not saved")
                click = self._open_server_handler(saved) if saved else h
                style = "class:page.link" if saved else "class:splash.text"
                row([("class:splash.dim", "          ", click),
                     ("class:page.linknum", f"{i}. ", click),
                     (style, f"{label:<22}", click),
                     ("class:splash.dim", note, click)])
                if not saved:
                    row([("class:splash.dim",
                          "             type  discover  for its key", h)])
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

        def styled(base: str, colour: str) -> str:
            fg = PAGE_COLOUR_FG.get(colour, "")
            return f"{base} fg:{fg}" if fg else base

        lines: list[list] = []
        for pline in parse_page(t.text):
            if pline.kind == "link":
                path = pline.link.path
                idx = by_path.get(path, None)
                lh = self._handler(idx)
                is_download = not is_page_path(path)
                link_class = "page.linkfile" if is_download else "page.link"
                marker = "↓ " if is_download else ""
                lines.append([
                    ("class:page.linknum", f"  [{idx + 1 if idx is not None else '?'}] ", lh),
                    (f"class:{styled(link_class, pline.colour)}", f"{marker}{pline.link.label}", lh),
                    ("class:splash.dim", f"  ({path})", lh),
                ])
            elif pline.kind == "h3":
                lines.append([(f"class:{styled('page.h3', pline.colour)}", pline.text, h)])
            elif pline.kind == "h2":
                lines.append([(f"class:{styled('page.h2', pline.colour)}", pline.text.upper(), h)])
            elif pline.kind == "h1":
                lines.append([(f"class:{styled('page.h1', pline.colour)}", f"═══ {pline.text.upper()} ═══", h)])
            elif pline.kind == "quote":
                lines.append([(f"class:{styled('page.quote', pline.colour)}", f"  │ {pline.text}", h)])
            elif pline.kind == "rule":
                lines.append([(f"class:{styled('page.rule', pline.colour)}", "-" * 50, h)])
            else:
                fg = PAGE_COLOUR_FG.get(pline.colour, "")
                lines.append([(f"fg:{fg}" if fg else "", pline.text, h)])
        return lines

    def _lines(self) -> list[list]:
        t = self.tab
        if t.loading:
            what = t.loading_path or t.path or ""
            return [[], [("class:splash.dim", f"  fetching {what} …", self._handler())]]
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

    def _button(self, label: str, action, enabled: bool = True,
                style: str = "class:tabbar.btn") -> tuple:
        if not enabled:
            return ("class:tabbar.btnoff", label)

        def click(ev):
            if ev.event_type != MouseEventType.MOUSE_UP:
                return NotImplemented
            action()
            return None
        return (style, label, click)

    def _toolbar(self) -> list:
        sh, t = self.shell, self.tab
        here = self._here()
        return [
            self._button(" \u2190 ", self._click_back, bool(sh.nav_stack)),
            ("class:tabbar", " "),
            self._button(" \u21bb ", self._click_reload, bool(t.server and t.path)),
            ("class:tabbar", " "),
            self._button(" \u2302 ", self._click_home, sh.current_server is not None),
            ("class:tabbar", " "),
            self._button(" B ", self._click_boards, sh.current_server is not None,
                         style=("class:tabbar.active" if sh.in_boards(here)
                                else "class:tabbar.btn")),
            ("class:tabbar", "  "),
        ]

    def _click_back(self) -> None:
        stack = self.shell.nav_stack
        self._spawn(self.shell.back(), stack[-1][1] if stack else None)

    def _click_reload(self) -> None:
        t = self.tab
        if t.server and t.path:
            self._spawn(self.shell.open_page(t.server, t.path, push_history=False),
                        t.path)

    def _click_home(self) -> None:
        self._spawn(self.shell.home(), "index.vn")

    def _click_boards(self) -> None:
        # Same toggle as ctrl-b: in the boards it leaves them, outside it enters.
        if self.shell.in_boards(self._here()):
            self._spawn(self.shell.home(), "index.vn")
        else:
            self._spawn(self.shell.open_boards(), page.BOARD_PREFIX)

    def _tabbar(self):
        frags = self._toolbar()
        for i, t in enumerate(self.tabs):
            def sel(ev, i=i):
                if ev.event_type == MouseEventType.MOUSE_UP:
                    self.active = i
                    self._refresh()
                else:
                    return NotImplemented
            style = "class:tabbar.active" if i == self.active else "class:tabbar.tab"
            label = self._tab_label(t)
            frags.append((style, f" {i + 1} {label} " if label else f" {i + 1} ", sel))
            # A close box on the tab itself, where a browser puts it.
            if label and (len(self.tabs) > 1 or not t.empty):
                frags.append(self._button(
                    "\u00d7 ", (lambda i=i: self._close_tab(i)),
                    style=("class:tabbar.close" if i == self.active
                           else "class:tabbar.tab")))
            frags.append(("class:tabbar", " "))
        frags.append(self._button(" + ", self._new_tab))
        frags.append(("class:tabbar", ""))
        return frags

    def _status(self):
        t = self.tab
        conn = ("class:status.on", " connected ") if self.shell.client else \
               ("class:status.off", " offline ")
        where = f"→ {t.loading_path}" if t.loading and t.loading_path else t.where
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
        self._spawn(self.shell.go(idx + 1), self.tab.links[idx].path)

    def _spawn(self, coro, target: str | None = None) -> None:
        # Async updating tab without locking UI
        tab = self.tab
        tab.loading = True
        tab.loading_path = target
        self._refresh()

        async def run():
            _op_tab.set(tab)
            try:
                await coro
            except Exception as exc:
                logger.exception("command failed")
                if tab in self.tabs:
                    tab.messages.append(("class:msg.err", f"{type(exc).__name__}: {exc}"))
            finally:
                tab.loading = False
                tab.loading_path = None
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
        if line == "help":
            self._help_tab()
            return False
        self._spawn(self.shell.run_command(line), self._destination(line))
        return False

    def _destination(self, line: str) -> str | None:
        try:
            parts = shlex.split(line)
        except ValueError:
            return None
        if not parts:
            return None
        cmd, args = parts[0], parts[1:]
        if cmd == "open":
            return args[1] if len(args) > 1 else "index.vn"
        if cmd == "boards":
            return page.BOARD_PREFIX
        if cmd == "home":
            return "index.vn"
        if cmd == "b" and args:
            return f"{page.BOARD_PREFIX}/{args[0]}"
        if cmd == "ls":
            return LISTING_PATH
        if cmd == "back":
            stack = self.shell.nav_stack
            return stack[-1][1] if stack else None
        if cmd == "get" and args:
            return args[0]
        if cmd == "post":
            return self.shell.current_path
        if cmd == "go" and args and args[0].isdigit():
            links = self.shell.current_links
            n = int(args[0])
            return links[n - 1].path if 1 <= n <= len(links) else None
        if cmd == "fav" and args and args[0].isdigit():
            favs = self.shell.config.favorites
            n = int(args[0])
            return favs[n - 1].path if 1 <= n <= len(favs) else None
        return None

    def _focus_content(self) -> None:
        if self.app:
            self.app.layout.focus(self.content_window)

    def _new_tab(self) -> None:
        self.tabs.append(Tab())
        self.active = len(self.tabs) - 1
        self._refresh()

    def _close_tab(self, index: int | None = None) -> None:
        i = self.active if index is None else index
        if not (0 <= i < len(self.tabs)):
            return
        if len(self.tabs) == 1:
            # Last tab: empty it on the UI
            self.tabs[0] = Tab()
            self.active = 0
        else:
            self.tabs.pop(i)
            self.active = min(self.active if i > self.active else self.active - 1,
                              len(self.tabs) - 1)
            self.active = max(0, self.active)
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
        sh = self.shell
        in_board = sh.in_boards(self._here())
        rows = ["# vagrantNet", "", "## Keys", ""]
        rows += [f"  {k:<10} {w}" for k, w in KEYS_HELP + KEYS_MORE]
        rows += ["", "## Commands", ""]
        rows += ["  " + l for l in HELP_TEXT.splitlines()]
        if in_board:
            # Only worth the screen space while a board is actually open.
            rows += ["", "## In a board", ""]
            rows += [f"  {k:<10} {w}" for k, w in KEYS_BOARD]
            rows += [""]
            rows += ["  " + l for l in BOARD_HELP.splitlines()]
        tab = Tab(server=None, path="help", text="\n".join(rows))
        self.tabs.append(tab)
        self.active = len(self.tabs) - 1
        self._refresh()

    # ---------------- wiring ----------------------------------------------
    def _bindings(self) -> KeyBindings:
        kb = KeyBindings()
        composing = Condition(lambda: self.compose_open)
        insert = Condition(lambda: self.cmd_open or self.compose_open)

        @kb.add("c-b", filter=~insert)
        def _(event):
            if self.shell.in_boards(self._here()):
                self._spawn(self.shell.home(), "index.vn")
            else:
                self._spawn(self.shell.open_boards(), page.BOARD_PREFIX)

        @kb.add("c-p", filter=~insert)
        def _(event): self._open_compose()

        @kb.add("c-s", filter=composing, eager=True)
        def _(event): self._send_compose()

        @kb.add("escape", filter=composing, eager=True)
        def _(event):
            self.compose_open = False
            self._focus_content()
            self._refresh()

        for n in range(1, 10):
            @kb.add(f"escape", f"{n}", filter=~insert)
            def _(event, n=n):
                order = self.shell.board_order
                if n <= len(order):
                    self._spawn(self.shell.enter_board(order[n - 1]),
                                f"{page.BOARD_PREFIX}/{order[n - 1]}")

        @kb.add("c-q")
        def _(event): event.app.exit()

        @kb.add("c-e", filter=~insert)
        def _(event):
            self.cmd_open = True
            self.cmd.text = ""
            event.app.layout.focus(self.cmd)
            self._refresh()

        @kb.add("escape", filter=Condition(lambda: self.cmd_open), eager=True)
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
                self._spawn(self.shell.open_page(t.server, t.path, push_history=False),
                            t.path)

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
        def _(event):
            stack = self.shell.nav_stack
            self._spawn(self.shell.back(), stack[-1][1] if stack else None)

        for n in range(1, 10):
            @kb.add(str(n), filter=~insert)
            def _(event, n=n): self._follow(n - 1)

        return kb

    def build(self) -> Application:
        self.content_control = FormattedTextControl(
            self._content, focusable=True, show_cursor=False)
        self.content_window = Window(self.content_control, wrap_lines=False)
        sidebar = ConditionalContainer(
            Window(FormattedTextControl(self._sidebar), width=21, style="class:side"),
            # Only while boards=true
            filter=Condition(lambda: self.shell.in_boards(self._here())),
        )
        layout = Layout(HSplit([
            Window(FormattedTextControl(self._tabbar), height=1, style="class:tabbar"),
            VSplit([sidebar, self.content_window]),
            Window(FormattedTextControl(self._status), height=1, style="class:status"),
            ConditionalContainer(
                HSplit([
                    Window(FormattedTextControl(self._compose_header), height=1),
                    self.compose,
                ]),
                filter=Condition(lambda: self.compose_open),
            ),
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
            tab = self.ui._target_tab()
            if tab is None:
                return
            tab.messages.append(("class:msg.err", self.format(record)))
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
