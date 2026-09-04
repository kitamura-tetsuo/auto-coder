"""Codex Cloud WHAM backend client for Auto-Coder.

Encapsulates internal ChatGPT/Codex backend endpoints for task status,
turn resolution, and follow-up messaging.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Mapping, Optional

import httpx

from .codex_usage_checker import load_codex_oauth_credentials
from .logger_config import get_logger

logger = get_logger(__name__)

DEFAULT_WHAM_BASE_URL = "https://chatgpt.com/backend-api/wham"


@dataclass(frozen=True)
class WhamTurn:
    """Represents a turn in a Codex Cloud WHAM task."""

    id: str = ""
    role: str = ""
    status: str = ""
    created_at: Optional[str] = None
    raw_data: Optional[object] = None


@dataclass(frozen=True)
class WhamTask:
    """Represents a Codex Cloud WHAM task."""

    id: str = ""
    status: str = ""
    title: Optional[str] = None
    turns: list[WhamTurn] = field(default_factory=list)
    raw_data: Optional[object] = None


class FollowUpDeliveryOutcome(str, Enum):
    """Semantic result of a non-idempotent WHAM follow-up request."""

    DELIVERED = "delivered"
    NOT_DELIVERED = "not_delivered"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class FollowUpDeliveryResult:
    """Delivery outcome with the HTTP status when one was received."""

    outcome: FollowUpDeliveryOutcome = FollowUpDeliveryOutcome.INDETERMINATE
    status_code: Optional[int] = None

    @property
    def delivered(self) -> bool:
        return self.outcome is FollowUpDeliveryOutcome.DELIVERED


@dataclass(frozen=True)
class WhamFollowUpPayload:
    """Represents a follow-up request payload to the WHAM tasks API."""

    task_id: str = ""
    turn_id: str = ""
    text: str = ""
    run_environment_in_qa_mode: bool = False

    def to_dict(self) -> dict[str, object]:
        """Convert to the expected WHAM tasks POST payload format."""
        return {
            "follow_up": {
                "task_id": self.task_id,
                "turn_id": self.turn_id,
                "run_environment_in_qa_mode": self.run_environment_in_qa_mode,
            },
            "input_items": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "content_type": "text",
                            "text": self.text,
                        }
                    ],
                }
            ],
        }


class CodexWhamClient:
    """Internal WHAM backend client for Codex Cloud tasks."""

    def __init__(
        self,
        base_url: str = DEFAULT_WHAM_BASE_URL,
        timeout: float = 30.0,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        """Initialize WHAM client.

        Args:
            base_url: Base URL for WHAM API endpoints.
            timeout: HTTP request timeout in seconds.
            now_fn: Optional callable returning current datetime for expiry checking.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.now_fn = now_fn

    def _get_headers(self) -> Optional[dict[str, str]]:
        """Load ChatGPT OAuth credentials and construct headers.

        Never falls back to or uses the OpenAI API key.
        """
        now = self.now_fn() if self.now_fn else None
        credentials = load_codex_oauth_credentials(now=now)
        if credentials is None:
            logger.warning("Codex ChatGPT OAuth credentials unavailable or expired for WHAM API")
            return None

        return {
            "Authorization": f"Bearer {credentials.access_token}",
            "ChatGPT-Account-Id": credentials.account_id,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "codex_cli_rs",
            "originator": "codex_cli_rs",
        }

    def _parse_turn_dict(self, item: object) -> Optional[WhamTurn]:
        """Parse a single turn dict into a WhamTurn."""
        if not isinstance(item, dict):
            return None

        # Handle wrapper format: {"id": "...", "turn": {...}}
        inner = item.get("turn")
        turn_data: dict[str, object] = inner if isinstance(inner, dict) else item

        tid = turn_data.get("id") or turn_data.get("turn_id") or item.get("id") or ""
        role = turn_data.get("role") or turn_data.get("type") or ""
        if not role and isinstance(turn_data.get("author"), dict):
            author_obj = turn_data["author"]
            if isinstance(author_obj, dict):
                role = author_obj.get("role", "")

        status = turn_data.get("status") or turn_data.get("state") or ""
        created_at = turn_data.get("created_at") or turn_data.get("timestamp")
        created_at_str = str(created_at) if created_at is not None else None

        return WhamTurn(
            id=str(tid),
            role=str(role),
            status=str(status),
            created_at=created_at_str,
            raw_data=item,
        )

    def get_task(self, task_id: str) -> Optional[WhamTask]:
        """Fetch task details for a given task ID.

        Executes:
            GET /backend-api/wham/tasks/{task_id}

        Args:
            task_id: The Codex Cloud task ID.

        Returns:
            WhamTask instance or None if not found / failed.
        """
        if not task_id:
            logger.warning("WHAM get_task called with empty task_id")
            return None

        headers = self._get_headers()
        if headers is None:
            return None

        url = f"{self.base_url}/tasks/{task_id}"
        try:
            response = httpx.get(url, headers=headers, timeout=self.timeout)
            if response.status_code in (401, 403):
                logger.warning(f"WHAM get_task authentication rejected (HTTP {response.status_code}) for task '{task_id}'")
                return None
            if response.status_code == 404:
                logger.debug(f"WHAM task '{task_id}' not found (HTTP 404)")
                return None
            response.raise_for_status()

            data = response.json()
            if not isinstance(data, dict):
                logger.warning(f"WHAM get_task returned non-dict response for task '{task_id}'")
                return None

            task_obj = data.get("task")
            raw_task: dict[str, object] = task_obj if isinstance(task_obj, dict) else data
            tid = str(raw_task.get("id") or raw_task.get("task_id") or task_id)
            status = str(raw_task.get("status") or raw_task.get("state") or "")
            title = raw_task.get("title")
            title_str = str(title) if title is not None else None

            # Extract turns if present in task object
            turns: list[WhamTurn] = []
            raw_turns = raw_task.get("turns") or raw_task.get("items") or []
            if isinstance(raw_turns, list):
                for item in raw_turns:
                    turn = self._parse_turn_dict(item)
                    if turn:
                        turns.append(turn)

            # Extract current_assistant_turn / current_user_turn if present
            current_asst = data.get("current_assistant_turn")
            if isinstance(current_asst, dict):
                asst_turn = self._parse_turn_dict(current_asst)
                if asst_turn and not any(t.id == asst_turn.id for t in turns):
                    turns.append(asst_turn)

            return WhamTask(
                id=tid,
                status=status,
                title=title_str,
                turns=turns,
                raw_data=data,
            )
        except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning(f"Failed to fetch WHAM task '{task_id}': {type(e).__name__}")
            return None

    def get_task_turns(self, task_id: str) -> list[WhamTurn]:
        """Fetch task turns for a given task ID.

        Executes:
            GET /backend-api/wham/tasks/{task_id}/turns

        Args:
            task_id: The Codex Cloud task ID.

        Returns:
            List of WhamTurn instances.
        """
        if not task_id:
            logger.warning("WHAM get_task_turns called with empty task_id")
            return []

        headers = self._get_headers()
        if headers is None:
            return []

        url = f"{self.base_url}/tasks/{task_id}/turns"
        try:
            response = httpx.get(url, headers=headers, timeout=self.timeout)
            if response.status_code in (401, 403):
                logger.warning(f"WHAM get_task_turns authentication rejected (HTTP {response.status_code}) for task '{task_id}'")
                return []
            if response.status_code == 404:
                logger.debug(f"WHAM task turns for '{task_id}' not found (HTTP 404)")
                return []
            response.raise_for_status()

            data = response.json()
            raw_list: list[object] = []
            if isinstance(data, list):
                raw_list = data
            elif isinstance(data, dict):
                if isinstance(data.get("turn_mapping"), dict):
                    turn_map = data["turn_mapping"]
                    if isinstance(turn_map, dict):
                        raw_list = list(turn_map.values())
                else:
                    raw_items = data.get("turns") or data.get("items") or data.get("data") or []
                    if isinstance(raw_items, list):
                        raw_list = raw_items

            turns: list[WhamTurn] = []
            for item in raw_list:
                turn = self._parse_turn_dict(item)
                if turn:
                    turns.append(turn)

            return turns
        except (httpx.HTTPError, json.JSONDecodeError, ValueError, TypeError) as e:

            logger.warning(f"Failed to fetch WHAM task turns for '{task_id}': {type(e).__name__}")
            return []

    def resolve_latest_assistant_turn(self, task_id: str) -> Optional[str]:
        """Resolve the latest usable assistant turn ID for a given task ID.

        Queries the WHAM task/turn APIs, verifies the turn belongs to the task,
        and ensures it represents a usable assistant turn.

        Args:
            task_id: The target Codex Cloud task ID.

        Returns:
            The resolved turn ID string (e.g. 'task_e_...~assttrn_e_...'),
            or None if no usable assistant turn could be resolved.
        """
        if not task_id:
            logger.warning("Cannot resolve latest assistant turn for empty task_id")
            return None

        # Try fetching turns list first
        turns = self.get_task_turns(task_id)

        # If empty, fallback to inspecting task details
        if not turns:
            task = self.get_task(task_id)
            if task and task.turns:
                turns = task.turns

        if not turns:
            logger.warning(f"Codex Cloud task '{task_id}' has no available turns to continue from")
            return None

        # Filter for assistant turns that belong to this task and are usable
        assistant_turns: list[WhamTurn] = []
        for turn in turns:
            if not turn.id:
                continue

            # Verify task ID ownership if turn.id contains '~'
            if "~" in turn.id:
                turn_task_prefix = turn.id.split("~")[0]
                if turn_task_prefix != task_id:
                    logger.debug(f"Ignoring turn '{turn.id}' because task prefix does not match target '{task_id}'")
                    continue

            # Check if this turn is an assistant turn
            role_lower = turn.role.lower()
            is_assistant = role_lower in ("assistant", "agent", "bot", "asst") or "assttrn_" in turn.id or turn.id.startswith(f"{task_id}~assttrn_")
            if not is_assistant:
                continue

            # Check for non-error / usable state
            status_lower = turn.status.lower()
            if status_lower in ("failed", "error", "cancelled"):
                logger.debug(f"Ignoring assistant turn '{turn.id}' with unusable status '{turn.status}'")
                continue

            assistant_turns.append(turn)

        if not assistant_turns:
            logger.warning(f"Codex Cloud task '{task_id}' contains no usable assistant turns (total turns: {len(turns)})")
            return None

        # Sort turns chronologically by created_at timestamp if present
        def _turn_sort_key(t: WhamTurn) -> float:
            if t.created_at:
                try:
                    return float(t.created_at)
                except (ValueError, TypeError):
                    pass
            return 0.0

        if any(t.created_at for t in assistant_turns):
            assistant_turns.sort(key=_turn_sort_key)

        # Select the latest assistant turn (the last item in the list)
        latest_turn = assistant_turns[-1]
        turn_id = latest_turn.id

        # Normalize turn ID: ensure task_id prefix format (task_id~turn_sub_id)
        if "~" not in turn_id:
            turn_id = f"{task_id}~{turn_id}"

        logger.info(f"Resolved latest assistant turn for Codex Cloud task '{task_id}': '{turn_id}'")
        return turn_id

    def reconcile_follow_up(self, task_id: str, pre_send_turn_id: str, message_fingerprint: str) -> Optional[bool]:
        """Reconcile an ambiguous POST against current remote turn state.

        ``True`` means the task advanced consistently with the submitted
        message. ``None`` means the available state cannot prove either
        acceptance or rejection; callers must defer rather than resend.
        """
        turns = self.get_task_turns(task_id)
        if not turns:
            task = self.get_task(task_id)
            turns = task.turns if task else []
        if not turns:
            return None

        normalized_pre = pre_send_turn_id.split("~", 1)[-1]
        pre_index = next((index for index, turn in enumerate(turns) if turn.id == pre_send_turn_id or turn.id.split("~", 1)[-1] == normalized_pre), None)
        if pre_index is None:
            return None
        later_turns = turns[pre_index + 1 :]
        if not later_turns:
            return None

        def _strings(value: object) -> list[str]:
            if isinstance(value, str):
                return [value]
            if isinstance(value, list):
                return [text for item in value for text in _strings(item)]
            if isinstance(value, dict):
                return [text for item in value.values() for text in _strings(item)]
            return []

        user_turns = [turn for turn in later_turns if turn.role.lower() in ("user", "human") or "usrtrn_" in turn.id]
        if user_turns:
            # When WHAM exposes submitted content, require the stable logical
            # message itself rather than mistaking unrelated advancement for it.
            exposed_strings = [text for turn in user_turns for text in _strings(turn.raw_data)]
            return True if any(hashlib.sha256(text.encode("utf-8")).hexdigest() == message_fingerprint for text in exposed_strings) else None
        if any(turn.id != pre_send_turn_id and ("assttrn_" in turn.id or turn.role.lower() in ("assistant", "agent", "bot", "asst")) for turn in later_turns):
            return True
        return None

    def send_follow_up(
        self,
        task_id: str,
        turn_id: str,
        prompt: str,
        run_environment_in_qa_mode: bool = False,
    ) -> FollowUpDeliveryResult:
        """Send a follow-up continuation message to an existing Codex Cloud task.

        Executes:
            POST /backend-api/wham/tasks

        Args:
            task_id: The target Codex Cloud task ID.
            turn_id: The latest assistant turn ID to continue from.
            prompt: The continuation message text.
            run_environment_in_qa_mode: QA mode flag (default False).

        Returns:
            A semantic delivery result. Ambiguous transport and server failures
            are not flattened into a definite rejection.
        """
        if not task_id or not turn_id or not prompt:
            logger.warning(f"Invalid follow-up parameters: task_id='{task_id}', turn_id='{turn_id}', prompt_len={len(prompt) if prompt else 0}")
            return FollowUpDeliveryResult(FollowUpDeliveryOutcome.NOT_DELIVERED)

        headers = self._get_headers()
        if headers is None:
            logger.warning(f"Cannot send follow-up for task '{task_id}': credentials unavailable or expired")
            return FollowUpDeliveryResult(FollowUpDeliveryOutcome.NOT_DELIVERED)

        payload = WhamFollowUpPayload(
            task_id=task_id,
            turn_id=turn_id,
            text=prompt,
            run_environment_in_qa_mode=run_environment_in_qa_mode,
        ).to_dict()

        url = f"{self.base_url}/tasks"
        logger.info(f"Sending WHAM follow-up to task '{task_id}' continuing from turn '{turn_id}'")

        try:
            response = httpx.post(url, headers=headers, json=payload, timeout=self.timeout)
            status = response.status_code

            if status in (200, 201, 202, 204):
                logger.info(f"WHAM follow-up accepted for task '{task_id}' (HTTP {status})")
                return FollowUpDeliveryResult(FollowUpDeliveryOutcome.DELIVERED, status)

            if status in (401, 403):
                logger.warning(f"WHAM follow-up rejected with HTTP {status} (Authentication/Authorization failure) for task '{task_id}'")
                return FollowUpDeliveryResult(FollowUpDeliveryOutcome.NOT_DELIVERED, status)

            # These responses can be generated after the non-idempotent request
            # reached WHAM, so server-side acceptance cannot be excluded.
            if status in (408, 409, 425, 429) or 500 <= status < 600:
                logger.warning(f"WHAM follow-up delivery is indeterminate after HTTP {status} for task '{task_id}'")
                return FollowUpDeliveryResult(FollowUpDeliveryOutcome.INDETERMINATE, status)

            if 400 <= status < 500:
                logger.warning(f"WHAM follow-up rejected with HTTP {status} (Client Error) for task '{task_id}'")
                return FollowUpDeliveryResult(FollowUpDeliveryOutcome.NOT_DELIVERED, status)

            logger.warning(f"WHAM follow-up failed with HTTP {status} (Server Error) for task '{task_id}'")
            return FollowUpDeliveryResult(FollowUpDeliveryOutcome.INDETERMINATE, status)

        except httpx.HTTPError as e:
            logger.warning(f"WHAM follow-up request failed for task '{task_id}': {type(e).__name__}")
            return FollowUpDeliveryResult(FollowUpDeliveryOutcome.INDETERMINATE)
        except Exception as e:
            logger.error(f"Unexpected error sending WHAM follow-up for task '{task_id}': {e}")
            return FollowUpDeliveryResult(FollowUpDeliveryOutcome.INDETERMINATE)
