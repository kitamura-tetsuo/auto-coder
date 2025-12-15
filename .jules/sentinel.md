## 2025-02-18 - Hardcoded Neo4j Credentials
**Vulnerability:** The GraphRAG integration used a hardcoded "password" for Neo4j in `docker-compose.graphrag.yml`, `cli_helpers.py`, and `cli_commands_graphrag.py`. This credential was also written to a local `.env` file for the MCP server.
**Learning:** Default credentials in development tools can be dangerous if the tool is deployed or used in shared environments. Hardcoding them in multiple places makes it difficult for users to secure their setup.
**Prevention:** Use environment variables for sensitive credentials with sensible defaults (if necessary for dev experience), but always allow overrides. Use `${VAR:-default}` in docker-compose and `os.getenv` in Python.
