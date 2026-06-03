# zoom-batch-downloader

Download, estimate, and safely delete Zoom cloud recordings for accounts with
[paid plans](https://zoom.us/pricing#personal).

This script uses Zoom Server-to-Server OAuth. Create a
[Server-to-Server OAuth app](https://developers.zoom.us/docs/internal-apps/create/)
in the [Zoom App Marketplace](https://marketplace.zoom.us/user/build), then copy
that app's credentials into `config.py`.

## Authentication

You do not paste a Zoom access token into this project. The script requests
short-lived access tokens automatically from Zoom by using the three app
credentials in `config.py`:

```python
ACCOUNT_ID = R"your_zoom_account_id"
CLIENT_ID = R"your_zoom_client_id"
CLIENT_SECRET = R"your_zoom_client_secret"
```

Find these values on the **App Credentials** page of your Zoom Server-to-Server
OAuth app:

- `ACCOUNT_ID` is Zoom's Account ID for the app.
- `CLIENT_ID` is the app's Client ID.
- `CLIENT_SECRET` is the app's Client Secret.

Keep `config.py` private. It is ignored by git because it contains secrets.

Scopes are not configured in `config.py`; add them to the Zoom app in the
Marketplace. If Zoom returns an authorization or permissions error, check the
app's scopes first, then confirm the three credential values above.

## Required Zoom app scopes

Configure these scopes in the Zoom Marketplace app before running the script.

For estimating and downloading recordings:

- `cloud_recording:read:list_user_recordings:admin`
- `cloud_recording:read:list_recording_files:admin`
- `user:read:list_users:admin` if `USERS` is empty and the script should scan every account user

For deletion modes, also add the corresponding cloud-recording delete/write
scope in your Zoom app. If you use classic scopes, the older equivalents are:

- `recording:read:admin`
- `recording:write:admin` for deletion
- `user:read:admin` if scanning all users

## Setup

1. Create and activate a Server-to-Server OAuth app in Zoom.

2. Clone or download this repository.

3. Copy `config_template.py` to `config.py`.

   ```bash
   cp config_template.py config.py
   ```

4. In `config.py`, set `ACCOUNT_ID`, `CLIENT_ID`, and `CLIENT_SECRET` from the
   Zoom app's **App Credentials** page. Then edit the date range, filters, and
   `OUTPUT_PATH`.

5. Install Python 3, create a virtual environment, and install requirements.

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   python3 -m pip install -r requirements.txt
   ```

6. Run a safe estimate first to confirm auth, scopes, filters, and disk-space
   reporting before downloading.

   ```python
   MODE = "estimate"
   ```

   ```bash
   python3 zoom_batch_downloader.py
   ```

7. If the estimate looks right, switch to download mode and run again.

   ```python
   MODE = "download"
   ```

   ```bash
   python3 zoom_batch_downloader.py
   ```

## Modes

Set `MODE` in `config.py`.

### Download

```python
MODE = "download"
```

This is the default. The script scans matching recordings, builds an inventory,
checks the destination drive has enough free space for missing files plus
`MINIMUM_FREE_DISK`, then downloads.

If the destination is short on space and `FAIL_IF_NOT_ENOUGH_SPACE = True`, the
script exits before downloading anything. If set to `False`, it keeps the older
per-file wait behavior.

### Estimate

```python
MODE = "estimate"
```

Scans matching recordings and prints:

- matched meeting and file counts
- total matched remote size
- already-present local size
- additional download size needed
- destination free space and required free space

No files are downloaded.

### Retry not-ready recordings

```python
MODE = "retry_not_ready"
```

Retries meeting UUIDs logged in `meetings.db`, runs the same space preflight, and
downloads files that are now available. Successfully downloaded or already
present meetings are removed from the retry table.

### Bulk delete

```python
MODE = "delete_bulk"
DRY_RUN = True
DELETE_ACTION = "trash"
DELETE_SCOPE = "files"
```

Bulk deletion uses the same filters as download mode. It is a dry run by
default and prints the exact targets without calling Zoom delete endpoints.

Use `DELETE_SCOPE = "files"` to delete matching recording files individually.
Use `DELETE_SCOPE = "meetings"` to delete each matched meeting's full recording
set.

To perform a real trash delete:

```python
MODE = "delete_bulk"
DRY_RUN = False
DELETE_ACTION = "trash"
DELETE_SCOPE = "files"
CONFIRM_DELETE = "DELETE"
```

### Delete one

Delete a single recording file:

```python
MODE = "delete_one"
DRY_RUN = True
DELETE_ACTION = "trash"
DELETE_MEETING_UUID = "MEETING_UUID"
DELETE_RECORDING_ID = "RECORDING_FILE_ID"
```

Delete a meeting's full recording set by leaving `DELETE_RECORDING_ID` empty:

```python
MODE = "delete_one"
DRY_RUN = True
DELETE_ACTION = "trash"
DELETE_MEETING_UUID = "MEETING_UUID"
DELETE_RECORDING_ID = None
```

To perform the real trash delete, set:

```python
DRY_RUN = False
CONFIRM_DELETE = "DELETE"
```

## Permanent deletion

Permanent deletion is intentionally hard to trigger. To permanently delete
recordings from Zoom instead of moving them to trash, all three settings are
required:

```python
DELETE_ACTION = "delete"
ALLOW_PERMANENT_DELETE = True
CONFIRM_DELETE = "DELETE PERMANENTLY"
```

Permanent deletion cannot be undone through Zoom trash recovery.

## Safety notes

- Delete modes never delete local downloaded files.
- Delete modes are dry-run by default.
- Trash deletion requires explicit confirmation.
- Permanent deletion requires separate explicit confirmation.
- Downloads keep TLS verification enabled and use bearer-token headers rather
  than appending tokens to logged URLs.
- Error output redacts bearer tokens and token query parameters.

Code written by Georg Kasmin, Lane Campbell, Sami Hassan and Aness Zurba.
