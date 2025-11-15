#!/usr/bin/env python3
"""Test script to verify the CLI setup"""

import sys
import os

# Add the src directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

try:
    from auto_coder.cli import main
    print("Successfully imported main from auto_coder.cli")
    
    # Check if the name is properly set
    print(f"Function name: {main.name}")
    print(f"Function doc: {main.__doc__}")
    
    # Test that it's a Click group
    import click
    print(f"Is Click group: {isinstance(main, click.Group)}")
    
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()