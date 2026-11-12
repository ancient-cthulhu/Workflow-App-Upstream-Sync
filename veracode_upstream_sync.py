#!/usr/bin/env python3
"""
Keep each organization's Veracode Workflow Integration repo ("veracode") in
sync with the upstream template, github.com/veracode/github-actions-integration,
through reviewable pull requests.

  * Never pushes to the default branch: every change is a pull request.
  * 3-way merge (base / local / upstream) keeps custom settings:
    custom files, custom workflow steps and veracode.yml settings.
  * repo_list.yml and --keep paths are never modified.
  * Anything that cannot be merged safely ends up in a draft PR with a short To do list.
  * Every GitHub call goes through one shared rate limiter.

Naming: O / B / T are ours (the repo being synced), base and theirs (upstream) in a 3-way merge.
"""

from __future__ import annotations

import argparse
import base64
import collections
import concurrent.futures as cf
import csv
import dataclasses
import datetime as dt
import difflib
import fnmatch
import http.client
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from contextlib import contextmanager, suppress
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: pip install pyyaml")

__version__ = "3.10.0"

DEFAULT_UPSTREAM = "https://github.com/veracode/github-actions-integration.git"
DEFAULT_API = "https://api.github.com"
MARKER = ".veracode-upstream-sync.json"  # records the synced upstream commit (next merge base)
TRAILER = "Veracode-Upstream-Sync"  # commit trailer that identifies this tool's commits
CONFIG_FILE = "veracode.yml"
ALWAYS_KEEP = ("repo_list.yml",)
CLI_DIR = "helper/cli/"
WORKFLOW_DIR = ".github/workflows/"
CLI_RE = re.compile(r"veracode-cli_(\d+(?:\.\d+)*)_")
USES_RE = re.compile(r"^\s*-?\s*uses:\s*['\"]?([^\s'\"#]+)", re.MULTILINE)
ORG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
BINARY_EXT = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".zip",
    ".gz",
    ".tgz",
    ".jar",
    ".exe",
    ".pdf",
}
BLOB_LIMIT = "blob:limit=2m"  # partial clones: skip the large CLI binaries in history
SYNC_LABEL = "veracode-upstream-sync"
LATEST = "latest-release"
SEMVER_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
REF_RE = re.compile(r"\b(secrets|vars)\.([A-Za-z_][A-Za-z0-9_]*)")
SHA_RE = re.compile(r"@[0-9a-f]{40}$")
MAX_WORKERS = 32  # parallel clones and merges; API calls are paced by the rate limiter either way

log = logging.getLogger("vsync")

Tree = dict[str, tuple[str, str]]  # path -> (mode, blob sha)


# --------------------------------------------------------------------------- #
# Rate limiting
# GitHub limits (docs.github.com, "Rate limits for the REST API"), paced at 80%
# and shared by all workers because GitHub applies them per token:
#   primary 5,000 requests/hour for a PAT (re-read from x-ratelimit-limit),
#   900 points/min (GET 1, POST/PATCH/PUT/DELETE 5), 80 content-creating
#   requests/min and 500/hour, 100 concurrent requests.
# --------------------------------------------------------------------------- #
class _Window:
    """Weighted sliding window; guarded by RateLimiter._lock."""

    def __init__(self, cap: int, seconds: float):
        self.cap, self.seconds = cap, seconds
        self.events: deque = deque()
        self.total = 0

    def wait(self, now: float, weight: int) -> float:
        while self.events and self.events[0][0] <= now - self.seconds:
            self.total -= self.events.popleft()[1]
        if self.total + weight <= self.cap:
            return 0.0
        need, freed = self.total + weight - self.cap, 0
        for ts, w in self.events:
            freed += w
            if freed >= need:
                return ts + self.seconds - now
        return self.seconds

    def add(self, now: float, weight: int) -> None:
        self.events.append((now, weight))
        self.total += weight


class RateLimiter:
    WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self._lock = threading.Lock()
        self.hourly = _Window(4000, 3600)
        self.points = _Window(720, 60)
        self.writes_min = _Window(60, 60)
        self.writes_hour = _Window(400, 3600)
        self._concurrent = threading.BoundedSemaphore(50)
        self._paused_until = 0.0
        self.requests = 0
        self.waited = 0.0

    @contextmanager
    def slot(self, method: str):
        write = method in self.WRITES
        windows = [(self.hourly, 1), (self.points, 5 if write else 1)]
        if write:
            windows += [(self.writes_min, 1), (self.writes_hour, 1)]
        while True:
            with self._lock:
                now = self.clock()
                delay = max([self._paused_until - now] + [w.wait(now, c) for w, c in windows])
                if delay <= 0:
                    for w, c in windows:
                        w.add(now, c)
                    self.requests += 1
                    break
            delay = min(delay, 300)
            if delay >= 5:
                log.info("Rate limiter: pacing %.0fs", delay)
            with self._lock:
                self.waited += delay
            self.sleep(delay)
        with self._concurrent:
            yield

    def pause(self, seconds: float) -> None:
        """Make every worker wait, e.g. after GitHub says the budget is exhausted."""
        with self._lock:
            self._paused_until = max(self._paused_until, self.clock() + min(seconds, 3900))

    def observe(self, headers) -> None:
        if not headers or (headers.get("X-RateLimit-Resource") or "core") != "core":
            return
        limit = headers.get("X-RateLimit-Limit") or ""
        if limit.isdigit() and int(limit) > 0:
            with self._lock:
                self.hourly.cap = max(1, int(int(limit) * 0.8))
        reset = headers.get("X-RateLimit-Reset") or ""
        if headers.get("X-RateLimit-Remaining") == "0" and reset.isdigit():
            self.pause(max(1, int(reset) - int(time.time()) + 1))


# --------------------------------------------------------------------------- #
# GitHub REST client
# --------------------------------------------------------------------------- #
class GitHubError(RuntimeError):
    def __init__(self, status: int, msg: str, maybe_applied: bool = False):
        super().__init__(f"HTTP {status}: {msg}" if status else msg)
        self.status = status
        self.maybe_applied = maybe_applied  # a write may have landed; check before retrying


class GitHub:
    def __init__(self, api: str, token: str, limiter: RateLimiter | None = None):
        self.api = api.rstrip("/")
        self.token = token
        self.limiter = limiter or RateLimiter()

    def req(
        self,
        method: str,
        path: str,
        body=None,
        ok404: bool = False,
        idempotent: bool | None = None,
    ):
        """Rate-limit rejections are always retried (GitHub did not process
        them). 5xx and network errors are retried only for idempotent calls;
        a POST raises GitHubError(maybe_applied=True) so the caller can check."""
        idempotent = method != "POST" if idempotent is None else idempotent
        url = path if path.startswith(("https://", "http://")) else self.api + path
        if not url.startswith(self.api + "/"):  # e.g. a pagination link: never send the token elsewhere
            raise GitHubError(
                0,
                f"refusing to send the token outside {self.api}: {urllib.parse.urlsplit(url).netloc}",
            )
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"veracode-upstream-sync/{__version__}",
            **({"Content-Type": "application/json"} if data else {}),
        }
        limited = faults = 0
        while True:
            try:
                with self.limiter.slot(method):
                    req = urllib.request.Request(url, data=data, method=method, headers=headers)
                    try:
                        with urllib.request.urlopen(req, timeout=60) as resp:
                            status, hdrs, raw = resp.status, resp.headers, resp.read()
                    except urllib.error.HTTPError as e:
                        status, hdrs, raw = e.code, e.headers, e.read()
            except (OSError, http.client.HTTPException) as e:
                if not idempotent:
                    raise GitHubError(0, f"network error on {method} {path}: {e}", maybe_applied=True) from None
                faults += 1
                if faults >= 3:
                    raise GitHubError(0, f"network error on {method} {path}: {e}") from None
                self.limiter.sleep(2**faults)
                continue
            self.limiter.observe(hdrs)
            if 200 <= status < 300:
                return (json.loads(raw) if raw else None), hdrs
            if status == 404 and ok404:
                return None, hdrs
            text = raw.decode(errors="replace")
            wait = self._rate_limit_wait(status, hdrs, text, limited)
            if wait is not None:
                limited += 1
                if limited >= 6:
                    raise GitHubError(
                        status,
                        f"still rate limited after {limited} attempts on {method} {path}",
                    )
                log.warning("Rate limited on %s %s, waiting %ss", method, path, wait)
                self.limiter.pause(wait)
                continue
            if status >= 500:
                if not idempotent:
                    raise GitHubError(status, self._message(text, hdrs), maybe_applied=True)
                faults += 1
                if faults >= 3:
                    raise GitHubError(status, self._message(text, hdrs))
                self.limiter.sleep(2**faults)
                continue
            raise GitHubError(status, self._message(text, hdrs))

    @staticmethod
    def _rate_limit_wait(status, headers, text, attempt) -> int | None:
        if status not in (403, 429):
            return None
        retry_after = (headers.get("Retry-After") or "").strip()
        if retry_after.isdigit():
            return max(1, int(retry_after))
        if headers.get("X-RateLimit-Remaining") == "0":
            reset = headers.get("X-RateLimit-Reset") or ""
            return max(1, int(reset) - int(time.time()) + 1) if reset.isdigit() else 60
        if status == 429 or "secondary rate limit" in text.lower():
            return min(60 * 2**attempt, 300)  # GitHub: wait at least a minute, then back off
        return None

    @staticmethod
    def _message(text, headers) -> str:
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        msg = str(data.get("message", text)) if isinstance(data, dict) else text[:300]
        if headers.get("X-GitHub-SSO"):
            msg += " (authorize the token for this org's SAML SSO)"
        return msg

    def get(self, path, ok404=False):
        return self.req("GET", path, ok404=ok404)[0]

    def paginate(self, path):
        url = f"{self.api}{path}{'&' if '?' in path else '?'}per_page=100"
        while url:
            data, headers = self.req("GET", url)
            yield from data or []
            m = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("Link") or "")
            url = m.group(1) if m else None


# --------------------------------------------------------------------------- #
# Git
# --------------------------------------------------------------------------- #
class GitError(RuntimeError):
    pass


def git_env(token: str | None = None, url: str | None = None) -> dict:
    """Locked-down git: no prompts, no hooks, HTTPS and local files only. The
    token is a header scoped to the one GitHub host, passed through the
    environment so it never appears in argv, remotes or .git/config."""
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true", GIT_ALLOW_PROTOCOL="https:file")
    settings = [("core.hooksPath", os.devnull)]
    if token and url and url.startswith("https://"):
        host = urllib.parse.urlsplit(url).netloc
        cred = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        settings.append((f"http.https://{host}/.extraHeader", f"Authorization: Basic {cred}"))
    n = int(env.get("GIT_CONFIG_COUNT") or 0)
    for i, (key, value) in enumerate(settings, n):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
    env["GIT_CONFIG_COUNT"] = str(n + len(settings))
    return env


class Git:
    timeout = 900

    def __init__(self, path: Path, env: dict):
        self.path, self.env = Path(path), env
        self._blobs: dict[str, bytes] = {}  # content-addressed, so caching is always correct

    def run(
        self,
        *args,
        input: bytes | None = None,
        raw: bool = False,
        env: dict | None = None,
    ):
        try:
            p = subprocess.run(
                ["git", "-C", str(self.path), *args],
                input=input,
                capture_output=True,
                env={**self.env, **(env or {})},
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]} timed out after {self.timeout}s (raise --git-timeout)") from None
        if p.returncode != 0:
            raise GitError(f"git {' '.join(args[:2])} failed: {p.stderr.decode(errors='replace').strip()[-500:]}")
        return p.stdout if raw else p.stdout.decode(errors="replace")

    def rev(self, ref: str) -> str:
        return self.run("rev-parse", "--verify", f"{ref}^{{commit}}").strip()

    def ls_tree(self, rev: str) -> Tree:
        """path -> (mode, blob sha)."""
        tree = {}
        for rec in self.run("ls-tree", "-r", "-z", rev, raw=True).split(b"\0"):
            if rec:
                meta, path = rec.split(b"\t", 1)
                mode, kind, sha = meta.decode().split()
                if kind == "blob":
                    tree[path.decode()] = (mode, sha)
        return tree

    def blob(self, sha: str) -> bytes:
        if sha not in self._blobs:
            data = self.run("cat-file", "blob", sha, raw=True)
            if len(data) > 1_000_000:  # never cache the CLI binaries
                return data
            self._blobs[sha] = data
        return self._blobs[sha]

    def text(self, entry) -> str:
        return self.blob(entry[1]).decode(errors="replace") if entry else ""

    def write_blob(self, data: bytes) -> str:
        return self.run("hash-object", "-w", "--stdin", input=data).strip()

    def commit(self, tree: dict, parent: str, message: str, author: tuple[str, str]) -> str:
        with tempfile.TemporaryDirectory() as td:
            index = {"GIT_INDEX_FILE": str(Path(td) / "index")}
            info = b"".join(f"{m} blob {s}\t{p}".encode() + b"\0" for p, (m, s) in sorted(tree.items()))
            self.run("update-index", "-z", "--index-info", input=info, env=index)
            tree_sha = self.run("write-tree", "--missing-ok", env=index).strip()
        name, email = author
        ids = {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }
        return self.run("commit-tree", tree_sha, "-p", parent, input=message.encode(), env=ids).strip()


