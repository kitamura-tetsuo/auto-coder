"""CLI interface."""

import json
import sys
from typing import Optional

import click
from loguru import logger

from .github_api import GitHubSubIssueAPI
from .logger_config import setup_logger


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging")
@click.option("--repo", "-R", help="Repository name (owner/repo format)")
@click.pass_context
def main(ctx: click.Context, verbose: bool, repo: Optional[str]) -> None:
    """CLI tool for operating GitHub sub-issues functionality."""
    setup_logger(verbose)
    ctx.ensure_object(dict)
    ctx.obj["repo"] = repo
    ctx.obj["verbose"] = verbose


@main.command()
@click.argument("parent")
@click.argument("sub_issues", nargs=-1, required=True)
@click.option("--replace-parent", is_flag=True, help="Replace existing parent")
@click.pass_context
def add(ctx: click.Context, parent: str, sub_issues: tuple[str, ...], replace_parent: bool) -> None:
    """Add existing issue as sub-issue.

    PARENT: Parent issue number or URL
    SUB_ISSUES: Sub-issue number(s) or URL(s) to add (can be specified multiple times)
    """
    try:
        api = GitHubSubIssueAPI(repo=ctx.obj["repo"])

        successful = 0
        failed = 0

        for sub_issue in sub_issues:
            try:
                result = api.add_sub_issue(parent, sub_issue, replace_parent)

                parent_info = result.get("data", {}).get("addSubIssue", {}).get("issue", {})
                sub_issue_info = result.get("data", {}).get("addSubIssue", {}).get("subIssue", {})

                click.echo(f"✅ Added sub-issue #{sub_issue_info.get('number')} to parent #{parent_info.get('number')}")
                click.echo(f"   Parent: {parent_info.get('title')}")
                click.echo(f"   Sub-issue: {sub_issue_info.get('title')}")
                successful += 1
            except Exception as e:
                logger.error(f"Failed to add sub-issue {sub_issue}: {e}")
                failed += 1
                continue

        click.echo(f"\n✅ Successfully added {successful} sub-issue(s).")
        if failed > 0:
            click.echo(f"❌ Failed to add {failed} sub-issue(s).")
            sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to add sub-issues: {e}")
        sys.exit(1)


@main.command()
@click.option("--parent", "-p", required=True, help="Parent issue number or URL")
@click.option("--title", "-t", required=True, help="Issue title")
@click.option("--body", "-b", help="Issue body")
@click.option("--body-file", help="Path to file containing issue body")
@click.option("--label", "-l", multiple=True, help="Label (can be specified multiple times)")
@click.option("--assignee", "-a", multiple=True, help="User to assign (can be specified multiple times)")
@click.pass_context
def create(
    ctx: click.Context,
    parent: str,
    title: str,
    body: Optional[str],
    body_file: Optional[str],
    label: tuple[str, ...],
    assignee: tuple[str, ...],
) -> None:
    """Create a new sub-issue.

    Creates a new issue linked to a parent issue.
    """
    try:
        api = GitHubSubIssueAPI(repo=ctx.obj["repo"])
        result = api.create_sub_issue(
            parent,
            title,
            body=body,
            labels=list(label) if label else None,
            assignees=list(assignee) if assignee else None,
            body_file=body_file,
        )

        click.echo(f"✅ Created sub-issue #{result['number']}: {result['title']}")
        click.echo(f"   URL: {result['url']}")
    except Exception as e:
        logger.error(f"Failed to create sub-issue: {e}")
        sys.exit(1)


@main.command(name="list")
@click.argument("parent")
@click.option("--state", "-s", type=click.Choice(["open", "closed", "all"], case_sensitive=False), default="open", help="Filter state")
@click.option("--json-output", "--json", is_flag=True, help="Output in JSON format")
@click.pass_context
def list_command(ctx: click.Context, parent: str, state: str, json_output: bool) -> None:
    """Display list of sub-issues.

    PARENT: Parent issue number or URL
    """
    try:
        api = GitHubSubIssueAPI(repo=ctx.obj["repo"])
        state_upper = state.upper()
        sub_issues = api.list_sub_issues(parent, state_upper)
        
        if json_output:
            click.echo(json.dumps(sub_issues, indent=2, ensure_ascii=False))
        else:
            if not sub_issues:
                click.echo(f"No {state} sub-issues found.")
                return
            
            click.echo(f"\n📋 Sub-issues ({len(sub_issues)} {state}):")
            click.echo("─" * 80)
            
            for si in sub_issues:
                state_icon = "✅" if si["state"] == "CLOSED" else "🔵"
                assignees = si.get("assignees", {}).get("nodes", [])
                assignee_str = ""
                if assignees:
                    assignee_logins = [a["login"] for a in assignees]
                    assignee_str = f" @{', @'.join(assignee_logins)}"
                
                click.echo(f"{state_icon} #{si['number']}  {si['title']:<50} [{si['state'].lower()}]{assignee_str}")
            
            click.echo()
    except Exception as e:
        logger.error(f"Failed to list sub-issues: {e}")
        sys.exit(1)


@main.command()
@click.argument("parent")
@click.argument("sub_issues", nargs=-1, required=True)
@click.option("--force", "-f", is_flag=True, help="Skip confirmation")
@click.pass_context
def remove(ctx: click.Context, parent: str, sub_issues: tuple[str, ...], force: bool) -> None:
    """Remove sub-issue.

    PARENT: Parent issue number or URL
    SUB_ISSUES: Sub-issue number(s) or URL(s) to remove (can be specified multiple times)
    """
    try:
        if not force:
            click.echo(f"⚠️  {len(sub_issues)} sub-issue(s) will be deleted:")
            for si in sub_issues:
                click.echo(f"   - {si}")

            if not click.confirm("Continue?"):
                click.echo("Cancelled.")
                return
        
        api = GitHubSubIssueAPI(repo=ctx.obj["repo"])
        
        for sub_issue in sub_issues:
            try:
                result = api.remove_sub_issue(parent, sub_issue)
                sub_issue_info = result.get("data", {}).get("removeSubIssue", {}).get("subIssue", {})
                click.echo(f"✅ Removed sub-issue #{sub_issue_info.get('number')}: {sub_issue_info.get('title')}")
            except Exception as e:
                logger.error(f"Failed to remove sub-issue {sub_issue}: {e}")
                continue

        click.echo(f"\n✅ Deleted {len(sub_issues)} sub-issue(s).")
    except Exception as e:
        logger.error(f"Failed to remove sub-issues: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

