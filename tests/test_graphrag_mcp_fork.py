"""
Tests for customized GraphRAG MCP fork.

This test verifies that the fork has been properly customized for code analysis.
"""

import os
import sys
import pytest
from pathlib import Path

# Add mcp/graphrag_mcp to path for imports
mcp_path = Path(__file__).parent.parent / "mcp" / "graphrag_mcp"
sys.path.insert(0, str(mcp_path))


def test_fork_info_exists():
    """Test that FORK_INFO.md exists and contains required information."""
    fork_info_path = mcp_path / "FORK_INFO.md"
    assert fork_info_path.exists(), "FORK_INFO.md should exist"
    
    content = fork_info_path.read_text()
    
    # Check for required sections
    assert "Original Repository" in content
    assert "rileylemm/graphrag_mcp" in content
    assert "Purpose of Fork" in content
    assert "code analysis" in content.lower()
    assert "ts-morph" in content.lower()


def test_code_analysis_tool_exists():
    """Test that code_analysis_tool.py exists."""
    code_tool_path = mcp_path / "graphrag_mcp" / "code_analysis_tool.py"
    assert code_tool_path.exists(), "code_analysis_tool.py should exist"


def test_code_analysis_tool_has_required_methods():
    """Test that CodeAnalysisTool has all required methods."""
    from graphrag_mcp.code_analysis_tool import CodeAnalysisTool
    
    # Check that class exists
    assert CodeAnalysisTool is not None
    
    # Check for required methods
    required_methods = [
        'find_symbol',
        'get_call_graph',
        'get_dependencies',
        'impact_analysis',
        'semantic_code_search'
    ]
    
    for method_name in required_methods:
        assert hasattr(CodeAnalysisTool, method_name), \
            f"CodeAnalysisTool should have {method_name} method"


def test_server_imports_code_analysis_tool():
    """Test that server.py imports CodeAnalysisTool instead of DocumentationGPTTool."""
    server_path = mcp_path / "server.py"
    content = server_path.read_text()
    
    # Should import CodeAnalysisTool
    assert "from graphrag_mcp.code_analysis_tool import CodeAnalysisTool" in content
    
    # Should NOT import DocumentationGPTTool
    assert "DocumentationGPTTool" not in content or \
           "from graphrag_mcp.documentation_tool import DocumentationGPTTool" not in content


def test_server_has_code_analysis_tools():
    """Test that server.py defines code analysis tools."""
    server_path = mcp_path / "server.py"
    content = server_path.read_text()
    
    # Check for code analysis tool definitions
    required_tools = [
        'find_symbol',
        'get_call_graph',
        'get_dependencies',
        'impact_analysis',
        'semantic_code_search'
    ]
    
    for tool_name in required_tools:
        assert f"def {tool_name}" in content, \
            f"server.py should define {tool_name} tool"


def test_server_has_enhanced_schema_description():
    """Test that server.py has enhanced schema description for code analysis."""
    server_path = mcp_path / "server.py"
    content = server_path.read_text()
    
    # Check for code-specific terminology in schema resource
    code_terms = [
        'TypeScript',
        'JavaScript',
        'File',
        'Function',
        'Class',
        'CALLS',
        'EXTENDS',
        'IMPLEMENTS',
        'IMPORTS'
    ]
    
    for term in code_terms:
        assert term in content, \
            f"server.py should mention '{term}' in schema description"


def test_mcp_server_name_updated():
    """Test that MCP server name reflects code analysis purpose."""
    server_path = mcp_path / "server.py"
    content = server_path.read_text()
    
    # Server name should reflect code analysis
    assert 'FastMCP("GraphRAG Code Analysis"' in content or \
           'Code Analysis' in content


def test_prompts_yaml_updated():
    """Test that prompts.yaml has been updated with code analysis tools."""
    prompts_path = Path(__file__).parent.parent / "src" / "auto_coder" / "prompts.yaml"
    content = prompts_path.read_text()
    
    # Check for code analysis tools in MCP section
    code_tools = [
        'find_symbol',
        'get_call_graph',
        'get_dependencies',
        'impact_analysis',
        'semantic_code_search'
    ]
    
    mcp_section_start = content.find('mcp:')
    assert mcp_section_start != -1, "prompts.yaml should have mcp section"
    
    mcp_section = content[mcp_section_start:mcp_section_start + 3000]
    
    for tool in code_tools:
        assert tool in mcp_section, \
            f"prompts.yaml MCP section should mention {tool}"


def test_client_features_yaml_updated():
    """Test that client-features.yaml documents the fork."""
    features_path = Path(__file__).parent.parent / "docs" / "client-features.yaml"
    content = features_path.read_text()
    
    # Check for fork documentation
    assert "CUSTOMIZED FORK" in content or "fork" in content.lower()
    assert "mcp/graphrag_mcp" in content
    
    # Check for code analysis tools
    code_tools = [
        'find_symbol',
        'get_call_graph',
        'impact_analysis'
    ]
    
    for tool in code_tools:
        assert tool in content, \
            f"client-features.yaml should document {tool}"


def test_graphrag_integration_doc_exists():
    """Test that GRAPHRAG_MCP_INTEGRATION.md exists and documents the fork."""
    doc_path = Path(__file__).parent.parent / "docs" / "GRAPHRAG_MCP_INTEGRATION.md"
    assert doc_path.exists(), "GRAPHRAG_MCP_INTEGRATION.md should exist"
    
    content = doc_path.read_text()
    
    # Check for key sections
    assert "Why Fork?" in content
    assert "Customizations" in content
    assert "ts-morph" in content
    assert "Code-Specific Tools" in content


def test_code_analysis_tool_method_signatures():
    """Test that CodeAnalysisTool methods have correct signatures."""
    from graphrag_mcp.code_analysis_tool import CodeAnalysisTool
    import inspect
    
    tool = CodeAnalysisTool.__new__(CodeAnalysisTool)  # Don't call __init__ (no DB connection)
    
    # Check find_symbol signature
    sig = inspect.signature(CodeAnalysisTool.find_symbol)
    assert 'fqname' in sig.parameters
    
    # Check get_call_graph signature
    sig = inspect.signature(CodeAnalysisTool.get_call_graph)
    assert 'symbol_id' in sig.parameters
    assert 'direction' in sig.parameters
    assert 'depth' in sig.parameters
    
    # Check get_dependencies signature
    sig = inspect.signature(CodeAnalysisTool.get_dependencies)
    assert 'file_path' in sig.parameters
    
    # Check impact_analysis signature
    sig = inspect.signature(CodeAnalysisTool.impact_analysis)
    assert 'symbol_ids' in sig.parameters
    assert 'max_depth' in sig.parameters
    
    # Check semantic_code_search signature
    sig = inspect.signature(CodeAnalysisTool.semantic_code_search)
    assert 'query' in sig.parameters
    assert 'limit' in sig.parameters
    assert 'kind_filter' in sig.parameters


def test_original_documentation_tool_not_used():
    """Test that original documentation_tool.py is not imported in server.py."""
    server_path = mcp_path / "server.py"
    content = server_path.read_text()
    
    # Should not use DocumentationGPTTool
    assert "doc_tool" not in content or "code_tool" in content
    
    # All references should be to code_tool
    if "doc_tool" in content:
        # Count occurrences
        doc_tool_count = content.count("doc_tool")
        code_tool_count = content.count("code_tool")
        assert code_tool_count > doc_tool_count, \
            "server.py should primarily use code_tool, not doc_tool"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

