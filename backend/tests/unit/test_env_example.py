"""`.env.example` documents every setting, and this is what keeps it true.

M13 found that the file had drifted **five milestones** behind: 23 of 45
settings were missing, two of them the ones production refuses to start
without, and one documented variable — `GOOGLE_REDIRECT_URI` — was read by
nothing at all. Nobody noticed, because a stale example file breaks nothing
until somebody deploys from it, and then it breaks everything at once.

It was fixed by hand, which fixes the instance and not the cause. This is the
cause: adding a field to `Settings` and forgetting the documentation was free.
Here it costs a red test naming the variable.

The check is deliberately blunt — the name appears somewhere in the file — for
the same reason `test_stub_manifest.py` is blunt: a test that also policed
values or ordering would fail on every reformat, and a test people learn to
edit past is worse than no test.
"""

from __future__ import annotations

import pathlib

from app.core.config import Settings

ENV_EXAMPLE = pathlib.Path(__file__).resolve().parents[3] / ".env.example"
"""The repo-root file, which is also the first entry in `Settings.env_file`."""


def _documented_names() -> set[str]:
    """Every `KEY=` on a line of `.env.example`, commented or not.

    Commented lines count. A setting that is documented and left commented out
    is documented; requiring it to be uncommented would force the file to carry
    a live value for every optional key, which is how an example file ends up
    with a plausible-looking secret in it.
    """
    names = set()

    for raw in ENV_EXAMPLE.read_text().splitlines():
        line = raw.lstrip("# ").strip()

        if "=" in line:
            key = line.split("=", 1)[0].strip()

            if key.isupper() and key.replace("_", "").isalnum():
                names.add(key)

    return names


def _setting_names() -> set[str]:
    """The environment variable each `Settings` field reads.

    `validation_alias` where one is set — `env` reads `APP_ENV`, and the field
    name is not the contract — and the upper-cased field name otherwise, which
    is what pydantic-settings does with `case_sensitive=False`.
    """
    names = set()

    for name, field in Settings.model_fields.items():
        alias = field.validation_alias
        names.add(str(alias) if isinstance(alias, str) else name.upper())

    return names


def test_every_setting_is_documented() -> None:
    """The direction that has already gone wrong once."""
    missing = sorted(_setting_names() - _documented_names())

    assert not missing, (
        f"{len(missing)} setting(s) exist in Settings and are absent from "
        f".env.example: {', '.join(missing)}. A deployment written from that "
        "file will not know they exist."
    )


def test_the_example_file_is_not_empty() -> None:
    """A guard on the guard.

    If `.env.example` were moved or emptied, `_documented_names()` would return
    nothing and the test above would fail loudly — but if the *parser* above
    silently stopped matching, it would return nothing and every assertion
    would still pass. This is the one that notices.
    """
    assert len(_documented_names()) > 30
