"""Tests for the steering→AGENTS.md generator (pure logic + atomic write)."""

from __future__ import annotations

import threading
import types
from types import SimpleNamespace

from lark_agent_bridge.bot import Bot
from lark_agent_bridge.context_sync import build, is_manual_inclusion, write_atomic


def _write(dir, name, text):
    (dir / name).write_text(text, encoding="utf-8")


def test_build_strips_frontmatter_and_orders(tmp_path):
    _write(tmp_path, "A.md", "---\ninclusion: always\n---\n# Title A\nbody a")
    _write(tmp_path, "B.md", "no frontmatter b")
    content, files = build(tmp_path)
    names = [f.name for f in files]
    assert names == ["A.md", "B.md"]  # sorted (no explicit order)
    assert "inclusion: always" not in content  # frontmatter stripped
    assert "# Title A" in content and "body a" in content
    assert "no frontmatter b" in content
    assert "<!-- from A.md -->" in content and "<!-- from B.md -->" in content
    assert content.startswith("# AGENTS.md")


def test_build_honours_order_first(tmp_path):
    _write(tmp_path, "A.md", "a")
    _write(tmp_path, "Z.md", "z")
    _, files = build(tmp_path, order=["Z.md"])
    assert [f.name for f in files] == ["Z.md", "A.md"]


def test_build_skips_manual_inclusion(tmp_path):
    _write(tmp_path, "Keep.md", "keep me")
    _write(tmp_path, "Skip.md", "---\ninclusion: manual\n---\nskip me")
    content, files = build(tmp_path)
    assert [f.name for f in files] == ["Keep.md"]
    assert "keep me" in content and "skip me" not in content


def test_is_manual_inclusion():
    assert is_manual_inclusion("---\ninclusion: manual\n---\nbody")
    assert not is_manual_inclusion("---\ninclusion: always\n---\nbody")
    assert not is_manual_inclusion("no frontmatter")


def test_write_atomic_replaces_content(tmp_path):
    out = tmp_path / "AGENTS.md"
    out.write_text("old", encoding="utf-8")
    write_atomic(out, "new content")
    assert out.read_text(encoding="utf-8") == "new content"
    assert not (tmp_path / "AGENTS.md.tmp").exists()


def test_maybe_refresh_regenerates_only_on_change(tmp_path):
    steering = tmp_path / "steering"
    steering.mkdir()
    _write(steering, "A.md", "alpha")
    settings = SimpleNamespace(
        knowledge_refresh_on_change=True,
        knowledge_steering_dir=str(steering),
        workdir=str(tmp_path),
    )
    fake = SimpleNamespace(settings=settings, knowledge=None, _context_lock=threading.Lock())
    fake._steering_snapshot = types.MethodType(Bot._steering_snapshot, fake)
    fake._maybe_refresh_context = types.MethodType(Bot._maybe_refresh_context, fake)
    fake._steering_state = fake._steering_snapshot()  # seed (post startup-ingest)

    agents = tmp_path / "AGENTS.md"
    fake._maybe_refresh_context()  # nothing changed → no write
    assert not agents.exists()

    _write(steering, "B.md", "bravo")  # new file → snapshot differs
    fake._maybe_refresh_context()
    assert "alpha" in agents.read_text(encoding="utf-8")
    assert "bravo" in agents.read_text(encoding="utf-8")


def test_maybe_refresh_disabled_is_noop(tmp_path):
    steering = tmp_path / "steering"
    steering.mkdir()
    _write(steering, "A.md", "alpha")
    settings = SimpleNamespace(
        knowledge_refresh_on_change=False,
        knowledge_steering_dir=str(steering),
        workdir=str(tmp_path),
    )
    fake = SimpleNamespace(settings=settings, knowledge=None, _context_lock=threading.Lock())
    fake._steering_snapshot = types.MethodType(Bot._steering_snapshot, fake)
    fake._maybe_refresh_context = types.MethodType(Bot._maybe_refresh_context, fake)
    fake._steering_state = {}
    _write(steering, "B.md", "bravo")
    fake._maybe_refresh_context()
    assert not (tmp_path / "AGENTS.md").exists()

