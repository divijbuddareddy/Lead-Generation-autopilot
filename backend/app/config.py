"""Runtime configuration.

Everything sensitive (JWT signing key, master encryption key) is read from the
environment, or generated once and stored under ./data with restricted use.
Provider API keys are NOT configured here - they are entered by admins in the
app and stored encrypted in the database (see services/secrets.py).
"""
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("GTM_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
FRONTEND_DIR = ROOT / "frontend"


def _persisted(name: str, factory) -> str:
    env = os.getenv(name)
    if env:
        return env
    path = DATA_DIR / f".{name.lower()}"
    if path.exists():
        return path.read_text().strip()
    value = factory()
    path.write_text(value)
    return value


DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'gtm.db').as_posix()}")
if DATABASE_URL.startswith("postgres://"):   # Render/Heroku style -> SQLAlchemy dialect name
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
JWT_SECRET = _persisted("GTM_JWT_SECRET", lambda: secrets.token_urlsafe(48))
# Envelope-encryption master key for stored credentials (use a KMS in production).
MASTER_KEY = _persisted("GTM_MASTER_KEY", lambda: Fernet.generate_key().decode())
JWT_TTL_HOURS = int(os.getenv("GTM_JWT_TTL_HOURS", "12"))

# Bootstrap accounts. No passwords live in code: if these env vars are unset, a random
# one-time password is generated on first start, printed once to the console, and must be
# changed at first login. Use `python -m app.reset_password <email>` if one is lost.
SUPERADMIN_EMAIL = os.getenv("GTM_SUPERADMIN_EMAIL", "superadmin@aiautomationlabs.local")
SUPERADMIN_PASSWORD = os.getenv("GTM_SUPERADMIN_PASSWORD") or None
OWNER_EMAIL = os.getenv("GTM_OWNER_EMAIL", "owner@aiautomationlabs.local")
OWNER_PASSWORD = os.getenv("GTM_OWNER_PASSWORD") or None
SEED_DEMO_USERS = os.getenv("GTM_SEED_DEMO_USERS", "1") == "1"
# Provider API keys (AI, SMTP, WhatsApp, Twilio, HubSpot...) are NOT configured here or in
# env vars: admins enter them in the app's encrypted credential vault.

# Background scheduler tick (seconds). Sequences use "sandbox minutes" = days
# when SANDBOX_TIME_COMPRESSION is on so the full loop can be demoed quickly.
WORKER_TICK_SECONDS = float(os.getenv("GTM_WORKER_TICK", "5"))
SANDBOX_TIME_COMPRESSION = os.getenv("GTM_TIME_COMPRESSION", "0") == "1"
# Demo mode (fictional companies, fake mailboxes, simulated replies) is OFF. Real data only.
# Only automated tests switch it on with GTM_DEMO_DATA=1.
DEMO_DATA = os.getenv("GTM_DEMO_DATA", "0") == "1"
# One-click demo sign-in buttons on the login page (no passwords involved). OFF unless set.
# WARNING: anyone who can open the site can then sign in as these roles.
DEMO_LOGIN = os.getenv("GTM_DEMO_LOGIN", "0") == "1"
# Which roles get a demo button (comma separated). Default: every role.
DEMO_LOGIN_ROLES = [r.strip() for r in os.getenv(
    "GTM_DEMO_LOGIN_ROLES", "owner,admin,gtm_manager,sales_user,viewer,platform_admin").split(",") if r.strip()]
