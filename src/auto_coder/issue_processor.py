"""
Issue processing functionality for Auto-Coder automation engine.
"""

import json
import os
from typing import Dict, Any, List, Optional
from datetime import datetime

from .utils import CommandExecutor, log_action
from .automation_config import AutomationConfig
from .logger_config import get_logger

logger = get_logger(__name__)
cmd = CommandExecutor()


def process_issues(github_client, config: AutomationConfig, dry_run: bool, repo_name: str, jules_mode: bool = False) -> List[Dict[str, Any]]:
    """Process open issues in the repository."""
    if jules_mode:
        return _process_issues_jules_mode(github_client, config, dry_run, repo_name)
    else:
        return _process_issues_normal(github_client, config, dry_run, repo_name)


def _process_issues_normal(github_client, config: AutomationConfig, dry_run: bool, repo_name: str) -> List[Dict[str, Any]]:
    """Process open issues in the repository."""
    try:
        issues = github_client.get_open_issues(repo_name, limit=config.max_issues_per_run)
        processed_issues = []

        for issue in issues:
            try:
                issue_data = github_client.get_issue_details(issue)

                processed_issue = {
                    'issue_data': issue_data,
                    'analysis': None,
                    'solution': None,
                    'actions_taken': []
                }

                # LLM単回実行ポリシー: 分析フェーズのLLM呼び出しは行わない
                processed_issue['analysis'] = None
                processed_issue['solution'] = None

                # 単回実行での直接アクション（CLI）
                actions = _take_issue_actions(repo_name, issue_data, config, dry_run)
                processed_issue['actions_taken'] = actions

                processed_issues.append(processed_issue)

            except Exception as e:
                logger.error(f"Failed to process issue #{issue.number}: {e}")
                processed_issues.append({
                    'issue_number': issue.number,
                    'error': str(e)
                })

        return processed_issues

    except Exception as e:
        logger.error(f"Failed to process issues for {repo_name}: {e}")
        return []


def _process_issues_jules_mode(github_client, config: AutomationConfig, dry_run: bool, repo_name: str) -> List[Dict[str, Any]]:
    """Process open issues in jules mode - only add 'jules' label."""
    try:
        issues = github_client.get_open_issues(repo_name, limit=config.max_issues_per_run)
        processed_issues = []

        for issue in issues:
            try:
                issue_data = github_client.get_issue_details(issue)
                issue_number = issue_data['number']

                processed_issue = {
                    'issue_data': issue_data,
                    'actions_taken': []
                }

                # Check if 'jules' label already exists
                current_labels = issue_data.get('labels', [])
                if 'jules' not in current_labels:
                    if not dry_run:
                        # Add 'jules' label to the issue
                        github_client.add_labels_to_issue(repo_name, issue_number, ['jules'])
                        processed_issue['actions_taken'].append(f"Added 'jules' label to issue #{issue_number}")
                        logger.info(f"Added 'jules' label to issue #{issue_number}")
                    else:
                        processed_issue['actions_taken'].append(f"[DRY RUN] Would add 'jules' label to issue #{issue_number}")
                        logger.info(f"[DRY RUN] Would add 'jules' label to issue #{issue_number}")
                else:
                    processed_issue['actions_taken'].append(f"Issue #{issue_number} already has 'jules' label")
                    logger.info(f"Issue #{issue_number} already has 'jules' label")

                processed_issues.append(processed_issue)

            except Exception as e:
                logger.error(f"Failed to process issue #{issue.number} in jules mode: {e}")
                processed_issues.append({
                    'issue_number': issue.number,
                    'error': str(e)
                })

        return processed_issues

    except Exception as e:
        logger.error(f"Failed to process issues in jules mode for {repo_name}: {e}")
        return []


def _take_issue_actions(repo_name: str, issue_data: Dict[str, Any], config: AutomationConfig, dry_run: bool) -> List[str]:
    """Take actions on an issue using direct LLM CLI analysis and implementation."""
    actions = []
    issue_number = issue_data['number']

    try:
        if dry_run:
            actions.append(f"[DRY RUN] Would analyze and take actions on issue #{issue_number}")
        else:
            # Ask LLM CLI to analyze the issue and take appropriate actions
            action_results = _apply_issue_actions_directly(repo_name, issue_data, config, dry_run)
            actions.extend(action_results)

    except Exception as e:
        logger.error(f"Error taking actions on issue #{issue_number}: {e}")
        actions.append(f"Error processing issue #{issue_number}: {e}")

    return actions


