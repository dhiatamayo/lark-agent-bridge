"""Group-mention gate identity: lazy bot open_id resolution.

The startup lookup can fail (e.g. right after a reboot). Without a retry the
gate stays fail-open and the bot answers mentions of other people, so
``_ensure_bot_open_id`` must resolve lazily and cache the result.
"""

import threading
from types import SimpleNamespace

from lark_agent_bridge.bot import Bot


class _Client:
    def __init__(self, value: str):
        self.value = value
        self.calls = 0

    def resolve_bot_open_id(self) -> str:
        self.calls += 1
        return self.value


def _stub(client, resolved: str = ""):
    return SimpleNamespace(
        settings=SimpleNamespace(bot_open_id_resolved=resolved),
        client=client,
        _open_id_lock=threading.Lock(),
    )


def test_resolves_lazily_when_startup_lookup_failed():
    client = _Client("ou_bot")
    stub = _stub(client)

    assert Bot._ensure_bot_open_id(stub) == "ou_bot"
    assert stub.settings.bot_open_id_resolved == "ou_bot"
    assert client.calls == 1


def test_env_value_skips_api_call():
    client = _Client("ou_api")
    stub = _stub(client, resolved="ou_env")

    assert Bot._ensure_bot_open_id(stub) == "ou_env"
    assert client.calls == 0


def test_result_is_cached_after_first_resolution():
    client = _Client("ou_bot")
    stub = _stub(client)

    Bot._ensure_bot_open_id(stub)
    Bot._ensure_bot_open_id(stub)

    assert client.calls == 1


def test_stays_empty_when_resolution_keeps_failing():
    client = _Client("")
    stub = _stub(client)

    assert Bot._ensure_bot_open_id(stub) == ""
    assert client.calls == 1
