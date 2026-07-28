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
    # The token is embedded into the repository URL: the runtime image only
    # adds credentials to HTTPS clone URLs, never plain HTTP, so Agnes's
    # internal http://app:8000 backend needs the creds pre-embedded or the
    # container clone prompts for a username and crash-loops.
    assert git["repository"] == "http://agnes:PATPAT@app:8000/data-apps.git/sales"
    assert git["branch"] == "agnes-live"
    assert git["username"] == "agnes"
    assert git["#password"] == "PATPAT"
    # secrets: caller-provided + injected platform vars
    assert cfg["dataApp"]["secrets"]["#DB_PASSWORD"] == "s3"
    assert cfg["dataApp"]["secrets"]["AGNES_TOKEN"] == "PATPAT"
    assert "input" not in cfg  # Data Loader never configured on this platform


def test_config_json_draft_uses_pinned_branch():
    from src.data_apps.spec import build_config_json

    row = {"repo_mode": "internal", "is_draft": True, "draft_branch": "init", "slug": "d--init"}
    cfg = build_config_json(row, secrets={}, clone_url="http://app:8000/data-apps.git/d", clone_token="PAT")
    assert cfg["dataApp"]["git"]["branch"] == "init"
    assert cfg["dataApp"]["git"]["repository"].endswith("/data-apps.git/d")
    assert cfg["dataApp"]["git"]["#password"] == "PAT"


def test_config_json_prod_still_agnes_live():
    from src.data_apps.spec import build_config_json

    row = {"repo_mode": "internal", "slug": "d"}
    cfg = build_config_json(row, secrets={}, clone_url="http://x/data-apps.git/d", clone_token="PAT")
    assert cfg["dataApp"]["git"]["branch"] == "agnes-live"


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


def test_config_json_embeds_percent_encoded_token():
    # A token with URL-significant characters must be percent-encoded in the
    # embedded repository URL so the container's `git clone` parses it.
    from src.data_apps.spec import build_config_json

    row = {"repo_mode": "internal", "slug": "s"}
    cfg = build_config_json(row, secrets={}, clone_url="http://app:8000/data-apps.git/s", clone_token="a/b@c:d")
    assert cfg["dataApp"]["git"]["repository"] == "http://agnes:a%2Fb%40c%3Ad@app:8000/data-apps.git/s"
    assert cfg["dataApp"]["git"]["#password"] == "a/b@c:d"  # raw token still in the field


def test_config_json_does_not_double_embed_credentials():
    from src.data_apps.spec import build_config_json

    row = {"repo_mode": "internal", "slug": "s"}
    cfg = build_config_json(
        row, secrets={}, clone_url="http://agnes:existing@app:8000/data-apps.git/s", clone_token="NEW"
    )
    assert cfg["dataApp"]["git"]["repository"] == "http://agnes:existing@app:8000/data-apps.git/s"


def test_config_json_external_repo_repository_untouched():
    from src.data_apps.spec import build_config_json

    row = {"repo_mode": "external", "repo_url": "https://github.com/org/repo", "repo_branch": "main", "slug": "s"}
    cfg = build_config_json(row, secrets={}, clone_url="ignored", clone_token="PAT")
    # External repos keep their own URL + branch; no token embedding, no username field.
    assert cfg["dataApp"]["git"] == {"repository": "https://github.com/org/repo", "branch": "main"}
