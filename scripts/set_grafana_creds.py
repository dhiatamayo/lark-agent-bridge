"""Store Grafana credentials in the encrypted secret store (hidden prompt).

Run locally; the password is never echoed or printed:
    .\\.venv\\Scripts\\python.exe scripts\\set_grafana_creds.py
"""

from __future__ import annotations

import getpass

from lark_agent_bridge.config import Settings, default_env_path
from lark_agent_bridge.secrets import SecretStore


def main() -> int:
    settings = Settings(default_env_path())
    store = SecretStore(settings.secrets_path, key_file=settings.secret_key_file or None)
    user = input("Grafana username/email: ").strip()
    password = getpass.getpass("Grafana password (hidden): ")
    if not user or not password:
        print("aborted: empty username or password")
        return 1
    store.set_many({"grafana.user": user, "grafana.password": password})
    store.save()
    print("saved to secrets.enc (grafana.user, grafana.password)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
