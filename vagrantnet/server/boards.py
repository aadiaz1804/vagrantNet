"""Message boards: storage and .vn rendering.

Boards are served over GET_PAGE
    b                     the board list
    b/<board>             threads, newest activity first
    b/<board>:<seq>       only what arrived after <seq>
    b/<board>/<thread>    one thread's posts

Each board is an append-only JSONL file to work natively with Linux
Authors are the 6-byte pubkey prefix from the request. Self declared
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..common import page

BOARD_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
POSTS_FILE = "posts.jsonl"
META_FILE = "meta.json"
PINNED_FILE = "pinned.vn"  # shown above the threads; owner edits it on the server
MOTD_FILE = "motd.vn"      # same, on the board index

# Pinned text rides along on every fetch so keep short
MAX_PINNED_BYTES = 512

MAX_SUBJECT = 48
MAX_TEXT = 2048  # same ceiling as a page
MAX_POSTS_PER_PAGE = 12
MAX_THREADS_PER_PAGE = 20

@dataclass
class Post:
    seq: int
    thread: int
    author: str
    nick: str
    at: float
    text: str
    subject: str = ""

    @property
    def starts_thread(self) -> bool:
        return self.seq == self.thread

@dataclass
class Board:
    name: str
    title: str = ""
    desc: str = ""
    posts: list[Post] = field(default_factory=list)
    pinned: list[str] = field(default_factory=list)

    @property
    def last_seq(self) -> int:
        return self.posts[-1].seq if self.posts else 0

@dataclass
class Upload:
    """A post arriving in pieces. Keyed by (client prefix, request id)."""
    dest: str
    total: int
    compressed: bool
    dict_id: int
    checksum: int = 0
    chunks: dict[int, bytes] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)

    def accept(self, index: int, payload: bytes) -> bool:
        """Store one chunk. Re-sending the same index just overwrites it."""
        if not 0 <= index < self.total:
            return False
        self.chunks[index] = payload
        return True

    @property
    def complete(self) -> bool:
        # Every index, not just the right count -- counting alone lets a
        # misnumbered chunk look complete and then fail on assembly.
        return all(i in self.chunks for i in range(self.total))

    def body(self) -> bytes:
        return b"".join(self.chunks[i] for i in range(self.total))

class UploadStore:
    # Temp partial post store
    def __init__(self, ttl_seconds: float = 120.0, max_open: int = 8):
        self.ttl = ttl_seconds
        self.max_open = max_open
        self._open: dict[tuple[bytes, int], Upload] = {}
        # Committed request ids
        self._committed: dict[tuple[bytes, int], float] = {}

    def _sweep(self) -> None:
        now = time.monotonic()
        for key, up in list(self._open.items()):
            if now - up.started > self.ttl:
                del self._open[key]
        for key, at in list(self._committed.items()):
            if now - at > self.ttl:
                del self._committed[key]

    def already_committed(self, key) -> bool:
        self._sweep()
        return key in self._committed

    def mark_committed(self, key) -> None:
        self._committed[key] = time.monotonic()

    def begin(self, key, dest: str, total: int, compressed: bool,
              dict_id: int, checksum: int = 0) -> Upload | None:
        self._sweep()
        if key not in self._open and len(self._open) >= self.max_open:
            return None
        up = self._open.get(key)
        if up is None:
            up = Upload(dest=dest, total=total, compressed=compressed,
                        dict_id=dict_id, checksum=checksum)
            self._open[key] = up
        return up

    def get(self, key) -> Upload | None:
        self._sweep()
        return self._open.get(key)

    def done(self, key) -> None:
        self._open.pop(key, None)

def valid_board_name(name: str) -> bool:
    return bool(BOARD_NAME_RE.match(name or ""))

def board_dir(root: Path, name: str) -> Path | None:
    if not valid_board_name(name):
        return None
    d = root / name
    return d if d.is_dir() else None

def list_boards(root: Path) -> list[Board]:
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if not valid_board_name(d.name):
            continue
        out.append(load_board(root, d.name, with_posts=False))
    return [b for b in out if b]

def load_board(root: Path, name: str, *, with_posts: bool = True) -> Board | None:
    d = board_dir(root, name)
    if d is None:
        return None
    meta = {}
    try:
        meta = json.loads((d / META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    board = Board(name=name, title=meta.get("title") or name,
                  desc=meta.get("desc", ""),
                  pinned=_read_notice(d / PINNED_FILE))
    if with_posts:
        board.posts = _read_posts(d / POSTS_FILE)
    return board

def _read_posts(path: Path) -> list[Post]:
    posts: list[Post] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return posts
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            posts.append(Post(seq=int(d["seq"]), thread=int(d["thread"]),
                              author=d.get("author", ""), nick=d.get("nick", ""),
                              at=float(d.get("at", 0)), text=d.get("text", ""),
                              subject=d.get("subject", "")))
        except (ValueError, KeyError, TypeError):
            continue  # a torn line at the tail shouldn't lose the board
    return posts

def append_post(root: Path, name: str, *, author: str, nick: str, text: str,
                thread: int | None = None, subject: str = "") -> Post | None:
    """Add a post. thread=None starts a new one. Returns None if the board is gone."""
    d = board_dir(root, name)
    if d is None:
        return None
    posts = _read_posts(d / POSTS_FILE)
    seq = (posts[-1].seq if posts else 0) + 1
    if thread is not None and not any(p.thread == thread for p in posts):
        return None  # replying into a thread that doesn't exist
    post = Post(seq=seq, thread=thread if thread is not None else seq,
                author=author, nick=nick[:24], at=time.time(),
                text=text[:MAX_TEXT], subject=subject[:MAX_SUBJECT])
    row = json.dumps({"seq": post.seq, "thread": post.thread,
                      "author": post.author, "nick": post.nick,
                      "at": round(post.at), "subject": post.subject,
                      "text": post.text}, ensure_ascii=False)
    with open(d / POSTS_FILE, "a", encoding="utf-8") as f:
        f.write(row + "\n")
    return post

# ------------------ rendering ------------------------------------
def _read_notice(path: Path) -> list[str]:
    """A pinned/motd .vn snippet, trimmed to its airtime budget."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    if len(raw.encode("utf-8")) > MAX_PINNED_BYTES:
        kept, total = [], 0
        for line in raw.splitlines():
            total += len(line.encode("utf-8")) + 1
            if total > MAX_PINNED_BYTES:
                break
            kept.append(line)
        raw = "\n".join(kept)
    lines = [l for l in raw.splitlines() if l.strip()]

    # Close a colour the operator left open.
    open_colour = False
    for line in lines:
        name, args = page.directive(line)
        if name == page.COLOUR_DIRECTIVE:
            open_colour = bool(args)
    if open_colour:
        lines.append("!c")
    return lines

