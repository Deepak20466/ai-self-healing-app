"""Generate an argon2 hash for `ADMIN_PASSWORD_HASH` in `.env`.

Usage: `python scripts/hash_password.py` (prompts for the password, hidden
input via `getpass`) or `python scripts/hash_password.py 'my-password'`
(argv, only for local scripting/CI — avoid typing real passwords into shell
history interactively).
"""

from __future__ import annotations

import getpass
import sys

from argon2 import PasswordHasher


def main() -> None:
    if len(sys.argv) > 1:
        password = sys.argv[1]
    else:
        password = getpass.getpass("Admin password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords did not match.", file=sys.stderr)
            raise SystemExit(1)

    if not password:
        print("Password must not be empty.", file=sys.stderr)
        raise SystemExit(1)

    hasher = PasswordHasher()
    print(hasher.hash(password))


if __name__ == "__main__":
    main()
