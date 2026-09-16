# Startup Memory

  startup_memory:
    description: "CLI startup avoids loading heavy dependencies that a given run will not use."
    resident_set_size: "~72MB to import auto_coder.cli (previously ~653MB)"
    lazy_backend_clients:
      - "CodexClient, GeminiClient, ClaudeClient, QwenClient, AuggieClient, AiderClient and CodexMCPClient are imported inside their factory functions in cli_helpers.py"
      - "A run instantiates only the selected backend, so aider and google.generativeai are never imported for other backends"
      - "Tests must patch these clients at their defining module (e.g. src.auto_coder.codex_client.CodexClient)"
    removed_dependencies:
      - "GraphRAG/RAG support was removed; sentence-transformers, torch, neo4j and qdrant-client are no longer dependencies"
    rules:
      - "Never import an optional or heavy dependency at module import time to probe availability; use importlib.util.find_spec()"