def clone(url: str, dest: Path, env: dict, branch: str | None = None) -> Git:
    """Bare partial clone: full history of small files, large binaries on demand."""
    args = ["git", "clone", "--bare", "--quiet", f"--filter={BLOB_LIMIT}"]
    if branch:  # a customer repo: only its default branch
        args += ["--single-branch", "--no-tags", "--branch", branch]
    try:
        p = subprocess.run(
            [*args, url, str(dest)],
            capture_output=True,
            env=env,
            timeout=Git.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise GitError(f"clone of {url} timed out after {Git.timeout}s") from None
    if p.returncode != 0:
        raise GitError(f"clone failed for {url}: {p.stderr.decode(errors='replace').strip()[-500:]}")
    return Git(dest, env)


class Upstream:
    def __init__(self, url: str, ref: str, workdir: Path):
        self.url = url
        self.git = clone(url, workdir / "upstream.git", git_env())
        self.ref = latest_release(self.git) if ref == LATEST else ref
        self.sha = self.git.rev(self.ref)
        self.commits = self.git.run("rev-list", "--topo-order", self.sha).split()
        self.commit_set = set(self.commits)
        self.newer = set(self.git.run("rev-list", "--all").split()) - self.commit_set  # past the target
        self.tree = self.git.ls_tree(self.sha)
        if CONFIG_FILE not in self.tree or not any(is_workflow(p) for p in self.tree):
            raise GitError(
                f"{url} at {ref} does not look like the Veracode integration (no {CONFIG_FILE} or workflows)"
            )
        self._pairs: dict[str, frozenset] = {}
        self._structure: set | None = None
        self._versions: dict[str, list[str]] = {}
        self._lock = threading.Lock()  # lazy blob fetches into one repo are serialized
        log.info("Upstream %s at %s (%s)", url, self.sha[:10], self.ref)

    def blob(self, sha: str) -> bytes:
        with self._lock:
            return self.git.blob(sha)

    def text(self, entry) -> str:
        return self.blob(entry[1]).decode(errors="replace") if entry else ""

    def tree_at(self, commit: str) -> Tree:
        with self._lock:
            return self.git.ls_tree(commit)

    def pairs_at(self, commit: str) -> frozenset:
        with self._lock:
            if commit not in self._pairs:
                self._pairs[commit] = frozenset((p, s) for p, (_, s) in self.git.ls_tree(commit).items())
            return self._pairs[commit]

    def closest_version(self, path: str, text: str, current: str | None) -> str | None:
        """For a repo copied without history: the upstream version of `path` closest to the
        local file, if closer than `current`. A whole-repo base can be right for most files
        and wrong for a few; a wrong base makes upstream changes look like local edits."""
        with self._lock:
            if path not in self._versions:
                raw = self.git.run("log", "--no-abbrev", "--raw", "--format=", self.sha, "--", path)
                shas = {x for line in raw.splitlines() for x in line.split()[2:4] if x.strip("0")}
                self._versions[path] = sorted(shas)
            versions = self._versions[path]
        local = text.splitlines()

        def distance(sha):
            other = self.blob(sha).decode(errors="replace").splitlines()
            ops = difflib.SequenceMatcher(None, local, other, autojunk=False).get_opcodes()
            return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in ops if tag != "equal")

        best, best_d = current, distance(current) if current else len(local) + 1
        for sha in versions:
            if sha != current and (d := distance(sha)) < best_d:
                best, best_d = sha, d
        return best if best != current else None

    def structure_baseline(self) -> set:
        """Workflow problems pure upstream already has (so they are not blamed on the merge)."""
        if self._structure is None:
            self._structure = workflow_problems({p: self.text(e) for p, e in self.tree.items() if checked_file(p)})
        return self._structure

    def web_url(self) -> str | None:
        m = re.match(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", self.url)
        return f"https://github.com/{m.group(1)}" if m else None


def latest_release(git: Git) -> str:
    versions = {
        t: tuple(int(x) for x in m.groups()) for t in git.run("tag", "-l").split() if (m := SEMVER_TAG.match(t))
    }
    if not versions:
        raise GitError("upstream has no release tags (vX.Y.Z); pass --upstream-ref")
    return max(versions, key=versions.__getitem__)


def marker_commit(cust: Git, ours: Tree) -> str:
    """The upstream commit recorded by the last sync, or "" if none (or unreadable)."""
    if MARKER in ours:
        with suppress(ValueError, GitError, AttributeError, TypeError):
            return str(json.loads(cust.blob(ours[MARKER][1])).get("commit") or "")
    return ""


def newer_than_target(cust: Git, head: str, ours: Tree, up: Upstream) -> bool:
    """True if the repo already has upstream changes made after the target, so syncing
    would move it backwards (for example it was set up from main, the target is a release)."""
    if marker_commit(cust, ours) in up.newer:
        return True
    if any(c in up.newer for c in cust.run("rev-list", "--max-count=20000", head).split()):
        return True
    if not up.newer or MARKER in ours:
        return False
    pairs = {(p, s) for p, (_, s) in ours.items() if not p.startswith(CLI_DIR)}
    best_target = max((len(pairs & up.pairs_at(c)) for c in up.commits), default=0)
    return any(len(pairs & up.pairs_at(c)) > best_target for c in up.newer)


# --------------------------------------------------------------------------- #
# Workflow validation: the structural problems a merge can introduce
# --------------------------------------------------------------------------- #
EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
STEPS_REF_RE = re.compile(r"\bsteps\.([A-Za-z_][\w-]*)")
NEEDS_REF_RE = re.compile(r"\bneeds\.([A-Za-z_][\w-]*)")
HELPER_RE = re.compile(r"(?:^|[\s'\"/])(helper/[\w./-]+\.(?:js|mjs|py|sh|ps1))")


def checked_file(path: str) -> bool:
    return is_workflow(path) or Path(path).name in ("action.yml", "action.yaml")


def _expressions(node) -> str:
    """Every `${{ }}` expression and every `if:` condition in a job, as one string."""
    out = []
    if isinstance(node, dict):
        for k, val in node.items():
            if k == "if" and isinstance(val, str):
                out.append(val)
            out.append(_expressions(val))
    elif isinstance(node, list):
        out += [_expressions(x) for x in node]
    elif isinstance(node, str):
        out += EXPR_RE.findall(node)
    return "\n".join(out)


def workflow_problems(files: dict[str, str]) -> set[tuple[str, str, str]]:
    """(path, job, problem) for references that do not resolve: `needs`, `needs.<job>`,
    `steps.<id>`, calls to local reusable workflows (missing file, required or unknown
    inputs and secrets), local actions and helper scripts. Plain Python, no external tool."""
    problems = set()
    for path, text in files.items():
        if not is_workflow(path):
            continue
        doc = safe_yaml(text)[0]
        jobs = doc.get("jobs") if isinstance(doc, dict) else None
        if not isinstance(jobs, dict):
            continue
        for job, jd in jobs.items():
            if not isinstance(jd, dict):
                continue

            def bad(msg: str, job: str = str(job), path: str = path) -> None:
                problems.add((path, job, msg))

            needs = jd.get("needs") or []
            needs = [needs] if isinstance(needs, str) else [str(n) for n in needs] if isinstance(needs, list) else []
            for n in needs:
                if n not in jobs:
                    bad(f"`needs: {n}` names no job: use the job's new name or remove it")
            expr = _expressions(jd)
            for n in sorted(set(NEEDS_REF_RE.findall(expr)) - set(needs)):
                bad(f"add `{n}` to `needs`, since the job uses `needs.{n}`")
            raw_steps = jd.get("steps")
            steps: list = raw_steps if isinstance(raw_steps, list) else []
            ids = [str(st["id"]) for st in steps if isinstance(st, dict) and st.get("id")]
            for i in sorted({i for i in ids if ids.count(i) > 1}):
                bad(f"step id `{i}` is used twice: rename one of the steps")
            for ref in sorted(set(STEPS_REF_RE.findall(expr)) - set(ids)):
                bad(f"`steps.{ref}` points to no step: use the step's new id")
            uses = str(jd.get("uses") or "")
            if uses.startswith("./"):
                _check_call(files, uses[2:].split("@")[0], jd, bad)
            for st in steps:
                if not isinstance(st, dict):
                    continue
                local = str(st.get("uses") or "")
                if local.startswith("./"):
                    d = local[2:].rstrip("/")
                    if f"{d}/action.yml" not in files and f"{d}/action.yaml" not in files:
                        bad(f"local action `{local}` is missing: restore it or remove the step")
                for helper in HELPER_RE.findall(str(st.get("run") or "")):
                    if helper not in files:
                        bad(f"`{helper}` is missing: restore it or update the step that runs it")
    return problems


def _check_call(files: dict[str, str], callee: str, jd: dict, bad) -> None:
    """A job that calls a local reusable workflow passes what it requires and nothing unknown."""
    if callee not in files:
        bad(f"calls `{callee}`, which is missing: restore it or remove this job")
        return
    doc = safe_yaml(files[callee])[0]
    on = doc.get("on", doc.get(True)) if isinstance(doc, dict) else None  # YAML 1.1 reads `on` as True
    call = on.get("workflow_call") if isinstance(on, dict) else None
    call = call if isinstance(call, dict) else {}
    for kind, given in (("inputs", jd.get("with")), ("secrets", jd.get("secrets"))):
        raw = call.get(kind)
        declared: dict = raw if isinstance(raw, dict) else {}
        if kind == "secrets" and given == "inherit":
            continue
        given = given if isinstance(given, dict) else {}
        for name, spec in declared.items():
            spec = spec if isinstance(spec, dict) else {}
            if spec.get("required") and "default" not in spec and name not in given:
                bad(
                    f"pass the {kind[:-1]} `{name}` that `{callee}` now requires"
                    + (" (or use `secrets: inherit`)" if kind == "secrets" else "")
                )
        for name in sorted(set(given) - set(declared)):
            bad(f"remove the {kind[:-1]} `{name}`: `{callee}` no longer accepts it")


def structure_check(plan: Plan, cust: Git, up: Upstream, ours: Tree) -> None:
    """Workflow problems in the result that neither the repo nor pure upstream had: the merge made them."""
    final, conflicted = plan.final, set(plan.changes["conflict"])
    if not any(final[p] != ours.get(p) for p in final if checked_file(p)):
        return
    after = {  # conflicted files are fixed by a person first: skip them and their callers
        (path, job, problem)
        for path, job, problem in workflow_problems({p: cust.text(e) for p, e in final.items() if checked_file(p)})
        if path not in conflicted and not any(f"`{c}`" in problem for c in conflicted)
    }
    after -= up.structure_baseline()
    if after:
        after -= workflow_problems({p: cust.text(e) for p, e in ours.items() if checked_file(p)})
    for path, job, problem in sorted(after)[:10]:
        loc = locate(cust.text(final[path]).splitlines(), ["jobs", job])
        plan.add(
            True,
            f"Fix `{path}`, job `{job}`: {problem}",
            "workflow",
            action="Fix broken workflow reference",
            where=f"{path} line {loc[0] + 1}, job {job}" if loc else f"{path}, job {job}",
            details=problem[:1].upper() + problem[1:],
        )


# --------------------------------------------------------------------------- #
# Repository settings the workflows depend on (read-only API checks)
# --------------------------------------------------------------------------- #
def _names(gh: GitHub, path: str, key: str) -> set[str] | None:
    """Names from a listing endpoint, every page; None if the token may not read it.
    Pages end by total_count: some listings (variables) return at most 30 per page."""
    out: set[str] = set()
    for page in range(1, 200):
        try:
            data = gh.get(f"{path}?per_page=100&page={page}") or {}
        except GitHubError as e:
            if e.status in (403, 404):
                return None
            raise
        items = data.get(key) or []
        out |= {i["name"] for i in items}
        if not items or len(out) >= int(data.get("total_count") or 0):
            break
    return out


def check_settings(gh: GitHub, full: str, plan: Plan, cust: Git, ours: Tree) -> None:
    """A workflow fails at runtime when a secret, variable or action it needs is not
    available. Check that against the repository's real settings instead of the reviewer."""
    _check_secrets_and_variables(gh, full, plan, cust, ours)
    _check_actions_policy(gh, full, plan)


def _check_secrets_and_variables(gh: GitHub, full: str, plan: Plan, cust: Git, ours: Tree) -> None:
    conflicted = set(plan.changes["conflict"])
    before = {r for p, e in ours.items() if is_workflow(p) for r in REF_RE.findall(cust.text(e))}
    needed: dict[tuple[str, str], str] = {}  # (secrets|vars, name) -> first workflow that uses it
    for p, e in sorted(plan.final.items()):
        if is_workflow(p) and p not in conflicted and e != ours.get(p):
            for ref in REF_RE.findall(cust.text(e)):
                if ref not in before and ref[1] != "GITHUB_TOKEN":
                    needed.setdefault(ref, p)
    for kind, label, key, endpoints in (
        ("secrets", "secret", "secrets", ("secrets", "organization-secrets")),
        ("vars", "variable", "variables", ("variables", "organization-variables")),
    ):
        wanted = sorted((name, path) for (k, name), path in needed.items() if k == kind)
        if not wanted:
            continue
        found = [_names(gh, f"/repos/{full}/actions/{ep}", key) for ep in endpoints]
        have = None if None in found else set().union(*found)  # type: ignore[arg-type]
        repo = full.split("/")[1]
        where = f"as an org {label} shared with `{repo}`, or a `{repo}` repo {label} (Settings > Secrets and variables)"
        for name, path in wanted:
            old = RENAMED_SECRETS.get(name) if kind == "secrets" else None
            renamed = f"Upstream renamed `{old}` to `{name}`: " if old and (kind, old) in before else ""
            value = " with the same value" if renamed else ""
            confirm, add = ("confirm", "add") if renamed else ("Confirm", "Add")
            access = f"give the {repo} repo access (or create it in that repo instead)"
            value = f" with the value of `{old}`" if renamed else ""
            row = {
                "where": "Org settings > Secrets and variables > Actions",
                "details": f"Create org {label} `{name}`{value} and {access}",
            }
            if have is None:
                text = f"{renamed}{confirm} `{name}` exists {where} (the token cannot list {label}s)"
                check = f"Check that org {label} `{name}` exists and the {repo} repo has access (the token cannot list them)"
                plan.add(
                    True,
                    text,
                    label,
                    rows=[{"action": f"Confirm {label} exists", **row, "details": check}],
                )
            elif name not in have:
                text = f"{renamed}{add} `{name}`{value} {where}, used by `{path}`"
                plan.add(
                    True,
                    text,
                    label,
                    rows=[
                        {
                            "action": f"Add {'renamed ' if renamed else ''}org {label}",
                            **row,
                        }
                    ],
                )


def _check_actions_policy(gh: GitHub, full: str, plan: Plan) -> None:
    if not plan.new_uses:
        return
    try:
        policy: dict = gh.get(f"/repos/{full}/actions/permissions") or {}
        selected: dict = {}
        if policy.get("allowed_actions") == "selected":
            selected = gh.get(f"/repos/{full}/actions/permissions/selected-actions") or {}
    except GitHubError:  # unreadable policy: every new action becomes a "Confirm" To do below
        policy, selected = {}, {}
    for uses in sorted(plan.new_uses):
        name = uses.split("@")[0]
        if not policy:
            plan.add(
                True,
                f"Confirm GitHub Actions allows `{name}` here (the token cannot read the policy)",
                "action",
                **_action_row(
                    "Confirm action is allowed",
                    f"Check `{name}` is allowed (the token cannot read the policy)",
                ),
            )
            continue
        allowed = policy.get("allowed_actions")
        github_owned = selected.get("github_owned_allowed") and name.split("/")[0].lower() in ("actions", "github")
        by_pattern = any(
            fnmatch.fnmatch(uses, pat) or fnmatch.fnmatch(name, pat.split("@")[0])
            for pat in selected.get("patterns_allowed") or []
        )
        if not (allowed == "all" or (allowed == "selected" and (github_owned or by_pattern))):
            if allowed == "selected" and selected.get("verified_allowed"):  # GitHub has no API to check this
                plan.add(
                    True,
                    f"Confirm `{name}` is from a verified Marketplace creator, or allow it in Settings > Actions",
                    "action",
                    **_action_row(
                        "Confirm verified creator",
                        f"Check `{name}` is from a verified creator; if not, add it to the allow list",
                    ),
                )
            else:
                plan.add(
                    True,
                    f"Allow the action `{name}` in Settings > Actions (it is blocked by policy)",
                    "action",
                    **_action_row("Allow action", f"Add `{name}` to the allowed actions list"),
                )
        if policy.get("sha_pinning_required") and not SHA_RE.search(uses):
            plan.add(
                True,
                f"Pin `{uses}` to a full commit SHA (this repository requires it)",
                "action",
                **_action_row(
                    "Pin action to a commit SHA",
                    f"Replace `{uses}` with its full commit SHA in the workflows",
                ),
            )


def _action_row(action: str, details: str) -> dict:
    return {
        "action": action,
        "where": "Repo settings > Actions > General",
        "details": details,
    }


# --------------------------------------------------------------------------- #
# YAML helpers (strict parsing; comment-preserving text edits)
# --------------------------------------------------------------------------- #
class _StrictLoader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):  # type: ignore[misc]
    """SafeLoader that rejects duplicate keys (plain PyYAML silently keeps the last).
    Uses libyaml when PyYAML has it (about 9x faster, same result)."""


