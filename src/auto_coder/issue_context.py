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
    keywords = r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)"

    # Pattern to match: keyword #123 or keyword owner/repo#123
    # We only extract the issue number, ignoring cross-repo references for now
    pattern = rf"{keywords}\s+(?:[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)?#(\d+)"

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
