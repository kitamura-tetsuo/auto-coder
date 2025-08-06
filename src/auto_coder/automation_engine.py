"""
Automation engine for Auto-Coder.
"""

import logging
from typing import Dict, Any, List, Optional
import json
import os
import subprocess
from datetime import datetime
from dataclasses import dataclass

from .github_client import GitHubClient
from .gemini_client import GeminiClient
from .config import settings

logger = logging.getLogger(__name__)


@dataclass
class AutomationConfig:
    """Configuration constants for automation engine."""

    # File paths
    REPORTS_DIR: str = "reports"
    TEST_SCRIPT_PATH: str = "scripts/test.sh"

    # Limits
    MAX_PR_DIFF_SIZE: int = 2000
    MAX_PROMPT_SIZE: int = 1000
    MAX_RESPONSE_SIZE: int = 200
    MAX_FIX_ATTEMPTS: int = 3

    # Git settings
    MAIN_BRANCH: str = "main"

    # GitHub CLI merge options
    MERGE_METHOD: str = "--squash"
    MERGE_AUTO: bool = True


@dataclass
class CommandResult:
    """Result of a command execution."""
    success: bool
    stdout: str
    stderr: str
    returncode: int


class CommandExecutor:
    """Utility class for executing commands with consistent error handling."""

    # Default timeouts for different command types
    DEFAULT_TIMEOUTS = {
        'git': 120,
        'gh': 60,
        'test': 3600,
        'default': 60
    }

    @classmethod
    def run_command(
        cls,
        cmd: List[str],
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
        check_success: bool = True
    ) -> CommandResult:
        """Run a command with consistent error handling."""
        if timeout is None:
            # Auto-detect timeout based on command type
            cmd_type = cmd[0] if cmd else 'default'
            timeout = cls.DEFAULT_TIMEOUTS.get(cmd_type, cls.DEFAULT_TIMEOUTS['default'])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd
            )

            success = result.returncode == 0 if check_success else True

            return CommandResult(
                success=success,
                stdout=result.stdout,
                stderr=result.stderr,
                returncode=result.returncode
            )

        except subprocess.TimeoutExpired:
            logger.error(f"Command timed out after {timeout}s: {' '.join(cmd)}")
            return CommandResult(
                success=False,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                returncode=-1
            )
        except Exception as e:
            logger.error(f"Command execution failed: {' '.join(cmd)}: {e}")
            return CommandResult(
                success=False,
                stdout="",
                stderr=str(e),
                returncode=-1
            )


