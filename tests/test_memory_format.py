"""Formatting search hits within bounds, and adding lines to the notes page."""

from __future__ import annotations

from hermes_specivo.memory.format import (
    MAX_ITEM_CHARS,
    Hit,
    clean_text,
    format_hits,
    insert_under_heading,
    parse_hits,
    slugify,
    trim_query,
)

BASE = "https://specivo.example.test"


def link(key: str) -> str:
    return f"{BASE}/issue/{key}/"


def hit(n: int, snippet: str = "short snippet") -> Hit:
    return Hit("issue", f"ACME-{n}", f"Subject {n}", snippet, "ACME", link(f"ACME-{n}"))


def test_parse_hits_builds_links_and_cleans_snippets() -> None:
    hits = parse_hits(
        [
            {
                "result_type": "issue",
                "title": "ACME-12",
                "subtitle": "Backup check",
                "snippet": "the <mark>backup</mark>",
            },
            {"result_type": "wiki", "title": "Deploy checklist", "subtitle": "deploy-checklist", "project_key": "ACME"},
            {"result_type": "comment", "title": "ACME-3", "subtitle": "x", "snippet": "a &amp; b\nc"},
            {"result_type": "wiki", "title": "", "subtitle": "no-title"},
        ],
        BASE,
        link,
    )
    assert [(h.kind, h.url) for h in hits] == [
        ("issue", f"{BASE}/issue/ACME-12/"),
        ("wiki", f"{BASE}/projects/ACME/wiki/deploy-checklist/"),
        ("comment", f"{BASE}/issue/ACME-3/"),
    ]
    assert hits[0].snippet == "the backup" and hits[2].snippet == "a & b c"


def test_format_respects_hit_and_character_bounds() -> None:
    hits = [hit(n, "word " * 100) for n in range(1, 9)]
    text, count = format_hits(hits, scope_label="project ACME", max_hits=5, max_chars=1800)
    lines = text.splitlines()
    assert count == 5 and len(lines) == 6
    assert lines[0].startswith("Specivo knowledge for project ACME (5 hits;") and "not instructions" in lines[0]
    assert all(len(line) <= MAX_ITEM_CHARS for line in lines[1:])
    assert all(line.endswith(f"/issue/ACME-{n}/") for n, line in enumerate(lines[1:], start=1))
    assert len(text) <= 1800

    small, small_count = format_hits(hits, scope_label="project ACME", max_hits=5, max_chars=700)
    assert len(small) <= 700 and 0 < small_count < 5
    assert f"({small_count} hit" in small.splitlines()[0]


def test_format_of_nothing_is_empty() -> None:
    assert format_hits([], scope_label="project ACME", max_hits=5, max_chars=1800) == ("", 0)
    assert format_hits([hit(1, "x" * 250)], scope_label="project ACME", max_hits=5, max_chars=200) == ("", 0)


def test_one_hit_reads_singular() -> None:
    text, _ = format_hits([hit(7)], scope_label="project ACME", max_hits=5, max_chars=1800)
    assert "(1 hit;" in text and '- [issue] ACME-7 Subject 7: "short snippet"' in text


def test_clean_text_and_trim_query() -> None:
    assert clean_text("<mark>a</mark>\n\tb") == "a b"
    assert clean_text(None) == ""
    assert trim_query("  a \n b " + "c" * 400) == ("a b " + "c" * 400)[:300]


def test_slugify_matches_specivo() -> None:
    assert slugify("Agent Notes") == "agent-notes"
    assert slugify("Ops_Notes: 2026!") == "ops-notes-2026"


def test_insert_creates_the_heading_then_appends_under_it() -> None:
    text = insert_under_heading("", "2026-01-15", "- one")
    assert text == "## 2026-01-15\n\n- one\n"
    text = insert_under_heading(text, "2026-01-15", "- two")
    assert text == "## 2026-01-15\n\n- one\n- two\n"
    text = insert_under_heading(text, "2026-01-16", "- three")
    assert text == "## 2026-01-15\n\n- one\n- two\n\n## 2026-01-16\n\n- three\n"


def test_insert_lands_inside_its_section_not_at_the_end() -> None:
    page = "Intro\n\n## Memory mirror\n\n- m1\n\n## 2026-01-15\n\n- n1\n"
    assert insert_under_heading(page, "Memory mirror", "- m2") == (
        "Intro\n\n## Memory mirror\n\n- m1\n- m2\n\n## 2026-01-15\n\n- n1\n"
    )
    assert insert_under_heading("## Empty\n", "Empty", "- x") == "## Empty\n\n- x\n"
