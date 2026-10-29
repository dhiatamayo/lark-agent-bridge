"""Orchestration: wire Lark WebSocket events to the command router and the
agent runner. This module stays deliberately thin — parsing, engine logic,
transcript I/O and command handling live in their own modules.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from . import commands
from .config import Settings
from .lark.client import LarkClient
from .lark.messages import bot_is_mentioned, extract_text, md_chunks, strip_mention_placeholders
from .runner import AgentRunner
from .transcript import TranscriptStore

logger = logging.getLogger(__name__)

_DOMAINS = {"feishu": lark.FEISHU_DOMAIN, "lark": lark.LARK_DOMAIN}


class Bot:
    """Holds all shared state and the inbound-message handling logic."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = LarkClient(
            settings.app_id, settings.app_secret, settings.domain_key, settings.max_chunk
        )
        self.transcript = TranscriptStore(settings.transcript_dir)
        self.runner = AgentRunner(settings)

        # Attach the observability service (log access) to settings so the pure
        # command dispatcher can reach it without a wider signature. None when
        # disabled/misconfigured — commands then report a friendly message.
        from .observability.service import build_observability
        settings.observability = build_observability(settings)

        # Knowledge retrieval (Postgres FTS): inject relevant past-session
        # chunks into each prompt instead of loading the whole markdown. None
        # when disabled/unavailable → prompts go through unchanged (fail-open).
        from .knowledge.service import build_knowledge
        self.knowledge = build_knowledge(settings)

        # Sync the knowledge DB with the source markdown on startup so the DB
        # never drifts behind the files after a memory update + restart.
        # Idempotent (unchanged chunks skipped) and fail-open (any error just
        # logs a warning; retrieval still runs against whatever is in the DB).
        if self.knowledge is not None and getattr(settings, "knowledge_auto_ingest", True):
            try:
                from .knowledge.ingest import ingest_configured
                summary = ingest_configured(settings)
                logger.info(
                    "knowledge auto-ingest: sources=%d chunks=%d inserted=%d "
                    "updated=%d skipped=%d pruned=%d",
                    summary.get("sources", 0), summary.get("chunks", 0),
                    summary.get("inserted", 0), summary.get("updated", 0),
                    summary.get("skipped", 0), summary.get("pruned", 0),
                )
            except Exception as exc:
                logger.warning("knowledge auto-ingest skipped: %r", exc)

        self._executor = ThreadPoolExecutor(max_workers=4)
        self._seen: OrderedDict[str, float] = OrderedDict()  # message_id dedupe
        self._seen_lock = threading.Lock()

        # Resolve the bot's own open_id so group-mention gating can tell when
        # the bot is specifically @mentioned (vs @all / other people).
        settings.bot_open_id_resolved = settings.bot_open_id or self.client.resolve_bot_open_id()

    # -- dedupe ------------------------------------------------------------ #
    def _already_seen(self, message_id: str) -> bool:
        with self._seen_lock:
            if message_id in self._seen:
                return True
            self._seen[message_id] = time.time()
            while len(self._seen) > 1000:
                self._seen.popitem(last=False)
            return False

    # -- reply convenience ------------------------------------------------- #
    def _reply(self, message_id: str, text: str, in_thread: bool) -> None:
        self.client.reply(message_id, text, in_thread)
        self.transcript.log("out", message_id=message_id, msg_type="reply", text=text)

    # -- the actual work (runs in a worker thread) ------------------------- #
    def _handle(self, message_id: str, prompt: str, in_thread: bool) -> None:
        # Slash commands first; they return reply text (or None to forward).
        reply_text = commands.dispatch(prompt, self.runner, self.transcript, self.settings)
        if reply_text is not None:
            self._reply(message_id, reply_text, in_thread)
            return

        # Retrieve relevant past-session knowledge and prepend it (fail-open:
        # any error → empty string → prompt unchanged).
        if self.knowledge is not None:
            memory = self.knowledge.relevant(prompt)
            if memory:
                prompt = (
                    f"[Relevant memory from past sessions — use if helpful]:\n{memory}\n\n"
                    f"[Current request]:\n{prompt}"
                )

        if self.settings.enable_streaming:
            self._handle_streaming(message_id, prompt, in_thread)
            return

        reaction_id = self.client.add_reaction(message_id, self.settings.typing_emoji)
        try:
            answer = self.runner.run(prompt)
        except Exception as exc:  # report failures back to the chat
            answer = f"Bridge error: {exc!r}"
        try:
            self._reply(message_id, answer, in_thread)
        finally:
            self.client.remove_reaction(message_id, reaction_id)

    def _handle_streaming(self, message_id: str, prompt: str, in_thread: bool) -> None:
        """Streaming variant: show a live-updating progress card while the agent
        works, then render the final answer. Falls back to a normal reply if the
        progress card can't be posted (so we never go silent)."""
        progress_mid = self.client.post_card(message_id, "⏳ thinking…", in_thread)
        if not progress_mid:
            # Couldn't open a progress card — degrade to the blocking path.
            try:
                answer = self.runner.run(prompt)
            except Exception as exc:
                answer = f"Bridge error: {exc!r}"
            self._reply(message_id, answer, in_thread)
            return

        def on_progress(label: str) -> None:
            self.client.update_card(progress_mid, f"⏳ {label}")

        try:
            answer = self.runner.run_streaming(prompt, on_progress)
        except Exception as exc:
            answer = f"Bridge error: {exc!r}"

        # Render the final answer. If it fits one card, edit the progress card in
        # place. If it needs chunking, finish the card and send the full reply.
        parts = list(md_chunks(answer, self.settings.max_chunk))
        if len(parts) <= 1:
            if not self.client.update_card(progress_mid, answer):
                self.client.reply(message_id, answer, in_thread)
        else:
            self.client.update_card(progress_mid, "✅ done — full answer below")
            self.client.reply(message_id, answer, in_thread)
        self.transcript.log("out", message_id=message_id, msg_type="reply", text=answer)

    # -- inbound event ----------------------------------------------------- #
    def on_message(self, data: P2ImMessageReceiveV1) -> None:
        msg = data.event.message
        sender = data.event.sender.sender_id.open_id

        if self._already_seen(msg.message_id):
            return

        allowed = self.settings.allowed_open_ids
        if not allowed:
            # Help the owner discover their open_id on first run; never run the agent.
            self.client.reply(
                msg.message_id,
                f"Bot is locked. Add your open_id to ALLOWED_OPEN_IDS:\n{sender}",
            )
            return
        if sender not in allowed:
            logger.warning("ignored message from unauthorized open_id=%s", sender)
            return

        if msg.message_type in ("text", "post"):
            text = extract_text(msg.message_type, msg.content)
        else:
            self.transcript.log("in", message_id=msg.message_id, msg_type=msg.message_type,
                                sender=sender, text="", raw=msg.content)
            self.client.reply(
                msg.message_id,
                "Only text and rich-text (post) messages are supported for now.",
            )
            return

        self.transcript.log("in", message_id=msg.message_id, msg_type=msg.message_type,
                            sender=sender, text=text, raw=msg.content)

        logger.info(
            "inbound diag: chat_type=%s thread_id=%r parent_id=%r root_id=%r",
            getattr(msg, "chat_type", None),
            getattr(msg, "thread_id", None),
            getattr(msg, "parent_id", None),
            getattr(msg, "root_id", None),
        )
        self.transcript.log(
            "in", message_id=msg.message_id, msg_type="diag", sender=sender,
            text=(
                f"chat_type={getattr(msg,'chat_type',None)} "
                f"thread_id={getattr(msg,'thread_id',None)!r} "
                f"parent_id={getattr(msg,'parent_id',None)!r} "
                f"root_id={getattr(msg,'root_id',None)!r}"
            ),
        )

        # In group chats, only respond when THIS bot is @mentioned specifically
        # (not on @all or mentions of other people). Set GROUP_REQUIRE_BOT_MENTION=0
        # to revert to the old "any mention" behaviour.
        if msg.chat_type == "group":
            if self.settings.group_require_bot_mention:
                if not bot_is_mentioned(msg.mentions, self.settings.bot_open_id_resolved):
                    return
            elif not msg.mentions:
                return

        text = strip_mention_placeholders(text)
        if not text:
            return

        # If the user @mentioned the bot inside a reply thread, pull in context.
        # Prefer the WHOLE thread (so the bot sees the full discussion, e.g. a
        # teammate's agent dropped an MR and a conversation followed); fall back
        # to just the single parent message if there's no thread. Both are
        # best-effort — on failure the prompt is just the user's text.
        prompt = text
        thread_id = getattr(msg, "thread_id", None)
        parent_id = getattr(msg, "parent_id", None) or getattr(msg, "root_id", None)

        thread_ctx = ""
        if thread_id:
            msgs = self.client.list_thread_messages(thread_id)
            # Drop the user's own just-sent @mention (it's already the request).
            convo = [(n, t) for n, t in msgs if t.strip() != text.strip()]
            if convo:
                lines = [f"{n or 'unknown'}: {t}" for n, t in convo]
                thread_ctx = "\n".join(lines)
                self.transcript.log("in", message_id=thread_id, msg_type="thread",
                                    sender="", text=thread_ctx)

        if thread_ctx:
            prompt = (
                f"[Context — the thread the user is replying in "
                f"(oldest first, 'sender: message')]:\n{thread_ctx}\n\n"
                f"[User request]:\n{text}"
            )
        elif parent_id and parent_id != msg.message_id:
            p_sender, p_text = self.client.get_message_text(parent_id)
            if p_text:
                self.transcript.log("in", message_id=parent_id, msg_type="parent",
                                    sender=p_sender, text=p_text)
                who = f" (from {p_sender})" if p_sender else ""
                prompt = (
                    f"[Context — the message the user replied to{who}]:\n{p_text}\n\n"
                    f"[User request]:\n{text}"
                )

        # Group chats reply in a thread; DMs reply inline.
        in_thread = msg.chat_type == "group"

        # Return immediately so Lark doesn't time out and redeliver; work async.
        self._executor.submit(self._handle, msg.message_id, prompt, in_thread)

    # -- run --------------------------------------------------------------- #
    def run(self) -> None:
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self.on_message)
            .build()
        )
        domain = _DOMAINS.get(self.settings.domain_key, lark.LARK_DOMAIN)
        ws = lark.ws.Client(
            self.settings.app_id,
            self.settings.app_secret,
            event_handler=handler,
            domain=domain,
            log_level=lark.LogLevel.INFO,
        )
        print(
            f"Lark <-> agent bridge running. engine: {self.runner.engine.name} "
            f"({self.runner.engine.binary}), workdir: {self.settings.workdir}, "
            f"trust-tools: {self.settings.trust_tools}"
        )
        ws.start()
