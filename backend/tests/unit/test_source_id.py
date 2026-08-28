"""
Stable source_id / excerpt_id hashing. These IDs flow through the prompt
and back through the LLM response, so drift would silently break the
citation linkage at the boundary.
"""

from schemas import stable_excerpt_id, stable_source_id


def test_source_id_is_deterministic():
    url = "https://example.com/path?q=1"
    assert stable_source_id(url) == stable_source_id(url)


def test_source_id_differs_by_url():
    assert stable_source_id("https://a.com") != stable_source_id("https://b.com")


def test_source_id_strips_whitespace():
    assert stable_source_id("  https://x.com  ") == stable_source_id("https://x.com")


def test_source_id_empty_sentinel():
    assert stable_source_id("") == "src_empty"
    assert stable_source_id(None) == "src_empty"


def test_source_id_format():
    sid = stable_source_id("https://example.com")
    assert sid.startswith("src_")
    assert len(sid) == 4 + 16


def test_excerpt_id_deterministic():
    url = "https://a.com"
    trigger = "The company exports to Tehran."
    assert stable_excerpt_id(url, trigger, 0) == stable_excerpt_id(url, trigger, 0)


def test_excerpt_id_index_discriminates():
    url = "https://a.com"
    trigger = "T"
    assert stable_excerpt_id(url, trigger, 0) != stable_excerpt_id(url, trigger, 1)


def test_excerpt_id_trigger_discriminates():
    url = "https://a.com"
    assert stable_excerpt_id(url, "trigger A", 0) != stable_excerpt_id(url, "trigger B", 0)


def test_excerpt_id_format():
    eid = stable_excerpt_id("u", "t", 3)
    assert eid.startswith("exc_")
    assert len(eid) == 4 + 16
