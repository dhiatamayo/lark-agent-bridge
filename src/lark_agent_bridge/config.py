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
        # Which backend drives the bot: "kiro" (kiro-cli), "agy" (antigravity),
        # or "copilot" (GitHub Copilot CLI).
        self.default_engine: str = os.getenv("AGENT_ENGINE", "kiro").strip().lower()
        # Optional explicit binary paths; auto-detected from PATH when empty.
        self.kiro_bin: str = os.getenv("KIRO_BIN", "")
        self.agy_bin: str = os.getenv("AGY_BIN", "")
        self.copilot_bin: str = os.getenv("COPILOT_BIN", "")

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
        # Streaming progress: post an editable card and patch it in place with
        # "what the agent is doing right now" labels while it works, so long
        # runs don't look dead. Additive; off by default (blocking path intact).
        self.enable_streaming: bool = os.getenv("ENABLE_STREAMING", "0").strip() == "1"

        # --- Mutable at runtime (changed via slash commands) -----------------
        self.timeout_sec: int = int(_env("AGENT_TIMEOUT_SEC", "KIRO_TIMEOUT_SEC", default="600"))
        # "final" = only the agent's final answer; "all" = every text segment.
        self.reply_mode: str = _env("AGENT_REPLY_MODE", "KIRO_REPLY_MODE", default="final")
        self.agy_model: str = os.getenv("AGY_MODEL", "")      # empty => agy's own default
        self.agy_effort: str = os.getenv("AGY_EFFORT", "")    # low|medium|high|xhigh|max
        self.copilot_model: str = os.getenv("COPILOT_MODEL", "")   # empty => copilot's default (e.g. 'auto')
        self.copilot_effort: str = os.getenv("COPILOT_EFFORT", "")  # none|minimal|low|medium|high|xhigh|max

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

        # --- Jira (read-only ticket triage; additive, off by default) --------
        # When enabled, /jira can run read-only JQL searches against Jira Cloud
        # using the encrypted secret store (jira.email / jira.token). The base
        # URL and default project/JQL are plain config so switching boards needs
        # no code change. Fail-open: disabled / misconfigured => bot runs as before.
        self.enable_jira: bool = os.getenv("ENABLE_JIRA", "0").strip() == "1"
        # e.g. https://dxtr-asia.atlassian.net
        self.jira_base_url: str = os.getenv("JIRA_BASE_URL", "").rstrip("/")
        # Default project key used when no JQL is given (e.g. SIT). Configurable
        # so moving boards/projects is an env change, not a code change.
        self.jira_default_project: str = os.getenv("JIRA_DEFAULT_PROJECT", "").strip()
        # Optional full default JQL (wins over the project shortcut if set).
        self.jira_default_jql: str = os.getenv("JIRA_DEFAULT_JQL", "").strip()
        # Default max issues returned (hard-capped at 100 in the client too).
        self.jira_default_limit: int = int(os.getenv("JIRA_DEFAULT_LIMIT", "30"))
        # Team roster (Jira displayNames, comma separated) so a lead can query
        # the whole team's tickets. Powers the `team` and `@name` JQL shortcuts
        # (see JiraService). Plain config — add/remove a member by editing .env,
        # no code change. Empty => the shortcuts are unavailable (clear error).
        self.jira_team_members: list[str] = [
            x.strip()
            for x in os.getenv("JIRA_TEAM_MEMBERS", "").split(",")
            if x.strip()
        ]
        # Optional nickname -> displayName aliases for the `@name` shortcut, so a
        # lead can type `@billy` even if Jira shows "Billisany Akhyar". Format:
        # "alias=Display Name" pairs, comma separated. No code change to tweak.
        self.jira_team_aliases: dict[str, str] = {}
        for pair in os.getenv("JIRA_TEAM_ALIASES", "").split(","):
            pair = pair.strip()
            if "=" in pair:
                alias, name = pair.split("=", 1)
                if alias.strip() and name.strip():
                    self.jira_team_aliases[alias.strip()] = name.strip()

        # --- Knowledge retrieval (Postgres FTS; additive, off by default) ----
        # When enabled, each request retrieves the most relevant chunks of past
        # knowledge (LOG_SESSIONS, BACKLOG, etc.) from Postgres and injects them
        # into the prompt, instead of loading the whole (large) markdown. Keeps
        # knowledge searchable without blowing up context size every turn.
        # Fail-open: if disabled / DB down / query fails, the bot runs normally.
        self.enable_knowledge: bool = os.getenv("ENABLE_KNOWLEDGE", "0").strip() == "1"
        # DSN, e.g. postgresql://andre:***@localhost:5432/andre_context
        self.knowledge_db_dsn: str = os.getenv("PG_CONTEXT_DSN", os.getenv("KNOWLEDGE_DB_DSN", ""))
        # How many chunks to retrieve and inject per request.
        self.knowledge_top_k: int = int(os.getenv("KNOWLEDGE_TOP_K", "6"))
        # Auto-sync the DB with the source markdown on startup (idempotent;
        # fail-open). Defaults ON whenever knowledge is enabled so the DB never
        # drifts behind the files after a memory update + restart. Set
        # KNOWLEDGE_AUTO_INGEST=0 to skip (e.g. for a faster startup).
        self.knowledge_auto_ingest: bool = (
            os.getenv("KNOWLEDGE_AUTO_INGEST", "1").strip() != "0"
        )
        # Markdown files (under the steering dir) to ingest into Postgres. These
        # are the large/situational ones; small always-load REF_* stay in files.
        self.knowledge_sources: list[str] = [
            x.strip()
            for x in os.getenv(
                "KNOWLEDGE_SOURCES",
                "LOG_SESSIONS.md,BACKLOG.md,SKILL_LOGS.md,FSD_EXECUTION_PLAN.md",
            ).split(",")
            if x.strip()
        ]
        # Where the source markdown lives (the Kiro steering dir).
        self.knowledge_steering_dir: str = os.getenv(
            "KNOWLEDGE_STEERING_DIR", str(self.workdir / ".kiro" / "steering")
        )

        # --- Process metadata ------------------------------------------------
        self.start_time: float = time.time()  # for /status uptime


def default_env_path() -> Path:
    """Return the conventional ``.env`` location (repo root)."""
    return Path(__file__).resolve().parent.parent.parent / ".env"
