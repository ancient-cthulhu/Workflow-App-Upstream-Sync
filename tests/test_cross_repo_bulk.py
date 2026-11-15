"""Cross-repo: Workflow-App-Bulk-Enterprise-Integration replaces upstream's
veracode/container_iac_secrets_scanning step with a Veracode CLI step marked
`# veracode-iac-rbac`. An upstream release that changes that step must not turn
into a conflict: the replacement is kept, upstream's other changes are taken.

Fixtures (tests/fixtures/): upstream's IaC workflow at two releases (v1.1.2 and
main) and Bulk's conversion of the first one, with its long CLI script trimmed."""

import argparse
import os
import subprocess
from pathlib import Path

import yaml

import veracode_upstream_sync as vus

FIXTURES = Path(__file__).parent / "fixtures"
IAC = ".github/workflows/veracode-iac-secrets-scan.yml"
ACTION = "veracode/container_iac_secrets_scanning"
ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def fixture(name, crlf=False):
    text = (FIXTURES / name).read_bytes().decode().replace("\r\n", "\n")  # checkout may add CRs
    return text.replace("\n", "\r\n") if crlf else text


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=ENV, text=True).stdout


def commit(repo, files, message):
    for path, text in files.items():
        p = Path(repo) / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode())
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


def _steps(text, job="veracode-iac-secrets-scan"):
    return yaml.safe_load(text)["jobs"][job]["steps"]


def _sync(tmp_path, crlf=False):
    up_src = tmp_path / "up"
    up_src.mkdir()
    git(up_src, "init", "-q", "-b", "main")
    git(up_src, "config", "core.autocrlf", "false")
    commit(up_src, {"veracode.yml": "a: 1\n", IAC: fixture("iac-upstream-v1.yml")}, "v1")
    git(up_src, "tag", "v1.1.2")
    commit(up_src, {IAC: fixture("iac-upstream-v2.yml")}, "v2")
    git(up_src, "tag", "v1.2.0")

    cust_src = tmp_path / "cust"
    git(tmp_path, "clone", "-q", "-b", "v1.1.2", str(up_src), str(cust_src))
    git(cust_src, "config", "core.autocrlf", "false")
    git(cust_src, "checkout", "-qb", "main")
    commit(cust_src, {IAC: fixture("iac-bulk-v1.yml", crlf)}, "Bulk: run IaC scans with the Veracode CLI")

    work = tmp_path / "w"
    work.mkdir()
    up = vus.Upstream(str(up_src), vus.LATEST, work)
    cust = vus.clone(str(cust_src), work / "c.git", vus.git_env(), branch="main")
    head = cust.rev("HEAD")
    ours = cust.ls_tree(head)
    base, how = vus.find_base(cust, head, ours, up)
    assert how == "shared history"
    plan = vus.merge_repo(cust, up, ours, base, how, argparse.Namespace(keep=[], keep_defaults=True))
    return plan, cust.text(plan.final[IAC]), cust.text(ours[IAC])


def test_bulk_iac_replacement_is_kept_and_upstream_changes_are_taken(tmp_path):
    plan, merged, ours = _sync(tmp_path)
    assert plan.changes["conflict"] == []
    assert plan.changes["merged"] == [IAC]
    assert not plan.blocking, plan.todo
    assert "<<<<<<<" not in merged

    # Bulk's replacement step and its marker, untouched.
    assert "# veracode-iac-rbac" in merged
    steps = _steps(merged)
    scan = next(s for s in steps if s.get("id") == "veracode_iac_scan")
    assert scan == next(s for s in _steps(ours) if s.get("id") == "veracode_iac_scan")
    assert not any(ACTION in str(s.get("uses", "")) for s in steps)
    assert any(s.get("name") == "Store Veracode IaC/Secrets Scanning results" for s in steps)

    # Upstream's other changes in the same file.
    assert "client_payload.workflow_name" in merged  # run-name
    assert "actions/checkout@v6" in merged
    assert "integration-helper@v0.1.12" in merged and "integration-helper@v0.1.9" not in merged
    assert any(s.get("id") == "verify_policy_name" for s in _steps(merged, "validations"))

    # The PR lists it as a kept customization, not a to-do.
    assert any(f"replaces `{ACTION}`" in label for p, _, label in plan.custom_steps if p == IAC)
    assert not any(ACTION in text for _, text in plan.todo)


def test_bulk_iac_replacement_with_crlf_checkout(tmp_path):
    plan, merged, _ = _sync(tmp_path, crlf=True)
    assert plan.changes["conflict"] == []
    assert "# veracode-iac-rbac" in merged and "actions/checkout@v6" in merged


def test_replaced_step_left_alone_when_upstream_drops_the_action():
    b = fixture("iac-upstream-v1.yml")
    o = fixture("iac-bulk-v1.yml")
    t = b.replace("container_iac_secrets_scanning@v1.0.7", "something_else@v2")
    # Upstream no longer has the step Bulk replaced: nothing to substitute, a person decides.
    assert vus.replace_marked_steps(o, b, t) == (b, t, {})


def test_newer_bulk_step_name_is_recognised_by_its_marker():
    b, t = fixture("iac-upstream-v1.yml"), fixture("iac-upstream-v2.yml")
    o = fixture("iac-bulk-v1.yml").replace(
        "- name: Run Veracode IaC/Secrets Scanning\n", "- name: Run Veracode IaC/Secrets Scanning (team RBAC)\n"
    )
    nb, nt, replaced = vus.replace_marked_steps(o, b, t)
    assert replaced == {"veracode-iac-secrets-scan": (("id", "veracode_iac_scan"), ACTION)}
    for text in (nb, nt):
        assert f"uses: {ACTION}" not in text and "(team RBAC)" in text and "# veracode-iac-rbac" in text
    assert "integration-helper@v0.1.12" in nt  # the rest of upstream's text is untouched


def test_unmarked_replacement_is_not_assumed():
    b, t = fixture("iac-upstream-v1.yml"), fixture("iac-upstream-v2.yml")
    o = fixture("iac-bulk-v1.yml").replace("# veracode-iac-rbac", "# my own scan")
    assert vus.replace_marked_steps(o, b, t) == (b, t, {})
