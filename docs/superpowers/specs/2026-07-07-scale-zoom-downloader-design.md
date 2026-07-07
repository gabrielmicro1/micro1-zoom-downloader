# Design: Scale the Zoom batch downloader (Approach B)

**Date:** 2026-07-07
**Status:** Approved (pending spec review)

## Goal

Make `micro1-zoom-downloader` capable of downloading on the order of 100,000
recordings from a Zoom account without falling over on speed, connection
overhead, or rate limits, and let downloads land in either the local filesystem
or Azure Blob Storage.

This is **Approach B** (minimal-change parallelization): keep the existing
"scan the whole account into an inventory, then download" shape and all existing
modes; parallelize the download phase and harden the API client.

## Non-goals

- No incremental/streaming pipeline that overlaps scan and download (that was
  Approach A, deferred).
- No persistent per-file state ledger. Resumability relies on skip-existing
  (existence + size check), extended to work against both storage backends.
- No trashed-recording (`trash=true`) support.
- No `pending`-status user scanning (only `active` + `inactive`, as today).
- No async/aiohttp rewrite (Approach C, rejected).

## Requirements (from brainstorming)

1. **Pluggable destination:** local filesystem or Azure Blob, selected in config.
2. **Resumability:** skip-existing only; no new ledger. The existence check must
   work against both backends.
3. **Concurrency:** configurable worker pool + shared rate limiter + exponential
   backoff on 429s.
4. **Completeness:** add an "all-time" date convenience. Do not touch trash /
   pending users.
5. Preserve all existing modes (`download`, `estimate`, `retry_not_ready`,
   `delete_bulk`, `delete_one`) and the config-driven UX.

## Architecture

### 1. Storage backend abstraction (new `storage.py`)

Introduce a small `Storage` interface, chosen by `STORAGE_BACKEND`
(`"local"` | `"azure"`) via a `create_storage(config)` factory.

Interface:

- `exists(path) -> bool`
- `size(path) -> int | None` — `None` when the object does not exist.
- `save_stream(response, dest_path, expected_size, verbose, size_tolerance) -> bool`
  — stream a `requests` response body into the destination with progress,
  atomic/safe write semantics, and post-write size validation.
- `remove(path) -> None` — delete a destination object (used to clear corrupt
  partial results).
- `free_space(path) -> int | None` — bytes free, or `None` when the backend has
  no meaningful limit (Azure).

Implementations:

- **`LocalStorage`** — preserves today's exact behavior. `save_stream` writes to
  `<dest>.tmp`, validates size against `expected_size` within
  `FILE_SIZE_MISMATCH_TOLERANCE`, then `os.rename` to the final path. `exists` /
  `size` use `os.path`. `free_space` uses `shutil.disk_usage`. Reuses the
  existing `utils.download_response_with_progress` logic.
- **`AzureBlobStorage`** — uses `azure-storage-blob`, imported lazily inside the
  class so local-only users need not install it. Constructed from either
  `AZURE_STORAGE_CONNECTION_STRING`, or `AZURE_STORAGE_ACCOUNT_URL` +
  credential, plus `AZURE_CONTAINER`. `exists` / `size` come from blob
  properties. `save_stream` streams response chunks into
  `upload_blob(overwrite=True)`, then validates the uploaded size and deletes the
  blob on mismatch (returning failure). `free_space` returns `None`.

**Path handling:** paths are still built exactly as today (an `OUTPUT_PATH`-rooted
string produced by `create_path` / `get_user_host_folder`). The storage backend
interprets that string:

- `LocalStorage` treats it as a filesystem path (current behavior).
- `AzureBlobStorage` converts it to a blob key: normalize `\` → `/`, strip any
  drive letter and leading slashes, and prepend the optional `AZURE_PREFIX`.

This keeps inventory/download code changes limited to routing `os.*` calls
through `storage.*`.

### 2. Parallel downloads

`download_inventory` replaces its serial `for item in inventory.files` loop with
a `concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY)`, submitting one
task per `RecordingItem` (each calls the existing `download_recording_item`).
Results are aggregated into `DownloadSummary` under a `threading.Lock`, preserving
`downloaded_count` / `skipped_count` / `failed_count` / `downloaded_size` /
`failed_meeting_uuids` semantics.

Progress: per-file tqdm bars collide across threads, so concurrent mode
(`CONCURRENCY > 1`) shows a **single aggregate progress bar** over file count
(plus a running byte total). When `CONCURRENCY == 1`, keep the existing per-file
byte progress bar for parity with today.

`download_recording_item` is refactored to call `storage.save_stream` /
`storage.exists` / `storage.size` / `storage.remove` instead of direct `os` calls
and `download_with_retry`. The download+retry wrapper is retained but writes via
the storage backend. `retry_not_ready` reuses `download_inventory` and benefits
automatically.

### 3. Zoom client hardening (`zoom_client.py`)

- **Connection pooling:** a single `requests.Session` with an `HTTPAdapter`
  (`pool_connections` / `pool_maxsize` sized to `CONCURRENCY`) mounted on
  `https://`, replacing per-call `requests.request()`. Eliminates a fresh TLS
  handshake per request.
