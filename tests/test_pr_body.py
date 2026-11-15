import json
import re

import veracode_upstream_sync as vus

GITHUB_MAX = 65536


class _Up:
    sha = "b" * 40

    def web_url(self):
        return "https://github.com/veracode/github-actions-integration"


def _big_plan(n=400):
    plan = vus.Plan("a" * 40, "marker", {})
    for i in range(n):
        plan.add(
            True,
            f"Resolve conflict {i} in `.github/workflows/w{i}.yml`",
            "conflict",
            rows=[
                {
                    "action": "Resolve merge conflict",
                    "where": f".github/workflows/w{i}.yml line {i}",
                    "details": "Keep yours: " + "x" * 150,
                }
            ],
        )
        plan.changes["conflict"].append(f".github/workflows/w{i}.yml")
        plan.changes["updated"].append(f".github/workflows/u{i}-" + "y" * 100 + ".yml")
    return plan


def _effort(body):
    return vus._pr_effort({"body": body, "draft": True})


def _balanced(body):
    return body.count("<details>") == body.count("</details>")


def test_pr_body_with_effort_tag_fits_github_limit():
    plan = _big_plan()
    body = vus.with_effort_tag(vus.pr_body(plan, _Up()), vus.plan_details(plan))
    assert len(body) < GITHUB_MAX - 1000
    assert vus.TRUNCATED_NOTE in body
    assert _balanced(body.split(vus.EFFORT_TAG)[0])
    data = _effort(body)["details"]  # the tag survives and still parses
    assert data["effort"] == plan.effort and data["items"]


def test_cut_is_on_a_line_boundary():
    plan = _big_plan()
    full = vus.pr_body(plan, _Up())
    body = vus.with_effort_tag(full, vus.plan_details(plan)).split("\n" + vus.EFFORT_TAG)[0]
    kept = body.split("\n\n" + vus.TRUNCATED_NOTE)[0]
    kept = re.sub(r"\n\n</details>$", "", kept)
    assert full.startswith(kept + "\n")


def test_huge_tag_items_are_trimmed():
    details = {"effort": "long", "work": "w", "items": [{"x": "z" * 1000} for _ in range(200)]}
    body = vus.with_effort_tag("short body", details)
    assert len(body) < GITHUB_MAX - 1000
    assert body.startswith("short body\n")
    data = _effort(body)["details"]
    assert data["items_truncated"] is True and 0 < len(data["items"]) < 200


def test_short_body_is_unchanged():
    details = {"effort": "ready", "work": "", "items": []}
    body = vus.with_effort_tag("hello -- world", details)
    assert body == f"hello -- world\n{vus.EFFORT_TAG}{json.dumps(details)} -->"


def test_revert_body_is_capped():
    plan = vus.Plan(None, "revert", {})
    for i in range(3000):
        plan.add(True, f"`f{i}-" + "q" * 40 + "` changed since the update: revert it by hand", "conflict")
        plan.changes["updated"].append(f"f{i}-" + "q" * 40)
    body = vus.with_effort_tag(vus.revert_body(plan, {"number": 5}, "c" * 40), vus.plan_details(plan))
    assert len(body) < GITHUB_MAX - 1000
    assert vus.TRUNCATED_NOTE in body and _effort(body)


def test_action_versions_heading_is_neutral():
    plan = vus.Plan("a" * 40, "marker", {})
    plan.versions = ["`x/y` v2 to v3", "kept newer local v5 (upstream v4)"]
    body = vus.pr_body(plan, _Up())
    assert "**Action versions**" in body
    assert "yours replaced" not in body