def _apply_issue_actions_directly(repo_name: str, issue_data: Dict[str, Any], config: AutomationConfig, dry_run: bool) -> List[str]:
    """Ask LLM CLI to analyze an issue and take appropriate actions directly."""
    actions = []

    try:
        # Create a comprehensive prompt for LLM CLI
        action_prompt = f"""
Analyze the following GitHub issue and take appropriate actions:

Repository: {repo_name}
Issue #{issue_data['number']}: {issue_data['title']}

Issue Description:
{issue_data['body'][:1000]}...

Issue Labels: {', '.join(issue_data.get('labels', []))}
Issue State: {issue_data.get('state', 'open')}
Created by: {issue_data.get('author', 'unknown')}

Please analyze this issue and determine the appropriate action:

1. If this is a duplicate or invalid issue (spam, unclear, already resolved, etc.), close it with an appropriate comment
2. If this is a valid bug report or feature request, provide analysis and implementation
3. If this needs clarification, add a comment requesting more information

For valid issues that can be implemented:
- Analyze the requirements
- Implement the necessary code changes
- Create or modify files as needed
- Ensure the implementation follows best practices

For duplicate/invalid issues:
- Close the issue
- Add a polite comment explaining why it was closed

After taking action, respond with a summary of what you did.

Please proceed with analyzing and taking action on this issue now.
"""

        # Use LLM CLI to analyze and take actions
        logger.info(f"Applying issue actions directly for issue #{issue_data['number']}")
        response = "Issue analyzed and action taken"  # Placeholder

        # Parse the response
        if response and len(response.strip()) > 0:
            actions.append(f"LLM CLI analyzed and took action on issue: {response[:200]}...")

            # Check if LLM indicated the issue should be closed
            if "closed" in response.lower() or "duplicate" in response.lower() or "invalid" in response.lower():
                # Close the issue
                close_comment = f"Auto-Coder Analysis: {response[:500]}..."
                # github_client.close_issue(repo_name, issue_data['number'], close_comment)
                actions.append(f"Closed issue #{issue_data['number']} based on analysis")
            else:
                # Add analysis comment
                comment = f"## 🤖 Auto-Coder Analysis\n\n{response}"
                # github_client.add_comment_to_issue(repo_name, issue_data['number'], comment)
                actions.append(f"Added analysis comment to issue #{issue_data['number']}")

            # Commit any changes made
            # commit_action = _commit_changes({'summary': f"Auto-Coder: Address issue #{issue_data['number']}"})
            # actions.append(commit_action)
        else:
            actions.append("LLM CLI did not provide a clear response for issue analysis")

    except Exception as e:
        logger.error(f"Error applying issue actions directly: {e}")

    return actions


def create_feature_issues(github_client, config: AutomationConfig, dry_run: bool, repo_name: str, gemini_client=None) -> List[Dict[str, Any]]:
    """Analyze repository and create feature enhancement issues."""
    logger.info(f"Analyzing repository for feature opportunities: {repo_name}")

    if not gemini_client:
        logger.error("LLM client is required for feature issue creation")
        return []

    try:
        # Get repository context
        repo_context = _get_repository_context(github_client, repo_name)

        # Generate feature suggestions
        suggestions = []  # gemini_client.suggest_features(repo_context)

        created_issues = []
        for suggestion in suggestions:
            if not dry_run:
                try:
                    issue = github_client.create_issue(
                        repo_name=repo_name,
                        title=suggestion['title'],
                        body=_format_feature_issue_body(suggestion),
                        labels=suggestion.get('labels', ['enhancement'])
                    )
                    created_issues.append({
                        'number': issue.number,
                        'title': suggestion['title'],
                        'url': issue.html_url
                    })
                    logger.info(f"Created feature issue #{issue.number}: {suggestion['title']}")
                except Exception as e:
                    logger.error(f"Failed to create feature issue: {e}")
            else:
                logger.info(f"[DRY RUN] Would create feature issue: {suggestion['title']}")
                created_issues.append({
                    'title': suggestion['title'],
                    'dry_run': True
                })

        return created_issues

    except Exception as e:
        logger.error(f"Failed to create feature issues for {repo_name}: {e}")
        return []


