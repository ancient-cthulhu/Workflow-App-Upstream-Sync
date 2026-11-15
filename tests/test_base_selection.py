"""Which upstream commits count as newer than the sync target (real local git repos)."""

import os
import subprocess
from pathlib import Path

import veracode_upstream_sync as vus

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=ENV, text=True).stdout


def write(repo, path, text):
    p = Path(repo) / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode())


def _upstream(tmp_path):
    """main: v1.0.0 -> v2.0.0; release/1.x branches off v1.0.0 with hotfix v1.0.1."""
    up = tmp_path / "up"
    up.mkdir()
    git(up, "init", "-q", "-b", "main")
    git(up, "config", "core.autocrlf", "false")
    write(up, "veracode.yml", "a: 1\n")
    write(up, ".github/workflows/w.yml", "on: push\njobs: {}\n")
    git(up, "add", "-A")
    git(up, "commit", "-qm", "c1")
    git(up, "tag", "v1.0.0")
    git(up, "checkout", "-qb", "release/1.x")
    write(up, "veracode.yml", "a: 1\nfix: true\n")
    git(up, "commit", "-qam", "1.x hotfix")
    git(up, "tag", "v1.0.1")
    git(up, "checkout", "-q", "main")
    write(up, "veracode.yml", "a: 2\n")
    git(up, "commit", "-qam", "c2")
    git(up, "tag", "v2.0.0")
    return up


def test_maintenance_branch_is_not_newer_than_target(tmp_path):
    up_src = _upstream(tmp_path)
    work = tmp_path / "w"
    work.mkdir()
    up = vus.Upstream(str(up_src), vus.LATEST, work)
    assert up.ref == "v2.0.0"
    assert up.git.rev("v1.0.1") not in up.newer

    cust = vus.clone(str(up_src), work / "c.git", vus.git_env(), branch="release/1.x")
    head = cust.rev("HEAD")
    assert not vus.newer_than_target(cust, head, cust.ls_tree(head), up)


def test_commits_after_target_are_newer(tmp_path):
    up_src = _upstream(tmp_path)
    work = tmp_path / "w"
    work.mkdir()
    up = vus.Upstream(str(up_src), "v1.0.0", work)
    assert up.git.rev("v2.0.0") in up.newer
    assert up.git.rev("v1.0.1") in up.newer  # a hotfix on top of the target is newer too

    cust = vus.clone(str(up_src), work / "c.git", vus.git_env(), branch="main")
    head = cust.rev("HEAD")
    assert vus.newer_than_target(cust, head, cust.ls_tree(head), up)
