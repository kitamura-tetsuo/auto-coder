"""
Progress decorators for Auto-Coder.

This module provides decorator functionality equivalent to ProgressStage
for automatic progress management through method decorators.
"""

import functools
import inspect
from typing import Any, Callable, Optional, Union

from .progress_footer import ProgressStage, clear_progress, get_progress_footer, pop_progress_stage, set_progress_item


def progress_stage(*args, **kwargs) -> Callable:
    """
    Decorator that provides ProgressStage functionality for methods.

    Usage:
        @progress_stage()
        def my_method(self):
            # Automatically manages progress for this method
            pass

        @progress_stage("Custom Stage")
        def my_method(self):
            # Uses "Custom Stage" as the stage name
            pass

        @progress_stage("PR", 123, "Analyzing")
        def my_method(self):
            # Sets item info and stage name
            pass

        @progress_stage("Analyzing", related_issues=[456], branch_name="fix-bug")
        def my_method(self):
            # Uses kwargs for additional info
            pass
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*call_args, **call_kwargs):
            # Determine stage name and item info from decorator arguments
            stage_name = None
            item_type = None
            item_number = None
            related_issues = None
            branch_name = None

            if len(args) == 0:
                # No arguments: use function name as stage
                stage_name = func.__name__.replace("_", " ").title()
            elif len(args) == 1:
                # Single argument: it's the stage name
                stage_name = args[0]
            elif len(args) == 3:
                # Three arguments: item_type, item_number, stage
                item_type, item_number, stage_name = args
                related_issues = kwargs.get("related_issues")
                branch_name = kwargs.get("branch_name")
            elif len(args) == 4:
                # Four arguments: item_type, item_number, stage, related_issues
                item_type, item_number, stage_name, related_issues = args
                branch_name = kwargs.get("branch_name")
            elif len(args) == 5:
                # Five arguments: item_type, item_number, stage, related_issues, branch_name
                item_type, item_number, stage_name, related_issues, branch_name = args
            else:
                raise ValueError("progress_stage decorator requires 0, 1, 3, 4, or 5 positional arguments")

            # Use ProgressStage context manager for automatic push/pop
            if item_type and item_number:
                with ProgressStage(item_type, item_number, stage_name, related_issues, branch_name):
                    # Execute the original function
                    return func(*call_args, **call_kwargs)
            else:
                with ProgressStage(stage_name):
                    # Execute the original function
                    return func(*call_args, **call_kwargs)

        return wrapper

    # Handle the case where decorator is used without parentheses: @progress_stage
    if len(args) == 1 and callable(args[0]) and not isinstance(args[0], str):
        # Called as @progress_stage (without parentheses)
        func = args[0]
        stage_name = func.__name__.replace("_", " ").title()

        @functools.wraps(func)
        def wrapper(*call_args, **call_kwargs):
            with ProgressStage(stage_name):
                return func(*call_args, **call_kwargs)

        return wrapper

    # Called as @progress_stage(...) (with parentheses)
    return decorator


def progress_method(stage_name: Optional[str] = None) -> Callable:
    """
    Alternative decorator syntax that always requires parentheses.

    Usage:
        @progress_method()
        def my_method(self):
            pass

        @progress_method("Custom Stage")
        def my_method(self):
            pass

        @progress_method("PR", 123, "Analyzing")
        def my_method(self):
            pass
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*call_args, **call_kwargs):
            # Determine actual stage name
            actual_stage_name = stage_name or func.__name__.replace("_", " ").title()

            with ProgressStage(actual_stage_name):
                return func(*call_args, **call_kwargs)

        return wrapper

    return decorator


def progress_context_item(
    item_type: str,
    item_number: int,
    stage: str,
    related_issues: Optional[list[int]] = None,
    branch_name: Optional[str] = None,
) -> Callable:
    """
    Decorator that sets item context and manages stage progression.

    Usage:
        @progress_context_item("PR", 123, "Analyzing")
        def my_method(self):
            pass

        @progress_context_item("Issue", 456, "Processing", [789], "feature-branch")
        def my_method(self):
            pass
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*call_args, **call_kwargs):
            # Use ProgressStage context manager for automatic push/pop with item context
            with ProgressStage(item_type, item_number, stage, related_issues, branch_name):
                return func(*call_args, **call_kwargs)

        return wrapper

    return decorator


class ProgressStageDecorator:
    """
    Class-based decorator for more complex progress management scenarios.

    Usage:
        class MyProcessor:
            @ProgressStageDecorator("Processing")
            def process_item(self):
                pass

            @ProgressStageDecorator("PR", 123, "Analyzing")
            def analyze_pr(self):
                pass
    """

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __call__(self, func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*call_args, **call_kwargs):
            # Use the same logic as the progress_stage decorator
            stage_name = None
            item_type = None
            item_number = None
            related_issues = None
            branch_name = None

            if len(self.args) == 0:
                stage_name = func.__name__.replace("_", " ").title()
            elif len(self.args) == 1:
                stage_name = self.args[0]
            elif len(self.args) == 3:
                item_type, item_number, stage_name = self.args
                related_issues = self.kwargs.get("related_issues")
                branch_name = self.kwargs.get("branch_name")
            elif len(self.args) == 4:
                item_type, item_number, stage_name, related_issues = self.args
                branch_name = self.kwargs.get("branch_name")
            elif len(self.args) == 5:
                item_type, item_number, stage_name, related_issues, branch_name = self.args
            else:
                raise ValueError("ProgressStageDecorator requires 0, 1, 3, 4, or 5 positional arguments")

            # Use ProgressStage context manager for automatic push/pop
            if item_type and item_number:
                with ProgressStage(item_type, item_number, stage_name, related_issues, branch_name):
                    return func(*call_args, **call_kwargs)
            else:
                with ProgressStage(stage_name):
                    return func(*call_args, **call_kwargs)

        return wrapper
