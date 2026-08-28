#!/usr/bin/env python3
"""Lag en passordhash for admin_users.yaml.

Bruk:  python3 scripts/hash_password.py
Spør interaktivt etter passord (vises ikke) og skriver ut hashen som limes
inn i admin_users.yaml:

    users:
      <brukernavn>: <hash>
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.auth import hash_password  # noqa: E402

if __name__ == "__main__":
    password = getpass.getpass("Passord: ")
    confirm = getpass.getpass("Gjenta: ")
    if password != confirm:
        sys.exit("Passordene er ulike.")
    if len(password) < 8:
        sys.exit("Minst 8 tegn.")
    print(hash_password(password))
