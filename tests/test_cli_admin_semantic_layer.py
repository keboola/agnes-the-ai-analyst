

def test_every_agnes_command_this_module_suggests_is_runnable():
    """Devin Review on #1248: the empty-state hint printed a flag that does not exist.

    It told the operator to run `agnes admin connection secret --id <connection>
    --kind master`; `connection_id` is a POSITIONAL argument there, so copying
    the line fails immediately with "No such option: --id". A suggestion the
    reader cannot run is worse than none — they conclude the feature is broken.

    Checked structurally: each `agnes …` command printed from a `typer.echo`
    in this module is resolved against the real Typer app, and every `--flag`
    it carries must be a real option of the command it targets.
    """
    import inspect
    import pathlib
    import re

    from cli.main import app

    src = (pathlib.Path(__file__).resolve().parents[1] / "cli" / "commands" / "admin_semantic_layer.py").read_text(
        encoding="utf-8"
    )
    printed = re.findall(r'typer\.echo\(\s*[fr]?"([^"]*)"', src)
    suggestions = [m.group(1) for line in printed for m in re.finditer(r"agnes ([a-z][^\"]*)", line)]
    assert suggestions, "no printed `agnes …` suggestions found — re-point this guard"

    def resolve(tokens):
        node = app
        for tok in tokens:
            groups = {g.name: g.typer_instance for g in getattr(node, "registered_groups", []) if g.name}
            cmds = {c.name: c for c in getattr(node, "registered_commands", []) if c.name}
            if tok in cmds:
                return cmds[tok]
            if tok in groups:
                node = groups[tok]
            else:
                return None
        return None

    checked = 0
    for raw in suggestions:
        parts = raw.split()
        words = [p for p in parts if not p.startswith("-") and not p.startswith("<")]
        cmd = resolve(words)
        if cmd is None:
            continue  # prose, not a command path
        checked += 1
        real_flags = {"--help"}
        for prm in inspect.signature(cmd.callback).parameters.values():
            real_flags.update(
                d for d in (getattr(prm.default, "param_decls", None) or []) if isinstance(d, str) and d.startswith("-")
            )
        for flag in (p for p in parts if p.startswith("--")):
            assert flag in real_flags, (
                f"suggested command `agnes {raw.strip()}` uses {flag}, which "
                f"`{' '.join(words)}` does not accept (real: {sorted(real_flags)})"
            )

    assert checked, "the guard resolved no commands — it would pass against any hint"


def test_a_registry_gap_is_not_reported_as_a_definition_defect():
    """Devin Review on #1248: `foreign_alias_reference` covered two causes.

    A metric whose joined table is simply not registered in Agnes was filed
    under "blocked by their own definition", telling the admin that
    registering a table would not help — when that is exactly the fix. The
    registry-caused failures now carry their own reason, which is NOT in
    `DEFINITION_BLOCKED_REASONS`.
    """
    from connectors.keboola.semantic_layer import DEFINITION_BLOCKED_REASONS

    assert "unresolved_joined_table" not in DEFINITION_BLOCKED_REASONS
    assert "foreign_alias_reference" in DEFINITION_BLOCKED_REASONS


def test_the_new_reason_still_lands_in_the_published_counter():
    """The per-source counters are a stable surface — the split must not add one."""
    import inspect

    from connectors.keboola import semantic_layer

    src = inspect.getsource(semantic_layer)
    assert 'skip_reason in ("foreign_alias_reference", "unresolved_joined_table")' in src
    assert "skipped_unresolved_joined_table" not in src, "a new counter key leaked into the payload"


def test_the_token_mismatch_strip_stays_hidden_when_empty():
    """`display:flex` beats the UA sheet's `[hidden] { display: none }`, so an
    empty orange band rendered under every Keboola connection card."""
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"
    ).read_text(encoding="utf-8")
    assert ".ds-token-mismatch[hidden]" in src
    assert src.index(".ds-token-mismatch[hidden]") < src.index(".ds-token-mismatch {"), (
        "the hidden rule must not be overridden by the later display:flex"
    )


class TestTheStorageTokenReadHonoursTheAllowlist:
    """Devin Review on #1248: a new read path skipped the gate.

    An admin can set `token_env` to ANY name, so an ungated
    `os.environ.get(token_env)` turns a connection row into a way to read an
    arbitrary host environment variable — the value is then sent to the
    configured stack as a Storage token, and here its project identity is
    rendered back onto the admin page. `/test` and `/tables` gate the same
    read behind `is_token_env_allowed` with an explicit SECURITY comment.
    """

    def test_a_disallowed_token_env_is_not_read(self, monkeypatch):
        from connectors.keboola.semantic_layer import _connection_storage_token

        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-never-be-read")
        monkeypatch.setattr(
            "src.orchestrator_security.get_allowed_token_envs", lambda: {"KEBOOLA_STORAGE_TOKEN"}
        )
        token = _connection_storage_token({"id": "c1", "token_env": "AWS_SECRET_ACCESS_KEY"})
        assert token == "", "an arbitrary host env var was read as a Storage token"

    def test_an_allowed_token_env_is_read(self, monkeypatch):
        from connectors.keboola.semantic_layer import _connection_storage_token

        monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "legit")
        monkeypatch.setattr(
            "src.orchestrator_security.get_allowed_token_envs", lambda: {"KEBOOLA_STORAGE_TOKEN"}
        )
        assert _connection_storage_token({"id": "c1", "token_env": "KEBOOLA_STORAGE_TOKEN"}) == "legit"

    def test_the_gate_is_the_shared_one(self):
        """Not a re-implementation — the allowlist lives in one place."""
        import inspect

        from connectors.keboola import semantic_layer

        body = inspect.getsource(semantic_layer._connection_storage_token)
        assert "is_token_env_allowed" in body
