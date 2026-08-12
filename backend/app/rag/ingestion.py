"""Document bytes → text. The parsing half of ingestion.

Layer: rag. Called by `app/workers/tasks/ingestion.py`, never from a request.

Everything here is *synchronous and blocking*, deliberately. PDF parsing is
CPU-bound: `async def` would buy nothing, because there is no I/O to await, and
it would actively mislead — an `await` that never yields still blocks the event
loop for the whole parse. The worker calls this through `asyncio.to_thread`,
which is where that problem is actually solved. Marking a CPU-bound function
`async` is how it gets hidden instead.

The pipeline this feeds: parse (here) → chunk → embed → store (all M6).
"""

from __future__ import annotations

import io

import structlog
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from app.core.exceptions import DocumentIngestionError

logger = structlog.get_logger(__name__)

PDF_MIME_TYPE = "application/pdf"
PLAIN_TEXT_MIME_TYPES = frozenset({"text/plain", "text/markdown"})

SUPPORTED_MIME_TYPES = frozenset({PDF_MIME_TYPE}) | PLAIN_TEXT_MIME_TYPES
"""What this module can actually read.

`Settings.allowed_upload_mime_types` must stay a subset of this, and a unit
test asserts exactly that. The two lists exist separately because they answer
different questions — "what will we accept?" is policy and belongs in
configuration; "what can we parse?" is capability and belongs in code — but a
type accepted at the door and unparseable in the worker means a guaranteed
`status=failed`, which is the worst possible way to discover a mismatch.
"""

_TEXT_ENCODINGS = ("utf-8-sig", "cp1252")
"""Tried in order.

`utf-8-sig` rather than plain `utf-8`, and it must come first. It is identical
to UTF-8 except that it strips a leading byte order mark — the three bytes
Windows editors prepend. Listing `utf-8` ahead of it is a subtle bug rather
than a redundancy: a BOM'd file decodes *successfully* as UTF-8, with the mark
surviving as a U+FEFF character at the very start of the text, so the sig codec
is never reached and every first chunk begins with an invisible stray
character. A test caught exactly that.

cp1252 is the last resort, and it is what files exported from Office actually
are. Latin-1 is absent because it decodes *any* byte sequence without
complaint — but so, very nearly, does cp1252: only five of its 256 bytes are
undefined. Encoding fallbacks alone therefore cannot tell text from binary,
which is what `_looks_like_text` is for.
"""

_MAX_CONTROL_CHARACTER_RATIO = 0.05
"""How much unprintable content a file may contain and still be called text.

Five percent is generous for prose and hopeless for a renamed archive. The
check exists because the encoding fallbacks above are far more permissive than
they look: without it, uploading `photos.zip` as `.txt` produces a document
that is `ready`, searchable, and made entirely of mojibake — which is worse
than a failure, because nothing about it looks wrong.
"""


def _looks_like_text(value: str) -> bool:
    """Reject decoded output that is obviously not prose.

    A NUL byte is decisive on its own: text files do not contain them, and
    every common binary format does. The ratio test catches the rest.
    """
    if "\x00" in value:
        return False

    controls = sum(
        1 for character in value if not character.isprintable() and character not in "\n\r\t"
    )
    return controls / len(value) <= _MAX_CONTROL_CHARACTER_RATIO


def extract_text(data: bytes, mime_type: str) -> str:
    """Turn stored bytes into text, or explain why that is impossible.

    Dispatches on the declared MIME type. A client that lies about the type
    reaches the wrong parser and gets a `DocumentIngestionError`, which becomes
    `status=failed` with a readable message — not a crash, and not a silently
    empty document.

    Every failure raises `DocumentIngestionError` with a message written for
    the person who uploaded the file. That is the contract with the worker: it
    catches this one type and copies `.message` straight onto
    `documents.error`, which the API returns verbatim.
    """
    if mime_type == PDF_MIME_TYPE:
        return _extract_pdf(data)

    if mime_type in PLAIN_TEXT_MIME_TYPES:
        return _decode_plain_text(data)

    supported = ", ".join(sorted(SUPPORTED_MIME_TYPES))
    message = f"Cannot extract text from {mime_type!r}. Supported types: {supported}."
    raise DocumentIngestionError(message)


def _extract_pdf(data: bytes) -> str:
    """Pull the text layer out of a PDF.

    Three distinct failures get three distinct messages, because the user's
    next action differs in each case: a corrupt file needs re-exporting, an
    encrypted one needs the password removed, and a scanned one needs OCR this
    project does not have yet. Collapsing them into "could not read PDF" would
    leave every one of those users with nowhere to go.
    """
    try:
        reader = PdfReader(io.BytesIO(data))
    except (PyPdfError, ValueError, OSError) as error:
        message = f"Could not read the PDF — the file may be corrupt or truncated ({error})."
        raise DocumentIngestionError(message) from error

    if reader.is_encrypted:
        # pypdf can open some encrypted PDFs with an empty password, but a
        # document whose owner set a password is one whose owner meant to
        # restrict it. Refusing is the correct default.
        message = "This PDF is password-protected. Remove the password and upload it again."
        raise DocumentIngestionError(message)

    try:
        pages = [page.extract_text() or "" for page in reader.pages]
    except (PyPdfError, ValueError) as error:
        message = f"Could not extract text from the PDF ({error})."
        raise DocumentIngestionError(message) from error

    # Blank line between pages: it is a real boundary, and at M6 the chunker
    # uses paragraph breaks to avoid splitting mid-sentence. Joining pages with
    # nothing would fuse the last line of one page to the first of the next.
    text = "\n\n".join(page for page in pages if page.strip())

    if not text.strip():
        message = (
            "No text could be extracted. The PDF appears to contain only scanned "
            "images; OCR is not supported yet, so upload a version with a text layer."
        )
        raise DocumentIngestionError(message)

    logger.debug("ingestion.pdf_extracted", pages=len(pages), characters=len(text))
    return text


def _decode_plain_text(data: bytes) -> str:
    """Decode bytes that are already text, trying the encodings that occur.

    An empty file is a failure rather than an empty string. It is almost always
    a truncated upload or a mistake, and letting it through would create a
    document that is permanently `ready` and permanently unsearchable — which
    nobody would ever think to investigate.
    """
    for encoding in _TEXT_ENCODINGS:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue

        if not text.strip():
            message = "The file is empty."
            raise DocumentIngestionError(message)

        if not _looks_like_text(text):
            # It decoded, and it is not text. Trying the next encoding would
            # only find another way to render the same bytes as noise, so stop.
            break

        logger.debug("ingestion.text_decoded", encoding=encoding, characters=len(text))
        return text

    message = (
        "Could not read the file as text — it appears to be binary. "
        "Save it as UTF-8 and upload it again."
    )
    raise DocumentIngestionError(message)