def _mapping_without_duplicates(loader, node, deep=False):
    seen: dict = {}
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=True)
        with suppress(TypeError):  # unhashable keys are reported by PyYAML itself
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None,
                    None,
                    f"duplicate key {key!r} (lines {seen[key] + 1} and {key_node.start_mark.line + 1})",
                )
            seen[key] = key_node.start_mark.line
    return loader.construct_mapping(node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_without_duplicates)


def safe_yaml(text: str):
    """(data, None) or (None, error)."""
    try:
        return yaml.load(text, Loader=_StrictLoader), None  # SafeLoader subclass
    except yaml.YAMLError as e:
        return None, str(e).splitlines()[0]


def flatten(obj, prefix=()) -> dict:
    if isinstance(obj, dict) and obj:
        out = {}
        for k, v in obj.items():
            out.update(flatten(v, (*prefix, k)))
        return out
    return {prefix: obj}


_MISSING = object()


def _get(obj, path):
    for k in path:
        if not isinstance(obj, dict) or k not in obj:
            return _MISSING
        obj = obj[k]
    return obj


def _without(obj, path):
    if not isinstance(obj, dict):
        return obj
    out = dict(obj)
    if len(path) == 1:
        out.pop(path[0], None)
    elif path[0] in out:
        out[path[0]] = _without(out[path[0]], path[1:])
    return out


