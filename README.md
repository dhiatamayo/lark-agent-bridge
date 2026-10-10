# lark-agent-bridge

A [Lark](https://www.larksuite.com/) (Feishu) bot that bridges chat messages to
agentic coding CLIs — currently **[Kiro CLI](https://kiro.dev)** and
**Antigravity (`agy`)** — and streams their answers back as nicely rendered
Lark cards.

Talk to your coding agent from a chat app: DM it, or @mention it in a group.
Switch engines on the fly, carry conversation context across engines, and keep
a local transcript of everything.

> **Why?** CLI coding agents are powerful but live in a terminal. This bridges
> them to where a team already talks (Lark/Feishu), with access control, a
> typing indicator, in-thread replies, and a tidy slash-command surface.

## Features

- **Engine-agnostic** — a small adapter per backend (`kiro`, `agy`); the rest of
  the bot never cares which one answers. Add a new engine by implementing
  `build_cmd()` + `parse()`.
- **Runtime switching** — `/engine agy` / `/engine kiro`, with recent transcript
  injected as context so the conversation carries over.
- **Slash commands** — `/help`, `/status`, `/usage`, `/model`, `/models`,
  `/effort`, `/timeout`, `/reply`, `/transcript`, `/new`.
- **Group-aware** — only responds when @mentioned in groups; replies in a thread
  to avoid flooding the timeline. Pulls in the parent message when you reply to
  one and @mention the bot (great for "review this" flows).
- **Access control** — locked to an allowlist of Lark `open_id`s. The agent can
  run tools on the host, so this is enforced by default.
- **Rich text** — renders markdown (headings, lists, tables, code) via Lark
  interactive cards, with safe chunking that keeps code fences valid across
  message boundaries.
- **Local transcript** — every message is appended to
  `transcripts/chat-YYYY-MM-DD.jsonl` as your own durable history.

## Architecture

```
src/lark_agent_bridge/
├── __main__.py        # entry point: load settings, run the bot
├── config.py          # Settings: all env/config in one place
├── bot.py             # orchestration: Lark events -> commands/runner (thin)
├── runner.py          # AgentRunner: active engine, sessions, subprocess exec
├── commands.py        # slash-command routing (pure function -> reply text)
├── transcript.py      # TranscriptStore: logging + usage/preview/context
├── engines/
│   ├── base.py        # Engine ABC + binary resolution
│   ├── kiro.py        # KiroEngine adapter
│   └── agy.py         # AgyEngine adapter
└── lark/
    ├── client.py      # lark-oapi wrapper (reply, reactions, fetch message)
    ├── messages.py    # content parsing + chunking (pure, no network)
    └── docs.py        # read Lark wiki/docx/sheet by URL
```

Parsing, engine logic, transcript I/O and command handling are all independently
unit-tested without needing a live Lark connection.

## Requirements

- Python >= 3.11
- A Lark/Feishu custom app (App ID + App Secret) with these scopes:
  `im:message`, `im:message:send_as_bot`,
  `im:message.group_at_msg.include_bot:readonly`,
  `im:message.p2p_msg:readonly`, `im:message.reactions:write_only`
  (and `wiki:wiki:readonly` + `docx:document:readonly` for the docs reader).
- At least one agent CLI on `PATH`: `kiro-cli` and/or `agy`.

## Quick start (with [uv](https://docs.astral.sh/uv/))

```bash
git clone https://github.com/dhiatamayo/lark-agent-bridge
cd lark-agent-bridge
uv sync                  # creates .venv and installs from uv.lock
cp .env.example .env     # fill in your credentials
uv run lark-agent-bridge # start the bot
```

On first run, DM the bot — if `ALLOWED_OPEN_IDS` is empty it replies with your
`open_id`. Put that in `.env` and restart.

### Without uv

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env     # fill in credentials
python -m lark_agent_bridge
```

## Configuration

All settings come from the environment (or a `.env` file). See
[`.env.example`](.env.example) for the full list. Highlights:

| Variable | Default | Purpose |
|---|---|---|
| `LARK_APP_ID` / `LARK_APP_SECRET` | — | Lark app credentials (required) |
| `LARK_DOMAIN` | `lark` | `lark` (larksuite.com) or `feishu` (feishu.cn) |
| `ALLOWED_OPEN_IDS` | — | comma-separated allowlist of Lark open_ids |
| `AGENT_ENGINE` | `kiro` | default backend: `kiro` or `agy` |
| `AGENT_TRUST_TOOLS` | `read,grep` | tools the agent may use unattended |
| `AGENT_WORKDIR` | `.` | directory the agent operates in |
| `AGENT_TIMEOUT_SEC` | `600` | per-run subprocess timeout |

## Commands

| Command | Description |
|---|---|
| `/help` | list commands |
| `/status` | engine, model, session, uptime, today's counts |
| `/usage` | message stats aggregated from the local transcript |
| `/engine [kiro\|agy]` | show or switch backend engine |
| `/model [name]` | show or change model (agy only) |
| `/models` | list known models (agy only) |
| `/effort [level]` | reasoning effort (agy only) |
| `/timeout [sec]` | adjust run timeout |
| `/reply [final\|all]` | final answer only, or include narration |
| `/transcript [n]` | preview recent transcript entries |
| `/new`, `/reset` | start a fresh conversation |

## Development

```bash
uv sync
uv run pytest          # run the test suite
uv run ruff check .    # lint
```

## Security notes

- Treat `ALLOWED_OPEN_IDS` as a hard gate: the agent runs real tools on the host.
- Prefer read-only `AGENT_TRUST_TOOLS` (`read,grep`) unless you understand the
  blast radius. `agy` trust is all-or-nothing.
- Never commit `.env`; it is gitignored.

## License

MIT — see [LICENSE](LICENSE).
