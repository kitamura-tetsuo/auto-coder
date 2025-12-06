# Progress Decorators for Auto-Coder

This document describes decorators that provide equivalent functionality to the `ProgressStage()` context manager.

## Overview

`src/auto_coder/progress_decorators.py` implements decorators that automatically manage progress during method execution. These provide the same functionality as the `ProgressStage` context manager but can be used as decorators.

## Decorator Types

### 1. `@progress_stage`

The most flexible decorator, accepting the same arguments as the ProgressStage context manager.

#### Usage Examples

```python
from src.auto_coder.progress_decorators import progress_stage

class MyProcessor:
    @progress_stage()
    def process_simple(self):
        # Uses automatically generated stage name ("Process Simple")
        pass

    @progress_stage("Custom Stage Name")
    def process_with_custom_name(self):
        # Custom stage name
        pass

    @progress_stage("PR", 123, "Analyzing")
    def analyze_pr(self):
        # With PR context
        pass

    @progress_stage("Issue", 456, "Processing", [789], "feature-branch")
    def process_issue_with_context(self):
        # With full context information
        pass
```

#### Argument Specifications

- `()`: Automatically generates method name (`method_name → Method Name`)
- `("stage_name")`: Custom stage name
- `("item_type", item_number, "stage_name")`: Item information and stage
- `("item_type", item_number, "stage_name", related_issues, branch_name)`: Full context

### 2. `@progress_method`

An alternative decorator that provides a simpler syntax.

```python
from src.auto_coder.progress_decorators import progress_method

class MyProcessor:
    @progress_method()
    def my_method(self):
        # Uses method name as stage name
        pass

    @progress_method("Custom Stage")
    def another_method(self):
        # Custom stage name
        pass
```

### 3. `@progress_context_item`

A decorator that sets item context and automatically clears it.

```python
from src.auto_coder.progress_decorators import progress_context_item

class MyProcessor:
    @progress_context_item("PR", 123, "Analyzing")
    def analyze_pr(self):
        # PR context set, automatically cleared after execution
        pass
```

### 4. `ProgressStageDecorator` Class

A class-based decorator that can handle more complex scenarios.

```python
from src.auto_coder.progress_decorators import ProgressStageDecorator

class MyProcessor:
    @ProgressStageDecorator("Processing")
    def process_item(self):
        pass

    @ProgressStageDecorator("PR", 123, "Analyzing")
    def analyze_pr(self):
        pass
```

## Comparison with ProgressStage

### ProgressStage (Context Manager)
```python
from src.auto_coder.progress_footer import ProgressStage

def my_function():
    with ProgressStage("PR", 123, "Analyzing"):
        # Execute processing
        pass
```

### progress_stage Decorator
```python
from src.auto_coder.progress_decorators import progress_stage

class MyProcessor:
    @progress_stage("PR", 123, "Analyzing")
    def my_method(self):
        # Execute processing (automatically managed progress)
        pass
```

## Nested Decorators

Decorators can be nested properly, with inner decorators being added to the outer decorator's stage stack.

```python
class MyProcessor:
    @progress_stage("Outer Operation")
    def outer_operation(self):
        # Outer stage: [PR #123] Outer Operation

        self.analyze_pr()  # Inner stage: [PR #123] Outer Operation / Analyzing
        self.process_simple()  # Inner stage: [PR #123] Outer Operation / Process Simple

        # Returns to outer stage: [PR #123] Outer Operation
```

## Interaction with Dot Format Output

When using `dot_format=True` in `CommandExecutor` along with `ProgressStage`, dots are automatically printed one line above the progress footer to ensure both outputs remain visible:

- **TTY environments**: Dots use ANSI escape sequences to position themselves one line above the footer
- **Non-TTY environments**: Dots are printed normally without cursor manipulation
- The ProgressStage footer remains at the bottom line of the terminal

This ensures that long-running commands show progress dots while maintaining visibility of the current processing stage.

## Practical Usage Examples

### Usage in GitHub Issue Processor

```python
from src.auto_coder.progress_decorators import progress_stage

class IssueProcessor:
    @progress_stage("Issue", 1, "Analyzing")
    def analyze_issue(self, issue):
        # Issue analysis processing
        self.validate_issue(issue)
        self.create_branch(issue)

    @progress_stage("Issue", 1, "Implementing")
    def implement_fix(self, issue):
        # Fix implementation
        self.modify_code(issue)
        self.run_tests()

    @progress_stage("Issue", 1, "Creating PR")
    def create_pull_request(self, issue):
        # Create PR
        self.commit_changes()
        self.push_branch()
        self.create_pr()
```

### Usage in PR Processor

```python
from src.auto_coder.progress_decorators import progress_stage, progress_context_item

class PRProcessor:
    @progress_stage("PR", 123, "Validating")
    def validate_pr(self, pr):
        # PR validation
        self.check_tests(pr)
        self.check_conflicts(pr)

    @progress_context_item("PR", 123, "Merging")
    def merge_pr(self, pr):
        # PR merge (automatically cleared after completion)
        self.merge_branch(pr)
        self.close_issue(pr)
```

## Error Handling

Decorators are automatically wrapped in `try/finally` blocks, so even if a method raises an exception, the progress stage is properly cleared.

```python
@progress_stage("PR", 123, "Analyzing")
def risky_method(self):
    # Even if an exception occurs, the progress stage is properly popped
    raise ValueError("Something went wrong")
```

## Summary

These decorators provide equivalent functionality to the ProgressStage context manager and can be used more concisely as method decorators. Complex nested operations and automatic error handling are also properly handled.