def _ind(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _is_item(line: str) -> bool:
    s = line.lstrip()
    return s.startswith("- ") or s.rstrip() == "-"


def _block_end(lines: list[str], i: int) -> int:
    """End (exclusive) of the key or list item at line i, without trailing blanks."""
    ind, item = _ind(lines[i]), _is_item(lines[i])
    j = i + 1
    while j < len(lines):
        if _content(lines[j]):
            k = _ind(lines[j])
            if k < ind or (k == ind and (item or not _is_item(lines[j]))):
                break
        j += 1
    while j > i + 1 and (
        not lines[j - 1].strip() or (lines[j - 1].lstrip().startswith("#") and _ind(lines[j - 1]) <= ind)
    ):
        j -= 1
    return j


def locate(lines: list[str], path) -> tuple[int, int] | None:
    """(start, end) line range of a mapping key path."""
    start, end, found = 0, len(lines), None
    for key in path:
        pat = re.compile(rf"^\s*(['\"]?){re.escape(str(key))}\1\s*:(\s|$)")
        child, hit = None, None
        for j in range(start, end):
            if _content(lines[j]):
                child = _ind(lines[j]) if child is None else child
                if _ind(lines[j]) == child and pat.match(lines[j]):
                    hit = j
                    break
        if hit is None:
            return None
        start, end = hit + 1, _block_end(lines, hit)
        found = (hit, end)
    return found


def _reindent(chunk: list[str], delta: int) -> list[str]:
    out = [
        ln if not ln.strip() or delta == 0 else (" " * delta + ln if delta > 0 else ln[min(-delta, _ind(ln)) :])
        for ln in chunk
    ]
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    return out


# ---- veracode.yml --------------------------------------------------------- #
def _fmt(v) -> str:
    if v is None or isinstance(v, bool):
        return {True: "true", False: "false", None: "null"}[v]
    return f"'{v}'" if isinstance(v, str) else json.dumps(v, default=str)


def insert_setting(merged: str, theirs: str, path: tuple) -> str | None:
    """Copy a new upstream setting (with its comments) into the merged text after
    its upstream neighbor. Returns None unless only that setting was added."""
    tl, ml = theirs.splitlines(keepends=True), merged.splitlines(keepends=True)
    T, M = safe_yaml(theirs)[0], safe_yaml(merged)[0]
    parent = path[:-1]
    t_parent, m_parent = _get(T, parent), _get(M, parent)
    loc = locate(tl, [str(k) for k in path])
    if not isinstance(t_parent, dict) or not isinstance(m_parent, dict) or not loc:
        return None
    ts, te = loc
    t_ind = _ind(tl[ts])
    while ts > 0 and tl[ts - 1].lstrip().startswith("#") and _ind(tl[ts - 1]) == t_ind:
        ts -= 1
    if parent:
        lp = locate(ml, [str(k) for k in parent])
        if not lp:
            return None
        ps, pe = lp
        child = next(
            (_ind(ml[j]) for j in range(ps + 1, pe) if _content(ml[j])),
            _ind(ml[ps]) + 2,
        )
    else:
        ps, pe, child = -1, len(ml), 0
    keys = list(t_parent)
    pos = None
    for sibling in reversed(keys[: keys.index(path[-1])]):
        if sibling in m_parent and (lsib := locate(ml, [str(k) for k in (*parent, sibling)])):
            pos = lsib[1]
            break
    if pos is None:
        pos = next((j for j in range(ps + 1, pe) if _content(ml[j])), pe)
    if pos > 0 and not ml[pos - 1].endswith("\n"):
        ml[pos - 1] += "\n"
    chunk = _reindent(tl[ts:te], child - t_ind)
    ml[pos:pos] = ["\n", *chunk] if not parent and pos > 0 and ml[pos - 1].strip() else chunk
    new = "".join(ml)
    N = safe_yaml(new)[0]
    if _get(N, path) != t_parent[path[-1]] or flatten(_without(N, path)) != flatten(M):
        return None
    return new


def restore_setting(merged: str, base: str, path: tuple) -> str | None:
    """Set `path` to its value in `base` (old or new upstream text), touching nothing else."""
    ml, bl = merged.splitlines(keepends=True), base.splitlines(keepends=True)
    keys = [str(k) for k in path]
    lm, lb = locate(ml, keys), locate(bl, keys)
    if not lm or not lb:
        return None
    ml[lm[0] : lm[1]] = _reindent(bl[lb[0] : lb[1]], _ind(ml[lm[0]]) - _ind(bl[lb[0]]))
    new = "".join(ml)
    N, M, B = safe_yaml(new)[0], safe_yaml(merged)[0], safe_yaml(base)[0]
    if N is None or _get(N, path) != _get(B, path) or flatten(_without(N, path)) != flatten(_without(M, path)):
        return None
    return new


def merge_config(merged: str, base: str, theirs: str, ours: str, keep_defaults: bool) -> tuple[str, dict]:
    """Add new upstream settings the text merge could not place, optionally keep
    old defaults, and report what changed for the PR."""
    rep: dict = {"added": [], "missing": [], "defaults": [], "kept": [], "unused": []}
    M, T, B, O = (safe_yaml(x)[0] for x in (merged, theirs, base, ours))
    if not all(isinstance(x, dict) for x in (M, T)):
        return merged, rep
    B = B if isinstance(B, dict) else {}
    for path in flatten(T):  # new upstream settings must end up exactly as upstream wrote them
        # top: where upstream added a key, or turned a plain value into a section
        top = next(path[:n] for n in range(1, len(path) + 1) if not isinstance(_get(B, path[:n]), dict))
        if top == path and _get(B, path) is not _MISSING:
            continue  # an existing value: changed defaults are handled below
        if _get(O, top) != _get(B, top) or _get(M, top) == _get(T, top):
            continue  # changed locally, or already right
        # Missing: insert it (not if its section was deleted locally). Present but wrong, e.g.
        # a conflict kept upstream's `issues:` line but not the lines under it: replace it.
        fix = insert_setting if _get(M, top) is _MISSING else restore_setting
        if new := fix(merged, theirs, top):
            merged, M = new, safe_yaml(new)[0]
    fo, fb, ft = flatten(O if isinstance(O, dict) else {}), flatten(B), flatten(T)
    for path, bv in fb.items():  # upstream changed a default the repo still had
        tv = ft.get(path, _MISSING)
        if tv is _MISSING or tv == bv or fo.get(path, _MISSING) != bv:
            continue
        key = ".".join(map(str, path))
        # Bring the value to the side the option asks for (a conflict resolved to the
        # local side may have kept the old value even when new defaults are wanted).
        want, src = (bv, base) if keep_defaults else (tv, theirs)
        if _get(M, path) != want and (new := restore_setting(merged, src, path)):
            merged, M = new, safe_yaml(new)[0]
        if _get(M, path) == bv:
            rep["kept"].append(f"`{key}`: {_fmt(bv)} (upstream default is now {_fmt(tv)})")
        elif _get(M, path) == tv:
            rep["defaults"].append((key, f"{_fmt(bv)} to {_fmt(tv)}"))
    fm = flatten(M)
    rep["unused"] = [  # set locally, but upstream no longer has this setting (removed or moved)
        ".".join(map(str, k)) for k, val in fo.items() if k in fb and val != fb[k] and k not in ft and k[0] in T
    ]
    for path, tv in ft.items():
        key = ".".join(map(str, path))
        if path not in fb:
            rep["added" if path in fm else "missing"].append(key)
        elif path in fm and fm[path] != tv and fm[path] == fo.get(path, _MISSING) and fo.get(path) != fb.get(path):
            rep["kept"].append(f"`{key}`: {_fmt(fm[path])} (upstream default {_fmt(tv)})")
    return merged, rep


# ---- workflows: custom steps ---------------------------------------------- #
def _step_key(step) -> tuple:
    """Step identity: id, then name, then action without version (a version bump is the same step)."""
    if not isinstance(step, dict):
        return ("raw", json.dumps(step, sort_keys=True, default=str))
    for k in ("id", "name"):
        if step.get(k):
            return (k, str(step[k]))
    if step.get("uses"):
        return ("uses", str(step["uses"]).split("@")[0])
    return ("run", str(step.get("run", "")).strip())


def _occ_keys(steps) -> list[tuple]:
    """Step identities numbered by occurrence, so two unnamed checkout steps stay distinct."""
    seen: collections.Counter = collections.Counter()
    out = []
    for st in steps:
        k = _step_key(st)
        seen[k] += 1
        out.append((k, seen[k]))
    return out


def _label(step, key) -> str:
    name = step.get("name") if isinstance(step, dict) else None
    return re.sub(r"[`\n]", "", str(name or key[0][1]))[:80]


def _job_steps(doc, job):
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    j = jobs.get(job) if isinstance(jobs, dict) else None
    s = j.get("steps") if isinstance(j, dict) else None
    return s if isinstance(s, list) else None


def _step_blocks(lines: list[str], job: str):
    """Line range of each step of a job, plus the item indent."""
    loc = locate(lines, ["jobs", job, "steps"])
    if not loc:
        return None
    i, e = loc
    first = next((j for j in range(i + 1, e) if _content(lines[j])), None)
    if first is None:
        return [], _ind(lines[i]) + 2, i
    if not _is_item(lines[first]):
        return None
    ind = _ind(lines[first])
    items = [j for j in range(first, e) if _content(lines[j]) and _is_item(lines[j])]
    items = [j for j in items if _ind(lines[j]) == ind]
    blocks = []
    for n, s in enumerate(items):
        end = min(_block_end(lines, s), items[n + 1] if n + 1 < len(items) else e)
        while s - 1 > i and lines[s - 1].lstrip().startswith("#") and _ind(lines[s - 1]) == ind:
            s -= 1
        blocks.append((s, end))
    return blocks, ind, i


def custom_steps(o_doc, b_doc) -> list[tuple]:
    """(job, occurrence key, step) for custom steps."""
    out = []
    for job in (o_doc.get("jobs") or {}) if isinstance(o_doc, dict) else {}:
        so = _job_steps(o_doc, job)
        if so is not None:
            base_keys = set(_occ_keys(_job_steps(b_doc, job) or []))
            out += [(job, k, st) for k, st in zip(_occ_keys(so), so, strict=True) if k not in base_keys]
    return out


def step_level_merge(o: str, b: str, t: str, labels) -> str | None:
    """Resolve a workflow conflict caused by custom steps: take them out,
    merge the rest, put each back after the step it followed (or before the step
    it preceded). None if that does not produce a clean, complete result."""
    O, B = safe_yaml(o)[0], safe_yaml(b)[0]
    if not isinstance(O, dict) or not isinstance(B, dict) or not isinstance(O.get("jobs"), dict):
        return None
    lines = o.splitlines(keepends=True)
    removals, groups = [], []
    for job in O["jobs"]:
        so, sb = _job_steps(O, job), _job_steps(B, job)
        loc = _step_blocks(lines, job) if so is not None and sb is not None else None
        if so is None or sb is None:
            continue
        if not loc or len(loc[0]) != len(so):
            return None
        blocks, ind, _ = loc
        base_keys, used = _occ_keys(sb), set()
        prev, group = None, None
        for idx, st in enumerate(so):
            k = _step_key(st)
            m = next(
                (n for n, bk in enumerate(base_keys) if bk[0] == k and n not in used),
                None,
            )
            if m is not None:
                used.add(m)
                if group:
                    group["next"] = base_keys[m]
                prev, group = base_keys[m], None
                continue
            removals.append(blocks[idx])
            if group is None:
                group = {
                    "job": job,
                    "prev": prev,
                    "next": None,
                    "chunk": [],
                    "ind": ind,
                    "keys": [],
                }
                groups.append(group)
            group["chunk"] += lines[blocks[idx][0] : blocks[idx][1]]
            group["keys"].append(k)
    if not groups:
        return None
    for s, e in sorted(removals, reverse=True):
        del lines[s:e]
    merged, conflicts = merge_text("".join(lines), b, t, labels)
    if conflicts:
        merged, conflicts = take_upstream_versions(merged)
    if conflicts:
        return None
    for g in groups:
        M = safe_yaml(merged)[0]
        ml = merged.splitlines(keepends=True)
        steps = _job_steps(M, g["job"])
        loc = _step_blocks(ml, g["job"]) if steps is not None else None
        if steps is None or not loc or len(loc[0]) != len(steps):
            return None
        blocks, ind, steps_line = loc
        keys = _occ_keys(steps)
        if g["prev"] in keys:
            pos = blocks[keys.index(g["prev"])][1]
        elif g["next"] in keys:
            pos = blocks[keys.index(g["next"])][0]
        elif g["prev"] is None:
            pos = blocks[0][0] if blocks else steps_line + 1
        else:
            return None
        if pos > 0 and not ml[pos - 1].endswith("\n"):
            ml[pos - 1] += "\n"
        ml[pos:pos] = _reindent(g["chunk"], ind - g["ind"])
        merged = "".join(ml)
    F = safe_yaml(merged)[0]
    for g in groups:
        have = {_step_key(s) for s in (_job_steps(F, g["job"]) or [])}
        if any(k not in have for k in g["keys"]):
            return None
    return merged


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Plan:
    base: str | None
    base_how: str
    final: Tree
    changes: dict = dataclasses.field(default_factory=lambda: collections.defaultdict(list))  # kind -> paths
    todo: list = dataclasses.field(default_factory=list)  # (blocking, text)
    config: dict = dataclasses.field(default_factory=dict)
    custom_files: list = dataclasses.field(default_factory=list)
    custom_steps: list = dataclasses.field(default_factory=list)  # (file, job, label)
    protected: list = dataclasses.field(default_factory=list)
    new_uses: set = dataclasses.field(default_factory=set)  # actions the repo did not use before
    versions: list = dataclasses.field(default_factory=list)  # your action versions replaced by upstream's
    work: collections.Counter = dataclasses.field(default_factory=collections.Counter)  # kind -> count
    items: list = dataclasses.field(default_factory=list)  # report rows, see Plan.add

    @property
    def blocking(self) -> bool:
        return any(b for b, _ in self.todo)

    def add(
        self,
        blocking: bool,
        text: str,
        kind: str,
        count: int = 1,
        rows: list | None = None,
        **row,
    ) -> None:
        """A To do item: `text` for the PR, `kind`/`count` for the effort level, and one or more
        report rows (action, where, details, and the page to link: file+line or a settings page)."""
        self.todo.append((blocking, text))
        self.work[kind] += count
        self.items += [{"must_fix": blocking, **r} for r in (rows or [row])]

    @property
    def effort(self) -> str:
        """ready: merge as is. check: merge after confirming something. quick: only GitHub
        settings to fix. edit: up to 3 file fixes. long: more than 3 file fixes."""
        if not self.todo:
            return "ready"
        if not self.blocking:
            return "check"
        fixes = sum(n for k, n in self.work.items() if k in FILE_FIXES)
        return "quick" if not fixes else "edit" if fixes <= 3 else "long"

    @property
    def work_summary(self) -> str:
        return ", ".join(f"{n} {WORK_NOUNS[k][n != 1]}" for k, n in sorted(self.work.items(), key=_work_order))


EFFORTS = ("ready", "check", "quick", "edit", "long", "manual")
RENAMED_SECRETS = {  # new name -> old name: upstream renamed these between 2023-09 and 2024-09
    "VERACODE_API_ID": "API_ID",
    "VERACODE_API_KEY": "API_KEY",
    "VERACODE_AGENT_TOKEN": "SRCCLR_API_TOKEN",
}
FILE_FIXES = {"conflict", "workflow", "yaml", "duplicate", "moved-setting", "placement"}
WORK_NOUNS = {
    "conflict": ("conflict", "conflicts"),
    "workflow": ("workflow fix", "workflow fixes"),
    "yaml": ("YAML fix", "YAML fixes"),
    "duplicate": ("duplicate setting", "duplicate settings"),
    "moved-setting": ("moved setting", "moved settings"),
    "placement": ("step to check", "steps to check"),
    "secret": ("secret to add", "secrets to add"),
    "variable": ("variable to add", "variables to add"),
    "action": ("action to allow", "actions to allow"),
    "default": ("new default to confirm", "new defaults to confirm"),
    "new-setting": ("new setting to review", "new settings to review"),
    "binary": ("replaced file", "replaced files"),
    "invalid-before": ("file already invalid", "files already invalid"),
}


def _work_order(item: tuple[str, int]) -> int:
    return list(WORK_NOUNS).index(item[0])


def plan_details(plan: Plan) -> dict:
    """What the report needs, also stored in the PR so re-runs can report it."""
    return {"effort": plan.effort, "work": plan.work_summary, "items": plan.items}


def merge_text(ours: str, base: str, theirs: str, labels, keep: str = "") -> tuple[str, int]:
    """3-way merge with `git merge-file`. keep='ours' resolves conflicting hunks to the repo's side."""
    with tempfile.TemporaryDirectory() as td:
        files = []
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            (Path(td) / name).write_bytes(text.encode())  # UTF-8 on every OS (not the locale code page)
            files.append(str(Path(td) / name))

        def merge_file(mode: str) -> subprocess.CompletedProcess:
            names = ["-L", labels[0], "-L", labels[1], "-L", labels[2]]
            return subprocess.run(
                ["git", "merge-file", "-p", mode, *names, *files],
                capture_output=True,
                check=False,
            )

        p = merge_file("--diff3")  # exit code = number of conflicts; negative or >= 128 is an error
        if not 0 <= p.returncode < 128:
            raise GitError(f"merge-file failed: {p.stderr.decode(errors='replace')}")
        if p.returncode and keep:
            return merge_file(f"--{keep}").stdout.decode(errors="replace"), p.returncode
        return p.stdout.decode(errors="replace"), p.returncode


USES_LINE_RE = re.compile(r"^(\s*-?\s*uses:\s*['\"]?)([^\s'\"#@]+)@([^\s'\"#]+)(['\"]?)\s*(?:#.*)?$")


def _uses(line: str) -> tuple[str, str] | None:
    """(prefix + action, ref) of a `uses: action@ref` line."""
    m = USES_LINE_RE.match(line.rstrip("\r\n"))
    return (m.group(1) + m.group(2) + m.group(4), m.group(3)) if m else None


def take_upstream_versions(merged: str) -> tuple[str, int]:
    """Upstream wins conflicts that are only about action versions: a hunk where your side
    differs from the previous upstream only in the `@ref` of the same actions takes upstream's
    side. Where upstream kept the old version, yours stays (never a downgrade). A ref pinned
    to a commit SHA stays a conflict (re-pin it, don't silently unpin)."""
    out: list[str] = []
    hunk: list[str] = []
    yours: list[str] = []
    base: list[str] = []
    upstream: list[str] = []
    side = None
    conflicts = 0

    def version_only() -> bool:
        if len(yours) != len(base) or yours == base:
            return False
        for y, b in zip(yours, base):
            if y == b:
                continue
            uy, ub = _uses(y), _uses(b)
            if not uy or not ub or uy[0] != ub[0] or re.fullmatch(r"[0-9a-f]{40}", uy[1]):
                return False
        return True

    for ln in merged.splitlines(keepends=True):
        bare = ln.rstrip("\r\n")
        if side is None and bare.startswith("<<<<<<< "):
            hunk, yours, base, upstream, side = [ln], [], [], [], "yours"
            continue
        if side is None:
            out.append(ln)
            continue
        hunk.append(ln)
        if side == "yours" and bare.startswith("||||||| "):
            side = "base"
        elif side in ("yours", "base") and bare == "=======":
            side = "upstream"
        elif side == "upstream" and bare.startswith(">>>>>>> "):
            if version_only():
                out += keep_newer_local(yours, base, upstream)
            else:
                out += hunk
                conflicts += 1
            side = None
        else:
            {"yours": yours, "base": base, "upstream": upstream}[side].append(ln)
    out += hunk if side else []
    return "".join(out), conflicts + (1 if side else 0)


def keep_newer_local(yours: list[str], base: list[str], upstream: list[str]) -> list[str]:
    """Upstream's lines, but an action upstream did not move off the old version keeps yours."""
    mine = {}  # action -> (old ref, your ref)
    for y, b in zip(yours, base):
        if y != b and (my := USES_LINE_RE.match(y.rstrip("\r\n"))) and (mb := USES_LINE_RE.match(b.rstrip("\r\n"))):
            mine[my.group(2)] = (mb.group(3), my.group(3))
    out = []
    for ln in upstream:
        m = USES_LINE_RE.match(ln.rstrip("\r\n"))
        if m and m.group(2) in mine and m.group(3) == mine[m.group(2)][0]:
            ln = ln[: m.start(3)] + mine[m.group(2)][1] + ln[m.end(3) :]
        out.append(ln)
    return out


def versions_taken(ours: str, base: str, merged: str) -> list[str]:
    """Actions where your own version was replaced by upstream's, for the PR."""

    def refs(text: str) -> dict[str, set[str]]:
        found: dict[str, set[str]] = collections.defaultdict(set)
        for ln in text.splitlines():
            if (u := _uses(ln)) and not ln.lstrip().startswith("#"):
                found[u[0].split("uses:", 1)[1].strip(" '\"")].add(u[1])
        return found

    o, b, m = refs(ours), refs(base), refs(merged)
    notes = []
    for action, mine in sorted(o.items()):
        gone = sorted((mine - b.get(action, set())) - m.get(action, set()))
        if gone and m.get(action):
            notes.append(f"`{action}`: {', '.join(gone)} to {', '.join(sorted(m[action]))}")
    return notes


def conflict_hunks(merged: str) -> list[dict]:
    """Line of each conflict and what to do about it, for the report."""
    out: list[dict] = []
    hunk: dict | None = None
    side = None
    for n, ln in enumerate(merged.splitlines(), 1):
        if ln.startswith("<<<<<<< "):
            hunk, side = {"line": n, "yours": [], "upstream": []}, "yours"
        elif hunk is not None and ln.startswith("||||||| "):
            side = None  # the previous upstream version is not needed to decide
        elif hunk is not None and ln == "=======":
            side = "upstream"
        elif hunk is not None and ln.startswith(">>>>>>> "):
            out.append(
                {
                    "line": hunk["line"],
                    "fix": _conflict_fix(hunk["yours"], hunk["upstream"]),
                }
            )
            hunk = side = None
        elif hunk is not None and side:
            hunk[side].append(ln)
    return out


def _conflict_fix(yours: list[str], upstream: list[str]) -> str:
    """One instruction for a conflict, from the first changed line on each side."""

    def first(lines: list[str]) -> tuple[str, str]:
        text = [ln.strip() for ln in lines if ln.strip()]
        more = f" (+{len(text) - 1} lines)" if len(text) > 1 else ""
        return (f"`{text[0][:70]}`" if text else ""), more

    uy, uu = (next(filter(None, map(_uses, side)), None) for side in (yours, upstream))
    if uy and uu and uy[0] == uu[0] and re.fullmatch(r"[0-9a-f]{40}", uy[1]):
        action = uy[0].split("uses:", 1)[1].strip(" '\"")
        return f"Upstream moved `{action}` to `{uu[1]}`: pin it to that version's commit SHA, like you did before"
    while yours and upstream and yours[0].strip() == upstream[0].strip():  # lines both sides share
        yours, upstream = yours[1:], upstream[1:]
    (y, y_more), (u, u_more) = first(yours), first(upstream)
    key = re.compile(r"^`-?\s*([\w.-]+):")
    if y and u and (ky := key.match(y)) and (ku := key.match(u)) and ky.group(1) == ku.group(1):
        return f"Pick one value for `{ky.group(1)}`: yours {y} or upstream's {u}"
    if y and u:
        return f"Keep your {y}{y_more} and add upstream's {u}{u_more}"
    if u:
        return f"You removed this and upstream changed it: re-add upstream's {u}{u_more} only if you need it"
    if y:
        return f"Upstream removed this and you changed it: keep your {y}{y_more} only if you still need it"
    return "Delete the conflict marker lines"


def find_base(cust: Git, head: str, ours: Tree, up: Upstream) -> tuple[str | None, str]:
    """The upstream commit the repo was last aligned with."""
    if (commit := marker_commit(cust, ours)) in up.commit_set:
        return commit, "last sync"
    for commit in cust.run("rev-list", "--max-count=20000", head).split():
        if commit in up.commit_set:
            return commit, "shared history"
    # Copied rather than imported: best-matching upstream commit, the oldest on a tie
    # (a too-new base would make upstream changes look like local reverts).
    pairs = {(p, s) for p, (_, s) in ours.items() if not p.startswith(CLI_DIR) and p != MARKER}
    best, best_commit = 0, None
    for commit in reversed(up.commits):
        score = len(pairs & up.pairs_at(commit))
        if score > best:
            best, best_commit = score, commit
    return (best_commit, "inferred") if best_commit else (None, "none")


def is_cli_binary(path: str) -> bool:
    """Veracode CLI binaries: a job in each repo keeps them current, so this tool never touches them."""
    return path.startswith(CLI_DIR) and bool(CLI_RE.search(path))


def is_workflow(path: str) -> bool:
    return path.startswith(WORKFLOW_DIR) and path.endswith((".yml", ".yaml"))


def merge_repo(
    cust: Git,
    up: Upstream,
    ours: Tree,
    base_commit: str | None,
    base_how: str,
    args: argparse.Namespace,
) -> Plan:
    """3-way merge of every file, then the checks that decide whether the PR is ready."""
    base = up.tree_at(base_commit) if base_commit else {}
    theirs = up.tree
    if base_how == "inferred":
        base = _per_file_bases(cust, up, ours, base)
    final = {p: e for p, e in ours.items() if p != MARKER}
    plan = Plan(base_commit, base_how, final)
    labels = ("your version", "previous upstream", "new upstream")

    def kept(path):
        return any(fnmatch.fnmatch(path, g) for g in (*ALWAYS_KEEP, *args.keep))

    def take_upstream(path):
        final[path] = (theirs[path][0], cust.write_blob(up.blob(theirs[path][1])))

    for path in sorted(p for p in set(ours) | set(theirs) | set(base) if p != MARKER and not is_cli_binary(p)):
        O, B, T = ours.get(path), base.get(path), theirs.get(path)
        o, b, t = (e[1] if e else None for e in (O, B, T))
        if o == t or b == t:  # nothing to do, or upstream did not change it
            continue
        if kept(path):
            plan.changes["protected-changed"].append(path)
            continue
        if b is not None and o == b and not (path == CONFIG_FILE and args.keep_defaults):
            if T:  # never changed locally: take upstream as is
                take_upstream(path)
                plan.changes["updated"].append(path)
            else:
                del final[path]
                plan.changes["removed"].append(path)
            continue
        if O is None:
            if B is None:
                take_upstream(path)
                plan.changes["added"].append(path)
            else:
                plan.changes["deleted-by-you"].append(path)
            continue
        if T is None:
            plan.changes["customized-removed-upstream"].append(path)
            continue

        o_text, t_text, b_text = cust.text(O), up.text(T), up.text(B)
        if Path(path).suffix.lower() in BINARY_EXT or "\0" in o_text[:8000] + t_text[:8000]:
            take_upstream(path)  # binaries cannot be merged: upstream wins and the PR says so
            plan.changes["updated"].append(path)
            plan.add(
                False,
                f"Upstream replaced `{path}`, which you had changed. Restore yours if needed",
                "binary",
                action="Check file upstream replaced",
                where=path,
                details="Restore your version from the default branch if you still need it",
            )
            continue
        merged, conflicts = merge_text(o_text, b_text, t_text, labels)
        if conflicts and path.endswith((".yml", ".yaml")):
            merged, conflicts = take_upstream_versions(merged)
        if conflicts and is_workflow(path) and (resolved := step_level_merge(o_text, b_text, t_text, labels)):
            merged, conflicts = resolved, 0
        plan.versions += [f"{x} in `{path}`" for x in versions_taken(o_text, b_text, merged)]
        if path == CONFIG_FILE:
            if conflicts:  # the repo's settings win; fall back to markers only if that breaks the file
                ours_wins = merge_text(o_text, b_text, t_text, labels, keep="ours")[0]
                if not safe_yaml(ours_wins)[1]:
                    merged, conflicts = ours_wins, 0
            if not conflicts:
                merged, plan.config = merge_config(merged, b_text, t_text, o_text, args.keep_defaults)
        if merged == o_text:
            continue
        final[path] = (O[0], cust.write_blob(merged.encode()))
        if conflicts:
            hunks = conflict_hunks(merged)
            lines = [h["line"] for h in hunks]
            where = f" (line{'s' if len(lines) > 1 else ''} {', '.join(map(str, lines))})" if lines else ""
            count = "the conflict" if len(lines) <= 1 else f"{len(lines)} conflicts"
            rows = [
                {
                    "action": "Resolve merge conflict",
                    "where": f"{path} line {h['line']}",
                    "details": h["fix"],
                }
                for h in hunks
            ]
            plan.add(
                True,
                f"Resolve {count} in `{path}`{where}",
                "conflict",
                max(1, len(lines)),
                rows=rows,
            )
            plan.changes["conflict"].append(path)
        else:
            plan.changes["merged"].append(path)

    _check(plan, cust, up, ours, base)
    plan.custom_files = sorted(
        p for p in ours if p not in base and p not in theirs and p != MARKER and not is_cli_binary(p)
    )
    plan.protected = sorted(p for p in ours if kept(p))
    structure_check(plan, cust, up, ours)
    verify(plan, cust, up, ours, base)
    return plan


def _per_file_bases(cust: Git, up: Upstream, ours: Tree, base: Tree) -> Tree:
    """Repo copied without history: give each file its own base, the upstream version closest
    to the local file. A single inferred commit can be right for most files and wrong for a few."""
    out = dict(base)
    for path, (mode, sha) in ours.items():
        if (
            path in up.tree
            and path != MARKER
            and not is_cli_binary(path)
            and Path(path).suffix.lower() not in BINARY_EXT
            and sha != base.get(path, ("", ""))[1]
            and (better := up.closest_version(path, cust.text(ours[path]), base.get(path, ("", None))[1]))
        ):
            out[path] = (mode, better)
    return out


def _check(plan: Plan, cust: Git, up: Upstream, ours: Tree, base: Tree) -> None:
    """YAML, settings and custom steps; workflow references are checked by structure_check."""
    _check_changed_yaml(plan, cust, ours)
    _config_todos(plan)
    _check_custom_steps(plan, cust, up, ours, base)


def _check_changed_yaml(plan: Plan, cust: Git, ours: Tree) -> None:
    """Every changed YAML file parses (no duplicate keys); collect actions new to the repo."""
    final, conflicted = plan.final, set(plan.changes["conflict"])
    used_before = {
        u.split("@")[0].lower() for p, e in ours.items() if is_workflow(p) for u in USES_RE.findall(cust.text(e))
    }
    for path in sorted(p for p, e in final.items() if ours.get(p) != e and p.endswith((".yml", ".yaml"))):
        text = cust.text(final[path])
        if path not in conflicted:
            err = safe_yaml(text)[1]
            if err:
                before = safe_yaml(cust.text(ours.get(path)))[1] if path in ours else None
                dup = re.search(r"duplicate key '([^']+)' \(lines (\d+) and (\d+)\)", err)
                if before:
                    plan.add(
                        False,
                        f"Fix `{path}`: it was already invalid before this update ({before})",
                        "invalid-before",
                        action="Fix file that was already invalid",
                        where=path,
                        details=f"Fix the YAML error (it was there before this update): {before}",
                    )
                elif dup:
                    plan.add(
                        True,
                        f"Keep only one `{dup[1]}` setting in `{path}` (lines {dup[2]} and {dup[3]})",
                        "duplicate",
                        action="Remove duplicate setting",
                        where=f"{path} lines {dup[2]} and {dup[3]}",
                        details=f"Delete one of the two `{dup[1]}` lines, keeping the value you want",
                    )
                else:
                    plan.add(
                        True,
                        f"Fix invalid YAML in `{path}`: {err}",
                        "yaml",
                        action="Fix invalid YAML",
                        where=path,
                        details=f"Fix the YAML error: {err}",
                    )
        if is_workflow(path) and path not in conflicted:
            plan.new_uses |= {
                u
                for u in USES_RE.findall(text)
                if not u.startswith(("./", "docker://")) and u.split("@")[0].lower() not in used_before
            }


def _config_todos(plan: Plan) -> None:
    row = {"where": CONFIG_FILE}
    for key, change in plan.config.get("defaults", []):
        text = f"Confirm the new default for `{key}` in `{CONFIG_FILE}` ({change})"
        details = f"`{key}` now uses upstream's new default ({change}): change it back if you relied on the old value"
        plan.add(
            False,
            text,
            "default",
            rows=[{"action": "Confirm new default", "details": details, **row}],
        )
    for key in plan.config.get("missing", []):
        text = f"Add the new setting `{key}` to `{CONFIG_FILE}` if you need it"
        details = f"Add `{key}` if you need it (new upstream setting; its section was removed here)"
        plan.add(
            False,
            text,
            "new-setting",
            rows=[{"action": "Review new setting", "details": details, **row}],
        )
    for key in plan.config.get("unused", []):
        text = f"Move your `{CONFIG_FILE}` setting `{key}`: upstream no longer reads it there"
        details = f"Set your `{key}` value where upstream now reads it, then delete the old line"
        plan.add(
            True,
            text,
            "moved-setting",
            rows=[{"action": "Move custom setting", "details": details, **row}],
        )


def _check_custom_steps(plan: Plan, cust: Git, up: Upstream, ours: Tree, base: Tree) -> None:
    """Custom steps are still in their job, and edits next to reshaped upstream steps get a check."""
    final, conflicted = plan.final, set(plan.changes["conflict"])
    for path in sorted(p for p in ours if is_workflow(p) and p in base and p in final):
        O, B = safe_yaml(cust.text(ours[path]))[0], safe_yaml(up.text(base[path]))[0]
        added = custom_steps(O, B)
        F = safe_yaml(cust.text(final[path]))[0] if path not in conflicted else None
        for job, k, st in added:
            label = _label(st, k)
            plan.custom_steps.append((path, job, label))
            if F is None:
                continue
            if k[0] in {_step_key(s) for s in _job_steps(F, job) or []}:
                continue
            elsewhere = [j for j in (F.get("jobs") or {}) if k[0] in {_step_key(s) for s in _job_steps(F, j) or []}]
            where = f"is now in job `{elsewhere[0]}`" if elsewhere else "is missing"
            loc = locate(
                cust.text(final[path]).splitlines(),
                ["jobs", elsewhere[0] if elsewhere else job],
            )
            plan.add(
                True,
                f"Check your step `{label}` in `{path}`: upstream reshaped job `{job}` and the step {where}",
                "placement",
                action="Check custom step",
                where=f"{path} line {loc[0] + 1}, job {elsewhere[0] if elsewhere else job}"
                if loc
                else f"{path}, job {elsewhere[0] if elsewhere else job}",
                details=(
                    f"Check your step `{label}` belongs in job `{elsewhere[0]}` (upstream reshaped `{job}`)"
                    if elsewhere
                    else f"Add your step `{label}` back to job `{job}` (upstream reshaped it)"
                ),
            )
        if F is not None and path in up.tree and not any(f"`{path}`" in t for b, t in plan.todo if b):
            for what, why in reshaped_edits(O, B, safe_yaml(up.text(up.tree[path]))[0]):
                plan.add(
                    True,
                    f"Check {what} in `{path}`: {why}",
                    "placement",
                    action="Check custom edit placement",
                    where=path,
                    details=f"Check {what} still sits where it should: {why}",
                )


def _jobs(doc) -> dict:
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    return {j: s for j in jobs if (s := _job_steps(doc, j)) is not None} if isinstance(jobs, dict) else {}


def _loose_keys(steps) -> list[tuple]:
    """Occurrence keys that ignore case and spacing, so `set version` to `Set version` is no change."""
    return [((kind, " ".join(str(val).lower().split())), n) for (kind, val), n in _occ_keys(steps)]


def reshaped_edits(O, B, T) -> list[tuple[str, str]]:
    """Local edits next to steps that upstream renamed, removed or rearranged. The
    merge keeps them, but where they land is a judgement call a person should confirm."""
    out = []
    b_jobs, t_jobs = _jobs(B), _jobs(T)
    for job, so in _jobs(O).items():
        sb, st = b_jobs.get(job), t_jobs.get(job)
        if sb is None or so == sb or st == sb:  # custom job, unchanged locally, or by upstream
            continue
        if st is None:
            out.append((f"your changes to job `{job}`", "upstream renamed or removed this job"))
            continue
        okeys, bkeys, tkeys = (_loose_keys(x) for x in (so, sb, st))
        for i, (k, step) in enumerate(zip(okeys, so, strict=True)):
            if k in bkeys:
                if step != sb[bkeys.index(k)] and k not in tkeys:
                    out.append(
                        (
                            f"your change to step `{_label(step, k)}`",
                            "upstream removed or renamed that step",
                        )
                    )
                continue
            prev = next((x for x in reversed(okeys[:i]) if x in bkeys), None)
            nxt = next((x for x in okeys[i + 1 :] if x in bkeys), None)
            moved = any(x is not None and x not in tkeys for x in (prev, nxt)) or (
                prev in tkeys
                and nxt in tkeys
                and tkeys.index(nxt) - tkeys.index(prev) != bkeys.index(nxt) - bkeys.index(prev)
            )
            if moved:
                out.append(
                    (
                        f"where your step `{_label(step, k)}` landed",
                        "upstream changed the steps around it",
                    )
                )
    return out


class SafetyError(RuntimeError):
    pass


def verify(plan: Plan, cust: Git, up: Upstream, ours: Tree, base: Tree) -> None:
    """Last check before anything is pushed. These must hold whatever the merge did;
    if one fails it is a bug, so the repo gets no pull request."""
    final, conflicted, bad = plan.final, set(plan.changes["conflict"]), []
    bad += [f"`{p}` would change" for p in plan.protected + plan.custom_files if final.get(p) != ours[p]]
    cli = {p for p in set(ours) | set(final) if is_cli_binary(p)}
    bad += [
        f"`{p}` would change (CLI binaries are left to the repo's own job)" for p in cli if final.get(p) != ours.get(p)
    ]
    for p, e in ours.items():
        if p not in final and p != MARKER and not is_cli_binary(p) and e != base.get(p):
            bad.append(f"`{p}` would be deleted although you changed it")
    marker = re.compile(r"^(<<<<<<<|>>>>>>>) ", re.MULTILINE)
    for p, e in final.items():
        if e == ours.get(p) or p in conflicted or is_cli_binary(p) or Path(p).suffix.lower() in BINARY_EXT:
            continue
        if marker.search(cust.text(e)) and not marker.search(cust.text(ours.get(p)) + up.text(up.tree.get(p))):
            bad.append(f"`{p}` would contain conflict markers")
    if CONFIG_FILE in ours and CONFIG_FILE in final and CONFIG_FILE not in conflicted:
        O, F = (
            safe_yaml(cust.text(ours[CONFIG_FILE]))[0],
            safe_yaml(cust.text(final[CONFIG_FILE]))[0],
        )
        B = safe_yaml(up.text(base.get(CONFIG_FILE)))[0]
        if isinstance(O, dict) and isinstance(F, dict):
            fb, ff = flatten(B if isinstance(B, dict) else {}), flatten(F)
            T = safe_yaml(up.text(up.tree.get(CONFIG_FILE)))[0]
            for k, val in flatten(O).items():
                if fb.get(k, _MISSING) != val and ff.get(k, _MISSING) != val:
                    why = (
                        "" if isinstance(T, dict) and k[0] in T else f" (upstream renamed or removed section `{k[0]}`)"
                    )
                    bad.append(f"`{CONFIG_FILE}` would lose your value for `{'.'.join(map(str, k))}`{why}")
    if bad:
        raise SafetyError("safety check failed, nothing pushed: " + "; ".join(bad[:5]))


# --------------------------------------------------------------------------- #
# Pull request
# --------------------------------------------------------------------------- #
DRAFT_NOTE = " This pull request is a draft until then."
NOT_DRAFT_NOTE = " Do not merge until then (this repository does not support draft pull requests)."
CONFLICT_HELP = [
    "1. Open **Files changed** and edit the file (or edit it on this branch).",
    (
        "2. Your version is on top, upstream's at the bottom. Keep the lines you need and delete the marker lines"
        " (`<<<<<<<`, `|||||||`, `=======`, `>>>>>>>`)."
    ),
    "3. Commit to this branch, then mark the pull request **Ready for review**.",
]


def _details(title: str, rows: list[str]) -> list[str]:
    while rows and not rows[-1]:
        rows = rows[:-1]
    return [f"<details><summary>{title}</summary>", "", *rows, "", "</details>", ""] if rows else []


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def pr_body(plan: Plan, up: Upstream) -> str:
    ch = plan.changes
    conflicts = len(ch["conflict"])
    others = len(plan.todo) - conflicts
    if plan.todo:
        parts = ([f"{_n(conflicts, 'conflict')} to resolve"] if conflicts else []) + (
            [f"{_n(others, 'item')} to check"] if others else []
        )
        status = f"**Action required:** {' and '.join(parts)}." + (DRAFT_NOTE if plan.blocking else "")
    elif ch["merged"]:
        status = "**No action needed.** Your customizations are kept. Review and merge when ready."
    else:
        status = "**No action needed.** This update only changes files you have not customized."
    lines = [status, "", "### To do", ""]
    lines += [f"- [ ] {text}" for _, text in sorted(plan.todo, key=lambda x: not x[0])]
    lines += [
        "- [ ] Merge this pull request",
        "- [ ] Run one scan after merging to confirm the checks pass",
        "",
    ]
    if conflicts:
        lines += _details("How to resolve a conflict", CONFLICT_HELP)

    cfg_kept = plan.config.get("kept", [])
    kept = [
        x
        for x in (
            plan.custom_files and _n(len(plan.custom_files), "custom file"),
            plan.custom_steps and _n(len(plan.custom_steps), "custom step"),
            cfg_kept and _n(len(cfg_kept), f"`{CONFIG_FILE}` setting"),
        )
        if x
    ]
    summary = ", ".join(kept[:-1]) + (" and " if len(kept) > 1 else "") + kept[-1] if kept else ""
    lines += [
        "### Your customizations",
        "",
        (f"Kept: {summary}." if summary else "No customizations found.")
        + (f" {', '.join(f'`{p}`' for p in plan.protected)} not modified." if plan.protected else ""),
        "",
    ]
    rows = []
    for title, items in (
        ("Custom files", [f"`{p}`" for p in plan.custom_files]),
        (
            "Custom steps",
            [f"`{lbl}` in `{p.rsplit('/', 1)[-1]}`, job `{job}`" for p, job, lbl in plan.custom_steps],
        ),
        (f"`{CONFIG_FILE}` settings", cfg_kept),
        (
            "Customized files updated with upstream changes",
            [f"`{p}`" for p in ch["merged"]],
        ),
        ("Action versions taken from upstream (yours replaced)", plan.versions),
        ("Deleted by you, not restored", [f"`{p}`" for p in ch["deleted-by-you"]]),
        (
            "Removed upstream, kept because you customized them",
            [f"`{p}`" for p in ch["customized-removed-upstream"]],
        ),
        (
            "Protected, upstream changes not applied",
            [f"`{p}`" for p in ch["protected-changed"]],
        ),
    ):
        if items:
            rows += [f"**{title}**", *(f"- {x}" for x in items), ""]
    lines += _details("Details", rows)

    counts = [f"{len(ch[k])} {k}" for k in ("updated", "added", "removed") if ch[k]]
    if counts:
        counts[0] = counts[0].replace(" ", " file " if counts[0].startswith("1 ") else " files ", 1)
    summary_bits = [
        x
        for x in (
            ", ".join(counts),
            ch["merged"] and f"{_n(len(ch['merged']), 'customized file')} updated",
            plan.versions and f"{_n(len(plan.versions), 'action version')} taken from upstream",
            plan.config.get("added") and _n(len(plan.config["added"]), f"new `{CONFIG_FILE}` setting"),
        )
        if x
    ]
    lines += [
        "### What this update includes",
        "",
        (". ".join(summary_bits) or "Sync metadata only") + ".",
        "",
    ]
    rows = []
    for k in ("updated", "added", "removed"):
        if ch[k]:
            rows += [f"**{k.capitalize()}**", *(f"- `{p}`" for p in ch[k]), ""]
    if plan.config.get("added"):
        rows += [
            f"**New `{CONFIG_FILE}` settings**",
            *(f"- `{x}`" for x in plan.config["added"]),
            "",
        ]
    lines += _details("Files", rows)
    web = up.web_url()
    source = f"[{up.sha[:7]}]({web}/commit/{up.sha})" if web else f"`{up.sha[:7]}`"
    base = f"`{(plan.base or 'none')[:7]}` ({plan.base_how})"
    lines.append(f"<sub>Upstream {source}, base {base}. veracode-upstream-sync {__version__}.</sub>")
    return "\n".join(lines)[:60000]


def has_trailer(message: str) -> bool:
    return any(line.startswith(f"{TRAILER}:") for line in message.splitlines())


def tool_only(gh: GitHub, full: str, base: str, head: str) -> bool:
    """True when every commit on `head` not in `base` was made by this tool."""
    q = urllib.parse.quote
    cmp = gh.get(f"/repos/{full}/compare/{q(base, safe='')}...{q(head, safe='')}", ok404=True)
    if not cmp:
        return False
    commits = cmp.get("commits") or []
    return cmp.get("total_commits", len(commits)) <= len(commits) and all(
        has_trailer((c.get("commit") or {}).get("message", "")) for c in commits
    )


def create_pr(gh: GitHub, full: str, owner: str, payload: dict) -> dict:
    """POST is not idempotent: after an ambiguous failure, look for the PR before retrying."""
    head = urllib.parse.quote(f"{owner}:{payload['head']}", safe=":")

    def existing() -> dict | None:
        found = gh.get(f"/repos/{full}/pulls?state=open&head={head}") or []
        return found[0] if found else None

    for attempt in range(3):
        try:
            return gh.req("POST", f"/repos/{full}/pulls", payload)[0]
        except GitHubError as e:
            if e.maybe_applied:
                if pr := existing():
                    return pr
                gh.limiter.sleep(2 ** (attempt + 1))
                continue
            if e.status == 422 and "already exists" in str(e).lower() and (pr := existing()):  # concurrent run
                return pr
            if e.status == 422 and payload.get("draft") and "draft" in str(e).lower():
                payload = {**payload, "draft": False}  # plan without draft PRs
                continue
            raise
    raise GitHubError(0, "could not create the pull request")


@dataclasses.dataclass
class Result:
    repo: str
    status: str
    detail: str = ""
    pr: str = ""
    effort: str = ""  # see Plan.effort; "manual" when no PR could be opened
    work: str = ""
    details: dict = dataclasses.field(default_factory=dict)  # plan_details(), for the report


def sync_repo(
    gh: GitHub,
    up: Upstream,
    repo: dict,
    args: argparse.Namespace,
    author: tuple[str, str],
    workdir: Path,
) -> Result:
    """Sync one repo: checks, merge, then (unless --dry-run) push a branch and open the PR."""
    full, default = repo["full_name"], repo["default_branch"]
    owner, name = full.split("/")
    branch = f"{args.branch_prefix}/{up.sha[:10]}"
    if skip := _precheck(repo, branch, args):
        return skip
    open_prs = [
        p
        for p in gh.paginate(f"/repos/{full}/pulls?state=open")
        if p["head"]["ref"].startswith(args.branch_prefix + "/")
        and (p["head"].get("repo") or {}).get("full_name") == full
    ]
    closed: list = []  # this update's closed PRs: declined, or merged and later undone
    if not args.dry_run:
        done, closed = _already_handled(gh, full, owner, branch, open_prs, args.force)
        if done:
            return done

    clone_url = repo["clone_url"]
    cust = clone(
        clone_url,
        workdir / f"{owner}__{name}.git",
        git_env(gh.token, clone_url),
        branch=default,
    )
    try:
        head = cust.rev("HEAD")
        ours = cust.ls_tree(head)
        base_commit, how = find_base(cust, head, ours, up)
        if CONFIG_FILE not in ours or base_commit is None:
            return Result(
                full,
                "skipped",
                f"does not look like the Veracode integration (no {CONFIG_FILE})",
            )
        if newer_than_target(cust, head, ours, up):
            return Result(
                full,
                "skipped",
                f"already has upstream changes newer than {up.ref}; nothing to do",
            )
        plan = merge_repo(cust, up, ours, base_commit, how, args)
        if {p: e for p, e in plan.final.items() if p != MARKER} == {p: e for p, e in ours.items() if p != MARKER}:
            return Result(full, "up-to-date", f"base: {how}")
        # merged before, yet the repo does not have it: someone undid it (--revert, GitHub's Revert button, by hand)
        if undone := [p for p in closed if p.get("merged_at")]:
            return Result(
                full, "skipped", "this update was reverted (use --force to apply it again)", undone[0]["html_url"]
            )
        check_settings(gh, full, plan, cust, ours)
        marker = {
            "upstream": up.url,
            "ref": up.ref,
            "commit": up.sha,
            "synced_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        plan.final[MARKER] = (
            "100644",
            cust.write_blob((json.dumps(marker, indent=2) + "\n").encode()),
        )
        details = plan_details(plan)
        tag = json.dumps(details).replace("--", "\\u002d\\u002d")  # never ends the HTML comment early
        body = pr_body(plan, up) + f"\n{EFFORT_TAG}{tag} -->"
        state = "needs work" if plan.blocking else ("check" if plan.todo else "ready")
        effort: dict = {
            "effort": plan.effort,
            "work": plan.work_summary,
            "details": details,
        }
        if args.dry_run:
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            commit = cust.commit(plan.final, head, "dry run", author)
            patch = cust.run("diff", head, commit)
            (out / f"{owner}__{name}.patch").write_text(patch, encoding="utf-8")
            (out / f"{owner}__{name}.md").write_text(body, encoding="utf-8")
            return Result(full, "dry-run", f"{state}, see {out}/{owner}__{name}.md", **effort)

        message = f"Sync Veracode integration with upstream {up.sha[:10]}\n\n"
        message += f"Upstream: {up.url} @ {up.sha}\n\n{TRAILER}: {up.sha}\n"
        commit = cust.commit(plan.final, head, message, author)
        if refused := _push_branch(gh, cust, full, default, branch, commit, clone_url):
            return refused
        title = f"{args.title}{': action required' if plan.todo else ''} ({up.sha[:7]})"
        pr = _open_pr(gh, full, owner, default, branch, plan, body, title, args)
        _close_superseded(gh, full, default, open_prs, up)
        return Result(full, "pr-created", state, pr["html_url"], **effort)
    finally:
        shutil.rmtree(cust.path, ignore_errors=True)


def revert_repo(gh: GitHub, repo: dict, args: argparse.Namespace, author: tuple[str, str], workdir: Path) -> Result:
    """Undo the last update: close an open update PR, or open a PR that reverts the last merged one.
    Only that update's changes are undone; later changes to the same files are kept."""
    full, default = repo["full_name"], repo["default_branch"]
    owner, name = full.split("/")
    if skip := _precheck(repo, f"{args.branch_prefix}-revert/", args):  # archived, no push access, host
        return skip
    mine = [
        p
        for p in gh.paginate(f"/repos/{full}/pulls?state=all")
        if p["head"]["ref"].startswith(args.branch_prefix + "/")
        and (p["head"].get("repo") or {}).get("full_name") == full
    ]
    if open_prs := [p for p in mine if p["state"] == "open"]:
        nums = ", ".join(f"#{p['number']}" for p in open_prs)
        if args.dry_run:
            return Result(
                full, "dry-run", f"would close the open update PR {nums} (not merged)", open_prs[0]["html_url"]
            )
        for p in open_prs:  # closed without merging: later runs treat it as declined
            gh.req("PATCH", f"/repos/{full}/pulls/{p['number']}", {"state": "closed"})
            if tool_only(gh, full, default, p["head"]["ref"]):
                ref = urllib.parse.quote(p["head"]["ref"])
                _best_effort(
                    f"{full}: delete branch",
                    lambda ref=ref: gh.req("DELETE", f"/repos/{full}/git/refs/heads/{ref}", ok404=True),
                )
        return Result(full, "reverted", f"closed the open update PR {nums}, it was not merged", open_prs[0]["html_url"])
    merged = sorted((p for p in mine if p.get("merged_at")), key=lambda p: (p["merged_at"], p["number"]))
    if not merged:
        return Result(full, "skipped", "nothing to revert: no merged update PR")
    pr = merged[-1]
    branch = revert_branch(pr["head"]["ref"])
    q = urllib.parse.quote(f"{owner}:{branch}", safe=":")
    for p in gh.get(f"/repos/{full}/pulls?state=all&head={q}") or []:
        if p.get("state") == "open":
            return Result(full, "pr-exists", "revert already open", p["html_url"], **_pr_effort(p))
        if (p.get("merged_at") or "") > pr["merged_at"]:
            return Result(full, "skipped", f"already reverted (#{pr['number']})", p["html_url"])

    clone_url = repo["clone_url"]
    cust = clone(clone_url, workdir / f"{owner}__{name}.git", git_env(gh.token, clone_url), branch=default)
    try:
        head = cust.rev("HEAD")
        cust.run("fetch", "--quiet", "origin", f"+refs/pull/{pr['number']}/head:refs/vsync/update")
        after = cust.rev("refs/vsync/update")
        # What the update changed: its head against the default branch it was based on. The commit
        # before the merge (merge commit, squash, rebase) or, for a fast-forward, the update's first parent.
        merge = pr.get("merge_commit_sha")
        anchor: str | None
        if merge and merge != after:
            anchor = cust.rev(f"{merge}^1")
        else:
            commits = gh.get(f"/repos/{full}/pulls/{pr['number']}/commits?per_page=250") or []
            anchor = (commits[0].get("parents") or [{}])[0].get("sha") if commits else None
            if not anchor:
                raise GitError(f"cannot tell what #{pr['number']} changed; revert it by hand")
        before = cust.run("merge-base", after, anchor).strip()
        plan = revert_plan(cust, cust.ls_tree(head), cust.ls_tree(after), cust.ls_tree(before))
        if plan.final == cust.ls_tree(head):
            return Result(full, "skipped", f"nothing to revert: files already match the state before #{pr['number']}")
        sha = pr["head"]["ref"].rsplit("/", 1)[-1]
        details = plan_details(plan)
        tag = json.dumps(details).replace("--", "\\u002d\\u002d")
        body = revert_body(plan, pr, sha) + f"\n{EFFORT_TAG}{tag} -->"
        state = "needs work" if plan.blocking else "ready"
        effort: dict = {"effort": plan.effort, "work": plan.work_summary, "details": details}
        if args.dry_run:
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            commit = cust.commit(plan.final, head, "dry run", author)
            (out / f"{owner}__{name}.revert.patch").write_text(cust.run("diff", head, commit), encoding="utf-8")
            (out / f"{owner}__{name}.revert.md").write_text(body, encoding="utf-8")
            return Result(full, "dry-run", f"revert {state}, see {out}/{owner}__{name}.revert.md", **effort)
        message = f"Revert Veracode integration update {sha}\n\nReverts #{pr['number']}.\n\n{TRAILER}: revert {sha}\n"
        commit = cust.commit(plan.final, head, message, author)
        if refused := _push_branch(gh, cust, full, default, branch, commit, clone_url):
            return refused
        title = f"Revert: {pr['title']}"
        new = _open_pr(gh, full, owner, default, branch, plan, body, title, args, extra_labels=("revert",))
        return Result(full, "pr-created", f"revert {state}", new["html_url"], **effort)
    finally:
        shutil.rmtree(cust.path, ignore_errors=True)


def revert_plan(cust: Git, ours: Tree, after: Tree, before: Tree) -> Plan:
    """Undo after->before on the current files with a 3-way merge per file."""
    plan = Plan(None, "revert", dict(ours))
    labels = ("current", "after the update", "before the update")
    for path in sorted(p for p in set(after) | set(before) if not is_cli_binary(p)):  # CLI: the repo's job
        a, b, o = after.get(path), before.get(path), ours.get(path)
        if a == b or o == b:
            continue  # not part of the update, or already reverted
        if o == a:  # unchanged since the update
            if b is None:
                del plan.final[path]
                plan.changes["removed"].append(path)
            else:
                plan.final[path] = b
                plan.changes["added" if a is None else "updated"].append(path)
            continue
        texts = [cust.text(x) if x else "" for x in (o, a, b)]
        if (
            o is None
            or a is None
            or b is None
            or Path(path).suffix.lower() in BINARY_EXT
            or any("\0" in t[:8000] for t in texts)
        ):
            plan.add(
                True,
                f"`{path}` changed since the update: revert it by hand",
                "conflict",
                action="Revert file by hand",
                where=path,
                details="It changed after the update, so it was left as it is now",
            )
            continue
        merged, conflicts = merge_text(texts[0], texts[1], texts[2], labels)
        plan.final[path] = (o[0], cust.write_blob(merged.encode()))
        if conflicts:
            rows = [
                {"action": "Resolve merge conflict", "where": f"{path} line {h['line']}", "details": h["fix"]}
                for h in conflict_hunks(merged)
            ]
            plan.add(True, f"Resolve {_n(len(rows), 'conflict')} in `{path}`", "conflict", max(1, len(rows)), rows=rows)
            plan.changes["conflict"].append(path)
        else:
            plan.changes["merged"].append(path)
            if path.endswith((".yml", ".yaml")) and (err := safe_yaml(merged)[1]) and not safe_yaml(texts[0])[1]:
                plan.add(
                    True,
                    f"Fix the YAML in `{path}`: {err}",
                    "yaml",
                    action="Fix YAML",
                    where=path,
                    details=f"Fix the YAML error: {err}",
                )
    return plan


def revert_body(plan: Plan, pr: dict, sha: str) -> str:
    ch = plan.changes
    lines = [
        (
            f"Reverts #{pr['number']} (upstream `{sha[:7]}`): the files go back to how they were before that "
            "update. Changes made after it are kept."
        ),
        "",
    ]
    if plan.todo:
        lines += [f"**Action required:** {_n(len(plan.todo), 'item')} to fix before merging.{DRAFT_NOTE}", ""]
        lines += ["### To do", "", *(f"- [ ] {text}" for _, text in plan.todo), ""]
        if ch["conflict"]:
            lines += [
                (
                    "In each conflict, `current` is the file now and `before the update` is what it goes back to. "
                    "Keep the lines you need and delete the marker lines."
                ),
                "",
            ]
    rows = []
    for title, key in (
        ("Restored", "updated"),
        ("Restored, merged with later changes", "merged"),
        ("Conflicts", "conflict"),
        ("Removed (added by the update)", "removed"),
        ("Re-added (removed by the update)", "added"),
    ):
        if ch[key]:
            rows += [f"**{title}**", *(f"- `{p}`" for p in ch[key]), ""]
    lines += _details("Files", rows)
    lines += [
        (
            "Later runs skip this upstream version for this repo. A newer upstream release opens a new update "
            "PR; to apply this one again, run with `--force`."
        ),
        "",
    ]
    return "\n".join(lines)


def _precheck(repo: dict, branch: str, args: argparse.Namespace) -> Result | None:
    full = repo["full_name"]
    if branch == repo["default_branch"]:
        return Result(full, "error", "refusing to use the default branch")
    if repo.get("archived"):
        return Result(full, "skipped", "archived")
    if not args.dry_run and not (repo.get("permissions") or {}).get("push"):
        return Result(full, "skipped", "token has no push access")
    clone_url = repo["clone_url"]
    if clone_url.startswith("https://") and urllib.parse.urlsplit(clone_url).netloc != args.git_host:
        return Result(
            full,
            "error",
            f"clone URL host is not {args.git_host}; refusing to send the token there",
        )
    return None


EFFORT_TAG = "<!-- veracode-upstream-sync effort: "


def _pr_effort(pr: dict) -> dict:
    """Effort and details recorded in an open sync PR. A draft a person marked ready counts as ready."""
    m = re.search(re.escape(EFFORT_TAG) + r"(\{.*\}) -->", pr.get("body") or "", re.DOTALL)
    with suppress(ValueError, TypeError, AttributeError):
        data = json.loads(m.group(1)) if m else {}
        effort, work = str(data.get("effort", "")), str(data.get("work", ""))
        if effort in ("quick", "edit", "long") and not pr.get("draft"):
            data.update(effort="ready", draft=False, work=f"marked ready ({work})")
        return {
            "effort": data.get("effort", ""),
            "work": data.get("work", ""),
            "details": data,
        }
    return {}


def _already_handled(
    gh: GitHub, full: str, owner: str, branch: str, open_prs: list, force: bool
) -> tuple[Result | None, list]:
    """A PR for this upstream commit is open, or it was closed without merging (declined).
    Also returns this update's closed PRs (none with --force), so they are read once."""
    same = [p for p in open_prs if p["head"]["ref"] == branch]
    if same:
        return Result(
            full,
            "pr-exists",
            "already open for this upstream commit",
            same[0]["html_url"],
            **_pr_effort(same[0]),
        ), []
    closed: list = []
    if not force:
        closed = _closed_prs(gh, full, owner, branch)
        declined = [
            p
            for p in closed
            if not p.get("merged_at") and "superseded" not in {lb["name"] for lb in p.get("labels", [])}
        ]
        if declined:
            return Result(
                full,
                "skipped",
                "this update was declined before (use --force)",
                declined[0]["html_url"],
            ), closed
    return None, closed


def _closed_prs(gh: GitHub, full: str, owner: str, branch: str) -> list:
    q = urllib.parse.quote(f"{owner}:{branch}", safe=":")
    return gh.get(f"/repos/{full}/pulls?state=closed&head={q}") or []


def revert_branch(sync_branch: str) -> str:
    """veracode-upstream-sync/<sha> -> veracode-upstream-sync-revert/<sha>: outside the sync
    prefix, so sync runs never treat a revert PR as an older update to close."""
    prefix, sha = sync_branch.rsplit("/", 1)
    return f"{prefix}-revert/{sha}"


def _push_branch(
    gh: GitHub,
    cust: Git,
    full: str,
    default: str,
    branch: str,
    commit: str,
    clone_url: str,
) -> Result | None:
    """Push the sync branch and confirm it landed. Never overwrites a branch a person committed to."""
    push = ["push", "--quiet", clone_url, f"{commit}:refs/heads/{branch}"]
    remote = cust.run("ls-remote", clone_url, f"refs/heads/{branch}").split()
    if remote:  # our branch exists with no open PR: overwrite only if no human committed to it
        if not tool_only(gh, full, default, branch):
            return Result(
                full,
                "error",
                f"branch {branch} has commits from someone else; delete it or use --branch-prefix",
            )
        push.insert(1, f"--force-with-lease=refs/heads/{branch}:{remote[0]}")
    try:
        cust.run(*push)
    except GitError as e:
        hint = " (the token needs the 'workflow' scope)" if "workflow" in str(e).lower() else ""
        raise GitError(f"{e}{hint}") from None
    landed = cust.run("ls-remote", clone_url, f"refs/heads/{branch}").split()
    if not landed or landed[0] != commit:
        raise GitError(f"push of {branch} could not be confirmed; no pull request opened")
    return None


def _best_effort(what: str, call) -> None:
    """For follow-up calls that must not fail the repo (labels, reviewers): log, don't hide."""
    try:
        call()
    except GitHubError as e:
        log.warning("%s failed: %s", what, e)


def _open_pr(
    gh: GitHub,
    full: str,
    owner: str,
    default: str,
    branch: str,
    plan: Plan,
    body: str,
    title: str,
    args,
    extra_labels: tuple = (),
) -> dict:
    payload = {
        "title": title,
        "head": branch,
        "base": default,
        "body": body,
        "draft": plan.blocking,
        "maintainer_can_modify": True,
    }
    pr = create_pr(gh, full, owner, payload)
    issue = f"/repos/{full}/issues/{pr['number']}"
    if plan.blocking and not pr.get("draft"):  # this plan has no draft PRs: make "not ready" explicit
        fixed = {"body": body.replace(DRAFT_NOTE, NOT_DRAFT_NOTE, 1)}
        _best_effort(
            f"{full}: body",
            lambda: gh.req("PATCH", f"/repos/{full}/pulls/{pr['number']}", fixed),
        )
    labels = [SYNC_LABEL, *extra_labels] + (["merge-conflicts"] if plan.changes["conflict"] else [])
    labels += ["needs-work"] if plan.blocking else []
    _best_effort(
        f"{full}: labels",
        lambda: gh.req("POST", f"{issue}/labels", {"labels": labels}, idempotent=True),
    )
    if plan.blocking and args.assign_drafts:
        assignees = {"assignees": args.assign_drafts}
        _best_effort(
            f"{full}: assignees",
            lambda: gh.req("POST", f"{issue}/assignees", assignees, idempotent=True),
        )
    if args.reviewers:
        reviewers = {
            "reviewers": [r for r in args.reviewers if not r.startswith("team:")],
            "team_reviewers": [r[5:] for r in args.reviewers if r.startswith("team:")],
        }
        _best_effort(
            f"{full}: reviewers",
            lambda: gh.req(
                "POST",
                f"/repos/{full}/pulls/{pr['number']}/requested_reviewers",
                reviewers,
                idempotent=True,
            ),
        )
    return pr


def _close_superseded(gh: GitHub, full: str, default: str, open_prs: list, up: Upstream) -> None:
    """Close older sync PRs and delete their branches. Never one a person has committed to,
    and never one for newer upstream code (e.g. a manual run on main, then a release run)."""
    for old in open_prs:
        old_sha = old["head"]["ref"].rsplit("/", 1)[-1]
        if not any(c.startswith(old_sha) for c in up.commit_set):
            log.info(
                "%s: keeping PR #%s, it is for newer upstream code than %s",
                full,
                old["number"],
                up.ref,
            )
            continue
        if not tool_only(gh, full, default, old["head"]["ref"]):
            continue

        def close(old=old):
            gh.req(
                "POST",
                f"/repos/{full}/issues/{old['number']}/labels",
                {"labels": ["superseded"]},
                idempotent=True,
            )
            gh.req("PATCH", f"/repos/{full}/pulls/{old['number']}", {"state": "closed"})
            gh.req(
                "DELETE",
                f"/repos/{full}/git/refs/heads/{urllib.parse.quote(old['head']['ref'])}",
                ok404=True,
            )

        _best_effort(f"{full}: closing superseded PR #{old['number']}", close)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def read_orgs_file(path: str) -> list[str]:
    names = (raw.split("#", 1)[0].strip() for raw in Path(path).read_text(encoding="utf-8").splitlines())
    return [n for n in names if n]


def discover(gh: GitHub, args: argparse.Namespace) -> tuple[list[dict], list[Result]]:
    """The integration repo of every requested org (or of every org the token can see), plus a
    result for each org that has none or could not be read, so no org drops out of the report."""
    orgs = list(dict.fromkeys(args.orgs + (read_orgs_file(args.orgs_file) if args.orgs_file else [])))
    if not orgs:
        orgs = [o["login"] for o in gh.paginate("/user/orgs")]
    bad = [o for o in orgs if not ORG_RE.match(o)]
    if bad:
        log.error("Invalid organization name(s): %s", ", ".join(bad))
        raise SystemExit(2)
    skip = {o.lower() for o in args.exclude_orgs}

    def lookup(org: str) -> dict | Result:
        full = f"{org}/{args.repo_name}"
        try:
            repo = gh.get(f"/repos/{full}", ok404=True)
        except GitHubError as e:
            return Result(full, "error", str(e), effort="manual")
        return repo or Result(full, "skipped", "no such repository, or the token cannot see it")

    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:  # GETs only, rate limited
        found = list(pool.map(lookup, [o for o in orgs if o.lower() not in skip]))
    return [r for r in found if isinstance(r, dict)], [r for r in found if isinstance(r, Result)]


def missing_scopes(header: str | None) -> list[str]:
    """Classic tokens list their scopes in X-OAuth-Scopes; fine-grained tokens send no header."""
    if not header:  # fine-grained tokens and app tokens: checked per repo instead
        return []
    scopes = {x.strip() for x in header.split(",")}
    return [s for s in ("repo", "workflow") if s not in scopes and not (s == "repo" and "public_repo" in scopes)]


def resolve_token(git_host: str) -> str | None:
    for var in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    if shutil.which("gh"):
        with suppress(OSError, subprocess.TimeoutExpired):
            p = subprocess.run(
                ["gh", "auth", "token", "--hostname", git_host],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Open pull requests that sync each org's Veracode integration repo with upstream."
    )
    ap.add_argument(
        "--orgs",
        nargs="+",
        default=[],
        help="organizations to sync (default: every org the token can see)",
    )
    ap.add_argument(
        "--orgs-file",
        help="file with one organization per line, # comments allowed; adds to --orgs",
    )
    ap.add_argument("--exclude-orgs", nargs="+", default=[], help="organizations to skip")
    ap.add_argument(
        "--repo-name",
        default="veracode",
        help="integration repo name (default: veracode)",
    )
    ap.add_argument(
        "--keep",
        nargs="+",
        default=[],
        metavar="GLOB",
        help="extra paths never modified (repo_list.yml always is)",
    )
    ap.add_argument(
        "--adopt-new-defaults",
        action="store_true",
        help=f"apply upstream's new {CONFIG_FILE} defaults (default: keep current values, no behavior change)",
    )
    ap.add_argument(
        "--assign-drafts",
        nargs="+",
        default=[],
        metavar="USER",
        help="assign PRs that need work",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="write the patch and PR text to --out-dir; push nothing",
    )
    ap.add_argument("--out-dir", default="out", help="dry-run output folder (default: out)")
    ap.add_argument("--reviewers", nargs="+", default=[], help="users and/or team:<slug> to request")
    ap.add_argument(
        "--force",
        action="store_true",
        help="open a PR even if this update was declined before",
    )
    ap.add_argument(
        "--revert",
        action="store_true",
        help="undo the last update: close its open PR, or open a PR that reverts the merged one",
    )
    ap.add_argument("--upstream-url", default=DEFAULT_UPSTREAM)
    ap.add_argument(
        "--upstream-ref",
        default=LATEST,
        help=f"upstream tag, commit or branch (default: {LATEST}, the highest vX.Y.Z tag)",
    )
    ap.add_argument(
        "--api-base",
        default=os.environ.get("GITHUB_API_BASE", DEFAULT_API),
        help="GitHub API root (GHE.com: https://api.SUBDOMAIN.ghe.com)",
    )
    ap.add_argument("--branch-prefix", default="veracode-upstream-sync")
    ap.add_argument("--title", default="Veracode integration update")
    ap.add_argument(
        "--workers",
        type=int,
        default=8,
        help=f"repos processed in parallel, 1 to {MAX_WORKERS} (default: 8)",
    )
    ap.add_argument(
        "--git-timeout",
        type=int,
        default=900,
        help="seconds per git operation (default: 900)",
    )
    ap.add_argument("--report", help="write a JSON report to this file")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    if not REPO_RE.match(args.repo_name) or not re.match(r"^[A-Za-z0-9._/-]{1,60}$", args.branch_prefix):
        ap.error("invalid --repo-name or --branch-prefix")
    if args.orgs_file and not Path(args.orgs_file).is_file():
        ap.error(f"--orgs-file not found: {args.orgs_file}")
    if args.git_timeout <= 0:
        ap.error("--git-timeout must be a positive number of seconds")
    if not 1 <= args.workers <= MAX_WORKERS:
        ap.error(f"--workers must be between 1 and {MAX_WORKERS}")
    api = urllib.parse.urlsplit(args.api_base)
    if api.scheme != "https" and not (api.scheme == "http" and api.hostname in ("localhost", "127.0.0.1", "::1")):
        ap.error("--api-base must be an https:// URL (the token is sent to it)")
    args.keep_defaults = not args.adopt_new_defaults
    host = urllib.parse.urlsplit(args.api_base).hostname or ""
    args.git_host = "github.com" if host == "api.github.com" else host.removeprefix("api.")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    Git.timeout = args.git_timeout
    token = resolve_token(args.git_host)
    if not token:
        log.error("No token: set GH_TOKEN or GITHUB_TOKEN, or run `gh auth login`")
        return 2
    gh = GitHub(args.api_base, token)
    try:
        me, headers = gh.req("GET", "/user")
    except GitHubError as e:
        log.error("Token check failed: %s", e)
        return 2
    missing = missing_scopes(headers.get("X-OAuth-Scopes"))
    if missing and not args.dry_run:
        log.error(
            "Token is missing scope(s): %s (needed to push workflow files and open PRs)",
            ", ".join(missing),
        )
        return 2
    author = (
        me.get("name") or me["login"],
        f"{me['id']}+{me['login']}@users.noreply.github.com",
    )

    workdir = Path(tempfile.mkdtemp(prefix="vsync-"))  # 0700, removed on exit
    try:
        try:
            with cf.ThreadPoolExecutor(max_workers=1) as bg:  # clone upstream meanwhile
                cloning = None if args.revert else bg.submit(Upstream, args.upstream_url, args.upstream_ref, workdir)
                repos, unreadable = discover(gh, args)
                up = cloning.result() if cloning else None
        except (GitError, GitHubError) as e:
            log.error("%s", e)
            return 2
        log.info("%s %d repo(s) as %s", "Reverting" if args.revert else "Syncing", len(repos), me["login"])

        def task(repo):
            try:
                if up is None:
                    return revert_repo(gh, repo, args, author, workdir)
                return sync_repo(gh, up, repo, args, author, workdir)
            except (GitError, GitHubError, SafetyError, OSError) as e:
                return Result(repo["full_name"], "error", str(e), effort="manual")
            except Exception as e:  # one repo must never stop the run
                log.exception("[%s] unexpected error", repo["full_name"])
                return Result(
                    repo["full_name"],
                    "error",
                    f"unexpected {type(e).__name__}: {e}",
                    effort="manual",
                )

        with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(task, r) for r in repos]
            try:
                for fut in cf.as_completed(futures):
                    r = fut.result()
                    log.info("[%s] %s %s %s", r.repo, r.status, r.detail, r.pr)
            except KeyboardInterrupt:
                log.warning("Interrupted: finishing repos in progress, skipping the rest")
                for fut in futures:
                    fut.cancel()
        results = unreadable + [f.result() for f in futures if f.done() and not f.cancelled()]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    upstream = {"revert": True} if up is None else {"url": up.url, "ref": up.ref, "commit": up.sha}
    counts = print_results(results, gh, args, upstream)
    return 1 if counts.get("error") else 0


