"""GitHub API を使った sub-issue 操作モジュール."""

import json
import re
import subprocess
from typing import Any, Dict, List, Optional

from loguru import logger


class GitHubSubIssueAPI:
    """GitHub sub-issue API クライアント."""

    def __init__(self, repo: Optional[str] = None):
        """初期化.

        Args:
            repo: リポジトリ名 (owner/repo 形式)。None の場合は現在のディレクトリから取得
        """
        self.repo = repo or self._get_current_repo()

    def _get_current_repo(self) -> str:
        """現在のディレクトリから GitHub リポジトリ名を取得.

        Returns:
            リポジトリ名 (owner/repo 形式)

        Raises:
            RuntimeError: リポジトリ名の取得に失敗した場合
        """
        try:
            result = subprocess.run(
                ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                capture_output=True,
                text=True,
                check=True,
            )
            repo = result.stdout.strip()
            logger.debug(f"Current repository: {repo}")
            return repo
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to get current repository: {e.stderr}")

    def _parse_issue_reference(self, reference: str) -> tuple[str, int]:
        """issue 参照を解析.

        Args:
            reference: issue 番号または URL

        Returns:
            (リポジトリ名, issue 番号) のタプル

        Raises:
            ValueError: 解析に失敗した場合
        """
        # URL の場合
        url_pattern = r"https://github\.com/([^/]+)/([^/]+)/issues/(\d+)"
        match = re.match(url_pattern, reference)
        if match:
            owner, repo, number = match.groups()
            return f"{owner}/{repo}", int(number)

        # issue 番号の場合
        try:
            number = int(reference)
            return self.repo, number
        except ValueError:
            raise ValueError(f"Invalid issue reference: {reference}")

    def _get_issue_id(self, repo: str, issue_number: int) -> str:
        """issue の ID を取得.

        Args:
            repo: リポジトリ名 (owner/repo 形式)
            issue_number: issue 番号

        Returns:
            issue ID

        Raises:
            RuntimeError: ID の取得に失敗した場合
        """
        try:
            result = subprocess.run(
                ["gh", "issue", "view", str(issue_number), "--repo", repo, "--json", "id", "--jq", ".id"],
                capture_output=True,
                text=True,
                check=True,
            )
            issue_id = result.stdout.strip()
            logger.debug(f"Issue #{issue_number} ID: {issue_id}")
            return issue_id
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to get issue ID for #{issue_number}: {e.stderr}")

    def add_sub_issue(self, parent_ref: str, sub_issue_ref: str, replace_parent: bool = False) -> Dict[str, Any]:
        """既存の issue を sub-issue として追加.

        Args:
            parent_ref: 親 issue の参照 (番号または URL)
            sub_issue_ref: sub-issue の参照 (番号または URL)
            replace_parent: 既存の親を置き換えるかどうか

        Returns:
            API レスポンス

        Raises:
            RuntimeError: API 呼び出しに失敗した場合
        """
        parent_repo, parent_number = self._parse_issue_reference(parent_ref)
        sub_issue_repo, sub_issue_number = self._parse_issue_reference(sub_issue_ref)

        parent_id = self._get_issue_id(parent_repo, parent_number)
        sub_issue_id = self._get_issue_id(sub_issue_repo, sub_issue_number)

        replace_str = "true" if replace_parent else "false"
        query = f"""
        mutation addSubIssue {{
            addSubIssue(input: {{ issueId: "{parent_id}", subIssueId: "{sub_issue_id}", replaceParent: {replace_str} }}) {{
                issue {{
                    number
                    title
                }}
                subIssue {{
                    number
                    title
                }}
            }}
        }}
        """

        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "-H", "GraphQL-Features: sub_issues", "-f", f"query={query}"],
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(result.stdout)
            logger.info(f"Added sub-issue #{sub_issue_number} to parent #{parent_number}")
            return data
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to add sub-issue: {e.stderr}")

    def remove_sub_issue(self, parent_ref: str, sub_issue_ref: str) -> Dict[str, Any]:
        """sub-issue を削除.

        Args:
            parent_ref: 親 issue の参照 (番号または URL)
            sub_issue_ref: sub-issue の参照 (番号または URL)

        Returns:
            API レスポンス

        Raises:
            RuntimeError: API 呼び出しに失敗した場合
        """
        parent_repo, parent_number = self._parse_issue_reference(parent_ref)
        sub_issue_repo, sub_issue_number = self._parse_issue_reference(sub_issue_ref)

        parent_id = self._get_issue_id(parent_repo, parent_number)
        sub_issue_id = self._get_issue_id(sub_issue_repo, sub_issue_number)

        query = f"""
        mutation removeSubIssue {{
            removeSubIssue(input: {{ issueId: "{parent_id}", subIssueId: "{sub_issue_id}" }}) {{
                issue {{
                    number
                    title
                }}
                subIssue {{
                    number
                    title
                }}
            }}
        }}
        """

        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "-H", "GraphQL-Features: sub_issues", "-f", f"query={query}"],
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(result.stdout)
            logger.info(f"Removed sub-issue #{sub_issue_number} from parent #{parent_number}")
            return data
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to remove sub-issue: {e.stderr}")

    def list_sub_issues(self, parent_ref: str, state: str = "OPEN") -> List[Dict[str, Any]]:
        """sub-issue の一覧を取得.

        Args:
            parent_ref: 親 issue の参照 (番号または URL)
            state: issue の状態 (OPEN, CLOSED, ALL)

        Returns:
            sub-issue のリスト

        Raises:
            RuntimeError: API 呼び出しに失敗した場合
        """
        parent_repo, parent_number = self._parse_issue_reference(parent_ref)
        owner, repo = parent_repo.split("/")

        query = f"""
        {{
            repository(owner: "{owner}", name: "{repo}") {{
                issue(number: {parent_number}) {{
                    number
                    title
                    subIssues(first: 100) {{
                        nodes {{
                            number
                            title
                            state
                            url
                            assignees(first: 10) {{
                                nodes {{
                                    login
                                }}
                            }}
                        }}
                    }}
                    subIssuesSummary {{
                        total
                        completed
                        percentCompleted
                    }}
                }}
            }}
        }}
        """

        try:
            result = subprocess.run(
                ["gh", "api", "graphql", "-H", "GraphQL-Features: sub_issues", "-f", f"query={query}"],
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(result.stdout)
            
            issue_data = data.get("data", {}).get("repository", {}).get("issue", {})
            sub_issues = issue_data.get("subIssues", {}).get("nodes", [])
            
            # 状態でフィルタリング
            if state != "ALL":
                sub_issues = [si for si in sub_issues if si.get("state") == state]
            
            logger.debug(f"Found {len(sub_issues)} sub-issues for #{parent_number}")
            return sub_issues
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to list sub-issues: {e.stderr}")

    def create_sub_issue(
        self,
        parent_ref: str,
        title: str,
        body: Optional[str] = None,
        labels: Optional[List[str]] = None,
        assignees: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """新しい sub-issue を作成.

        Args:
            parent_ref: 親 issue の参照 (番号または URL)
            title: issue のタイトル
            body: issue の本文
            labels: ラベルのリスト
            assignees: アサインするユーザーのリスト

        Returns:
            作成された issue の情報

        Raises:
            RuntimeError: issue の作成に失敗した場合
        """
        parent_repo, parent_number = self._parse_issue_reference(parent_ref)

        # まず issue を作成
        cmd = ["gh", "issue", "create", "--repo", parent_repo, "--title", title]
        
        if body:
            cmd.extend(["--body", body])
        
        if labels:
            for label in labels:
                cmd.extend(["--label", label])
        
        if assignees:
            for assignee in assignees:
                cmd.extend(["--assignee", assignee])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )
            issue_url = result.stdout.strip()
            # URL から issue 番号を抽出
            match = re.search(r"/issues/(\d+)", issue_url)
            if not match:
                raise RuntimeError(f"Failed to extract issue number from URL: {issue_url}")
            
            issue_number = int(match.group(1))
            logger.info(f"Created issue #{issue_number}: {title}")

            # sub-issue として追加
            self.add_sub_issue(parent_ref, str(issue_number))

            return {
                "number": issue_number,
                "title": title,
                "url": issue_url,
            }
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to create sub-issue: {e.stderr}")

