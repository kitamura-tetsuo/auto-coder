import json
import subprocess
import sys
from typing import List, Optional

import click


def run_command(command: List[str], check: bool = True, capture_output: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command."""
    try:
        result = subprocess.run(command, check=check, text=True, capture_output=capture_output)
        return result
    except subprocess.CalledProcessError as e:
        click.echo(f"Error executing command: {' '.join(command)}")
        click.echo(f"Error output: {e.stderr}")
        raise e


def get_issue_details(issue_ref: str) -> dict:
    """Get details of an issue using gh cli."""
    cmd = ["gh", "issue", "view", issue_ref, "--json", "title,body,number,url"]
    result = run_command(cmd)
    return json.loads(result.stdout)


def get_sub_issues(issue_ref: str) -> List[dict]:
    """Get sub-issues of an issue using github-sub-issue cli."""
    # First, we need to list sub-issues.
    # github-sub-issue list <ref> --state all --json
    cmd = ["github-sub-issue", "list", issue_ref, "--state", "all", "--json"]
    try:
        result = run_command(cmd)
        sub_issues = json.loads(result.stdout)
        return sub_issues
    except Exception as e:
        click.echo(f"Error fetching sub-issues: {e}")
        # If run_command raised, it already printed error output.
        # But we previously caught everything.
        # Let's verify if run_command failing raises an exception we catch here.
        # run_command raises CalledProcessError if check=True.
        # So we should see it.
        # But if json.loads fails (because output isn't JSON), we catch that too.
        raise e


def create_issue(title: str, body: str, dry_run: bool = False) -> Optional[str]:
    """Create a new issue and return its URL."""
    if dry_run:
        click.echo(f"[DRY RUN] Would create issue: {title}")
        return "https://github.com/example/repo/issues/DRYRUN"

    cmd = ["gh", "issue", "create", "--title", title, "--body", body]
    result = run_command(cmd)
    # gh issue create returns the URL of the created issue on stdout
    return result.stdout.strip()


def add_sub_issues(parent_url: str, child_urls: List[str], dry_run: bool = False):
    """Link sub-issues to a parent."""
    if not child_urls:
        return

    if dry_run:
        click.echo(f"[DRY RUN] Would link sub-issues {child_urls} to {parent_url}")
        return

    cmd = ["github-sub-issue", "add", parent_url] + child_urls
    run_command(cmd)


def clone_recursive(issue_ref: str, dry_run: bool = False, visited: set = None) -> Optional[str]:
    """
    Recursively clone an issue and its sub-issues.
    Returns the URL of the new issue.
    """
    if visited is None:
        visited = set()

    # Avoid infinite recursion if there are cycles (though unlikely in sub-issues DAG)
    if issue_ref in visited:
        click.echo(f"Skipping already visited issue: {issue_ref}")
        return None
    visited.add(issue_ref)

    click.echo(f"Processing issue: {issue_ref}...")

    try:
        details = get_issue_details(issue_ref)
    except subprocess.CalledProcessError:
        click.echo(f"Failed to fetch details for {issue_ref}. Skipping.")
        return None

    title = details.get("title")
    body = details.get("body")
    original_url = details.get("url")

    # update visited with URL to be sure
    visited.add(original_url)
    visited.add(str(details.get("number")))

    # 1. Clone the issue itself
    new_issue_url = create_issue(title, body, dry_run)
    click.echo(f"Created new issue: {new_issue_url}")

    # 2. Find sub-issues
    sub_issues = get_sub_issues(issue_ref)

    new_sub_issue_urls = []
    for sub in sub_issues:
        # sub is a dict, likely containing 'url' or 'number'
        sub_ref = sub.get("url")
        if sub_ref:
            cloned_sub_url = clone_recursive(sub_ref, dry_run, visited)
            if cloned_sub_url:
                new_sub_issue_urls.append(cloned_sub_url)

    # 3. Link new sub-issues to the new parent
    if new_sub_issue_urls:
        click.echo(f"Linking {len(new_sub_issue_urls)} sub-issues to {new_issue_url}...")
        add_sub_issues(new_issue_url, new_sub_issue_urls, dry_run)

    return new_issue_url


@click.command()
@click.argument("issue_refs", nargs=-1, required=True)
@click.option("--dry-run", is_flag=True, help="Simulate execution without creating issues.")
def main(issue_refs: List[str], dry_run: bool):
    """
    Clone GitHub issues and their sub-issues recursively.
    ISSUE_REFS can be issue numbers or URLs.
    """
    for ref in issue_refs:
        click.echo(f"Starting clone for: {ref}")
        clone_recursive(ref, dry_run=dry_run)
        click.echo("-" * 40)


if __name__ == "__main__":
    main()
