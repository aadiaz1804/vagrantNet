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
    completed_at: float | None = None  # set once the final chunk has been sent

    @property
    def total_chunks(self) -> int:
        return len(self.chunks)

class TransferStore:
    def __init__(
        self,
        ttl_seconds: int = 300,
        max_total: int = 8,
        max_per_client: int = 2,
        linger_seconds: int = 60,
    ):
        self._by_token: dict[int, Transfer] = {}
        self.ttl_seconds = ttl_seconds
        self.max_total = max_total
        self.max_per_client = max_per_client
        self.linger_seconds = linger_seconds

    def _sweep_expired(self) -> None:
        now = time.monotonic()
        for tok, t in list(self._by_token.items()):
            if t.completed_at is None:
                stale = now - t.created_at > self.ttl_seconds
            else:
                stale = now - t.completed_at > self.linger_seconds
            if stale:
                del self._by_token[tok]

    def _count_for_client(self, prefix: bytes) -> int:
        return sum(
            1
            for t in self._by_token.values()
            if t.client_pubkey_prefix == prefix and t.completed_at is None
        )

    def start(self, client_pubkey_prefix: bytes, transfer: Transfer) -> int:
        """Register a transfer and return its content_token."""
        self._sweep_expired()

        in_flight = sum(1 for t in self._by_token.values() if t.completed_at is None)
        if in_flight >= self.max_total:
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
                transfer.completed_at is None
                and transfer.client_pubkey_prefix == client_pubkey_prefix
                and transfer.request_id == request_id
            ):
                return token, transfer
        return None

    def finish(self, token: int) -> None:
        """Mark a transfer done without dropping it. It lingers, still
        fetchable, so a lost final chunk can be re-requested instead of coming
        back as UNKNOWN_TOKEN -- which the client treats as unrecoverable.
        Lingering transfers stop counting against the in-flight caps."""
        transfer = self._by_token.get(token)
        if transfer is not None and transfer.completed_at is None:
            transfer.completed_at = time.monotonic()
