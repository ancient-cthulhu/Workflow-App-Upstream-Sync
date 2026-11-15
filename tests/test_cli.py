import argparse
import json
import threading
import time

import pytest

import veracode_upstream_sync as vus


class _Hdrs(dict):
    def get(self, k, d=None):
        for kk, v in self.items():
            if kk.lower() == k.lower():
                return v
        return d


# --- Ctrl-C ---------------------------------------------------------------- #
def test_interrupt_reports_unfinished_repos_and_exits_130(monkeypatch):
    monkeypatch.setattr(vus, "resolve_token", lambda h: "t")
    monkeypatch.setattr(vus.GitHub, "req", lambda self, m, p, *a, **k: ({"login": "me", "id": 1, "name": "Me"}, _Hdrs()))
    repos = [{"full_name": f"o{i}/veracode"} for i in range(5)]
    monkeypatch.setattr(vus, "discover", lambda gh, args: (repos, []))

    class Up:
        url, ref, sha = "u", "v1", "c" * 40

        def close(self):
            pass

    monkeypatch.setattr(vus, "Upstream", lambda *a: Up())
    started = threading.Event()

    def slow(gh, up, repo, args, author, workdir):
        started.set()
        time.sleep(0.3)
        return vus.Result(repo["full_name"], "pr-created", "ready")

    monkeypatch.setattr(vus, "sync_repo", slow)

    def interrupted(fs, timeout=None):
        started.wait()
        raise KeyboardInterrupt

    monkeypatch.setattr(vus.cf, "as_completed", interrupted)
    seen = {}

    def capture(results, gh, args, upstream):
        seen["results"] = list(results)
        return vus.collections.Counter(r.status for r in results)

    monkeypatch.setattr(vus, "print_results", capture)
    rc = vus.main(["--orgs", "o0", "--workers", "1"])
    assert rc == 130
    by_repo = {r.repo: r for r in seen["results"]}
    assert len(by_repo) == 5  # no repo silently dropped
    assert by_repo["o0/veracode"].status == "pr-created"  # the one in progress finished
    assert [r.detail for r in by_repo.values() if r.status == "error"] == ["interrupted"] * 4


# --- discover -------------------------------------------------------------- #
def _args(orgs):
    return argparse.Namespace(orgs=orgs, orgs_file=None, exclude_orgs=[], repo_name="veracode", workers=2)


def test_discover_dedupes_orgs_case_insensitively():
    calls = []

    class GH:
        def get(self, path, ok404=False):
            calls.append(path)
            return {"full_name": "Acme/veracode"}

    repos, _ = vus.discover(GH(), _args(["Acme", "acme", "ACME"]))
    assert [r["full_name"] for r in repos] == ["Acme/veracode"]
    assert calls == ["/repos/Acme/veracode"]


def test_discover_dedupes_repos_by_full_name():
    class GH:  # a renamed org: both names redirect to the same repo
        def get(self, path, ok404=False):
            return {"full_name": "new/veracode" if "old" in path else "New/Veracode"}

    repos, _ = vus.discover(GH(), _args(["old", "New"]))
    assert len(repos) == 1


# --- report names ---------------------------------------------------------- #
@pytest.mark.parametrize(
    "report, csv",
    [("run.json", "run.csv"), ("run", "run.csv"), ("run.csv", "run.actions.csv"), ("x/Run.CSV", "x/Run.actions.csv")],
)
def test_report_csv_path(report, csv):
    assert vus.report_csv_path(vus.Path(report)) == vus.Path(csv)


def test_report_csv_does_not_overwrite_json(tmp_path, capsys):
    rep = tmp_path / "report.csv"
    gh = vus.GitHub("https://api.github.com", "t", vus.RateLimiter(sleep=lambda s: None))
    vus.print_results([vus.Result("o/veracode", "up-to-date", "base: marker")], gh, argparse.Namespace(report=str(rep)), {"url": "u"})
    assert json.loads(rep.read_text(encoding="utf-8"))["results"][0]["repo"] == "o/veracode"
    assert (tmp_path / "report.actions.csv").read_text(encoding="utf-8-sig").startswith("org,status")


# --- dry run --------------------------------------------------------------- #
BRANCH = "veracode-upstream-sync/" + "c" * 10
REPO = {
    "full_name": "o/veracode",
    "default_branch": "main",
    "clone_url": "https://github.com/o/veracode.git",
    "permissions": {"push": True},
}


class _Up:
    sha = "c" * 40


def _dry_args():
    return argparse.Namespace(branch_prefix="veracode-upstream-sync", dry_run=True, force=False, git_host="github.com")


def _no_clone(*a, **k):
    raise AssertionError("dry run went on to clone and plan")


def test_dry_run_reports_open_pr(monkeypatch, tmp_path):
    class GH:
        def paginate(self, path):
            return [{"head": {"ref": BRANCH, "repo": {"full_name": "o/veracode"}}, "html_url": "pr-url", "body": ""}]

    monkeypatch.setattr(vus, "clone", _no_clone)
    r = vus.sync_repo(GH(), _Up(), REPO, _dry_args(), ("a", "b"), tmp_path)
    assert (r.status, r.pr) == ("pr-exists", "pr-url")


def test_dry_run_reports_declined_update(monkeypatch, tmp_path):
    class GH:
        def paginate(self, path):
            return []

        def get(self, path, ok404=False):
            assert "state=closed" in path
            return [{"merged_at": None, "labels": [], "html_url": "declined-url"}]

    monkeypatch.setattr(vus, "clone", _no_clone)
    r = vus.sync_repo(GH(), _Up(), REPO, _dry_args(), ("a", "b"), tmp_path)
    assert r.status == "skipped" and "declined" in r.detail
