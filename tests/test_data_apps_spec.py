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
    # `ports` is a test-only escape hatch the apps-runner API accepts (see
    # services/apps_runner/api.py::up) so tests/test_data_apps_e2e_docker.py
    # can reach the runtime container directly without the ingress proxy.
    # Production specs must never set it — apps are reached exclusively
    # through the proxy.
    assert "ports" not in spec


def test_config_json_external_repo():
    app_external = {
        "id": "app_ext",
        "slug": "custom-app",
        "repo_mode": "external",
        "repo_url": "https://github.com/user/repo.git",
        "repo_branch": "feature-x",
        "runtime_tag": "",
        "mem_limit": "",
        "cpu_limit": "",
        "env": "{}",
    }
    cfg = build_config_json(app_external, secrets={}, clone_url="", clone_token="")
    git = cfg["dataApp"]["git"]
    assert git["repository"] == "https://github.com/user/repo.git"
    assert git["branch"] == "feature-x"
    assert "username" not in git
    assert "#password" not in git


def test_container_spec_malformed_env_json():
    app_bad_env = APP.copy()
    app_bad_env["env"] = '{"invalid": json}'
    try:
        build_container_spec(app_bad_env, defaults=DEFAULTS, data_dir="/data")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "invalid env JSON" in str(exc)
        assert "sales" in str(exc)


def test_container_spec_malformed_cpu_limit():
    app_bad_cpu = APP.copy()
    app_bad_cpu["cpu_limit"] = "not-a-number"
    try:
        build_container_spec(app_bad_cpu, defaults=DEFAULTS, data_dir="/data")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "invalid cpu_limit" in str(exc)
        assert "sales" in str(exc)
