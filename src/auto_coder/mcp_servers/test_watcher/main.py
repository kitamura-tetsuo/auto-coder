"""
Main entry point for Test Watcher MCP Server.
"""

import os
from dotenv import load_dotenv

def main():
    # Load environment variables from .env file
    load_dotenv()
    
    # Import here to avoid circular imports
    from server import mcp
    
    # Run the MCP server directly
    mcp.run()

if __name__ == "__main__":
    main()

