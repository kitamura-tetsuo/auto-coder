"""
Git utilities for Auto-Coder.
"""

import os
import re
import logging
from typing import Optional
from urllib.parse import urlparse

try:
    from git import Repo, InvalidGitRepositoryError
    GIT_AVAILABLE = True
except ImportError:
    GIT_AVAILABLE = False

logger = logging.getLogger(__name__)


def get_current_repo_name(path: Optional[str] = None) -> Optional[str]:
    """
    Get the GitHub repository name (owner/repo) from the current directory.
    
    Args:
        path: Optional path to check. If None, uses current directory.
        
    Returns:
        Repository name in format "owner/repo" or None if not found.
    """
    if not GIT_AVAILABLE:
        logger.warning("GitPython not available. Cannot auto-detect repository.")
        return None
    
    try:
        # Use provided path or current directory
        repo_path = path or os.getcwd()
        
        # Try to find git repository
        repo = Repo(repo_path, search_parent_directories=True)
        
        # Get remote origin URL
        if 'origin' not in repo.remotes:
            logger.debug("No 'origin' remote found in repository")
            return None
            
        origin_url = repo.remotes.origin.url
        logger.debug(f"Found origin URL: {origin_url}")
        
        # Parse GitHub repository name from URL
        repo_name = parse_github_repo_from_url(origin_url)
        if repo_name:
            logger.info(f"Auto-detected repository: {repo_name}")
            return repo_name
        else:
            logger.debug(f"Could not parse GitHub repository from URL: {origin_url}")
            return None
            
    except InvalidGitRepositoryError:
        logger.debug(f"No git repository found in {repo_path}")
        return None
    except Exception as e:
        logger.debug(f"Error detecting repository: {e}")
        return None


def parse_github_repo_from_url(url: str) -> Optional[str]:
    """
    Parse GitHub repository name from various URL formats.
    
    Args:
        url: Git remote URL
        
    Returns:
        Repository name in format "owner/repo" or None if not a GitHub URL.
    """
    if not url:
        return None
    
    # Remove .git suffix if present
    url = url.rstrip('/')
    if url.endswith('.git'):
        url = url[:-4]
    
    # Handle different URL formats
    patterns = [
        # HTTPS: https://github.com/owner/repo
        r'https://github\.com/([^/]+)/([^/]+)',
        # SSH: git@github.com:owner/repo
        r'git@github\.com:([^/]+)/([^/]+)',
        # SSH alternative: ssh://git@github.com/owner/repo
        r'ssh://git@github\.com/([^/]+)/([^/]+)',
    ]
    
    for pattern in patterns:
        match = re.match(pattern, url)
        if match:
            owner, repo = match.groups()
            return f"{owner}/{repo}"
    
    # Try parsing as URL
    try:
        parsed = urlparse(url)
        if parsed.hostname == 'github.com' and parsed.path:
            # Remove leading slash and split path
            path_parts = parsed.path.lstrip('/').split('/')
            if len(path_parts) >= 2:
                owner, repo = path_parts[0], path_parts[1]
                return f"{owner}/{repo}"
    except Exception:
        pass
    
    return None


def is_git_repository(path: Optional[str] = None) -> bool:
    """
    Check if the given path (or current directory) is a Git repository.
    
    Args:
        path: Optional path to check. If None, uses current directory.
        
    Returns:
        True if it's a Git repository, False otherwise.
    """
    if not GIT_AVAILABLE:
        return False
    
    try:
        repo_path = path or os.getcwd()
        Repo(repo_path, search_parent_directories=True)
        return True
    except InvalidGitRepositoryError:
        return False
    except Exception:
        return False


def get_current_branch(path: Optional[str] = None) -> Optional[str]:
    """
    Get the current Git branch name.
    
    Args:
        path: Optional path to check. If None, uses current directory.
        
    Returns:
        Current branch name or None if not found.
    """
    if not GIT_AVAILABLE:
        return None
    
    try:
        repo_path = path or os.getcwd()
        repo = Repo(repo_path, search_parent_directories=True)
        
        if repo.head.is_detached:
            return None
            
        return repo.active_branch.name
    except Exception:
        return None


def get_repo_root(path: Optional[str] = None) -> Optional[str]:
    """
    Get the root directory of the Git repository.
    
    Args:
        path: Optional path to check. If None, uses current directory.
        
    Returns:
        Repository root path or None if not found.
    """
    if not GIT_AVAILABLE:
        return None
    
    try:
        repo_path = path or os.getcwd()
        repo = Repo(repo_path, search_parent_directories=True)
        return repo.working_dir
    except Exception:
        return None
