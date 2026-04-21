def test_intentional_fail_for_dryrun():
    """Intentional failure to verify CI gate blocks broken PRs. Remove after dryrun."""
    assert False, "dryrun: this test is supposed to fail"
