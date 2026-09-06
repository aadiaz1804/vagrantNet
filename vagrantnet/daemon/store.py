"""In-memory transfer state for the chunk-continuation (CONTINUE) scheme."""

from __future__ import annotations
import time
from dataclasses import dataclass, field

class NoTokenAvailable(RuntimeError):
    pass

@dataclass
class Transfer:
    client_pubkey_prefix: bytes
    request_id: int  # the GET_PAGE/GET_FILE request that started this transfer
    chunks: list[bytes]  # already compressed + chunked, ready to send as-is
    uncompressed_size: int
    compressed: bool
    checksum: int | None
    created_at: float = field(default_factory=time.monotonic)

    @property
    def total_chunks(self) -> int:
        return len(self.chunks)

class TransferStore:
    def __init__(self, ttl_seconds: int = 300, max_total: int = 8, max_per_client: int = 2):
        self._by_token: dict[int, Transfer] = {}
        self.ttl_seconds = ttl_seconds
        self.max_total = max_total
        self.max_per_client = max_per_client

    def _sweep_expired(self) -> None:
        now = time.monotonic()
        expired = [
            tok
            for tok, t in self._by_token.items()
            if now - t.created_at > self.ttl_seconds
        ]
        for tok in expired:
            del self._by_token[tok]

    def _count_for_client(self, prefix: bytes) -> int:
        return sum(
            1 for t in self._by_token.values() if t.client_pubkey_prefix == prefix
        )

    def start(self, client_pubkey_prefix: bytes, transfer: Transfer) -> int:
        """Register a transfer and return its content_token."""
        self._sweep_expired()

        if len(self._by_token) >= self.max_total:
            raise NoTokenAvailable("daemon at max_total in-flight transfers")
        if self._count_for_client(client_pubkey_prefix) >= self.max_per_client:
            raise NoTokenAvailable("client at max_per_client in-flight transfers")

        for token in range(256):
            if token not in self._by_token:
                self._by_token[token] = transfer
                return token
        raise NoTokenAvailable("no free token in 0-255 range")

    def get(self, token: int) -> Transfer | None:
        self._sweep_expired()
        return self._by_token.get(token)

    def find_by_request(self, client_pubkey_prefix: bytes, request_id: int) -> tuple[int, Transfer] | None:
        # See if a client did a retry, get the existing token and continue the transfer instead of starting a new one.
        self._sweep_expired()
        for token, transfer in self._by_token.items():
            if (
                transfer.client_pubkey_prefix == client_pubkey_prefix
                and transfer.request_id == request_id
            ):
                return token, transfer
        return None

    def finish(self, token: int) -> None:
        self._by_token.pop(token, None)
