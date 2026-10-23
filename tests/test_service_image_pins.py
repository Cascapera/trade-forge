"""The service images CI runs against have to be the ones `docker compose` runs against.

⚠️ **They are pinned in two files, and only one of them is watched.** Dependabot's
`docker-compose` ecosystem updates `docker-compose.yml`; its `github-actions` ecosystem updates
`uses:` lines, not the `services:` images inside a workflow. So a bump arrives as a pull request
that changes the database developers use and leaves the database CI tests against untouched —
and the pull request goes green, because the job that would have caught a problem never ran the
new version at all.

That is not a hypothetical: `postgres 16 -> 18` and `redis 7 -> 8` both sat open for weeks with
ten green checks each, and neither check had seen either image.

A comment asking people to remember would not have worked; one exactly like it is written above
the constraint names in `rev_0011`, and this repository doubled a prefix again in `rev_0012` the
same morning. So this is a test. A bump that touches one file and not the other is red, and the
fix is to touch the other one.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = REPO_ROOT / "docker-compose.yml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The services whose image must be identical in both places. Not every image in the compose
# file: `api`, `web` and `worker` are built from this repository, have no counterpart in CI, and
# would be a false positive here.
SHARED_SERVICES = ("postgres", "redis")

_IMAGE = re.compile(r"^\s*image:\s*(?P<image>(?P<name>[a-z0-9._/-]+):(?P<tag>[A-Za-z0-9._-]+))\s*$")


def pinned_images(path: Path) -> dict[str, set[str]]:
    """Every `image: name:tag` in a file, grouped by image name.

    A set per name rather than a single value, because a file may pin the same image more than
    once and the interesting failure is exactly that they disagree.
    """
    found: dict[str, set[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _IMAGE.match(line)
        if match:
            found.setdefault(match["name"], set()).add(match["image"])
    return found


@pytest.mark.parametrize("service", SHARED_SERVICES)
def test_ci_and_compose_pin_the_same_image(service: str) -> None:
    """The whole point. If these disagree, CI is measuring a database nobody runs."""
    compose = pinned_images(COMPOSE).get(service, set())
    workflow = pinned_images(WORKFLOW).get(service, set())

    assert compose, f"no {service} image pinned in {COMPOSE.name}"
    assert workflow, f"no {service} image pinned in {WORKFLOW.name}"
    assert compose == workflow, (
        f"{service} is pinned to {sorted(compose)} in {COMPOSE.name} but "
        f"{sorted(workflow)} in {WORKFLOW.name} — a dependabot bump touches only the first, "
        "so CI would go green without ever running the new image"
    )


@pytest.mark.parametrize("service", SHARED_SERVICES)
def test_the_pin_names_a_version_and_not_a_moving_tag(service: str) -> None:
    """`postgres:16-alpine` floats: two developers run different databases while believing they
    run the same one, and a CI run is not reproducible a month later. The compose file says as
    much in a comment above the postgres image; this is that comment as a rule."""
    for path in (COMPOSE, WORKFLOW):
        for image in pinned_images(path).get(service, set()):
            tag = image.split(":", 1)[1]
            assert re.match(r"^\d+\.\d+", tag), (
                f"{image} in {path.name} pins a moving tag; pin a full version"
            )


def test_the_matcher_actually_finds_something() -> None:
    """⚠️ Every assertion above is vacuous if the regex stops matching — a renamed key or a
    quoted value would make `pinned_images` return `{}` and the parametrised tests would then
    be comparing two empty sets. This is what stops that from reading as a pass."""
    compose = pinned_images(COMPOSE)
    workflow = pinned_images(WORKFLOW)

    assert set(SHARED_SERVICES) <= set(compose), f"only found {sorted(compose)} in {COMPOSE.name}"
    assert set(SHARED_SERVICES) <= set(workflow), (
        f"only found {sorted(workflow)} in {WORKFLOW.name}"
    )
