"""Thin wrapper over the lark-oapi SDK for the operations the bot needs:
sending (card/text) replies, typing reactions, and fetching a message by id.
"""

from __future__ import annotations

import json
import logging

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    DeleteMessageReactionRequest,
    Emoji,
    GetMessageRequest,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from .messages import extract_text, md_chunks

logger = logging.getLogger(__name__)

_DOMAINS = {"feishu": lark.FEISHU_DOMAIN, "lark": lark.LARK_DOMAIN}


class LarkClient:
    """Convenience methods on top of the lark-oapi client."""

    def __init__(self, app_id: str, app_secret: str, domain_key: str = "lark",
                 max_chunk: int = 3500):
        domain = _DOMAINS.get(domain_key, lark.LARK_DOMAIN)
        self._client = lark.Client.builder().app_id(app_id).app_secret(app_secret).domain(domain).build()
        self.max_chunk = max_chunk
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain_key = domain_key

    # -- raw send ---------------------------------------------------------- #
    def _send(self, message_id: str, msg_type: str, content: dict, in_thread: bool) -> bool:
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(json.dumps(content, ensure_ascii=False))
                .reply_in_thread(in_thread)
                .build()
            )
            .build()
        )
        resp = self._client.im.v1.message.reply(req)
        if not resp.success():
            logger.error("reply (%s) failed: %s %s", msg_type, resp.code, resp.msg)
        return resp.success()

    def reply(self, message_id: str, text: str, in_thread: bool = False) -> None:
        """Reply as an interactive card so markdown renders; fall back to text.

        Long replies are chunked (keeping code fences valid). In group chats,
        pass ``in_thread=True`` to keep replies in a thread off the user's
        message instead of flooding the main timeline.
        """
        for part in md_chunks(text, self.max_chunk):
            card = {
                "schema": "2.0",
                "body": {"elements": [{"tag": "markdown", "content": part}]},
            }
            if not self._send(message_id, "interactive", card, in_thread):
                self._send(message_id, "text", {"text": part}, in_thread)

    # -- streaming: post an editable card, then patch it in place ---------- #
    @staticmethod
    def _card(text: str) -> dict:
        return {"schema": "2.0",
                "body": {"elements": [{"tag": "markdown", "content": text}]}}

    def post_card(self, message_id: str, text: str, in_thread: bool = False) -> str | None:
        """Send an interactive card as a reply and return its message_id so it
        can be edited later via :meth:`update_card`. Returns None on failure."""
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(json.dumps(self._card(text), ensure_ascii=False))
                .reply_in_thread(in_thread)
                .build()
            )
            .build()
        )
        resp = self._client.im.v1.message.reply(req)
        if not resp.success():
            logger.error("post_card failed: %s %s", resp.code, resp.msg)
            return None
        return resp.data.message_id

    def update_card(self, message_id: str, text: str) -> bool:
        """Edit a previously-sent card in place (progress updates). Best-effort;
        returns False on failure (caller may ignore — it's a progress tick)."""
        req = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps(self._card(text), ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = self._client.im.v1.message.patch(req)
        if not resp.success():
            logger.warning("update_card failed: %s %s", resp.code, resp.msg)
        return resp.success()

    # -- reactions (typing indicator) -------------------------------------- #
    def add_reaction(self, message_id: str, emoji_type: str) -> str | None:
        """Add an emoji reaction; return its reaction_id (or None on failure)."""
        req = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                .build()
            )
            .build()
        )
        resp = self._client.im.v1.message_reaction.create(req)
        if not resp.success():
            logger.error("add reaction failed: %s %s", resp.code, resp.msg)
            return None
        return resp.data.reaction_id

    def remove_reaction(self, message_id: str, reaction_id: str | None) -> None:
        if not reaction_id:
            return
        req = (
            DeleteMessageReactionRequest.builder()
            .message_id(message_id)
            .reaction_id(reaction_id)
            .build()
        )
        resp = self._client.im.v1.message_reaction.delete(req)
        if not resp.success():
            logger.error("remove reaction failed: %s %s", resp.code, resp.msg)

    # -- fetch a message (for reply-in-thread parent context) -------------- #
    def get_message_text(self, message_id: str) -> tuple[str, str]:
        """Fetch a message by id; return ``(sender_open_id, plain_text)``.

        Used to pull in the parent message when a user replies-in-thread and
        @mentions the bot (e.g. someone drops a link, the user replies "review
        this"). Best-effort: returns ``("", "")`` on any failure/unsupported type.
        """
        try:
            req = GetMessageRequest.builder().message_id(message_id).build()
            resp = self._client.im.v1.message.get(req)
            if not resp.success() or not resp.data or not resp.data.items:
                if not resp.success():
                    logger.warning("get_message failed: %s %s", resp.code, resp.msg)
                return "", ""
            item = resp.data.items[0]
            content = item.body.content if item.body else ""
            text = extract_text(item.msg_type or "", content or "")
            sender = ""
            if item.sender and getattr(item.sender, "id", None):
                sender = item.sender.id
            return sender, text
        except Exception as exc:  # never break the main flow
            logger.warning("get_message_text error: %r", exc)
            return "", ""

    # -- bot identity (for @mention gating) -------------------------------- #
    def resolve_bot_open_id(self) -> str:
        """Return the bot's own open_id, used to detect when it is @mentioned.

        Calls Lark's ``bot/v3/info`` (no extra scope needed). Best-effort:
        returns ``""`` on any failure, in which case group-mention gating falls
        back to "any mention" so the bot never goes silent.
        """
        try:
            import requests  # provided transitively by lark-oapi
            host = ("https://open.feishu.cn" if self._domain_key == "feishu"
                    else "https://open.larksuite.com")
            tok = requests.post(
                f"{host}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
                timeout=10,
            ).json().get("tenant_access_token")
            if not tok:
                logger.warning("resolve_bot_open_id: no tenant_access_token")
                return ""
            info = requests.get(
                f"{host}/open-apis/bot/v3/info",
                headers={"Authorization": f"Bearer {tok}"}, timeout=10,
            ).json()
            open_id = (info.get("bot") or {}).get("open_id", "")
            if open_id:
                logger.info("resolved bot open_id=%s", open_id)
            return open_id
        except Exception as exc:
            logger.warning("resolve_bot_open_id error: %r", exc)
            return ""

    @property
    def raw(self) -> lark.Client:
        """Access the underlying lark-oapi client (for the WS event loop, etc.)."""
        return self._client
