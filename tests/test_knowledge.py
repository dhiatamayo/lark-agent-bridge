"""Tests for the knowledge package (pure parts: chunker + tsquery builder).

DB-backed paths (store.search/upsert, service with a live connection) are
exercised manually against a local Postgres; here we cover the logic that
needs no database.
"""

from __future__ import annotations

from lark_agent_bridge.knowledge.chunker import chunk_markdown
from lark_agent_bridge.knowledge.store import _to_or_prefix_tsquery
from lark_agent_bridge.knowledge.service import build_knowledge


def test_chunk_builds_heading_path():
    md = "# A\nintro\n## B1\nbody b1\n### C1\ndeep\n## B2\nbody b2"
    chunks = chunk_markdown(md)
    sections = [c.section for c in chunks]
    assert sections == ["A", "A > B1", "A > B1 > C1", "A > B2"]
    assert chunks[0].content == "intro"
    assert chunks[2].content == "deep"


def test_chunk_content_before_first_heading():
    md = "preamble line\n\n# H\nbody"
    chunks = chunk_markdown(md)
    assert chunks[0].section == "(intro)"
    assert "preamble" in chunks[0].content


def test_chunk_oversized_split_keeps_section_prefix():
    body = "\n\n".join([f"para {i} " + "x" * 200 for i in range(10)])
    md = f"# Big\n{body}"
    chunks = chunk_markdown(md, max_chars=500)
    assert len(chunks) > 1
    assert all(c.section.startswith("Big") for c in chunks)
    assert any("(part" in c.section for c in chunks)


def test_chunk_content_hash_stable():
    a = chunk_markdown("# H\nsame body")[0]
    b = chunk_markdown("# H\nsame body")[0]
    assert a.content_hash == b.content_hash
    c = chunk_markdown("# H\ndifferent")[0]
    assert a.content_hash != c.content_hash


def test_chunk_drops_empty_sections():
    md = "# Empty\n\n## AlsoEmpty\n\n## Real\ncontent"
    chunks = chunk_markdown(md)
    sections = [c.section for c in chunks]
    assert "Empty > Real" in sections
    # headings with no body produce no chunk
    assert "Empty" not in sections
    assert "Empty > AlsoEmpty" not in sections


def test_tsquery_or_of_prefix_terms():
    q = _to_or_prefix_tsquery("KIE multi source")
    assert q == "kie:* | multi:* | source:*"


def test_tsquery_splits_identifier_tokens():
    q = _to_or_prefix_tsquery("python-dde SIT-4568")
    # keeps the whole token AND its parts (for better recall)
    assert "python-dde:*" in q
    assert "python:*" in q
    assert "dde:*" in q
    assert "sit:*" in q
    assert "4568:*" in q


def test_tsquery_drops_punctuation_and_shorts():
    q = _to_or_prefix_tsquery("a, the! (failover)?")
    # single-char 'a' dropped; words kept
    assert "failover:*" in q
    assert "a:*" not in q


def test_tsquery_empty_on_garbage():
    assert _to_or_prefix_tsquery("!!! ??? ...") == ""


def test_build_knowledge_disabled_returns_none():
    class S:
        enable_knowledge = False
        knowledge_db_dsn = "postgresql://x:y@localhost:5432/z"
        knowledge_top_k = 6

    assert build_knowledge(S()) is None


def test_build_knowledge_no_dsn_returns_none():
    class S:
        enable_knowledge = True
        knowledge_db_dsn = ""
        knowledge_top_k = 6

    assert build_knowledge(S()) is None


def test_build_knowledge_bad_dsn_returns_none():
    # enabled + a DSN that can't connect => None (fail-open), never raises.
    class S:
        enable_knowledge = True
        knowledge_db_dsn = "postgresql://nope:nope@127.0.0.1:1/nodb"
        knowledge_top_k = 6

    assert build_knowledge(S()) is None


# -- ingest_configured (no DB: inject a fake store) --------------------------

def _settings_for(tmp_path, sources):
    """Minimal settings stub pointing the ingester at a temp steering dir."""
    class S:
        knowledge_db_dsn = "postgresql://andre:x@localhost:5432/andre_context"
        knowledge_steering_dir = str(tmp_path)
        knowledge_sources = sources
    return S()


class _FakeStore:
    """Records upsert calls; returns per-file counts like the real store."""
    def __init__(self):
        self.calls = []

    def upsert(self, source, chunks):
        self.calls.append((source, len(chunks)))
        # pretend everything was freshly inserted
        return {"inserted": len(chunks), "updated": 0, "skipped": 0, "pruned": 0}


def test_ingest_configured_totals_and_skips_missing(tmp_path):
    from lark_agent_bridge.knowledge.ingest import ingest_configured

    (tmp_path / "LOG_SESSIONS.md").write_text("# A\nbody a\n## B\nbody b", encoding="utf-8")
    (tmp_path / "BACKLOG.md").write_text("# X\nbody x", encoding="utf-8")
    settings = _settings_for(tmp_path, ["LOG_SESSIONS.md", "BACKLOG.md", "GONE.md"])

    fake = _FakeStore()
    total = ingest_configured(settings, store=fake)

    # two real files ingested, the missing one skipped silently
    assert total["sources"] == 2
    assert total["chunks"] == 3  # A>?, A>B, X  (3 non-empty chunks)
    assert total["inserted"] == 3
    assert {c[0] for c in fake.calls} == {"LOG_SESSIONS.md", "BACKLOG.md"}


def test_ingest_configured_no_dsn_raises():
    from lark_agent_bridge.knowledge.ingest import ingest_configured

    class S:
        knowledge_db_dsn = ""
        knowledge_steering_dir = "."
        knowledge_sources = []

    try:
        ingest_configured(S())
        assert False, "expected ValueError"
    except ValueError:
        pass
