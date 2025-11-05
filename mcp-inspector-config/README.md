# MCP Inspector Configuration

This directory contains configuration files for visualizing MCP servers with MCP Inspector.

## Setup Instructions

### 1. Starting MCP Inspector

Start MCP Inspector with the following command:

```bash
mcp-inspector /home/node/src/auto-coder/mcp-inspector-config/mcp-servers.json
```

### 2. Access via Web Browser

Inspector starts by default at http://localhost:5173. Access it via your browser.

## Connected MCP Servers

### 1. graphrag-mcp
- **Description**: GraphRAG code analysis server
- **Features**:
  - Code threshold search
  - Call graph analysis
  - Dependency analysis
  - Impact analysis
  - Semantic code search
- **Dependencies**: neo4j, qdrant-client, sentence-transformers

### 2. test-watcher
- **Description**: Test monitoring server (file change monitoring and automatic test execution)
- **Features**:
  - Start/stop file watching
  - Query test results
  - Get status
- **Dependencies**: loguru, watchdog, pathspec

## Environment Requirements

### graphrag-mcp
- Neo4j database must be running
- Qdrant vector database must be running
- Code graph must be built

### test-watcher
- Node.js and npm must be installed
- Playwright must be installed: `npm install -D @playwright/test`
- Project root must be `/home/node/src/auto-coder`

## Troubleshooting

### Server doesn't start
1. Ensure required dependencies are installed
2. Ensure environment variables are correctly set
3. Check logs for detailed error information

### Database connection error (graphrag-mcp)
Neo4j or Qdrant might not be running:
```bash
# Start Neo4j (example)
neo4j start

# Start Qdrant (example)
qdrant
```
