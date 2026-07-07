from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import storage as storage_module
from storage import AzureBlobStorage, LocalStorage, create_storage


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


def make_azure(prefix=""):
    container = MagicMock()
    store = AzureBlobStorage(container, prefix=prefix)
    return store, container


def test_azure_blob_name_normalizes_path():
    store, _ = make_azure(prefix="recordings")
    assert store._blob_name(r"C:\Zoom\host\file.mp4") == "recordings/Zoom/host/file.mp4"
    assert store._blob_name("/Zoom/host/file.mp4") == "recordings/Zoom/host/file.mp4"


def test_azure_exists_and_size():
    store, container = make_azure()
    blob = container.get_blob_client.return_value
    blob.exists.return_value = True
    blob.get_blob_properties.return_value = SimpleNamespace(size=42)

    assert store.exists("host/file.mp4") is True
    assert store.size("host/file.mp4") == 42

    blob.exists.return_value = False
    assert store.size("host/missing.mp4") is None


def test_azure_free_space_is_none():
    store, _ = make_azure()
    assert store.free_space("anything") is None


def test_azure_save_stream_uploads_and_validates_size():
    store, container = make_azure()
    blob = container.get_blob_client.return_value
    blob.get_blob_properties.return_value = SimpleNamespace(size=6)

    store.save_stream(FakeResponse([b"abc", b"def"]), "host/file.mp4", expected_size=6,
                      verbose_output=False, size_tolerance=0, show_progress=False)

    assert blob.upload_blob.call_count == 1
    assert blob.delete_blob.call_count == 0


def test_azure_save_stream_deletes_blob_on_mismatch():
    store, container = make_azure()
    blob = container.get_blob_client.return_value
    blob.get_blob_properties.return_value = SimpleNamespace(size=3)

    with pytest.raises(Exception):
        store.save_stream(FakeResponse([b"abc"]), "host/file.mp4", expected_size=6,
                          verbose_output=False, size_tolerance=0, show_progress=False)

    assert blob.delete_blob.call_count == 1
