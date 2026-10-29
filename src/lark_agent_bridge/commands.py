"""Slash-command routing.

:func:`dispatch` inspects a user's message. If it is a slash command it is
handled here and the reply text is returned. If it is a normal message,
``None`` is returned so the caller forwards it to the agent. Keeping this as a
pure function of ``(text, runner, transcript, settings)`` makes the command
surface easy to unit-test without any Lark connection.
"""

from __future__ import annotations

import time

from .config import Settings
from .engines import EngineNotFound
from .runner import AgentRunner
from .transcript import TranscriptStore

# agy's advertised model list (shown by /models). Update as the CLI evolves.
AGY_KNOWN_MODELS = [
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-low",
    "gemini-3.7-flash-high",
    "gemini-3.7-flash-medium",
    "gemini-3.1-pro-high",
    "gemini-3.1-pro-low",
    "claude-sonnet-4-6",
    "claude-opus-4-6-thinking",
    "gpt-oss-120b-medium",
]

# copilot's commonly-available models (shown by /models). The exact set depends
# on your Copilot plan and changes over time; `--model` accepts any string, and
# `auto` lets Copilot pick. Update as the CLI/plan evolves.
COPILOT_KNOWN_MODELS = [
    "auto",
    "claude-sonnet-5.5",
    "claude-sonnet-4.6",
    "claude-opus-4.6",
    "gpt-5.4",
    "gemini-3-pro",
]

# Reasoning-effort levels, per engine (used by /effort validation).
AGY_EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
COPILOT_EFFORT_LEVELS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]


def format_help() -> str:
    return (
        "🤖 **lark-agent-bridge — commands**\n\n"
        "**Session & conversation:**\n"
        "• `/new` or `/reset` — start a fresh conversation on the active engine\n"
        "• `/status` — bot overview (engine, model, session, uptime)\n"
        "• `/usage` — today's message stats from the local transcript\n"
        "• `/transcript [n]` — show the last [n] transcript entries today (default 5)\n\n"
        "**Engine & model:**\n"
        "• `/engine` — show the active engine (kiro / agy / copilot)\n"
        "• `/engine <kiro|agy|copilot>` — switch backend engine (+ inject recent context)\n"
        "• `/model` — show the active model on the current engine\n"
        "• `/model <name>` — change model (agy / copilot)\n"
        "• `/models` — list known models (agy / copilot)\n"
        "• `/effort [level]` — show/set reasoning effort (agy / copilot)\n\n"
        "**Operational:**\n"
        "• `/timeout [sec]` — show/adjust command timeout (default 600s)\n"
        "• `/reply [final|all]` — show/toggle reply mode\n"
        "• `/help` — show this message\n\n"
        "**Observability (logs):**\n"
        "• `/projects` — list projects wired for log access\n"
        "• `/logs <project> [window] [filter]` — read logs, e.g. `/logs mt4api-dev 15m error`\n"
        "• `/logs <project> from <date time> to <date time> [filter]` — exact range,\n"
        "  e.g. `/logs mt4api-dev from 2026-10-19 10:00:00 to 2026-10-19 11:00:00 error`\n"
        "• `/project-add <name> <org> <stream> [window]` — register an OpenObserve project\n"
        "• `/project-remove <name>` — unregister a project\n\n"
        "**Jira (tickets):**\n"
        "• `/jira` — open issues in the default project (stalest first)\n"
        "• `/jira <PROJECT>` — open issues in that project, e.g. `/jira SIT`\n"
        "• `/jira <JQL>` — run any JQL, e.g. "
        "`/jira assignee = currentUser() AND statusCategory != Done ORDER BY updated ASC`"
    )


