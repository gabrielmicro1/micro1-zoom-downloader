import datetime
import threading
from types import SimpleNamespace

import pytest

import utils
import zoom_batch_downloader as zbd
from storage import LocalStorage


class FakeSpaceStorage:
    def __init__(self, free):
        self._free = free

    def free_space(self, path):
        return self._free


def make_config(tmp_path, **overrides):
    config = SimpleNamespace(
        ACCOUNT_ID="acct",
        CLIENT_ID="client",
        CLIENT_SECRET="secret",
        MODE="download",
        OUTPUT_PATH=str(tmp_path),
        START_DAY=1,
        START_MONTH=1,
        START_YEAR=2024,
        END_DAY=31,
        END_MONTH=1,
        END_YEAR=2024,
        USERS=["host@example.com"],
        TOPICS=[],
        RECORDING_FILE_TYPES=[],
        GROUP_BY_USER=True,
        GROUP_BY_TOPIC=True,
        GROUP_BY_RECORDING=False,
        INCLUDE_PARTICIPANT_AUDIO=True,
        VERBOSE_OUTPUT=False,
        FAIL_IF_NOT_ENOUGH_SPACE=True,
        DRY_RUN=True,
        CONFIRM_DELETE="",
        DELETE_ACTION="trash",
        ALLOW_PERMANENT_DELETE=False,
        DELETE_SCOPE="files",
        DELETE_MEETING_UUID=None,
        DELETE_RECORDING_ID=None,
        MINIMUM_FREE_DISK=0,
        FILE_SIZE_MISMATCH_TOLERANCE=0,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def sample_meeting():
    return {
        "_requested_uuid": "meeting/uuid==",
        "topic": "Team Sync",
        "recording_files": [
            {
                "id": "rec-00000001",
                "file_type": "MP4",
                "file_size": 10,
                "download_url": "https://download.example.com/one",
                "recording_start": "2024-01-01T12:00:00Z",
                "recording_type": "shared_screen_with_speaker_view",
                "file_extension": "MP4",
                "file_name": "video.mp4",
            },
            {
                "id": "rec-00000002",
                "file_type": "M4A",
                "file_size": 20,
                "download_url": "https://download.example.com/two?access_token=secret",
                "recording_start": "2024-01-01T12:00:00Z",
                "recording_type": "audio_only",
                "file_extension": "M4A",
                "file_name": "audio.m4a",
            },
            {
                "id": "rec-missing",
                "file_type": "CHAT",
            },
        ],
    }


def test_inventory_calculates_sizes_and_existing_files(tmp_path):
    config = make_config(tmp_path)
    meeting = sample_meeting()
    first_file_name = zbd.build_file_name(
        meeting["recording_files"][0], "team-sync__2024-01-01t120000z"
    )
    existing_path = (
        tmp_path
        / "host@example.com"
        / "team-sync"
        / first_file_name
    )
    existing_path.parent.mkdir(parents=True)
    existing_path.write_bytes(b"x" * 10)

    inventory = zbd.build_inventory_from_meetings(
        config, [meeting], host_folder=str(tmp_path / "host@example.com")
    )

    assert len(inventory.files) == 2
    assert inventory.meeting_count == 1
    assert inventory.total_size == 30
    assert inventory.already_present_size == 10
    assert inventory.additional_size == 20
    assert inventory.additional_file_count == 1
    assert inventory.skipped_missing_size_count == 1


def test_missing_config_raises_clean_config_error():
    with pytest.raises(zbd.ConfigError, match="Missing config file"):
        zbd.load_config()


def test_estimate_space_check_reports_without_failing(tmp_path, monkeypatch):
    config = make_config(tmp_path, MINIMUM_FREE_DISK=100)
    inventory = zbd.RecordingInventory(
        files=[
            zbd.RecordingItem(
                meeting_uuid="m1",
                topic="topic",
                recording_id="r1",
                file_type="MP4",
                file_size=50,
                download_url="https://example.com",
                destination_path=str(tmp_path / "file.mp4"),
                file_name="file.mp4",
                recording_name="topic",
                recording_type="",
                local_exists=False,
                local_size_matches=False,
                source_kind="recording_file",
            )
        ],
        meeting_count=1,
    )
    status = zbd.check_destination_space(
        config, FakeSpaceStorage(25), inventory, fail_on_shortage=False
    )

    assert status.required_bytes == 150
    assert status.free_bytes == 25
    assert not status.has_enough_space


def test_download_space_check_fails_before_download_when_short(tmp_path, monkeypatch):
    config = make_config(tmp_path, MINIMUM_FREE_DISK=100)
    inventory = zbd.RecordingInventory(
        files=[
            zbd.RecordingItem(
                meeting_uuid="m1",
                topic="topic",
                recording_id="r1",
                file_type="MP4",
                file_size=50,
                download_url="https://example.com",
                destination_path=str(tmp_path / "file.mp4"),
                file_name="file.mp4",
                recording_name="topic",
                recording_type="",
                local_exists=False,
                local_size_matches=False,
                source_kind="recording_file",
            )
        ],
        meeting_count=1,
    )
    with pytest.raises(RuntimeError, match="Not enough free space"):
        zbd.check_destination_space(
            config, FakeSpaceStorage(25), inventory, fail_on_shortage=True
        )


class FakeDeleteClient:
    def __init__(self):
        self.deleted = []

    def delete(self, url, params=None):
        self.deleted.append((url, params))


def test_bulk_delete_dry_run_sends_no_delete(tmp_path):
    config = make_config(tmp_path, MODE="delete_bulk", DRY_RUN=True)
    inventory = zbd.build_inventory_from_meetings(
        config, [sample_meeting()], host_folder=str(tmp_path / "host@example.com")
    )
    client = FakeDeleteClient()

    summary = zbd.delete_inventory(config, client, inventory)

    assert client.deleted == []
    assert summary.skipped_count == 2


def test_bulk_file_delete_calls_individual_recording_endpoint(tmp_path):
    config = make_config(
        tmp_path,
        MODE="delete_bulk",
        DRY_RUN=False,
        CONFIRM_DELETE="DELETE",
        DELETE_SCOPE="files",
    )
    inventory = zbd.build_inventory_from_meetings(
        config, [sample_meeting()], host_folder=str(tmp_path / "host@example.com")
    )
    client = FakeDeleteClient()

    summary = zbd.delete_inventory(config, client, inventory)

    assert summary.deleted_count == 2
    assert len(client.deleted) == 2
    assert "/recordings/rec-00000001" in client.deleted[0][0]
    assert client.deleted[0][1] == {"action": "trash"}


def test_bulk_meeting_delete_calls_meeting_recordings_endpoint(tmp_path):
    config = make_config(
        tmp_path,
        MODE="delete_bulk",
        DRY_RUN=False,
        CONFIRM_DELETE="DELETE",
        DELETE_SCOPE="meetings",
    )
    inventory = zbd.build_inventory_from_meetings(
        config, [sample_meeting()], host_folder=str(tmp_path / "host@example.com")
    )
    client = FakeDeleteClient()

    summary = zbd.delete_inventory(config, client, inventory)

    assert summary.deleted_count == 1
    assert len(client.deleted) == 1
    assert client.deleted[0][0].endswith("/recordings")
    assert client.deleted[0][1] == {"action": "trash"}


def test_permanent_delete_requires_all_explicit_opt_ins(tmp_path):
    config = make_config(
        tmp_path,
        DELETE_ACTION="delete",
        ALLOW_PERMANENT_DELETE=True,
        CONFIRM_DELETE="DELETE",
    )

    with pytest.raises(zbd.ConfigError, match="Permanent deletion requires"):
        zbd.validate_delete_config(config)


def test_delete_one_requires_meeting_uuid(tmp_path):
    config = make_config(tmp_path, MODE="delete_one")

    with pytest.raises(zbd.ConfigError, match="DELETE_MEETING_UUID"):
        zbd.delete_one(config, FakeDeleteClient())


class FakeDownloadResponse:
    ok = True
    status_code = 200

    def iter_content(self, chunk_size):
        yield b"abc"
        yield b"def"


class FakeDownloadClient:
    def __init__(self, meeting):
        self.meeting = meeting

    def get(self, url):
        return self.meeting

    def request(self, method, url, stream=False):
        assert method == "GET"
        assert stream is True
        return FakeDownloadResponse()


def test_retry_not_ready_downloads_and_clears_successful_uuid(tmp_path):
    config = make_config(tmp_path, GROUP_BY_USER=False)
    meeting = sample_meeting()
    meeting["recording_files"] = [meeting["recording_files"][0]]
    meeting["recording_files"][0]["file_size"] = 6
    conn = zbd.ensure_meetings_db(str(tmp_path / "meetings.db"))
    zbd.log_meeting_uuid(conn, "meeting/uuid==")

    zbd.run_retry_not_ready(config, FakeDownloadClient(meeting), conn, LocalStorage())

    assert zbd.read_logged_meeting_uuids(conn) == []
    assert len(list(tmp_path.rglob("*.MP4"))) == 1


def test_redacts_access_tokens_from_text_and_urls():
    text = "https://example.com/file?access_token=secret-token&x=1 Bearer abc.def"

    assert "secret-token" not in utils.redact_sensitive_text(text)
    assert "abc.def" not in utils.redact_sensitive_text(text)
    assert "secret-token" not in utils.redact_url(text.split()[0])


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
