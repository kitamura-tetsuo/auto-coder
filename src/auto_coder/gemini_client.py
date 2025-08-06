"""
Gemini CLI client for Auto-Coder.
"""

import logging
import json
import subprocess
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


class GeminiClient:
    """Gemini CLI client for analyzing issues and generating solutions."""

    def __init__(self, model_name: str = "gemini-2.5-pro"):
        """Initialize Gemini CLI client."""
        self.model_name = model_name
        self.timeout = None  # No timeout - let gemini CLI run as long as needed

        # Check if gemini CLI is available
        try:
            result = subprocess.run(
                ['gemini', '--version'],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode != 0:
                raise RuntimeError("Gemini CLI not available or not working")
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError) as e:
            raise RuntimeError(f"Gemini CLI not available: {e}")

    def _run_gemini_cli(self, prompt: str) -> str:
        """Run gemini CLI with the given prompt and show real-time output."""
        try:
            # Run gemini CLI with prompt via stdin and additional prompt parameter
            cmd = [
                'gemini',
                '--model', self.model_name,
                '--force-model',
                '--prompt', prompt
            ]

            logger.debug(f"Running gemini CLI with prompt length: {len(prompt)} characters")
            print(f"🤖 Running: gemini --model {self.model_name} --force-model --prompt [prompt]")
            print("=" * 60)

            # Run with real-time output
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )

            output_lines = []

            # Read output line by line and display in real-time
            for line in process.stdout:
                line = line.rstrip('\n')
                print(line)  # Display in real-time
                output_lines.append(line)

            # Wait for process to complete
            return_code = process.wait()

            print("=" * 60)

            if return_code != 0:
                raise RuntimeError(f"Gemini CLI failed with return code {return_code}")

            # Join all output lines
            full_output = '\n'.join(output_lines)
            return full_output.strip()

        except Exception as e:
            if "timed out" not in str(e):  # Don't mention timeout since we removed it
                raise RuntimeError(f"Failed to run Gemini CLI: {e}")
            raise
    
    def suggest_features(self, repo_context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Suggest new features based on repository analysis."""
        prompt = self._create_feature_suggestion_prompt(repo_context)

        try:
            response_text = self._run_gemini_cli(prompt)
            suggestions = self._parse_feature_suggestions(response_text)
            logger.info(f"Generated {len(suggestions)} feature suggestions")
            return suggestions

        except Exception as e:
            logger.error(f"Failed to generate feature suggestions: {e}")
            return []
    
    def _create_pr_analysis_prompt(self, pr_data: Dict[str, Any]) -> str:
        """Create prompt for pull request analysis."""
        return f"""
Analyze the following GitHub pull request and provide a structured analysis:

Title: {pr_data['title']}
Body: {pr_data['body']}
Labels: {', '.join(pr_data['labels'])}
Branch: {pr_data['head_branch']} -> {pr_data['base_branch']}
Changes: +{pr_data['additions']} -{pr_data['deletions']} files: {pr_data['changed_files']}
Draft: {pr_data['draft']}
Mergeable: {pr_data['mergeable']}

Please provide analysis in the following JSON format:
{{
    "category": "bugfix|feature|refactor|documentation|test",
    "risk_level": "low|medium|high",
    "review_priority": "low|medium|high",
    "estimated_review_time": "minutes|hours",
    "recommendations": [
        {{
            "action": "description of recommended action",
            "rationale": "why this action is recommended"
        }}
    ],
    "potential_issues": ["issue1", "issue2"],
    "summary": "brief summary of the changes"
}}
"""
    
    def _create_feature_suggestion_prompt(self, repo_context: Dict[str, Any]) -> str:
        """Create prompt for feature suggestions."""
        return f"""
Based on the following repository context, suggest new features that would be valuable:

Repository: {repo_context.get('name', 'Unknown')}
Description: {repo_context.get('description', 'No description')}
Language: {repo_context.get('language', 'Unknown')}
Recent Issues: {repo_context.get('recent_issues', [])}
Recent PRs: {repo_context.get('recent_prs', [])}

Please provide feature suggestions in the following JSON format:
[
    {{
        "title": "Feature title",
        "description": "Detailed description of the feature",
        "rationale": "Why this feature would be valuable",
        "priority": "low|medium|high",
        "complexity": "simple|moderate|complex",
        "estimated_effort": "hours|days|weeks",
        "labels": ["enhancement", "feature"],
        "acceptance_criteria": [
            "criteria 1",
            "criteria 2"
        ]
    }}
]
"""
    
    def _parse_feature_suggestions(self, response_text: str) -> List[Dict[str, Any]]:
        """Parse feature suggestions from Gemini."""
        try:
            # Try to extract JSON array from the response
            start_idx = response_text.find('[')
            end_idx = response_text.rfind(']') + 1
            
            if start_idx != -1 and end_idx != -1:
                json_str = response_text[start_idx:end_idx]
                return json.loads(json_str)
            else:
                return []
        except json.JSONDecodeError:
            return []
