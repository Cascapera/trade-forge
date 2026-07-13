"""Strategy DSL — the central contract of the system.

A strategy is declarative JSON, not code: the UI composes it, the engine
interprets it, and an LLM can generate it (phase 3). This package owns the JSON
Schema and the validators that guard it on both sides of the wire — Python here,
generated TypeScript types for the frontend.

The schema itself lands in PR-004, which is where the real design work happens.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