def dispatch(text: str, runner: AgentRunner, transcript: TranscriptStore,
             settings: Settings) -> str | None:
    """Handle a slash command and return its reply, or ``None`` to forward the
    message to the agent."""
    raw = text.strip()
    cmd = raw.lower()
    if not cmd.startswith("/"):
        return None

    if cmd in ("/new", "/reset"):
        runner.reset_session()
        return f"Started a new conversation ({runner.engine.name})."

    if cmd in ("/help", "/start"):
        return format_help()

    if cmd == "/status":
        return _status(runner, transcript, settings)

    if cmd == "/usage":
        return _usage(transcript)

    if cmd == "/model" or cmd.startswith("/model "):
        return _model(raw, runner, settings)

    if cmd == "/models":
        if runner.engine.name == "agy":
            return "📋 **Available models for agy:**\n\n" + "\n".join(
                f"• `{m}`" for m in AGY_KNOWN_MODELS
            ) + "\n\nSelect with `/model <name>`."
        if runner.engine.name == "copilot":
            return ("📋 **Common models for copilot:**\n\n" + "\n".join(
                f"• `{m}`" for m in COPILOT_KNOWN_MODELS
            ) + "\n\nSelect with `/model <name>`. `auto` lets Copilot pick. "
                "The exact set depends on your Copilot plan and may change; "
                "`--model` accepts other names too.")
        return "Model list is only available for the *agy* and *copilot* engines."

    if cmd == "/effort" or cmd.startswith("/effort "):
        return _effort(raw, runner, settings)

    if cmd == "/timeout" or cmd.startswith("/timeout "):
        return _timeout(raw, settings)

    if cmd == "/reply" or cmd.startswith("/reply "):
        return _reply_mode(raw, settings)

    if cmd == "/transcript" or cmd.startswith("/transcript "):
        return _transcript(raw, transcript)

    if cmd == "/projects":
        return _projects(settings)

    if cmd == "/project-add" or cmd.startswith("/project-add "):
        return _project_add(raw, settings)

    if cmd == "/project-remove" or cmd.startswith("/project-remove "):
        return _project_remove(raw, settings)

    if cmd == "/logs" or cmd.startswith("/logs "):
        return _logs(raw, settings)

    if cmd == "/jira" or cmd.startswith("/jira "):
        return _jira(raw, settings)

    if cmd == "/engine" or cmd.startswith("/engine "):
        return _engine(cmd, runner, transcript, settings)

    # Unknown slash command: tell the user instead of forwarding to the agent.
    return f"Unknown command `{raw.split()[0]}`. Try `/help`."


# --------------------------------------------------------------------------- #
# Individual command handlers
# --------------------------------------------------------------------------- #
def _status(runner: AgentRunner, transcript: TranscriptStore, settings: Settings) -> str:
    uptime_sec = int(time.time() - settings.start_time)
    uptime_str = f"{uptime_sec // 3600}h {(uptime_sec % 3600) // 60}m {uptime_sec % 60}s"
    sid = runner.session_ids.get(runner.engine.name)
    if sid and len(sid) > 16:
        sid_str = f"`{sid[:16]}...`"
    elif sid:
        sid_str = f"`{sid}`"
    else:
        sid_str = "None (fresh)"

    if runner.engine.name == "agy":
        model_info = settings.agy_model or "default (cli)"
        effort_info = settings.agy_effort or "default"
    elif runner.engine.name == "copilot":
        model_info = settings.copilot_model or "default (cli)"
        effort_info = settings.copilot_effort or "default"
    else:
        model_info = "managed by Kiro profile"
        effort_info = "n/a"

    stats = transcript.usage_stats()
    return (
        "📊 **Bot status**\n\n"
        f"• **Active engine:** *{runner.engine.name}* (`{runner.engine.binary}`)\n"
        f"• **Model:** {model_info}\n"
        f"• **Effort:** {effort_info}\n"
        f"• **Session ID:** {sid_str}\n"
        f"• **Reply mode:** `{settings.reply_mode}`\n"
        f"• **Timeout:** `{settings.timeout_sec}s`\n"
        f"• **Uptime:** {uptime_str}\n"
        f"• **Messages today:** {stats['today_in']} in, {stats['today_out']} out "
        f"({stats['today_total']} total)"
    )


