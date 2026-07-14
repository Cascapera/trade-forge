"""The package exports what the rest of the monorepo imports from it."""

import tradeforge_db


def test_exposes_a_version() -> None:
    assert tradeforge_db.__version__


def test_the_public_surface_is_importable() -> None:
    for name in tradeforge_db.__all__:
        assert hasattr(tradeforge_db, name), name
