"""Bound the source text sent in full to every interview model turn."""

MAX_RESUME_CHARS = 30_000
MAX_JOB_OFFER_CHARS = 20_000


def validate_source_documents(resume_markdown: str, job_offer: str) -> None:
    """Reject oversized sources rather than silently truncating evidence."""
    for label, text, limit in (
        ("Resume text", resume_markdown, MAX_RESUME_CHARS),
        ("Job offer", job_offer, MAX_JOB_OFFER_CHARS),
    ):
        if len(text) > limit:
            raise ValueError(
                f"{label} exceeds the {limit:,} character limit. Use a shorter document."
            )
