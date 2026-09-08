"""
.vn page format: minimal parser/renderer.

  # / ## / ###   headings
  [Label|path]   link
  > text         quote line
  ---            horizontal rule
  !c <colour>    colour every following line until the next !c (!c alone resets)
  !allow <key>   restrict this page to the listed pkeys (whitelisting)
  anything else  plain paragraph text

Malformed lines (including an unknown `!directive`) render as plain text.

`!c` colours a *block*, not a span. Done for bandwidth
`!allow` Matched against a 6-byte pubkey prefix

A link whose path does not end in `.vn` (and is not the special `files` listing
path) names a plain file clients can fetch it with GET_FILE and save.
"""

from __future__ import annotations
import re
from dataclasses import dataclass

LINK_RE = re.compile(r"^\[(?P<label>[^|\]]+)\|(?P<path>[^\]]+)\]$")
# Named colours only, no hex for compression v1 engine
COLOURS = ("dim", "red", "green", "yellow", "blue", "magenta", "cyan", "white")
DEFAULT_COLOUR = ""

SERVER_DIRECTIVES = ("allow",)
CLIENT_DIRECTIVES = ("seq", "bseq")  # read by the client
FILES_LISTING_PATH = "files"  # server-generated listing of downloads_dir
BOARD_PREFIX = "b"  # b, b/<board>, b/<board>:<seq> (new), b/<board>@<seq> (older)

_ANSI_RESET = "\x1b[0m"
_ANSI_BOLD = "\x1b[1m"
_ANSI_UNDERLINE = "\x1b[4m"
_ANSI_DIM = "\x1b[2m"
_ANSI_COLOUR = {
    "red": "\x1b[31m", "green": "\x1b[32m", "yellow": "\x1b[33m",
    "blue": "\x1b[34m", "magenta": "\x1b[35m", "cyan": "\x1b[36m",
    "white": "\x1b[37m", "dim": _ANSI_DIM,
}

def is_page_path(path: str) -> bool:
    p = (path or "").strip("/")
    return (p in ("", FILES_LISTING_PATH) or p.endswith(".vn")
            or p == BOARD_PREFIX or p.startswith(BOARD_PREFIX + "/"))

def board_seqs(vn_text: str) -> dict[str, int]:
    # !bseq <board> <seq> on the index, so one fetch gives every unread count.
    out: dict[str, int] = {}
    for raw in vn_text.splitlines():
        parts = raw.strip()[1:].split() if raw.strip().startswith("!") else []
        if len(parts) > 2 and parts[0] == "bseq":
            try:
                out[parts[1]] = int(parts[2])
            except ValueError:
                continue
    return out

def seq(vn_text: str) -> int | None:
    # Highest board sequence this page reflects, for tracking what's unread.
    for raw in vn_text.splitlines():
        parts = raw.strip()[1:].split() if raw.strip().startswith("!") else []
        if len(parts) > 1 and parts[0] == "seq":
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None

@dataclass
class Link:
    label: str
    path: str

@dataclass
class Line:
    kind: str  # h1 h2 h3 link quote rule text
    text: str = ""
    link: Link | None = None
    colour: str = DEFAULT_COLOUR

def directives(vn_text: str) -> list[tuple[str, list[str]]]:
    # Server-side command processing
    out = []
    for raw in vn_text.splitlines():
        stripped = raw.strip()
        if not stripped.startswith("!"):
            continue
        parts = stripped[1:].split()
        if parts and parts[0] in SERVER_DIRECTIVES:
            out.append((parts[0], parts[1:]))
    return out

def strip_server_directives(vn_text: str) -> str:
    # Page ready to transmit
    keep = []
    for raw in vn_text.splitlines():
        parts = raw.strip()[1:].split() if raw.strip().startswith("!") else []
        if parts and parts[0] in SERVER_DIRECTIVES:
            continue
        keep.append(raw)
    return "\n".join(keep) + ("\n" if vn_text.endswith("\n") else "")

def parse(vn_text: str) -> list[Line]:
    # Parse line/colors
    lines: list[Line] = []
    colour = DEFAULT_COLOUR
    for raw in vn_text.splitlines():
        stripped = raw.strip()

        if stripped.startswith("!"):
            parts = stripped[1:].split()
            name = parts[0] if parts else ""
            if name in SERVER_DIRECTIVES or name in CLIENT_DIRECTIVES:
                continue
            if name == "c":
                arg = parts[1].lower() if len(parts) > 1 else ""
                colour = arg if arg in COLOURS else DEFAULT_COLOUR
                continue
            # unknown directive, add as text
            lines.append(Line("text", raw, colour=colour))
            continue

        match = LINK_RE.match(stripped)
        if match:
            lines.append(Line("link", colour=colour,
                              link=Link(match.group("label"), match.group("path"))))
        elif stripped.startswith("### "):
            lines.append(Line("h3", stripped[4:], colour=colour))
        elif stripped.startswith("## "):
            lines.append(Line("h2", stripped[3:], colour=colour))
        elif stripped.startswith("# "):
            lines.append(Line("h1", stripped[2:], colour=colour))
        elif stripped.startswith("> "):
            lines.append(Line("quote", stripped[2:], colour=colour))
        elif stripped == "---":
            lines.append(Line("rule", colour=colour))
        else:
            lines.append(Line("text", raw, colour=colour))
    return lines

