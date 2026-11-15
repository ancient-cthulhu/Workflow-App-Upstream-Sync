import os
import stat
import subprocess

import pytest

import veracode_upstream_sync as vus


def _git_repo(path):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@b",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@b",
    }
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "a.txt").write_text("hi")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "x"], check=True, env=env)
    return path


def test_rmtree_removes_read_only_files(tmp_path):
    d = tmp_path / "w"
    (d / "sub").mkdir(parents=True)
    f = d / "sub" / "pack.idx"
    f.write_text("x")
    os.chmod(f, stat.S_IREAD)
    vus.rmtree(d)
    assert not d.exists()


def test_rmtree_removes_a_bare_clone(tmp_path):
    src = _git_repo(tmp_path / "src")
    dest = tmp_path / "o__veracode.git"
    vus.clone(str(src), dest, vus.git_env())
    vus.rmtree(dest)
    assert not dest.exists()


def test_rmtree_missing_path_is_fine(tmp_path, caplog):
    vus.rmtree(tmp_path / "nope")
    assert "Could not delete" not in caplog.text


def test_rmtree_logs_what_it_could_not_delete(tmp_path, monkeypatch, caplog):
    d = tmp_path / "w"
    d.mkdir()
    (d / "f").write_text("x")

    def refuse(*a, **k):
        raise PermissionError("in use")

    monkeypatch.setattr(vus.os, "unlink", refuse)
    monkeypatch.setattr(vus.os, "remove", refuse)
    vus.rmtree(d)
    assert d.exists()
    assert "Could not delete" in caplog.text


def test_failed_clone_leaves_no_partial_dir(tmp_path, monkeypatch):
    dest = tmp_path / "o__veracode.git"

    def fake_run(cmd, **kw):
        (dest / "objects").mkdir(parents=True)
        f = dest / "objects" / "pack"
        f.write_text("x")
        os.chmod(f, stat.S_IREAD)
        return subprocess.CompletedProcess(cmd, 128, b"", b"fatal: early EOF")

    monkeypatch.setattr(vus.subprocess, "run", fake_run)
    with pytest.raises(vus.GitError):
        vus.clone("https://github.com/o/veracode.git", dest, {})
    assert not dest.exists()
