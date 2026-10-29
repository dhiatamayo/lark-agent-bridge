"""Tests for LarkClient.list_thread_messages (thread context fetch).

The SDK network layer is faked so we exercise only our pagination / flatten /
cap / fail-open logic — no real Lark calls.
"""

import json
import types

from lark_agent_bridge.lark.client import LarkClient


def _msg(sender_name, text, msg_type="text"):
    """Build a fake SDK message item with the fields our code reads."""
    body = types.SimpleNamespace(content=json.dumps({"text": text}))
    sender = types.SimpleNamespace(sender_name=sender_name)
    return types.SimpleNamespace(msg_type=msg_type, body=body, sender=sender)


def _resp(items, has_more=False, page_token=None, success=True):
    data = types.SimpleNamespace(items=items, has_more=has_more, page_token=page_token)
    return types.SimpleNamespace(
        data=data,
        code=0 if success else 99999,
        msg="ok" if success else "fail",
        success=lambda: success,
    )


def _client_with(list_fn):
    """Create a LarkClient without touching the real SDK, then inject a fake
    ``im.v1.message.list`` callable."""
    c = LarkClient.__new__(LarkClient)
    c._client = types.SimpleNamespace(
        im=types.SimpleNamespace(
            v1=types.SimpleNamespace(
                message=types.SimpleNamespace(list=list_fn)
            )
        )
    )
    return c


def test_single_page():
    resp = _resp([_msg("Dyeva", "halo"), _msg("Billy", "review dong")])
    c = _client_with(lambda req: resp)
    out = c.list_thread_messages("omt_x")
    assert out == [("Dyeva", "halo"), ("Billy", "review dong")]


def test_pagination_across_pages():
    pages = [
        _resp([_msg("A", "1")], has_more=True, page_token="p2"),
        _resp([_msg("B", "2")], has_more=False),
    ]
    calls = {"n": 0}

    def fake_list(req):
        r = pages[calls["n"]]
        calls["n"] += 1
        return r

    c = _client_with(fake_list)
    out = c.list_thread_messages("omt_x")
    assert out == [("A", "1"), ("B", "2")]
    assert calls["n"] == 2


def test_cap_max_messages():
    # Server keeps saying has_more, but we must stop at max_messages.
    def fake_list(req):
        return _resp([_msg("A", "x")], has_more=True, page_token="p")

    c = _client_with(fake_list)
    out = c.list_thread_messages("omt_x", max_messages=3)
    assert len(out) == 3


def test_skips_empty_and_unsupported():
    resp = _resp([
        _msg("A", "keep"),
        _msg("B", "", msg_type="image"),   # extract_text -> "" -> skipped
        _msg("C", "also"),
    ])
    c = _client_with(lambda req: resp)
    out = c.list_thread_messages("omt_x")
    assert out == [("A", "keep"), ("C", "also")]


def test_missing_sender_name():
    item = _msg(None, "no name")
    item.sender = types.SimpleNamespace(sender_name=None)
    c = _client_with(lambda req: _resp([item]))
    out = c.list_thread_messages("omt_x")
    assert out == [("", "no name")]


def test_api_failure_returns_empty():
    c = _client_with(lambda req: _resp([], success=False))
    assert c.list_thread_messages("omt_x") == []


def test_exception_is_swallowed():
    def boom(req):
        raise RuntimeError("network down")

    c = _client_with(boom)
    assert c.list_thread_messages("omt_x") == []
