"""Composing an email from an instruction, and refusing to (M14).

The parser is strict for the reason `calendar/tools.py` gives and more so: a
half-understood calendar instruction puts a meeting at the wrong time, and a
half-understood email instruction sends the wrong words to a real person, under
the user's name, with no way to recall it.

So most of these tests are about what it *refuses*.
"""

from __future__ import annotations

from app.agents.email.tools import (
    MAX_BODY_LENGTH,
    PROPOSED_ACTION_KIND,
    describe,
    parse_draft_request,
)


def test_a_well_formed_instruction_is_parsed() -> None:
    action = parse_draft_request(
        "Email ada@example.test about the Q3 numbers saying the report is ready."
    )

    assert action == {
        "kind": PROPOSED_ACTION_KIND,
        "to": "ada@example.test",
        "subject": "the Q3 numbers",
        "body": "the report is ready.",
    }


def test_no_recipient_is_a_refusal() -> None:
    """There is no sensible default for "who". Guessing sends mail to a stranger."""
    assert parse_draft_request("Email the team saying we shipped") is None


def test_no_body_is_a_refusal() -> None:
    """An email with no subject is unhelpful; an email with no body is not worth
    sending, and inventing one is exactly what there is no model here to do."""
    assert parse_draft_request("Email ada@example.test about the Q3 numbers") is None


def test_an_empty_body_is_a_refusal() -> None:
    assert parse_draft_request("Email ada@example.test saying    ") is None


def test_a_missing_subject_falls_back_rather_than_refusing() -> None:
    """Unlike the recipient and the body, this one has a safe default — and Gmail's
    own is the same string."""
    action = parse_draft_request("Email ada@example.test saying we shipped")

    assert action is not None
    assert action["subject"] == "(no subject)"


def test_the_subject_cannot_be_rewritten_from_inside_the_body() -> None:
    """**The one that would be a real bug.**

    The subject is searched for only in the text *before* the body. Scanning the
    whole instruction would let a message that happens to contain the word "about"
    change what the email claims to be about — and the person approving reads the
    subject.
    """
    action = parse_draft_request(
        "Email ada@example.test about the Q3 numbers saying I heard about the layoffs"
    )

    assert action is not None
    assert action["subject"] == "the Q3 numbers"
    assert action["body"] == "I heard about the layoffs"


def test_a_second_address_in_the_body_does_not_change_the_recipient() -> None:
    """The first address wins, and it is the one before the body. A message quoting
    somebody else's address must not redirect the mail to them."""
    action = parse_draft_request(
        "Email ada@example.test saying please copy grace@example.test next time"
    )

    assert action is not None
    assert action["to"] == "ada@example.test"


def test_a_long_body_is_truncated_rather_than_stored_whole() -> None:
    """It goes into JSONB, into an approval summary, and eventually into a prompt.
    An unbounded body is an unbounded prompt."""
    action = parse_draft_request(f"Email ada@example.test saying {'x' * (MAX_BODY_LENGTH + 500)}")

    assert action is not None
    assert len(action["body"]) == MAX_BODY_LENGTH


def test_whitespace_is_collapsed() -> None:
    action = parse_draft_request("Email ada@example.test saying   we   shipped\n\n  it")

    assert action is not None
    assert action["body"] == "we shipped it"


def test_the_separator_is_case_insensitive() -> None:
    action = parse_draft_request("email ada@example.test ABOUT Q3 SAYING done")

    assert action is not None
    assert action["subject"] == "Q3"


def test_a_trailing_dot_is_not_treated_as_part_of_the_domain() -> None:
    """`ada@example.test.` would be an address nobody can deliver to."""
    action = parse_draft_request("Email ada@example.test. saying hello")

    assert action is not None
    assert action["to"] == "ada@example.test"


def test_the_description_names_the_recipient_and_subject() -> None:
    """Rendered by code, never by a model (ADR-0015). It deliberately does not
    summarise the body: a summary of an email is a second account of it, and the
    body is what actually gets sent. The full text is in `requested_action`."""
    action = parse_draft_request("Email ada@example.test about Q3 saying done")

    assert action is not None
    assert describe(action) == "Send an email to ada@example.test with the subject 'Q3'"
