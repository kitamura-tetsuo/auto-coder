#!/usr/bin/env python3
"""
Test script to see if there are import errors when loading the CLI components
"""

import sys
import os

# Add the src directory to the path to simulate the installed package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

try:
    # Try to import the CLI module to see what happens
    from auto_coder.cli import main
    print("Successfully imported CLI module")
    
    # Try to call the CLI with --help via Click testing interface
    from click.testing import CliRunner
    
    # Create a runner for testing
    runner = CliRunner()
    
    # Test help output
    result = runner.invoke(main, ['--help'])
    print(f"Exit code: {result.exit_code}")
    print(f"Output:\n{result.output}")
    
    if result.exit_code != 0:
        print(f"Error: {result.exception}")
        if result.exception:
            import traceback
            traceback.print_exception(type(result.exception), result.exception, result.exception.__traceback__)
    
    # Check for presence of expected strings
    if "Usage:" in result.output:
        print("✓ Found 'Usage:' in output")
    else:
        print("✗ Missing 'Usage:' in output")
        
    if "Auto-Coder" in result.output:
        print("✓ Found 'Auto-Coder' in output")
    else:
        print("✗ Missing 'Auto-Coder' in output")

except Exception as e:
    print(f"Import error: {e}")
    import traceback
    traceback.print_exc()