def _ago(then: float, now: float | None = None) -> str:
    secs = max(0, int((now if now is not None else time.time()) - then))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit} ago"
    return "just now"

def render_index(boards: list[Board], seqs: dict[str, int] | None = None,
                 root: Path | None = None) -> str:
    out = ["# Boards", ""]
    if root is not None:
        motd = _read_notice(root / MOTD_FILE)
        if motd:
            out.extend(motd)
            out.append("---")
    if not boards:
        out.append("No boards yet.")
    for b in boards:
        # !bseq lets the client work out every unread count from this one page
        out.append(f"!bseq {b.name} {(seqs or {}).get(b.name, 0)}")
        out.append(f"[{b.title}|b/{b.name}]")
        if b.desc:
            out.append(f"> {b.desc}")
    return "\n".join(out) + "\n"

def last_seq(path: Path, tail_bytes: int = 4096) -> int:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            tail = f.read()
    except OSError:
        return 0
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return int(json.loads(line)["seq"])
        except (ValueError, KeyError, TypeError):
            continue  # partial first line of the window, or a torn tail
    return 0

def board_seqs(root: Path) -> dict[str, int]:
    return {b.name: last_seq(root / b.name / POSTS_FILE) for b in list_boards(root)}

def render_threads(board: Board, since: int = 0, before: int = 0) -> str:
    """Threads by most recent activity.

    since>0  only threads that moved after that seq (catch-up)
    before>0 only threads older than that seq (paging back through history)
    """
    latest: dict[int, Post] = {}
    starters: dict[int, Post] = {}
    unread: dict[int, int] = {}
    for p in board.posts:
        starters.setdefault(p.thread, p)
        latest[p.thread] = p
        if p.seq > since:
            unread[p.thread] = unread.get(p.thread, 0) + 1

    threads = sorted(latest.values(), key=lambda p: p.seq, reverse=True)
    if since:
        threads = [p for p in threads if unread.get(p.thread)]
    if before:
        threads = [p for p in threads if p.seq < before]

    out = [f"# {board.title}", f"!seq {board.last_seq}", ""]
    if board.pinned:
        out.append("!c yellow")
        out.extend(board.pinned)
        out.append("!c")
        out.append("---")
    if not threads:
        out.append("Nothing new." if since else "No threads yet. Post the first one.")
    dropped = max(0, len(threads) - MAX_THREADS_PER_PAGE)
    threads = threads[:MAX_THREADS_PER_PAGE]
    now = time.time()
    for tip in threads:
        head = starters.get(tip.thread)
        subject = (head.subject if head else "") or "(no subject)"
        replies = sum(1 for p in board.posts if p.thread == tip.thread) - 1
        new = unread.get(tip.thread, 0)
        mark = f" +{new}" if since and new else ""
        out.append(f"[{subject}|b/{board.name}/{tip.thread}]")
        out.append(f"> {replies} repl{'y' if replies == 1 else 'ies'}, "
                   f"{_ago(tip.at, now)}{mark}")
    if dropped and threads:
        # A link to archived data
        out.append(f"[{dropped} older thread(s)|b/{board.name}@{threads[-1].seq}]")
    if board.posts and not since and not before:
        out.append(f"[Archive by month|b/{board.name}/archive]")
    return "\n".join(out) + "\n"

