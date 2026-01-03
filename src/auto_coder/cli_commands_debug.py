import re
import sys
import os
import click
from .auth_utils import get_github_token
from .github_client import GitHubClient
from .util.github_action import _get_playwright_artifact_logs
from .logger_config import setup_logger

@click.command()
@click.option("--github-action-log-summary", help="GitHub Action Run URL to summarize", required=False)
def debug(github_action_log_summary: str) -> None:
    """Debug utilities for Auto-Coder."""
    # Route logs to stderr
    setup_logger(stream=sys.stderr)
    
    if github_action_log_summary:
        # Ensure GitHub token is available and client is initialized
        token = get_github_token()
        if not token:
            click.echo("Error: GitHub token not found. Please set GITHUB_TOKEN or run 'gh auth login'.", err=True)
            sys.exit(1)
            
        # Initialize GitHubClient
        GitHubClient.get_instance(token=token)

        url = github_action_log_summary
        # Parse URL
        # Pattern: github.com/{owner}/{repo}/actions/runs/{run_id}
        match = re.search(r"github\.com/([^/]+)/([^/]+)/actions/runs/(\d+)", url)
        if not match:
            click.echo("Invalid GitHub Action Run URL format. Expected: https://github.com/owner/repo/actions/runs/run_id", err=True)
            sys.exit(1)

        owner, repo, run_id = match.groups()
        repo_name = f"{owner}/{repo}"
        
        click.echo(f"Fetching Playwright summary for Run ID: {run_id} in {repo_name}...", err=True)
        
        try:
            # 1. Get Playwright Artifact Logs
            summary, artifacts = _get_playwright_artifact_logs(repo_name, int(run_id))
            
            final_output = []
            playwright_summary_printed = False
            
            # 2. Get other failed job logs
            # We need to list jobs to find other failures (like eslint)
            try:
                from .util.github_action import get_ghapi_client, get_github_actions_logs_from_url
                
                api = get_ghapi_client(token)
                jobs_data = api.actions.list_jobs_for_workflow_run(owner=owner, repo=repo, run_id=int(run_id))
                jobs = jobs_data.get("jobs", [])
                
                # Sort jobs by workflow definition if possible
                jobs = _sort_jobs_by_workflow(jobs, owner, repo, int(run_id), token)
                
                for job in jobs:
                    if job.get("conclusion") == "failure":
                        job_name = job.get("name", "").lower()
                        job_id = job.get("id")
                        html_url = job.get("html_url")
                        
                        is_playwright = "playwright" in job_name or "e2e" in job_name
                        
                        if is_playwright:
                            if summary:
                                if not playwright_summary_printed:
                                    final_output.append(summary)
                                    playwright_summary_printed = True
                                continue
                            # If no summary, proceed to fetch logs normally
                            
                        # Fetch logs for this job
                        click.echo(f"Fetching logs for failed job: {job.get('name')}...", err=True)
                        if html_url:
                            job_log = get_github_actions_logs_from_url(html_url)
                            if job_log:
                                final_output.append(f"\n\n{job_log}")
                
                # Fallback: if summary exists but wasn't printed (e.g. e2e job not found in failed list?), print it at end
                if summary and not playwright_summary_printed:
                     final_output.append(summary)
                                 
            except Exception as e:
                click.echo(f"Warning: Failed to fetch other job logs: {e}", err=True)
            
            if final_output:
                click.echo("--- LLM Summary Start ---")
                click.echo("\n".join(final_output))
                click.echo("--- LLM Summary End ---")
            else:
                click.echo("No logs found.", err=True)
                sys.exit(1)
                 
        except Exception as e:
             click.echo(f"Error fetching summary: {e}", err=True)
             sys.exit(1)
    else:
        click.echo(click.get_current_context().get_help())


def _sort_jobs_by_workflow(jobs: list, owner: str, repo: str, run_id: int, token: str) -> list:
    """Sort jobs based on the order defined in the workflow file."""
    try:
        import yaml
        from .util.github_action import get_ghapi_client
        api = get_ghapi_client(token)
        
        # Get run details to find workflow file path
        run = api.actions.get_workflow_run(owner, repo, run_id)
        workflow_path = run.get("path") # e.g. .github/workflows/ci.yml
        
        if not workflow_path:
            return jobs
            
        # Check if file exists locally
        # We assume the user is running this in the repo 
        # (or at least has access to the workflow file we care about)
        if not os.path.exists(workflow_path):
             # Try absolute path from workspace root if implied
             possible_path = os.path.join(os.getcwd(), workflow_path)
             if os.path.exists(possible_path):
                 workflow_path = possible_path
             else:
                 # Check if path starts with .github, maybe we are in root
                 if workflow_path.startswith(".github"):
                     if os.path.exists(workflow_path):
                         pass
                     else:
                         return jobs
                 else:
                     return jobs

        with open(workflow_path, "r") as f:
            workflow_data = yaml.safe_load(f)
            
        if not workflow_data or "jobs" not in workflow_data:
            return jobs
            
        # Create map of job name/key to index
        job_order = {}
        for idx, (job_key, job_def) in enumerate(workflow_data["jobs"].items()):
            # Map key
            job_order[job_key] = idx
            # Map name if present
            if isinstance(job_def, dict) and "name" in job_def:
                job_order[job_def["name"]] = idx

        # Sort jobs
        def get_sort_index(job):
            name = job.get("name")
            # Try exact match
            if name in job_order:
                return job_order[name]
            
            # Try clean match (sometimes API returns "Job / Key" or similar?)
            # Usually API 'name' is the 'name' property or key.
            # But for matrix, it might be "test (3.11)".
            # Let's try to match start of string if not exact?
            # Or splitting by parentheses.
            
            # Check against keys
            for key, idx in job_order.items():
                if name == key:
                    return idx
                # Basic fuzzy match: if key is word-bounded in name?
                # e.g. "e2e-test" in "e2e-test / chrome"
                if key in name: 
                     return idx
            
            return 9999

        return sorted(jobs, key=get_sort_index)
        
    except Exception as e:
        click.echo(f"Warning: Failed to sort jobs by workflow: {e}", err=True)
        return jobs
