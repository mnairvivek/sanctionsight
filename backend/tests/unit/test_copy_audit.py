"""Frontend copy audit.

The regulated-buyer rebuild renames the LLM output from "compliance verdict"
to "investigator brief" so the tool never claims the model makes a decision.
This test guards against regressions: no user-visible surface may claim the
AI renders a verdict, clears, or decides on an entity.

Runs as a Python test (grep over source files) because the frontend does not
have its own JS test runner wired up. Benefit: ships with the normal pytest
suite, catches regressions pre-merge.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
LANDING_HTML = REPO_ROOT / "index.html"

# Words and phrases that imply automated decision-making. Each pattern is a
# regex (case-insensitive). Matches in comments are still flagged — any
# mention of "verdict" in user-facing React source is a signal that the
# rebuild has drifted.
BANNED_PATTERNS = [
    r"\bverdict\b",
    r"compliance verdict",
    r"\bAI decides\b",
    r"\bmodel decides\b",
    r"\bclears the entity\b",
    r"\bclears? you\b",
    r"automatic(ally)? clear(ed|s)?",
]

# Known safe exceptions — legacy payload keys we must still read from older
# saved jobs, and internal state machine values that are not user-visible.
ALLOWED_SNIPPETS = [
    "llm_verdict",  # legacy payload key, only in a back-compat fallback
]


def _iter_surface_files():
    for path in FRONTEND_SRC.rglob("*"):
        if path.suffix in {".jsx", ".js", ".tsx", ".ts", ".html"} and path.is_file():
            yield path
    if LANDING_HTML.is_file():
        yield LANDING_HTML


def _strip_allowed(text: str) -> str:
    for allowed in ALLOWED_SNIPPETS:
        text = text.replace(allowed, "")
    return text


@pytest.mark.parametrize("pattern", BANNED_PATTERNS)
def test_banned_copy_absent(pattern: str) -> None:
    regex = re.compile(pattern, re.IGNORECASE)
    offenders: list[tuple[Path, int, str]] = []
    for path in _iter_surface_files():
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_no, line in enumerate(raw.splitlines(), start=1):
            scrubbed = _strip_allowed(line)
            if regex.search(scrubbed):
                offenders.append((path.relative_to(REPO_ROOT), line_no, line.strip()))
    assert not offenders, (
        f"Banned copy pattern {pattern!r} found in user-facing source:\n"
        + "\n".join(f"  {p}:{n} → {t}" for p, n, t in offenders)
    )


def test_investigator_brief_copy_present() -> None:
    """Positive check: the new phrase must exist somewhere user-visible so we
    catch a merge that accidentally strips it."""
    found = False
    for path in _iter_surface_files():
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "investigator brief" in raw.lower():
            found = True
            break
    assert found, "Expected the phrase 'investigator brief' to appear in user-facing copy."
