"""
Authentication utilities for Auto-Coder.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Optional, cast

import yaml

from .gh_logger import get_gh_logger
from .logger_config import get_logger

logger = get_logger(__name__)


def get_github_token() -> Optional[str]:
    """
    Get GitHub token from various sources in order of preference:
    1. Environment variable GITHUB_TOKEN
    2. gh CLI authentication
    3. GitHub CLI config file

    Returns:
        GitHub token or None if not found.
    """
    # 1. Check environment variable first
    token = os.getenv("GITHUB_TOKEN")
    if token:
        logger.debug("Using GitHub token from GITHUB_TOKEN environment variable")
        return token

    # 2. Try to get token from gh CLI
    try:
        gh_logger = get_gh_logger()
        result = gh_logger.execute_with_logging(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            token = result.stdout.strip()
            if token:
                logger.info("Using GitHub token from gh CLI authentication")
                return cast(str, token)
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        FileNotFoundError,
    ) as e:
        logger.debug(f"Could not get token from gh CLI: {e}")

    # 3. Try to read from gh config file
    try:
        gh_config_path = Path.home() / ".config" / "gh" / "hosts.yml"
        if gh_config_path.exists():
            with open(gh_config_path, "r") as f:
                config = yaml.safe_load(f)

            if config and "github.com" in config:
                github_config = config["github.com"]
                if "oauth_token" in github_config:
                    logger.info("Using GitHub token from gh config file")
                    return cast(str, github_config["oauth_token"])
    except Exception as e:
        logger.debug(f"Could not read gh config file: {e}")

    logger.debug("No GitHub token found")
    return None


def get_gemini_api_key() -> Optional[str]:
    """
    Get Gemini API key from various sources in order of preference:
    1. Environment variable GEMINI_API_KEY
    2. Google AI Studio CLI config
    3. gcloud CLI authentication (if available)

    Returns:
        Gemini API key or None if not found.
    """
    # 1. Check environment variable first
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        logger.debug("Using Gemini API key from GEMINI_API_KEY environment variable")
        return api_key

    # 2. Try to get from gemini CLI config
    try:
        # Check if gemini CLI has stored credentials
        result = subprocess.run(
            ["gemini", "config", "get", "api_key"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            api_key = result.stdout.strip()
            if api_key and api_key != "null":
                logger.info("Using Gemini API key from gemini CLI config")
                return api_key
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        FileNotFoundError,
    ) as e:
        logger.debug(f"Could not get API key from gemini CLI: {e}")

    # 3. Try alternative gemini CLI command
    try:
        result = subprocess.run(["gemini", "auth", "status"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and "authenticated" in result.stdout.lower():
            # Try to extract API key from status output
            lines = result.stdout.split("\n")
            for line in lines:
                if "api_key" in line.lower() or "key" in line.lower():
                    parts = line.split(":")
                    if len(parts) > 1:
                        api_key = parts[1].strip()
                        if api_key and api_key != "null":
                            logger.info("Using Gemini API key from gemini CLI status")
                            return api_key
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        FileNotFoundError,
    ) as e:
        logger.debug(f"Could not get API key from gemini CLI status: {e}")

    # 4. Try to read from common config locations
    config_paths = [
        Path.home() / ".config" / "gemini" / "config.json",
        Path.home() / ".gemini" / "config.json",
        Path.home() / ".config" / "google-ai-studio" / "config.json",
    ]

    for config_path in config_paths:
        try:
            if config_path.exists():
                with open(config_path, "r") as f:
                    config = json.load(f)

                # Try different possible key names
                key_names = ["api_key", "apiKey", "key", "token"]
                for key_name in key_names:
                    if key_name in config:
                        api_key = config[key_name]
                        if api_key:
                            logger.info(f"Using Gemini API key from {config_path}")
                            return cast(str, api_key)
        except Exception as e:
            logger.debug(f"Could not read config file {config_path}: {e}")

    # 5. Try gcloud authentication as fallback
    try:
        result = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            token = result.stdout.strip()
            if token:
                logger.info("Using access token from gcloud CLI (may work for some Gemini APIs)")
                return token
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        FileNotFoundError,
    ) as e:
        logger.debug(f"Could not get token from gcloud CLI: {e}")

    logger.debug("No Gemini API key found")
    return None


def check_gh_auth() -> bool:
    """
    Check if gh CLI is authenticated.

    Returns:
        True if authenticated, False otherwise.
    """
    try:
        gh_logger = get_gh_logger()
        result = gh_logger.execute_with_logging(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # gh auth status often outputs to stderr
        output = (result.stdout + result.stderr).lower()
        return result.returncode == 0 and "logged in" in output
    except Exception:
        return False


def check_gemini_auth() -> bool:
    """
    Check if gemini CLI is authenticated.

    Returns:
        True if authenticated, False otherwise.
    """
    try:
        # Try to check if gemini CLI is authenticated
        result = subprocess.run(["gemini", "auth", "status"], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return "authenticated" in result.stdout.lower()

        # Alternative: try to get config
        result = subprocess.run(
            ["gemini", "config", "get", "api_key"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() not in ["", "null"]

    except Exception:
        return False


def get_auth_status() -> dict:
    """
    Get authentication status for all services.

    Returns:
        Dictionary with authentication status.
    """
    return {
        "github": {
            "authenticated": check_gh_auth(),
            "token_available": get_github_token() is not None,
        },
        "gemini": {
            "authenticated": check_gemini_auth(),
            "api_key_available": get_gemini_api_key() is not None,
        },
    }


def verify_github_access() -> bool:
    """
    Verify GitHub access by making a lightweight API call.
    Returns True if successful, False otherwise.
    Logs appropriate error messages if access fails.
    """
    try:
        import httpx

        from .util.gh_cache import get_ghapi_client

        token = get_github_token()
        if not token:
            logger.debug("No GitHub token found during verification.")
            return False

        # Use httpx directly to bypass cache and ensure the token is valid right now.
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "Cache-Control": "no-cache",
        }

        try:
            resp = httpx.get("https://api.github.com/user", headers=headers, timeout=10)
            resp.raise_for_status()
            logger.debug("GitHub access verification successful.")
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                # Log the specific error message for 401
                logger.error("GitHub API authentication failed (401). Please update your GITHUB_TOKEN. (https://github.com/settings/tokens)")
                raise
            else:
                logger.debug(f"GitHub access verification failed: {e}")
                raise
        except Exception as e:
            logger.debug(f"GitHub access verification failed with unexpected error: {e}")
            raise

    except ImportError:
        logger.debug("Could not import dependencies for GitHub verification.")
        return False
    except Exception:
        # If get_github_token fails or other setup issues
        return False
