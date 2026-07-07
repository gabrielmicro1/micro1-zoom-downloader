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


class AzureBlobStorage(Storage):
    def __init__(self, container_client, prefix=""):
        self.container_client = container_client
        self.prefix = prefix or ""

    @classmethod
    def from_config(cls, config):
        from azure.storage.blob import BlobServiceClient

        connection_string = getattr(config, "AZURE_STORAGE_CONNECTION_STRING", "")
        account_url = getattr(config, "AZURE_STORAGE_ACCOUNT_URL", "")
        container = getattr(config, "AZURE_CONTAINER", "")
        prefix = getattr(config, "AZURE_PREFIX", "")

        if not container:
            raise ValueError("Azure storage requires AZURE_CONTAINER.")

        if connection_string:
            service = BlobServiceClient.from_connection_string(connection_string)
        elif account_url:
            service = BlobServiceClient(account_url=account_url)
        else:
            raise ValueError(
                "Azure storage requires AZURE_STORAGE_CONNECTION_STRING or AZURE_STORAGE_ACCOUNT_URL."
            )

        return cls(service.get_container_client(container), prefix=prefix)

    def _blob_name(self, path):
        name = str(path).replace("\\", "/")
        name = re.sub(r"^[A-Za-z]:/", "", name)
        name = name.lstrip("/")
        if self.prefix:
            name = self.prefix.rstrip("/") + "/" + name
        return name

    def _blob(self, path):
        return self.container_client.get_blob_client(self._blob_name(path))

    def exists(self, path):
        return self._blob(path).exists()

    def size(self, path):
        blob = self._blob(path)
        if not blob.exists():
            return None
        return blob.get_blob_properties().size

    def free_space(self, path):
        return None

    def remove(self, path):
        blob = self._blob(path)
        if blob.exists():
            blob.delete_blob()

    def save_stream(self, response, dest_path, expected_size, verbose_output, size_tolerance, show_progress=True):
        blob = self._blob(dest_path)
        blob.upload_blob(
            response.iter_content(chunk_size=4 * 1024 * 1024),
            overwrite=True,
            length=expected_size,
        )
        uploaded_size = blob.get_blob_properties().size
        if abs(uploaded_size - expected_size) > size_tolerance:
            blob.delete_blob()
            raise Exception(
                f"Failed to upload file: expected {expected_size} bytes but stored {uploaded_size}."
            )


def create_storage(config):
    backend = getattr(config, "STORAGE_BACKEND", "local")
    if backend == "local":
        return LocalStorage()
    if backend == "azure":
        from storage import AzureBlobStorage  # defined in Task 2
        return AzureBlobStorage.from_config(config)
    raise ValueError(f"Unknown STORAGE_BACKEND {backend!r}. Expected 'local' or 'azure'.")
