"""Pure-logic tests for resume chunking (no Qdrant, no embeddings)."""

from interview_agent.interview.rag import _MAX_CHUNK_CHARS, chunk_markdown


def test_empty_markdown_yields_no_chunks():
    assert chunk_markdown("") == []
    assert chunk_markdown("\n\n  \n") == []


def test_small_document_is_a_single_chunk():
    md = "# Resume\n\nPython developer with 5 years of experience."
    chunks = chunk_markdown(md)
    assert len(chunks) == 1
    assert "Python developer" in chunks[0]


def test_headings_start_new_blocks_when_over_the_cap():
    # Two sections of ~700 chars each: they cannot merge under the 1200 cap,
    # so the heading boundary must produce two chunks.
    section = "x" * 700
    md = f"# Experience\n{section}\n# Education\n{section}"
    chunks = chunk_markdown(md)
    assert len(chunks) == 2
    assert chunks[0].startswith("# Experience")
    assert chunks[1].startswith("# Education")


def test_small_sections_merge_greedily():
    md = "# A\nshort\n# B\nalso short\n# C\ntiny"
    chunks = chunk_markdown(md)
    assert len(chunks) == 1  # everything fits well under the cap


def test_oversized_block_splits_on_blank_lines():
    paragraphs = ["word " * 100 for _ in range(5)]  # ~500 chars each
    md = "\n\n".join(paragraphs)  # single block of ~2500 chars, no headings
    chunks = chunk_markdown(md)
    assert len(chunks) > 1
    assert all(len(c) <= _MAX_CHUNK_CHARS for c in chunks)


def test_content_is_preserved():
    md = "# One\nalpha beta\n\ngamma\n# Two\ndelta epsilon"
    joined = "\n\n".join(chunk_markdown(md))
    for token in ("alpha", "beta", "gamma", "delta", "epsilon"):
        assert token in joined
