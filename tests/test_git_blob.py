import os
import subprocess
import threading

import pytest

import veracode_upstream_sync as vus


@pytest.fixture
def repo(tmp_path):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@b",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@b",
    }
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    files = {f"f{i}.txt": (f"line {i}\n" * (i + 1)).encode() for i in range(20)}
    files["empty"] = b""
    files["bin"] = bytes(range(256)) * 10
    for name, data in files.items():
        (src / name).write_bytes(data)
    subprocess.run(["git", "-C", str(src), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(src), "commit", "-qm", "x"], check=True, env=env)
    g = vus.Git(src, vus.git_env())
    tree = g.ls_tree("HEAD")
    yield g, {name: tree[name][1] for name in files}, files
    g.close()


def _count_processes(monkeypatch):
    calls = {"popen": 0, "run": 0}
    real_popen, real_run = subprocess.Popen, subprocess.run

    def popen(*a, **k):
        calls["popen"] += 1
        return real_popen(*a, **k)

    def run(cmd, *a, **k):
        if "cat-file" in cmd:
            calls["run"] += 1
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(vus.subprocess, "Popen", popen)
    monkeypatch.setattr(vus.subprocess, "run", run)
    return calls


def test_blobs_read_through_one_process(repo, monkeypatch):
    g, shas, files = repo
    calls = _count_processes(monkeypatch)
    for name, sha in shas.items():
        assert g.blob(sha) == files[name]
    assert calls == {"popen": 1, "run": 0}
    g._blobs.clear()
    assert g.blob(shas["bin"]) == files["bin"]  # same process again
    assert calls["popen"] == 1


def test_missing_object_falls_back_and_fails_clearly(repo):
    g, shas, files = repo
    with pytest.raises(vus.GitError):
        g.blob("1" * 40)
    assert g.blob(shas["f3.txt"]) == files["f3.txt"]  # the stream is still usable


def test_non_blob_is_an_error(repo):
    g, shas, files = repo
    tree = g.run("rev-parse", "HEAD^{tree}").strip()
    with pytest.raises(vus.GitError):
        g.blob(tree)
    assert g.blob(shas["f1.txt"]) == files["f1.txt"]


def test_bad_object_name_is_rejected(repo):
    g, _, _ = repo
    with pytest.raises(vus.GitError):
        g.blob("HEAD\nHEAD")


def test_close_stops_the_process_and_reads_restart(repo):
    g, shas, files = repo
    g.blob(shas["f0.txt"])
    proc = g._cat
    g.close()
    assert proc.poll() is not None and g._cat is None
    g._blobs.clear()
    assert g.blob(shas["f0.txt"]) == files["f0.txt"]


def test_concurrent_reads(repo):
    g, shas, files = repo
    errors = []

    def worker():
        try:
            for _ in range(5):
                g._blobs.clear()
                for name, sha in shas.items():
                    assert g.blob(sha) == files[name]
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_clone_can_be_deleted_after_close(repo, tmp_path):
    g, shas, _ = repo
    dest = tmp_path / "o__veracode.git"
    c = vus.clone(str(g.path), dest, vus.git_env())
    c.blob(shas["f2.txt"])
    c.close()
    vus.rmtree(dest)
    assert not dest.exists()


def test_upstream_cached_blob_does_not_wait_for_upstream_lock(repo):
    g, shas, files = repo
    up = object.__new__(vus.Upstream)
    up.git, up._lock = g, threading.Lock()
    g.blob(shas["f5.txt"])
    got = []
    with up._lock:  # another thread busy in pairs_at/closest_version
        t = threading.Thread(target=lambda: got.append(up.blob(shas["f5.txt"])))
        t.start()
        t.join(timeout=5)
    assert got == [files["f5.txt"]]
