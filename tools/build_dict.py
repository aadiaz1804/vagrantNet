"""Build the built-in zstd dictionary for .vn pages in case you want a custom one.
Currently .vn is optimized for known web traffic in english are sent in 147-byte chunks
where every chunk costs a ~0.6s round trip.

Usage:  python tools/build_dict.py [--size 8192]
Writes: vagrantnet/common/vn_dict.bin
"""

from __future__ import annotations

import argparse
import pathlib
import random

import zstandard as zstd

OUT = pathlib.Path(__file__).resolve().parent.parent / "vagrantnet" / "common" / "vn_dict.bin"

# Vocabulary that actually shows up on these pages.
TITLES = ["Welcome", "Index", "Home", "About", "Help", "Files", "Downloads",
          "News", "Status", "Contact", "Blog", "Node Info", "Bulletins",
          "Weather", "Notes", "Links", "Archive", "Guide", "FAQ", "Readme"]
SECTIONS = ["About", "Usage", "Details", "Notes", "Files", "Contact", "Status",
            "How it works", "What is here", "Recent", "Links", "Warning"]
PATHS = ["index.vn", "help.vn", "about.vn", "files", "news.vn", "status.vn",
         "guide.vn", "faq.vn", "notes.vn", "archive.vn", "contact.vn", "readme.vn"]
WORDS = """the a an and or but for with from this that these those you your
node page pages file files server client mesh network radio link links line
over under about after before while when where which what who how why is are
was were be been being have has had do does did can could will would should
may might must not no yes all any some more most other same new old first last
time day days week month year hour minute second local remote public private
open close read write send receive request reply message data byte bytes size
chunk chunks compress compressed transfer download upload connect connected
disconnect retry timeout error ok status info warning note please see also here
there back next previous home index help about contact welcome hello thanks
vagrantNet MeshCore LoRa mesh companion firmware repeater room bulletin board
offline internet without bandwidth minimal text only plain simple small fast
slow signal antenna battery solar power range hop hops path route
""".split()

def sentence(rng: random.Random, n: int = 12) -> str:
    words = [rng.choice(WORDS) for _ in range(rng.randint(6, n))]
    return " ".join(words).capitalize() + "."

def make_page(rng: random.Random) -> bytes:
    out = [f"# {rng.choice(TITLES)}", ""]
    for _ in range(rng.randint(1, 3)):
        out.append(sentence(rng))
    out.append("")
    for _ in range(rng.randint(1, 3)):
        out.append(f"## {rng.choice(SECTIONS)}")
        out.append("")
        for _ in range(rng.randint(1, 4)):
            out.append(sentence(rng))
        out.append("")
        for _ in range(rng.randint(0, 3)):
            out.append(f"[{rng.choice(TITLES)}|{rng.choice(PATHS)}]")
        out.append("")
    if rng.random() < 0.4:
        out.append(f"> {sentence(rng)}")
        out.append("")
    if rng.random() < 0.5:
        out.append("---")
    return ("\n".join(out) + "\n").encode("utf-8")

def make_listing(rng: random.Random) -> bytes:
    # Make dummy listings
    lines = ["# Available Pages", ""]
    for _ in range(rng.randint(1, 12)):
        path = rng.choice(PATHS)
        lines.append(f"[{path.split('.')[0]}|{path}]")
    return ("\n".join(lines) + "\n").encode("utf-8")

def corpus(rng: random.Random) -> list[bytes]:
    samples = [make_page(rng) for _ in range(240)]
    samples += [make_listing(rng) for _ in range(40)]
    # Pull as a minimum the repo examples
    for path in (pathlib.Path(__file__).parent.parent / "examples").glob("*.vn"):
        samples += [path.read_bytes()] * 4
    return samples

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=8192)
    ap.add_argument("--seed", type=int, default=20260906)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    samples = corpus(rng)
    zdict = zstd.train_dictionary(args.size, samples)
    OUT.write_bytes(zdict.as_bytes())
    print(f"wrote {OUT} ({len(zdict.as_bytes())} bytes, dict_id={zdict.dict_id()}) "
          f"from {len(samples)} samples")

if __name__ == "__main__":
    main()
