"""Stamping a served lesson PDF with the account it was served to.

Nothing here prevents a copy being made. It cannot: the bytes reach the
browser, and a teacher who is entitled to open a lesson can save it from the
address bar in two steps. What this does is make a copy that escapes name the
account it was served to, which is the part that was actually missing.

The mark is a small grey line along the foot of each page, not a banner across
the middle. These PDFs are projected to a class for forty minutes at a time,
and a name written over the teaching material is a cost paid in every lesson to
deter a leak that may never happen. A footer can be cropped by someone who
means to; someone who means to has easier routes than cropping.
"""

from __future__ import annotations

from pathlib import Path

from app.models import User

# Bottom-left, a third of an inch in, in the grey of a page number.
_MARGIN = 36.0
_BASELINE = 18.0
_SIZE = 7.0
_GREY = (0.45, 0.45, 0.45)


def mark_for(user: User) -> str:
    """The line stamped onto every page: the account this copy went to.

    The address alone, deliberately. A display name is free text, is not unique
    and can be edited; the address is the account. Nothing else is in the line
    because everything else made it longer without making it identify anyone
    more precisely.
    """
    return f"Served to {user.email} · IM-Telligence"


def stamp(path: Path, mark: str) -> bytes | None:
    """The PDF with ``mark`` along the foot of every page, or None if it can't be.

    None is not an error for the caller to raise on. A lesson that will not
    stamp is still a lesson the teacher is entitled to open, and refusing it
    would take a class off the air to protect a footer. The caller serves the
    original and the copy is untraceable — which is where we were before this
    existed, not somewhere worse.
    """
    try:
        import pymupdf
    except Exception:  # pragma: no cover - import guard, as in pdf_render
        return None

    try:
        with pymupdf.open(str(path)) as doc:
            # An encrypted PDF opens but will not take an edit, and a file with
            # no pages has nothing to stamp.
            if doc.needs_pass or doc.page_count == 0:
                return None
            for page in doc:
                # `page.rect` is rotation-adjusted but `insert_text` places into
                # unrotated space, so on a rotated page the line lands along a
                # different edge. It is still on the page and still says whose
                # copy it is, which is all the line is for.
                page.insert_text(
                    (_MARGIN, page.rect.height - _BASELINE),
                    mark,
                    fontsize=_SIZE,
                    fontname="helv",
                    color=_GREY,
                    overlay=True,
                )
            return doc.tobytes()
    except Exception:
        return None
