# Veracode Workflow Integration - Upstream Sync

Keeps the `veracode` repo in each GitHub organization up to date with [veracode/github-actions-integration](https://github.com/veracode/github-actions-integration). Every update is a pull request, and custom settings are kept: `repo_list.yml`, `veracode.yml` values, custom workflow steps and custom files.

---

## How It Works

For each organization, the script:

1. Skips repos that are archived, aren't the integration, or are already on newer upstream code than the target
2. Merges the latest upstream release into the repo with a 3-way merge, keeping custom settings
3. Checks the result:
   - YAML is valid, with no duplicate settings
   - Workflow references still resolve: `needs`, step ids, local actions, helper scripts, and the inputs and secrets of shared workflows
   - Secrets and variables the new workflows use exist in the org or the repo (and which old secret to copy when upstream renamed one)
   - New actions are allowed by the Actions policy
   - No custom setting or custom file was lost
4. Opens a pull request. If every check passes, it's ready to merge. Otherwise it's a draft, labeled `needs-work`, with a To do list. If a custom setting would be lost, no PR is opened and the repo is reported as an error.

It never pushes to the default branch. `veracode.yml` keeps its current values, and new upstream settings and product sections are added as upstream wrote them (with their comments), listed in the PR. When upstream updates an action version, upstream's version is used even if the repo had changed it, and the PR lists it. A version the repo moved ahead of upstream is never downgraded. A version pinned to a commit SHA is left as a conflict, so it gets re-pinned instead of unpinned.

---

## Quickstart

```bash
# 1. Test on one org first: merge its PR and run a scan
python veracode_upstream_sync.py --orgs test-org

# 2. See what would happen everywhere. No writes, results in ./out
python veracode_upstream_sync.py --orgs-file orgs.txt --dry-run

# 3. Roll out the version you tested, assign drafts to yourself
python veracode_upstream_sync.py --orgs-file orgs.txt --upstream-ref <tested tag or sha> --assign-drafts my-user

# Undo the last update in an org
python veracode_upstream_sync.py --orgs my-org --revert
```

Re-running is safe.
---

## Requirements

Python 3.10+, git 2.31+, and `pip install -r requirements.txt`.

Token in `GH_TOKEN` or `GITHUB_TOKEN`, or a `gh auth login` session:

- **Classic PAT:** `repo`, `workflow`, `read:org`. Authorize it for SAML SSO orgs.
- **Fine-grained PAT:** read and write on Contents, Pull requests, Workflows and Issues, plus read on Secrets, Variables, Administration and Actions. One org per token.

---

## Options

| Flag | Description |
|---|---|
| `--orgs`, `--orgs-file`, `--exclude-orgs` | Orgs to sync. Default: every org the token can see |
| `--upstream-ref` | Upstream tag or commit. Default: the latest `vX.Y.Z` release |
| `--dry-run` | Write the diff and PR text to `--out-dir`, push nothing |
| `--assign-drafts` | Assign draft PRs to these users |
| `--reviewers` | Request reviewers (`user` or `team:slug`) |
| `--keep` | Extra files never to change (`repo_list.yml` is always kept) |
| `--adopt-new-defaults` | Apply upstream's new `veracode.yml` defaults instead of keeping current values |
| `--force` | Reopen an update that was closed without merging, or apply a reverted one again |
| `--revert` | Undo the last update: close its open PR, or open a PR that reverts the merged one (see below) |
| `--report` | Write a JSON report and the CSV action list |
| `--workers` | Repos processed in parallel, 1 to 32 (default 4) |
| `--api-base` | GHE.com API URL (HTTPS only) |

---

## Output

| Status | Meaning |
|---|---|
| `pr-created` | PR opened: `ready`, or `needs work` (draft) |
| `pr-exists` | A PR for this upstream version is already open |
| `up-to-date` / `skipped` | Nothing to do, or the org has no `veracode` repo (the detail says why) |
| `reverted` | `--revert` closed the open update PR (nothing had been merged) |
| `error` | Nothing pushed for this repo (or the org could not be read), the others still ran |

Exit code `1` if any repo errored, `2` on setup errors (token, upstream).

### Report

`--report run.json` also writes `run.csv`, the list of actions for the team: one row per action, orgs with the least to fix first. Run it with `--dry-run` first to plan the rollout. Re-runs read the actions back from open PRs.

| Column | Content |
|---|---|
| `org` | The organization |
| `status` | `Ready to merge`, `Needs fixes`, `No PR: fix by hand`, `Nothing to do` |
| `fixes_before_merge` | How many actions must be done before merging |
| `must_fix` | `yes` if this action blocks the merge |
| `action` | For example `Resolve merge conflict`, `Add renamed org secret`, `Fix broken workflow reference`, `Merge the PR` |
| `where` | File and line, or the settings page |
| `details` | What to change, for example `Pick one value for "fetch-depth": yours "fetch-depth: 1" or upstream's "fetch-depth: 0"` |
| `pr` | Link to the PR |

### Revert

`--revert` undoes only what the last update changed, with a 3-way merge per file, so changes made after it are kept. If a later change touched the same lines, the revert PR is a draft with the conflicts in its To do list. Later runs skip a reverted upstream version however it was reverted (this flag, GitHub's Revert button, or by hand), and the next upstream release opens a new update as usual. Works with `--dry-run` and `--report`.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `missing scope(s): workflow` | Add the `workflow` scope to the token |
| `authorize the token for this org's SAML SSO` | Configure SSO on the token for that org |
| `branch ... has commits from someone else` | Delete that branch, then re-run |
| `would lose your value ... (upstream renamed or removed section ...)` | The repo predates a `veracode.yml` restructure: move those values to the new section by hand once |

