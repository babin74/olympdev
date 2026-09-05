from pathlib import Path

import pytest

from olympdev.storage.filestorage import FileStorage

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@pytest.fixture
def storage(tmp_path: Path) -> FileStorage:
    return FileStorage(tmp_path / "storage")


def test_storage_root_path(tmp_path: Path):
    root = tmp_path / "a" / "b" / "storage"
    storage = FileStorage(root)
    assert storage.get_root_path() == root
    assert root.is_dir()


def test_storage_bytes(storage: FileStorage):
    content = b"Hello, world!"
    digest = storage.upload_bytes(content)

    assert storage.exists(digest)
    assert (storage.get_root_path() / digest).is_file()
    assert storage.download_bytes(digest) == content
    assert storage.unlink(digest)
    assert not storage.exists(digest)
    assert not (storage.get_root_path() / digest).exists()


def test_storage_file(storage: FileStorage, test_assets_path: Path):
    file_path = test_assets_path / "sample_file.txt"
    content = file_path.read_bytes()

    digest = storage.upload_file(file_path)
    assert storage.exists(digest)
    assert storage.download_bytes(digest) == content
    assert storage.get_file_digest(file_path) == digest
    assert storage.unlink(digest)
    assert not storage.exists(digest)


def test_storage_dictionary(storage: FileStorage):
    dictionary = {"a": "123", "b": 456, "c": True, "d": {"p": 1, "q": [2, 3, 4]}}
    digest = storage.upload_dictionary(dictionary)
    assert storage.exists(digest)
    assert storage.get_dictionary_digest(dictionary) == digest
    assert storage.download_dictionary(digest) == dictionary
    assert storage.unlink(digest)
    assert not storage.exists(digest)


def test_download_file(storage: FileStorage, tmp_path: Path):
    content = b"payload"
    digest = storage.upload_bytes(content)
    destination = tmp_path / "out" / "nested" / "file.bin"

    assert storage.download_file(digest, destination)
    assert destination.read_bytes() == content


def test_file_and_bytes_share_digest(storage: FileStorage, tmp_path: Path):
    content = b"same-content"
    source = tmp_path / "source.bin"
    source.write_bytes(content)

    bytes_digest = storage.upload_bytes(content)
    file_digest = storage.upload_file(source)
    assert bytes_digest == file_digest
    assert list(storage.get_root_path().iterdir()) == [storage.get_root_path() / bytes_digest]


def test_reupload_is_idempotent(storage: FileStorage):
    content = b"once"
    first = storage.upload_bytes(content)
    second = storage.upload_bytes(content)
    assert first == second
    assert storage.download_bytes(first) == content
    blobs = list(storage.get_root_path().iterdir())
    assert len(blobs) == 1


def test_empty_bytes(storage: FileStorage):
    digest = storage.upload_bytes(b"")
    assert digest == EMPTY_SHA256
    assert storage.download_bytes(digest) == b""


def test_dictionary_key_order_does_not_affect_digest(storage: FileStorage):
    left = {"b": 1, "a": {"z": 0, "y": 1}}
    right = {"a": {"y": 1, "z": 0}, "b": 1}
    assert storage.upload_dictionary(left) == storage.upload_dictionary(right)
    assert storage.get_dictionary_digest(left) == storage.get_dictionary_digest(right)


def test_missing_blob(storage: FileStorage, tmp_path: Path):
    digest = storage.get_bytes_digest(b"never-uploaded")
    destination = tmp_path / "missing.bin"

    assert not storage.exists(digest)
    assert storage.download_bytes(digest) is None
    assert storage.download_dictionary(digest) is None
    assert storage.download_file(digest, destination) is False
    assert not destination.exists()
    assert storage.unlink(digest) is False


def test_invalid_digest_is_rejected(storage: FileStorage, tmp_path: Path):
    destination = tmp_path / "out.bin"
    for digest in ("", "abc", "../" + "a" * 60, "A" * 64, "a" * 63, "a" * 65):
        with pytest.raises(ValueError):
            storage.validate_digest(digest)
        with pytest.raises(ValueError):
            storage.exists(digest)
        with pytest.raises(ValueError):
            storage.download_bytes(digest)
        with pytest.raises(ValueError):
            storage.download_file(digest, destination)
        with pytest.raises(ValueError):
            storage.unlink(digest)


def test_upload_file_does_not_follow_symlink(storage: FileStorage, tmp_path: Path):
    target = tmp_path / "secret.bin"
    target.write_bytes(b"secret")
    link = tmp_path / "link.bin"
    link.symlink_to(target)
    with pytest.raises(OSError):
        storage.upload_file(link)
