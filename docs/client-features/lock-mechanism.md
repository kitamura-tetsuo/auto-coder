# Lock Mechanism

  lock_mechanism:
    description: "Prevents concurrent auto-coder executions to avoid conflicts and data corruption"
    behavior:
      - "Automatically acquires a lock before executing any command (except read-only commands like 'config' and 'unlock')"
      - "Lock files are stored in the repository's .git directory as 'auto-coder.lock'"
      - "Lock information includes PID, hostname, and start time"
      - "Automatically detects stale locks (process no longer running)"
      - "Lock is released when command completes successfully"
      - "Supports context manager protocol for safe lock acquisition and release"
    usage_patterns:
      context_manager:
        description: "Recommended pattern using Python's with statement"
        example: |
          with LockManager() as lock:
              # Lock is automatically acquired here
              do_work()
          # Lock is automatically released here, even if an exception occurs
        benefits:
          - "Automatic cleanup even on exceptions"
          - "More Pythonic and readable code"
          - "Prevents accidental lock leaks"
      manual_management:
        description: "Explicit acquire/release pattern"
        example: |
          lock = LockManager()
          if lock.acquire():
              try:
                  do_work()
              finally:
                  lock.release()
        use_cases:
          - "Complex control flow requiring early release"
          - "Legacy code compatibility"
    commands:
      auto_lock:
        description: "Lock is automatically acquired by default for all commands"
        exceptions: "Read-only commands ('config', 'unlock', 'auth-status', 'get-actions-logs', 'mcp-pdb', 'health', 'usage-amount')"
      unlock:
        description: "Manually remove a lock file"
        usage: "auto-coder lock unlock"
        options:
          - "--force: Force remove lock file even if process appears to be running (use with caution)"
        behavior:
          - "Shows lock information before removing"
          - "Prevents removal if process is still running (unless --force is used)"
          - "Automatically detects and removes stale locks without --force flag"
    error_handling:
      concurrent_execution:
        description: "When another instance is already running"
        message: "Displays error with lock information (PID, hostname, start time, status)"
        action: "User must wait for existing instance to complete or use 'unlock' command"
      stale_lock:
        description: "When lock file exists but process is no longer running"
        detection: "Checks if PID is still active using os.kill(pid, 0) on Unix or OpenProcess on Windows"
        action: "Safe to remove without --force flag"
    technical_details:
      implementation: "LockManager class in src/auto_coder/lock_manager.py"
      cli_commands: "src/auto_coder/cli_commands_lock.py"
      integration: "Lock check in cli.py before command execution"
      skip_conditions: "Temporary directories (paths containing 'tmp' or 'pytest')"
      storage_format: "JSON file with pid, hostname, and started_at fields"
