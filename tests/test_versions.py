"""Action version conflicts: upstream's newer version wins, a newer local one is kept."""

import veracode_upstream_sync as vus

L = ("your version", "previous upstream", "new upstream")


def _wf(ref, depth):
    return f"jobs:\n  x:\n    steps:\n      - uses: actions/checkout@{ref}\n        with:\n          fetch-depth: {depth}\n"


def _versions(o, b, t):
    merged, conflicts = vus.merge_text(o, b, t, L)
    assert conflicts, "expected a raw conflict first"
    return vus.take_upstream_versions(merged)


def test_local_version_ahead_of_upstream_is_not_downgraded():
    out, conflicts = _versions(_wf("v5", 0), _wf("v3", 0), _wf("v4", 1))
    assert not conflicts
    assert "checkout@v5" in out and "fetch-depth: 1" in out


def test_upstream_version_ahead_of_local_is_taken():
    out, conflicts = _versions(_wf("v4", 0), _wf("v3", 0), _wf("v5.1", 1))
    assert not conflicts and "checkout@v5.1" in out


def test_branch_ref_that_differs_stays_a_conflict():
    out, conflicts = _versions(_wf("my-branch", 0), _wf("v3", 0), _wf("v4", 1))
    assert conflicts == 1 and "<<<<<<<" in out


def test_kept_newer_local_version_is_reported():
    o, b, t = _wf("v5", 0), _wf("v3", 0), _wf("v4", 1)
    merged = vus.take_upstream_versions(vus.merge_text(o, b, t, L)[0])[0]
    notes = vus.versions_taken(o, b, merged, t)
    assert notes == ["`actions/checkout`: kept newer local v5 (upstream v4)"]


def test_replaced_local_version_is_still_reported():
    o, b, t = _wf("v4", 0), _wf("v3", 0), _wf("v5", 1)
    merged = vus.take_upstream_versions(vus.merge_text(o, b, t, L)[0])[0]
    assert vus.versions_taken(o, b, merged, t) == ["`actions/checkout`: v4 to v5"]


def test_keep_newer_local_same_action_twice_in_a_hunk():
    yours = ["- uses: a/b@v5\n", "- uses: a/b@v9\n"]
    base = ["- uses: a/b@v1\n", "- uses: a/b@v1\n"]
    upstream = ["- uses: a/b@v1\n", "- uses: a/b@v1\n"]
    assert vus.keep_newer_local(yours, base, upstream) == yours


def test_keep_newer_local_keeps_each_occurrence_ahead():
    yours = ["- uses: a/b@v2\n", "- uses: a/b@v9\n"]
    base = ["- uses: a/b@v1\n", "- uses: a/b@v1\n"]
    upstream = ["- uses: a/b@v3\n", "- uses: a/b@v3\n"]
    assert vus.keep_newer_local(yours, base, upstream) == ["- uses: a/b@v3\n", "- uses: a/b@v9\n"]
