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
    # Added "Related issue(s)", "Relates to", "Related to"
    keywords = r"(?:close|closes|closed|closing|fix|fixes|fixed|resolve|resolves|resolved|resolving|related issue|related issues|relates to|related to|relate to)"

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


def extract_associated_issue_numbers(
    pr_data: Optional[Dict[str, Any]] = None,
    pr_body: str = "",
    pr_title: str = "",
    branch_name: str = "",
) -> List[int]:
    """Extract associated issue numbers hierarchically from PR body, title, branch, or metadata.

    Hierarchical selection priority:
    1. Explicit linking keywords in PR body (Fixes #123, Closes #123, etc.)
    2. Explicit issue references in PR title (e.g. 'feat: implement #123')
    3. Branch name issue pattern (e.g. 'issue-123-fix', 'feat/issue-123')
    4. General issue references in PR body (e.g. 'Issue #123')

    Args:
        pr_data: Optional PR data dictionary
        pr_body: PR description/body text
        pr_title: PR title text
        branch_name: Branch name

    Returns:
        List of authoritative candidate issue numbers
    """
    body_text = pr_body
    title_text = pr_title
    branch_text = branch_name
    current_pr_number = None

    if pr_data:
        body_text = body_text or (pr_data.get("body") or "")
        title_text = title_text or (pr_data.get("title") or "")
        branch_text = branch_text or (pr_data.get("head", {}).get("ref") or "")
        current_pr_number = pr_data.get("number")

    # Tier 1: Explicit linking keywords in body (authoritative)
    if body_text:
        explicit_issues = extract_linked_issues_from_pr_body(body_text)
        filtered_explicit = [n for n in explicit_issues if n != current_pr_number]
        if filtered_explicit:
            return filtered_explicit

    # Tier 2: Issue reference in PR title
    if title_text:
        title_matches = re.finditer(r"(?:issue|fix|fixes|close|closes|resolve|resolves)?[-_:\s]*#(\d+)", title_text, re.IGNORECASE)
        title_numbers = [int(m.group(1)) for m in title_matches]
        title_issue_matches = re.finditer(r"\bissue[-_\s]+(\d+)\b", title_text, re.IGNORECASE)
        for m in title_issue_matches:
            title_numbers.append(int(m.group(1)))

        filtered_title = []
        seen_title = set()
        for num in title_numbers:
            if num != current_pr_number and num not in seen_title:
                seen_title.add(num)
                filtered_title.append(num)
        if filtered_title:
            return filtered_title

    # Tier 3: Issue reference in branch name
    if branch_text:
        branch_numbers = []
        branch_matches = re.finditer(r"(?:issue|feat|fix)[-_/](\d+)", branch_text, re.IGNORECASE)
        for m in branch_matches:
            branch_numbers.append(int(m.group(1)))

        direct_branch_match = re.match(r"^(\d+)[-_]", branch_text)
        if direct_branch_match:
            branch_numbers.append(int(direct_branch_match.group(1)))

        filtered_branch = []
        seen_branch = set()
        for num in branch_numbers:
            if num != current_pr_number and num not in seen_branch:
                seen_branch.add(num)
                filtered_branch.append(num)
        if filtered_branch:
            return filtered_branch

    # Tier 4: General issue reference fallback in body
    if body_text:
        body_matches = re.finditer(r"(?:issue|task)[-_:\s]+#?(\d+)", body_text, re.IGNORECASE)
        body_fallback_numbers = []
        seen_fallback = set()
        for m in body_matches:
            num = int(m.group(1))
            if num != current_pr_number and num not in seen_fallback:
                seen_fallback.add(num)
                body_fallback_numbers.append(num)
        if body_fallback_numbers:
            return body_fallback_numbers

    return []


def get_linked_issues_context(
    github_client: Any,
    repo_name: str,
    pr_body: str = "",
    pr_data: Optional[Dict[str, Any]] = None,
) -> str:
    """Extract linked issues from PR body/metadata and fetch their details (including parent issues)."""
    if not github_client:
        return ""

    linked_issues_context = ""
    try:
        linked_issues = extract_associated_issue_numbers(pr_data=pr_data, pr_body=pr_body)
        context_parts = []

        for issue_number in linked_issues:
            try:
                # Fetch linked issue details
                issue = github_client.get_issue(repo_name, issue_number)
                if issue:
                    # Make sure it's an actual issue, not a PR
                    if isinstance(issue, dict) and issue.get("pull_request"):
                        continue
                    if hasattr(issue, "_rawData") and isinstance(issue._rawData, dict) and issue._rawData.get("pull_request"):
                        continue

                    title = issue.get("title") if isinstance(issue, dict) else getattr(issue, "title", "Unknown")
                    body = issue.get("body") if isinstance(issue, dict) else getattr(issue, "body", "")

                    context_parts.append(f"Linked Issue #{issue_number}: {title}")
                    context_parts.append(f"Issue Description:\n{body}")

                    # Check for parent issue
                    try:
                        parent_details = github_client.get_parent_issue_details(repo_name, issue_number)
                        if parent_details:
                            parent_number = parent_details.get("number")
                            parent_body = github_client.get_parent_issue_body(repo_name, issue_number)
                            if parent_body:
                                context_parts.append(
                                    f"Parent Issue #{parent_number} (CONTEXT ONLY - Parent of #{issue_number}): {parent_details.get('title', 'Unknown')}\n"
                                    f"[SCOPE BOUNDARY NOTICE: The following parent issue description is provided for background context only. "
                                    f"The implementation scope and acceptance criteria for this PR are defined strictly by the child issue #{issue_number}. "
                                    f"Do NOT require parent requirements outside the child issue scope.]\n"
                                    f"Parent Issue Description:\n{parent_body}"
                                )
                    except Exception as e:
                        logger.warning(f"Failed to fetch parent issue for #{issue_number}: {e}")
            except Exception as e:
                logger.warning(f"Failed to fetch details for linked issue #{issue_number}: {e}")

        if context_parts:
            linked_issues_context = "Linked Issues Context:\n" + "\n\n".join(context_parts)

    except Exception as e:
        logger.warning(f"Failed to fetch linked issues context: {e}")

    return linked_issues_context
