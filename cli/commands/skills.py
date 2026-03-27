"""Skills command — da skills. Knowledge base for AI agents."""

from pathlib import Path

import typer

skills_app = typer.Typer(help="Built-in knowledge base for AI agents")

SKILLS_DIR = Path(__file__).parent.parent / "skills"


@skills_app.command("list")
def list_skills():
    """List available skills."""
    if not SKILLS_DIR.exists():
        typer.echo("No skills directory found.")
        return
    for f in sorted(SKILLS_DIR.glob("*.md")):
        name = f.stem
        # Read first line as description
        first_line = f.read_text().split("\n")[0].strip("# ").strip()
        typer.echo(f"  {name:25s} {first_line}")


@skills_app.command("show")
def show_skill(name: str = typer.Argument(..., help="Skill name to display")):
    """Display a skill's content."""
    skill_file = SKILLS_DIR / f"{name}.md"
    if not skill_file.exists():
        typer.echo(f"Skill '{name}' not found. Run: da skills list", err=True)
        raise typer.Exit(1)
    typer.echo(skill_file.read_text())
