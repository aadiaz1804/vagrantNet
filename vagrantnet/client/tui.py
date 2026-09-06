"""vagrantNet interactive client shell """

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import NestedCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from ..common.envelope import Subcommand
from ..common.page import Link, extract_links, render_ansi
from .client import VagrantNetClient, VagrantNetError
from .config import ClientConfig, DEFAULT_CONFIG_PATH

logger = logging.getLogger("vagrantnet.tui")

HISTORY_PATH = DEFAULT_CONFIG_PATH.parent / "shell_history"
_BLE_ADDRESS_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

HELP_TEXT = """\
connect <serial-port|ble-address> [pin]   connect to a radio
server add <name> <pubkey-hex>            remember a server under a short name
server ls                                 list saved servers
open <server> [path]                      fetch and render a page (default: index.vn)
go <n>                                    follow link <n> from the current page
back                                      return to the previous page
ls [server]                               list a server's pages (default: current server)
get <path>                                download a file from the current server
fav add <label>                           save the current page as a favorite
fav ls                                    list favorites
fav <n>                                   open favorite <n>
help                                      show this text
quit / exit                               leave
"""

class Shell:
    def __init__(self) -> None:
        self.config = ClientConfig.load()
        self.client: VagrantNetClient | None = None
        self.current_server: str | None = None  # name or pubkey, as typed
        self.current_path: str | None = None
        self.current_links: list[Link] = []
        self.nav_stack: list[tuple[str, str]] = []

    # ---------------- connection -----------------------------------------
    async def connect(self, target: str, pin: str | None = None) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None

        kind = "ble" if _BLE_ADDRESS_RE.match(target) else "serial"
        print(f"connecting ({kind}) to {target}...")
        try:
            self.client = (
                await VagrantNetClient.connect_ble(target, pin=pin)
                if kind == "ble"
                else await VagrantNetClient.connect_serial(target)
            )
        except VagrantNetError as e:
            print(f"connect failed: {e}")
            return
        self.config.set_last_connection(kind, target, pin)
        print("connected.")

    async def autoconnect_if_known(self) -> None:
        if self.config.last_connection_target:
            print(f"reconnecting to last radio: {self.config.last_connection_target}")
            await self.connect(
                self.config.last_connection_target, self.config.last_connection_pin
            )

    def _require_client(self) -> VagrantNetClient | None:
        if self.client is None:
            print("not connected -- try: connect <serial-port|ble-address>")
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
            print(f"fetch failed: {e}")
            return

        if push_history and self.current_server is not None and self.current_path is not None:
            self.nav_stack.append((self.current_server, self.current_path))

        text = body.decode("utf-8", errors="replace")
        self.current_server = server
        self.current_path = path
        self.current_links = extract_links(text)
        print(render_ansi(text))
        if self.current_links:
            print()
            for i, link in enumerate(self.current_links, start=1):
                print(f"  [{i}] {link.label}  ({link.path})")

    async def go(self, n: int) -> None:
        if not (1 <= n <= len(self.current_links)):
            print(f"no link {n} on this page")
            return
        if self.current_server is None:
            print("no current page")
            return
        link = self.current_links[n - 1]
        await self.open_page(self.current_server, link.path)

    async def back(self) -> None:
        if not self.nav_stack:
            print("nothing to go back to")
            return
        server, path = self.nav_stack.pop()
        await self.open_page(server, path, push_history=False)

    async def list_pages(self, server: str | None) -> None:
        client = self._require_client()
        if client is None:
            return
        target = server or self.current_server
        if target is None:
            print("no server given and no current server -- try: ls <server>")
            return
        pubkey = self._resolve_server(target)
        try:
            body = await client.fetch(pubkey, Subcommand.LIST_PAGES, "")
        except VagrantNetError as e:
            print(f"list-pages failed: {e}")
            return
        print(render_ansi(body.decode("utf-8", errors="replace")))

    async def get_file(self, path: str) -> None:
        client = self._require_client()
        if client is None:
            return
        if self.current_server is None:
            print("no current server -- open a page first")
            return
        pubkey = self._resolve_server(self.current_server)
        try:
            body = await client.fetch(pubkey, Subcommand.GET_FILE, path)
        except VagrantNetError as e:
            print(f"get-file failed: {e}")
            return
        out_name = path.split("/")[-1] or "download.bin"
        with open(out_name, "wb") as f:
            f.write(body)
        print(f"saved {len(body)} bytes to {out_name}")

    # ---------------- favorites/servers -----------------------------------------
    def fav_add(self, label: str) -> None:
        if self.current_server is None or self.current_path is None:
            print("no current page to favorite")
            return
        self.config.add_favorite(self.current_server, self.current_path, label)
        print(f"favorited {label!r}")

    def fav_ls(self) -> None:
        if not self.config.favorites:
            print("no favorites yet -- try: fav add <label>")
            return
        for i, fav in enumerate(self.config.favorites, start=1):
            print(f"  [{i}] {fav.label}  ({fav.server}:{fav.path})")

    async def fav_open(self, n: int) -> None:
        if not (1 <= n <= len(self.config.favorites)):
            print(f"no favorite {n}")
            return
        fav = self.config.favorites[n - 1]
        await self.open_page(fav.server, fav.path)

    def server_add(self, name: str, pubkey: str) -> None:
        self.config.add_server(name, pubkey)
        print(f"saved server {name!r}")

    def server_ls(self) -> None:
        if not self.config.servers:
            print("no saved servers -- try: server add <name> <pubkey-hex>")
            return
        for name, pubkey in self.config.servers.items():
            print(f"  {name}  {pubkey}")

    # ---------------- command dispatch -----------------------------------------
    async def run_command(self, line: str) -> bool:
        """Returns False when the shell should exit."""
        try:
            parts = shlex.split(line)
        except ValueError as e:
            print(f"parse error: {e}")
            return True
        if not parts:
            return True

        cmd, args = parts[0], parts[1:]

        if cmd in ("quit", "exit"):
            return False
        elif cmd == "help":
            print(HELP_TEXT)
        elif cmd == "connect":
            if not args:
                print("usage: connect <serial-port|ble-address> [pin]")
            else:
                await self.connect(args[0], args[1] if len(args) > 1 else None)
        elif cmd == "server":
            if args and args[0] == "add" and len(args) == 3:
                self.server_add(args[1], args[2])
            elif args and args[0] == "ls":
                self.server_ls()
            else:
                print("usage: server add <name> <pubkey-hex> | server ls")
        elif cmd == "open":
            if not args:
                print("usage: open <server> [path]")
            else:
                await self.open_page(args[0], args[1] if len(args) > 1 else "index.vn")
        elif cmd == "go":
            if not args or not args[0].isdigit():
                print("usage: go <n>")
            else:
                await self.go(int(args[0]))
        elif cmd == "back":
            await self.back()
        elif cmd == "ls":
            await self.list_pages(args[0] if args else None)
        elif cmd == "get":
            if not args:
                print("usage: get <path>")
            else:
                await self.get_file(args[0])
        elif cmd == "fav":
            if args and args[0] == "add" and len(args) >= 2:
                self.fav_add(" ".join(args[1:]))
            elif args and args[0] == "ls":
                self.fav_ls()
            elif args and args[0].isdigit():
                await self.fav_open(int(args[0]))
            else:
                print("usage: fav add <label> | fav ls | fav <n>")
        else:
            print(f"unknown command: {line!r} (try: help)")
        return True

    async def shutdown(self) -> None:
        if self.client is not None:
            await self.client.disconnect()

def _completer() -> NestedCompleter:
    return NestedCompleter.from_nested_dict(
        {
            "connect": None,
            "server": {"add": None, "ls": None},
            "open": None,
            "go": None,
            "back": None,
            "ls": None,
            "get": None,
            "fav": {"add": None, "ls": None},
            "help": None,
            "quit": None,
            "exit": None,
        }
    )

async def main() -> None:
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
                    print("that command hit an unexpected error -- see log for details")
    finally:
        await shell.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
