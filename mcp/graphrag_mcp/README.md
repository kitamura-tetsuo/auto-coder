# GraphRAG MCP Server

A Model Context Protocol server for querying a hybrid graph and vector database system, combining Neo4j (graph database) and Qdrant (vector database) for powerful semantic and graph-based document retrieval.

## Overview

GraphRAG MCP provides a seamless integration between large language models and a hybrid retrieval system that leverages the strengths of both graph databases (Neo4j) and vector databases (Qdrant). This enables:

- Semantic search through document embeddings
- Graph-based context expansion following relationships
- Hybrid search combining vector similarity with graph relationships
- Full integration with Claude and other LLMs through MCP

This project follows the [Model Context Protocol](https://github.com/modelcontextprotocol/python-sdk) specification, making it compatible with any MCP-enabled client.

## Features

- **Semantic search** using sentence embeddings and Qdrant
- **Graph-based context expansion** using Neo4j
- **Hybrid search** combining both approaches
- **MCP tools and resources** for LLM integration
- Full documentation of Neo4j schema and Qdrant collection information

## Prerequisites

- Python 3.12+
- Neo4j running on localhost:7687 (default configuration)
- Qdrant running on localhost:6333 (default configuration)
- Document data indexed in both databases

## Installation

### Quick Start

1. Clone this repository:
   ```bash
   git clone https://github.com/rileylemm/graphrag_mcp.git
   cd graphrag_mcp
   ```

2. Install dependencies with uv:
   ```bash
   uv install
   ```

3. Configure your database connections in the `.env` file:
   ```
   # Neo4j Configuration
   NEO4J_URI=bolt://localhost:7687
   NEO4J_USER=neo4j
   NEO4J_PASSWORD=password

   # Qdrant Configuration
   QDRANT_HOST=localhost
   QDRANT_PORT=6333
   QDRANT_COLLECTION=document_chunks
   ```

4. Run the server:
   ```bash
   uv run main.py
   ```

### Detailed Setup Guide

For a detailed guide on setting up the underlying hybrid database system, please refer to the companion repository: [GraphRAG Hybrid Database](https://github.com/rileylemm/graphrag-hybrid)

#### Setting up Neo4j and Qdrant

1. Install and start Neo4j:
   ```bash
   # Using Docker
   docker run \
     --name neo4j \
     -p 7474:7474 -p 7687:7687 \
     -e NEO4J_AUTH=neo4j/password \
     -v $HOME/neo4j/data:/data \
     -v $HOME/neo4j/logs:/logs \
     -v $HOME/neo4j/import:/import \
     -v $HOME/neo4j/plugins:/plugins \
     neo4j:latest
   ```

2. Install and start Qdrant:
   ```bash
   # Using Docker
   docker run -p 6333:6333 -p 6334:6334 \
     -v $HOME/qdrant/storage:/qdrant/storage \
     qdrant/qdrant
   ```

#### Indexing Documents

To index your documents in both databases, follow these steps:

1. Prepare your documents
2. Create embeddings using sentence-transformers
3. Store documents in Neo4j with relationship information
4. Store document chunk embeddings in Qdrant

Refer to the [GraphRAG Hybrid Database](https://github.com/rileylemm/graphrag-hybrid) repository for detailed indexing scripts and procedures.

## Integration with MCP Clients

### Claude Desktop / Cursor Integration

1. Make the run script executable:
   ```bash
   chmod +x run_server.sh
   ```

2. Add the server to your MCP configuration file (`~/.cursor/mcp.json` or Claude Desktop equivalent):
   ```json
   {
     "mcpServers": {
       "GraphRAG": {
         "command": "/path/to/graphrag_mcp/run_server.sh",
         "args": []
       }
     }
   }
   ```

3. Restart your MCP client (Cursor, Claude Desktop, etc.)

## Usage

### MCP Tools

This server provides the following tools for LLM use:

1. `search_documentation` - Search for information using semantic search
   ```python
   # Example usage in MCP context
   result = search_documentation(
       query="How does graph context expansion work?",
       limit=5,
       category="technical"
   )
   ```

2. `hybrid_search` - Search using both semantic and graph-based approaches
   ```python
   # Example usage in MCP context
   result = hybrid_search(
       query="Vector similarity with graph relationships",
       limit=10,
       category=None,
       expand_context=True
   )
   ```

### MCP Resources

The server provides the following resources:

1. `https://graphrag.db/schema/neo4j` - Information about the Neo4j graph schema
2. `https://graphrag.db/collection/qdrant` - Information about the Qdrant vector collection

## Troubleshooting

- **Connection issues**: Ensure Neo4j and Qdrant are running and accessible
- **Empty results**: Check that your document collection is properly indexed
- **Missing dependencies**: Run `uv install` to ensure all packages are installed
- **Database authentication**: Verify credentials in your `.env` file

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

MIT License

Copyright (c) 2025 Riley Lemm

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Attribution

If you use this MCP server or adapt it for your own purposes, please provide attribution to Riley Lemm and link back to this repository (https://github.com/rileylemm/graphrag_mcp).
