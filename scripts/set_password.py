"""Set (or clear) an employee's sign-in password.

There is no bootstrap/default credential anywhere in this platform, by design
- an account with no password simply cannot sign in with one. This script is
how the first password gets set, run by a human who already has database
access.

The password is never taken from the command line: an argument would land in
your shell history, in `ps` output, and in any terminal recording. It is
prompted for (hidden), or read from the SAD_SET_PASSWORD environment variable
for non-interactive use. Only the scrypt hash is ever written.

    # interactive - prompts twice, echoes nothing
    .venv\\Scripts\\python.exe scripts\\set_password.py E1001

    # non-interactive
    $env:SAD_SET_PASSWORD = "..."; .venv\\Scripts\\python.exe scripts\\set_password.py E1001

    # revoke password sign-in for an account
    .venv\\Scripts\\python.exe scripts\\set_password.py E1001 --clear
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai-service"))

from app.repositories import employee_repository  # noqa: E402
from app.security.passwords import hash_password  # noqa: E402

MIN_LENGTH = 12


def _read_password() -> str:
    from_env = os.environ.get("SAD_SET_PASSWORD")
    if from_env:
        return from_env
    if not sys.stdin.isatty():
        print("error: no TTY and SAD_SET_PASSWORD is not set", file=sys.stderr)
        raise SystemExit(2)
    first = getpass.getpass("New password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        print("error: passwords do not match", file=sys.stderr)
        raise SystemExit(2)
    return first


def main() -> int:
    parser = argparse.ArgumentParser(description="Set or clear an employee's sign-in password.")
    parser.add_argument("employee", help="Employee number (E1001) or email address")
    parser.add_argument("--clear", action="store_true", help="Remove the password instead of setting one")
    args = parser.parse_args()

    employee = employee_repository.get_by_login(args.employee)
    if employee is None:
        print(f"error: no employee matching {args.employee!r}", file=sys.stderr)
        return 1
    if not employee.IsActive:
        print(f"error: {employee.EmployeeNumber} is not active", file=sys.stderr)
        return 1

    if args.clear:
        employee_repository.clear_password_hash(employee.EmployeeId)
        print(f"cleared password for {employee.EmployeeNumber} ({employee.DisplayName}) - "
              f"this account can no longer sign in with a password")
        return 0

    password = _read_password()
    if len(password) < MIN_LENGTH:
        print(f"error: password must be at least {MIN_LENGTH} characters", file=sys.stderr)
        return 2

    employee_repository.set_password_hash(employee.EmployeeId, hash_password(password))
    print(f"password set for {employee.EmployeeNumber} ({employee.DisplayName}) <{employee.Email}>")
    print("sign in with that employee number or email at the UI's login screen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
