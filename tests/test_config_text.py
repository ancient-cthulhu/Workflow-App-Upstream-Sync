"""veracode.yml: comment-preserving text edits and the settings report."""

import veracode_upstream_sync as vus
from fakes import merge_repo


def test_merge_config_empty_section_in_base_and_upstream():
    b = "a: 1\nfoo: {}\n"
    t = "a: 2\nfoo: {}\n"
    merged, rep = vus.merge_config(b, b, t, b, True)
    assert merged == b
    assert rep["kept"] == ["`a`: 1 (upstream default is now 2)"]


def test_insert_setting_keeps_crlf():
    merged = "a: 1\r\nb: 2\r\n"
    theirs = "a: 1\nb: 2\n# new\nc: 3\n"
    new = vus.insert_setting(merged, theirs, ("c",))
    assert new == "a: 1\r\nb: 2\r\n\r\n# new\r\nc: 3\r\n"  # a blank line before a new top-level key


def test_insert_setting_nested_keeps_crlf():
    merged = "a:\r\n  x: 1\r\nb: 2\r\n"
    theirs = "a:\n  x: 1\n  y: 2\nb: 2\n"
    assert vus.insert_setting(merged, theirs, ("a", "y")) == "a:\r\n  x: 1\r\n  y: 2\r\nb: 2\r\n"


def test_insert_setting_lf_unchanged():
    merged = "a:\n  x: 1\nb: 2\n"
    theirs = "a:\n  x: 1\n  y: 2\nb: 2\n"
    assert vus.insert_setting(merged, theirs, ("a", "y")) == theirs


def test_restore_setting_keeps_crlf():
    merged = "a:\r\n  x: 1\r\nb: 2\r\n"
    base = "a:\n  x: 5\nb: 2\n"
    assert vus.restore_setting(merged, base, ("a",)) == "a:\r\n  x: 5\r\nb: 2\r\n"


def test_locate_skips_lines_inside_multiline_quoted_scalar():
    lines = 'a:\n  d: "x\n  b: fake"\n  b: 1\n'.splitlines(keepends=True)
    assert vus.locate(lines, ["a", "b"]) == (3, 4)


def test_locate_block_end_ignores_less_indented_string_continuation():
    lines = "a:\n  d: 'x\nb: not a key'\n  e: 1\nb: 2\n".splitlines(keepends=True)
    assert vus.locate(lines, ["a"]) == (0, 4)
    assert vus.locate(lines, ["b"]) == (4, 5)


def test_restore_setting_next_to_multiline_string():
    merged = 'a:\n  d: "x\n  b: fake"\n  b: 1\n'
    base = 'a:\n  d: "x\n  b: fake"\n  b: 9\n'
    assert vus.restore_setting(merged, base, ("a", "b")) == base


def test_adopt_new_defaults_reports_default_change_in_unmodified_config():
    blobs = {"b": b"scan: true\n", "t": b"scan: false\nnew: 1\n"}
    ours = {"veracode.yml": ("100644", "b")}
    cust, plan = merge_repo(blobs, ours, dict(ours), {"veracode.yml": ("100644", "t")}, keep_defaults=False)
    assert cust.text(plan.final["veracode.yml"]) == "scan: false\nnew: 1\n"
    assert plan.changes["updated"] == ["veracode.yml"]
    assert plan.config["defaults"] == [("scan", "true to false")]
    assert plan.config["added"] == ["new"]
    assert any("scan" in text for _, text in plan.todo)
