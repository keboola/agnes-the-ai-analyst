from src.data_apps.spec import SLUG_RE, build_config_json, build_container_spec

APP = {
    "id": "app_abc",
    "slug": "sales",
    "repo_mode": "internal",
    "repo_url": "",
    "repo_branch": "main",
    "runtime_tag": "",
    "mem_limit": "",
    "cpu_limit": "",
    "env": '{"FOO": "bar"}',
    "sleep_mode": "recreate",
}
DEFAULTS = {
    "runtime_image": "keboolapublic.azurecr.io/data-app-python-js:1.6.2_python-3.13_node-24",
    "default_mem_limit": "1g",
    "default_cpus": 1.0,
}


def test_slug_re():
    assert SLUG_RE.match("sales-dash")
    assert not SLUG_RE.match("Sales")
    assert not SLUG_RE.match("-x")


def test_config_json_internal_repo_embeds_token():
    cfg = build_config_json(
        APP, secrets={"DB_PASSWORD": "s3"}, clone_url="http://app:8000/data-apps.git/sales", clone_token="PATPAT"
    )
    git = cfg["dataApp"]["git"]
    assert git["repository"] == "http://app:8000/data-apps.git/sales"
    assert git["branch"] == "agnes-live"
    assert git["username"] == "agnes"
    assert git["#password"] == "PATPAT"
    # secrets: caller-provided + injected platform vars
    assert cfg["dataApp"]["secrets"]["#DB_PASSWORD"] == "s3"
    assert cfg["dataApp"]["secrets"]["AGNES_TOKEN"] == "PATPAT"
    assert "input" not in cfg  # Data Loader never configured on this platform


def test_container_spec_defaults_and_overrides():
    spec = build_container_spec(APP, defaults=DEFAULTS, data_dir="/data")
    assert spec["name"] == "agnes-dataapp-sales"
    assert spec["image"] == DEFAULTS["runtime_image"]
    assert spec["mem_limit"] == "1g"
    assert spec["network"] == "agnes-apps"
    assert spec["labels"] == {"agnes.data-app": "app_abc"}
    assert spec["cache_volume"] == "agnes-dataapp-cache-sales"
    assert spec["env"]["AGNES_URL"] == "http://app:8000"
    assert spec["env"]["FOO"] == "bar"
    assert "DATA_LOADER_API_URL" not in spec["env"]
