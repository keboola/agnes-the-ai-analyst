

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