def extract_links(vn_text: str) -> list[Link]:
    # Pull out every [Label|path] link, in document order.
    links: list[Link] = []
    for line in vn_text.splitlines():
        m = LINK_RE.match(line.strip())
        if m:
            links.append(Link(label=m.group("label"), path=m.group("path")))
    return links

def render_ansi(vn_text: str) -> str:
    out_lines: list[str] = []
    for line in parse(vn_text):
        colour = _ANSI_COLOUR.get(line.colour, "")
        if line.kind == "link":
            marker = "" if is_page_path(line.link.path) else "↓ "  # download
            out_lines.append(
                f"  {colour}{_ANSI_UNDERLINE}{marker}{line.link.label}"
                f"{_ANSI_RESET} {_ANSI_DIM}[{line.link.path}]{_ANSI_RESET}"
            )
        elif line.kind == "h3":
            out_lines.append(f"{colour}{_ANSI_BOLD}{line.text}{_ANSI_RESET}")
        elif line.kind == "h2":
            out_lines.append(f"\n{colour}{_ANSI_BOLD}{line.text.upper()}{_ANSI_RESET}")
        elif line.kind == "h1":
            out_lines.append(f"\n{colour}{_ANSI_BOLD}=== {line.text.upper()} ==={_ANSI_RESET}\n")
        elif line.kind == "quote":
            out_lines.append(f"  {colour or _ANSI_DIM}| {line.text}{_ANSI_RESET}")
        elif line.kind == "rule":
            out_lines.append((colour or _ANSI_DIM) + ("-" * 40) + _ANSI_RESET)
        else:
            out_lines.append(f"{colour}{line.text}{_ANSI_RESET}" if colour else line.text)
    return "\n".join(out_lines)


def wire_cost(vn_text: str) -> tuple[int, int, bool]:
    # Cost to transmit (bytes, chunks, compressed?).
    from . import chunking, compress

    raw = vn_text.encode("utf-8")
    raw_chunks = chunking.split(raw)
    packed, _ = compress.compress(raw)
    packed_chunks = chunking.split(packed)
    if len(packed_chunks) < len(raw_chunks):
        return len(packed), len(packed_chunks), True
    return len(raw), len(raw_chunks), False

def validate(
    vn_text: str, max_bytes: int = 2_048, max_airtime_seconds: float = 5.0
) -> list[str]:
    # Non-fatal lint. Against 2kb/5s airtime convention
    from .envelope import MAX_TOTAL_CHUNKS, MAX_UNCOMPRESSED_SIZE
    from . import airtime

    warnings: list[str] = []
    size = len(vn_text.encode("utf-8"))
    wire, chunks, packed = wire_cost(vn_text)

    if size > MAX_UNCOMPRESSED_SIZE:
        warnings.append(
            f"page is {size} bytes, over the {MAX_UNCOMPRESSED_SIZE}-byte "
            "protocol limit -- a server will refuse to send it"
        )
    if chunks > MAX_TOTAL_CHUNKS:
        warnings.append(
            f"page needs {chunks} chunks, over the {MAX_TOTAL_CHUNKS}-chunk "
            "protocol limit -- a server will refuse to send it"
        )
    if size > max_bytes:
        warnings.append(
            f"page is {size} bytes uncompressed, over the recommended "
            f"{max_bytes}-byte guideline"
        )

    # What it actually costs the mesh.
    seconds = airtime.fetch_cost(chunks)
    if seconds > max_airtime_seconds:
        warnings.append(
            f"page costs {seconds:.1f}s of shared airtime per fetch at zero "
            f"hop ({airtime.share_of_mesh_day(seconds):.2f}% of the mesh's "
            f"day), over the {max_airtime_seconds:.0f}s guideline"
            "split file and link it instead of transmitting everything is possible"
        )
    for i, line in enumerate(vn_text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("[") and not LINK_RE.match(stripped):
            warnings.append(f"line {i}: looks like a broken link: {stripped!r}")
    return warnings

if __name__ == "__main__":
    # Lint a .vn page before publishing it
    import sys
    from . import airtime

    if len(sys.argv) < 2:
        print("usage: python -m vagrantnet.common.page <page.vn> [...]")
        raise SystemExit(1)
    bad = 0
    for path in sys.argv[1:]:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        wire, chunks, packed = wire_cost(text)
        how = "compressed" if packed else "raw"
        seconds = airtime.fetch_cost(chunks)
        print(f"{path}: {wire} bytes on the wire ({how}), {chunks} chunk(s), "
              f"{seconds:.1f}s airtime "
              f"({airtime.share_of_mesh_day(seconds):.2f}% of the mesh's day), "
              f"{len(extract_links(text))} link(s)")
        for warning in validate(text):
            bad += 1
            print(f"  ! {warning}")
    raise SystemExit(1 if bad else 0)
