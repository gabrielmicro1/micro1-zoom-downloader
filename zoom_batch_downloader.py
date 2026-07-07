from __future__ import annotations

import datetime
import importlib
import math
import os
import shutil
import sqlite3
import sys
import threading
import time
import traceback
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import colorama
from colorama import Fore, Style

import utils
from storage import create_storage, LocalStorage
from zoom_client import zoom_client


SUPPORTED_MODES = {"download", "estimate", "retry_not_ready", "delete_bulk", "delete_one"}
DELETE_ACTIONS = {"trash", "delete"}
DELETE_SCOPES = {"files", "meetings"}


class ConfigError(Exception):
    pass


@dataclass
class RecordingItem:
    meeting_uuid: str
    topic: str
    recording_id: str
    file_type: str
    file_size: int
    download_url: str
    destination_path: str
    file_name: str
    recording_name: str
    recording_type: str
    local_exists: bool
    local_size_matches: bool
    source_kind: str


@dataclass
class RecordingInventory:
    files: list[RecordingItem]
    meeting_count: int
    skipped_missing_size_count: int = 0

    @property
    def total_size(self):
        return sum(item.file_size for item in self.files)

    @property
    def already_present_size(self):
        return sum(item.file_size for item in self.files if item.local_size_matches)

    @property
    def additional_size(self):
        return sum(item.file_size for item in self.files if not item.local_size_matches)

    @property
    def additional_file_count(self):
        return sum(1 for item in self.files if not item.local_size_matches)

    @property
    def meeting_uuids(self):
        return sorted({item.meeting_uuid for item in self.files})


@dataclass
class SpaceStatus:
    usage_path: str
    free_bytes: int
    required_bytes: int
    additional_bytes: int
    reserve_bytes: int

    @property
    def has_enough_space(self):
        return self.free_bytes >= self.required_bytes


@dataclass
class DownloadSummary:
    downloaded_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    downloaded_size: int = 0
    failed_meeting_uuids: set[str] | None = None

    def __post_init__(self):
        if self.failed_meeting_uuids is None:
            self.failed_meeting_uuids = set()


@dataclass
class DeleteSummary:
    deleted_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0


def load_config():
    try:
        return importlib.import_module("config")
    except ImportError as error:
        raise ConfigError(
            "Missing config file, copy config_template.py to config.py and change as needed."
        ) from error


def create_client(config):
    return zoom_client(
        account_id=config.ACCOUNT_ID,
        client_id=config.CLIENT_ID,
        client_secret=config.CLIENT_SECRET,
        concurrency=config_value(config, "CONCURRENCY", 8),
        requests_per_second=config_value(config, "REQUESTS_PER_SECOND", 8),
        max_rate_limit_retries=config_value(config, "MAX_RATE_LIMIT_RETRIES", 8),
    )


def resolve_mode(config):
    if hasattr(config, "MODE"):
        mode = config.MODE
    elif getattr(config, "NOT_READY_FILES_ONLY", False):
        utils.print_bright_red(
            "NOT_READY_FILES_ONLY is deprecated; using MODE = 'retry_not_ready'."
        )
        mode = "retry_not_ready"
    else:
        mode = "download"

    if mode not in SUPPORTED_MODES:
        raise ConfigError(
            f"Unsupported MODE {mode!r}. Expected one of {sorted(SUPPORTED_MODES)}."
        )

    return mode


def config_value(config, name, default):
    return getattr(config, name, default)


def main():
    colorama.init()
    config = load_config()
    run(config)


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


def print_filter_warnings(config):
    did_print = False

    if config.TOPICS:
        utils.print_bright(f"Topics filter is active {config.TOPICS}")
        did_print = True
    if config.USERS:
        utils.print_bright(f"Users filter is active {config.USERS}")
        did_print = True
    if config.RECORDING_FILE_TYPES:
        utils.print_bright(
            f"Recording file types filter is active {config.RECORDING_FILE_TYPES}"
        )
        did_print = True

    if did_print:
        print()


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


