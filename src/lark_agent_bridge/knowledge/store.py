"""Postgres-backed knowledge store (pg8000, pure-Python driver).

Thin wrapper around the ``knowledge`` table:
- :meth:`upsert` — idempotent insert/update keyed on ``(source, section)``,
  skipping rows whose content hash is unchanged.
- :meth:`search` — full-text ranked retrieval of the top-K chunks.
- :meth:`prune_missing` — drop rows for a source whose section no longer
  exists (so deletions in the markdown propagate).

All queries use parameter binding (``:name``) — no string interpolation.
"""

from __future__ import annotations

import urllib.parse as _up
from dataclasses import dataclass

import pg8000.native

from .chunker import Chunk


@dataclass(frozen=True)
class Hit:
    source: str
    section: str
    content: str
    rank: float


def _parse_dsn(dsn: str) -> dict:
    u = _up.urlparse(dsn)
    if u.scheme not in ("postgresql", "postgres"):
        raise ValueError(f"unsupported DSN scheme: {u.scheme!r}")
    return {
        "user": _up.unquote(u.username or ""),
        "password": _up.unquote(u.password or ""),
        "host": u.hostname or "localhost",
        "port": u.port or 5432,
        "database": (u.path or "").lstrip("/") or None,
    }


class KnowledgeStore:
    """Connection + queries for the knowledge table.

    Open a short-lived connection per operation (``connect()`` context) — the
    bot is low-QPS, so a persistent pool is unnecessary and a fresh connection
    sidesteps stale-socket issues.
    """

    def __init__(self, dsn: str) -> None:
        self._params = _parse_dsn(dsn)

    def _connect(self) -> pg8000.native.Connection:
        return pg8000.native.Connection(**self._params)

    # -- write ---------------------------------------------------------------
    def upsert(self, source: str, chunks: list[Chunk]) -> dict:
        """Insert/update chunks for *source*; returns counts.

        Idempotent: a row whose ``content_hash`` matches is left untouched.
        Sections no longer present are pruned.
        """
        inserted = updated = skipped = 0
        conn = self._connect()
        try:
            seen_sections: list[str] = []
            for ch in chunks:
                seen_sections.append(ch.section)
                existing = conn.run(
                    "SELECT content_hash FROM knowledge "
                    "WHERE source = :s AND section = :sec",
                    s=source,
                    sec=ch.section,
                )
                if existing and existing[0][0] == ch.content_hash:
                    skipped += 1
                    continue
                if existing:
                    conn.run(
                        "UPDATE knowledge SET content = :c, content_hash = :h, "
                        "updated_at = now() WHERE source = :s AND section = :sec",
                        c=ch.content,
                        h=ch.content_hash,
                        s=source,
                        sec=ch.section,
                    )
                    updated += 1
                else:
                    conn.run(
                        "INSERT INTO knowledge (source, section, content, content_hash) "
                        "VALUES (:s, :sec, :c, :h)",
                        s=source,
                        sec=ch.section,
                        c=ch.content,
                        h=ch.content_hash,
                    )
                    inserted += 1
            pruned = self._prune_missing(conn, source, seen_sections)
            return {
                "inserted": inserted,
                "updated": updated,
                "skipped": skipped,
                "pruned": pruned,
            }
        finally:
            conn.close()

    def _prune_missing(
        self, conn: pg8000.native.Connection, source: str, keep_sections: list[str]
    ) -> int:
        rows = conn.run("SELECT section FROM knowledge WHERE source = :s", s=source)
        keep = set(keep_sections)
        to_drop = [r[0] for r in rows if r[0] not in keep]
        for sec in to_drop:
            conn.run(
                "DELETE FROM knowledge WHERE source = :s AND section = :sec",
                s=source,
                sec=sec,
            )
        return len(to_drop)

    # -- read ----------------------------------------------------------------
    def search(self, query: str, *, top_k: int = 6) -> list[Hit]:
        """Return the top-K chunks ranked by full-text relevance.

        The free-text *query* becomes an OR of prefix-matched terms
        (``term1:* | term2:* | ...``). OR (not AND) is deliberate: memory
        retrieval should surface chunks matching *some* of the query words and
        let ``ts_rank`` order them, rather than requiring every word to appear.
        """
        tsquery = _to_or_prefix_tsquery(query)
        if not tsquery:
            return []
        conn = self._connect()
        try:
            rows = conn.run(
                "SELECT source, section, content, "
                "       ts_rank(tsv, q) AS rank "
                "FROM knowledge, to_tsquery('simple', :tsq) AS q "
                "WHERE tsv @@ q "
                "ORDER BY rank DESC "
                "LIMIT :k",
                tsq=tsquery,
                k=top_k,
            )
            return [Hit(r[0], r[1], r[2], float(r[3])) for r in rows]
        finally:
            conn.close()

    def count(self) -> int:
        conn = self._connect()
        try:
            return int(conn.run("SELECT count(*) FROM knowledge")[0][0])
        finally:
            conn.close()


def _to_or_prefix_tsquery(query: str) -> str:
    """Turn free text into an OR of prefix-matched tsquery terms.

    ``"KIE multi source python-dde"`` -> ``"KIE:* | multi:* | source:* |
    python:* | dde:*"``. Identifier tokens with separators (``python-dde``,
    ``SIT-4568``) are also split so their parts match individually. Stop-ish
    1-char tokens are dropped. Returns "" when nothing usable remains.
    """
    import re

    cleaned = re.sub(r"[^0-9A-Za-z\-_./ ]+", " ", query)
    raw_terms = cleaned.split()

    terms: list[str] = []
    for t in raw_terms:
        parts = [t] + re.split(r"[-_./]+", t)
        for p in parts:
            p = p.strip("-_./").lower()
            if len(p) >= 2 and p not in terms:
                terms.append(p)

    if not terms:
        return ""
    # Escape nothing needed: terms are already restricted to [0-9a-z]; append
    # prefix wildcard and OR them.
    return " | ".join(f"{t}:*" for t in terms)