def print_results(results: list[Result], gh: GitHub, args: argparse.Namespace, upstream: dict) -> collections.Counter:
    results.sort(key=lambda r: r.repo.lower())
    width = max([len(r.repo) for r in results] + [10])
    print(f"\n{'REPO':<{width}}  {'STATUS':<12}  {'EFFORT':<7}  DETAIL")
    for r in results:
        extra = f"  ({r.work or r.detail})" if r.pr and (r.work or r.detail) else ""
        print(f"{r.repo:<{width}}  {r.status:<12}  {r.effort or '-':<7}  {r.pr or r.detail}{extra}")
    counts = collections.Counter(r.status for r in results)
    print(
        "\n"
        + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
        + f" | API requests: {gh.limiter.requests}, rate-limit waits: {gh.limiter.waited:.0f}s"
    )
    if args.report:
        report = {
            "upstream": upstream,
            "results": [dataclasses.asdict(r) for r in results],
        }
        try:
            Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
            write_report_csv(results, Path(args.report).with_suffix(".csv"))
        except OSError as e:
            log.error("Could not write --report %s: %s", args.report, e)
    return counts


STATUS_ORDER = ("Ready to merge", "Needs fixes", "No PR: fix by hand", "Nothing to do")
NEXT_STEP = (  # (status, text in the detail) -> (org status, action, details) for results without a plan
    (
        "skipped",
        "no push access",
        2,
        "Give the token push access",
        "Give the token write access to this repo, then re-run",
    ),
    (
        "skipped",
        "declined",
        2,
        "Re-propose the update",
        "It was closed without merging: re-run with --force",
    ),
    ("skipped", "was reverted", 3, "None", "It was reverted: re-run with --force to apply it again"),
    ("skipped", "nothing to revert", 3, "None", "No merged update to revert, or its changes are already undone"),
    ("skipped", "already reverted", 3, "None", "Already reverted"),
    ("reverted", "", 3, "None", "Closed the open update PR, it was not merged"),
    ("dry-run", "would close", 3, "Close the open update PR", "Run again without --dry-run"),
    ("skipped", "archived", 3, "None", "The repo is archived"),
    (
        "skipped",
        "does not look like",
        3,
        "None",
        "This repo is not the Veracode integration",
    ),
    ("skipped", "newer than", 3, "None", "Already on newer upstream code"),
    (
        "skipped",
        "no such repository",
        3,
        "None",
        "No integration repo, or the token cannot see it",
    ),
    ("up-to-date", "", 3, "None", "Already up to date"),
    (
        "error",
        "renamed or removed section",
        2,
        "Update veracode.yml by hand",
        "{sections}",
    ),
    ("error", "safety check failed", 2, "Update by hand", "{reason}"),
    (
        "error",
        "SAML SSO",
        2,
        "Authorize the token",
        "Token settings > Configure SSO > Authorize for this org, then re-run",
    ),
    ("error", "", 2, "Re-run", "If it fails again: {detail}"),
)


