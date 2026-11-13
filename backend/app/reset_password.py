"""Reset a user's password from the server console (for lost first-run passwords).

Usage (from the backend folder):  python -m app.reset_password owner@aiautomationlabs.local
Prints a new one-time password; the user must change it at next login.
"""
import secrets
import sys

from sqlalchemy import func, select

from . import models as m
from .database import Base, SessionLocal, engine
from .security import hash_password
from .services import core


def main():
    if len(sys.argv) != 2:
        print("Usage: python -m app.reset_password <email>")
        sys.exit(1)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        user = db.scalars(select(m.User).where(func.lower(m.User.email) == sys.argv[1].lower())).first()
        if not user:
            print(f"No user with email {sys.argv[1]}")
            sys.exit(1)
        pw = secrets.token_urlsafe(12)
        user.password_hash, user.must_change_password, user.is_active = hash_password(pw), True, True
        core.audit(db, None, "auth.password_reset_console", actor_type="system", entity_type="user", entity_id=user.id)
        db.commit()
        print(f"One-time password for {user.email}: {pw}\n(It must be changed at next login.)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
