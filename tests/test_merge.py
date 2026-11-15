"""merge_repo: file modes and encodings."""

import hashlib

from fakes import merge_repo as _merge_repo


# --------------------------------------------------------------------------- #
# File modes and encodings
# --------------------------------------------------------------------------- #
def test_upstream_mode_change_is_applied():
    blobs = {"s1": b"#!/bin/sh\necho hi\n", "cfg": b"a: 1\n"}
    ours = {"helper/run.sh": ("100644", "s1"), "veracode.yml": ("100644", "cfg")}
    theirs = {"helper/run.sh": ("100755", "s1"), "veracode.yml": ("100644", "cfg")}
    _, plan = _merge_repo(blobs, ours, dict(ours), theirs)
    assert plan.final["helper/run.sh"] == ("100755", "s1")
    assert "helper/run.sh" in plan.changes["updated"]


def test_local_mode_change_survives_upstream_content_change():
    blobs = {"s1": b"#!/bin/sh\necho hi\n", "s2": b"#!/bin/sh\necho bye\n"}
    ours = {"helper/run.sh": ("100755", "s1")}
    base = {"helper/run.sh": ("100644", "s1")}
    theirs = {"helper/run.sh": ("100644", "s2")}
    _, plan = _merge_repo(blobs, ours, base, theirs)
    assert plan.final["helper/run.sh"][0] == "100755"
    assert plan.final["helper/run.sh"][1] == hashlib.sha1(blobs["s2"]).hexdigest()


def test_conflicting_mode_changes_keep_yours_with_a_note():
    blobs = {"s1": b"x\n"}
    ours = {"helper/run.sh": ("100755", "s1")}
    base = {"helper/run.sh": ("120000", "s1")}
    theirs = {"helper/run.sh": ("100644", "s1")}
    _, plan = _merge_repo(blobs, ours, base, theirs)
    assert plan.final["helper/run.sh"][0] == "100755"
    assert any("mode" in text for _, text in plan.todo)


def test_non_utf8_text_is_never_written_with_replacement_chars():
    blobs = {
        "b": "name: caf\u00e9\nx: 1\ny: 1\nz: 1\n".encode("latin-1"),
        "o": "name: caf\u00e9 local\nx: 1\ny: 1\nz: 1\n".encode("latin-1"),
        "t": "name: caf\u00e9\nx: 1\ny: 1\nz: 2\n".encode("latin-1"),
    }
    path = "helper/notes.txt"
    cust, plan = _merge_repo(blobs, {path: ("100644", "o")}, {path: ("100644", "b")}, {path: ("100644", "t")})
    data = cust.blob(plan.final[path][1])
    assert "\ufffd".encode() not in data
    assert data == blobs["t"]  # treated like a binary: upstream's bytes, and the PR says so
    assert any("replaced" in text for _, text in plan.todo)


def test_utf8_text_still_merges():
    blobs = {
        "b": "name: caf\u00e9\nx: 1\ny: 1\nz: 1\n".encode(),
        "o": "name: caf\u00e9 local\nx: 1\ny: 1\nz: 1\n".encode(),
        "t": "name: caf\u00e9\nx: 1\ny: 1\nz: 2\n".encode(),
    }
    path = "helper/notes.txt"
    cust, plan = _merge_repo(blobs, {path: ("100644", "o")}, {path: ("100644", "b")}, {path: ("100644", "t")})
    assert cust.blob(plan.final[path][1]) == "name: caf\u00e9 local\nx: 1\ny: 1\nz: 2\n".encode()
    assert plan.changes["merged"] == [path]


def test_upstream_mode_change_with_local_content_change():
    blobs = {"b": b"1\n2\n3\n4\n", "o": b"1 local\n2\n3\n4\n", "t": b"1\n2\n3\n4 upstream\n"}
    ours = {"helper/run.sh": ("100644", "o")}
    base = {"helper/run.sh": ("100644", "b")}
    theirs = {"helper/run.sh": ("100755", "t")}
    cust, plan = _merge_repo(blobs, ours, base, theirs)
    assert plan.final["helper/run.sh"][0] == "100755"
    assert cust.blob(plan.final["helper/run.sh"][1]) == b"1 local\n2\n3\n4 upstream\n"
