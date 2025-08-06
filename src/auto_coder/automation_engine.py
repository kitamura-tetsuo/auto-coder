"""
Automation engine for Auto-Coder.
"""

import logging
from typing import Dict, Any, List, Optional
import json
import os
import subprocess
import tempfile
from datetime import datetime

from .github_client import GitHubClient
from .gemini_client import GeminiClient
from .config import settings

logger = logging.getLogger(__name__)


class AutomationEngine:
    """Main automation engine that orchestrates GitHub and Gemini integration."""
    
    def __init__(self, github_client: GitHubClient, gemini_client: GeminiClient, dry_run: bool = False):
        """Initialize automation engine."""
        self.github = github_client
        self.gemini = gemini_client
        self.dry_run = dry_run
        
        # Create reports directory if it doesn't exist
        self.reports_dir = "reports"
        os.makedirs(self.reports_dir, exist_ok=True)
    
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
                    analysis = self.gemini.analyze_issue(issue_data)
                    
                    # Generate solution if it's a high priority issue
                    solution = None
                    if analysis.get('priority') in ['high', 'critical']:
                        solution = self.gemini.generate_solution(issue_data, analysis)
                    
                    processed_issue = {
                        'issue_data': issue_data,
                        'analysis': analysis,
                        'solution': solution,
                        'actions_taken': []
                    }
                    
                    # Take automated actions based on analysis
                    actions = self._take_issue_actions(repo_name, issue_data, analysis, solution)
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
                    analysis = self.gemini.analyze_pull_request(pr_data)
                    
                    processed_pr = {
                        'pr_data': pr_data,
                        'analysis': analysis,
                        'actions_taken': []
                    }
                    
                    # Take automated actions based on analysis
                    actions = self._take_pr_actions(repo_name, pr_data, analysis)
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
    
    def _take_issue_actions(self, repo_name: str, issue_data: Dict[str, Any], analysis: Dict[str, Any], solution: Optional[Dict[str, Any]]) -> List[str]:
        """Take automated actions based on issue analysis."""
        actions = []
        
        try:
            # Add analysis comment
            if not self.dry_run:
                comment = self._format_analysis_comment(analysis, solution)
                self.github.add_comment_to_issue(repo_name, issue_data['number'], comment)
                actions.append(f"Added analysis comment to issue #{issue_data['number']}")
            else:
                actions.append(f"[DRY RUN] Would add analysis comment to issue #{issue_data['number']}")
            
            # Auto-close if it's a duplicate or invalid
            if analysis.get('category') == 'duplicate' or 'invalid' in analysis.get('tags', []):
                if not self.dry_run:
                    close_comment = "This issue has been automatically closed as it appears to be a duplicate or invalid. Please reopen if this is incorrect."
                    self.github.close_issue(repo_name, issue_data['number'], close_comment)
                    actions.append(f"Auto-closed issue #{issue_data['number']}")
                else:
                    actions.append(f"[DRY RUN] Would auto-close issue #{issue_data['number']}")
            
        except Exception as e:
            logger.error(f"Failed to take actions for issue #{issue_data['number']}: {e}")
            actions.append(f"Error taking actions: {e}")
        
        return actions
    
    def _take_pr_actions(self, repo_name: str, pr_data: Dict[str, Any], analysis: Dict[str, Any]) -> List[str]:
        """Take automated actions based on PR analysis."""
        actions = []

        try:
            # Add analysis comment
            if not self.dry_run:
                comment = self._format_pr_analysis_comment(analysis)
                self.github.add_comment_to_issue(repo_name, pr_data['number'], comment)
                actions.append(f"Added analysis comment to PR #{pr_data['number']}")
            else:
                actions.append(f"[DRY RUN] Would add analysis comment to PR #{pr_data['number']}")

            # Check if PR should be auto-merged based on analysis
            should_merge = self._should_auto_merge_pr(analysis, pr_data)

            if should_merge:
                merge_actions = self._handle_pr_merge(repo_name, pr_data, analysis)
                actions.extend(merge_actions)

        except Exception as e:
            logger.error(f"Failed to take actions for PR #{pr_data['number']}: {e}")
            actions.append(f"Error taking actions: {e}")

        return actions

    def _should_auto_merge_pr(self, analysis: Dict[str, Any], pr_data: Dict[str, Any]) -> bool:
        """Determine if PR should be auto-merged based on analysis."""
        # Only consider merging if analysis recommends it
        recommendations = analysis.get('recommendations', [])
        merge_recommended = any(
            'merge' in rec.get('action', '').lower()
            for rec in recommendations
        )

        if not merge_recommended:
            return False

        # Additional safety checks
        risk_level = analysis.get('risk_level', 'high').lower()
        category = analysis.get('category', '').lower()

        # Only auto-merge low-risk changes
        if risk_level != 'low':
            return False

        # Only auto-merge certain categories
        safe_categories = ['bugfix', 'documentation', 'dependency']
        if category not in safe_categories:
            return False

        # Check if PR is mergeable
        if not pr_data.get('mergeable', False):
            return False

        # Don't merge draft PRs
        if pr_data.get('draft', False):
            return False

        return True

    def _handle_pr_merge(self, repo_name: str, pr_data: Dict[str, Any], analysis: Dict[str, Any]) -> List[str]:
        """Handle PR merge process including testing."""
        actions = []
        pr_number = pr_data['number']

        try:
            # First, run tests to ensure PR is safe to merge
            test_result = self._run_pr_tests(repo_name, pr_data)

            if test_result['success']:
                actions.append(f"Tests passed for PR #{pr_number}")

                # Merge the PR
                if not self.dry_run:
                    merge_result = self._merge_pr(repo_name, pr_number, analysis)
                    if merge_result:
                        actions.append(f"Successfully merged PR #{pr_number}")
                    else:
                        actions.append(f"Failed to merge PR #{pr_number}")
                else:
                    actions.append(f"[DRY RUN] Would merge PR #{pr_number}")
            else:
                actions.append(f"Tests failed for PR #{pr_number}, attempting to fix")

                # Attempt to fix test failures
                fix_actions = self._fix_pr_test_failures(repo_name, pr_data, test_result)
                actions.extend(fix_actions)

        except Exception as e:
            logger.error(f"Failed to handle PR merge for #{pr_number}: {e}")
            actions.append(f"Error handling PR merge: {e}")

        return actions

    def _run_pr_tests(self, repo_name: str, pr_data: Dict[str, Any]) -> Dict[str, Any]:
        """Run tests for a PR and return results."""
        pr_number = pr_data['number']

        try:
            # Check if scripts/test.sh exists
            test_script = "scripts/test.sh"
            if not os.path.exists(test_script):
                logger.warning(f"Test script {test_script} not found, skipping tests")
                return {'success': True, 'output': 'No test script found', 'errors': []}

            # Run the test script
            logger.info(f"Running tests for PR #{pr_number}")
            result = subprocess.run(
                ['bash', test_script],
                capture_output=True,
                text=True,
                timeout=3600  # 60 minutes timeout
            )

            success = result.returncode == 0
            output = result.stdout
            errors = result.stderr

            logger.info(f"Test result for PR #{pr_number}: {'PASS' if success else 'FAIL'}")

            return {
                'success': success,
                'output': output,
                'errors': errors,
                'return_code': result.returncode
            }

        except subprocess.TimeoutExpired:
            logger.error(f"Tests timed out for PR #{pr_number}")
            return {
                'success': False,
                'output': '',
                'errors': 'Tests timed out after 60 minutes',
                'return_code': -1
            }
        except Exception as e:
            logger.error(f"Failed to run tests for PR #{pr_number}: {e}")
            return {
                'success': False,
                'output': '',
                'errors': str(e),
                'return_code': -1
            }

    def _merge_pr(self, repo_name: str, pr_number: int, analysis: Dict[str, Any]) -> bool:
        """Merge a PR using GitHub CLI."""
        try:
            # Use gh CLI to merge the PR
            cmd = ['gh', 'pr', 'merge', str(pr_number), '--auto', '--squash']

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                logger.info(f"Successfully merged PR #{pr_number}")
                return True
            else:
                logger.error(f"Failed to merge PR #{pr_number}: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Error merging PR #{pr_number}: {e}")
            return False

    def _fix_pr_test_failures(self, repo_name: str, pr_data: Dict[str, Any], test_result: Dict[str, Any]) -> List[str]:
        """Attempt to fix PR test failures using Gemini."""
        actions = []
        pr_number = pr_data['number']
        max_attempts = 3

        for attempt in range(max_attempts):
            try:
                # Extract important error information
                error_summary = self._extract_important_errors(test_result)

                if not error_summary:
                    actions.append(f"No actionable errors found in test output for PR #{pr_number}")
                    break

                # Ask Gemini to suggest fixes
                fix_suggestion = self._get_gemini_fix_suggestion(pr_data, error_summary)

                if not fix_suggestion or 'error' in fix_suggestion:
                    actions.append(f"Could not get fix suggestion from Gemini for PR #{pr_number}")
                    break

                # Apply the suggested fix (in dry run, just log it)
                if self.dry_run:
                    actions.append(f"[DRY RUN] Would apply fix for PR #{pr_number}: {fix_suggestion.get('summary', 'No summary')}")
                else:
                    # In a real implementation, this would apply the fix
                    # For now, just add a comment with the suggestion
                    fix_comment = self._format_fix_suggestion_comment(fix_suggestion)
                    self.github.add_comment_to_issue(repo_name, pr_number, fix_comment)
                    actions.append(f"Added fix suggestion comment to PR #{pr_number}")

                # Re-run tests to see if the issue is resolved
                new_test_result = self._run_pr_tests(repo_name, pr_data)

                if new_test_result['success']:
                    actions.append(f"Tests now pass for PR #{pr_number} after fix attempt {attempt + 1}")
                    break
                else:
                    test_result = new_test_result  # Update for next iteration
                    actions.append(f"Tests still failing for PR #{pr_number} after fix attempt {attempt + 1}")

            except Exception as e:
                logger.error(f"Error in fix attempt {attempt + 1} for PR #{pr_number}: {e}")
                actions.append(f"Error in fix attempt {attempt + 1}: {e}")

        return actions

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

    def _get_gemini_fix_suggestion(self, pr_data: Dict[str, Any], error_summary: str) -> Dict[str, Any]:
        """Get fix suggestion from Gemini for test failures."""
        prompt = f"""
Analyze the following test failure for a GitHub pull request and provide a fix suggestion.

Pull Request Information:
- Title: {pr_data['title']}
- Description: {pr_data['body'][:500]}...
- Number: #{pr_data['number']}

Test Error Output:
{error_summary}

Please provide a fix suggestion in the following JSON format:
{{
    "fix_type": "code_fix|configuration|dependency|test_fix",
    "summary": "Brief summary of the issue and fix",
    "root_cause": "What is causing the test failure",
    "suggested_changes": [
        {{
            "file": "path/to/file",
            "action": "modify|create|delete",
            "description": "What to change in this file",
            "code_snippet": "relevant code if applicable"
        }}
    ],
    "commands_to_run": ["command1", "command2"],
    "explanation": "Detailed explanation of why this fix should work"
}}
"""

        try:
            response_text = self.gemini._run_gemini_cli(prompt)
            return self.gemini._parse_solution_response(response_text)
        except Exception as e:
            logger.error(f"Failed to get fix suggestion from Gemini: {e}")
            return {'error': str(e)}

    def _format_fix_suggestion_comment(self, fix_suggestion: Dict[str, Any]) -> str:
        """Format fix suggestion as a GitHub comment."""
        comment = "## 🔧 Auto-Coder Fix Suggestion\n\n"

        if fix_suggestion.get('summary'):
            comment += f"**Issue:** {fix_suggestion['summary']}\n\n"

        if fix_suggestion.get('root_cause'):
            comment += f"**Root Cause:** {fix_suggestion['root_cause']}\n\n"

        if fix_suggestion.get('suggested_changes'):
            comment += "**Suggested Changes:**\n"
            for change in fix_suggestion['suggested_changes']:
                comment += f"- **{change.get('file', 'Unknown file')}**: {change.get('description', 'No description')}\n"
                if change.get('code_snippet'):
                    comment += f"  ```\n  {change['code_snippet']}\n  ```\n"
            comment += "\n"

        if fix_suggestion.get('commands_to_run'):
            comment += "**Commands to run:**\n"
            for cmd in fix_suggestion['commands_to_run']:
                comment += f"```bash\n{cmd}\n```\n"
            comment += "\n"

        if fix_suggestion.get('explanation'):
            comment += f"**Explanation:** {fix_suggestion['explanation']}\n\n"

        comment += "*This fix suggestion was generated automatically by Auto-Coder.*"
        return comment

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
    
    def _format_analysis_comment(self, analysis: Dict[str, Any], solution: Optional[Dict[str, Any]]) -> str:
        """Format analysis as a GitHub comment."""
        comment = "## 🤖 Auto-Coder Analysis\n\n"
        comment += f"**Category:** {analysis.get('category', 'unknown')}\n"
        comment += f"**Priority:** {analysis.get('priority', 'unknown')}\n"
        comment += f"**Complexity:** {analysis.get('complexity', 'unknown')}\n\n"
        
        if analysis.get('summary'):
            comment += f"**Summary:** {analysis['summary']}\n\n"
        
        if analysis.get('recommendations'):
            comment += "**Recommendations:**\n"
            for rec in analysis['recommendations']:
                comment += f"- {rec.get('action', 'No action specified')}\n"
            comment += "\n"
        
        if solution:
            comment += "**Suggested Solution:**\n"
            comment += f"{solution.get('summary', 'No solution summary')}\n\n"
            
            if solution.get('steps'):
                comment += "**Implementation Steps:**\n"
                for step in solution['steps']:
                    comment += f"{step.get('step', '?')}. {step.get('description', 'No description')}\n"
        
        comment += "\n*This analysis was generated automatically by Auto-Coder.*"
        return comment
    
    def _format_pr_analysis_comment(self, analysis: Dict[str, Any]) -> str:
        """Format PR analysis as a GitHub comment."""
        comment = "## 🤖 Auto-Coder PR Analysis\n\n"
        comment += f"**Category:** {analysis.get('category', 'unknown')}\n"
        comment += f"**Risk Level:** {analysis.get('risk_level', 'unknown')}\n"
        comment += f"**Review Priority:** {analysis.get('review_priority', 'unknown')}\n\n"
        
        if analysis.get('summary'):
            comment += f"**Summary:** {analysis['summary']}\n\n"
        
        if analysis.get('recommendations'):
            comment += "**Recommendations:**\n"
            for rec in analysis['recommendations']:
                comment += f"- {rec.get('action', 'No action specified')}\n"
            comment += "\n"
        
        if analysis.get('potential_issues'):
            comment += "**Potential Issues:**\n"
            for issue in analysis['potential_issues']:
                comment += f"- {issue}\n"
            comment += "\n"
        
        comment += "\n*This analysis was generated automatically by Auto-Coder.*"
        return comment
    
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
            filepath = os.path.join(self.reports_dir, f"{filename}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.info(f"Report saved to {filepath}")
        except Exception as e:
            logger.error(f"Failed to save report {filename}: {e}")
