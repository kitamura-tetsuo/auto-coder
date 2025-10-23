# Fork Information

## Original Repository

This directory contains a fork of [rileylemm/graphrag_mcp](https://github.com/rileylemm/graphrag_mcp).

- **Original Author**: Riley Lemm
- **Original Repository**: https://github.com/rileylemm/graphrag_mcp
- **License**: MIT License
- **Fork Date**: 2025-01-23

## Purpose of Fork

This fork has been customized for the auto-coder project to provide specialized code analysis capabilities using TypeScript code structure indexed by ts-morph.

### Key Modifications

1. **Specialized for Code Analysis**: Changed from generic documentation search to TypeScript/JavaScript code structure analysis
2. **Custom Graph Schema**: Adapted to work with ts-morph generated graph structure:
   - Node types: `File`, `Function`, `Method`, `Class`, `Interface`, `Type`
   - Relationship types: `CONTAINS`, `CALLS`, `EXTENDS`, `IMPLEMENTS`, `IMPORTS`
3. **Code-Specific Tools**: Added tools for code analysis:
   - `find_symbol`: Find code symbols by fully qualified name
   - `get_call_graph`: Analyze function/method call relationships
   - `get_dependencies`: Analyze file dependencies
   - `impact_analysis`: Analyze change impact across codebase
4. **Enhanced Self-Description**: Updated tool descriptions to reflect code analysis domain

## Maintaining the Fork

### Syncing with Upstream

To sync with upstream changes from the original repository:

```bash
cd mcp/graphrag_mcp
git remote add upstream https://github.com/rileylemm/graphrag_mcp.git
git fetch upstream
git merge upstream/main
# Resolve conflicts, preserving our customizations
```

### Contributing Back

If general improvements are made that could benefit the original project:
1. Create a separate branch with only the general improvements
2. Submit a pull request to https://github.com/rileylemm/graphrag_mcp
3. Reference this fork in the PR description

## Attribution

This fork maintains full attribution to the original author Riley Lemm. All original code is subject to the MIT License as specified in the LICENSE file.

## Differences from Original

### Original Design (rileylemm/graphrag_mcp)
- **Purpose**: Generic documentation search and retrieval
- **Graph Schema**: Document-oriented (Document, Chunk, Category nodes)
- **Tools**: `search_documentation`, `hybrid_search`
- **Use Case**: General knowledge base / documentation systems

### This Fork (auto-coder/graphrag_mcp)
- **Purpose**: TypeScript/JavaScript code analysis
- **Graph Schema**: Code-oriented (File, Symbol nodes with code relationships)
- **Tools**: Code analysis tools (find_symbol, get_call_graph, impact_analysis, etc.)
- **Use Case**: AI-powered code understanding and modification

## Contact

For questions about this fork, please refer to the auto-coder project documentation.
For questions about the original project, please contact Riley Lemm or visit the original repository.

