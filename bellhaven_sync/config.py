"""Configuration. Secrets come from the environment (or a local .env file)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv(ROOT / ".env")

SITE_BASE = os.environ.get("SITE_BASE", "https://analyst-assessment-production.up.railway.app").rstrip("/")
CRM_BASE = os.environ.get("CRM_BASE", "https://analyst-assessment-production.up.railway.app/api/v1").rstrip("/")
CRM_TOKEN = os.environ.get("CRM_TOKEN", "")

STATE_DB = Path(os.environ.get("STATE_DB", ROOT / "state" / "proposals.db"))
DATA_DIR = ROOT / "data"

# The operator we are syncing. The parent account is resolved by name at runtime.
OPERATOR_NAME = "Bellhaven"
PARENT_ACCOUNT_NAME = "Bellhaven Senior Living (Parent Account)"

# Parents whose portfolios Bellhaven absorbed, newest acquisition first. Used only as a
# tie-breaker when several CRM records describe the same building: the record held by
# the most recent prior owner is the most current one and survives the merge.
PRIOR_OWNERS_NEWEST_FIRST = ["Cedar Trail Communities", "Harborview Care Group"]

# Website care offering label -> CRM care_type value
CARE_TYPE_MAP = {
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "independent living": "Independent Living",
}

# Matching thresholds
CONFIDENT_SCORE = 0.75
POSSIBLE_SCORE = 0.40

NOTE_TAG = "[bellhaven-sync]"

# Abort a run if the scrape returns fewer locations than this fraction of the Active
# Bellhaven facilities already in the CRM (protects against outages / layout changes).
MIN_SCRAPE_RATIO = 0.5

# macOS system Python links against LibreSSL; the urllib3 warning is noise for this tool.
import warnings as _w
_w.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")