def _rows_for(r: Result) -> tuple[int, list[dict]]:
    """(index in STATUS_ORDER, report rows) for one repo, must-fix rows first, merge last."""
    d = r.details if isinstance(r.details, dict) else {}
    items = d.get("items")
    if r.status in ("pr-created", "pr-exists", "dry-run") and isinstance(items, list):
        rows = sorted(items, key=lambda i: not i.get("must_fix"))
        if r.effort == "ready" and r.status == "pr-exists" and "marked ready" in r.work:
            rows = [
                {
                    "action": "Review the PR",
                    "details": "Someone fixed it and marked it ready: check the changes",
                }
            ]
        if r.status == "dry-run":
            rows.append({"action": "Open the PR", "details": "Run again without --dry-run"})
        else:
            rows.append(
                {
                    "action": "Merge the PR",
                    "details": "Then run one scan to confirm it works",
                }
            )
        return (1 if any(i.get("must_fix") for i in rows) else 0), rows
    for status, needle, order, action, details in NEXT_STEP:
        if r.status == status and needle in r.detail:
            sections = ", ".join(sorted(set(re.findall(r"renamed or removed section `([^`]+)`", r.detail))))
            text = details.format(
                sections=f"Move your settings out of {sections} (upstream renamed or removed them), then re-run",
                reason=r.detail.split("nothing pushed: ", 1)[-1],
                detail=r.detail,
            )
            row = {"action": action, "details": text, "must_fix": order == 2}
            return order, [row | ({"where": CONFIG_FILE} if "veracode.yml" in action else {})]
    return 2, [{"action": "Check the error", "details": r.detail, "must_fix": True}]


