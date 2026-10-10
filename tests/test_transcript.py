"""Tests for TranscriptStore (log, usage stats, preview, recent context)."""

from lark_agent_bridge.transcript import TranscriptStore


def test_log_and_usage(tmp_path):
    store = TranscriptStore(tmp_path)
    store.log("in", message_id="m1", msg_type="text", sender="u1", text="hi")
    store.log("out", message_id="m1", msg_type="reply", text="hello")
    store.log("in", message_id="m2", msg_type="text", sender="u1", text="again")

    stats = store.usage_stats()
    assert stats["today_in"] == 2
    assert stats["today_out"] == 1
    assert stats["today_total"] == 3
    assert stats["total_files"] == 1
    assert stats["all_time_lines"] == 3


def test_preview_entries(tmp_path):
    store = TranscriptStore(tmp_path)
    store.log("in", text="question one")
    store.log("out", text="answer one")
    entries = store.preview_entries(5)
    assert len(entries) == 2
    assert "User" in entries[0]
    assert "Bot" in entries[1]


def test_preview_truncates_long_text(tmp_path):
    store = TranscriptStore(tmp_path)
    store.log("out", text="x" * 500)
    entries = store.preview_entries(1)
    assert entries[0].endswith("...")


def test_recent_context_format(tmp_path):
    store = TranscriptStore(tmp_path)
    store.log("in", text="hello")
    store.log("out", text="hi there")
    ctx = store.recent_context(20)
    assert "Recent conversation context" in ctx
    assert "User: hello" in ctx
    assert "Bot: hi there" in ctx
    assert ctx.rstrip().endswith("=== End of context ===")


def test_recent_context_disabled(tmp_path):
    store = TranscriptStore(tmp_path)
    store.log("in", text="hello")
    assert store.recent_context(0) == ""


def test_recent_context_empty_dir(tmp_path):
    store = TranscriptStore(tmp_path / "nonexistent")
    assert store.recent_context(20) == ""
    assert store.preview_entries(5) == []


def test_log_never_raises_on_bad_dir(tmp_path):
    # Point at a path that can't be created (a file, not a dir) and ensure no raise.
    f = tmp_path / "afile"
    f.write_text("x")
    store = TranscriptStore(f / "sub")  # parent is a file -> mkdir fails
    store.log("in", text="should be swallowed")  # must not raise
