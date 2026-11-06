# Workflow App Upstream Sync

Keeps the `veracode` repo (Veracode GitHub Workflow Integration) in every org you target in sync with upstream [`veracode/github-actions-integration`](https://github.com/veracode/github-actions-integration), through pull requests.

- Every change is a pull request a person reviews and merges.
- **Keeps current customizations.** A 3-way merge keeps custom files, custom workflow steps and `veracode.yml` settings. `repo_list.yml` is never touched.
-  Anything it cannot merge safely becomes a **draft** PR with conflict markers and a short To do list.
- **Checks itself before every push.** If a result would change a protected or custom file, drop a customer `veracode.yml` value or leave stray conflict markers, that repo gets no PR and the run reports an error.

## Requirements

- Python 3.10+ (tested on 3.11, 3.12 and 3.13), `git` 2.31+, `pip install -r requirements.txt` (PyYAML only)
- A token, read in this order: `GH_TOKEN`, `GITHUB_TOKEN`, then `gh auth token`.

| Token type | Permissions |
|---|---|
| Classic | `repo`, `workflow`, `read:org`. If the org uses SAML SSO, **authorize the token for that org**. |
| Fine-grained | One resource owner per token, so pass `--orgs`. Repository: **Contents**, **Pull requests**, **Workflows**, **Issues**: Read and write. |

`workflow` / **Workflows** is mandatory: GitHub rejects any push that touches `.github/workflows/` without it. A classic token without `repo` and `workflow` is refused before anything runs.

## Quick start

```bash
export GH_TOKEN=ghp_xxx

# 1. Preview: no push, no PR. Writes <org>__veracode.patch and .md (the PR text) to ./out
python veracode_upstream_sync.py --orgs-file orgs.txt --dry-run

# 2. Pilot on one org, then review and merge its PR
python veracode_upstream_sync.py --orgs acme

# 3. Roll out
python veracode_upstream_sync.py --orgs-file orgs.txt --report run1.json
```

```
REPO              STATUS       DETAIL
acme/veracode     pr-created   https://github.com/acme/veracode/pull/12  (ready)
globex/veracode   pr-created   https://github.com/globex/veracode/pull/7  (needs work)
initech/veracode  up-to-date   base: last sync

pr-created: 2, up-to-date: 1 | API requests: 41, rate-limit waits: 0s
```

Re-running is safe: if a PR for the same upstream commit is open, nothing is pushed (`pr-exists`).

## Options

| Goal | Flag |
|---|---|
| Every org the token can see | *(no org flag)* |
| Only some orgs | `--orgs a b` or `--orgs-file orgs.txt` (one per line, `#` comments). Both can be combined. |
| Skip orgs | `--exclude-orgs x y` |
| Protect more customer-owned files | `--keep README.md 'docs/*'` |
| Never change existing `veracode.yml` values, only add new settings | `--keep-defaults` |
| Pin to an upstream tag or commit | `--upstream-ref v1.2.3` |
| Request reviewers | `--reviewers alice team:appsec` |
| Re-propose an update the customer closed | `--force` |
| Integration repo has another name | `--repo-name veracode-integration` |
| GHE.com (data residency) | `--api-base https://api.SUBDOMAIN.ghe.com` |
| Machine-readable results | `--report run.json` |
| Large repos or slow links | `--git-timeout 1800` |
| Parallel repos (default 4) | `--workers 8` |

## How files are merged

The merge base is the upstream commit the repo was last synced to (recorded in `.veracode-upstream-sync.json`). Without it, the shared git history is used, and for repos that were copied rather than imported, the upstream commit that matches the most files.

| Path | Behavior |
|---|---|
| `repo_list.yml` and `--keep` globs | Never modified. The PR notes when upstream changed them. |
| `veracode.yml` | 3-way merge. On conflict the **customer value wins**. New upstream settings are added after their upstream neighbor, with their comments. Sections the customer deleted stay deleted. If a value still matches the old default and upstream changed the default, the new default is applied and the PR asks to confirm it; `--keep-defaults` keeps the old value. |
| Upstream workflows | 3-way merge. If custom steps cause a conflict, it retries at **step level**: takes the custom steps out, merges, then puts each one back after the step it followed (or before the step it preceded). Steps are matched by `id`, `name`, then action without version, so `checkout@v4` to `@v6` is the same step. Only edits to the same upstream lines remain conflicts. |
| Customer-only files (custom workflows, helpers) | Untouched. |
| `helper/cli/` Veracode CLI binaries | Newer version wins. |
| New upstream files | Added. |
| Removed upstream | Removed if the customer never changed them, otherwise kept and listed. |
| Deleted by the customer | Stay deleted. |

Every changed YAML file is parsed with a strict loader (duplicate keys are errors). A merge that leaves a duplicate setting or invalid YAML, or a custom step that ended up in another job, makes the PR a draft with a To do item. When upstream renamed, removed or rearranged the steps next to a customer edit, the PR adds a "Check" item so the reviewer confirms where it landed.

## Reviewing a sync PR

Each PR opens with one line and a checklist ([ready to merge](examples/sample-pr-ready-to-merge.md), [needs review](examples/sample-pr-needs-review.md)):

| First line | What to do |
|---|---|
| **No action needed.** | Review the diff and merge. |
| **Action required:** N items to check | Tick each item (new default to confirm, new action to allow), then merge. |
| **Action required:** N conflicts to resolve (draft) | Edit each listed file on the PR branch: keep the lines you need, delete the `<<<<<<<` / `\|\|\|\|\|\|\|` / `=======` / `>>>>>>>` lines, commit, mark **Ready for review**. |

Then run one scan to confirm the checks pass. Labels: `veracode-upstream-sync`, plus `merge-conflicts` on drafts.

## Safety guards

| Guard | What happens |
|---|---|
| Upstream is not the Veracode integration (wrong URL or ref) | Run stops before touching any org (exit 2). |
| A repo named `veracode` that is not the integration (no `veracode.yml`, nothing in common with upstream) | `skipped`, nothing pushed. |
| Final check before push fails (see above) | `error: safety check failed, nothing pushed`. Likely a bug: send the repo's `--dry-run` output. |
| Push cannot be confirmed on the remote | `error`, no PR opened. The next run picks it up. |
| Another run created the same PR at the same time | The existing PR is reused. |
| Interrupted between push and PR | The next run reuses the branch (only tool commits) and opens the PR. |

## Rate limits and security

All workers share one limiter, paced at 80% of GitHub's [documented limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api):

| Window | GitHub limit | Paced at |
|---|---|---|
| Primary requests | 5,000/hour (PAT) | 4,000/hour, re-derived from `x-ratelimit-limit` |
| REST points (GET 1, write 5) | 900/min | 720/min |
| Content-creating writes | 80/min, 500/hour | 60/min, 400/hour |
| Concurrent requests | 100 | 50 |

On a 403/429 it waits for `retry-after`, else until `x-ratelimit-reset`, else at least a minute with exponential backoff, and pauses every worker. Reads are retried on 5xx and network errors. Writes are not blindly retried: after an ambiguous failure it checks whether the PR was created before trying again, so it never opens duplicates.

## Statuses and exit codes

| Status | Meaning |
|---|---|
| `pr-created` | Branch `veracode-upstream-sync/<upstream sha>` pushed, PR opened (detail: `ready`, `check` or `needs work`). Older sync PRs with only tool commits are labeled `superseded`, closed and their branches deleted. |
| `pr-exists` | A PR for this upstream commit is already open. |
| `up-to-date` | Nothing to change. |
| `skipped` | Archived, no push access, not the integration repo, or this update was closed without merging (use `--force`). |
| `dry-run` | Preview files written to `--out-dir`. |
| `error` | See detail. Other orgs still run. |

Exit code: `0` all good, `1` at least one `error`, `2` setup failure (token, upstream). Ctrl+C finishes the repos in progress and reports them.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Token is missing scope(s): workflow` or `refusing to allow a Personal Access Token to create or update workflow` | Add `workflow` scope (classic) or Workflows: Read and write (fine-grained). |
| `authorize the token for this org's SAML SSO` | GitHub > Settings > Developer settings > Tokens > Configure SSO > Authorize. |
| Org missing from the run | Token cannot see it. Classic: check `read:org` and SSO. Fine-grained: pass `--orgs`. |
| `branch ... has commits from someone else` | Someone committed to a sync branch with no open PR. Delete the branch or use `--branch-prefix`. |
| Push rejected on `veracode-upstream-sync/*` | A ruleset restricts branch names. Allow the prefix or use `--branch-prefix`. |
