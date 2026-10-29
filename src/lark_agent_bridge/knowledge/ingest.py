"""Ingest markdown knowledge files into Postgres.

CLI:
    python -m lark_agent_bridge.knowledge.ingest [--source FILE ...] [--dry-run]

Reads each configured source markdown (under the steering dir), chunks it, and
upserts into the ``knowledge`` table. Idempotent — safe to re-run after every
memory update; unchanged chunks are skipped, removed sections are pruned.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..config import Settings, default_env_path
from .chunker import chunk_markdown
from .store import KnowledgeStore


def _ingest_file(store: KnowledgeStore, path: Path, *, dry_run: bool) -> dict:
    text = path.read_text(encoding="utf-8")
    chunks = chunk_markdown(text)
    if dry_run:
        return {"chunks": len(chunks), "inserted": 0, "updated": 0, "skipped": 0, "pruned": 0}
    result = store.upsert(path.name, chunks)
    result["chunks"] = len(chunks)
    return result


def ingest_configured(settings, *, store: KnowledgeStore | None = None) -> dict:
    """Ingest the configured KNOWLEDGE_SOURCES in-process; return a totals dict.

    Reusable entry point (used by the CLI and by the bot's startup auto-ingest).
    Idempotent: unchanged chunks are skipped, removed sections pruned. Raises if
    the DSN is missing or a source read / DB write fails — callers that must not
    crash (e.g. startup) should wrap this in try/except.
    """
    dsn = settings.knowledge_db_dsn
    if not dsn:
        raise ValueError("PG_CONTEXT_DSN / KNOWLEDGE_DB_DSN not set")
    if store is None:
        store = KnowledgeStore(dsn)

    steering = Path(settings.knowledge_steering_dir)
    total = {"chunks": 0, "inserted": 0, "updated": 0, "skipped": 0, "pruned": 0, "sources": 0}
    for name in settings.knowledge_sources:
        path = steering / name
        if not path.exists():
            continue
        res = _ingest_file(store, path, dry_run=False)
        for k in ("chunks", "inserted", "updated", "skipped", "pruned"):
            total[k] += res.get(k, 0)
        total["sources"] += 1
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest steering markdown into Postgres.")
    parser.add_argument("--source", action="append", help="Specific source filename(s); default = configured KNOWLEDGE_SOURCES.")
    parser.add_argument("--dry-run", action="store_true", help="Chunk only; don't write to DB.")
    args = parser.parse_args(argv)

    settings = Settings(default_env_path())
    dsn = settings.knowledge_db_dsn
    if not dsn:
        print("error: PG_CONTEXT_DSN / KNOWLEDGE_DB_DSN not set", file=sys.stderr)
        return 2

    steering = Path(settings.knowledge_steering_dir)
    sources = args.source or settings.knowledge_sources

    store = None if args.dry_run else KnowledgeStore(dsn)
    total = {"chunks": 0, "inserted": 0, "updated": 0, "skipped": 0, "pruned": 0}
    for name in sources:
        path = steering / name
        if not path.exists():
            print(f"skip (not found): {path}")
            continue
        if args.dry_run:
            text = path.read_text(encoding="utf-8")
            res = {"chunks": len(chunk_markdown(text)), "inserted": 0, "updated": 0, "skipped": 0, "pruned": 0}
        else:
            res = _ingest_file(store, path, dry_run=False)
        for k in total:
            total[k] += res.get(k, 0)
        print(
            f"{name}: chunks={res['chunks']} inserted={res['inserted']} "
            f"updated={res['updated']} skipped={res['skipped']} pruned={res['pruned']}"
        )

    print(
        f"TOTAL: chunks={total['chunks']} inserted={total['inserted']} "
        f"updated={total['updated']} skipped={total['skipped']} pruned={total['pruned']}"
    )
    if not args.dry_run:
        print(f"rows in table: {store.count()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
