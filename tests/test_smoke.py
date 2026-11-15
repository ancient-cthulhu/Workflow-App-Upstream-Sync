import veracode_upstream_sync as vus


def test_parse_args_dry_run():
    args = vus.parse_args(["--orgs", "acme", "--dry-run"])
    assert args.dry_run
