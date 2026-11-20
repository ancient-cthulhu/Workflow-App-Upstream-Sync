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

It never pushes to the default branch. `veracode.yml` keeps its current values, and new upstream settings and product sections are added as upstream wrote them (with their comments), listed in the PR. When upstream updates an action version, upstream's version is used even if the repo had changed it, and the PR lists it. A version the repo moved ahead of upstream (`vX[.Y[.Z]]`) is never downgraded, and the PR lists it as kept. A version pinned to a commit SHA, or a branch that differs from upstream's ref, is left as a conflict, so it gets re-pinned instead of unpinned.

A step marked with a `# veracode-iac-rbac` comment (added by [Workflow-App-Bulk-Enterprise-Integration](https://github.com/ancient-cthulhu/Workflow-App-Bulk-Enterprise-Integration) to run IaC/secrets scans with the Veracode CLI) replaces upstream's `veracode/container_iac_secrets_scanning` step on purpose: it is kept, upstream's other changes to that workflow are taken, and the PR lists it as a custom step. Changes upstream makes to the replaced action's step are not applied; re-run Bulk if they matter.

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

# Let GitHub merge ready PRs once reviews and checks pass (preview first with --dry-run)
python veracode_upstream_sync.py --orgs-file orgs.txt --dry-run --auto-merge --report plan.json
python veracode_upstream_sync.py --orgs-file orgs.txt --auto-merge --report run.json
```

Re-running is safe.
---

## Requirements

Python 3.10+, git 2.31+, and `pip install -r requirements.txt`.

### GitHub App (multi-org)

```bash
export GITHUB_APP_ID=123456
export GITHUB_APP_PRIVATE_KEY="$(cat app.private-key.pem)"
```

`GITHUB_APP_PRIVATE_KEY` takes the PEM text, the PEM on one line with `\n`, or a path to the `.pem` file. The script signs a JWT, looks up each org's installation (`GET /orgs/{org}/installation`), mints a 1-hour installation token per org and caches it, renewing 10 minutes before expiry. Orgs where the App is not installed (or suspended) are reported and skipped; the run continues. Needs `pip install "PyJWT[crypto]"`. Without `--orgs`, every org the App is installed on is synced. Commits and PRs are authored by the App's bot (`<app-slug>[bot]`).

|Permission|Access|Why|
|---|---|---|
|Contents|Read and write|clone, push the sync branch, revert|
|Pull requests|Read and write|open, label, close and auto-merge PRs|
|Workflows|Read and write|push `.github/workflows/*`|
|Issues|Read and write|create the `veracode-upstream-sync` label|
|Secrets, Variables, Administration (org), Actions|Read|settings checks in the PR To do list (optional)|

A repo is skipped when the App lacks Contents, Pull requests or Workflows write.

### Personal access token (fallback when the App variables are not set)

Token in `GH_TOKEN` or `GITHUB_TOKEN`, or a `gh auth login` session:

- **Classic PAT:** `repo`, `workflow`, `read:org`. Authorize it for SAML SSO orgs.
- **Fine-grained PAT:** read and write on Contents, Pull requests, Workflows and Issues, plus read on Secrets, Variables, Administration and Actions. One org per token.

`--auto-merge` needs nothing more: the same push access and Pull requests write permission. The repo must have **Settings > General > Allow auto-merge** turned on; the tool never changes repo settings or branch protection.

---

## Options

| Flag | Description |
|---|---|
| `--orgs`, `--orgs-file`, `--exclude-orgs` | Orgs to sync. Default: every org the token can see |
| `--upstream-ref` | Upstream tag or commit. Default: the latest `vX.Y.Z` release |
| `--dry-run` | Write the diff and PR text to `--out-dir`, push nothing |
| `--auto-merge` | Turn on GitHub's auto-merge for PRs that pass every check (see below). With `--dry-run`, only report which would qualify |
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

Exit code `1` if any repo errored, `2` on setup errors (token, upstream), `130` when interrupted with Ctrl-C (repos in progress finish; the rest are reported as `error`, `interrupted`).

### Report

`--report run.json` also writes `run.csv` (`--report run.csv` writes the JSON there and the CSV to `run.actions.csv`), the list of actions for the team: one row per action, orgs with the least to fix first. Run it with `--dry-run` first to plan the rollout; it also reports PRs already open or declined, like a real run. Re-runs read the actions back from open PRs.

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

### Auto-merge

`--auto-merge` turns on GitHub's own [auto-merge](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/incorporating-changes-from-a-pull-request/automatically-merging-a-pull-request) (GraphQL `enablePullRequestAutoMerge`). It never merges anything itself: GitHub merges the PR only once branch protection, required reviews and required checks are satisfied, and nothing is bypassed. Off by default; without it nothing changes.

A PR qualifies only if all of these hold, checked on every run:

- The merge had no conflicts, every check passed, and the To do list is empty (no `needs-work`, no missing secret or variable, nothing to confirm such as new defaults, replaced files or replaced action versions)
- The PR is open, not a draft, not merged, not labeled `needs-work` or `merge-conflicts`, and comes from this update's branch
- For a PR opened by an earlier run: its branch holds only this tool's commits and changes exactly what a fresh merge and fresh checks of the current default branch change. Results from earlier runs or reports are never reused

Auto-merge is pinned to the commit that was checked, so GitHub refuses it if the branch moved since. The merge method is the repo's: the only one it allows, else GitHub's default for the token. A PR that already has auto-merge is left as it is, and no PR is opened twice. If auto-merge can't be turned on (disabled in the repo, permissions, merge method, rate limit, or the PR could already merge with nothing pending, which GitHub doesn't queue), the report says why and the sync still counts as successful.

`--dry-run --auto-merge` runs the same checks read-only and reports what would happen: no branches, commits, PRs or auto-merge.

| `auto_merge.status` | Meaning |
|---|---|
| `auto-merge-enabled` | Turned on; GitHub merges when the requirements are met |
| `auto-merge-already-enabled` | It was already on |
| `auto-merge-ineligible` | The PR doesn't qualify (`reason` says why) |
| `auto-merge-unavailable` | The PR qualifies, but GitHub wouldn't turn it on (`reason` says why) |
| `would-auto-merge` / `would-not-auto-merge` | `--dry-run` |

The repo's `status` (`pr-created`, `pr-exists`, `dry-run`, ...) keeps its meaning; the outcome is added under `auto_merge` in the JSON report:

```json
{
  "repo": "acme/veracode",
  "status": "pr-created",
  "pr": "https://github.com/acme/veracode/pull/42",
  "effort": "ready",
  "auto_merge": {
    "requested": true,
    "status": "auto-merge-enabled",
    "eligible": true,
    "enabled": true,
    "reason": "All validation checks passed.",
    "merge_method": "squash",
    "pr_number": 42,
    "validation": {"effort": "ready", "work": ""}
  }
}
```

---

## Troubleshooting

| Error | Fix |
|---|---|
| `missing scope(s): workflow` | Add the `workflow` scope to the token |
| `authorize the token for this org's SAML SSO` | Configure SSO on the token for that org |
| `branch ... has commits from someone else` | Delete that branch, then re-run |
| `would lose your value ... (upstream renamed or removed section ...)` | The repo predates a `veracode.yml` restructure: move those values to the new section by hand once |

