""" Check if X to Y machine are message compatible dict/protocol

    python -m tools.protocol_check                  # show this build
    python -m tools.protocol_check --compare <fp>   # check against the other end

Sends nothing and needs no radio.
"""

from __future__ import annotations

import argparse
import hashlib
import sys

sys.path.insert(0, ".")
from vagrantnet.common import chunking, compress, envelope  # noqa: E402
from vagrantnet.common.envelope import (  # noqa: E402
    Request,
    Response,
    StatusCode,
    Subcommand,
)

def dict_digest() -> str:
    # Check hash of the actual dictionary bytes.
    try:
        return hashlib.sha256(compress.DICT_PATH.read_bytes()).hexdigest()[:12]
    except OSError:
        return "none"

def fingerprint() -> str:
    return f"v{envelope.VERSION}-d{compress.active_dict_id()}-{dict_digest()}"

def describe() -> list[tuple[str, str]]:
    return [
        ("fingerprint", fingerprint()),
        ("protocol version", str(envelope.VERSION)),
        ("speaks versions", ", ".join(str(v) for v in sorted(envelope.SUPPORTED_VERSIONS))),
        ("dictionary id", str(compress.active_dict_id())),
        ("dictionary sha256", dict_digest()),
        ("max wire payload", f"{envelope.MAX_SAFE_PAYLOAD} bytes"),
        ("max chunk payload", f"{chunking.MAX_CHUNK_PAYLOAD} bytes"),
        ("max transfer", f"{envelope.MAX_TOTAL_CHUNKS * chunking.MAX_CHUNK_PAYLOAD} bytes"),
    ]

# Healthcheck installation
def self_check() -> list[str]:
    problems: list[str] = []

    req = Request(request_id=4242, client_pubkey_prefix=b"\xaa" * 6,
                  subcommand=Subcommand.GET_PAGE, path="index.vn",
                  dict_id=compress.active_dict_id())
    back = Request.decode(req.encode())
    if (back.request_id, back.path, back.dict_id) != (4242, "index.vn", req.dict_id):
        problems.append("request does not survive its own encode/decode")

    resp = Response(request_id=1, status=StatusCode.OK, chunk_number=0,
                    content_token=3, payload=b"hello", compressed=True,
                    is_final=True, uncompressed_size=5, total_chunks=1,
                    dict_id=compress.active_dict_id())
    if Response.decode(resp.encode()).dict_id != resp.dict_id:
        problems.append("response loses its dictionary id")

    # a peer on another version must raise, not decode as garbage
    frame = bytearray(req.encode())
    frame[0] = (frame[0] & ~0b110000) | (((envelope.VERSION + 1) & 0b11) << 4)
    try:
        Request.decode(bytes(frame))
        problems.append("a foreign protocol version decoded as if it were ours")
    except envelope.UnsupportedVersionError as exc:
        if exc.request_id != 4242:
            problems.append("version refusal cannot address a reply")
    except envelope.EnvelopeError:
        problems.append("foreign version rejected, peer gets no answer")

    sample = b"# Page\n\nvagrantNet over LoRa.\n[Files|files]\n" * 3
    for peer in (compress.active_dict_id(), compress.DICT_NONE, 5):
        blob, used = compress.compress(sample, peer_dict_id=peer)
        try:
            if compress.decompress(blob, dict_id=used) != sample:
                problems.append(f"compressed payload for peer dict {peer} came back wrong")
        except Exception as exc:
            problems.append(f"cannot read back own output for peer dict {peer}: {exc}")

    return problems

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--compare", metavar="FINGERPRINT",
                    help="fingerprint printed by the other end")
    args = ap.parse_args()

    for label, value in describe():
        print(f"  {label:<20} {value}")

    problems = self_check()
    print()
    if problems:
        for p in problems:
            print(f"  BROKEN: {p}")
        print("\nThis build cannot talk reliably to anything. Reinstall before deploying.")
        return 1
    print("  self-check ok")

    if args.compare:
        mine = fingerprint()
        print()
        if args.compare.strip() == mine:
            print(f"  both ends are {mine} COMPATIBLE")
            return 0
        print(f"  MISMATCH: this end {mine}, other end {args.compare.strip()}")
        theirs = args.compare.strip().split("-")
        if theirs and theirs[0] != f"v{envelope.VERSION}":
            print("  different protocol version: CAN'T FETCH")
        else:
            print("  same protocol version, different dictionary")
            print("  fetch, uncompressed, at a worse ratio. Sync the build to fix.")
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
