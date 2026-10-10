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


def format_help() -> str:
    return (
        "🤖 **lark-agent-bridge — commands**\n\n"
        "**Session & conversation:**\n"
        "• `/new` or `/reset` — start a fresh conversation on the active engine\n"
        "• `/status` — bot overview (engine, model, session, uptime)\n"
        "• `/usage` — today's message stats from the local transcript\n"
        "• `/transcript [n]` — show the last [n] transcript entries today (default 5)\n\n"
        "**Engine & model:**\n"
        "• `/engine` — show the active engine (kiro / agy)\n"
        "• `/engine <kiro|agy>` — switch backend engine (+ inject recent context)\n"
        "• `/model` — show the active model on the current engine\n"
        "• `/model <name>` — change model (agy only)\n"
        "• `/models` — list known models (agy only)\n"
        "• `/effort [level]` — show/set reasoning effort (agy only: low|medium|high|xhigh|max)\n\n"
        "**Operational:**\n"
        "• `/timeout [sec]` — show/adjust command timeout (default 600s)\n"
        "• `/reply [final|all]` — show/toggle reply mode\n"
        "• `/help` — show this message"
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
        if runner.engine.name != "agy":
            return "Model list is only available for the *agy* engine."
        return "📋 **Available models for agy:**\n\n" + "\n".join(
            f"• `{m}`" for m in AGY_KNOWN_MODELS
        ) + "\n\nSelect with `/model <name>`."

    if cmd == "/effort" or cmd.startswith("/effort "):
        return _effort(raw, runner, settings)

    if cmd == "/timeout" or cmd.startswith("/timeout "):
        return _timeout(raw, settings)

    if cmd == "/reply" or cmd.startswith("/reply "):
        return _reply_mode(raw, settings)

    if cmd == "/transcript" or cmd.startswith("/transcript "):
        return _transcript(raw, transcript)

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
    if len(parts) == 1:  # show
        if runner.engine.name == "agy":
            cur = settings.agy_model or "default (auto/cli)"
            return (f"Engine: *agy*\nActive model: `{cur}`\n\n"
                    "Change with `/model <name>` or list with `/models`.")
        return ("Engine: *kiro*\nModel is managed server-side by the Kiro "
                "profile and cannot be changed via the CLI.")
    # set
    new_model = parts[1].strip()
    if runner.engine.name != "agy":
        return "Kiro does not support changing models via the CLI. This is agy-only."
    if new_model.lower() in ("default", "reset", "clear"):
        settings.agy_model = ""
        return "agy model reset to the CLI default."
    settings.agy_model = new_model
    return f"agy model set to `{settings.agy_model}`."


def _effort(raw: str, runner: AgentRunner, settings: Settings) -> str:
    parts = raw.split(maxsplit=1)
    if len(parts) == 1:  # show
        if runner.engine.name == "agy":
            cur = settings.agy_effort or "default"
            return (f"Engine: *agy*\nReasoning effort: `{cur}`\n\n"
                    "Options: `low`, `medium`, `high`, `xhigh`, `max`, or `default`.")
        return "Reasoning effort is only supported by the *agy* engine."
    if runner.engine.name != "agy":
        return "Reasoning effort is only supported by the *agy* engine."
    lvl = parts[1].strip().lower()
    if lvl in ("default", "reset", "clear"):
        settings.agy_effort = ""
        return "agy effort reset to default."
    if lvl in ("low", "medium", "high", "xhigh", "max"):
        settings.agy_effort = lvl
        return f"agy effort set to `{settings.agy_effort}`."
    return f"Invalid effort level `{lvl}`. Use: low, medium, high, xhigh, max."


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
        return (f"Active engine: *{runner.engine.name}*. Available: kiro, agy.\n"
                "Switch with `/engine kiro` or `/engine agy`.")
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
