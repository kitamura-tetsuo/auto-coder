# Type checking comprehensive setup for Python
# This file demonstrates multiple approaches for comprehensive type checking

import inspect
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Type, Union

# Example usage in Python code:
# from typing import Protocol
#
# class HasSuccessAttribute(Protocol):
#     success: bool
#     ids: list[int]
#
# def process_checks(checks: Union[dict, HasSuccessAttribute]) -> bool:
#     if isinstance(checks, dict):
#         return checks.get("success", False)
#     else:
#         return checks.success


@dataclass
class StrictTypeChecker:
    """Comprehensive type checking utilities."""

    @staticmethod
    def check_type_annotations_enabled() -> bool:
        """Check if type annotations are enabled in current Python version."""
        return sys.version_info >= (3, 5)

    @staticmethod
    def get_function_signature(func) -> Optional[inspect.Signature]:
        """Get function signature with type information."""
        try:
            return inspect.signature(func)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def validate_return_type(
        func_name: str, expected_type: Type, actual_value: Any
    ) -> bool:
        """Validate return type at runtime."""
        if not isinstance(actual_value, expected_type):
            print(
                f"Type validation failed for {func_name}: expected {expected_type}, got {type(actual_value)}"
            )
            return False
        return True


# Pyright configuration for stricter checking
PYRIGHT_CONFIG = """
# pyproject.toml section for Pyright
[tool.pyright]
pythonVersion = "3.11"
strictListInference = true
strictDictInference = true
strictSetInference = true
strictParameterNoneValue = true
reportOptionalSubscript = true
reportOptionalMemberAccess = true
reportOptionalCall = true
reportOptionalIndexedValue = true
reportOptionalIterable = true
reportOptionalContextManager = true
reportOptionalOperand = true
reportTypedDictNotRequiredAccess = true
reportPrivateImportUsage = true
reportUnknownParameterType = true
reportUnknownArgumentType = true
reportUnknownLambdaType = true
reportUnknownVariableType = true
reportUnknownMemberType = true
reportMissingTypeStubs = true
reportImportCycles = true
reportUnusedImport = true
reportUnusedClass = true
reportUnusedFunction = true
reportUnusedVariable = true
reportDuplicateImport = true
reportWildcardImportFromLibrary = true
reportPrivateUsage = true
reportTypeCommentUsage = true
reportIncompatibleMethodOverride = true
reportIncompatibleVariableOverride = true
reportIncompleteStub = true
reportUnsupportedDunderAll = true
reportUntypedNamedTuple = true
reportUntypedClassDecorator = true
reportUntypedFunctionDecorator = true
reportUntypedBaseClass = true
reportPrivateSubscript = true
reportInvalidStubStatement = true
reportIncompletePickle = true
reportConstantRedefinition = true
reportDeprecated = true
"""
