"""
.vn page format: minimal parser/renderer.

  # / ## / ###   headings
  [Label|path]   link
  > text         quote line
  ---            horizontal rule
  anything else  plain paragraph text

Malformed lines render as plain text
"""

from __future__ import annotations

import re
from dataclasses import dataclass

LINK_RE = re.compile(r"^\[(?P<label>[^|\]]+)\|(?P<path>[^\]]+)\]$")

# TODO: Check if more ANSI codes are worth supporting
_ANSI_RESET = "\x1b[0m"
_ANSI_BOLD = "\x1b[1m"
_ANSI_UNDERLINE = "\x1b[4m"
_ANSI_DIM = "\x1b[2m"

@dataclass
class Link:
    label: str
    path: str

# TODO: Check if ANSI links are better than the custom format
# If so extract_links and link match are not needed, and render_ansi can be simplified to just return the text with ANSI codes.
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
        # TODO: Check rendering of headings
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


def validate(vn_text: str, max_bytes: int = 10_240) -> list[str]:
    """Non-fatal lint: returns warnings, never raises."""
    warnings: list[str] = []
    size = len(vn_text.encode("utf-8"))
    # TODO: Consider pre-compressing instead of checking uncompressed size.
    if size > max_bytes:
        warnings.append(
            f"page is {size} bytes uncompressed, over the recommended "
            f"{max_bytes}-byte guideline chunking on LoRa might make it fail to transfer"
        )
    for i, line in enumerate(vn_text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("[") and not LINK_RE.match(stripped):
            warnings.append(f"line {i}: looks like a broken link: {stripped!r}")
    return warnings
