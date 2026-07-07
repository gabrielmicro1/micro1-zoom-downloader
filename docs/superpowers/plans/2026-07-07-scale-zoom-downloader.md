# Scale Zoom Downloader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `micro1-zoom-downloader` handle ~100k recordings by parallelizing downloads, pooling connections, rate-limiting the Zoom API, and supporting a pluggable local-or-Azure destination, plus an all-time date convenience.

**Architecture:** Approach B (minimal-change parallelization). Keep the existing "scan the whole account into an inventory, then download" shape and all modes. Introduce a `Storage` abstraction (`LocalStorage` / `AzureBlobStorage`) that every filesystem touchpoint routes through; parallelize the download phase with a thread pool; harden `zoom_client` with a pooled `requests.Session`, thread-safe token refresh, a shared rate limiter, and exponential backoff.

**Tech Stack:** Python 3.11, `requests`, `tqdm`, `colorama`, `azure-storage-blob` (lazy-imported), `pytest` + `responses`.

## Global Constraints

- Python 3.11 (`.python-version` = 3.11.7).
- `azure-storage-blob` MUST be imported lazily (inside the Azure class / factory) so local-only users don't need it installed.
- Preserve all existing modes: `download`, `estimate`, `retry_not_ready`, `delete_bulk`, `delete_one`.
- Preserve all deletion safety gates (dry-run default, `CONFIRM_DELETE`, permanent-delete triple gate) unchanged.
- `STORAGE_BACKEND` defaults to `"local"` (backwards compatible). New config keys are read via `getattr`/`config_value` with defaults so pre-existing `config.py` files keep working.
- Defaults: `CONCURRENCY = 8`, `REQUESTS_PER_SECOND = 8`, `MAX_RATE_LIMIT_RETRIES = 8`, `ALL_TIME = False`, `ALL_TIME_START = 2012-01-01`.
- Secrets (Azure connection strings, SAS tokens, bearer tokens) MUST be redacted in error output.
- Run the full suite with `python3 -m pytest` from the repo root.

## File Structure

- **Create `storage.py`** — `Storage` interface, `LocalStorage`, `AzureBlobStorage`, `create_storage(config)` factory. Owns every destination read/write/existence/size/free-space operation.
- **Modify `utils.py`** — add `show_progress` param to `download_response_with_progress`; extend `redact_sensitive_text` for Azure secrets.
- **Modify `zoom_client.py`** — pooled `Session`, thread-safe token, `RateLimiter`, exponential backoff.
- **Modify `zoom_batch_downloader.py`** — thread storage through inventory/download/space-check; parallelize `download_inventory`; all-time date range; pass new client config.
- **Modify `config_template.py`** — new config keys.
- **Modify `requirements.txt`** — add `azure-storage-blob`.
- **Create `tests/test_storage.py`** — LocalStorage + AzureBlobStorage (mocked).
- **Modify `tests/test_zoom_client.py`** — pooling/token/backoff/rate-limiter tests + update existing tests for new params.
- **Modify `tests/test_zoom_batch_downloader.py`** — concurrent download, all-time range, updated space-check signature.

---

## Task 1: Storage interface + LocalStorage

**Files:**
- Create: `storage.py`
- Modify: `utils.py` (add `show_progress` to `download_response_with_progress`)
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: `utils.download_response_with_progress`, `utils.find_existing_parent`.
- Produces:
  - `Storage` base class.
  - `LocalStorage()` with `exists(path)->bool`, `size(path)->int|None`, `free_space(path)->int|None`, `remove(path)->None`, `save_stream(response, dest_path, expected_size, verbose_output, size_tolerance, show_progress=True)->None` (raises on failure).
  - `create_storage(config)->Storage` (returns `LocalStorage` for `STORAGE_BACKEND` in {absent,`"local"`}).

- [ ] **Step 1: Add `show_progress` to `utils.download_response_with_progress`**

In `utils.py`, change the signature and skip the tqdm bar when `show_progress` is False. Replace the function body's write loop so it works with or without the bar:

```python
def download_response_with_progress(response, output_path, expected_size, verbose_output, size_tolerance, show_progress=True):
	class download_progress_bar(tqdm):
		def __init__(self, expected_size=None, dynamic_ncols=True):
			r_bar = '| {n_fmt}{unit}/{total_fmt}{unit} [{elapsed}<{remaining}, {rate_fmt}{postfix}]'
			format = '{l_bar}{bar}' + r_bar

			tqdm.__init__(
				self, total=expected_size, unit='B', unit_divisor=1024, unit_scale=True, miniters=1,
				dynamic_ncols=dynamic_ncols, bar_format=format
			)

		def update_to(self, b=1, bsize=1, tsize=None):
			if tsize is not None:
				self.total = tsize
			self.update(b * bsize - self.n)

	try:
		with open(output_path, "wb") as output_file:
			bar = download_progress_bar(expected_size=expected_size) if show_progress else None
			try:
				for chunk in response.iter_content(chunk_size=1024 * 1024):
					if not chunk:
						continue
					output_file.write(chunk)
					if bar is not None:
						bar.update(len(chunk))
			finally:
				if bar is not None:
					bar.close()

		file_size = os.path.getsize(output_path)
		if abs(file_size - expected_size) > size_tolerance:
			if verbose_output:
				print_dim_red(
					f'Size mismatch: Expected {expected_size} bytes but got {file_size}. '
					f'Size difference: {size_to_string(abs(file_size - expected_size))}.\n'
					f'You might want to increase FILE_SIZE_MISMATCH_TOLERANCE in config.py'
				)
			raise Exception(
				"Failed to download file."
				f'{"" if verbose_output else " Enable verbose output for more details."}'
			)

		if file_size != expected_size and verbose_output:
			print_dim_red(
				f'Size mismatch within tolerance: Expected {expected_size} bytes but got {file_size}. '
				f'Size difference: {size_to_string(abs(file_size - expected_size))}.'
			)
	except:
		try:
			os.remove(output_path)
		except OSError:
			pass
		raise
```