class AutomationEngine:
    """Main automation engine that orchestrates GitHub and Gemini integration."""

    def __init__(
        self,
        github_client: GitHubClient,
        gemini_client: GeminiClient,
        dry_run: bool = False,
        config: Optional[AutomationConfig] = None
    ):
        """Initialize automation engine."""
        self.github = github_client
        self.gemini = gemini_client
        self.dry_run = dry_run
        self.config = config or AutomationConfig()
        self.cmd = CommandExecutor()

        # Create reports directory if it doesn't exist
        os.makedirs(self.config.REPORTS_DIR, exist_ok=True)

    def _handle_error(self, operation: str, error: Exception, context: str = "") -> str:
        """Standardized error handling."""
        error_msg = f"Error {operation}"
        if context:
            error_msg += f" for {context}"
        error_msg += f": {error}"

        logger.error(error_msg)
        return error_msg

    def _log_action(self, action: str, success: bool = True, details: str = "") -> str:
        """Standardized action logging."""
        level = logging.INFO if success else logging.ERROR
        message = action
        if details:
            message += f": {details}"

        logger.log(level, message)
        return message
    
    def run(self, repo_name: str) -> Dict[str, Any]:
        """Run the main automation process."""
        logger.info(f"Starting automation for repository: {repo_name}")
        
        results = {
            'repository': repo_name,
            'timestamp': datetime.now().isoformat(),
            'dry_run': self.dry_run,
            'issues_processed': [],
            'prs_processed': [],
            'errors': []
        }
        
        try:
            # Process issues
            issues_result = self._process_issues(repo_name)
            results['issues_processed'] = issues_result
            
            # Process pull requests
            prs_result = self._process_pull_requests(repo_name)
            results['prs_processed'] = prs_result
            
            # Save results report
            self._save_report(results, f"automation_report_{repo_name.replace('/', '_')}")
            
            logger.info(f"Automation completed for {repo_name}")
            return results
            
        except Exception as e:
            error_msg = f"Automation failed for {repo_name}: {e}"
            logger.error(error_msg)
            results['errors'].append(error_msg)
            return results
    
    def create_feature_issues(self, repo_name: str) -> List[Dict[str, Any]]:
        """Analyze repository and create feature enhancement issues."""
        logger.info(f"Analyzing repository for feature opportunities: {repo_name}")
        
        try:
            # Get repository context
            repo_context = self._get_repository_context(repo_name)
            
            # Generate feature suggestions
            suggestions = self.gemini.suggest_features(repo_context)
            
            created_issues = []
            for suggestion in suggestions:
                if not self.dry_run:
                    try:
                        issue = self.github.create_issue(
                            repo_name=repo_name,
                            title=suggestion['title'],
                            body=self._format_feature_issue_body(suggestion),
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
            
            # Save feature suggestions report
            report = {
                'repository': repo_name,
                'timestamp': datetime.now().isoformat(),
                'suggestions': suggestions,
                'created_issues': created_issues
            }
            self._save_report(report, f"feature_suggestions_{repo_name.replace('/', '_')}")
            
            return created_issues
            
        except Exception as e:
            logger.error(f"Failed to create feature issues for {repo_name}: {e}")
            return []
    
    def _process_issues(self, repo_name: str) -> List[Dict[str, Any]]:
        """Process open issues in the repository."""
        try:
            issues = self.github.get_open_issues(repo_name, limit=settings.max_issues_per_run)
            processed_issues = []

            for issue in issues:
                try:
                    issue_data = self.github.get_issue_details(issue)

                    processed_issue = {
                        'issue_data': issue_data,
                        'actions_taken': []
                    }

                    # Take automated actions using direct Gemini CLI
                    actions = self._take_issue_actions(repo_name, issue_data)
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
    
    def _process_pull_requests(self, repo_name: str) -> List[Dict[str, Any]]:
        """Process open pull requests in the repository."""
        try:
            prs = self.github.get_open_pull_requests(repo_name, limit=settings.max_prs_per_run)
            processed_prs = []

            for pr in prs:
                try:
                    pr_data = self.github.get_pr_details(pr)

                    processed_pr = {
                        'pr_data': pr_data,
                        'actions_taken': []
                    }

                    # Take automated actions using direct Gemini CLI
                    actions = self._take_pr_actions(repo_name, pr_data)
                    processed_pr['actions_taken'] = actions

                    processed_prs.append(processed_pr)

                except Exception as e:
                    logger.error(f"Failed to process PR #{pr.number}: {e}")
                    processed_prs.append({
                        'pr_number': pr.number,
                        'error': str(e)
                    })

            return processed_prs

        except Exception as e:
            logger.error(f"Failed to process PRs for {repo_name}: {e}")
            return []
    
    def _take_issue_actions(self, repo_name: str, issue_data: Dict[str, Any]) -> List[str]:
        """Take actions on an issue using direct Gemini CLI analysis and implementation."""
        actions = []
        issue_number = issue_data['number']

        try:
            if self.dry_run:
                actions.append(f"[DRY RUN] Would analyze and take actions on issue #{issue_number}")
            else:
                # Ask Gemini CLI to analyze the issue and take appropriate actions
                action_results = self._apply_issue_actions_directly(repo_name, issue_data)
                actions.extend(action_results)

        except Exception as e:
            logger.error(f"Error taking actions on issue #{issue_number}: {e}")
            actions.append(f"Error processing issue #{issue_number}: {e}")

        return actions

    def _apply_issue_actions_directly(self, repo_name: str, issue_data: Dict[str, Any]) -> List[str]:
        """Ask Gemini CLI to analyze an issue and take appropriate actions directly."""
        actions = []

        try:
            # Create a comprehensive prompt for Gemini CLI
            action_prompt = f"""
Analyze the following GitHub issue and take appropriate actions:

Repository: {repo_name}
Issue #{issue_data['number']}: {issue_data['title']}

Issue Description:
{issue_data['body'][:1000]}...

Issue Labels: {', '.join([label['name'] for label in issue_data.get('labels', [])])}
Issue State: {issue_data.get('state', 'open')}
Created by: {issue_data.get('user', {}).get('login', 'unknown')}

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

            # Use Gemini CLI to analyze and take actions
            logger.info(f"Applying issue actions directly for issue #{issue_data['number']}")
            response = self.gemini._run_gemini_cli(action_prompt)

            # Parse the response
            if response and len(response.strip()) > 0:
                actions.append(f"Gemini CLI analyzed and took action on issue: {response[:200]}...")

                # Check if Gemini indicated the issue should be closed
                if "closed" in response.lower() or "duplicate" in response.lower() or "invalid" in response.lower():
                    # Close the issue
                    close_comment = f"Auto-Coder Analysis: {response[:500]}..."
                    self.github.close_issue(repo_name, issue_data['number'], close_comment)
                    actions.append(f"Closed issue #{issue_data['number']} based on analysis")
                else:
                    # Add analysis comment
                    comment = f"## 🤖 Auto-Coder Analysis\n\n{response}"
                    self.github.add_comment_to_issue(repo_name, issue_data['number'], comment)
                    actions.append(f"Added analysis comment to issue #{issue_data['number']}")

                # Commit any changes made
                commit_action = self._commit_changes({'summary': f"Auto-Coder: Address issue #{issue_data['number']}"})
                actions.append(commit_action)
            else:
                actions.append("Gemini CLI did not provide a clear response for issue analysis")

        except Exception as e:
            logger.error(f"Error applying issue actions directly: {e}")
            actions.append(f"Error applying issue actions: {e}")

        return actions

    def _take_pr_actions(self, repo_name: str, pr_data: Dict[str, Any]) -> List[str]:
        """Take actions on a PR including merge handling and analysis."""
        actions = []
        pr_number = pr_data['number']

        try:
            if self.dry_run:
                actions.append(f"[DRY RUN] Would handle PR merge and analysis for PR #{pr_number}")
                # In dry run, still show what merge actions would be taken
                dry_run_analysis = {'category': 'feature', 'risk_level': 'low'}  # Mock analysis
                merge_actions = self._handle_pr_merge(repo_name, pr_data, dry_run_analysis)
                actions.extend(merge_actions)
            else:
                # First, handle the merge process (GitHub Actions, testing, etc.)
                # This doesn't depend on Gemini analysis
                merge_actions = self._handle_pr_merge(repo_name, pr_data, {})
                actions.extend(merge_actions)

                # If merge process completed successfully (PR was merged), skip analysis
                if any("Successfully merged" in action for action in merge_actions):
                    actions.append(f"PR #{pr_number} was merged, skipping further analysis")
                elif any("skipping to next PR" in action for action in merge_actions):
                    actions.append(f"PR #{pr_number} processing deferred, skipping analysis")
                else:
                    # Only do Gemini analysis if merge process didn't complete
                    analysis_results = self._apply_pr_actions_directly(repo_name, pr_data)
                    actions.extend(analysis_results)

        except Exception as e:
            actions.append(self._handle_error("taking PR actions", e, f"PR #{pr_number}"))

        return actions

    def _apply_pr_actions_directly(self, repo_name: str, pr_data: Dict[str, Any]) -> List[str]:
        """Ask Gemini CLI to analyze a PR and take appropriate actions directly."""
        actions = []

        try:
            # Get PR diff for analysis
            pr_diff = self._get_pr_diff(repo_name, pr_data['number'])

            # Create analysis prompt
            action_prompt = self._create_pr_analysis_prompt(repo_name, pr_data, pr_diff)

            # Use Gemini CLI to analyze and take actions
            self._log_action(f"Applying PR actions directly for PR #{pr_data['number']}")
            response = self.gemini._run_gemini_cli(action_prompt)

            # Process the response
            if response and len(response.strip()) > 0:
                actions.append(f"Gemini CLI analyzed and took action on PR: {response[:self.config.MAX_RESPONSE_SIZE]}...")

                # Check if Gemini indicated the PR should be merged
                if "merged" in response.lower() or "auto-merge" in response.lower():
                    actions.append(f"Auto-merged PR #{pr_data['number']} based on analysis")
                else:
                    # Add analysis comment
                    comment = f"## 🤖 Auto-Coder PR Analysis\n\n{response}"
                    self.github.add_comment_to_issue(repo_name, pr_data['number'], comment)
                    actions.append(f"Added analysis comment to PR #{pr_data['number']}")
            else:
                actions.append("Gemini CLI did not provide a clear response for PR analysis")

        except Exception as e:
            actions.append(self._handle_error("applying PR actions directly", e))

        return actions

    def _get_pr_diff(self, repo_name: str, pr_number: int) -> str:
        """Get PR diff for analysis."""
        try:
            result = self.cmd.run_command(['gh', 'pr', 'diff', str(pr_number), '--repo', repo_name])
            return result.stdout[:self.config.MAX_PR_DIFF_SIZE] if result.success else "Could not retrieve PR diff"
        except Exception:
            return "Could not retrieve PR diff"

    def _create_pr_analysis_prompt(self, repo_name: str, pr_data: Dict[str, Any], pr_diff: str) -> str:
        """Create a comprehensive prompt for PR analysis."""
        return f"""
Analyze the following GitHub Pull Request and take appropriate actions:

Repository: {repo_name}
PR #{pr_data['number']}: {pr_data['title']}

PR Description:
{pr_data['body'][:self.config.MAX_PROMPT_SIZE]}...

PR Author: {pr_data.get('user', {}).get('login', 'unknown')}
PR State: {pr_data.get('state', 'open')}
Draft: {pr_data.get('draft', False)}
Mergeable: {pr_data.get('mergeable', False)}

PR Changes (first {self.config.MAX_PR_DIFF_SIZE} chars):
{pr_diff}

Please analyze this PR and determine the appropriate action:

1. Analyze the code changes for:
   - Risk level (low/medium/high)
   - Category (bugfix/feature/documentation/dependency/etc.)
   - Code quality and best practices
   - Potential issues or improvements

2. Based on the analysis, take appropriate action:
   - For low-risk, well-written changes (bugfixes, documentation, dependencies): Consider auto-merging
   - For higher-risk or complex changes: Add analysis comment only
   - For problematic PRs: Add feedback and suggestions

3. Auto-merge criteria (all must be true):
   - Low risk level
   - Category is bugfix, documentation, or dependency
   - Code follows best practices
   - No obvious issues
   - Not a draft PR
   - Is mergeable

If auto-merging, use: gh pr merge {pr_data['number']} --repo {repo_name} {self.config.MERGE_METHOD}

After taking action, respond with a summary of what you did and why.

Please proceed with analyzing and taking action on this PR now.
"""

    def _check_github_actions_status(self, repo_name: str, pr_data: Dict[str, Any]) -> Dict[str, Any]:
        """Check GitHub Actions status for a PR."""
        pr_number = pr_data['number']

        try:
            # Use gh CLI to get PR status checks (text output)
            result = self.cmd.run_command(['gh', 'pr', 'checks', str(pr_number)], check_success=False)

            # Note: gh pr checks returns non-zero exit code when some checks fail
            # This is expected behavior, not an error
            if result.returncode != 0 and not result.stdout.strip():
                # Only treat as error if there's no output (real failure)
                self._log_action(f"Failed to get PR checks for #{pr_number}", False, result.stderr)
                return {
                    'success': False,
                    'error': f"Failed to get PR checks: {result.stderr}",
                    'checks': []
                }

            # Parse text output to extract check information
            checks_output = result.stdout.strip()
            if not checks_output:
                # No checks found, assume success
                return {
                    'success': True,
                    'checks': [],
                    'failed_checks': [],
                    'total_checks': 0
                }

            # Parse the text output
            checks = []
            failed_checks = []
            all_passed = True
            has_in_progress = False

            lines = checks_output.split('\n')
            for line in lines:
                line = line.strip()
                if not line:
                    continue

                # Check if this is tab-separated format (newer gh CLI)
                if '\t' in line:
                    # Format: name\tstatus\ttime\turl
                    parts = line.split('\t')
                    if len(parts) >= 2:
                        name = parts[0].strip()
                        status = parts[1].strip().lower()
                        url = parts[3].strip() if len(parts) > 3 else ''

                        if status in ['pass', 'success']:
                            checks.append({
                                'name': name,
                                'state': 'completed',
                                'conclusion': 'success'
                            })
                        elif status in ['fail', 'failure', 'error']:
                            all_passed = False
                            check_info = {
                                'name': name,
                                'state': 'completed',
                                'conclusion': 'failure'
                            }
                            checks.append(check_info)
                            failed_checks.append({
                                'name': name,
                                'conclusion': 'failure',
                                'details_url': url
                            })
                        elif status in ['skipping', 'skipped', 'pending', 'in_progress']:
                            # Check for in-progress status
                            if status in ['pending', 'in_progress']:
                                has_in_progress = True
                                all_passed = False
                            # Don't count skipped checks as failures
                            elif status not in ['skipping', 'skipped']:
                                all_passed = False
                            check_info = {
                                'name': name,
                                'state': 'pending' if status in ['pending', 'in_progress'] else 'skipped',
                                'conclusion': status
                            }
                            checks.append(check_info)
                            if status in ['pending', 'in_progress']:
                                failed_checks.append({
                                    'name': name,
                                    'conclusion': status,
                                    'details_url': url
                                })
                else:
                    # Legacy format: "✓ check-name" or "✗ check-name" or "- check-name"
                    if line.startswith('✓'):
                        # Successful check
                        name = line[2:].strip()
                        checks.append({
                            'name': name,
                            'state': 'completed',
                            'conclusion': 'success'
                        })
                    elif line.startswith('✗'):
                        # Failed check
                        name = line[2:].strip()
                        all_passed = False
                        check_info = {
                            'name': name,
                            'state': 'completed',
                            'conclusion': 'failure'
                        }
                        checks.append(check_info)
                        failed_checks.append({
                            'name': name,
                            'conclusion': 'failure',
                            'details_url': ''
                        })
                    elif line.startswith('-') or line.startswith('○'):
                        # Pending/in-progress check
                        name = line[2:].strip() if line.startswith('-') else line[2:].strip()
                        has_in_progress = True
                        all_passed = False
                        check_info = {
                            'name': name,
                            'state': 'pending',
                            'conclusion': 'pending'
                        }
                        checks.append(check_info)
                        failed_checks.append({
                            'name': name,
                            'conclusion': 'pending',
                            'details_url': ''
                        })

            return {
                'success': all_passed,
                'in_progress': has_in_progress,
                'checks': checks,
                'failed_checks': failed_checks,
                'total_checks': len(checks)
            }

        except Exception as e:
            logger.error(f"Error checking GitHub Actions for PR #{pr_number}: {e}")
            return {
                'success': False,
                'error': str(e),
                'checks': []
            }

    def _get_github_actions_logs(self, repo_name: str, failed_checks: List[Dict[str, Any]]) -> str:
        """Get GitHub Actions logs for failed checks."""
        logs = []

        try:
            # Get the latest workflow runs for this PR
            cmd = ['gh', 'run', 'list', '--limit', '5']

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                # Parse the run list to find failed runs
                lines = result.stdout.strip().split('\n')
                for line in lines[1:]:  # Skip header
                    if 'failure' in line.lower() or 'cancelled' in line.lower():
                        # Extract run ID (first column)
                        parts = line.split('\t')
                        if len(parts) > 0:
                            run_id = parts[0].strip()

                            # Get logs for this run
                            log_cmd = ['gh', 'run', 'view', run_id, '--log-failed']

                            log_result = subprocess.run(
                                log_cmd,
                                capture_output=True,
                                text=True,
                                timeout=120
                            )

                            if log_result.returncode == 0:
                                log_content = log_result.stdout
                                # Extract important error information
                                important_logs = self._extract_important_errors({
                                    'success': False,
                                    'output': log_content,
                                    'errors': ''
                                })

                                logs.append(f"=== Run {run_id} ===\n{important_logs}")
                            break  # Only get logs from the most recent failed run

            # If no logs found from runs, try to get general error info
            if not logs:
                for check in failed_checks:
                    check_name = check.get('name', 'Unknown')
                    conclusion = check.get('conclusion', 'unknown')
                    logs.append(f"=== {check_name} ===\nStatus: {conclusion}\nNo detailed logs available")

        except Exception as e:
            logger.error(f"Error getting GitHub Actions logs: {e}")
            logs.append(f"Error getting logs: {e}")

        return '\n\n'.join(logs) if logs else "No detailed logs available"

    def _handle_pr_merge(self, repo_name: str, pr_data: Dict[str, Any], analysis: Dict[str, Any]) -> List[str]:
        """Handle PR merge process following the intended flow."""
        actions = []
        pr_number = pr_data['number']

        try:
            # Step 1: Check GitHub Actions status
            github_checks = self._check_github_actions_status(repo_name, pr_data)

            # Step 2: Skip if GitHub Actions are still in progress
            if github_checks.get('in_progress', False):
                actions.append(f"GitHub Actions checks are still in progress for PR #{pr_number}, skipping to next PR")
                return actions

            # Step 3: If GitHub Actions passed, merge directly
            if github_checks['success']:
                actions.append(f"All GitHub Actions checks passed for PR #{pr_number}")

                if not self.dry_run:
                    merge_result = self._merge_pr(repo_name, pr_number, analysis)
                    if merge_result:
                        actions.append(f"Successfully merged PR #{pr_number}")
                    else:
                        actions.append(f"Failed to merge PR #{pr_number}")
                else:
                    actions.append(f"[DRY RUN] Would merge PR #{pr_number}")
                return actions

            # Step 4: GitHub Actions failed - checkout PR branch
            failed_checks = github_checks.get('failed_checks', [])
            actions.append(f"GitHub Actions checks failed for PR #{pr_number}: {len(failed_checks)} failed")

            checkout_result = self._checkout_pr_branch(repo_name, pr_data)
            if not checkout_result:
                actions.append(f"Failed to checkout PR #{pr_number} branch")
                return actions

            actions.append(f"Checked out PR #{pr_number} branch")

            # Step 5: Update with latest main branch commits
            update_actions = self._update_with_main_branch(repo_name, pr_data)
            actions.extend(update_actions)

            # Step 6: If main branch update required pushing changes, skip to next PR
            if any("Pushed updated branch" in action for action in update_actions):
                actions.append(f"Updated PR #{pr_number} with main branch, skipping to next PR for GitHub Actions check")
                return actions

            # Step 7: If no main branch updates were needed, the test failures are due to PR content
            # Get GitHub Actions error logs and ask Gemini to fix
            if any("up to date with" in action for action in update_actions):
                actions.append(f"PR #{pr_number} is up to date with main branch, test failures are due to PR content")

                # Fix PR issues using GitHub Actions logs first, then local tests
                if failed_checks:
                    github_logs = self._get_github_actions_logs(repo_name, failed_checks)
                    fix_actions = self._fix_pr_issues_with_testing(repo_name, pr_data, github_logs)
                    actions.extend(fix_actions)
                else:
                    actions.append(f"No specific failed checks found for PR #{pr_number}")
            else:
                # If we reach here, some other update action occurred
                actions.append(f"PR #{pr_number} processing completed")

        except Exception as e:
            actions.append(self._handle_error("handling PR merge", e, f"PR #{pr_number}"))

        return actions

    def _fix_pr_issues_with_testing(self, repo_name: str, pr_data: Dict[str, Any], github_logs: str) -> List[str]:
        """Fix PR issues using GitHub Actions logs first, then local testing loop."""
        actions = []
        pr_number = pr_data['number']

        try:
            # Step 1: Initial fix using GitHub Actions logs
            actions.append(f"Starting PR issue fixing for PR #{pr_number} using GitHub Actions logs")

            initial_fix_actions = self._apply_github_actions_fix(repo_name, pr_data, github_logs)
            actions.extend(initial_fix_actions)

            # Step 2: Local testing and iterative fixing loop
            for attempt in range(self.config.MAX_FIX_ATTEMPTS):
                actions.append(f"Running local tests (attempt {attempt + 1}/{self.config.MAX_FIX_ATTEMPTS})")

                test_result = self._run_pr_tests(repo_name, pr_data)

                if test_result['success']:
                    actions.append(f"Local tests passed on attempt {attempt + 1}")

                    # Commit and push the successful fix
                    if not self.dry_run:
                        commit_result = self._commit_and_push_fix(pr_data, f"Fix PR issues (attempt {attempt + 1})")
                        actions.append(commit_result)
                    else:
                        actions.append(f"[DRY RUN] Would commit and push fix for PR #{pr_number}")

                    break
                else:
                    actions.append(f"Local tests failed on attempt {attempt + 1}")

                    if attempt < self.config.MAX_FIX_ATTEMPTS - 1:
                        # Apply local test failure fix
                        local_fix_actions = self._apply_local_test_fix(repo_name, pr_data, test_result)
                        actions.extend(local_fix_actions)
                    else:
                        actions.append(f"Max fix attempts ({self.config.MAX_FIX_ATTEMPTS}) reached for PR #{pr_number}")

        except Exception as e:
            actions.append(self._handle_error("fixing PR issues with testing", e, f"PR #{pr_number}"))

        return actions

    def _apply_github_actions_fix(self, repo_name: str, pr_data: Dict[str, Any], github_logs: str) -> List[str]:
        """Apply initial fix using GitHub Actions error logs."""
        actions = []
        pr_number = pr_data['number']

        try:
            # Create prompt for GitHub Actions error fix
            fix_prompt = f"""
Fix the following GitHub Actions test failures for PR #{pr_number}:

Repository: {repo_name}
PR Title: {pr_data.get('title', 'Unknown')}

GitHub Actions Error Logs:
{github_logs[:self.config.MAX_PROMPT_SIZE]}

Please analyze the errors and provide specific code fixes to resolve the test failures.
Focus on the root cause of the failures and provide complete, working code solutions.

After analyzing, apply the necessary fixes to the codebase.
"""

            if not self.dry_run:
                response = self.gemini._run_gemini_cli(fix_prompt)
                if response:
                    actions.append(f"Applied GitHub Actions fix: {response[:self.config.MAX_RESPONSE_SIZE]}...")
                else:
                    actions.append("No response from Gemini for GitHub Actions fix")
            else:
                actions.append(f"[DRY RUN] Would apply GitHub Actions fix for PR #{pr_number}")

        except Exception as e:
            actions.append(self._handle_error("applying GitHub Actions fix", e, f"PR #{pr_number}"))

        return actions

    def _apply_local_test_fix(self, repo_name: str, pr_data: Dict[str, Any], test_result: Dict[str, Any]) -> List[str]:
        """Apply fix using local test failure logs."""
        actions = []
        pr_number = pr_data['number']

        try:
            # Extract important error information
            error_summary = self._extract_important_errors(test_result)

            if not error_summary:
                actions.append(f"No actionable errors found in local test output for PR #{pr_number}")
                return actions

            # Create prompt for local test error fix
            fix_prompt = f"""
Fix the following local test failures for PR #{pr_number}:

Repository: {repo_name}
PR Title: {pr_data.get('title', 'Unknown')}

Test Output:
{test_result.get('output', '')[:self.config.MAX_PROMPT_SIZE]}

Test Errors:
{test_result.get('errors', '')[:self.config.MAX_PROMPT_SIZE]}

Key Errors:
{error_summary}

Please analyze the test failures and provide specific code fixes.
Focus on making the tests pass while maintaining code quality.

After analyzing, apply the necessary fixes to the codebase.
"""

            if not self.dry_run:
                response = self.gemini._run_gemini_cli(fix_prompt)
                if response:
                    actions.append(f"Applied local test fix: {response[:self.config.MAX_RESPONSE_SIZE]}...")
                else:
                    actions.append("No response from Gemini for local test fix")
            else:
                actions.append(f"[DRY RUN] Would apply local test fix for PR #{pr_number}")

        except Exception as e:
            actions.append(self._handle_error("applying local test fix", e, f"PR #{pr_number}"))

        return actions

    def _commit_and_push_fix(self, pr_data: Dict[str, Any], commit_message: str) -> str:
        """Commit and push the applied fixes."""
        try:
            # Add all modified files
            add_result = self.cmd.run_command(['git', 'add', '.'])
            if not add_result.success:
                return f"Failed to add files to git: {add_result.stderr}"

            # Commit the changes
            full_commit_message = f"Auto-Coder: {commit_message}"
            commit_result = self.cmd.run_command(['git', 'commit', '-m', full_commit_message])

            if commit_result.success:
                # Push the changes
                push_result = self.cmd.run_command(['git', 'push'])
                if push_result.success:
                    return f"Successfully committed and pushed: {commit_message}"
                else:
                    return f"Committed but failed to push: {push_result.stderr}"
            else:
                # Check if there were no changes to commit
                if 'nothing to commit' in commit_result.stdout:
                    return "No changes to commit"
                else:
                    return f"Failed to commit changes: {commit_result.stderr}"

        except Exception as e:
            return f"Error committing and pushing changes: {e}"

    def _checkout_pr_branch(self, repo_name: str, pr_data: Dict[str, Any]) -> bool:
        """Checkout the PR branch for local testing."""
        pr_number = pr_data['number']

        try:
            result = self.cmd.run_command(['gh', 'pr', 'checkout', str(pr_number)])

            if result.success:
                self._log_action(f"Successfully checked out PR #{pr_number}")
                return True
            else:
                self._log_action(f"Failed to checkout PR #{pr_number}", False, result.stderr)
                return False

        except Exception as e:
            self._handle_error("checking out PR", e, f"#{pr_number}")
            return False

    def _update_with_main_branch(self, repo_name: str, pr_data: Dict[str, Any]) -> List[str]:
        """Update PR branch with latest main branch commits."""
        actions = []
        pr_number = pr_data['number']

        try:
            # Fetch latest changes from origin
            result = self.cmd.run_command(['git', 'fetch', 'origin'])
            if not result.success:
                actions.append(f"Failed to fetch latest changes: {result.stderr}")
                return actions

            # Check if main branch has new commits
            result = self.cmd.run_command(['git', 'rev-list', '--count', f'HEAD..origin/{self.config.MAIN_BRANCH}'])
            if not result.success:
                actions.append(f"Failed to check main branch status: {result.stderr}")
                return actions

            commits_behind = int(result.stdout.strip())
            if commits_behind == 0:
                actions.append(f"PR #{pr_number} is up to date with {self.config.MAIN_BRANCH} branch")
                return actions

            actions.append(f"PR #{pr_number} is {commits_behind} commits behind {self.config.MAIN_BRANCH}, updating...")

            # Try to merge main branch
            result = self.cmd.run_command(['git', 'merge', f'origin/{self.config.MAIN_BRANCH}'])
            if result.success:
                actions.append(f"Successfully merged {self.config.MAIN_BRANCH} branch into PR #{pr_number}")

                # Push the updated branch
                push_result = self.cmd.run_command(['git', 'push'])
                if push_result.success:
                    actions.append(f"Pushed updated branch for PR #{pr_number}")
                else:
                    actions.append(f"Failed to push updated branch: {push_result.stderr}")
            else:
                # Merge conflict occurred, ask Gemini to resolve it
                actions.append(f"Merge conflict detected for PR #{pr_number}, asking Gemini to resolve...")

                # Get conflict information
                conflict_info = self._get_merge_conflict_info()
                merge_actions = self._resolve_merge_conflicts_with_gemini(pr_data, conflict_info)
                actions.extend(merge_actions)

        except Exception as e:
            actions.append(self._handle_error("updating with main branch", e, f"PR #{pr_number}"))

        return actions

    def _get_merge_conflict_info(self) -> str:
        """Get information about merge conflicts."""
        try:
            result = self.cmd.run_command(['git', 'status', '--porcelain'])
            return result.stdout if result.success else "Could not get merge conflict information"
        except Exception as e:
            return f"Error getting conflict info: {e}"

    def _resolve_merge_conflicts_with_gemini(self, pr_data: Dict[str, Any], conflict_info: str) -> List[str]:
        """Ask Gemini CLI to resolve merge conflicts."""
        actions = []

        try:
            # Create a prompt for Gemini CLI to resolve conflicts
            resolve_prompt = f"""
There are merge conflicts when trying to merge main branch into PR #{pr_data['number']}: {pr_data['title']}

PR Description:
{pr_data['body'][:500]}...

Merge Conflict Information:
{conflict_info}

Please resolve these merge conflicts by:
1. Examining the conflicted files
2. Choosing the appropriate resolution for each conflict
3. Staging the resolved files with 'git add'
4. Completing the merge with 'git commit'
5. Pushing the resolved changes

After resolving the conflicts, respond with a summary of what you resolved.

Please proceed with resolving these merge conflicts now.
"""

            # Use Gemini CLI to resolve conflicts
            logger.info(f"Asking Gemini to resolve merge conflicts for PR #{pr_data['number']}")
            response = self.gemini._run_gemini_cli(resolve_prompt)

            # Parse the response
            if response and len(response.strip()) > 0:
                actions.append(f"Gemini CLI resolved merge conflicts: {response[:200]}...")

                # Check if merge was completed
                result = subprocess.run(
                    ['git', 'status', '--porcelain'],
                    capture_output=True,
                    text=True,
                    timeout=30
                )

                if result.returncode == 0 and not result.stdout.strip():
                    actions.append(f"Merge conflicts resolved and committed for PR #{pr_data['number']}")

                    # Push the resolved changes
                    result = subprocess.run(
                        ['git', 'push'],
                        capture_output=True,
                        text=True,
                        timeout=120
                    )

                    if result.returncode == 0:
                        actions.append(f"Pushed resolved merge for PR #{pr_data['number']}")
                    else:
                        actions.append(f"Failed to push resolved merge: {result.stderr}")
                else:
                    actions.append(f"Merge conflicts may not be fully resolved for PR #{pr_data['number']}")
            else:
                actions.append("Gemini CLI did not provide a clear response for merge conflict resolution")

        except Exception as e:
            logger.error(f"Error resolving merge conflicts with Gemini: {e}")
            actions.append(f"Error resolving merge conflicts: {e}")

        return actions

    def _commit_changes(self, fix_suggestion: Dict[str, Any]) -> str:
        """Commit the applied changes to git."""
        try:
            # Add all modified files
            add_result = self.cmd.run_command(['git', 'add', '.'])
            if not add_result.success:
                return f"Failed to add files to git: {add_result.stderr}"

            # Create commit message
            summary = fix_suggestion.get('summary', 'Auto-Coder fix')
            commit_message = f"Auto-Coder: {summary}"

            # Commit the changes
            commit_result = self.cmd.run_command(['git', 'commit', '-m', commit_message])

            if commit_result.success:
                return f"Committed changes: {commit_message}"
            else:
                # Check if there were no changes to commit
                if 'nothing to commit' in commit_result.stdout:
                    return "No changes to commit"
                else:
                    return f"Failed to commit changes: {commit_result.stderr}"

        except Exception as e:
            return f"Error committing changes: {e}"

    def _run_pr_tests(self, repo_name: str, pr_data: Dict[str, Any]) -> Dict[str, Any]:
        """Run tests for a PR and return results."""
        pr_number = pr_data['number']

        try:
            # Check if test script exists
            if not os.path.exists(self.config.TEST_SCRIPT_PATH):
                logger.warning(f"Test script {self.config.TEST_SCRIPT_PATH} not found, skipping tests")
                return {'success': True, 'output': 'No test script found', 'errors': ''}

            # Run the test script
            self._log_action(f"Running tests for PR #{pr_number}")
            result = self.cmd.run_command(['bash', self.config.TEST_SCRIPT_PATH], timeout=self.cmd.DEFAULT_TIMEOUTS['test'])

            self._log_action(f"Test result for PR #{pr_number}: {'PASS' if result.success else 'FAIL'}")

            return {
                'success': result.success,
                'output': result.stdout,
                'errors': result.stderr,
                'return_code': result.returncode
            }

        except Exception as e:
            error_msg = self._handle_error("running tests", e, f"PR #{pr_number}")
            return {
                'success': False,
                'output': '',
                'errors': error_msg,
                'return_code': -1
            }

    def _merge_pr(self, repo_name: str, pr_number: int, analysis: Dict[str, Any]) -> bool:
        """Merge a PR using GitHub CLI."""
        try:
            cmd = ['gh', 'pr', 'merge', str(pr_number)]
            if self.config.MERGE_AUTO:
                cmd.append('--auto')
            cmd.append(self.config.MERGE_METHOD)

            result = self.cmd.run_command(cmd)

            if result.success:
                self._log_action(f"Successfully merged PR #{pr_number}")
                return True
            else:
                self._log_action(f"Failed to merge PR #{pr_number}", False, result.stderr)
                return False

        except Exception as e:
            self._handle_error("merging PR", e, f"#{pr_number}")
            return False



    def _extract_important_errors(self, test_result: Dict[str, Any]) -> str:
        """Extract important error information from test output."""
        if test_result['success']:
            return ""

        errors = test_result.get('errors', '')
        output = test_result.get('output', '')

        # Combine stderr and stdout
        full_output = f"{errors}\n{output}".strip()

        if not full_output:
            return "Tests failed but no error output available"

        # Extract important lines (errors, failures, etc.)
        important_lines = []
        lines = full_output.split('\n')

        # Keywords that indicate important error information
        error_keywords = [
            'error:', 'Error:', 'ERROR:',
            'failed:', 'Failed:', 'FAILED:',
            'exception:', 'Exception:', 'EXCEPTION:',
            'traceback:', 'Traceback:', 'TRACEBACK:',
            'assertion', 'Assertion', 'ASSERTION',
            'syntax error', 'SyntaxError',
            'import error', 'ImportError',
            'module not found', 'ModuleNotFoundError',
            'test failed', 'Test failed', 'TEST FAILED'
        ]

        for i, line in enumerate(lines):
            line_lower = line.lower()

            # Include lines with error keywords
            if any(keyword.lower() in line_lower for keyword in error_keywords):
                # Include some context around error lines
                start = max(0, i - 2)
                end = min(len(lines), i + 3)
                context_lines = lines[start:end]
                important_lines.extend(context_lines)

        # Remove duplicates while preserving order
        seen = set()
        unique_lines = []
        for line in important_lines:
            if line not in seen:
                seen.add(line)
                unique_lines.append(line)

        # Limit output length
        result = '\n'.join(unique_lines)
        if len(result) > 2000:  # Limit to 2000 characters
            result = result[:2000] + "\n... (output truncated)"

        return result if result else "Tests failed but no specific error information found"

    def _get_repository_context(self, repo_name: str) -> Dict[str, Any]:
        """Get repository context for feature analysis."""
        try:
            repo = self.github.get_repository(repo_name)
            recent_issues = self.github.get_open_issues(repo_name, limit=5)
            recent_prs = self.github.get_open_pull_requests(repo_name, limit=5)
            
            return {
                'name': repo.name,
                'description': repo.description,
                'language': repo.language,
                'stars': repo.stargazers_count,
                'forks': repo.forks_count,
                'recent_issues': [self.github.get_issue_details(issue) for issue in recent_issues],
                'recent_prs': [self.github.get_pr_details(pr) for pr in recent_prs]
            }
        except Exception as e:
            logger.error(f"Failed to get repository context for {repo_name}: {e}")
            return {'name': repo_name, 'description': '', 'language': 'Unknown'}
    

    
    def _format_feature_issue_body(self, suggestion: Dict[str, Any]) -> str:
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
    
    def _save_report(self, data: Dict[str, Any], filename: str) -> None:
        """Save report to file."""
        try:
            filepath = os.path.join(self.config.REPORTS_DIR, f"{filename}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self._log_action(f"Report saved to {filepath}")
        except Exception as e:
            self._handle_error("saving report", e, filename)