def ensure_meetings_db(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS meetings
        (id INTEGER PRIMARY KEY, uuid TEXT UNIQUE)"""
    )
    conn.commit()
    return conn


def read_logged_meeting_uuids(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT uuid FROM meetings")
    return [row[0] for row in cursor.fetchall()]


def log_meeting_uuid(conn, meeting_uuid):
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO meetings (uuid) VALUES (?)", (meeting_uuid,))
    conn.commit()


def clear_meeting_uuid(conn, meeting_uuid):
    cursor = conn.cursor()
    cursor.execute("DELETE FROM meetings WHERE uuid = ?", (meeting_uuid,))
    conn.commit()


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


def get_users(config, client):
    if config.USERS:
        return [(email, "") for email in config.USERS]

    utils.print_bright("Scanning for users:")
    active_users_url = "https://api.zoom.us/v2/users?status=active"
    inactive_users_url = "https://api.zoom.us/v2/users?status=inactive"

    users = []
    pages = utils.chain(client.paginate(active_users_url), client.paginate(inactive_users_url))
    for page in utils.percentage_tqdm(pages):
        users.extend([(user["email"], get_user_name(user)) for user in page["users"]])

    print()
    return users


def get_user_name(user_data):
    first_name = user_data.get("first_name")
    last_name = user_data.get("last_name")

    if first_name and last_name:
        return f"{first_name} {last_name}"
    return first_name or last_name


def get_user_description(user_email, user_name):
    return f"{user_email} ({user_name})" if user_name else user_email


def get_user_host_folder(config, user_email):
    if config.GROUP_BY_USER:
        return os.path.join(config.OUTPUT_PATH, user_email)
    return config.OUTPUT_PATH


def get_meeting_host_folder(config, meeting):
    host_email = meeting.get("host_email") or meeting.get("host_id") or "unknown-user"
    return get_user_host_folder(config, host_email)


def date_to_str(date):
    return date.strftime("%Y-%m-%d")


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


def get_meeting_uuids(client, user_email, start_date, end_date):
    meeting_uuids = []
    local_start_date = start_date
    delta = datetime.timedelta(days=29)

    utils.print_bright("Scanning for recorded meetings:")
    estimated_iterations = max(
        1, math.ceil((end_date - start_date) / datetime.timedelta(days=30))
    )
    with utils.percentage_tqdm(total=estimated_iterations) as progress_bar:
        while local_start_date <= end_date:
            local_end_date = min(local_start_date + delta, end_date)

            local_start_date_str = date_to_str(local_start_date)
            local_end_date_str = date_to_str(local_end_date)
            url = (
                f"https://api.zoom.us/v2/users/{user_email}/recordings?"
                f"from={local_start_date_str}&to={local_end_date_str}"
            )

            ids = []
            for page in client.paginate(url):
                ids.extend([meeting["uuid"] for meeting in page["meetings"]])

            meeting_uuids.extend(reversed(ids))
            local_start_date = local_end_date + datetime.timedelta(days=1)
            progress_bar.update(1)

    return meeting_uuids


def get_meetings(client, meeting_uuids, conn=None, log_failures=True, clear_success=True):
    meetings = []

    if meeting_uuids:
        utils.print_bright("Scanning for recordings:")

        for meeting_uuid in utils.percentage_tqdm(meeting_uuids):
            url = (
                "https://api.zoom.us/v2/meetings/"
                f"{utils.double_encode(meeting_uuid)}/recordings"
            )
            try:
                meeting = client.get(url)
                meeting["_requested_uuid"] = meeting_uuid
                meetings.append(meeting)
                if conn is not None and clear_success:
                    clear_meeting_uuid(conn, meeting_uuid)
            except Exception as error:
                if conn is not None and log_failures:
                    log_meeting_uuid(conn, meeting_uuid)
                utils.print_bright(
                    "Logging error occurred while retrieving recordings for "
                    f"meeting {meeting_uuid}: {utils.redact_sensitive_text(error)}"
                )

    return meetings


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


def meeting_matches_topic_filter(config, meeting):
    topic = meeting.get("topic", "")
    return not (
        config.TOPICS
        and topic not in config.TOPICS
        and utils.slugify(topic) not in config.TOPICS
    )


def recording_file_matches_type_filter(config, recording_file):
    return not (
        config.RECORDING_FILE_TYPES
        and recording_file.get("file_type") not in config.RECORDING_FILE_TYPES
    )


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


def build_file_name(recording_file, recording_name):
    raw_file_name = recording_file.get("file_name", "")
    extension = recording_file.get("file_extension") or os.path.splitext(raw_file_name)[1]
    extension = str(extension).lstrip(".") or "download"
    file_id = recording_file["id"]
    file_name_suffix = (
        os.path.splitext(raw_file_name)[0] + "__" if raw_file_name else ""
    )
    recording_type_suffix = (
        recording_file["recording_type"] + "__" if "recording_type" in recording_file else ""
    )
    return (
        utils.slugify(
            f"{recording_name}__{recording_type_suffix}{file_name_suffix}{file_id[-8:]}"
        )
        + "."
        + extension
    )


def create_path(config, host_folder, file_name, topic, recording_name, create_dirs=True):
    folder_path = host_folder

    if config.GROUP_BY_TOPIC:
        folder_path = os.path.join(folder_path, topic)
    if config.GROUP_BY_RECORDING:
        folder_path = os.path.join(folder_path, recording_name)

    if create_dirs:
        os.makedirs(folder_path, exist_ok=True)

    return os.path.join(folder_path, file_name)


def print_inventory_summary(inventory):
    utils.print_bright("Recording inventory:")
    print(f"Matched meetings: {inventory.meeting_count}")
    print(f"Matched files: {len(inventory.files)}")
    print(f"Matched remote size: {utils.size_to_string(inventory.total_size)}")
    print(
        f"Already present locally: {utils.size_to_string(inventory.already_present_size)}"
    )
    print(f"Additional download size: {utils.size_to_string(inventory.additional_size)}")
    if inventory.skipped_missing_size_count:
        print(f"Skipped files missing size/id: {inventory.skipped_missing_size_count}")
    print()


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


def print_destination_space_status(status):
    utils.print_bright("Destination drive space:")
    print(f"Checked path: {status.usage_path}")
    print(f"Free space: {utils.size_to_string(status.free_bytes)}")
    print(f"Additional download size: {utils.size_to_string(status.additional_bytes)}")
    print(f"Configured reserve: {utils.size_to_string(status.reserve_bytes)}")
    print(f"Required free space: {utils.size_to_string(status.required_bytes)}")
    if status.has_enough_space:
        print(f"{Fore.GREEN}Enough free space is available.{Fore.RESET}")
    else:
        print(f"{Fore.RED}Not enough free space is available.{Fore.RESET}")
    print()


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


def print_download_summary(summary):
    total_size_str = utils.size_to_string(summary.downloaded_size)
    print(
        f"{Style.BRIGHT}Downloaded {Fore.GREEN}{summary.downloaded_count}{Fore.RESET} files.",
        f"Total size: {Fore.GREEN}{total_size_str}{Fore.RESET}.{Style.RESET_ALL}",
        f"Skipped: {summary.skipped_count} files.",
        f"Failed: {summary.failed_count} files.",
    )


def validate_delete_config(config):
    action = config_value(config, "DELETE_ACTION", "trash")
    scope = config_value(config, "DELETE_SCOPE", "files")
    dry_run = config_value(config, "DRY_RUN", True)
    confirm = config_value(config, "CONFIRM_DELETE", "")
    allow_permanent = config_value(config, "ALLOW_PERMANENT_DELETE", False)

    if action not in DELETE_ACTIONS:
        raise ConfigError(f"DELETE_ACTION must be one of {sorted(DELETE_ACTIONS)}.")
    if scope not in DELETE_SCOPES:
        raise ConfigError(f"DELETE_SCOPE must be one of {sorted(DELETE_SCOPES)}.")
    if action == "delete" and (not allow_permanent or confirm != "DELETE PERMANENTLY"):
        raise ConfigError(
            "Permanent deletion requires DELETE_ACTION = 'delete', "
            "ALLOW_PERMANENT_DELETE = True, and CONFIRM_DELETE = 'DELETE PERMANENTLY'."
        )
    if not dry_run and action == "trash" and confirm != "DELETE":
        raise ConfigError("Trash deletion requires CONFIRM_DELETE = 'DELETE'.")

    return action, scope, dry_run


def delete_inventory(config, client, inventory):
    action, scope, dry_run = validate_delete_config(config)
    summary = DeleteSummary()

    if scope == "meetings":
        targets = [(meeting_uuid, None) for meeting_uuid in inventory.meeting_uuids]
    else:
        targets = [(item.meeting_uuid, item) for item in inventory.files]

    if dry_run:
        utils.print_bright("Deletion dry run:")

    for meeting_uuid, item in targets:
        try:
            if dry_run:
                print_delete_target(meeting_uuid, item, action, scope)
                summary.skipped_count += 1
                continue

            delete_recording_target(client, meeting_uuid, item, action)
            summary.deleted_count += 1
        except Exception as error:
            summary.failed_count += 1
            utils.print_dim_red(
                f"Delete failed for meeting {meeting_uuid}: {utils.redact_sensitive_text(error)}"
            )

    return summary


def delete_one(config, client):
    action, _, dry_run = validate_delete_config(config)
    meeting_uuid = config_value(config, "DELETE_MEETING_UUID", None)
    recording_id = config_value(config, "DELETE_RECORDING_ID", None)

    if not meeting_uuid:
        raise ConfigError("MODE = 'delete_one' requires DELETE_MEETING_UUID.")

    item = None
    if recording_id:
        item = RecordingItem(
            meeting_uuid=meeting_uuid,
            topic="",
            recording_id=recording_id,
            file_type="",
            file_size=0,
            download_url="",
            destination_path="",
            file_name=recording_id,
            recording_name="",
            recording_type="",
            local_exists=False,
            local_size_matches=False,
            source_kind="recording_file",
        )

    if dry_run:
        utils.print_bright("Deletion dry run:")
        print_delete_target(meeting_uuid, item, action, "files" if item else "meetings")
        print_delete_summary(DeleteSummary(skipped_count=1))
        return

    delete_recording_target(client, meeting_uuid, item, action)
    print_delete_summary(DeleteSummary(deleted_count=1))


def print_delete_target(meeting_uuid, item, action, scope):
    encoded_uuid = utils.double_encode(meeting_uuid)
    if item is None:
        print(
            "Would delete meeting recording set: "
            f"meeting={meeting_uuid}, action={action}, "
            f"endpoint=/meetings/{encoded_uuid}/recordings"
        )
        return

    print(
        "Would delete recording file: "
        f"meeting={meeting_uuid}, recording_id={item.recording_id}, "
        f"file={item.file_name}, size={utils.size_to_string(item.file_size)}, "
        f"scope={scope}, action={action}, "
        f"endpoint=/meetings/{encoded_uuid}/recordings/{item.recording_id}"
    )


def delete_recording_target(client, meeting_uuid, item, action):
    encoded_uuid = utils.double_encode(meeting_uuid)
    if item is None:
        url = f"https://api.zoom.us/v2/meetings/{encoded_uuid}/recordings"
    else:
        url = (
            f"https://api.zoom.us/v2/meetings/{encoded_uuid}/recordings/"
            f"{item.recording_id}"
        )
    client.delete(url, params={"action": action})


def print_delete_summary(summary):
    print(
        f"{Style.BRIGHT}Deleted {Fore.GREEN}{summary.deleted_count}{Fore.RESET} targets.",
        f"Dry-run/skipped: {summary.skipped_count}.",
        f"Failed: {summary.failed_count}.{Style.RESET_ALL}",
    )


if __name__ == "__main__":
    try:
        main()
    except ConfigError as error:
        print()
        utils.print_bright_red(error)
        sys.exit(1)
    except AttributeError as error:
        print()
        utils.print_bright_red(
            f"Missing config value: {error}. See config_template.py for the complete list."
        )
        sys.exit(1)
    except KeyboardInterrupt:
        print()
        utils.print_bright_red("Interrupted by the user")
        sys.exit(1)
    except Exception as error:
        print()
        verbose = False
        try:
            verbose = bool(getattr(importlib.import_module("config"), "VERBOSE_OUTPUT", False))
        except Exception:
            pass
        if verbose and utils.is_debug():
            raise
        if verbose:
            utils.print_dim_red(traceback.format_exc())
        else:
            utils.print_bright_red(f"Error: {utils.redact_sensitive_text(error)}")
        sys.exit(1)