- [ ] **Step 2: Write the failing test for `LocalStorage`**

Create `tests/test_storage.py`:

```python
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_storage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'storage'`.

- [ ] **Step 4: Implement `storage.py` (base + LocalStorage + factory)**

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_storage.py tests/test_zoom_batch_downloader.py -v`
Expected: PASS (new storage tests pass; existing downloader tests still pass because `download_response_with_progress` keeps `show_progress=True` default).

- [ ] **Step 6: Commit**

```bash
git add storage.py utils.py tests/test_storage.py
git commit -m "feat: add Storage interface and LocalStorage backend

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: AzureBlobStorage backend

**Files:**
- Modify: `storage.py` (add `AzureBlobStorage`)
- Modify: `requirements.txt`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: `azure.storage.blob.BlobServiceClient` (lazy), config attributes `AZURE_STORAGE_CONNECTION_STRING` or `AZURE_STORAGE_ACCOUNT_URL`, `AZURE_CONTAINER`, `AZURE_PREFIX`.
- Produces: `AzureBlobStorage(container_client, prefix="")` implementing the `Storage` interface; `AzureBlobStorage.from_config(config)`; blob-name mapping via `_blob_name(path)`. `free_space` returns `None`.

- [ ] **Step 1: Write the failing tests for `AzureBlobStorage`**

Append to `tests/test_storage.py`:

```python
from unittest.mock import MagicMock

from storage import AzureBlobStorage


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_storage.py -k azure -v`
Expected: FAIL with `ImportError: cannot import name 'AzureBlobStorage'`.

- [ ] **Step 3: Implement `AzureBlobStorage` in `storage.py`**

Add above `create_storage`:

```python
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
```

- [ ] **Step 4: Add the dependency**

Append to `requirements.txt`:

```
azure-storage-blob>=12.19.0
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_storage.py -v`
Expected: PASS (all storage tests, including Azure, pass without a live Azure account).

- [ ] **Step 6: Commit**

```bash
git add storage.py requirements.txt tests/test_storage.py
git commit -m "feat: add AzureBlobStorage backend

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Route the downloader through the Storage backend

**Files:**
- Modify: `zoom_batch_downloader.py`
- Test: `tests/test_zoom_batch_downloader.py`

**Interfaces:**
- Consumes: `storage.create_storage`, `storage.LocalStorage`, and the `Storage` methods from Tasks 1–2.
- Produces (new signatures later tasks rely on):
  - `build_inventory(config, client, conn, from_date, to_date, storage)`
  - `build_inventory_from_meetings(config, meetings, host_folder=None, storage=None)` (defaults to `LocalStorage()` when `storage is None`)
  - `make_inventory_item(config, meeting, recording_file, host_folder, storage)`
  - `check_destination_space(config, storage, inventory, fail_on_shortage)`
  - `get_space_status(config, inventory, free_bytes)`
  - `download_recording_item(config, client, item, storage)`
  - `download_with_retry(client, storage, download_url, dest_path, file_size, verbose_output, size_tolerance, show_progress, max_retries=10)`
  - `download_inventory(config, client, inventory, storage)` (still serial in this task)
  - `run_retry_not_ready(config, client, conn, storage)`

- [ ] **Step 1: Update the existing tests to the new signatures (they will fail first)**

In `tests/test_zoom_batch_downloader.py`:

Add import at top:

```python
from storage import LocalStorage
```

Add a fake space storage helper near the top:

```python
class FakeSpaceStorage:
    def __init__(self, free):
        self._free = free

    def free_space(self, path):
        return self._free
```

Replace the two space-check tests' final calls to use the new signature:

In `test_estimate_space_check_reports_without_failing`, remove the `monkeypatch.setattr(zbd.shutil, ...)` line and change the call to:

```python
    status = zbd.check_destination_space(
        config, FakeSpaceStorage(25), inventory, fail_on_shortage=False
    )
```

In `test_download_space_check_fails_before_download_when_short`, remove the `monkeypatch.setattr(zbd.shutil, ...)` line and change the call to:

```python
    with pytest.raises(RuntimeError, match="Not enough free space"):
        zbd.check_destination_space(
            config, FakeSpaceStorage(25), inventory, fail_on_shortage=True
        )
```

Update `test_retry_not_ready_downloads_and_clears_successful_uuid` to pass storage:

```python
    zbd.run_retry_not_ready(config, FakeDownloadClient(meeting), conn, LocalStorage())
```

(The `test_inventory_calculates_sizes_and_existing_files` and delete tests keep calling `build_inventory_from_meetings(config, [...], host_folder=...)` unchanged — `storage` defaults to `LocalStorage()`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_zoom_batch_downloader.py -v`
Expected: FAIL (`check_destination_space` / `run_retry_not_ready` got unexpected/positional args) — proving the tests now target the new API.

- [ ] **Step 3: Implement the storage wiring in `zoom_batch_downloader.py`**

Add import near the top:

```python
from storage import create_storage, LocalStorage
```

