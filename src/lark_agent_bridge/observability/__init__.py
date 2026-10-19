"""Observability tooling: read-only log access to OpenObserve / Grafana.

Additive package — the core chat/engine path does not depend on it. Everything
here is gated behind ``settings.enable_observability`` so the bot still runs if
it is disabled or misconfigured.
"""
