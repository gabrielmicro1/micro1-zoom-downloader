from types import SimpleNamespace

import pytest

import storage as storage_module
from storage import LocalStorage, create_storage


class FakeResponse:
    def __init__(self, chunks):
        self._chunks = chunks

    def iter_content(self, chunk_size):
        for chunk in self._chunks:
            yield chunk


def test_local_storage_save_size_exists_remove(tmp_path):
    store = LocalStorage()
    dest = str(tmp_path / "sub" / "file.mp4")

    assert store.exists(dest) is False
    assert store.size(dest) is None

    store.save_stream(FakeResponse([b"abc", b"def"]), dest, expected_size=6,
                      verbose_output=False, size_tolerance=0, show_progress=False)

    assert store.exists(dest) is True
    assert store.size(dest) == 6
    assert not (tmp_path / "sub" / "file.mp4.tmp").exists()

    store.remove(dest)
    assert store.exists(dest) is False


def test_local_storage_rejects_size_mismatch_and_cleans_up(tmp_path):
    store = LocalStorage()
    dest = str(tmp_path / "file.mp4")

    with pytest.raises(Exception):
        store.save_stream(FakeResponse([b"abc"]), dest, expected_size=6,
                          verbose_output=False, size_tolerance=0, show_progress=False)

    assert store.exists(dest) is False
    assert not (tmp_path / "file.mp4.tmp").exists()


def test_local_storage_free_space(tmp_path, monkeypatch):
    store = LocalStorage()
    monkeypatch.setattr(storage_module.shutil, "disk_usage",
                        lambda path: SimpleNamespace(free=1234))
    assert store.free_space(str(tmp_path / "nope")) == 1234


def test_create_storage_defaults_to_local():
    assert isinstance(create_storage(SimpleNamespace()), LocalStorage)
    assert isinstance(create_storage(SimpleNamespace(STORAGE_BACKEND="local")), LocalStorage)
