"""Tests for reading cards (card JSON 2.0) and merge_forward messages."""

import json
import types

from lark_agent_bridge.lark.client import CARD_CONTENT_TYPE, LarkClient
from lark_agent_bridge.lark.messages import extract_text, flatten_card


def test_card_content_type_constant():
    assert CARD_CONTENT_TYPE == "user_card_content"


def test_flatten_card_v2_markdown():
    card = {"schema": "2.0", "body": {"elements": [
        {"tag": "markdown", "content": "**Jawaban:** nggak ada"},
        {"tag": "markdown", "content": "- item 1\n- item 2"},
    ]}}
    assert flatten_card(json.dumps(card)) == "**Jawaban:** nggak ada\n- item 1\n- item 2"


def test_flatten_card_v2_header_and_nested():
    card = {
        "header": {"title": {"tag": "plain_text", "content": "Judul"}},
        "body": {"elements": [
            {"tag": "column_set", "columns": [
                {"tag": "column", "elements": [{"tag": "markdown", "content": "kiri"}]},
                {"tag": "column", "elements": [{"tag": "markdown", "content": "kanan"}]},
            ]},
            {"tag": "div", "text": {"tag": "plain_text", "content": "isi div"}},
            {"tag": "button", "text": {"tag": "plain_text", "content": "klik"}},
        ]},
    }
    out = flatten_card(json.dumps(card))
    assert out.splitlines() == ["Judul", "kiri", "kanan", "isi div"]


def test_flatten_card_v2_placeholder_stays_empty():
    ph = {"title": None, "elements": [[{"tag": "img", "image_key": "x"},
                                       {"tag": "text", "text": " "},
                                       {"tag": "text", "text": ""}]]}
    assert flatten_card(json.dumps(ph)) == ""


def test_extract_text_interactive_v2():
    card = {"body": {"elements": [{"tag": "markdown", "content": "halo"}]}}
    assert extract_text("interactive", json.dumps(card)) == "halo"


def _item(msg_type, content, message_id="om_x", sender="A"):
    return types.SimpleNamespace(
        msg_type=msg_type,
        message_id=message_id,
        body=types.SimpleNamespace(content=content),
        sender=types.SimpleNamespace(sender_name=sender, id="ou_1"),
    )


def _resp(items):
    data = types.SimpleNamespace(items=items, has_more=False, page_token=None)
    return types.SimpleNamespace(data=data, code=0, msg="ok", success=lambda: True)


def _client(list_fn, get_fn):
    c = LarkClient.__new__(LarkClient)
    c._client = types.SimpleNamespace(im=types.SimpleNamespace(v1=types.SimpleNamespace(
        message=types.SimpleNamespace(list=list_fn, get=get_fn))))
    return c


def test_thread_expands_merge_forward_and_cards():
    card = json.dumps({"body": {"elements": [{"tag": "markdown", "content": "dari bot"}]}})
    thread = [
        _item("merge_forward", "Merged and Forwarded Message", "om_mf", "Jun"),
        _item("interactive", card, "om_c", "Bot"),
    ]
    forwarded = [
        _item("merge_forward", "Merged and Forwarded Message", "om_mf"),
        _item("text", json.dumps({"text": "pertanyaan 1"})),
        _item("text", json.dumps({"text": "pertanyaan 2"})),
    ]
    got = {}

    def fake_get(req):
        got["id"] = req.paths["message_id"]
        return _resp(forwarded)

    c = _client(lambda req: _resp(thread), fake_get)
    out = c.list_thread_messages("omt_x")
    assert got["id"] == "om_mf"
    assert out[0] == ("Jun", "[Forwarded messages]\npertanyaan 1\npertanyaan 2")
    assert out[1] == ("Bot", "dari bot")


def test_get_message_text_merge_forward():
    items = [
        _item("merge_forward", "Merged and Forwarded Message", "om_mf"),
        _item("text", json.dumps({"text": "anak 1"})),
    ]
    c = _client(None, lambda req: _resp(items))
    _, text = c.get_message_text("om_mf")
    assert text == "[Forwarded messages]\nanak 1"


def test_merge_forward_fetch_failure_is_swallowed():
    def boom(req):
        raise RuntimeError("down")

    thread = [_item("merge_forward", "x", "om_mf"),
              _item("text", json.dumps({"text": "tetap kebaca"}))]
    c = _client(lambda req: _resp(thread), boom)
    assert c.list_thread_messages("omt_x") == [("A", "tetap kebaca")]
