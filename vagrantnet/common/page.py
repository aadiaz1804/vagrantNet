"""
.vn page format: minimal parser/renderer.

  # / ## / ###   headings
  [Label|path]   link
  > text         quote line
  ---            horizontal rule
  !c <colour>    colour every following line until the next !c (!c alone resets)
  !allow <key>   restrict this page to the listed pkeys (whitelisting)
  anything else  plain paragraph text

Malformed lines render as plain text.

`!c` colours a *block*, not a span. Done for bandwidth
`!allow` never reaches the radio, done server-side
"""

from __future__ import annotations
import re
from dataclasses import dataclass

LINK_RE = re.compile(r"^\[(?P<label>[^|\]]+)\|(?P<path>[^\]]+)\]$")
# Named colours only, no hex for compression v1 engine
COLOURS = ("dim", "red", "green", "yellow", "blue", "magenta", "cyan", "white")
DEFAULT_COLOUR = ""

SERVER_DIRECTIVES = ("allow",)
_ANSI_RESET = "\x1b[0m"
_ANSI_BOLD = "\x1b[1m"
_ANSI_UNDERLINE = "\x1b[4m"
_ANSI_DIM = "\x1b[2m"

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
            if name in SERVER_DIRECTIVES:
                continue  # server-only
            if name == "c":
                arg = parts[1].lower() if len(parts) > 1 else ""
                colour = arg if arg in COLOURS else DEFAULT_COLOUR
                continue
            continue  # unknown directive, continues

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
    """Pull out every [Label|path] link, in document order."""
    links: list[Link] = []
    for line in vn_text.splitlines():
        m = LINK_RE.match(line.strip())
        if m:
            links.append(Link(label=m.group("label"), path=m.group("path")))
    return links

def render_ansi(vn_text: str) -> str:
    out_lines: list[str] = []
    for raw_line in vn_text.splitlines():
        line = raw_line.rstrip("\n")
        stripped = line.strip()

        link_match = LINK_RE.match(stripped)
        if link_match:
            out_lines.append(
                f"  {_ANSI_UNDERLINE}{link_match.group('label')}"
                f"{_ANSI_RESET} {_ANSI_DIM}[{link_match.group('path')}]{_ANSI_RESET}"
            )
        elif stripped.startswith("### "):
            out_lines.append(f"{_ANSI_BOLD}{stripped[4:]}{_ANSI_RESET}")
        elif stripped.startswith("## "):
            out_lines.append(f"\n{_ANSI_BOLD}{stripped[3:].upper()}{_ANSI_RESET}")
        elif stripped.startswith("# "):
            out_lines.append(f"\n{_ANSI_BOLD}=== {stripped[2:].upper()} ==={_ANSI_RESET}\n")
        elif stripped.startswith("> "):
            out_lines.append(f"  {_ANSI_DIM}| {stripped[2:]}{_ANSI_RESET}")
        elif stripped == "---":
            out_lines.append(_ANSI_DIM + ("-" * 40) + _ANSI_RESET)
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def wire_cost(vn_text: str) -> tuple[int, int, bool]:
    # Cost to transmit (bytes, chunks, compressed?).
    from . import chunking, compress

    raw = vn_text.encode("utf-8")
    raw_chunks = chunking.split(raw)
    packed = compress.compress(raw)
    packed_chunks = chunking.split(packed)
    if len(packed_chunks) < len(raw_chunks):
        return len(packed), len(packed_chunks), True
    return len(raw), len(raw_chunks), False

def validate(vn_text: str, max_bytes: int = 10_240) -> list[str]:
    # Non-fatal lint
    from .envelope import MAX_TOTAL_CHUNKS, MAX_UNCOMPRESSED_SIZE

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
    for i, line in enumerate(vn_text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("[") and not LINK_RE.match(stripped):
            warnings.append(f"line {i}: looks like a broken link: {stripped!r}")
    return warnings

if __name__ == "__main__":
    # Lint a .vn page before publishing it
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m vagrantnet.common.page <page.vn> [...]")
        raise SystemExit(1)
    bad = 0
    for path in sys.argv[1:]:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        wire, chunks, packed = wire_cost(text)
        how = "compressed" if packed else "raw"
        print(f"{path}: {wire} bytes on the wire ({how}), {chunks} chunk(s), "
              f"~{chunks * 0.6:.1f}s to fetch, {len(extract_links(text))} link(s)")
        for warning in validate(text):
            bad += 1
            print(f"  ! {warning}")
    raise SystemExit(1 if bad else 0)
