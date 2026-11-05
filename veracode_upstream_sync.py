#!/usr/bin/env python3
"""
Keep each organization's Veracode Workflow Integration repo ("veracode") in
sync with the upstream template, github.com/veracode/github-actions-integration,
through reviewable pull requests.

  * Never pushes to the default branch: every change is a pull request.
  * 3-way merge (base / customer / upstream) keeps customer customizations:
    custom files, custom workflow steps and veracode.yml settings.
  * repo_list.yml and --keep paths are never modified.
  * Anything that cannot be merged safely ends up in a draft PR with a short To do list.
  * Every GitHub call goes through one shared rate limiter.

Naming: O / B / T are ours (customer), base and theirs (upstream) in a 3-way merge.
"""

from __future__ import annotations

import argparse
import base64
import collections
import concurrent.futures as cf
import dataclasses
import datetime as dt
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

__version__ = "3.1.0"

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
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".gz", ".tgz", ".jar", ".exe", ".pdf"}
BLOB_LIMIT = "blob:limit=2m"  # partial clones: skip the large CLI binaries in history
SYNC_LABEL = "veracode-upstream-sync"

log = logging.getLogger("vsync")


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

    def req(self, method: str, path: str, body=None, ok404: bool = False, idempotent: bool | None = None):
        """Rate-limit rejections are always retried (GitHub did not process
        them). 5xx and network errors are retried only for idempotent calls;
        a POST raises GitHubError(maybe_applied=True) so the caller can check."""
        idempotent = method != "POST" if idempotent is None else idempotent
        url = path if path.startswith("http") else self.api + path
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
                    raise GitHubError(status, f"still rate limited after {limited} attempts on {method} {path}")
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
            msg = json.loads(text).get("message", text)
        except ValueError:
            msg = text[:300]
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

    def run(self, *args, input: bytes | None = None, raw: bool = False, env: dict | None = None):
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

    def ls_tree(self, rev: str) -> dict[str, tuple[str, str]]:
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
    if branch:
        args += ["--single-branch", "--branch", branch]
    try:
        p = subprocess.run([*args, url, str(dest)], capture_output=True, env=env, timeout=Git.timeout, check=False)
    except subprocess.TimeoutExpired:
        raise GitError(f"clone of {url} timed out after {Git.timeout}s") from None
    if p.returncode != 0:
        raise GitError(f"clone failed for {url}: {p.stderr.decode(errors='replace').strip()[-500:]}")
    return Git(dest, env)


