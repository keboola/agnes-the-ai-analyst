"""`da disk-info` — show snapshot dir disk usage (spec §4.3)."""

import json as json_lib
import os
import shutil
from pathlib import Path
import typer

disk_info_app = typer.Typer(help="Show snapshot disk usage")


def _local_dir() -> Path:
    return Path(os.environ.get("DA_LOCAL_DIR", ".")).resolve()


def _format_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


@disk_info_app.callback(invoke_without_command=True)
def disk_info(
    ctx: typer.Context,
    json: bool = typer.Option(False, "--json"),
):
    """Show snapshots disk usage."""
    if ctx.invoked_subcommand is not None:
        return
    snap_dir = _local_dir() / "user" / "snapshots"
    used = sum(p.stat().st_size for p in snap_dir.rglob("*") if p.is_file()) if snap_dir.exists() else 0
    count = len(list(snap_dir.glob("*.parquet"))) if snap_dir.exists() else 0
    free = shutil.disk_usage(snap_dir).free if snap_dir.exists() else 0
    quota_gb = int(os.environ.get("AGNES_SNAPSHOT_QUOTA_GB", "10"))

    if json:
        typer.echo(json_lib.dumps({
            "snapshots_dir": str(snap_dir),
            "used_bytes": used, "snapshot_count": count,
            "free_bytes": free, "quota_gb": quota_gb,
        }))
        return

    typer.echo(f"Snapshots dir:    {snap_dir}")
    typer.echo(f"Used by Agnes:    {_format_size(used)} across {count} snapshots")
    typer.echo(f"Free disk:        {_format_size(free)}")
    typer.echo(f"Configured cap:   {quota_gb} GB (set AGNES_SNAPSHOT_QUOTA_GB to override)")