- **Thread-safe token:** a `threading.Lock` guarding `cached_token` and
  `fetch_token`, using double-checked locking so concurrent 401s trigger at most
  one refresh.
- **Shared rate limiter:** a token-bucket limiter (`REQUESTS_PER_SECOND`,
  internally locked) acquired before every API/download request, shared across
  all worker threads.
- **Backoff:** on HTTP 429, exponential backoff with jitter that honors
  `Retry-After`, with a higher retry ceiling (`MAX_RATE_LIMIT_RETRIES`), and a
  clear, distinct message when Zoom's daily request limit is reached.

Concurrency note: `requests.Session` is used for independent per-thread requests
with a connection pool sized to `CONCURRENCY`; token refresh and the rate limiter
are the only shared mutable state and are lock-guarded.

### 4. All-time date convenience

`ALL_TIME = True` makes `get_date_range` return `(ALL_TIME_START, today)` and
ignore `START_*` / `END_*`. `ALL_TIME_START` defaults to `2012-01-01`. "Today" is
derived from the current date. The existing ~30-day windowing loop in
`get_meeting_uuids` already handles the long span.

### 5. Config additions (`config_template.py`)

- `STORAGE_BACKEND = "local"`  # "local" | "azure"
- Azure: `AZURE_STORAGE_CONNECTION_STRING`, or `AZURE_STORAGE_ACCOUNT_URL`;
  `AZURE_CONTAINER`; `AZURE_PREFIX` (optional).
- `CONCURRENCY = 8`
- `REQUESTS_PER_SECOND = 8`
- `MAX_RATE_LIMIT_RETRIES` (raised default) and backoff knobs.
- `ALL_TIME = False`, `ALL_TIME_START` (default `2012-01-01`).

`requirements.txt` gains `azure-storage-blob` (only needed at runtime when
`STORAGE_BACKEND = "azure"`; imported lazily).

### 6. Safety & error handling

- Disk-space checks (`check_destination_space`, `wait_for_disk_space`) run only
  for the local backend; skipped when `free_space` is `None` (Azure).
- `utils.redact_sensitive_text` extended to redact Azure connection strings and
  SAS tokens.
- Local writes stay atomic (`.tmp` + rename). Azure validates uploaded size and
  deletes the blob on mismatch.
- `DownloadSummary` aggregation is lock-guarded for thread safety.
- All existing deletion safety gates (dry-run default, `CONFIRM_DELETE`,
  permanent-delete triple gate) are unchanged.

## Testing

`pytest` + `responses`, extending the existing suite:

- `LocalStorage`: `exists` / `size` / `save_stream` / `remove` / `free_space`
  against a temp dir, including size-mismatch rejection.
- `AzureBlobStorage`: against a mocked `BlobServiceClient` / `BlobClient` (no live
  Azure) — exists/size/save/remove and mismatch-triggered blob deletion.
- Concurrent `download_inventory` (`CONCURRENCY > 1`): all files downloaded,
  summary aggregation correct, a single failing item is counted and does not
  abort the batch.
- Zoom client: 429 exponential backoff honoring `Retry-After`; token refresh
  under a simulated 401 is single-flight; rate limiter paces requests.
- `get_date_range` with `ALL_TIME = True` resolves to `(ALL_TIME_START, today)`.

## Defaults

- `CONCURRENCY = 8`, `REQUESTS_PER_SECOND = 8` (safe starting point; tunable up
  per Zoom plan).
- `STORAGE_BACKEND = "local"` (backwards-compatible default).
- `ALL_TIME = False`.
