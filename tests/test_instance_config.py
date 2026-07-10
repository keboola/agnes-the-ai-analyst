"""Tests for instance_config loading."""
import pytest


class TestInstanceConfig:
    def test_missing_config_returns_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        from app.instance_config import get_instance_name
        name = get_instance_name()
        assert isinstance(name, str)

    def test_reads_nested_instance_name(self, tmp_path, monkeypatch):
        """get_instance_name should read instance.name from YAML, not flat instance_name."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text(
            "instance:\n  name: Acme Analytics\n  subtitle: Data Team\n"
        )

        import importlib
        import app.instance_config as mod
        # Reset cached config to force reload
        mod._instance_config = None
        importlib.reload(mod)

        assert mod.get_instance_name() == "Acme Analytics"
        assert mod.get_instance_subtitle() == "Data Team"

        # Cleanup: reset cache after test
        mod._instance_config = None


class TestInstanceBrand:
    """Brand and workspace_dir resolution: env > YAML > default,
    workspace_dir derives from brand when not explicitly set."""

    def _reload(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        import importlib
        import app.instance_config as mod
        mod._instance_config = None
        importlib.reload(mod)
        return mod

    def test_brand_defaults_to_agnes(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_BRAND", raising=False)
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_brand() == "Agnes"
        mod._instance_config = None

    def test_brand_from_yaml(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_BRAND", raising=False)
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text(
            "instance:\n  name: Acme\n  brand: Foundry AI\n"
        )
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_brand() == "Foundry AI"
        mod._instance_config = None

    def test_brand_env_overrides_yaml(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text(
            "instance:\n  name: Acme\n  brand: FromYaml\n"
        )
        monkeypatch.setenv("AGNES_INSTANCE_BRAND", "FromEnv")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_brand() == "FromEnv"
        mod._instance_config = None

    def test_brand_empty_falls_back_to_default(self, tmp_path, monkeypatch):
        # Empty env should not override the YAML/default to empty.
        monkeypatch.setenv("AGNES_INSTANCE_BRAND", "   ")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_instance_brand() == "Agnes"
        mod._instance_config = None

    def test_workspace_dir_derives_from_brand(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_WORKSPACE_DIR_NAME", raising=False)
        monkeypatch.setenv("AGNES_INSTANCE_BRAND", "Foundry AI")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_workspace_dir_name() == "FoundryAI"
        mod._instance_config = None

    def test_workspace_dir_strips_all_non_alphanumeric(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_WORKSPACE_DIR_NAME", raising=False)
        monkeypatch.setenv("AGNES_INSTANCE_BRAND", "ACME's Data!")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_workspace_dir_name() == "ACMEsData"
        mod._instance_config = None

    def test_workspace_dir_default_when_brand_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_WORKSPACE_DIR_NAME", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_BRAND", raising=False)
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_workspace_dir_name() == "Agnes"
        mod._instance_config = None

    def test_workspace_dir_explicit_env_overrides_derivation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_BRAND", "Foundry AI")
        monkeypatch.setenv("AGNES_WORKSPACE_DIR_NAME", "fdry")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_workspace_dir_name() == "fdry"
        mod._instance_config = None

    def test_workspace_dir_explicit_yaml_overrides_derivation(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_WORKSPACE_DIR_NAME", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_BRAND", raising=False)
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text(
            "instance:\n  name: Acme\n  brand: Foundry AI\n  workspace_dir: fdry\n"
        )
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_workspace_dir_name() == "fdry"
        mod._instance_config = None

    def test_brand_flows_into_resolve_lines(self, tmp_path, monkeypatch):
        """Brand + workspace_dir substitute into the setup script lines."""
        mod = self._reload(tmp_path, monkeypatch)
        from app.web.setup_instructions import resolve_lines
        joined = "\n".join(resolve_lines(
            "agnes.whl",
            instance_brand="Foundry AI",
            workspace_dir="FoundryAI",
        ))
        assert "Set up the Foundry AI CLI on this machine." in joined
        # Step 2 is the user-centric decision tree (#442); brand +
        # workspace_dir thread through the 2a "pick a workspace folder
        # (e.g. ~/Desktop/{workspace_dir})" copy, the 2c "default" hint,
        # and the manual-mkdir example. The default-path mention now
        # renders as `~/Desktop/...` (tilde), not `$HOME/Desktop/...`.
        assert "~/Desktop/FoundryAI" in joined
        assert "mkdir -p ~/Desktop/FoundryAI && cd ~/Desktop/FoundryAI" in joined
        assert "Bootstrap your Foundry AI workspace" in joined
        assert "Foundry AI workspace is ready" in joined
        # No raw placeholders survive substitution.
        assert "{instance_brand}" not in joined
        assert "{workspace_dir}" not in joined
        mod._instance_config = None

    def test_default_brand_keeps_agnes_branding(self, tmp_path, monkeypatch):
        """Backwards-compat: callers that don't pass brand/workspace_dir
        get the literal 'Agnes' / '~/Desktop/Agnes' rendering."""
        mod = self._reload(tmp_path, monkeypatch)
        from app.web.setup_instructions import resolve_lines
        joined = "\n".join(resolve_lines("agnes.whl"))
        assert "Set up the Agnes CLI on this machine." in joined
        # Step 2 is the user-centric decision tree (#442); default path
        # renders as `~/Desktop/Agnes` (tilde) inside the 2c "default"
        # branch + the manual-mkdir example.
        assert "~/Desktop/Agnes" in joined
        assert "mkdir -p ~/Desktop/Agnes && cd ~/Desktop/Agnes" in joined
        assert "Bootstrap your Agnes workspace" in joined
        assert "Agnes workspace is ready" in joined
        mod._instance_config = None


class TestHiddenLoginFeatures:
    """instance.hide_login_features / AGNES_INSTANCE_HIDE_LOGIN_FEATURES —
    stable /login feature-card keys to hide. Resolution: env (comma-string) >
    YAML (list or comma-string) > empty; normalized to a lowercase,
    whitespace-stripped, de-duplicated frozenset."""

    def _reload(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        import importlib
        import app.instance_config as mod
        mod._instance_config = None
        importlib.reload(mod)
        return mod

    def _write(self, tmp_path, yaml_body: str):
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text(yaml_body)

    def test_default_empty(self, tmp_path, monkeypatch):
        """Unset env + unset YAML → hide nothing (OSS vendor-neutral default)."""
        monkeypatch.delenv("AGNES_INSTANCE_HIDE_LOGIN_FEATURES", raising=False)
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_hidden_login_features() == frozenset()
        mod._instance_config = None

    def test_returns_frozenset(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_HIDE_LOGIN_FEATURES", "mcp")
        mod = self._reload(tmp_path, monkeypatch)
        assert isinstance(mod.get_hidden_login_features(), frozenset)
        mod._instance_config = None

    def test_env_comma_separated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_HIDE_LOGIN_FEATURES", "mcp,memory")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_hidden_login_features() == frozenset({"mcp", "memory"})
        mod._instance_config = None

    def test_env_whitespace_case_and_dedupe(self, tmp_path, monkeypatch):
        """Whitespace trimmed, lowercased, empties dropped, duplicates collapsed."""
        monkeypatch.setenv("AGNES_INSTANCE_HIDE_LOGIN_FEATURES", " MCP , Memory , mcp ,, ")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_hidden_login_features() == frozenset({"mcp", "memory"})
        mod._instance_config = None

    def test_env_overrides_yaml(self, tmp_path, monkeypatch):
        self._write(tmp_path, "instance:\n  name: Acme\n  hide_login_features: [data]\n")
        monkeypatch.setenv("AGNES_INSTANCE_HIDE_LOGIN_FEATURES", "mcp")
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_hidden_login_features() == frozenset({"mcp"})
        mod._instance_config = None

    def test_yaml_list(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_HIDE_LOGIN_FEATURES", raising=False)
        self._write(
            tmp_path,
            "instance:\n  name: Acme\n  hide_login_features:\n    - MCP\n    - memory\n",
        )
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_hidden_login_features() == frozenset({"mcp", "memory"})
        mod._instance_config = None

    def test_yaml_comma_string(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_HIDE_LOGIN_FEATURES", raising=False)
        self._write(
            tmp_path,
            'instance:\n  name: Acme\n  hide_login_features: "mcp, memory"\n',
        )
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_hidden_login_features() == frozenset({"mcp", "memory"})
        mod._instance_config = None


class TestCustomScripts:
    """instance.custom_scripts — operator-injected HTML/JS blocks rendered
    by base.html. Validates the normalization + filtering done by
    get_custom_scripts() so the template can iterate over a clean list."""

    def _reload(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")
        import importlib
        import app.instance_config as mod
        mod._instance_config = None
        importlib.reload(mod)
        return mod

    def _write(self, tmp_path, yaml_body: str):
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "instance.yaml").write_text(yaml_body)

    def test_yaml_absent_returns_empty_list(self, tmp_path, monkeypatch):
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_custom_scripts() == []
        mod._instance_config = None

    def test_valid_entry_normalized(self, tmp_path, monkeypatch):
        self._write(tmp_path, (
            "instance:\n"
            "  name: Acme\n"
            "  custom_scripts:\n"
            "    - name: marker-io\n"
            "      enabled: true\n"
            "      placement: head_end\n"
            "      html: |\n"
            "        <script>window.markerConfig={project:'abc'};</script>\n"
        ))
        mod = self._reload(tmp_path, monkeypatch)
        scripts = mod.get_custom_scripts()
        assert len(scripts) == 1
        s = scripts[0]
        assert s["name"] == "marker-io"
        assert s["enabled"] is True
        assert s["placement"] == "head_end"
        assert "markerConfig" in s["html"]
        mod._instance_config = None

    def test_disabled_entry_dropped(self, tmp_path, monkeypatch):
        self._write(tmp_path, (
            "instance:\n"
            "  name: Acme\n"
            "  custom_scripts:\n"
            "    - name: off\n"
            "      enabled: false\n"
            "      placement: head_end\n"
            "      html: <script>1</script>\n"
        ))
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_custom_scripts() == []
        mod._instance_config = None

    def test_empty_html_dropped(self, tmp_path, monkeypatch):
        self._write(tmp_path, (
            "instance:\n"
            "  name: Acme\n"
            "  custom_scripts:\n"
            "    - name: noop\n"
            "      enabled: true\n"
            "      placement: head_end\n"
            "      html: '   '\n"
        ))
        mod = self._reload(tmp_path, monkeypatch)
        assert mod.get_custom_scripts() == []
        mod._instance_config = None

    def test_bad_placement_dropped_with_warning(self, tmp_path, monkeypatch, caplog):
        self._write(tmp_path, (
            "instance:\n"
            "  name: Acme\n"
            "  custom_scripts:\n"
            "    - name: typo\n"
            "      enabled: true\n"
            "      placement: body_start\n"
            "      html: <script>1</script>\n"
        ))
        mod = self._reload(tmp_path, monkeypatch)
        import logging
        with caplog.at_level(logging.WARNING, logger="app.instance_config"):
            assert mod.get_custom_scripts() == []
        assert any("unknown placement" in r.message for r in caplog.records)
        mod._instance_config = None

    def test_missing_placement_defaults_to_head_end(self, tmp_path, monkeypatch):
        self._write(tmp_path, (
            "instance:\n"
            "  name: Acme\n"
            "  custom_scripts:\n"
            "    - name: defaulting\n"
            "      enabled: true\n"
            "      html: <script>x</script>\n"
        ))
        mod = self._reload(tmp_path, monkeypatch)
        scripts = mod.get_custom_scripts()
        assert len(scripts) == 1
        assert scripts[0]["placement"] == "head_end"
        mod._instance_config = None

    def test_three_placements_all_pass_through(self, tmp_path, monkeypatch):
        self._write(tmp_path, (
            "instance:\n"
            "  name: Acme\n"
            "  custom_scripts:\n"
            "    - {name: a, enabled: true, placement: head_start, html: '<script>1</script>'}\n"
            "    - {name: b, enabled: true, placement: head_end,   html: '<script>2</script>'}\n"
            "    - {name: c, enabled: true, placement: body_end,   html: '<script>3</script>'}\n"
        ))
        mod = self._reload(tmp_path, monkeypatch)
        scripts = mod.get_custom_scripts()
        assert [s["placement"] for s in scripts] == ["head_start", "head_end", "body_end"]
        assert [s["name"] for s in scripts] == ["a", "b", "c"]
        mod._instance_config = None

    def test_non_list_value_ignored_with_warning(self, tmp_path, monkeypatch, caplog):
        self._write(tmp_path, (
            "instance:\n"
            "  name: Acme\n"
            "  custom_scripts: not-a-list\n"
        ))
        mod = self._reload(tmp_path, monkeypatch)
        import logging
        with caplog.at_level(logging.WARNING, logger="app.instance_config"):
            assert mod.get_custom_scripts() == []
        assert any("must be a list" in r.message for r in caplog.records)
        mod._instance_config = None

    @pytest.mark.parametrize("enabled_yaml,expect_dropped", [
        # Boolean false in every YAML truthy-shape the operator might use.
        # All of these must drop the entry so the kill switch behaves the
        # same regardless of whether the operator pasted a quoted block.
        ("false",   True),
        ("False",   True),
        ('"false"', True),  # quoted string — bool("false") == True in Python
        ('"no"',    True),
        ('"NO"',    True),
        ('"off"',   True),
        ('"0"',     True),
        ("0",       True),
        # Boolean true / typical live values must keep the entry alive.
        ("true",    False),
        ("True",    False),
        ('"true"',  False),
        ('"yes"',   False),
        ("1",       False),
    ])
    def test_enabled_coercion(self, tmp_path, monkeypatch, enabled_yaml, expect_dropped):
        """Quoted-string + numeric `enabled` values must be coerced the same
        way the operator expects from a Boolean field — the kill switch is
        the whole point of the field, and `bool("false") == True` would
        silently leave the snippet live (review PR #372)."""
        self._write(tmp_path, (
            "instance:\n"
            "  name: Acme\n"
            "  custom_scripts:\n"
            f"    - name: probe\n"
            f"      enabled: {enabled_yaml}\n"
            "      placement: head_end\n"
            "      html: <script>1</script>\n"
        ))
        mod = self._reload(tmp_path, monkeypatch)
        scripts = mod.get_custom_scripts()
        if expect_dropped:
            assert scripts == [], (
                f"enabled={enabled_yaml!r} should drop the entry but it survived"
            )
        else:
            assert len(scripts) == 1, (
                f"enabled={enabled_yaml!r} should keep the entry but it was dropped"
            )
        mod._instance_config = None
