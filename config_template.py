# Zoom API credentials.
ACCOUNT_ID = R"##########"
CLIENT_ID = R"##########"
CLIENT_SECRET = R"##########"


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


# Tool mode.
# - "download": scan, check destination free space, and download matched recordings.
# - "estimate": scan and report size/free-space requirements without downloading.
# - "retry_not_ready": retry meeting UUIDs logged in meetings.db and download them.
# - "delete_bulk": delete matched Zoom cloud recordings using the filters below.
# - "delete_one": delete one meeting recording set or one recording file by ID.
MODE = "download"


# Put your own download path here, no need to escape backslashes but avoid ending with one.
OUTPUT_PATH = R"C:\Test\Zoom"


# Date range (inclusive) for downloads, None value for Days gets replaced by first/last day of the month.
START_DAY, START_MONTH, START_YEAR = None, 5, 2020
END_DAY, END_MONTH, END_YEAR = None, 3, 2022

# Put here emails of the users you want to check for recordings. If empty, all users under the account will be checked.
USERS = [
    # R"####@####.####",
    # R"####@####.####",
]

# Put here the topics of the meetings you wish to download recordings for. If empty, no topic filtering will happen.
TOPICS = [
    # R"############",
    # R"############",
]

# Put here the file types you wish to download. If empty, no file type filtering will happen.
RECORDING_FILE_TYPES = [
    # R"MP4",            # Video file of the recording.
    # R"M4A",            # Audio-only file of the recording.
    # R"TIMELINE",       # Timestamp file of the recording in JSON file format.
    # R"TRANSCRIPT",     # Transcription file of the recording in VTT format.
    # R"CHAT",           # A TXT file containing in-meeting chat messages that were sent during the meeting.
    # R"CC",             # File containing closed captions of the recording in VTT file format.
    # R"CSV",            # File containing polling data in CSV format.
    # R"SUMMARY",        # Summary file of the recording in JSON file format.
]

# If True, recordings will be grouped in folders by their owning user.
GROUP_BY_USER = True

# If True, recordings will be grouped in folders by their topics.
GROUP_BY_TOPIC = True

# If True, each instance of recording will be in its own folder (which may contain multiple files).
# Note: One "meeting" can have multiple recording instances.
GROUP_BY_RECORDING = False

# If True, participant audio files will be downloaded as well.
# This works when "Record a separate audio file of each participant" is enabled.
INCLUDE_PARTICIPANT_AUDIO = True

# Set to True for more verbose output.
VERBOSE_OUTPUT = False


# If True, downloads fail before the first file when the destination drive does not have enough free space
# for all missing matched files plus MINIMUM_FREE_DISK. If False, the script prints a warning and falls back
# to waiting before each individual file.
FAIL_IF_NOT_ENOUGH_SPACE = True


# Deletion safety settings. Deletion is dry-run by default.
DRY_RUN = True

# Required for real trash deletion: CONFIRM_DELETE = "DELETE"
# Required for permanent deletion: CONFIRM_DELETE = "DELETE PERMANENTLY"
CONFIRM_DELETE = ""

# Use "trash" to move recordings to Zoom trash, or "delete" for permanent deletion.
DELETE_ACTION = "trash"

# Permanent deletion requires DELETE_ACTION = "delete", ALLOW_PERMANENT_DELETE = True,
# and CONFIRM_DELETE = "DELETE PERMANENTLY".
ALLOW_PERMANENT_DELETE = False

# In delete_bulk mode, use "files" to delete matched recording files individually or "meetings"
# to delete each matched meeting's full recording set.
DELETE_SCOPE = "files"

# Used only when MODE = "delete_one". If DELETE_RECORDING_ID is empty, the meeting's full
# recording set is deleted. If DELETE_RECORDING_ID is set, only that recording file is deleted.
DELETE_MEETING_UUID = None
DELETE_RECORDING_ID = None

# Constants used for indicating size in bytes.
B = 1
KB = 1024 * B
MB = 1024 * KB
GB = 1024 * MB
TB = 1024 * GB

# Minimum free disk space in bytes for downloads to happen, downloading will be stalled if disk space is
# expected to get below this amount as a result of the new file.
MINIMUM_FREE_DISK = 1 * GB

# Tolerance for recording files size mismatch between the declared size in Zoom Servers and the files
# actually downloaded from the server.
# This was observed to happen sometimes on google drive mounted storage (mismatches of < 300 KBs).
# Note: High tolerance might cause issues like corrupt downloads not being recognized by script.
FILE_SIZE_MISMATCH_TOLERANCE = 0 * KB
