import re
from typing import Any, Dict, List, Optional

from .logger_config import get_logger

logger = get_logger(__name__)


def extract_linked_issues_from_pr_body(pr_body: str) -> List[int]:
    """Extract issue numbers from PR body using GitHub's linking keywords.

    Supports keywords: close, closes, closed, fix, fixes, fixed, resolve, resolves, resolved
    Formats: #123, owner/repo#123

    Args:
        pr_body: PR description/body text

    Returns:
        List of issue numbers found in the PR body
    """
    if not pr_body:
        return []

    # GitHub's supported keywords for linking issues
    # Added "Related issue(s)" as requested
    keywords = r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved|related issue|related issues)"

    # Pattern to match: keyword #123 or keyword owner/repo#123
    # We allow an optional colon after the keyword (e.g. "Related issue: #123")
    pattern = rf"{keywords}:?\s+(?:[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)?#(\d+)"

    matches = re.finditer(pattern, pr_body, re.IGNORECASE)
    issue_numbers = [int(m.group(1)) for m in matches]

    # Remove duplicates while preserving order
    seen = set()
    unique_issues = []
    for num in issue_numbers:
        if num not in seen:
            seen.add(num)
            unique_issues.append(num)

    return unique_issues


def validate_issue_references(pr_body: str, github_client: Any, repo_name: str) -> None:
    """Validate that issue references in PR body point to Issues, not PRs.

    Args:
        pr_body: The body text of the PR or comment.
        github_client: GitHubClient instance.
        repo_name: Repository name (owner/repo).

    Raises:
        ValueError: If a referenced number points to a Pull Request instead of an Issue.
    """
    if not pr_body or not github_client:
        return

    issue_numbers = extract_linked_issues_from_pr_body(pr_body)

    for issue_number in issue_numbers:
        try:
            # Fetch the issue/PR object
            # In GitHub API, PRs are Issues, so get_issue works for both.
            # If it's a PR, it will have a 'pull_request' key.
            issue = github_client.get_issue(repo_name, issue_number)

            if issue and "pull_request" in issue:
                raise ValueError(f"Reference #{issue_number} points to a Pull Request, but should refer to an Issue.")

        except ValueError:
            raise
        except Exception as e:
            # Log warning but don't block if API fails?
            # The prompt says "output an error" if it IS a PR.
            # If we can't verify, maybe we should warn but allow proceed?
            # For now, let's assume we proceed unless we DEFINITELY know it's a PR.
            logger.warning(f"Failed to validate reference #{issue_number}: {e}")


def get_linked_issues_context(github_client: Any, repo_name: str, pr_body: str) -> str:
    """Extract linked issues from PR body and fetch their details (including parent issues)."""
    if not github_client or not pr_body:
        return ""

    linked_issues_context = ""
    try:
        linked_issues = extract_linked_issues_from_pr_body(pr_body)
        context_parts = []

        for issue_number in linked_issues:
            try:
                # Fetch linked issue details
                issue = github_client.get_issue(repo_name, issue_number)
                if issue:
                    context_parts.append(f"Linked Issue #{issue_number}: {issue.title}")
                    context_parts.append(f"Issue Description:\n{issue.body}")

                    # Check for parent issue
                    try:
                        parent_details = github_client.get_parent_issue_details(repo_name, issue_number)
                        if parent_details:
                            parent_number = parent_details.get("number")
                            parent_body = github_client.get_parent_issue_body(repo_name, issue_number)
                            if parent_body:
                                context_parts.append(f"Parent Issue #{parent_number} (of #{issue_number}): {parent_details.get('title', 'Unknown')}")
                                context_parts.append(f"Parent Issue Description:\n{parent_body}")
                    except Exception as e:
                        logger.warning(f"Failed to fetch parent issue for #{issue_number}: {e}")
            except Exception as e:
                logger.warning(f"Failed to fetch details for linked issue #{issue_number}: {e}")

        if context_parts:
            linked_issues_context = "Linked Issues Context:\n" + "\n\n".join(context_parts)

    except Exception as e:
        logger.warning(f"Failed to fetch linked issues context: {e}")

    return linked_issues_context
