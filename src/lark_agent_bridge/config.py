"""Centralised configuration, loaded once from environment / .env.

Everything the bot can tune lives here as a single ``Settings`` instance so the
rest of the package imports ``settings`` instead of touching ``os.environ``
directly. A tiny ``.env`` loader is included so there's no hard dependency on
python-dotenv (though it's used if present).
"""

from __future__ import annotations

import os
import time
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Minimal ``.env`` loader so no extra dependency is strictly required.

    Lines are ``KEY=value``; ``#`` comments and blank lines are ignored.
    Existing environment variables are never overwritten (``setdefault``).
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(*keys: str, default: str = "") -> str:
    """Return the first set environment variable among *keys* (fallback chain)."""
    for key in keys:
        val = os.getenv(key)
        if val is not None:
            return val
    return default


class Settings:
    """Resolved runtime configuration.

    Instantiate once (see module-level ``settings``). Attributes marked
    *mutable at runtime* can be changed from chat via slash commands; they live
    on this object so a single source of truth is shared across the bot.
    """

    def __init__(self, env_path: Path | None = None) -> None:
        if env_path is not None:
            load_dotenv(env_path)

        # --- Lark credentials ------------------------------------------------
        self.app_id: str = os.environ["LARK_APP_ID"]
        self.app_secret: str = os.environ["LARK_APP_SECRET"]
        # "lark" -> open.larksuite.com (international); "feishu" -> open.feishu.cn
        self.domain_key: str = os.getenv("LARK_DOMAIN", "lark").strip().lower()

        # --- Access control --------------------------------------------------
        # Only these Lark users (open_id, comma separated) may talk to the bot.
        # The agent can run tools on this machine, so never leave this open in
        # production. Empty => bot stays locked and just reports your open_id.
        self.allowed_open_ids: set[str] = {
            x.strip() for x in os.getenv("ALLOWED_OPEN_IDS", "").split(",") if x.strip()
        }

        # In group chats, only reply when THIS bot is @mentioned specifically
        # (not on @all or mentions of other people). The bot's own open_id is
        # auto-resolved from the API at startup; set BOT_OPEN_ID to override.
        # Set GROUP_REQUIRE_BOT_MENTION=0 to revert to the old "any mention".
        self.bot_open_id: str = os.getenv("BOT_OPEN_ID", "").strip()
        self.group_require_bot_mention: bool = (
            os.getenv("GROUP_REQUIRE_BOT_MENTION", "1").strip() != "0"
        )
        # Filled at startup (env value or API lookup); shared across the bot.
        self.bot_open_id_resolved: str = ""

        # --- Agent execution -------------------------------------------------
        self.workdir: Path = Path(os.getenv("AGENT_WORKDIR", os.getenv("KIRO_WORKDIR", str(Path.cwd())))).resolve()
        # Tools the agent may use without asking: "read,grep" (read-only),
        # a custom comma list, or "all". (kiro supports granular lists; agy is
        # all-or-nothing — any non-empty value trusts everything.)
        self.trust_tools: str = _env("AGENT_TRUST_TOOLS", "KIRO_TRUST_TOOLS", default="read,grep")
        self.agent_name: str = _env("AGENT_NAME", "KIRO_AGENT", default="")

        # --- Engine selection ------------------------------------------------
        # Which backend drives the bot: "kiro" (kiro-cli) or "agy" (antigravity).
        self.default_engine: str = os.getenv("AGENT_ENGINE", "kiro").strip().lower()
        # Optional explicit binary paths; auto-detected from PATH when empty.
        self.kiro_bin: str = os.getenv("KIRO_BIN", "")
        self.agy_bin: str = os.getenv("AGY_BIN", "")

        # --- Transcript ------------------------------------------------------
        default_transcripts = Path(__file__).resolve().parent.parent.parent / "transcripts"
        self.transcript_dir: Path = Path(os.getenv("LARK_TRANSCRIPT_DIR", str(default_transcripts)))

        # --- UI / behaviour --------------------------------------------------
        self.max_chunk: int = 3500  # chars per Lark message
        # How many recent transcript entries to inject as context on an engine
        # switch (≈ 2 entries per turn). 0 disables.
        self.engine_switch_context_lines: int = int(os.getenv("ENGINE_SWITCH_CONTEXT_LINES", "20"))
        # Reaction shown on the user's message while the agent works.
        self.typing_emoji: str = os.getenv("LARK_TYPING_EMOJI", "Typing")

        # --- Mutable at runtime (changed via slash commands) -----------------
        self.timeout_sec: int = int(_env("AGENT_TIMEOUT_SEC", "KIRO_TIMEOUT_SEC", default="600"))
        # "final" = only the agent's final answer; "all" = every text segment.
        self.reply_mode: str = _env("AGENT_REPLY_MODE", "KIRO_REPLY_MODE", default="final")
        self.agy_model: str = os.getenv("AGY_MODEL", "")      # empty => agy's own default
        self.agy_effort: str = os.getenv("AGY_EFFORT", "")    # low|medium|high|xhigh|max

        # --- Observability (log access tool; additive, off by default) -------
        # When enabled, /logs and /projects can query OpenObserve/Grafana using
        # a project registry (pointer per project) and an encrypted secret store.
        self.enable_observability: bool = os.getenv("ENABLE_OBSERVABILITY", "0").strip() == "1"
        self.observability_registry: str = os.getenv("OBSERVABILITY_REGISTRY", "")
        self.openobserve_base_url: str = os.getenv("OPENOBSERVE_BASE_URL", "").rstrip("/")
        # Encrypted secret store location + key source (key lives OUTSIDE repo).
        self.secrets_path: str = os.getenv("LAB_SECRETS_PATH", "")
        self.secret_key_file: str = os.getenv("LAB_SECRET_KEY_FILE", "")
        # Default max log rows returned to chat (hard-capped in the client too).
        self.logs_default_limit: int = int(os.getenv("LOGS_DEFAULT_LIMIT", "50"))
        # Timezone (hours offset from UTC) used to interpret absolute timestamps
        # typed in `/logs ... from <date> to <date>`. OpenObserve stores UTC;
        # people type wall-clock time. Default 7 = WIB (Asia/Jakarta).
        self.logs_tz_offset_hours: float = float(os.getenv("LOGS_TZ_OFFSET_HOURS", "7"))

        # --- Process metadata ------------------------------------------------
        self.start_time: float = time.time()  # for /status uptime


def default_env_path() -> Path:
    """Return the conventional ``.env`` location (repo root)."""
    return Path(__file__).resolve().parent.parent.parent / ".env"
