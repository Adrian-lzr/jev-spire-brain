import pytest

from spirebrain.analysis.replay_blob import ReplayBlobStore


def test_blob_roundtrip_is_content_addressed_and_redacted(tmp_path):
    store = ReplayBlobStore(tmp_path)
    first = store.put({"state": {"screen": "COMBAT"}, "api_key": "secret"})
    second = store.put({"state": {"screen": "COMBAT"}, "api_key": "secret"})
    assert first == second
    assert store.get(first)["api_key"] == "[redacted]"


def test_blob_rejects_hash_mismatch_and_missing(tmp_path):
    store = ReplayBlobStore(tmp_path)
    ref = store.put({"state": "safe"})
    (tmp_path / ref["path"]).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        store.get(ref)
    with pytest.raises(FileNotFoundError):
        store.get({"sha256": "0" * 64})


def test_blob_size_limit_is_enforced(tmp_path):
    store = ReplayBlobStore(tmp_path, max_bytes=256)
    with pytest.raises(ValueError, match="size limit"):
        store.put({"data": "x" * 300})