Update `run` to build storage and thread it through:

```python
def run(config, client=None, db_path="meetings.db", storage=None):
    mode = resolve_mode(config)
    config.OUTPUT_PATH = utils.prepend_path_on_windows(config.OUTPUT_PATH)
    client = client or create_client(config)
    storage = storage or create_storage(config)

    if mode == "delete_one":
        delete_one(config, client)
        return

    conn = ensure_meetings_db(db_path)
    try:
        if mode == "retry_not_ready":
            run_retry_not_ready(config, client, conn, storage)
            return

        print_filter_warnings(config)
        from_date, to_date = get_date_range(config)
        inventory = build_inventory(config, client, conn, from_date, to_date, storage)
        print_inventory_summary(inventory)

        if mode == "estimate":
            check_destination_space(config, storage, inventory, fail_on_shortage=False)
            return

        if mode == "download":
            check_destination_space(
                config, storage, inventory,
                fail_on_shortage=config_value(config, "FAIL_IF_NOT_ENOUGH_SPACE", True),
            )
            summary = download_inventory(config, client, inventory, storage)
            print_download_summary(summary)
            return

        if mode == "delete_bulk":
            summary = delete_inventory(config, client, inventory)
            print_delete_summary(summary)
            return
    finally:
        conn.close()
```

Update `run_retry_not_ready` signature and calls:

```python
def run_retry_not_ready(config, client, conn, storage):
    meeting_uuids = read_logged_meeting_uuids(conn)
    if not meeting_uuids:
        utils.print_bright("No not-ready meeting UUIDs are logged.")
        return

    meetings = get_meetings(
        client, meeting_uuids, conn=conn, log_failures=True, clear_success=False
    )
    inventory = build_inventory_from_meetings(config, meetings, storage=storage)
    print_inventory_summary(inventory)
    check_destination_space(
        config, storage, inventory,
        fail_on_shortage=config_value(config, "FAIL_IF_NOT_ENOUGH_SPACE", True),
    )
    summary = download_inventory(config, client, inventory, storage)

    fetched_uuids = {meeting["_requested_uuid"] for meeting in meetings}
    for meeting_uuid in fetched_uuids - summary.failed_meeting_uuids:
        clear_meeting_uuid(conn, meeting_uuid)

    print_download_summary(summary)
```

Update `build_inventory` to thread storage:

```python
def build_inventory(config, client, conn, from_date, to_date, storage):
    all_files = []
    meeting_count = 0
    skipped_missing_size_count = 0

    for user_email, user_name in get_users(config, client):
        user_description = get_user_description(user_email, user_name)
        utils.print_bright(
            f"Scanning recordings from user {user_description} - Starting at {date_to_str(from_date)} "
            f"and up to {date_to_str(to_date)} (inclusive)."
        )

        meeting_uuids = get_meeting_uuids(client, user_email, from_date, to_date)
        meetings = get_meetings(client, meeting_uuids, conn=conn)
        inventory = build_inventory_from_meetings(
            config, meetings, host_folder=get_user_host_folder(config, user_email), storage=storage
        )
        all_files.extend(inventory.files)
        meeting_count += inventory.meeting_count
        skipped_missing_size_count += inventory.skipped_missing_size_count
        utils.print_bright(
            "######################################################################"
        )
        print()

    return RecordingInventory(
        files=all_files,
        meeting_count=meeting_count,
        skipped_missing_size_count=skipped_missing_size_count,
    )
```

Update `build_inventory_from_meetings` and `make_inventory_item`:

```python
def build_inventory_from_meetings(config, meetings, host_folder=None, storage=None):
    if storage is None:
        storage = LocalStorage()
    files = []
    skipped_missing_size_count = 0

    for meeting in meetings:
        if not meeting_matches_topic_filter(config, meeting):
            continue

        folder = host_folder or get_meeting_host_folder(config, meeting)
        recording_files = meeting.get("recording_files") or []
        participant_audio_files = (
            (meeting.get("participant_audio_files") or [])
            if config.INCLUDE_PARTICIPANT_AUDIO
            else []
        )

        for recording_file in recording_files + participant_audio_files:
            if "file_size" not in recording_file or "id" not in recording_file:
                skipped_missing_size_count += 1
                continue

            if not recording_file_matches_type_filter(config, recording_file):
                continue

            files.append(make_inventory_item(config, meeting, recording_file, folder, storage))

    matched_meetings = {item.meeting_uuid for item in files}
    return RecordingInventory(
        files=files,
        meeting_count=len(matched_meetings),
        skipped_missing_size_count=skipped_missing_size_count,
    )
```

In `make_inventory_item`, replace the `local_exists` / `local_size_matches` block:

```python
def make_inventory_item(config, meeting, recording_file, host_folder, storage):
    topic = utils.slugify(meeting.get("topic", "untitled"))
    recording_name = utils.slugify(
        f'{topic}__{recording_file.get("recording_start", "unknown-start")}'
    )
    file_name = build_file_name(recording_file, recording_name)
    destination_path = create_path(
        config, host_folder, file_name, topic, recording_name, create_dirs=False
    )
    file_size = int(recording_file["file_size"])
    existing_size = storage.size(destination_path)
    local_exists = existing_size is not None
    local_size_matches = (
        local_exists
        and abs(existing_size - file_size) <= config.FILE_SIZE_MISMATCH_TOLERANCE
    )

    return RecordingItem(
        meeting_uuid=meeting.get("_requested_uuid") or meeting.get("uuid") or str(meeting.get("id")),
        topic=topic,
        recording_id=recording_file["id"],
        file_type=recording_file.get("file_type", ""),
        file_size=file_size,
        download_url=recording_file.get("download_url", ""),
        destination_path=destination_path,
        file_name=file_name,
        recording_name=recording_name,
        recording_type=recording_file.get("recording_type", ""),
        local_exists=local_exists,
        local_size_matches=local_size_matches,
        source_kind="participant_audio"
        if recording_file.get("participant_email")
        else "recording_file",
    )
```

