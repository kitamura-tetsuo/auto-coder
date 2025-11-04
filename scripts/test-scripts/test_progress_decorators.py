"""
Test file for progress decorators functionality.
"""

import time
from src.auto_coder.progress_decorators import (
    progress_stage,
    progress_method,
    progress_context_item,
    ProgressStageDecorator
)
from src.auto_coder.progress_footer import clear_progress


class TestProcessor:
    """Test class to demonstrate decorator usage."""
    
    @progress_stage()
    def process_simple(self):
        """Simple method with automatic stage name."""
        print("Processing with auto-generated stage name...")
        time.sleep(1)
    
    @progress_stage("Custom Processing Stage")
    def process_with_custom_stage(self):
        """Method with custom stage name."""
        print("Processing with custom stage name...")
        time.sleep(1)
    
    @progress_stage("PR", 123, "Analyzing Code")
    def analyze_pr(self):
        """Method with PR context and stage."""
        print("Analyzing PR with context...")
        time.sleep(1)
    
    @progress_stage("Issue", 456, "Processing", [789], "feature-branch")
    def process_issue_with_context(self):
        """Method with full context information."""
        print("Processing issue with full context...")
        time.sleep(1)
    
    @progress_method()
    def method_with_method_decorator(self):
        """Method using progress_method decorator."""
        print("Using progress_method decorator...")
        time.sleep(1)
    
    @progress_method("Custom Method Stage")
    def method_with_custom_method_stage(self):
        """Method using progress_method with custom stage."""
        print("Using progress_method with custom stage...")
        time.sleep(1)
    
    @progress_context_item("PR", 789, "Full Analysis")
    def analyze_full_context(self):
        """Method using progress_context_item decorator."""
        print("Analyzing with full context...")
        time.sleep(1)
    
    @ProgressStageDecorator("Class-based Decorator")
    def method_with_class_decorator(self):
        """Method using ProgressStageDecorator class."""
        print("Using class-based decorator...")
        time.sleep(1)
    
    @ProgressStageDecorator("PR", 321, "Deep Analysis", related_issues=[654], branch_name="deep-fix")
    def deep_analysis(self):
        """Method using ProgressStageDecorator with context."""
        print("Performing deep analysis...")
        time.sleep(1)


def test_all_decorators():
    """Test all decorator variations."""
    processor = TestProcessor()
    
    print("=== Testing Progress Decorators ===\n")
    
    # Clear any existing progress
    clear_progress()
    
    print("1. Testing progress_stage() with auto-generated name:")
    processor.process_simple()
    print()
    
    print("2. Testing progress_stage() with custom stage name:")
    processor.process_with_custom_stage()
    print()
    
    print("3. Testing progress_stage() with PR context:")
    processor.analyze_pr()
    print()
    
    print("4. Testing progress_stage() with full context:")
    processor.process_issue_with_context()
    print()
    
    print("5. Testing progress_method() decorator:")
    processor.method_with_method_decorator()
    print()
    
    print("6. Testing progress_method() with custom stage:")
    processor.method_with_custom_method_stage()
    print()
    
    print("7. Testing progress_context_item() decorator:")
    processor.analyze_full_context()
    print()
    
    print("8. Testing ProgressStageDecorator class:")
    processor.method_with_class_decorator()
    print()
    
    print("9. Testing ProgressStageDecorator with context:")
    processor.deep_analysis()
    print()
    
    print("=== All decorator tests completed ===")


def test_nested_decorators():
    """Test nested decorator usage."""
    processor = TestProcessor()
    
    print("\n=== Testing Nested Decorators ===\n")
    clear_progress()
    
    @progress_stage("Outer Operation")
    def outer_operation():
        """Outer operation that calls inner decorated methods."""
        print("Starting outer operation...")
        
        processor.process_simple()
        processor.analyze_pr()
        processor.method_with_method_decorator()
        
        print("Completed outer operation...")
    
    outer_operation()
    print("=== Nested decorator tests completed ===")


if __name__ == "__main__":
    test_all_decorators()
    test_nested_decorators()