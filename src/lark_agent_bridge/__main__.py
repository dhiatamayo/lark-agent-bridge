"""Entry point: ``python -m lark_agent_bridge`` or the ``lark-agent-bridge`` script."""

from __future__ import annotations

import logging

from .bot import Bot
from .config import Settings, default_env_path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings(env_path=default_env_path())
    Bot(settings).run()


if __name__ == "__main__":
    main()
