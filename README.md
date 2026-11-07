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
   - Secrets and variables the new workflows use exist in the repo or org
   - New actions are allowed by the Actions policy
   - No custom setting or custom file was lost
4. Opens a pull request. If every check passes, it's ready to merge. Otherwise it's a draft, labeled `needs-work`, with a To do list. If a custom setting would be lost, no PR is opened and the repo is reported as an error.

It never pushes to the default branch, and never changes a branch that someone else has committed to. `veracode.yml` keeps its current values, and new upstream settings are added.

---

## Quickstart

```bash

# 1. See what would happen everywhere. No writes, results in ./out
python veracode_upstream_sync.py --orgs-file orgs.txt --dry-run

# 2. Roll out a specific version and assign drafts to yourself
python veracode_upstream_sync.py --orgs-file orgs.txt --upstream-ref <tested tag or sha> --assign-drafts my-user
```

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
| `--force` | Reopen an update that was closed without merging |
| `--report` | Write a JSON report |
| `--workers` | Repos processed in parallel, 1 to 32 (default 4) |
| `--api-base` | GHE.com API URL (HTTPS only) |

---

## Output

| Status | Meaning |
|---|---|
| `pr-created` | PR opened: `ready`, or `needs work` (draft) |
| `pr-exists` | A PR for this upstream version is already open |
| `up-to-date` / `skipped` | Nothing to do, or the org has no `veracode` repo (the detail says why) |
| `error` | Nothing pushed for this repo (or the org could not be read), the others still ran |

Exit code `1` if any repo errored, `2` on setup errors (token, upstream).

---

## Troubleshooting

| Error | Fix |
|---|---|
| `missing scope(s): workflow` | Add the `workflow` scope to the token |
| `authorize the token for this org's SAML SSO` | Configure SSO on the token for that org |
| `branch ... has commits from someone else` | Delete that branch, then re-run |
| `would lose your value ... (upstream renamed or removed section ...)` | The repo predates a `veracode.yml` restructure: move those values to the new section by hand once |

