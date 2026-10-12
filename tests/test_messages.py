"""Tests for pure message helpers: parsing and chunking."""

import json

from lark_agent_bridge.lark import messages


def test_extract_text_plain():
    content = json.dumps({"text": "hello world"})
    assert messages.extract_text("text", content) == "hello world"


def test_extract_text_unsupported_type():
    assert messages.extract_text("image", "{}") == ""


def test_extract_text_bad_json():
    assert messages.extract_text("text", "not-json") == ""


def test_flatten_post_flat_format():
    # Flat format (no locale wrapper) with a numbered list across elements.
    content = json.dumps({
        "title": "T",
        "content": [
            [{"tag": "text", "text": "intro"}],
            [{"tag": "text", "text": "1. "}, {"tag": "text", "text": "first"}],
            [{"tag": "text", "text": "2. "}, {"tag": "text", "text": "second"}],
        ],
    })
    out = messages.flatten_post(content)
    assert out == "T\nintro\n1. first\n2. second"


def test_flatten_post_locale_wrapped():
    content = json.dumps({
        "en_us": {
            "title": "",
            "content": [[{"tag": "text", "text": "line one"}],
                        [{"tag": "text", "text": "line two"}]],
        }
    })
    assert messages.flatten_post(content) == "line one\nline two"


def test_flatten_post_at_tag():
    content = json.dumps({
        "title": "",
        "content": [[{"tag": "at", "user_name": "Bob"}, {"tag": "text", "text": " hi"}]],
    })
    assert messages.flatten_post(content) == "Bob hi"


def test_strip_mention_placeholders():
    assert messages.strip_mention_placeholders("@_user_1 hello @_user_22 there") == "hello  there"


def test_strip_ansi():
    assert messages.strip_ansi("\x1b[31mred\x1b[0m text") == "red text"


def test_chunks_small_text():
    assert list(messages.chunks("short", 100)) == ["short"]


def test_chunks_splits_on_newline():
    text = "a" * 60 + "\n" + "b" * 60
    parts = list(messages.chunks(text, 80))
    assert len(parts) == 2
    assert parts[0] == "a" * 60
    assert parts[1] == "b" * 60


def test_md_chunks_balances_code_fence():
    # A big code block that must be split keeps each chunk's fences balanced.
    body = "\n".join(f"line{i}" for i in range(200))
    text = "```python\n" + body + "\n```"
    parts = list(messages.md_chunks(text, 300))
    assert len(parts) > 1
    for part in parts:
        assert part.count("```") % 2 == 0  # each chunk is self-contained



# -- bot_is_mentioned ------------------------------------------------------- #

import types

BOT = "ou_bot_123"


def _mention(open_id):
    return types.SimpleNamespace(id=types.SimpleNamespace(open_id=open_id))


def test_bot_is_mentioned_specific_true():
    assert messages.bot_is_mentioned([_mention(BOT)], BOT) is True


def test_bot_is_mentioned_other_person_false():
    assert messages.bot_is_mentioned([_mention("ou_other")], BOT) is False


def test_bot_is_mentioned_at_all_false():
    # @all typically carries no open_id.
    assert messages.bot_is_mentioned([_mention(None)], BOT) is False


def test_bot_is_mentioned_bot_among_others_true():
    assert messages.bot_is_mentioned([_mention("ou_x"), _mention(BOT)], BOT) is True


def test_bot_is_mentioned_no_mentions_false():
    assert messages.bot_is_mentioned([], BOT) is False
    assert messages.bot_is_mentioned(None, BOT) is False


def test_bot_is_mentioned_fail_open_when_bot_id_unknown():
    # Unknown bot open_id -> any mention counts (never go silent).
    assert messages.bot_is_mentioned([_mention("ou_other")], "") is True
    assert messages.bot_is_mentioned([], "") is False
