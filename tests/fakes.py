"""In-memory stand-ins for Git and Upstream so merge_repo runs without git."""

import hashlib
import types

import veracode_upstream_sync as vus


class FakeGit:
    """Content-addressed blobs, like the real object store."""

    def __init__(self, blobs):
        self.blobs = dict(blobs)

    def blob(self, sha):
        return self.blobs[sha]

    def text(self, entry):
        return self.blobs[entry[1]].decode(errors="replace") if entry else ""

    def write_blob(self, data):
        sha = hashlib.sha1(data).hexdigest()
        self.blobs[sha] = data
        return sha


class FakeUp(FakeGit):
    def __init__(self, blobs, tree, base_tree):
        super().__init__(blobs)
        self.tree, self._base = tree, base_tree

    def tree_at(self, commit):
        return self._base

    def structure_baseline(self):
        return set()


def merge_repo(blobs, ours, base, theirs, keep_defaults=True):
    """(customer repo fake, Plan) for a 3-way merge of path -> (mode, blob key) trees."""
    cust = FakeGit(blobs)
    up = FakeUp(blobs, theirs, base)
    args = types.SimpleNamespace(keep=[], keep_defaults=keep_defaults)
    return cust, vus.merge_repo(cust, up, ours, "c", "last sync", args)