Update `check_destination_space` and `get_space_status`:

```python
def check_destination_space(config, storage, inventory, fail_on_shortage):
    free_bytes = storage.free_space(config.OUTPUT_PATH)
    if free_bytes is None:
        utils.print_bright("Object storage destination; skipping disk-space check.")
        print()
        return None

    status = get_space_status(config, inventory, free_bytes)
    print_destination_space_status(status)
    if fail_on_shortage and not status.has_enough_space:
        raise RuntimeError(
            "Not enough free space at destination. "
            f"Need {utils.size_to_string(status.required_bytes)} including reserve, "
            f"but only {utils.size_to_string(status.free_bytes)} is available."
        )
    return status


def get_space_status(config, inventory, free_bytes):
    reserve_bytes = config.MINIMUM_FREE_DISK
    additional_bytes = inventory.additional_size
    required_bytes = additional_bytes + reserve_bytes
    return SpaceStatus(
        usage_path=config.OUTPUT_PATH,
        free_bytes=free_bytes,
        required_bytes=required_bytes,
        additional_bytes=additional_bytes,
        reserve_bytes=reserve_bytes,
    )
```

Update `download_inventory` (still serial here), `download_recording_item`, and `download_with_retry`:

```python
def download_inventory(config, client, inventory, storage):
    summary = DownloadSummary()

    for item in inventory.files:
        try:
            result = download_recording_item(config, client, item, storage)
        except Exception as error:
            summary.failed_count += 1
            summary.failed_meeting_uuids.add(item.meeting_uuid)
            utils.print_dim_red(
                f"Download failed for {item.file_name}: {utils.redact_sensitive_text(error)}"
            )
            continue

        if result:
            summary.downloaded_count += 1
            summary.downloaded_size += item.file_size
        else:
            summary.skipped_count += 1

    return summary


def download_recording_item(config, client, item, storage, show_progress=True):
    if config.VERBOSE_OUTPUT:
        print()
        utils.print_dim(f"URL: {utils.redact_url(item.download_url)}")

    if item.local_size_matches:
        utils.print_dim(f"Skipping existing file: {item.file_name}")
        return False

    if item.local_exists:
        utils.print_dim_red(f"Deleting corrupt file: {item.file_name}")
        storage.remove(item.destination_path)

    utils.print_bright(f"Downloading: {item.file_name}")
    if storage.free_space(config.OUTPUT_PATH) is not None:
        utils.wait_for_disk_space(
            item.file_size, config.OUTPUT_PATH, config.MINIMUM_FREE_DISK, interval=5
        )

    if download_with_retry(
        client,
        storage,
        item.download_url,
        item.destination_path,
        item.file_size,
        config.VERBOSE_OUTPUT,
        config.FILE_SIZE_MISMATCH_TOLERANCE,
        show_progress,
    ):
        return True

    raise RuntimeError("Max retries reached, download failed.")


def download_with_retry(
    client,
    storage,
    download_url,
    dest_path,
    file_size,
    verbose_output,
    file_size_mismatch_tolerance,
    show_progress,
    max_retries=10,
):
    retries = 0
    while retries < max_retries:
        try:
            response = client.request("GET", download_url, stream=True)
            storage.save_stream(
                response,
                dest_path,
                file_size,
                verbose_output,
                file_size_mismatch_tolerance,
                show_progress=show_progress,
            )
            return True
        except Exception as error:
            retries += 1
            print(f"Download failed: {utils.redact_sensitive_text(error)}")
            if retries < max_retries:
                print(f"Retrying ({retries}/{max_retries}) in 5 seconds...")
                time.sleep(5)
    return False
```

- [ ] **Step 4: Run the full suite to verify it passes**

Run: `python3 -m pytest -v`
Expected: PASS (all storage + downloader + client tests green with the new signatures).

- [ ] **Step 5: Commit**

```bash
git add zoom_batch_downloader.py tests/test_zoom_batch_downloader.py
git commit -m "refactor: route downloader through Storage backend

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Parallel downloads

**Files:**
- Modify: `zoom_batch_downloader.py` (`download_inventory` + imports)
- Test: `tests/test_zoom_batch_downloader.py`

**Interfaces:**
- Consumes: `download_recording_item(config, client, item, storage, show_progress)`, `config_value`.
- Produces: `download_inventory(config, client, inventory, storage)` that runs downloads via `ThreadPoolExecutor(max_workers=config_value(config, "CONCURRENCY", 8))`, aggregating a thread-safe `DownloadSummary`. Aggregate progress bar; per-file bars only when workers == 1.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_zoom_batch_downloader.py`:

