import http.server
import io
import json
import threading
import urllib.error

import pytest

import veracode_upstream_sync as vus


class _Hdrs(dict):
    def get(self, k, d=None):
        for kk, v in self.items():
            if kk.lower() == k.lower():
                return v
        return d


def _http_error(code, body):
    return urllib.error.HTTPError("https://api.github.com/x", code, "err", _Hdrs(), io.BytesIO(json.dumps(body).encode()))


class _Resp:
    def __init__(self, body, status=200):
        self.status, self.headers, self._raw = status, _Hdrs(), json.dumps(body).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _gh():
    return vus.GitHub("https://api.github.com", "t", vus.RateLimiter(sleep=lambda s: None))


def _fake_open(monkeypatch, handler):
    monkeypatch.setattr(vus.GitHub, "_urlopen", lambda self, req: handler(req))


# --- error messages -------------------------------------------------------- #
def test_message_includes_validation_errors():
    text = json.dumps(
        {
            "message": "Validation Failed",
            "errors": [
                {"resource": "PullRequest", "code": "custom", "message": "Draft pull requests are not supported."},
                {"resource": "Issue", "field": "title", "code": "missing_field"},
                "plain string error",
            ],
        }
    )
    msg = vus.GitHub._message(text, _Hdrs())
    assert msg.startswith("Validation Failed")
    assert "Draft pull requests are not supported." in msg
    assert "title" in msg and "missing_field" in msg
    assert "plain string error" in msg


def test_create_pr_retries_without_draft(monkeypatch):
    calls = []

    def handler(req):
        payload = json.loads(req.data)
        calls.append(payload["draft"])
        if payload["draft"]:
            raise _http_error(
                422,
                {
                    "message": "Validation Failed",
                    "errors": [
                        {
                            "resource": "PullRequest",
                            "code": "custom",
                            "message": "Draft pull requests are not supported in this repository.",
                        }
                    ],
                },
            )
        return _Resp({"number": 7, "draft": False}, 201)

    _fake_open(monkeypatch, handler)
    pr = vus.create_pr(_gh(), "o/veracode", "o", {"head": "b", "base": "main", "title": "t", "body": "x", "draft": True})
    assert pr["number"] == 7
    assert calls == [True, False]


def test_create_pr_returns_existing_pr(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req.get_method())
        if req.get_method() == "POST":
            raise _http_error(
                422,
                {
                    "message": "Validation Failed",
                    "errors": [
                        {"resource": "PullRequest", "code": "custom", "message": "A pull request already exists for o:b."}
                    ],
                },
            )
        assert "head=o:b" in req.full_url
        return _Resp([{"number": 3, "html_url": "u"}])

    _fake_open(monkeypatch, handler)
    pr = vus.create_pr(_gh(), "o/veracode", "o", {"head": "b", "base": "main", "title": "t", "body": "x", "draft": False})
    assert pr["number"] == 3
    assert calls == ["POST", "GET"]


# --- redirects ------------------------------------------------------------- #
def _serve(handler_cls):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_redirect_to_other_host_is_not_followed():
    seen = {}

    class B(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["auth"] = self.headers.get("Authorization")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv_b = _serve(B)

    class A(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{srv_b.server_address[1]}/steal")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv_a = _serve(A)
    try:
        gh = vus.GitHub(f"http://127.0.0.1:{srv_a.server_address[1]}", "SECRET", vus.RateLimiter(sleep=lambda s: None))
        with pytest.raises(vus.GitHubError) as ei:
            gh.get("/repos/o/veracode")
    finally:
        srv_a.shutdown()
        srv_b.shutdown()
    assert ei.value.status == 302
    assert "auth" not in seen


def test_redirect_on_same_api_keeps_token_and_posts_are_not_turned_into_gets():
    seen = []

    class A(http.server.BaseHTTPRequestHandler):
        def _go(self):
            seen.append((self.command, self.path, self.headers.get("Authorization")))
            self.rfile.read(int(self.headers.get("Content-Length") or 0))  # else Windows may reset
            if self.path == "/old":
                self.send_response(301)
                self.send_header("Location", "/new")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        do_GET = do_POST = _go

        def log_message(self, *a):
            pass

    srv = _serve(A)
    try:
        gh = vus.GitHub(f"http://127.0.0.1:{srv.server_address[1]}", "SECRET", vus.RateLimiter(sleep=lambda s: None))
        assert gh.get("/old") == {"ok": True}
        with pytest.raises(vus.GitHubError) as ei:
            gh.req("POST", "/old", {"a": 1})
    finally:
        srv.shutdown()
    assert seen[:2] == [("GET", "/old", "Bearer SECRET"), ("GET", "/new", "Bearer SECRET")]
    assert seen[2:] == [("POST", "/old", "Bearer SECRET")]  # no silent GET /new
    assert ei.value.status == 301


# --- rate limiter ---------------------------------------------------------- #
def test_observe_without_rate_limit_headers_lifts_hourly_cap():
    lim = vus.RateLimiter()
    lim.observe(_Hdrs({}))
    assert lim.hourly.cap > 100_000
    assert lim.writes_min.cap == 60 and lim.points.cap == 720  # secondary limits kept
    lim.observe(_Hdrs({"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "4999"}))
    assert lim.hourly.cap == 4000


def test_observe_error_without_headers_keeps_cap():
    lim = vus.RateLimiter()
    lim.observe(_Hdrs({}), success=False)
    assert lim.hourly.cap == 4000


class _Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def test_pause_adds_jitter(monkeypatch):
    clock = _Clock()
    lim = vus.RateLimiter(clock=clock, sleep=clock.sleep)
    monkeypatch.setattr(vus.random, "uniform", lambda a, b: b)
    lim.pause(30)
    with lim.slot("GET"):
        pass
    assert clock.slept and clock.slept[0] > 30


def test_network_backoff_adds_jitter(monkeypatch):
    slept = []
    gh = vus.GitHub("https://api.github.com", "t", vus.RateLimiter(sleep=slept.append))
    monkeypatch.setattr(vus.random, "uniform", lambda a, b: b)

    def handler(req):
        raise OSError("boom")

    _fake_open(monkeypatch, handler)
    with pytest.raises(vus.GitHubError):
        gh.get("/x")
    assert slept and all(s > 2 ** (i + 1) for i, s in enumerate(slept))
