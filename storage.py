from __future__ import annotations

import os
import re
import shutil

import utils


class Storage:
    """Destination backend interface."""

    def exists(self, path):
        raise NotImplementedError

    def size(self, path):
        raise NotImplementedError

    def free_space(self, path):
        raise NotImplementedError

    def remove(self, path):
        raise NotImplementedError

    def save_stream(self, response, dest_path, expected_size, verbose_output, size_tolerance, show_progress=True):
        raise NotImplementedError


class LocalStorage(Storage):
    def exists(self, path):
        return os.path.exists(path)

    def size(self, path):
        return os.path.getsize(path) if os.path.exists(path) else None

    def free_space(self, path):
        return shutil.disk_usage(utils.find_existing_parent(path)).free

    def remove(self, path):
        if os.path.exists(path):
            os.remove(path)

    def save_stream(self, response, dest_path, expected_size, verbose_output, size_tolerance, show_progress=True):
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        tmp_path = dest_path + ".tmp"
        utils.download_response_with_progress(
            response, tmp_path, expected_size, verbose_output, size_tolerance, show_progress=show_progress
        )
        os.rename(tmp_path, dest_path)


def create_storage(config):
    backend = getattr(config, "STORAGE_BACKEND", "local")
    if backend == "local":
        return LocalStorage()
    if backend == "azure":
        from storage import AzureBlobStorage  # defined in Task 2
        return AzureBlobStorage.from_config(config)
    raise ValueError(f"Unknown STORAGE_BACKEND {backend!r}. Expected 'local' or 'azure'.")
