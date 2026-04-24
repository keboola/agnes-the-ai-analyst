from app.api.marketplace import _packager as packager


def test_packager_reads_source(configured):
    data = packager.load_source_marketplace()
    names = {p["name"] for p in data["plugins"]}
    assert names == {"alpha", "beta", "gamma"}


def test_groups_resolve(configured):
    groups = packager.load_user_groups("finance@test")
    assert groups == ["grp_finance"]
    allowed = packager.resolve_allowed_plugin_names(groups)
    assert allowed == {"alpha"}
