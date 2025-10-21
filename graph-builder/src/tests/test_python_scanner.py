"""
Tests for Python scanner
"""

import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scanner.python_scanner import scan_python_file, scan_python_project


def test_scan_python_file():
    """Test scanning a single Python file"""
    sample_file = Path(__file__).parent.parent.parent / 'sample-repo' / 'python' / 'sample.py'
    
    if not sample_file.exists():
        print(f"Sample file not found: {sample_file}")
        return
    
    result = scan_python_file(str(sample_file), 'sample')
    
    assert len(result.nodes) > 0, "Should have nodes"
    assert len(result.edges) > 0, "Should have edges"
    
    # Check for expected nodes
    function_nodes = [n for n in result.nodes if n.kind == 'Function']
    class_nodes = [n for n in result.nodes if n.kind == 'Class']
    method_nodes = [n for n in result.nodes if n.kind == 'Method']
    
    assert len(function_nodes) > 0, "Should have function nodes"
    assert len(class_nodes) > 0, "Should have class nodes"
    assert len(method_nodes) > 0, "Should have method nodes"
    
    # Check node properties
    for node in result.nodes:
        assert node.id, "Node should have id"
        assert node.kind, "Node should have kind"
        assert node.fqname, "Node should have fqname"
        assert node.sig is not None, "Node should have sig"
        assert node.short, "Node should have short"
        assert node.complexity >= 0, "Node should have complexity >= 0"
        assert node.tokens_est > 0, "Node should have tokens_est > 0"
    
    print(f"✓ Found {len(result.nodes)} nodes")
    print(f"✓ Found {len(result.edges)} edges")
    print(f"✓ Functions: {len(function_nodes)}")
    print(f"✓ Classes: {len(class_nodes)}")
    print(f"✓ Methods: {len(method_nodes)}")


def test_fqname_generation():
    """Test fqname generation"""
    sample_file = Path(__file__).parent.parent.parent / 'sample-repo' / 'python' / 'sample.py'
    
    if not sample_file.exists():
        print(f"Sample file not found: {sample_file}")
        return
    
    result = scan_python_file(str(sample_file), 'sample')
    
    # Find specific function
    fetch_user_func = next(
        (n for n in result.nodes if n.kind == 'Function' and 'fetch_user_from_api' in n.fqname),
        None
    )
    
    assert fetch_user_func is not None, "Should find fetch_user_from_api function"
    assert 'sample:fetch_user_from_api' in fetch_user_func.fqname, "fqname should be correct"
    
    print(f"✓ fqname: {fetch_user_func.fqname}")


def test_signature_generation():
    """Test signature generation"""
    sample_file = Path(__file__).parent.parent.parent / 'sample-repo' / 'python' / 'sample.py'
    
    if not sample_file.exists():
        print(f"Sample file not found: {sample_file}")
        return
    
    result = scan_python_file(str(sample_file), 'sample')
    
    # Find specific function
    calculate_age_func = next(
        (n for n in result.nodes if n.kind == 'Function' and 'calculate_age' in n.fqname),
        None
    )
    
    assert calculate_age_func is not None, "Should find calculate_age function"
    assert 'int' in calculate_age_func.sig, "Signature should contain int"
    
    print(f"✓ signature: {calculate_age_func.sig}")


def test_tag_detection():
    """Test tag detection"""
    sample_file = Path(__file__).parent.parent.parent / 'sample-repo' / 'python' / 'sample.py'
    
    if not sample_file.exists():
        print(f"Sample file not found: {sample_file}")
        return
    
    result = scan_python_file(str(sample_file), 'sample')
    
    # Find async function
    async_func = next(
        (n for n in result.nodes if n.kind == 'Function' and 'fetch_user_from_api' in n.fqname),
        None
    )
    
    assert async_func is not None, "Should find async function"
    assert 'ASYNC' in async_func.tags, "Should have ASYNC tag"
    
    print(f"✓ tags: {async_func.tags}")


if __name__ == '__main__':
    print("Running Python scanner tests...")
    print()
    
    print("Test 1: Scan Python file")
    test_scan_python_file()
    print()
    
    print("Test 2: FQName generation")
    test_fqname_generation()
    print()
    
    print("Test 3: Signature generation")
    test_signature_generation()
    print()
    
    print("Test 4: Tag detection")
    test_tag_detection()
    print()
    
    print("All tests passed! ✓")

