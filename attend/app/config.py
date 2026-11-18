"""Runtime configuration, read from environment variables."""
import os
import secrets
from zoneinfo import ZoneInfo

DB_URL = os.environ.get("ATTEND_DB_URL", "sqlite:///./attend.db")
TZ = ZoneInfo(os.environ.get("ATTEND_TZ", "Asia/Amman"))
# Set ATTEND_SECRET in production; a random one logs everyone out on restart.
SECRET = os.environ.get("ATTEND_SECRET") or secrets.token_hex(32)
HTTPS_ONLY = os.environ.get("ATTEND_HTTPS_ONLY", "0") == "1"
# How old an offline-queued event may be when it finally syncs.
OFFLINE_MAX_HOURS = int(os.environ.get("ATTEND_OFFLINE_MAX_HOURS", "72"))
