from pathlib import Path

import pytest

from track_insight.blobstore import LocalBlobStore


def test_put_bytes_is_content_addressed_and_idempotent(workspace_tmp_path: Path) -> None:
    store = LocalBlobStore(workspace_tmp_path)

    first = store.put_bytes("政策材料".encode())
    second = store.put_bytes("政策材料".encode())

    assert first.sha256 == second.sha256
    assert first.storage_key == second.storage_key
    assert first.path == second.path
    assert store.read_bytes(first.storage_key) == "政策材料".encode()


def test_storage_key_cannot_escape_root(workspace_tmp_path: Path) -> None:
    store = LocalBlobStore(workspace_tmp_path)

    with pytest.raises(ValueError, match="escapes blob root"):
        store.path_for("../outside")


def test_invalid_sha256_is_rejected() -> None:
    with pytest.raises(ValueError, match="sha256"):
        LocalBlobStore.storage_key("not-a-hash")