def _usage(transcript: TranscriptStore) -> str:
    stats = transcript.usage_stats()
    today = time.strftime("%Y-%m-%d")
    return (
        f"📈 **Usage statistics ({today})**\n\n"
        f"• **Inbound (user):** {stats['today_in']}\n"
        f"• **Outbound (bot):** {stats['today_out']}\n"
        f"• **Total today:** {stats['today_total']}\n\n"
        f"• **Days recorded:** {stats['total_files']}\n"
        f"• **Total transcript lines:** {stats['all_time_lines']}\n\n"
        "_(kiro-cli & agy expose no public billing API; these are aggregated "
        "from the local transcript.)_"
    )


def _model(raw: str, runner: AgentRunner, settings: Settings) -> str:
    parts = raw.split(maxsplit=1)
    engine = runner.engine.name
    if len(parts) == 1:  # show
        if engine == "agy":
            cur = settings.agy_model or "default (auto/cli)"
            return (f"Engine: *agy*\nActive model: `{cur}`\n\n"
                    "Change with `/model <name>` or list with `/models`.")
        if engine == "copilot":
            cur = settings.copilot_model or "default (cli, e.g. auto)"
            return (f"Engine: *copilot*\nActive model: `{cur}`\n\n"
                    "Change with `/model <name>` or list with `/models`.")
        return ("Engine: *kiro*\nModel is managed server-side by the Kiro "
                "profile and cannot be changed via the CLI.")
    # set
    new_model = parts[1].strip()
    if engine == "agy":
        if new_model.lower() in ("default", "reset", "clear"):
            settings.agy_model = ""
            return "agy model reset to the CLI default."
        settings.agy_model = new_model
        return f"agy model set to `{settings.agy_model}`."
    if engine == "copilot":
        if new_model.lower() in ("default", "reset", "clear"):
            settings.copilot_model = ""
            return "copilot model reset to the CLI default."
        settings.copilot_model = new_model
        return f"copilot model set to `{settings.copilot_model}`."
    return "Kiro does not support changing models via the CLI. This is agy/copilot-only."


def _effort(raw: str, runner: AgentRunner, settings: Settings) -> str:
    parts = raw.split(maxsplit=1)
    engine = runner.engine.name
    if engine == "agy":
        levels, attr = AGY_EFFORT_LEVELS, "agy_effort"
    elif engine == "copilot":
        levels, attr = COPILOT_EFFORT_LEVELS, "copilot_effort"
    else:
        return "Reasoning effort is only supported by the *agy* and *copilot* engines."

    if len(parts) == 1:  # show
        cur = getattr(settings, attr) or "default"
        opts = ", ".join(f"`{x}`" for x in levels)
        return (f"Engine: *{engine}*\nReasoning effort: `{cur}`\n\n"
                f"Options: {opts}, or `default`.")
    lvl = parts[1].strip().lower()
    if lvl in ("default", "reset", "clear"):
        setattr(settings, attr, "")
        return f"{engine} effort reset to default."
    if lvl in levels:
        setattr(settings, attr, lvl)
        return f"{engine} effort set to `{lvl}`."
    return f"Invalid effort level `{lvl}`. Use: {', '.join(levels)}."


def _timeout(raw: str, settings: Settings) -> str:
    parts = raw.split()
    if len(parts) == 1:
        return f"Current timeout: `{settings.timeout_sec}s`.\nChange with `/timeout <sec>`."
    try:
        val = int(parts[1])
        if val <= 0:
            raise ValueError
    except ValueError:
        return "Invalid timeout. Use a positive integer, e.g. `/timeout 300`."
    settings.timeout_sec = val
    return f"Timeout set to `{settings.timeout_sec}s`."


def _reply_mode(raw: str, settings: Settings) -> str:
    parts = raw.split()
    if len(parts) == 1:
        return (f"Current reply mode: `{settings.reply_mode}` (options: `final` or `all`).\n"
                "Change with `/reply final` or `/reply all`.")
    mode = parts[1].lower()
    if mode in ("final", "all"):
        settings.reply_mode = mode
        return f"Reply mode set to `{settings.reply_mode}`."
    return "Invalid reply mode. Use `final` or `all`."


