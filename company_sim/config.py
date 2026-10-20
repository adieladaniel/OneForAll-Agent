"""Static configuration for the simulated company environment."""
from __future__ import annotations

import os
import secrets
from datetime import date, datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(os.environ.get("SIM_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "sim.db"
PDF_DIR = DATA_DIR / "pdfs"
WORKSPACE_DIR = BASE_DIR.parent / "workspace"

HOST = os.environ.get("SIM_HOST", "127.0.0.1")
PORT = int(os.environ.get("SIM_PORT", "8001"))
BASE_URL = f"http://localhost:{PORT}"

# "Today" inside the simulation. Seed dates are computed relative to it.
SIM_TODAY: date = (
    date.fromisoformat(os.environ["SIM_TODAY"]) if os.environ.get("SIM_TODAY") else date.today()
)

# Identity of this simulator process, shown on /healthz and /admin/state. Random per process start unless SIM_INSTANCE_ID
# is set, so a client (the eval harness) can tell this simulator from a stale or second one answering on the same port.
INSTANCE_ID: str = os.environ.get("SIM_INSTANCE_ID", "").strip() or secrets.token_hex(8)
STARTED_AT: str = datetime.now(timezone.utc).isoformat(timespec="seconds")

SESSION_SECRET = "company-sim-dev-secret"  # dev-only; signs session cookies

# Credentials (also written to workspace/credentials.json by the seeder).
MAIL_USER, MAIL_PASSWORD = "ap@acme.example", "mail-pass-123"
PORTAL_USER, PORTAL_PASSWORD = "acme-ap", "portal-pass-456"
ERP_USER, ERP_PASSWORD = "ap.clerk", "ledger-pass-789"
ERP_API_KEY = "ledger-api-key-001"

def tenant_secret() -> str:
    """``SIM_TENANT_SECRET``: when set, a tenant's entry token, API key and passwords are derived from it and the tenant id
    (the same id gives the same secrets in every simulator that shares the secret). Read at call time; empty = random."""
    return os.environ.get("SIM_TENANT_SECRET", "").strip()


PAGE_SIZE = 5  # vendor portal invoices per page