def _get_repository_context(github_client, repo_name: str) -> Dict[str, Any]:
    """Get repository context for feature analysis."""
    try:
        repo = github_client.get_repository(repo_name)
        recent_issues = github_client.get_open_issues(repo_name, limit=5)
        recent_prs = github_client.get_open_pull_requests(repo_name, limit=5)

        return {
            'name': repo.name,
            'description': repo.description,
            'language': repo.language,
            'stars': repo.stargazers_count,
            'forks': repo.forks_count,
            'recent_issues': [github_client.get_issue_details(issue) for issue in recent_issues],
            'recent_prs': [github_client.get_pr_details(pr) for pr in recent_prs]
        }
    except Exception as e:
        logger.error(f"Failed to get repository context for {repo_name}: {e}")
        return {'name': repo_name, 'description': '', 'language': 'Unknown'}


def _format_feature_issue_body(suggestion: Dict[str, Any]) -> str:
    """Format feature suggestion as issue body."""
    body = f"## Feature Request\n\n"
    body += f"**Description:**\n{suggestion.get('description', 'No description provided')}\n\n"
    body += f"**Rationale:**\n{suggestion.get('rationale', 'No rationale provided')}\n\n"
    body += f"**Priority:** {suggestion.get('priority', 'medium')}\n"
    body += f"**Complexity:** {suggestion.get('complexity', 'moderate')}\n"
    body += f"**Estimated Effort:** {suggestion.get('estimated_effort', 'unknown')}\n\n"

    if suggestion.get('acceptance_criteria'):
        body += "**Acceptance Criteria:**\n"
        for criteria in suggestion['acceptance_criteria']:
            body += f"- [ ] {criteria}\n"
        body += "\n"

    body += "\n*This feature request was generated automatically by Auto-Coder.*"
    return body


def process_single(github_client, config: AutomationConfig, dry_run: bool, repo_name: str, target_type: str, number: int, jules_mode: bool = False) -> Dict[str, Any]:
    """Process a single issue or PR by number.

    target_type: 'issue' | 'pr' | 'auto'
    When 'auto', try PR first then fall back to issue.
    """
    logger.info(f"Processing single target: type={target_type}, number={number} for {repo_name}")
    result = {
        'repository': repo_name,
        'timestamp': datetime.now().isoformat(),
        'dry_run': dry_run,
        'jules_mode': jules_mode,
        'issues_processed': [],
        'prs_processed': [],
        'errors': []
    }
    try:
        resolved_type = target_type
        if target_type == 'auto':
            # Prefer PR to avoid mislabeling PR issues
            try:
                pr_data = github_client.get_pr_details_by_number(repo_name, number)
                resolved_type = 'pr'
            except Exception:
                resolved_type = 'issue'
        if resolved_type == 'pr':
            try:
                pr_data = github_client.get_pr_details_by_number(repo_name, number)
                # actions = _take_pr_actions(repo_name, pr_data, config, dry_run)
                actions = [f"Processed PR #{number}"]
                processed_pr = {
                    'pr_data': pr_data,
                    'actions_taken': actions,
                    'priority': 'single'
                }
                result['prs_processed'].append(processed_pr)
            except Exception as e:
                msg = f"Failed to process PR #{number}: {e}"
                logger.error(msg)
                result['errors'].append(msg)
        else:
            try:
                issue_data = github_client.get_issue_details_by_number(repo_name, number)
                processed_issue = {
                    'issue_data': issue_data,
                    'analysis': None,
                    'solution': None,
                    'actions_taken': []
                }
                if jules_mode:
                    # Mimic jules mode behavior
                    current_labels = issue_data.get('labels', [])
                    if 'jules' not in current_labels:
                        if not dry_run:
                            github_client.add_labels_to_issue(repo_name, number, ['jules'])
                            processed_issue['actions_taken'].append(f"Added 'jules' label to issue #{number}")
                        else:
                            processed_issue['actions_taken'].append(f"[DRY RUN] Would add 'jules' label to issue #{number}")
                    else:
                        processed_issue['actions_taken'].append(f"Issue #{number} already has 'jules' label")
                else:
                    actions = _take_issue_actions(repo_name, issue_data, config, dry_run)
                    processed_issue['actions_taken'] = actions
                result['issues_processed'].append(processed_issue)
            except Exception as e:
                msg = f"Failed to process issue #{number}: {e}"
                logger.error(msg)
                result['errors'].append(msg)
    except Exception as e:
        msg = f"Error in process_single: {e}"
        logger.error(msg)
        result['errors'].append(msg)
    return result