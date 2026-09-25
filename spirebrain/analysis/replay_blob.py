"""Content-addressed, bounded artifacts for deterministic offline replay."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from spirebrain.redaction import redact_value

_SECRET_KEYS = {"api_key", "apikey", "authorization", "access_token", "secret",
                "password", "openrouter_api_key", "openai_api_key"}


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): ("[redacted]" if str(key).lower() in _SECRET_KEYS
                           else _sanitize(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return redact_value(value)


class ReplayBlobStore:
    """Store sanitized JSON blobs by SHA-256; readers verify before decoding."""

    def __init__(self, root: str | Path, *, max_bytes: int = 256_000) -> None:
        self.root = Path(root)
        self.max_bytes = max(256, int(max_bytes))

    def put(self, value: Any) -> dict[str, Any]:
        payload = json.dumps(_sanitize(value), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        if len(payload) > self.max_bytes:
            raise ValueError("replay blob exceeds configured size limit")
        digest = hashlib.sha256(payload).hexdigest()
        target = self.root / f"{digest}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(payload)
        return {"sha256": digest, "bytes": len(payload), "path": target.name}

    def get(self, reference: dict[str, Any]) -> Any:
        digest = str(reference.get("sha256", ""))
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("invalid replay blob digest")
        try:
            payload = (self.root / f"{digest}.json").read_bytes()
        except OSError as exc:
            raise FileNotFoundError("replay blob missing") from exc
        if len(payload) > self.max_bytes:
            raise ValueError("replay blob exceeds configured size limit")
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError("replay blob hash mismatch")
        return json.loads(payload.decode("utf-8"))
