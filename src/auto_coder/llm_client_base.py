"""
Base class for LLM clients.
"""
from abc import ABC, abstractmethod
from typing import Optional


class LLMClientBase(ABC):
    """Base class for all LLM clients.

    All LLM clients must implement the _run_llm_cli method.
    """

    @abstractmethod
    def _run_llm_cli(self, prompt: str) -> str:
        """Execute LLM with the given prompt.

        Args:
            prompt: The prompt to send to the LLM

        Returns:
            The LLM's response as a string
        """
        pass

    def switch_to_default_model(self) -> None:
        """Switch to the default model.

        This is optional and can be overridden by subclasses.
        Default implementation does nothing.
        """
        pass

    def close(self) -> None:
        """Close the client and clean up resources.

        This is optional and can be overridden by subclasses.
        Default implementation does nothing.
        """
        pass


class LLMBackendManagerBase(LLMClientBase):
    """Base class for LLM backend managers.

    Backend managers must implement additional methods for managing backends.
    """

    @abstractmethod
    def run_test_fix_prompt(
        self, prompt: str, current_test_file: Optional[str] = None
    ) -> str:
        """Execute LLM for test fix with optional test file tracking.

        Args:
            prompt: The prompt to send to the LLM
            current_test_file: Optional test file being fixed

        Returns:
            The LLM's response as a string
        """
        pass

    def close(self) -> None:
        """Close the backend manager and clean up resources.

        This is optional and can be overridden by subclasses.
        Default implementation does nothing.
        """
        pass
