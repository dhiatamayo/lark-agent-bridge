"""High-level knowledge retrieval service used by the bot.

``build_knowledge(settings)`` returns a service or ``None`` and never raises —
mirrors the observability fail-open pattern. ``relevant(query)`` returns a
ready-to-inject string of the most relevant past-session chunks, or an empty
string when nothing matches / anything goes wrong.
"""

from __future__ import annotations

from .store import KnowledgeStore


class KnowledgeService:
    def __init__(self, store: KnowledgeStore, top_k: int) -> None:
        self._store = store
        self._top_k = top_k

    def relevant(self, query: str, *, k: int | None = None) -> str:
        """Return formatted relevant chunks for *query* (empty on any failure)."""
        try:
            hits = self._store.search(query, top_k=k or self._top_k)
        except Exception:
            return ""
        if not hits:
            return ""
        blocks = []
        for h in hits:
            blocks.append(f"### [{h.source}] {h.section}\n{h.content}")
        return "\n\n".join(blocks)


def build_knowledge(settings) -> KnowledgeService | None:
    """Construct the service from settings; return None if disabled/unavailable."""
    if not getattr(settings, "enable_knowledge", False):
        return None
    dsn = getattr(settings, "knowledge_db_dsn", "")
    if not dsn:
        return None
    try:
        store = KnowledgeStore(dsn)
        # Probe connectivity once so a dead DB disables the feature cleanly
        # instead of failing on every request.
        store.count()
    except Exception:
        return None
    return KnowledgeService(store, int(getattr(settings, "knowledge_top_k", 6)))
