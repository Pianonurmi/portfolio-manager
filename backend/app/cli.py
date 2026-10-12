"""Command-line helpers: `python -m app.cli <command>`."""

import sys

from cryptography.fernet import Fernet


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("commands: gen-key | run-job <name> | reset-password <username>")
        return 0
    cmd = argv[0]
    if cmd == "gen-key":
        print(Fernet.generate_key().decode())
        return 0
    if cmd == "run-job":
        from app.main import load_modules
        from app.platform.jobs import run_now

        load_modules()
        run = run_now(argv[1], trigger="manual")
        print(run.status, run.summary or "")
        return 0 if run.status != "failed" else 1
    if cmd == "reset-password":
        import getpass

        from sqlalchemy import select

        from app.platform.db import session_factory
        from app.platform.models import AppUser
        from app.platform.security import hash_password

        pw = getpass.getpass("New password (min 12 chars): ")
        if len(pw) < 12:
            print("too short")
            return 1
        with session_factory()() as db:
            u = db.scalar(select(AppUser).where(AppUser.username == argv[1]))
            if u is None:
                print("no such user")
                return 1
            u.password_hash = hash_password(pw)
            db.commit()
        print("password updated")
        return 0
    print(f"unknown command {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