```python
import threading


class RecordingDownloadClient:
    """Records concurrent download calls and serves fixed content."""

    def __init__(self):
        self.threads = set()
        self._lock = threading.Lock()

    def request(self, method, url, stream=False):
        with self._lock:
            self.threads.add(threading.current_thread().name)
        return FakeDownloadResponse()


def make_item(tmp_path, index, size=6, fail=False):
    url = "https://download.example.com/fail" if fail else "https://download.example.com/ok"
    return zbd.RecordingItem(
        meeting_uuid=f"m{index}",
        topic="topic",
        recording_id=f"r{index}",
        file_type="MP4",
        file_size=size,
        download_url=url,
        destination_path=str(tmp_path / f"file{index}.mp4"),
        file_name=f"file{index}.mp4",
        recording_name="topic",
        recording_type="",
        local_exists=False,
        local_size_matches=False,
        source_kind="recording_file",
    )


def test_download_inventory_runs_concurrently_and_aggregates(tmp_path):
    config = make_config(tmp_path, CONCURRENCY=4)
    inventory = zbd.RecordingInventory(
        files=[make_item(tmp_path, i) for i in range(6)], meeting_count=6
    )
    client = RecordingDownloadClient()

    summary = zbd.download_inventory(config, client, inventory, LocalStorage())

    assert summary.downloaded_count == 6
    assert summary.downloaded_size == 36
    assert summary.failed_count == 0
    assert len(list(tmp_path.glob("file*.mp4"))) == 6
    assert len(client.threads) > 1  # actually used multiple workers


def test_download_inventory_counts_failures_without_aborting(tmp_path, monkeypatch):
    config = make_config(tmp_path, CONCURRENCY=2)
    inventory = zbd.RecordingInventory(
        files=[make_item(tmp_path, 0), make_item(tmp_path, 1)], meeting_count=2
    )
    client = RecordingDownloadClient()

    original = zbd.download_recording_item

    def flaky(config, client, item, storage, show_progress=True):
        if item.recording_id == "r0":
            raise RuntimeError("boom")
        return original(config, client, item, storage, show_progress=show_progress)

    monkeypatch.setattr(zbd, "download_recording_item", flaky)

    summary = zbd.download_inventory(config, client, inventory, LocalStorage())

    assert summary.failed_count == 1
    assert summary.downloaded_count == 1
    assert "m0" in summary.failed_meeting_uuids
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_zoom_batch_downloader.py -k "concurrently or counts_failures" -v`
Expected: FAIL (`test_download_inventory_runs_concurrently_and_aggregates` fails the `len(client.threads) > 1` assertion because downloads are still serial).

- [ ] **Step 3: Implement the thread pool**

Add imports at the top of `zoom_batch_downloader.py`:

```python
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
```

Replace `download_inventory`:

```python
def download_inventory(config, client, inventory, storage):
    summary = DownloadSummary()
    workers = max(1, int(config_value(config, "CONCURRENCY", 8)))
    show_progress = workers == 1

    def worker(item):
        try:
            result = download_recording_item(config, client, item, storage, show_progress)
        except Exception as error:
            return ("failed", item, error)
        return ("downloaded" if result else "skipped", item, None)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(worker, item) for item in inventory.files]
        for future in utils.percentage_tqdm(as_completed(futures), total=len(futures)):
            status, item, error = future.result()
            if status == "downloaded":
                summary.downloaded_count += 1
                summary.downloaded_size += item.file_size
            elif status == "skipped":
                summary.skipped_count += 1
            else:
                summary.failed_count += 1
                summary.failed_meeting_uuids.add(item.meeting_uuid)
                utils.print_dim_red(
                    f"Download failed for {item.file_name}: {utils.redact_sensitive_text(error)}"
                )

    return summary
```

