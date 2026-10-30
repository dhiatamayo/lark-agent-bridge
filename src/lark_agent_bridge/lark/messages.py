"""Pure message-content helpers: parse Lark content JSON to text, and chunk
long replies for Lark's per-message size limit.

These functions have no network dependency, which makes them straightforward to
unit-test.
"""

from __future__ import annotations

import json
import re

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
FENCE_RE = re.compile(r"^\s*(```[^\n]*)", re.M)
MENTION_PLACEHOLDER_RE = re.compile(r"@_user_\d+")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences (CLI colour codes) from *text*."""
    return ANSI_RE.sub("", text)


def strip_mention_placeholders(text: str) -> str:
    """Drop Lark ``@_user_N`` mention placeholders and surrounding space."""
    return MENTION_PLACEHOLDER_RE.sub("", text).strip()


def bot_is_mentioned(mentions, bot_open_id: str) -> bool:
    """Return True when THIS bot is specifically @mentioned.

    *mentions* is the message's mention list (each item exposing ``id.open_id``);
    *bot_open_id* is the bot's own open_id. Matches only a mention that targets
    the bot, so ``@all`` (whose ``id.open_id`` is typically ``None``) and
    mentions of other people return False.

    Fails open: when *bot_open_id* is empty (could not be resolved), any mention
    counts as a match so the bot stays responsive rather than going silent.
    """
    items = mentions or []
    if not bot_open_id:
        return bool(items)
    for m in items:
        mid = getattr(m, "id", None)
        open_id = getattr(mid, "open_id", None) if mid else None
        if open_id and open_id == bot_open_id:
            return True
    return False


def flatten_post(content: str) -> str:
    """Flatten a Lark 'post' (rich-text) content JSON into plain text.

    Lark may send the content either locale-wrapped
    (``{"en_us": {title, content}}``) or flat (``{title, content}``). Both are
    handled: we walk the line/element AST and join text-bearing tags
    (text/a/at/link). Falls back to the SDK parser for exotic formats.
    """
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""

    # Unwrap a locale layer if present (e.g. {"en_us": {...}} / {"zh_cn": {...}}).
    body = data
    if "content" not in data and "title" not in data:
        for val in data.values():
            if isinstance(val, dict) and ("content" in val or "title" in val):
                body = val
                break

    title = body.get("title") or ""
    lines: list[str] = []
    for line in body.get("content", []) or []:
        parts: list[str] = []
        for el in line or []:
            if not isinstance(el, dict):
                continue
            tag = el.get("tag")
            if tag in ("text", "a", "link"):
                parts.append(el.get("text", ""))
            elif tag == "at":
                parts.append(el.get("user_name") or el.get("text") or "")
        lines.append("".join(parts))
    out = "\n".join(x for x in lines if x is not None)
    text = f"{title}\n{out}".strip() if title else out.strip()

    if text:
        return text
    # Last resort: let the SDK try (older/newer formats).
    try:
        from lark_oapi.channel.normalize.registry import parse_message_content
        parsed = parse_message_content("post", content)
        return (f"{parsed.title}\n{parsed.text}" if getattr(parsed, "title", "")
                else getattr(parsed, "text", "")).strip()
    except Exception:
        return ""


def flatten_card(content: str) -> str:
    """Flatten a Lark 'interactive' (card) content JSON into plain text.

    Card content varies, but the user-authored text is most reliably found in
    the ``user_dsl`` field (a JSON *string*) under ``body.elements[].content``
    (tags ``markdown``/``text``/``lark_md``/...). When a card is forwarded/shared
    (e.g. a doc preview), the top-level ``elements`` only carry the rendered
    thumbnail (``img`` + blank ``text``), so ``user_dsl`` is preferred.

    Resolution order: ``user_dsl.body.elements[].content`` -> top-level
    ``elements`` (post-style line/element AST) -> "". Never raises.
    """
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""

    # 1) Preferred: the card's source DSL (carries real markdown/text).
    dsl = data.get("user_dsl")
    if isinstance(dsl, str) and dsl:
        try:
            parsed = json.loads(dsl)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            body = parsed.get("body") or {}
            parts: list[str] = []
            for el in body.get("elements", []) or []:
                if not isinstance(el, dict):
                    continue
                txt = el.get("content") or el.get("text") or ""
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt)
            joined = "\n".join(parts).strip()
            if joined:
                return joined

    # 2) Fallback: top-level 'elements' is a post-style line/element AST.
    lines: list[str] = []
    for line in data.get("elements", []) or []:
        seg: list[str] = []
        for el in line or []:
            if not isinstance(el, dict):
                continue
            tag = el.get("tag")
            if tag in ("text", "a", "link", "markdown", "lark_md"):
                seg.append(el.get("text") or el.get("content") or "")
            elif tag == "at":
                seg.append(el.get("user_name") or el.get("text") or "")
        if seg:
            lines.append("".join(seg))
    title = data.get("title") or ""
    out = "\n".join(x for x in lines if x).strip()
    return (f"{title}\n{out}".strip() if title else out) or ""


def extract_text(msg_type: str, content: str) -> str:
    """Turn a Lark message content JSON into plain text, by message type.

    Shared by inbound events and fetched parent messages. Returns "" for
    unsupported types (image/file/sticker/...) so callers can skip them.
    """
    if msg_type == "text":
        try:
            return json.loads(content).get("text", "") or ""
        except (ValueError, TypeError):
            return ""
    if msg_type == "post":
        return flatten_post(content)
    if msg_type == "interactive":
        return flatten_card(content)
    return ""


def chunks(text: str, size: int):
    """Yield *text* in <= *size* pieces, preferring to break on newlines."""
    while text:
        if len(text) <= size:
            yield text
            return
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > size // 2 else size
        yield text[:cut]
        text = text[cut:].lstrip("\n")


def md_chunks(text: str, size: int):
    """Like :func:`chunks`, but keep ``` code blocks valid across boundaries.

    If a chunk ends with an unclosed fence, close it and re-open the same
    language in the next chunk so each message renders as valid markdown.
    """
    reopen = ""
    for part in chunks(text, size):
        part = reopen + part
        fences = FENCE_RE.findall(part)
        if len(fences) % 2:  # a code block is still open at the cut
            reopen = fences[-1].strip() + "\n"
            part += "\n```"
        else:
            reopen = ""
        yield part
