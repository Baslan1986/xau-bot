"""Runtime configuration. Everything comes from the environment; nothing secret lives in the repo.

Nothing here is plant-specific: timezone, shifts, polygons and locale come from plant profiles.
"""
import os
import secrets

DB_URL = os.environ.get("ATTEND_DB_URL", "sqlite:///./attend.db")
# Session signing key. Must be set in production; a random one logs everyone out on restart.
SECRET = os.environ.get("ATTEND_SECRET") or secrets.token_hex(32)
HTTPS_ONLY = os.environ.get("ATTEND_HTTPS_ONLY", "0") == "1"
OFFLINE_MAX_HOURS = int(os.environ.get("ATTEND_OFFLINE_MAX_HOURS", "72"))

# Where people and leave live:
#   "local"    standalone mode (demo / dev / tests): Attend keeps its own people + leave workflow
#   "solnerve" SolNerve is the single source; Attend's local people/leave writes are disabled and
#              data arrives through /api/integration (see docs/ATTEND_API.md)
PEOPLE_SOURCE = os.environ.get("ATTEND_PEOPLE_SOURCE", "local")
SOLNERVE_URL = os.environ.get("ATTEND_SOLNERVE_URL", "")  # shown to staff for leave in solnerve mode

# Machine-to-machine token for /api/integration/*. Integration is disabled when unset.
INTEGRATION_TOKEN = os.environ.get("ATTEND_INTEGRATION_TOKEN", "")

# Outbound notification hook (Shams delivers over WhatsApp). In-app only when unset.
NOTIFY_URL = os.environ.get("ATTEND_NOTIFY_URL", "")
NOTIFY_SECRET = os.environ.get("ATTEND_NOTIFY_SECRET", "")  # HMAC-SHA256 signing key
NOTIFY_MAX_ATTEMPTS = int(os.environ.get("ATTEND_NOTIFY_MAX_ATTEMPTS", "20"))

# Optional directory of plant profile JSON files, upserted at startup.
PROFILES_DIR = os.environ.get("ATTEND_PROFILES_DIR", "")
# Background tick (seconds) for notification delivery and catch-up jobs. 0 disables (tests).
TICK_SECONDS = int(os.environ.get("ATTEND_TICK_SECONDS", "20"))
