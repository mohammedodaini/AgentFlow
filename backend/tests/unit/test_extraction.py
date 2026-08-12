"""Text extraction: every failure a user can cause (M5).

`extract_text` is the only part of ingestion that touches untrusted input, so
it is the part most likely to be handed something malformed — and the part
whose error messages a user actually reads, hours later, with no other context.
Each test below pins one message's *usefulness*, not just its existence.
"""

from __future__ import annotations

import io

import pytest
from pypdf import PdfWriter

from app.core.config import get_settings
from app.core.exceptions import DocumentIngestionError
from app.rag.ingestion import SUPPORTED_MIME_TYPES, extract_text
from tests.factories import TEXT_BYTES, make_pdf


def _encrypted_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("hunter2")

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _pdf_without_a_text_layer() -> bytes:
    """A structurally perfect PDF with nothing to extract — a scan, in effect."""
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


def test_a_pdf_with_a_text_layer_is_extracted() -> None:
    text = extract_text(make_pdf("Hello AgentFlow"), "application/pdf")

    assert "Hello AgentFlow" in text


def test_a_corrupt_pdf_fails_with_an_actionable_message() -> None:
    """Truncated uploads are the single most common real-world failure."""
    truncated = make_pdf()[:120]

    with pytest.raises(DocumentIngestionError) as error:
        extract_text(truncated, "application/pdf")

    assert "corrupt or truncated" in str(error.value)


def test_a_password_protected_pdf_is_refused_by_name() -> None:
    """pypdf can open some encrypted PDFs with an empty password. A document
    whose owner set a password is one whose owner meant to restrict it, so the
    correct behaviour is to refuse — and to say which problem it is."""
    with pytest.raises(DocumentIngestionError) as error:
        extract_text(_encrypted_pdf(), "application/pdf")

    assert "password" in str(error.value).lower()


def test_a_scanned_pdf_says_ocr_is_not_supported() -> None:
    """The failure that would otherwise be silent.

    Without this branch a scanned PDF extracts to `""`, the document is marked
    `ready`, and it stays permanently unsearchable with nothing to indicate
    why. Failing loudly is the whole point.
    """
    with pytest.raises(DocumentIngestionError) as error:
        extract_text(_pdf_without_a_text_layer(), "application/pdf")

    message = str(error.value)
    assert "OCR" in message
    assert "scanned" in message


# --------------------------------------------------------------------------
# Plain text
# --------------------------------------------------------------------------


def test_utf8_text_is_decoded() -> None:
    assert "second paragraph" in extract_text(TEXT_BYTES, "text/plain")


def test_a_byte_order_mark_is_stripped() -> None:
    """Windows editors prepend a BOM. Left in, it becomes a stray character at
    the start of the first chunk — and therefore in the first citation."""
    text = extract_text(b"\xef\xbb\xbfHello", "text/markdown")

    assert text == "Hello"


def test_cp1252_text_is_decoded_rather_than_rejected() -> None:
    r"""Real files exported from Office are cp1252. `\x92` is a curly
    apostrophe there and invalid UTF-8, so the fallback is what makes an
    ordinary business document ingestible instead of a support ticket."""
    text = extract_text(b"Don\x92t panic", "text/plain")

    assert "Don" in text
    assert "t panic" in text


def test_binary_disguised_as_text_is_refused() -> None:
    """Encoding fallbacks alone cannot tell text from binary.

    Only five of cp1252's 256 bytes are undefined, so a `.zip` renamed `.txt`
    decodes perfectly happily into noise. Without the printable-character check
    this produces a document that is `ready`, searchable, and entirely
    meaningless — worse than a failure, because nothing about it looks wrong.
    """
    binary = b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + bytes(range(0, 32)) * 4

    with pytest.raises(DocumentIngestionError) as error:
        extract_text(binary, "text/plain")

    assert "binary" in str(error.value)


def test_an_empty_file_is_a_failure_not_an_empty_document() -> None:
    """Almost always a truncated upload. Letting it through would create a
    document that is permanently `ready` and permanently unsearchable."""
    with pytest.raises(DocumentIngestionError) as error:
        extract_text(b"   \n\t  ", "text/plain")

    assert "empty" in str(error.value)


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


def test_an_unsupported_type_lists_the_ones_that_work() -> None:
    """An "unsupported media type" with no list is an error nobody can act on."""
    with pytest.raises(DocumentIngestionError) as error:
        extract_text(b"PK\x03\x04", "application/zip")

    message = str(error.value)
    assert "application/zip" in message
    assert "application/pdf" in message


def test_a_client_lying_about_the_type_fails_cleanly() -> None:
    """A zip announced as a PDF reaches the PDF parser. It must produce a
    readable failure, not a traceback — the upload path trusts `Content-Type`,
    so this is the branch that contains the damage."""
    with pytest.raises(DocumentIngestionError):
        extract_text(b"PK\x03\x04not a pdf at all", "application/pdf")


def test_every_accepted_upload_type_can_actually_be_parsed() -> None:
    """The consistency check between policy and capability.

    `allowed_upload_mime_types` is configuration and `SUPPORTED_MIME_TYPES` is
    code. A type accepted at the door but unparseable in the worker is a
    guaranteed `status=failed` — a user-visible bug produced entirely by two
    lists drifting apart, which is exactly the kind of thing nobody notices
    until a customer reports it.
    """
    accepted = set(get_settings().allowed_upload_mime_types)

    assert accepted <= SUPPORTED_MIME_TYPES, (
        f"accepted but unparseable: {sorted(accepted - SUPPORTED_MIME_TYPES)}"
    )