def _transcript(raw: str, transcript: TranscriptStore) -> str:
    parts = raw.split()
    n = 5
    if len(parts) > 1:
        try:
            n = max(1, min(30, int(parts[1])))
        except ValueError:
            pass
    entries = transcript.preview_entries(n)
    if not entries:
        return "No transcript history for today yet."
    header = f"📜 **Last {len(entries)} transcript entries:**\n\n"
    return header + "\n\n".join(entries)


def _engine(cmd: str, runner: AgentRunner, transcript: TranscriptStore,
            settings: Settings) -> str:
    parts = cmd.split()
    if len(parts) == 1:  # show
        return (f"Active engine: *{runner.engine.name}*. Available: kiro, agy, copilot.\n"
                "Switch with `/engine kiro`, `/engine agy`, or `/engine copilot`.")
    target = parts[1]
    if target == runner.engine.name:
        return f"Already on *{target}*."
    try:
        new_engine = runner.switch_engine(target)
    except (EngineNotFound, ValueError) as exc:
        return f"Can't switch to '{target}': {exc}"
    # Carry recent conversation across engines for continuity.
    ctx = transcript.recent_context(settings.engine_switch_context_lines)
    if ctx:
        runner.queue_context(new_engine.name, ctx)
        note = (f"\n\n_({settings.engine_switch_context_lines} recent transcript "
                "entries will be injected as context on your next message.)_")
    else:
        note = ""
    return f"Switched engine to *{new_engine.name}* (new conversation).{note}"


# --------------------------------------------------------------------------- #
# Observability commands (/projects, /logs)
# --------------------------------------------------------------------------- #
_WINDOW_TOKEN = __import__("re").compile(r"^\d+[smhd]$", __import__("re").IGNORECASE)
# Clock time token: HH:MM or HH:MM:SS (used to detect a 2-token end timestamp).
_CLOCK_TOKEN = __import__("re").compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def _observability(settings: Settings):
    """Return the attached ObservabilityService, or None if unavailable."""
    return getattr(settings, "observability", None)


def _projects(settings: Settings) -> str:
    svc = _observability(settings)
    if svc is None:
        return ("Log access is not enabled. Set `ENABLE_OBSERVABILITY=1` + "
                "`OBSERVABILITY_REGISTRY` (and OpenObserve creds) to use `/logs`.")
    return svc.list_projects()


def _project_add(raw: str, settings: Settings) -> str:
    """Parse ``/project-add <name> <org> <stream> [window]`` and register it.

    Backend is always openobserve for now. Window is optional and defaults to
    the registry default (15m). All inputs are validated by the registry before
    anything is written to disk.
    """
    svc = _observability(settings)
    if svc is None:
        return ("Log access is not enabled. Set `ENABLE_OBSERVABILITY=1` + "
                "`OBSERVABILITY_REGISTRY` (and OpenObserve creds) first.")

    parts = raw.split()
    # parts[0] == "/project-add"; need name, org, stream (+ optional window)
    if len(parts) < 4:
        return ("Usage: `/project-add <name> <org> <stream> [window]`\n"
                "e.g. `/project-add mt4api-uat MTM-UAT meta_python_mt4uat_connector 15m`\n"
                "Backend is openobserve; window defaults to 15m.")
    name, org, stream = parts[1], parts[2], parts[3]
    window = parts[4] if len(parts) >= 5 else None
    return svc.add_project(name, org, stream, window)


def _project_remove(raw: str, settings: Settings) -> str:
    """Parse ``/project-remove <name>`` and delete it from the registry."""
    svc = _observability(settings)
    if svc is None:
        return ("Log access is not enabled. Set `ENABLE_OBSERVABILITY=1` + "
                "`OBSERVABILITY_REGISTRY` (and OpenObserve creds) first.")

    parts = raw.split()
    if len(parts) != 2:
        return ("Usage: `/project-remove <name>`\n"
                "See `/projects` for the list.")
    return svc.remove_project(parts[1])