class Upstream:
    def __init__(self, url: str, ref: str, workdir: Path):
        self.url, self.ref = url, ref
        self.git = clone(url, workdir / "upstream.git", git_env())
        self.sha = self.git.rev(ref)
        self.commits = self.git.run("rev-list", "--topo-order", self.sha).split()
        self.commit_set = set(self.commits)
        self.tree = self.git.ls_tree(self.sha)
        if CONFIG_FILE not in self.tree or not any(is_workflow(p) for p in self.tree):
            raise GitError(
                f"{url} at {ref} does not look like the Veracode integration (no {CONFIG_FILE} or workflows)"
            )
        self._pairs: dict[str, frozenset] = {}
        self._lock = threading.Lock()  # lazy blob fetches into one repo are serialized
        log.info("Upstream %s at %s", url, self.sha[:10])

    def blob(self, sha: str) -> bytes:
        with self._lock:
            return self.git.blob(sha)

    def text(self, entry) -> str:
        return self.blob(entry[1]).decode(errors="replace") if entry else ""

    def tree_at(self, commit: str) -> dict:
        with self._lock:
            return self.git.ls_tree(commit)

    def pairs_at(self, commit: str) -> frozenset:
        with self._lock:
            if commit not in self._pairs:
                self._pairs[commit] = frozenset((p, s) for p, (_, s) in self.git.ls_tree(commit).items())
            return self._pairs[commit]

    def web_url(self) -> str | None:
        m = re.match(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", self.url)
        return f"https://github.com/{m.group(1)}" if m else None


# --------------------------------------------------------------------------- #
# YAML helpers (strict parsing; comment-preserving text edits)
# --------------------------------------------------------------------------- #
class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys (plain PyYAML silently keeps the last)."""


def _mapping_without_duplicates(loader, node, deep=False):
    seen: dict = {}
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=True)
        with suppress(TypeError):  # unhashable keys are reported by PyYAML itself
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r} (lines {seen[key] + 1} and {key_node.start_mark.line + 1})"
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
        child = next((_ind(ml[j]) for j in range(ps + 1, pe) if _content(ml[j])), _ind(ml[ps]) + 2)
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
    """Put the base (old default) value of `path` back, touching nothing else."""
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
    rep: dict = {"added": [], "missing": [], "defaults": [], "kept": []}
    M, T, B, O = (safe_yaml(x)[0] for x in (merged, theirs, base, ours))
    if not all(isinstance(x, dict) for x in (M, T)):
        return merged, rep
    B = B if isinstance(B, dict) else {}
    for path in flatten(T):  # new upstream settings (not in base) the merge did not carry
        if _get(M, path) is not _MISSING or _get(B, path) is not _MISSING:
            continue
        top = next(path[:n] for n in range(1, len(path) + 1) if _get(M, path[:n]) is _MISSING)
        if _get(B, top) is _MISSING and (new := insert_setting(merged, theirs, top)):
            merged, M = new, safe_yaml(new)[0]
    fo, fb, ft = flatten(O if isinstance(O, dict) else {}), flatten(B), flatten(T)
    for path, bv in fb.items():  # upstream changed a default the customer still had
        tv = ft.get(path, _MISSING)
        if tv is _MISSING or tv == bv or fo.get(path, _MISSING) != bv or _get(M, path) != tv:
            continue
        key = ".".join(map(str, path))
        if keep_defaults and (new := restore_setting(merged, base, path)):
            merged, M = new, safe_yaml(new)[0]
            rep["kept"].append(f"`{key}`: {_fmt(bv)} (upstream default is now {_fmt(tv)})")
        else:
            rep["defaults"].append((key, f"{_fmt(bv)} to {_fmt(tv)}"))
    fm = flatten(M)
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
    """(job, occurrence key, step) for steps the customer added."""
    out = []
    for job in (o_doc.get("jobs") or {}) if isinstance(o_doc, dict) else {}:
        so = _job_steps(o_doc, job)
        if so is not None:
            base_keys = set(_occ_keys(_job_steps(b_doc, job) or []))
            out += [(job, k, st) for k, st in zip(_occ_keys(so), so) if k not in base_keys]
    return out


def step_level_merge(o: str, b: str, t: str, labels) -> str | None:
    """Resolve a workflow conflict caused by customer-added steps: take them out,
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
            m = next((n for n, bk in enumerate(base_keys) if bk[0] == k and n not in used), None)
            if m is not None:
                used.add(m)
                if group:
                    group["next"] = base_keys[m]
                prev, group = base_keys[m], None
                continue
            removals.append(blocks[idx])
            if group is None:
                group = {"job": job, "prev": prev, "next": None, "chunk": [], "ind": ind, "keys": []}
                groups.append(group)
            group["chunk"] += lines[blocks[idx][0] : blocks[idx][1]]
            group["keys"].append(k)
    if not groups:
        return None
    for s, e in sorted(removals, reverse=True):
        del lines[s:e]
    merged, conflicts = merge_text("".join(lines), b, t, labels)
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
    final: dict
    changes: dict = dataclasses.field(default_factory=lambda: collections.defaultdict(list))  # kind -> paths
    todo: list = dataclasses.field(default_factory=list)  # (blocking, text)
    config: dict = dataclasses.field(default_factory=dict)
    cli: str = ""
    custom_files: list = dataclasses.field(default_factory=list)
    custom_steps: list = dataclasses.field(default_factory=list)  # (file, job, label)
    protected: list = dataclasses.field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return any(b for b, _ in self.todo)


def merge_text(ours: str, base: str, theirs: str, labels, keep: str = "") -> tuple[str, int]:
    """3-way merge with `git merge-file`. keep='ours' resolves conflicting hunks to the customer's side."""
    with tempfile.TemporaryDirectory() as td:
        files = []
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            (Path(td) / name).write_text(text)
            files.append(str(Path(td) / name))
        cmd = ["git", "merge-file", "-p", "--diff3", "-L", labels[0], "-L", labels[1], "-L", labels[2], *files]
        p = subprocess.run(cmd, capture_output=True, check=False)
        if not 0 <= p.returncode < 128:
            raise GitError(f"merge-file failed: {p.stderr.decode(errors='replace')}")
        if p.returncode and keep:
            resolved = subprocess.run([*cmd[:3], f"--{keep}", *cmd[4:]], capture_output=True, check=False)
            return resolved.stdout.decode(errors="replace"), p.returncode
        return p.stdout.decode(errors="replace"), p.returncode


def find_base(cust: Git, head: str, ours: dict, up: Upstream) -> tuple[str | None, str]:
    """The upstream commit the customer repo was last aligned with."""
    if MARKER in ours:
        with suppress(ValueError, GitError, AttributeError):
            commit = json.loads(cust.blob(ours[MARKER][1])).get("commit", "")
            if commit in up.commit_set:
                return commit, "last sync"
    for commit in cust.run("rev-list", "--max-count=20000", head).split():
        if commit in up.commit_set:
            return commit, "shared history"
    # Copied rather than imported: best-matching upstream commit, the oldest on a tie
    # (a too-new base would make upstream changes look like customer reverts).
    pairs = {(p, s) for p, (_, s) in ours.items() if not p.startswith(CLI_DIR) and p != MARKER}
    best, best_commit = 0, None
    for commit in reversed(up.commits):
        score = len(pairs & up.pairs_at(commit))
        if score > best:
            best, best_commit = score, commit
    return (best_commit, "inferred") if best_commit else (None, "none")


def cli_version(paths) -> tuple:
    found = [tuple(int(x) for x in m.group(1).split(".")) for p in paths if (m := CLI_RE.search(p))]
    return max(found) if found else ()


def is_workflow(path: str) -> bool:
    return path.startswith(WORKFLOW_DIR) and path.endswith((".yml", ".yaml"))


def merge_repo(cust: Git, up: Upstream, ours: dict, base_commit: str | None, base_how: str, args) -> Plan:
    base = up.tree_at(base_commit) if base_commit else {}
    theirs = up.tree
    final = {p: e for p, e in ours.items() if p != MARKER}
    plan = Plan(base_commit, base_how, final)
    labels = ("your version", "previous upstream", "new upstream")

    def kept(path):
        return any(fnmatch.fnmatch(path, g) for g in (*ALWAYS_KEEP, *args.keep))

    def take_upstream(path):
        final[path] = (theirs[path][0], cust.write_blob(up.blob(theirs[path][1])))

    # Veracode CLI binaries: the newer set wins.
    o_cli = {p for p in ours if p.startswith(CLI_DIR) and CLI_RE.search(p)}
    t_cli = {p for p in theirs if p.startswith(CLI_DIR) and CLI_RE.search(p)}
    ov, tv = cli_version(o_cli), cli_version(t_cli)
    if tv > ov:
        for p in o_cli - t_cli:
            del final[p]
        for p in t_cli:
            if ours.get(p, (0, 0))[1] != theirs[p][1]:
                take_upstream(p)
        plan.cli = f"Veracode CLI {'.'.join(map(str, ov)) or 'none'} to {'.'.join(map(str, tv))}"

    for path in sorted((set(ours) | set(theirs) | set(base)) - o_cli - t_cli - {MARKER}):
        O, B, T = ours.get(path), base.get(path), theirs.get(path)
        o, b, t = (e[1] if e else None for e in (O, B, T))
        if o == t or b == t:  # nothing to do, or upstream did not change it
            continue
        if kept(path):
            plan.changes["protected-changed"].append(path)
            continue
        if b is not None and o == b and not (path == CONFIG_FILE and args.keep_defaults):
            if T:  # the customer never touched it: take upstream as is
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
            take_upstream(path)  # binaries cannot be merged: upstream wins, the customer is told
            plan.changes["updated"].append(path)
            plan.todo.append((False, f"Upstream replaced `{path}`, which you had changed. Restore yours if needed"))
            continue
        merged, conflicts = merge_text(o_text, b_text, t_text, labels)
        if conflicts and is_workflow(path) and (resolved := step_level_merge(o_text, b_text, t_text, labels)):
            merged, conflicts = resolved, 0
        if path == CONFIG_FILE:
            if conflicts:  # the customer's settings win; fall back to markers only if that breaks the file
                ours_wins = merge_text(o_text, b_text, t_text, labels, keep="ours")[0]
                if not safe_yaml(ours_wins)[1]:
                    merged, conflicts = ours_wins, 0
            if not conflicts:
                merged, plan.config = merge_config(merged, b_text, t_text, o_text, args.keep_defaults)
        if merged == o_text:
            continue
        final[path] = (O[0], cust.write_blob(merged.encode()))
        if conflicts:
            lines = [n for n, ln in enumerate(merged.splitlines(), 1) if ln.startswith("<<<<<<< ")]
            where = f" (line{'s' if len(lines) > 1 else ''} {', '.join(map(str, lines))})" if lines else ""
            count = "the conflict" if len(lines) <= 1 else f"{len(lines)} conflicts"
            plan.todo.append((True, f"Resolve {count} in `{path}`{where}"))
            plan.changes["conflict"].append(path)
        else:
            plan.changes["merged"].append(path)

    _check(plan, cust, up, ours, base)
    plan.custom_files = sorted(p for p in ours if p not in base and p not in theirs and p != MARKER and p not in o_cli)
    plan.protected = sorted(p for p in ours if kept(p))
    verify(plan, cust, up, ours, base)
    return plan


def _check(plan: Plan, cust: Git, up: Upstream, ours: dict, base: dict) -> None:
    """Validate every changed YAML file and confirm customer steps survived."""
    final, conflicted = plan.final, set(plan.changes["conflict"])
    used_before = {
        u.split("@")[0].lower() for p, e in ours.items() if is_workflow(p) for u in USES_RE.findall(cust.text(e))
    }
    new_actions: set[str] = set()
    for path in sorted(p for p, e in final.items() if ours.get(p) != e and p.endswith((".yml", ".yaml"))):
        text = cust.text(final[path])
        if path not in conflicted:
            err = safe_yaml(text)[1]
            if err:
                before = safe_yaml(cust.text(ours.get(path)))[1] if path in ours else None
                dup = re.search(r"duplicate key '([^']+)' \(lines (\d+) and (\d+)\)", err)
                if before:
                    plan.todo.append((False, f"Fix `{path}`: it was already invalid before this update ({before})"))
                elif dup:
                    plan.todo.append(
                        (True, f"Keep only one `{dup[1]}` setting in `{path}` (lines {dup[2]} and {dup[3]})")
                    )
                else:
                    plan.todo.append((True, f"Fix invalid YAML in `{path}`: {err}"))
        if is_workflow(path):
            for u in USES_RE.findall(text):
                name = u.split("@")[0]
                if not u.startswith("./") and name.lower() not in used_before:
                    new_actions.add(name)
    for name in sorted(new_actions):
        plan.todo.append((False, f"If your organization restricts GitHub Actions, allow `{name}`"))
    for key, change in plan.config.get("defaults", []):
        plan.todo.append((False, f"Confirm the new default for `{key}` in `{CONFIG_FILE}` ({change})"))
    for key in plan.config.get("missing", []):
        plan.todo.append((False, f"Add the new setting `{key}` to `{CONFIG_FILE}` if you need it"))
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
            plan.todo.append(
                (True, f"Check your step `{label}` in `{path}`: upstream reshaped job `{job}` and the step {where}")
            )
        if F is not None and path in up.tree and not any(f"`{path}`" in t for b, t in plan.todo if b):
            for what, why in reshaped_edits(O, B, safe_yaml(up.text(up.tree[path]))[0]):
                plan.todo.append((False, f"Check {what} in `{path}`: {why}"))


def _jobs(doc) -> dict:
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    return {j: s for j in jobs if (s := _job_steps(doc, j)) is not None} if isinstance(jobs, dict) else {}


def _loose_keys(steps) -> list[tuple]:
    """Occurrence keys that ignore case and spacing, so `set version` to `Set version` is no change."""
    return [((kind, " ".join(str(val).lower().split())), n) for (kind, val), n in _occ_keys(steps)]


def reshaped_edits(O, B, T) -> list[tuple[str, str]]:
    """Customer edits next to steps that upstream renamed, removed or rearranged. The
    merge keeps them, but where they land is a judgement call a person should confirm."""
    out = []
    b_jobs, t_jobs = _jobs(B), _jobs(T)
    for job, so in _jobs(O).items():
        sb, st = b_jobs.get(job), t_jobs.get(job)
        if sb is None or so == sb or st == sb:  # custom job, untouched by the customer, or by upstream
            continue
        if st is None:
            out.append((f"your changes to job `{job}`", "upstream renamed or removed this job"))
            continue
        okeys, bkeys, tkeys = (_loose_keys(x) for x in (so, sb, st))
        for i, (k, step) in enumerate(zip(okeys, so)):
            if k in bkeys:
                if step != sb[bkeys.index(k)] and k not in tkeys:
                    out.append((f"your change to step `{_label(step, k)}`", "upstream removed or renamed that step"))
                continue
            prev = next((x for x in reversed(okeys[:i]) if x in bkeys), None)
            nxt = next((x for x in okeys[i + 1 :] if x in bkeys), None)
            moved = any(x is not None and x not in tkeys for x in (prev, nxt)) or (
                prev in tkeys
                and nxt in tkeys
                and tkeys.index(nxt) - tkeys.index(prev) != bkeys.index(nxt) - bkeys.index(prev)
            )
            if moved:
                out.append((f"where your step `{_label(step, k)}` landed", "upstream changed the steps around it"))
    return out


class SafetyError(RuntimeError):
    pass


def verify(plan: Plan, cust: Git, up: Upstream, ours: dict, base: dict) -> None:
    """Last check before anything is pushed. These must hold whatever the merge did;
    if one fails it is a bug, so the repo gets no pull request."""
    final, conflicted, bad = plan.final, set(plan.changes["conflict"]), []
    for p in plan.protected + plan.custom_files:
        if final.get(p) != ours[p]:
            bad.append(f"`{p}` would change")
    for p, e in ours.items():
        if p not in final and p != MARKER and not CLI_RE.search(p) and e != base.get(p):
            bad.append(f"`{p}` would be deleted although you changed it")
    marker = re.compile(r"^(<<<<<<<|>>>>>>>) ", re.MULTILINE)
    for p, e in final.items():
        if e == ours.get(p) or p in conflicted or p.startswith(CLI_DIR) or Path(p).suffix.lower() in BINARY_EXT:
            continue
        if marker.search(cust.text(e)) and not marker.search(cust.text(ours.get(p)) + up.text(up.tree.get(p))):
            bad.append(f"`{p}` would contain conflict markers")
    if CONFIG_FILE in ours and CONFIG_FILE in final and CONFIG_FILE not in conflicted:
        O, F = safe_yaml(cust.text(ours[CONFIG_FILE]))[0], safe_yaml(cust.text(final[CONFIG_FILE]))[0]
        B = safe_yaml(up.text(base.get(CONFIG_FILE)))[0]
        if isinstance(O, dict) and isinstance(F, dict):
            fb, ff = flatten(B if isinstance(B, dict) else {}), flatten(F)
            bad += [
                f"`{CONFIG_FILE}` would lose your value for `{'.'.join(map(str, k))}`"
                for k, val in flatten(O).items()
                if fb.get(k, _MISSING) != val and ff.get(k, _MISSING) != val
            ]
    if bad:
        raise SafetyError("safety check failed, nothing pushed: " + "; ".join(bad[:5]))


# --------------------------------------------------------------------------- #
# Pull request
# --------------------------------------------------------------------------- #
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
        status = f"**Action required:** {' and '.join(parts)}." + (
            " This pull request is a draft until then." if plan.blocking else ""
        )
    elif ch["merged"]:
        status = "**No action needed.** Your customizations are kept. Review and merge when ready."
    else:
        status = "**No action needed.** This update only changes files you have not customized."
    lines = [status, "", "### To do", ""]
    lines += [f"- [ ] {text}" for _, text in sorted(plan.todo, key=lambda x: not x[0])]
    lines += ["- [ ] Merge this pull request", "- [ ] Run one scan after merging to confirm the checks pass", ""]
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
        ("Custom steps", [f"`{lbl}` in `{p.rsplit('/', 1)[-1]}`, job `{job}`" for p, job, lbl in plan.custom_steps]),
        (f"`{CONFIG_FILE}` settings", cfg_kept),
        ("Customized files updated with upstream changes", [f"`{p}`" for p in ch["merged"]]),
        ("Deleted by you, not restored", [f"`{p}`" for p in ch["deleted-by-you"]]),
        ("Removed upstream, kept because you customized them", [f"`{p}`" for p in ch["customized-removed-upstream"]]),
        ("Protected, upstream changes not applied", [f"`{p}`" for p in ch["protected-changed"]]),
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
            plan.cli,
            ", ".join(counts),
            ch["merged"] and f"{_n(len(ch['merged']), 'customized file')} updated",
            plan.config.get("added") and _n(len(plan.config["added"]), f"new `{CONFIG_FILE}` setting"),
        )
        if x
    ]
    lines += ["### What this update includes", "", (". ".join(summary_bits) or "Sync metadata only") + ".", ""]
    rows = []
    for k in ("updated", "added", "removed"):
        if ch[k]:
            rows += [f"**{k.capitalize()}**", *(f"- `{p}`" for p in ch[k]), ""]
    if plan.config.get("added"):
        rows += [f"**New `{CONFIG_FILE}` settings**", *(f"- `{x}`" for x in plan.config["added"]), ""]
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
    for attempt in range(3):
        try:
            return gh.req("POST", f"/repos/{full}/pulls", payload)[0]
        except GitHubError as e:
            if e.maybe_applied:
                found = gh.get(f"/repos/{full}/pulls?state=open&head={head}") or []
                if found:
                    return found[0]
                gh.limiter.sleep(2 ** (attempt + 1))
                continue
            if e.status == 422 and "already exists" in str(e).lower():  # e.g. a concurrent run
                found = gh.get(f"/repos/{full}/pulls?state=open&head={head}") or []
                if found:
                    return found[0]
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


