import pytest

import veracode_upstream_sync as vus

WF = ".github/workflows/a.yml"


# --- actions policy -------------------------------------------------------- #
def _policy_gh(patterns):
    class GH:
        def get(self, path, ok404=False):
            if path.endswith("/selected-actions"):
                return {"github_owned_allowed": False, "verified_allowed": False, "patterns_allowed": patterns}
            return {"enabled": True, "allowed_actions": "selected"}

    return GH()


@pytest.mark.parametrize(
    "uses, patterns, allowed",
    [
        ("someorg/scan@v3", ["someorg/scan@v4"], False),  # pinned pattern: only that ref
        ("someorg/scan@v4", ["someorg/scan@v4"], True),
        ("someorg/scan@v3", ["someorg/scan"], True),
        ("someorg/scan@v3", ["someorg/scan@*"], True),
        ("someorg/scan@v3", ["someorg/*"], True),
        ("SomeOrg/Scan@v3", ["someorg/scan@*"], True),  # case-insensitive on every OS
        ("someorg/scan@V4", ["SOMEORG/SCAN@v4"], True),
        ("other/scan@v3", ["someorg/*"], False),
    ],
)
def test_actions_policy_patterns(uses, patterns, allowed):
    plan = vus.Plan(None, "x", {})
    plan.new_uses = {uses}
    vus._check_actions_policy(_policy_gh(patterns), "o/veracode", plan)
    assert (plan.todo == []) is allowed, plan.todo


# --- secrets and variables ------------------------------------------------- #
class _Cust:
    def text(self, e):
        return e[1] if e else ""


def _plan(workflow_text):
    ours = {WF: ("100644", "steps: []")}
    return vus.Plan(None, "x", {WF: ("100644", workflow_text)}), ours


class _GH:
    def __init__(self, listings):
        self.listings, self.paths = listings, []

    def get(self, path, ok404=False):
        path = path.split("?")[0]
        self.paths.append(path)
        for suffix, value in self.listings.items():
            if path.endswith(suffix):
                if isinstance(value, Exception):
                    raise value
                key = "secrets" if "secrets" in suffix else "variables"
                return {"total_count": len(value), key: [{"name": n} for n in value]}
        return {"total_count": 0, "secrets": [], "variables": []}


def _todo_text(plan):
    return " | ".join(str(t) for t in plan.todo)


def test_secret_names_are_case_insensitive():
    plan, ours = _plan("run: echo ${{ secrets.veracode_api_id }}")
    vus._check_secrets_and_variables(_GH({"/actions/secrets": ["VERACODE_API_ID"]}), "o/veracode", plan, _Cust(), ours)
    assert plan.todo == []


def test_secret_already_used_before_in_other_case_is_not_new():
    ours = {WF: ("100644", "run: ${{ secrets.Veracode_Api_Id }}")}
    plan = vus.Plan(None, "x", {WF: ("100644", "run: ${{ secrets.VERACODE_API_ID }} # changed")})
    gh = _GH({})
    vus._check_secrets_and_variables(gh, "o/veracode", plan, _Cust(), ours)
    assert plan.todo == [] and gh.paths == []


def test_environment_secrets_are_checked():
    text = "jobs:\n  scan:\n    environment: Prod Env\n    steps:\n      - run: ${{ secrets.ENV_ONLY }}\n"
    plan, ours = _plan(text)
    gh = _GH({"/environments/Prod%20Env/secrets": ["ENV_ONLY"]})
    vus._check_secrets_and_variables(gh, "o/veracode", plan, _Cust(), ours)
    assert plan.todo == []
    assert "/repos/o/veracode/environments/Prod%20Env/secrets" in gh.paths


def test_environment_given_as_mapping():
    text = "jobs:\n  scan:\n    environment:\n      name: prod\n    steps:\n      - run: ${{ vars.ENV_VAR }}\n"
    plan, ours = _plan(text)
    vus._check_secrets_and_variables(_GH({"/environments/prod/variables": ["ENV_VAR"]}), "o/veracode", plan, _Cust(), ours)
    assert plan.todo == []


def test_one_unreadable_listing_does_not_disable_the_check():
    plan, ours = _plan("run: ${{ secrets.IN_REPO }} ${{ secrets.MISSING }}")
    gh = _GH({"/actions/secrets": ["IN_REPO"], "/actions/organization-secrets": vus.GitHubError(403, "nope")})
    vus._check_secrets_and_variables(gh, "o/veracode", plan, _Cust(), ours)
    text = _todo_text(plan)
    assert "IN_REPO" not in text  # found in the readable repo listing
    assert "MISSING" in text and "Confirm" in text  # not found, org list unreadable: confirm, not add


def test_missing_everywhere_and_all_readable_is_add():
    plan, ours = _plan("run: ${{ secrets.MISSING }}")
    vus._check_secrets_and_variables(_GH({}), "o/veracode", plan, _Cust(), ours)
    text = _todo_text(plan)
    assert "Add `MISSING`" in text and "Confirm" not in text