def _cell(value) -> str:
    """Plain text for a spreadsheet: no backticks, and never read as a formula (text from a
    repo, such as a YAML line starting with `-` or `=`, would otherwise run as one)."""
    text = str(value).replace("`", '"')  # code in quotes stays readable
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _hyperlink(url: str, label: str) -> str:
    return f'=HYPERLINK("{url.replace(chr(34), "%22")}","{label}")' if url else ""


def write_report_csv(results: list[Result], path: Path) -> None:
    """The report: one row per action, orgs with the least to fix first. Each row says what to
    do, where, and links straight to the line or settings page. Saved with a BOM for Excel."""
    orgs = []
    for r in results:
        order, rows = _rows_for(r)
        fixes = sum(1 for row in rows if row.get("must_fix"))
        orgs.append((order, fixes, r, rows))
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "org",
                "status",
                "fixes_before_merge",
                "must_fix",
                "action",
                "where",
                "details",
                "pr",
            ]
        )
        for order, fixes, r, rows in sorted(orgs, key=lambda x: (x[0], x[1], x[2].repo.lower())):
            pr = _hyperlink(r.pr, f"PR #{r.pr.rstrip('/').rsplit('/', 1)[-1]}") if r.pr else ""
            for row in rows:
                w.writerow(
                    [
                        r.repo.split("/")[0],
                        STATUS_ORDER[order],
                        fixes,
                        "yes" if row.get("must_fix") else "no",
                        _cell(row.get("action", "")),
                        _cell(row.get("where", "")),
                        _cell(row.get("details", "")),
                        pr,
                    ]
                )
    log.info("Report: %s", path)


if __name__ == "__main__":
    sys.exit(main())
