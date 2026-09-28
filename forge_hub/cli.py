"""Admin bootstrap: python -m forge_hub.cli create-admin NAME [forge1|forge2]."""

import getpass
import os
import sys

from .settings import WORKER_IDS, load_settings
from .store import Store


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "set-password":
        store = Store(load_settings().database_path)
        store.initialize()
        account = next((item for item in store.list_accounts() if item["username"] == sys.argv[2]), None)
        if account is None:
            print("Account not found", file=sys.stderr)
            return 1
        password = getpass.getpass("New account password (at least 1 character): ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            print("Passwords do not match", file=sys.stderr)
            return 1
        try:
            store.reset_password(account["id"], password)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Password updated for {account['username']}; active sessions were revoked.")
        return 0

    if len(sys.argv) == 3 and sys.argv[1] == "reset-bootstrap-password":
        if os.environ.get("FORGE_HUB_PRIVATE_BOOTSTRAP") != "1":
            print("This command requires FORGE_HUB_PRIVATE_BOOTSTRAP=1", file=sys.stderr)
            return 2
        store = Store(load_settings().database_path)
        store.initialize()
        account = next((item for item in store.list_accounts() if item["username"] == sys.argv[2]), None)
        if account is None or account["role"] != "admin":
            print("Admin account not found", file=sys.stderr)
            return 1
        password = getpass.getpass("Temporary admin password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            print("Passwords do not match", file=sys.stderr)
            return 1
        try:
            store.reset_password(account["id"], password, temporary=True)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Reset {account['username']}; password change is required at next login.")
        return 0

    if len(sys.argv) not in (3, 4) or sys.argv[1] != "create-admin":
        print("Usage: python -m forge_hub.cli create-admin USERNAME [forge1|forge2]", file=sys.stderr)
        print("   or: python -m forge_hub.cli set-password USERNAME", file=sys.stderr)
        print("   or: FORGE_HUB_PRIVATE_BOOTSTRAP=1 python -m forge_hub.cli reset-bootstrap-password USERNAME", file=sys.stderr)
        return 2
    worker_id = sys.argv[3] if len(sys.argv) == 4 else "forge1"
    if worker_id not in WORKER_IDS:
        print("Worker must be forge1 or forge2", file=sys.stderr)
        return 2
    password = getpass.getpass("New admin password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        print("Passwords do not match", file=sys.stderr)
        return 1
    store = Store(load_settings().database_path)
    store.initialize()
    try:
        temporary = len(password) < 12 and os.environ.get("FORGE_HUB_PRIVATE_BOOTSTRAP") == "1"
        account = store.create_account(sys.argv[2], password, "admin", worker_id, temporary=temporary)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Created admin {account['username']} assigned to {account['worker_id']}")
    if account["must_change_password"]:
        print("Temporary password: changing it is required before Forge access.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
