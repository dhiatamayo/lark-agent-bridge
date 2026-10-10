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
from .lark.messages import extract_text, strip_mention_placeholders
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
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._seen: OrderedDict[str, float] = OrderedDict()  # message_id dedupe
        self._seen_lock = threading.Lock()

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

        reaction_id = self.client.add_reaction(message_id, self.settings.typing_emoji)
        try:
            answer = self.runner.run(prompt)
        except Exception as exc:  # report failures back to the chat
            answer = f"Bridge error: {exc!r}"
        try:
            self._reply(message_id, answer, in_thread)
        finally:
            self.client.remove_reaction(message_id, reaction_id)

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

        # In group chats, only respond when the bot is @mentioned.
        if msg.chat_type == "group" and not msg.mentions:
            return

        text = strip_mention_placeholders(text)
        if not text:
            return

        # If the user replied to another message and @mentioned the bot (the
        # typical "review this" flow), pull in the parent message for context.
        parent_id = getattr(msg, "parent_id", None) or getattr(msg, "root_id", None)
        prompt = text
        if parent_id and parent_id != msg.message_id:
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
