from pathlib import Path

import pytest

from olympdev.settings import Settings
from olympdev.storage import CachingStorage, FileStorage, open_storage


@pytest.fixture
def tiers(tmp_path: Path) -> tuple[FileStorage, FileStorage, CachingStorage]:
    local = FileStorage(tmp_path / "local")
    remote = FileStorage(tmp_path / "remote")
    return local, remote, CachingStorage(local, remote)


def test_read_pulls_through_and_keeps_a_local_copy(tiers):
    local, remote, cache = tiers
    digest = remote.upload_bytes(b"payload")
    assert not local.exists(digest)

    assert cache.download_bytes(digest) == b"payload"
    assert local.exists(digest), "pull-through should populate the local tier"


def test_second_read_is_served_locally(tiers):
    local, remote, cache = tiers
    digest = remote.upload_bytes(b"payload")
    cache.download_bytes(digest)

    # Remove it remotely: a local hit must still answer.
    remote.unlink(digest)
    assert cache.download_bytes(digest) == b"payload"


def test_download_file_pulls_through(tiers, tmp_path: Path):
    _local, remote, cache = tiers
    digest = remote.upload_bytes(b"file-payload")
    target = tmp_path / "out" / "blob.bin"

    assert cache.download_file(digest, target)
    assert target.read_bytes() == b"file-payload"


def test_missing_digest_reports_absent(tiers, tmp_path: Path):
    _local, _remote, cache = tiers
    absent = "0" * 64
    assert not cache.exists(absent)
    assert cache.download_bytes(absent) is None
    assert not cache.download_file(absent, tmp_path / "nothing.bin")


def test_writes_reach_both_tiers(tiers):
    local, remote, cache = tiers
    digest = cache.upload_bytes(b"written")
    assert local.download_bytes(digest) == b"written"
    assert remote.download_bytes(digest) == b"written"


def test_evict_drops_only_the_local_copy(tiers):
    local, remote, cache = tiers
    digest = cache.upload_bytes(b"written")

    assert cache.evict(digest)
    assert not local.exists(digest)
    assert remote.exists(digest)
    assert cache.download_bytes(digest) == b"written"


def test_dictionaries_survive_a_round_trip_through_the_cache(tiers):
    _local, remote, cache = tiers
    digest = remote.upload_dictionary({"job_type": "compile", "n": 1})
    assert cache.download_dictionary(digest) == {"job_type": "compile", "n": 1}


def test_open_storage_is_plain_local_without_a_remote(tmp_path: Path):
    settings = Settings.model_validate({"storage": {"root": str(tmp_path / "s")}})
    assert isinstance(open_storage(settings), FileStorage)


def test_open_storage_wraps_local_in_a_cache_when_remote_is_set(tmp_path: Path):
    settings = Settings.model_validate({
        "storage": {
            "root": str(tmp_path / "local"),
            "remote_root": str(tmp_path / "remote"),
        }
    })
    storage = open_storage(settings)
    assert isinstance(storage, CachingStorage)
    digest = FileStorage(tmp_path / "remote").upload_bytes(b"from-remote")
    assert storage.download_bytes(digest) == b"from-remote"
