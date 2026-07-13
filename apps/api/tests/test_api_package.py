"""Smoke test: the API package is importable and can reach its workspace deps."""

import tradeforge_api
import tradeforge_engine
import tradeforge_schema


def test_api_exposes_a_version() -> None:
    assert tradeforge_api.__version__ == "0.1.0"


def test_api_can_import_its_workspace_dependencies() -> None:
    """The api declares engine + schema as dependencies; editable installs must resolve."""
    assert tradeforge_engine.__version__
    assert tradeforge_schema.__version__