def sync_repo(gh: GitHub, up: Upstream, repo: dict, args, author, workdir: Path) -> Result:
    full, default = repo["full_name"], repo["default_branch"]
    owner, name = full.split("/")
    branch = f"{args.branch_prefix}/{up.sha[:10]}"
    if branch == default:
        return Result(full, "error", "refusing to use the default branch")
    if repo.get("archived"):
        return Result(full, "skipped", "archived")
    if not args.dry_run and not (repo.get("permissions") or {}).get("push"):
        return Result(full, "skipped", "token has no push access")
    clone_url = repo["clone_url"]
    if clone_url.startswith("https://") and urllib.parse.urlsplit(clone_url).netloc != args.git_host:
        return Result(full, "error", f"clone URL host is not {args.git_host}; refusing to send the token there")

    ours_prs = [
        p
        for p in gh.paginate(f"/repos/{full}/pulls?state=open")
        if p["head"]["ref"].startswith(args.branch_prefix + "/")
        and (p["head"].get("repo") or {}).get("full_name") == full
    ]
    if not args.dry_run:
        same = [p for p in ours_prs if p["head"]["ref"] == branch]
        if same:
            return Result(full, "pr-exists", "already open for this upstream commit", same[0]["html_url"])
        if not args.force:
            q = urllib.parse.quote(f"{owner}:{branch}", safe=":")
            declined = [
                p
                for p in gh.get(f"/repos/{full}/pulls?state=closed&head={q}") or []
                if not p.get("merged_at") and "superseded" not in {lb["name"] for lb in p.get("labels", [])}
            ]
            if declined:
                return Result(full, "skipped", "this update was declined before (use --force)", declined[0]["html_url"])

    dest = workdir / f"{owner}__{name}.git"
    cust = clone(clone_url, dest, git_env(gh.token, clone_url), branch=default)
    try:
        head = cust.rev("HEAD")
        ours = cust.ls_tree(head)
        base_commit, how = find_base(cust, head, ours, up)
        if CONFIG_FILE not in ours or base_commit is None:
            return Result(full, "skipped", f"does not look like the Veracode integration (no {CONFIG_FILE})")
        plan = merge_repo(cust, up, ours, base_commit, how, args)
        if {p: e for p, e in plan.final.items() if p != MARKER} == {p: e for p, e in ours.items() if p != MARKER}:
            return Result(full, "up-to-date", f"base: {how}")
        marker = {
            "upstream": up.url,
            "ref": up.ref,
            "commit": up.sha,
            "synced_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        plan.final[MARKER] = ("100644", cust.write_blob((json.dumps(marker, indent=2) + "\n").encode()))
        body = pr_body(plan, up)
        state = "needs work" if plan.blocking else ("check" if plan.todo else "ready")
        if args.dry_run:
            out = Path(args.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            commit = cust.commit(plan.final, head, "dry run", author)
            (out / f"{owner}__{name}.patch").write_text(
                cust.run("diff", head, commit, "--", ".", f":(exclude){CLI_DIR}")
            )
            (out / f"{owner}__{name}.md").write_text(body)
            return Result(full, "dry-run", f"{state}, see {out}/{owner}__{name}.md")

        message = f"Sync Veracode integration with upstream {up.sha[:10]}\n\n"
        message += f"Upstream: {up.url} @ {up.sha}\n\n{TRAILER}: {up.sha}\n"
        commit = cust.commit(plan.final, head, message, author)
        push = ["push", "--quiet", clone_url, f"{commit}:refs/heads/{branch}"]
        remote = cust.run("ls-remote", clone_url, f"refs/heads/{branch}").split()
        if remote:  # our branch exists with no open PR: overwrite only if no human committed to it
            if not tool_only(gh, full, default, branch):
                return Result(
                    full, "error", f"branch {branch} has commits from someone else; delete it or use --branch-prefix"
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

        title = f"{args.title}{': action required' if plan.todo else ''} ({up.sha[:7]})"
        pr = create_pr(
            gh,
            full,
            owner,
            {
                "title": title,
                "head": branch,
                "base": default,
                "body": body,
                "draft": plan.blocking,
                "maintainer_can_modify": True,
            },
        )
        labels = [SYNC_LABEL] + (["merge-conflicts"] if plan.changes["conflict"] else [])
        with suppress(GitHubError):
            gh.req("POST", f"/repos/{full}/issues/{pr['number']}/labels", {"labels": labels}, idempotent=True)
        if args.reviewers:
            with suppress(GitHubError):
                gh.req(
                    "POST",
                    f"/repos/{full}/pulls/{pr['number']}/requested_reviewers",
                    {
                        "reviewers": [r for r in args.reviewers if not r.startswith("team:")],
                        "team_reviewers": [r[5:] for r in args.reviewers if r.startswith("team:")],
                    },
                    idempotent=True,
                )
        for old in ours_prs:  # close older sync PRs, but never one a person has committed to
            if not tool_only(gh, full, default, old["head"]["ref"]):
                continue
            with suppress(GitHubError):
                gh.req(
                    "POST", f"/repos/{full}/issues/{old['number']}/labels", {"labels": ["superseded"]}, idempotent=True
                )
                gh.req("PATCH", f"/repos/{full}/pulls/{old['number']}", {"state": "closed"})
                gh.req("DELETE", f"/repos/{full}/git/refs/heads/{urllib.parse.quote(old['head']['ref'])}", ok404=True)
        return Result(full, "pr-created", state, pr["html_url"])
    finally:
        shutil.rmtree(dest, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def read_orgs_file(path: str) -> list[str]:
    names = (raw.split("#", 1)[0].strip() for raw in Path(path).read_text().splitlines())
    return [n for n in names if n]


def discover(gh: GitHub, args) -> list[dict]:
    orgs = list(dict.fromkeys(args.orgs + (read_orgs_file(args.orgs_file) if args.orgs_file else [])))
    if not orgs:
        orgs = [o["login"] for o in gh.paginate("/user/orgs")]
    bad = [o for o in orgs if not ORG_RE.match(o)]
    if bad:
        raise SystemExit(f"invalid organization name(s): {', '.join(bad)}")
    skip = {o.lower() for o in args.exclude_orgs}
    repos = []
    for org in orgs:
        if org.lower() in skip:
            continue
        try:
            repo = gh.get(f"/repos/{org}/{args.repo_name}", ok404=True)
        except GitHubError as e:
            log.warning("%s: %s", org, e)
            continue
        if repo:
            repos.append(repo)
        else:
            log.info("%s: no '%s' repository (or no access)", org, args.repo_name)
    return repos


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
                ["gh", "auth", "token", "--hostname", git_host], capture_output=True, text=True, timeout=15, check=False
            )
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
    return None


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Open pull requests that sync each org's Veracode integration repo with upstream."
    )
    ap.add_argument(
        "--orgs", nargs="+", default=[], help="organizations to sync (default: every org the token can see)"
    )
    ap.add_argument("--orgs-file", help="file with one organization per line, # comments allowed; adds to --orgs")
    ap.add_argument("--exclude-orgs", nargs="+", default=[], help="organizations to skip")
    ap.add_argument("--repo-name", default="veracode", help="integration repo name (default: veracode)")
    ap.add_argument(
        "--keep", nargs="+", default=[], metavar="GLOB", help="extra paths never modified (repo_list.yml always is)"
    )
    ap.add_argument(
        "--keep-defaults",
        action="store_true",
        help=f"keep current {CONFIG_FILE} values when upstream changes a default",
    )
    ap.add_argument("--dry-run", action="store_true", help="write the patch and PR text to --out-dir; push nothing")
    ap.add_argument("--out-dir", default="out", help="dry-run output folder (default: out)")
    ap.add_argument("--reviewers", nargs="+", default=[], help="users and/or team:<slug> to request")
    ap.add_argument("--force", action="store_true", help="open a PR even if this update was declined before")
    ap.add_argument("--upstream-url", default=DEFAULT_UPSTREAM)
    ap.add_argument("--upstream-ref", default="main", help="upstream branch, tag or commit (default: main)")
    ap.add_argument(
        "--api-base",
        default=os.environ.get("GITHUB_API_BASE", DEFAULT_API),
        help="GitHub API root (GHE.com: https://api.SUBDOMAIN.ghe.com)",
    )
    ap.add_argument("--branch-prefix", default="veracode-upstream-sync")
    ap.add_argument("--title", default="Veracode integration update")
    ap.add_argument("--workers", type=int, default=4, help="repos processed in parallel (default: 4)")
    ap.add_argument("--git-timeout", type=int, default=900, help="seconds per git operation (default: 900)")
    ap.add_argument("--report", help="write a JSON report to this file")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    if not REPO_RE.match(args.repo_name) or not re.match(r"^[A-Za-z0-9._/-]{1,60}$", args.branch_prefix):
        ap.error("invalid --repo-name or --branch-prefix")
    host = urllib.parse.urlsplit(args.api_base).hostname or ""
    args.git_host = "github.com" if host == "api.github.com" else host.removeprefix("api.")
    return args


def main(argv=None) -> int:
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
        log.error("Token is missing scope(s): %s (needed to push workflow files and open PRs)", ", ".join(missing))
        return 2
    author = (me.get("name") or me["login"], f"{me['id']}+{me['login']}@users.noreply.github.com")

    workdir = Path(tempfile.mkdtemp(prefix="vsync-"))  # 0700, removed on exit
    try:
        try:
            up = Upstream(args.upstream_url, args.upstream_ref, workdir)
            repos = discover(gh, args)
        except (GitError, GitHubError) as e:
            log.error("%s", e)
            return 2
        log.info("Syncing %d repo(s) as %s", len(repos), me["login"])

        def task(repo):
            try:
                return sync_repo(gh, up, repo, args, author, workdir)
            except (GitError, GitHubError, SafetyError, OSError) as e:
                return Result(repo["full_name"], "error", str(e))
            except Exception as e:  # one repo must never stop the run
                log.exception("[%s] unexpected error", repo["full_name"])
                return Result(repo["full_name"], "error", f"unexpected {type(e).__name__}: {e}")

        with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = [pool.submit(task, r) for r in repos]
            try:
                for fut in cf.as_completed(futures):
                    r = fut.result()
                    log.info("[%s] %s %s %s", r.repo, r.status, r.detail, r.pr)
            except KeyboardInterrupt:
                log.warning("Interrupted: finishing repos in progress, skipping the rest")
                for fut in futures:
                    fut.cancel()
        results = [f.result() for f in futures if f.done() and not f.cancelled()]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    results.sort(key=lambda r: r.repo.lower())
    width = max([len(r.repo) for r in results] + [10])
    print(f"\n{'REPO':<{width}}  {'STATUS':<11}  DETAIL")
    for r in results:
        print(
            f"{r.repo:<{width}}  {r.status:<11}  {r.pr or r.detail}" + (f"  ({r.detail})" if r.pr and r.detail else "")
        )
    counts = collections.Counter(r.status for r in results)
    print(
        "\n"
        + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
        + f" | API requests: {gh.limiter.requests}, rate-limit waits: {gh.limiter.waited:.0f}s"
    )
    if args.report:
        Path(args.report).write_text(
            json.dumps(
                {"upstream": {"url": up.url, "commit": up.sha}, "results": [dataclasses.asdict(r) for r in results]},
                indent=2,
            )
        )
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