(`threading` is imported for the test helper's use and future-proofing; aggregation itself is single-threaded in the main loop, so no lock is required.)

- [ ] **Step 4: Run the full suite to verify it passes**

Run: `python3 -m pytest -v`
Expected: PASS (concurrent download tests pass; `test_retry_not_ready...` still passes — it now runs through the pool).

- [ ] **Step 5: Commit**

```bash
git add zoom_batch_downloader.py tests/test_zoom_batch_downloader.py
git commit -m "feat: parallelize downloads with a configurable thread pool

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Zoom client — connection pooling + thread-safe token

**Files:**
- Modify: `zoom_client.py`
- Test: `tests/test_zoom_client.py`

**Interfaces:**
- Produces: `zoom_client(..., concurrency=8, ...)` that uses a shared `requests.Session` with an `HTTPAdapter` sized to `concurrency`, and refreshes the OAuth token under a lock (single-flight on concurrent 401s). Existing `get`/`delete`/`request`/`paginate`/`fetch_token` behavior preserved.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_zoom_client.py`:

```python
from zoom_client import zoom_client


def test_client_uses_a_single_pooled_session():
    client = zoom_client("acct", "client", "secret", concurrency=5)
    adapter = client.session.get_adapter("https://api.zoom.us/")
    assert adapter._pool_maxsize == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_zoom_client.py::test_client_uses_a_single_pooled_session -v`
Expected: FAIL (`zoom_client` has no `session` attribute / unexpected `concurrency` kwarg).

- [ ] **Step 3: Implement pooling + token lock in `zoom_client.py`**

Add imports:

```python
import threading

from requests.adapters import HTTPAdapter
```

Update `__init__` to add `concurrency` and build a pooled session + token lock:

```python
    def __init__(
        self,
        account_id: str,
        client_id: str,
        client_secret: str,
        PAGE_SIZE: int = 300,
        timeout=(10, 120),
        max_rate_limit_retries: int = 8,
        concurrency: int = 8,
        sleep=time.sleep,
    ):
        self.account_id = account_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.PAGE_SIZE = PAGE_SIZE
        self.timeout = timeout
        self.max_rate_limit_retries = max_rate_limit_retries
        self.sleep = sleep
        self.cached_token = None
        self.token_lock = threading.Lock()

        self.session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=concurrency, pool_maxsize=concurrency, max_retries=0
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
```

Add token helpers and update `_request_with_token`, `_send`, and `fetch_token` to use the session:

```python
    def _get_token(self):
        with self.token_lock:
            if self.cached_token is None:
                self.cached_token = self.fetch_token()
            return self.cached_token

    def _refresh_token(self, stale_token):
        with self.token_lock:
            if self.cached_token == stale_token:
                self.cached_token = self.fetch_token()
            return self.cached_token

    def _request_with_token(self, method, url, params=None, json=None, stream=False):
        token = self._get_token()
        response = self._send(method, url, token, params=params, json=json, stream=stream)

        if response.status_code == 401:
            token = self._refresh_token(token)
            response = self._send(method, url, token, params=params, json=json, stream=stream)

        return response

    def _send(self, method, url, token, params=None, json=None, stream=False):
        return self.session.request(
            method,
            url,
            headers=self.get_headers(token),
            params=params,
            json=json,
            stream=stream,
            timeout=self.timeout,
        )
```

In `fetch_token`, change `requests.post(` to `self.session.post(`.

- [ ] **Step 4: Run the client tests to verify they pass**

Run: `python3 -m pytest tests/test_zoom_client.py -v`
Expected: PASS (new pooling test passes; `test_request_refreshes_token_once_after_401` still passes with exactly two token POSTs and `["Bearer token-one", "Bearer token-two"]`).

- [ ] **Step 5: Commit**

```bash
git add zoom_client.py tests/test_zoom_client.py
git commit -m "feat: pool connections and make token refresh thread-safe

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Zoom client — shared rate limiter + exponential backoff

**Files:**
- Modify: `zoom_client.py`
- Test: `tests/test_zoom_client.py`

**Interfaces:**
- Consumes: pooled session + token from Task 5.
- Produces:
  - `RateLimiter(rate_per_second, sleep=time.sleep, monotonic=time.monotonic)` with `acquire()`.
  - `zoom_client(..., requests_per_second=8, backoff_base=1.0, backoff_jitter=None, monotonic=time.monotonic, ...)` that acquires the limiter before each HTTP call and, on 429, waits `max(retry_after, backoff_base * 2**attempt) + jitter` up to `max_rate_limit_retries`, honoring `Retry-After`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_zoom_client.py`:

```python
import responses

from zoom_client import RateLimiter, zoom_client


def test_rate_limiter_spaces_calls():
    now = [0.0]
    sleeps = []
    limiter = RateLimiter(2, sleep=sleeps.append, monotonic=lambda: now[0])

    limiter.acquire()   # first call: no wait
    limiter.acquire()   # second call at same time: must wait 0.5s

    assert sleeps == [0.5]


@responses.activate
def test_backoff_uses_exponential_when_no_retry_after():
    sleeps = []
    responses.add(responses.POST, "https://api.zoom.us/oauth/token",
                  json={"access_token": "t"}, status=200)
    responses.add(responses.GET, "https://api.zoom.us/v2/users/me", status=429,
                  json={"message": "slow down"})
    responses.add(responses.GET, "https://api.zoom.us/v2/users/me", status=429,
                  json={"message": "slow down"})
    responses.add(responses.GET, "https://api.zoom.us/v2/users/me", status=200,
                  json={"id": "me"})

    client = zoom_client("acct", "client", "secret", sleep=sleeps.append,
                         requests_per_second=None, backoff_base=1.0,
                         backoff_jitter=lambda: 0)

    assert client.get("/users/me") == {"id": "me"}
    assert sleeps == [1.0, 2.0]  # 2**0, 2**1
```

Also update the two existing client tests so the rate limiter and jitter don't perturb their `sleeps` assertions:

In `test_request_honors_retry_after_for_rate_limit`, change the client construction to:

```python
    client = zoom_client("acct", "client", "secret", sleep=sleeps.append,
                         requests_per_second=None, backoff_base=1.0,
                         backoff_jitter=lambda: 0)
```

(That test's `sleeps == [2]` still holds: `max(2, 1*2**0) + 0 == 2`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_zoom_client.py -k "rate_limiter or backoff" -v`
Expected: FAIL (`cannot import name 'RateLimiter'`).

- [ ] **Step 3: Implement `RateLimiter` and backoff**

Add the class near the top of `zoom_client.py` (after imports):

```python
class RateLimiter:
    def __init__(self, rate_per_second, sleep=time.sleep, monotonic=time.monotonic):
        self.min_interval = 1.0 / rate_per_second if rate_per_second else 0.0
        self.sleep = sleep
        self.monotonic = monotonic
        self.lock = threading.Lock()
        self.next_time = None

    def acquire(self):
        if not self.min_interval:
            return
        with self.lock:
            now = self.monotonic()
            if self.next_time is None or now >= self.next_time:
                self.next_time = now + self.min_interval
                return
            wait = self.next_time - now
            self.next_time += self.min_interval
        self.sleep(wait)
```

Extend `__init__` with the new params and build the limiter:

```python
    def __init__(
        self,
        account_id: str,
        client_id: str,
        client_secret: str,
        PAGE_SIZE: int = 300,
        timeout=(10, 120),
        max_rate_limit_retries: int = 8,
        concurrency: int = 8,
        requests_per_second=8,
        backoff_base: float = 1.0,
        backoff_jitter=None,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ):
        # ... existing assignments from Task 5 ...
        self.max_rate_limit_retries = max_rate_limit_retries
        self.sleep = sleep
        self.backoff_base = backoff_base
        self.backoff_jitter = backoff_jitter or (lambda: random.uniform(0, backoff_base))
        self.rate_limiter = RateLimiter(requests_per_second, sleep=sleep, monotonic=monotonic)
        # ... session + token_lock from Task 5 ...
```

Add `import random` at the top.

Acquire the limiter inside `_send` (paces every HTTP call, including token POSTs since `fetch_token` also uses the session — but the limiter is only invoked here; add an explicit acquire in `fetch_token` too):

```python
    def _send(self, method, url, token, params=None, json=None, stream=False):
        self.rate_limiter.acquire()
        return self.session.request(
            method, url, headers=self.get_headers(token),
            params=params, json=json, stream=stream, timeout=self.timeout,
        )
```

Replace the 429 loop in `request` to use exponential backoff:

```python
    def request(self, method, url, params=None, json=None, stream=False):
        request_url = self._normalize_url(url)
        response = self._request_with_token(
            method, request_url, params=params, json=json, stream=stream
        )

        attempt = 0
        while response.status_code == 429 and attempt < self.max_rate_limit_retries:
            retry_after = self._retry_after_seconds(response.headers.get("Retry-After"))
            backoff = self.backoff_base * (2 ** attempt)
            wait = max(retry_after, backoff) + self.backoff_jitter()
            if wait > 0:
                self.sleep(wait)
            attempt += 1
            response = self._request_with_token(
                method, request_url, params=params, json=json, stream=stream
            )

        if not response.ok:
            raise self._error_from_response(response)

        return response
```

- [ ] **Step 4: Run the client tests to verify they pass**

Run: `python3 -m pytest tests/test_zoom_client.py -v`
Expected: PASS (rate limiter spacing, exponential backoff `[1.0, 2.0]`, retry-after `[2]`, and single-refresh 401 all pass).

- [ ] **Step 5: Wire the new client config in `create_client`**

In `zoom_batch_downloader.py`, replace `create_client`:

```python
def create_client(config):
    return zoom_client(
        account_id=config.ACCOUNT_ID,
        client_id=config.CLIENT_ID,
        client_secret=config.CLIENT_SECRET,
        concurrency=config_value(config, "CONCURRENCY", 8),
        requests_per_second=config_value(config, "REQUESTS_PER_SECOND", 8),
        max_rate_limit_retries=config_value(config, "MAX_RATE_LIMIT_RETRIES", 8),
    )
```

- [ ] **Step 6: Run the full suite to verify it passes**

Run: `python3 -m pytest -v`
Expected: PASS (all tests).

- [ ] **Step 7: Commit**

```bash
git add zoom_client.py zoom_batch_downloader.py tests/test_zoom_client.py
git commit -m "feat: add shared rate limiter and exponential backoff

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: All-time date convenience

**Files:**
- Modify: `zoom_batch_downloader.py` (`get_date_range`)
- Test: `tests/test_zoom_batch_downloader.py`

**Interfaces:**
- Produces: `get_date_range(config, today=None)`. When `config.ALL_TIME` is truthy, returns `(datetime(ALL_TIME_START_YEAR/MONTH/DAY), today or now)` and ignores `START_*`/`END_*`. Defaults: year 2012, month 1, day 1.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_zoom_batch_downloader.py`:

```python
import datetime


def test_get_date_range_all_time_uses_floor_and_today(tmp_path):
    config = make_config(tmp_path, ALL_TIME=True)
    today = datetime.datetime(2026, 7, 7)

    from_date, to_date = zbd.get_date_range(config, today=today)

    assert from_date == datetime.datetime(2012, 1, 1)
    assert to_date == today


def test_get_date_range_respects_explicit_range_when_not_all_time(tmp_path):
    config = make_config(tmp_path, ALL_TIME=False)

    from_date, to_date = zbd.get_date_range(config)

    assert from_date == datetime.datetime(2024, 1, 1)
    assert to_date == datetime.datetime(2024, 1, 31)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_zoom_batch_downloader.py -k date_range -v`
Expected: FAIL (`get_date_range` takes 1 positional arg / ignores `ALL_TIME`).

- [ ] **Step 3: Implement `ALL_TIME` handling**

Replace `get_date_range`:

```python
def get_date_range(config, today=None):
    if config_value(config, "ALL_TIME", False):
        from_date = datetime.datetime(
            config_value(config, "ALL_TIME_START_YEAR", 2012),
            config_value(config, "ALL_TIME_START_MONTH", 1),
            config_value(config, "ALL_TIME_START_DAY", 1),
        )
        to_date = today or datetime.datetime.now()
        return from_date, to_date

    from_date = datetime.datetime(
        config.START_YEAR, config.START_MONTH, config.START_DAY or 1
    )
    to_date = datetime.datetime(
        config.END_YEAR,
        config.END_MONTH,
        config.END_DAY or monthrange(config.END_YEAR, config.END_MONTH)[1],
    )
    return from_date, to_date
```

- [ ] **Step 4: Run the full suite to verify it passes**

Run: `python3 -m pytest -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add zoom_batch_downloader.py tests/test_zoom_batch_downloader.py
git commit -m "feat: add ALL_TIME date-range convenience

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Config template, secret redaction, and README

**Files:**
- Modify: `config_template.py`
- Modify: `utils.py` (`redact_sensitive_text`)
- Modify: `README.md`
- Test: `tests/test_zoom_batch_downloader.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: documented config keys `STORAGE_BACKEND`, `AZURE_*`, `CONCURRENCY`, `REQUESTS_PER_SECOND`, `MAX_RATE_LIMIT_RETRIES`, `ALL_TIME`, `ALL_TIME_START_*`; `redact_sensitive_text` that also redacts Azure `AccountKey` and `sig` values.

- [ ] **Step 1: Write the failing test for Azure secret redaction**

Add to `tests/test_zoom_batch_downloader.py`:

```python
def test_redacts_azure_secrets():
    conn = ("DefaultEndpointsProtocol=https;AccountName=acct;"
            "AccountKey=SUPERSECRETKEY==;EndpointSuffix=core.windows.net")
    sas = "https://acct.blob.core.windows.net/c/blob?sig=SECRETSIG&sv=2021"

    assert "SUPERSECRETKEY" not in utils.redact_sensitive_text(conn)
    assert "SECRETSIG" not in utils.redact_sensitive_text(sas)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_zoom_batch_downloader.py::test_redacts_azure_secrets -v`
Expected: FAIL (`SUPERSECRETKEY` / `SECRETSIG` still present).

- [ ] **Step 3: Extend `redact_sensitive_text` in `utils.py`**

Add two substitutions before the `return`:

```python
def redact_sensitive_text(text):
	text = str(text)
	text = re.sub(r"access_token=([^&\s]+)", "access_token=[REDACTED]", text)
	text = re.sub(r"token=([^&\s]+)", "token=[REDACTED]", text)
	text = re.sub(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
	text = re.sub(r"AccountKey=[^;]+", "AccountKey=[REDACTED]", text)
	text = re.sub(r"(sig=)[^&\s;]+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
	return text
```

- [ ] **Step 4: Run the redaction test to verify it passes**

Run: `python3 -m pytest tests/test_zoom_batch_downloader.py -k redact -v`
Expected: PASS (both the existing token redaction test and the new Azure one).

- [ ] **Step 5: Add the new config keys to `config_template.py`**

Append after the existing credentials block / near related settings:

```python
# --- Scale & destination settings ---

# Where downloads land: "local" writes to OUTPUT_PATH on disk; "azure" streams to Azure Blob Storage.
STORAGE_BACKEND = "local"

# Azure Blob settings (used only when STORAGE_BACKEND = "azure").
# Provide either a connection string, or an account URL (with an appropriate credential/SAS in the URL).
AZURE_STORAGE_CONNECTION_STRING = R""
AZURE_STORAGE_ACCOUNT_URL = R""
AZURE_CONTAINER = R""
# Optional prefix (virtual folder) prepended to every blob name.
AZURE_PREFIX = R""

# Number of concurrent download workers. Raise if your Zoom plan's rate limits allow.
CONCURRENCY = 8

# Global cap on Zoom API requests per second, shared across all workers.
REQUESTS_PER_SECOND = 8

# How many times to retry a single request after a 429 (rate limit) before giving up.
MAX_RATE_LIMIT_RETRIES = 8

# If True, ignore the START_*/END_* range and scan from ALL_TIME_START to today.
ALL_TIME = False
ALL_TIME_START_YEAR = 2012
ALL_TIME_START_MONTH = 1
ALL_TIME_START_DAY = 1
```

- [ ] **Step 6: Document the new options in `README.md`**

Add a short subsection under **Configuration** describing `STORAGE_BACKEND` + Azure keys, `CONCURRENCY` / `REQUESTS_PER_SECOND` / `MAX_RATE_LIMIT_RETRIES`, and `ALL_TIME`. Note that `azure-storage-blob` is only required when `STORAGE_BACKEND = "azure"`, and that Azure destinations skip the disk-space preflight.

- [ ] **Step 7: Run the full suite to verify it passes**

Run: `python3 -m pytest -v`
Expected: PASS (all tests across all files).

- [ ] **Step 8: Commit**

```bash
git add config_template.py utils.py README.md tests/test_zoom_batch_downloader.py
git commit -m "feat: document scale/Azure config and redact Azure secrets

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Pluggable local/Azure destination → Tasks 1, 2, 3, 8. ✅
- Skip-existing across both backends → Task 3 (`make_inventory_item` uses `storage.size`/`exists`). ✅
- Configurable concurrency → Task 4. ✅
- Shared rate limiter → Task 6. ✅
- Exponential backoff on 429 → Task 6. ✅
- Connection pooling → Task 5. ✅
- Thread-safe token refresh → Task 5. ✅
- All-time date convenience → Task 7. ✅
- Backend-aware disk-space checks → Task 3 (`check_destination_space` skips when `free_space` is `None`). ✅
- Azure secret redaction → Task 8. ✅
- Preserve all modes / deletion gates → unchanged code paths; existing tests retained. ✅
- Lazy `azure-storage-blob` import → Task 1 factory + Task 2 `from_config`/methods. ✅

**Placeholder scan:** No TBD/TODO; every code step shows complete code. ✅

**Type/signature consistency:** `download_recording_item(config, client, item, storage, show_progress=True)` defined in Task 3 and consumed by Task 4's worker; `download_with_retry(client, storage, download_url, dest_path, file_size, verbose_output, size_tolerance, show_progress, max_retries=10)` consistent between Tasks 3 and its callers; `check_destination_space(config, storage, inventory, fail_on_shortage)` consistent across Task 3 caller sites and updated tests; `RateLimiter` / `get_date_range(config, today=None)` signatures match tests. ✅
