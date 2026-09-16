import hashlib
import json
from typing import Any


def fingerprint(payload: Any, *versions: str) -> str:
    value = {
        "payload": payload,
        "versions": list(versions),
    }
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
