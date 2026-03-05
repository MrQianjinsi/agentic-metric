"""Typer CLI for agentic-metric."""

from __future__ import annotations

from importlib.metadata import version as _pkg_version

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    name="agentic-metric",
    help="Monitor token usage and costs across AI coding agents.",
    invoke_without_command=True,
    add_completion=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"agentic-metric {_pkg_version('agentic-metric')}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _default(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-v", callback=_version_callback,
        is_eager=True, help="Show version and exit.",
    ),
) -> None:
    """Launch TUI by default when no command is given."""
    if ctx.invoked_subcommand is None:
        tui()


@app.command()
def tui() -> None:
    """Launch the interactive TUI dashboard."""
    from .tui.app import AgenticMetricApp

    AgenticMetricApp().run()


@app.command()
def status() -> None:
    """Show currently active agent sessions."""
    from .collectors import create_default_registry
    from .pricing import estimate_session_cost

    registry = create_default_registry()
    sessions = [s for s in registry.get_live_sessions()
                if s.user_turns > 0 or s.output_tokens > 0]

    if not sessions:
        console.print("No active agents.")
        return

    table = Table(title="Active Agent Sessions")
    table.add_column("PID", justify="right", style="cyan")
    table.add_column("Agent", style="magenta")
    table.add_column("Project", style="green")
    table.add_column("Model", style="blue")
    table.add_column("Turns", justify="right")
    table.add_column("Messages", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost", justify="right", style="yellow")

    for s in sessions:
        cost = estimate_session_cost(s)
        project = s.project_path.split("/")[-1] if s.project_path else ""
        table.add_row(
            str(s.pid),
            s.agent_type,
            project,
            s.model or "-",
            str(s.user_turns),
            str(s.message_count),
            _fmt_tokens(s.total_tokens),
            f"${cost:.2f}",
        )

    console.print(table)


@app.command()
def today() -> None:
    """Show today's usage overview."""
    from .store.database import Database
    from .store import aggregator
    from .collectors import create_default_registry

    db = Database()
    registry = create_default_registry()
    registry.sync_all(db)

    overview = aggregator.get_today_overview(db)
    today_sessions = aggregator.get_today_sessions(db)
    live_sessions = registry.get_live_sessions()
    aggregator.merge_live_into_overview(overview, live_sessions, today_sessions)
    overview.active_agents = sum(
        1 for s in live_sessions if s.user_turns > 0 or s.output_tokens > 0
    )

    db.close()

    table = Table(title=f"Today's Overview  ({overview.date})")
    table.add_column("Agent", style="magenta")
    table.add_column("Sessions", justify="right")
    table.add_column("Turns", justify="right")
    table.add_column("Messages", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost", justify="right", style="yellow")

    if overview.by_agent:
        for agent, data in overview.by_agent.items():
            total_tokens = data["input_tokens"] + data["output_tokens"]
            table.add_row(
                agent,
                str(data["session_count"]),
                str(data["turns"]),
                str(data["message_count"]),
                _fmt_tokens(total_tokens),
                f"${data['cost']:.2f}",
            )
        table.add_section()

    active_str = f" ([green]{overview.active_agents} active[/green])" if overview.active_agents else ""
    table.add_row(
        "[bold]Total[/bold]",
        f"[bold]{overview.session_count}[/bold]{active_str}",
        f"[bold]{overview.tool_call_count}[/bold]",
        f"[bold]{overview.message_count}[/bold]",
        f"[bold]{_fmt_tokens(overview.total_tokens)}[/bold]",
        f"[bold yellow]${overview.estimated_cost_usd:.2f}[/bold yellow]",
    )

    console.print(table)


@app.command()
def history(
    days: int = typer.Option(30, "--days", "-d", help="Number of days to show."),
) -> None:
    """Show daily usage trends."""
    from .store.database import Database
    from .store import aggregator
    from .collectors import create_default_registry

    db = Database()
    registry = create_default_registry()
    registry.sync_all(db)

    trends = aggregator.get_daily_trends(db, days=days)

    # Merge live data into today's row
    from datetime import datetime
    today_str = datetime.now().strftime("%Y-%m-%d")
    overview = aggregator.get_today_overview(db)
    today_sessions = aggregator.get_today_sessions(db)
    live_sessions = registry.get_live_sessions()
    aggregator.merge_live_into_overview(overview, live_sessions, today_sessions)
    db.close()

    if not trends and not overview.session_count:
        console.print("No history data available.")
        return

    table = Table(title=f"Daily Trends (last {days} days)")
    table.add_column("Date", style="cyan")
    table.add_column("Sessions", justify="right")
    table.add_column("Turns", justify="right")
    table.add_column("Messages", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost", justify="right", style="yellow")

    for t in trends:
        if t.date == today_str:
            # Use live-merged overview for today
            table.add_row(
                t.date,
                str(overview.session_count),
                str(overview.tool_call_count),
                str(overview.message_count),
                _fmt_tokens(overview.total_tokens),
                f"${overview.estimated_cost_usd:.2f}",
            )
        else:
            table.add_row(
                t.date,
                str(t.session_count),
                str(t.user_turns),
                str(t.message_count),
                _fmt_tokens(t.total_tokens),
                f"${t.estimated_cost_usd:.2f}",
            )

    console.print(table)


def _fmt_tokens(n: int) -> str:
    """Format token count for compact display: 1234 -> 1.2K, 1234567 -> 1.2M."""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


@app.command()
def bar() -> None:
    """Print a one-line summary for status bars (i3blocks, waybar, etc.)."""
    import sys
    from .store.database import Database
    from .store import aggregator
    from .collectors import create_default_registry

    try:
        db = Database()
        registry = create_default_registry()
        registry.sync_all(db)
        overview = aggregator.get_today_overview(db)
        today_sessions = aggregator.get_today_sessions(db)
        live_sessions = registry.get_live_sessions()
        aggregator.merge_live_into_overview(overview, live_sessions, today_sessions)
        db.close()
    except Exception:
        print("AM: --")
        sys.exit(0)

    all_tokens = (overview.input_tokens + overview.output_tokens
                   + overview.cache_read_tokens + overview.cache_creation_tokens)
    tokens = _fmt_tokens(all_tokens)
    cost = f"${overview.estimated_cost_usd:.2f}"
    # full_text
    print(f"AM: {cost} | {tokens}")
    # short_text
    print(f"AM: {cost}")


@app.command()
def sync() -> None:
    """Force sync all collectors to the database."""
    from .store.database import Database
    from .collectors import create_default_registry

    db = Database()
    registry = create_default_registry()

    console.print("Syncing all collectors...")
    registry.sync_all(db)
    db.close()

    console.print("[green]Sync complete.[/green]")
    console.print(f"  Collectors: {len(registry.get_all())}")
    for c in registry.get_all():
        console.print(f"    - {c.agent_type}")
