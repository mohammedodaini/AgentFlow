"""Load and render prompt templates.

Layer: prompts. Prompts are *files*, not string literals — versionable,
diffable, reviewable in a pull request, and editable by someone who does not
write Python. A prompt buried in an f-string inside a service is invisible to
everyone who most needs to read it.

Convention: `prompts/<feature>/<name>.md`, with `{placeholder}` fields.

The rule this module exists to enforce
--------------------------------------
**Fail loudly on a missing placeholder.** `str.format` already raises `KeyError`
for a field with no value, and that is the behaviour we want — but the
temptation, the first time it fires in production, is to reach for a defaulting
formatter that substitutes `""`. Do not. A prompt that silently renders with an
empty context block produces a fluent, confident answer drawn entirely from the
model's training data, cites nothing, and looks completely normal. The
`KeyError` is the cheapest possible version of that bug.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPTS_ROOT = Path(__file__).parent
"""Templates live beside this file, inside the package.

Which means they are shipped in any wheel or container image — they are code,
not configuration. `pyproject.toml` packages this directory, so a `.md` under it
travels with the module that reads it.
"""


class PromptNotFoundError(Exception):
    """A named template does not exist.

    Its own type rather than a bare `FileNotFoundError`, because the fix is
    different in kind: not "the disk is wrong" but "this name is wrong". The
    message lists what does exist, so a typo is visible immediately instead of
    sending someone to look at deployment paths.
    """


@lru_cache(maxsize=64)
def load_prompt(name: str) -> str:
    """Read `prompts/<name>.md`, cached for the life of the process.

    Cached because a prompt is read on every request and changes only on
    deploy — the alternative is a filesystem hit in the hot path of every
    answer.

    The cost is that editing a template while the server runs has no effect
    until it restarts: right for production, mildly annoying in development.
    `load_prompt.cache_clear()` is the escape hatch, and the tests use it.
    """
    path = PROMPTS_ROOT / f"{name}.md"

    if not path.is_file():
        available = sorted(
            str(candidate.relative_to(PROMPTS_ROOT).with_suffix(""))
            for candidate in PROMPTS_ROOT.rglob("*.md")
        )
        message = f"No prompt named {name!r}. Available: {', '.join(available) or 'none'}"
        raise PromptNotFoundError(message)

    return path.read_text(encoding="utf-8").strip()


def render(name: str, /, **variables: object) -> str:
    """Load `name` and substitute its placeholders.

    `name` is positional-only so that a template with its own `{name}` field is
    not shadowed by this function's parameter — a collision that would otherwise
    surface as a `TypeError` about duplicate arguments, some distance from the
    template that caused it.

    A missing placeholder raises `KeyError`. See the module docstring: that is
    the feature, not an oversight.
    """
    return load_prompt(name).format(**variables)
