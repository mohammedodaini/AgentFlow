"""Load prompt templates from this package (prompts are FILES, not string
literals — versionable, diffable, editable by non-engineers).

Convention: prompts/<agent_or_feature>/<name>.md with {placeholders}.
"""

from __future__ import annotations


# TODO(M7): load_prompt(name) -> str (cached), render(name, **variables)
#           (fail LOUD on missing placeholder — silent template bugs are evil)
