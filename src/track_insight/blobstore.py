import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StoredBlob:
    sha256: str
    storage_key: str
    size_bytes: int
    path: Path


class LocalBlobStore:
    """使用 SHA-256 作为对象键的本地不可变存储。"""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def storage_key(sha256: str) -> str:
        if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise ValueError("sha256 must be a 64-character lowercase hexadecimal string")
        return f"sha256/{sha256[:2]}/{sha256}"

    def path_for(self, storage_key: str) -> Path:
        candidate = (self.root / storage_key).resolve()
        if self.root != candidate and self.root not in candidate.parents:
            raise ValueError("storage key escapes blob root")
        return candidate

    def put_bytes(self, content: bytes) -> StoredBlob:
        digest = hashlib.sha256(content).hexdigest()
        key = self.storage_key(digest)
        target = self.path_for(key)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(prefix=".blob-", dir=target.parent)
            try:
                with os.fdopen(descriptor, "wb") as temporary:
                    temporary.write(content)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temporary_name, target)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        return StoredBlob(digest, key, len(content), target)

    def put_file(self, source: Path) -> StoredBlob:
        return self.put_bytes(source.read_bytes())

    def read_bytes(self, storage_key: str) -> bytes:
        return self.path_for(storage_key).read_bytes()

    def exists(self, storage_key: str) -> bool:
        return self.path_for(storage_key).is_file()
