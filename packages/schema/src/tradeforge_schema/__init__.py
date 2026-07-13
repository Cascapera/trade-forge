"""Strategy DSL — the central contract of the system.

A strategy is declarative JSON, not code: the UI composes it, the engine interprets
it, and an LLM can generate it (phase 3). Validation happens in two layers, and the
distinction matters:

* **Shape** — the JSON Schema, generated from the Pydantic models here and shared
  with the frontend. Catches malformed documents.
* **Meaning** — `semantic.py`. Catches well-formed documents that cannot run: a
  reference to an indicator that was never declared, a target with no stop.
"""

from tradeforge_schema.generate import SCHEMA_PATH, build_schema, render_schema
from tradeforge_schema.models import SCHEMA_VERSION, Condition, Indicator, Strategy
from tradeforge_schema.semantic import (
    SemanticError,
    SemanticValidationError,
    assert_executable,
    validate_semantics,
)

__all__ = [
    "SCHEMA_PATH",
    "SCHEMA_VERSION",
    "Condition",
    "Indicator",
    "SemanticError",
    "SemanticValidationError",
    "Strategy",
    "__version__",
    "assert_executable",
    "build_schema",
    "render_schema",
    "validate_semantics",
]

__version__ = "0.1.0"