def render_thread(board: Board, thread: int, since: int = 0) -> str | None:
    posts = [p for p in board.posts if p.thread == thread]
    if not posts:
        return None
    head = posts[0]
    remaining = [p for p in posts if p.seq > since]
    shown = remaining[:MAX_POSTS_PER_PAGE]
    more = remaining[MAX_POSTS_PER_PAGE:]

    out = [f"# {head.subject or '(no subject)'}",
           f"!seq {board.last_seq}", ""]
    if not shown:
        out.append("Nothing new in this thread.")
    now = time.time()
    for p in shown:
        out.append("!c cyan")
        out.append(f"{p.nick or p.author[:6]}  {_ago(p.at, now)}")
        out.append("!c")
        out.extend(p.text.splitlines() or [""])
        out.append("---")
    if more:
        # Paging is forward-only, which is also how you read a thread.
        out.append(f"[{len(more)} more post(s)|b/{board.name}/{thread}:{shown[-1].seq}]")
    out.append(f"[Back to {board.title}|b/{board.name}]")
    return "\n".join(out) + "\n"

def _month(ts: float) -> str:
    return time.strftime("%Y-%m", time.localtime(ts))

def _month_name(key: str) -> str:
    try:
        return time.strftime("%B %Y", time.strptime(key, "%Y-%m"))
    except ValueError:
        return key

def render_archive(board: Board) -> str:
    months: dict[str, int] = {}
    for p in board.posts:
        key = _month(p.at)
        months[key] = months.get(key, 0) + 1

    out = [f"# {board.title} archive", ""]
    if not months:
        out.append("Nothing archived yet.")
    for key in sorted(months, reverse=True):
        out.append(f"[{_month_name(key)}|b/{board.name}/archive/{key}]")
        out.append(f"> {months[key]} post(s)")
    out.append(f"[Back to {board.title}|b/{board.name}]")
    return "\n".join(out) + "\n"

def render_month(board: Board, key: str) -> str | None:
    """Threads with activity in one month."""
    try:
        time.strptime(key, "%Y-%m")
    except ValueError:
        return None

    starters: dict[int, Post] = {}
    latest: dict[int, Post] = {}
    for p in board.posts:
        starters.setdefault(p.thread, p)
        if _month(p.at) == key:
            latest[p.thread] = p

    out = [f"# {_month_name(key)}", ""]
    if not latest:
        out.append("Nothing that month.")
    threads = sorted(latest.values(), key=lambda p: p.seq, reverse=True)
    dropped = max(0, len(threads) - MAX_THREADS_PER_PAGE)
    for tip in threads[:MAX_THREADS_PER_PAGE]:
        head = starters.get(tip.thread)
        subject = (head.subject if head else "") or "(no subject)"
        out.append(f"[{subject}|b/{board.name}/{tip.thread}]")
    if dropped:
        out.append(f"> {dropped} more that month, narrow the range")
    out.append(f"[Archive|b/{board.name}/archive]")
    return "\n".join(out) + "\n"