def _logs(raw: str, settings: Settings) -> str:
    """Parse ``/logs <project> ...`` and run the query.

    Two time-range modes are supported after the project name:

    - **Relative window** (default):
      ``/logs <project> [window] [filter...]``
      If the 2nd token looks like ``15m``/``1h`` it is the window; otherwise it
      falls back to the project default and the rest is the free-text filter.

    - **Absolute range** (``from ... to ...``):
      ``/logs <project> from <YYYY-MM-DD[ HH:MM[:SS]]> to <...> [filter...]``
      Timestamps are read in local time (``LOGS_TZ_OFFSET_HOURS``, default WIB).
      Anything after the end timestamp is the free-text filter.
    """
    svc = _observability(settings)
    if svc is None:
        return ("Log access is not enabled. Set `ENABLE_OBSERVABILITY=1` + "
                "`OBSERVABILITY_REGISTRY` (and OpenObserve creds) to use `/logs`.")

    parts = raw.split()
    if len(parts) < 2:
        return ("Usage:\n"
                "• `/logs <project> [window] [filter]` — e.g. `/logs mt4api-dev 15m error`\n"
                "• `/logs <project> from <date time> to <date time> [filter]`\n"
                "  e.g. `/logs mt4api-dev from 2026-10-19 10:00:00 to 2026-10-19 11:00:00 error`\n"
                "See `/projects` for the list.")

    project = parts[1]
    rest = parts[2:]

    # Absolute range mode: look for the `from` keyword as the first token.
    if rest and rest[0].lower() == "from":
        return _logs_absolute(svc, project, rest, settings)

    # Relative window mode (unchanged).
    window: str | None = None
    if rest and _WINDOW_TOKEN.match(rest[0]):
        window = rest[0]
        rest = rest[1:]
    query_text = " ".join(rest).strip() or None
    return svc.query_logs(project, window, query_text, settings.logs_default_limit)


def _logs_absolute(svc, project: str, rest: list[str], settings: Settings) -> str:
    """Handle ``from <...> to <...> [filter]`` after the project name.

    ``rest`` starts with the literal ``from`` token. Tokens are re-joined with
    spaces because a timestamp like ``2026-10-19 10:00:00`` spans two tokens.
    """
    # Find the `to` separator (case-insensitive), splitting start vs end+filter.
    lowered = [t.lower() for t in rest]
    try:
        to_idx = lowered.index("to", 1)
    except ValueError:
        return ("Absolute range needs a `to`, e.g.\n"
                "`/logs <project> from 2026-10-19 10:00:00 to 2026-10-19 11:00:00`")

    start_tokens = rest[1:to_idx]
    after = rest[to_idx + 1:]
    if not start_tokens or not after:
        return ("Usage: `/logs <project> from <date time> to <date time> [filter]`\n"
                "e.g. `/logs mt4api-dev from 2026-10-19 10:00:00 to 2026-10-19 11:00:00`")

    start = " ".join(start_tokens)
    # The end timestamp is 1 token (bare date) or 2 (date + time). Decide by
    # whether the 2nd token after `to` looks like a clock time (HH:MM[:SS]).
    end_tokens = [after[0]]
    filter_tokens = after[1:]
    if len(after) >= 2 and _CLOCK_TOKEN.match(after[1]):
        end_tokens.append(after[1])
        filter_tokens = after[2:]
    end = " ".join(end_tokens)
    query_text = " ".join(filter_tokens).strip() or None

    return svc.query_logs(
        project, None, query_text, settings.logs_default_limit,
        start=start, end=end, tz_offset_hours=settings.logs_tz_offset_hours,
    )


# --------------------------------------------------------------------------- #
# Jira command (/jira)
# --------------------------------------------------------------------------- #
def _jira(raw: str, settings: Settings) -> str:
    """Parse ``/jira [JQL | PROJECT]`` and run a read-only search.

    - ``/jira``            -> configured default project/JQL
    - ``/jira SIT``        -> open issues in SIT, stalest first
    - ``/jira <full JQL>`` -> run the JQL verbatim
    """
    svc = getattr(settings, "jira", None)
    if svc is None:
        return ("Jira access is not enabled. Set `ENABLE_JIRA=1` + `JIRA_BASE_URL` "
                "(and Jira creds in the secret store) to use `/jira`.")
    parts = raw.split(maxsplit=1)
    jql = parts[1].strip() if len(parts) > 1 else None
    return svc.search(jql, settings.jira_default_limit)
