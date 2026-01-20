"""Example demonstrating the new keep_label() functionality."""

from src.auto_coder.label_manager import LabelManager


# Example 1: Basic usage with boolean check (backward compatible)
def example_1():
    """Basic usage - label will be removed on exit."""
    with LabelManager(...) as context:
        if not context:
            return  # Skip processing if label already exists

        # Process the issue/PR
        do_work()

        # Label will be automatically removed on exit


# Example 2: Using keep_label() to retain the label
def example_2():
    """Using keep_label() to prevent automatic removal."""
    with LabelManager(...) as context:
        if not context:
            return  # Skip processing if label already exists

        # Process the issue/PR
        do_work()

        # Keep the label instead of removing it
        context.keep_label()


# Example 3: Context object can be used directly in boolean expressions
def example_3():
    """Context works with boolean expressions."""
    with LabelManager(...) as context:
        # Can use in if statements
        if context:
            do_work()
        else:
            return

        # Can use in while loops
        while context:
            do_work()
            break


# Example 4: Explicit boolean check
def example_4():
    """Explicit boolean checks also work."""
    with LabelManager(...) as context:
        if bool(context):
            do_work()

        # Or use not for negation
        if not context:
            return
