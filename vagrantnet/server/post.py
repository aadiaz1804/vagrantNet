"""Server admin post to a local board without going over the radio.

For whoever runs the server: announcements, net notices, anything a script or
cron job should say. Writes straight to the board log.
"""

from __future__ import annotations
import argparse
import sys
from pathlib import Path
from . import boards
from .config import ServerConfig

DEFAULT_NICK = "[Server]"

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vagrantnet.server.post", description=__doc__)
    ap.add_argument("config", type=Path)
    ap.add_argument("board")
    ap.add_argument("subject", nargs="?", default="")
    ap.add_argument("-m", "--message")
    ap.add_argument("-f", "--file", type=Path, help="read the body from a file, or stdin")
    ap.add_argument("--nick", default=DEFAULT_NICK)
    ap.add_argument("--thread", type=int, help="reply to this thread instead of starting one")
    args = ap.parse_args(argv)

    if args.file is not None:
        text = sys.stdin.read() if str(args.file) == "-" else args.file.read_text(encoding="utf-8")
    elif args.message is not None:
        text = args.message
    else:
        text = sys.stdin.read()
    text = text.strip()

    if not text:
        print("nothing to post", file=sys.stderr)
        return 1
    if args.thread is None and not args.subject:
        print("a new thread needs a subject", file=sys.stderr)
        return 1

    config = ServerConfig.load(args.config)
    if boards.board_dir(config.boards_dir, args.board) is None:
        existing = ", ".join(b.name for b in boards.list_boards(config.boards_dir)) or "none"
        print(f"no board {args.board!r} in {config.boards_dir} (have: {existing})",
              file=sys.stderr)
        return 1

    if len(text.encode("utf-8")) > boards.MAX_TEXT:
        print(f"post is {len(text.encode('utf-8'))} bytes, over the "
              f"{boards.MAX_TEXT}-byte limit: output will be truncated", file=sys.stderr)

    post = boards.append_post(
        config.boards_dir, args.board, author="", nick=args.nick,
        text=text, thread=args.thread, subject=args.subject,
    )
    if post is None:
        print(f"could not post, thread {args.thread} does not exist", file=sys.stderr)
        return 1

    where = f"thread {post.thread}" if args.thread else f"new thread {post.seq}"
    print(f"posted to {args.board} as {args.nick}: {where}, seq {post.seq}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
