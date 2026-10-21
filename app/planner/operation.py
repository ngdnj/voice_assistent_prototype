"""Transport-independent identity of a logical creation request."""

import hashlib
import json
from dataclasses import dataclass


class OperationConflictError(ValueError):
    """The same owner/type/operation_id was used with different business data."""


@dataclass(frozen=True)
class CreationOperation:
    operation_id: str
    fingerprint: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.operation_id, str)
            or not self.operation_id.strip()
            or len(self.operation_id) > 200
        ):
            raise ValueError("operation_id must contain 1 to 200 characters")
        if len(self.fingerprint) != 64 or any(c not in "0123456789abcdef" for c in self.fingerprint):
            raise ValueError("fingerprint must be a SHA-256 hex digest")

    @classmethod
    def from_data(cls, operation_id: str, **data: str) -> "CreationOperation":
        canonical = json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        return cls(operation_id, hashlib.sha256(canonical.encode("utf-8")).hexdigest())
