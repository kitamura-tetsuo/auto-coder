# Runtime Health Monitoring

  health_monitoring:
    description: "Records resource usage and termination causes so a run that stops after hours can be diagnosed."
    implementation: "src/auto_coder/health_monitor.py, summarized by 'auto-coder health' (src/auto_coder/cli_commands_health.py)"
    application_log:
      - "setup_logger writes to ~/.auto-coder/logs/auto-coder.log when --log-file is not given (10 MB rotation, 14 days retention, zip compression)."
      - "AUTO_CODER_LOG_FILE overrides that path; AUTO_CODER_FILE_LOG_LEVEL sets the file sink level independently of the console (e.g. DEBUG on file, INFO on console)."
      - "No default file is created under pytest; LLM_LOGGING_DISABLED still disables file logging."
    snapshots:
      - "A daemon thread appends a ResourceSnapshot every AUTO_CODER_HEALTH_INTERVAL_SECONDS (default 60) to ~/.auto-coder/logs/health-YYYY-MM-DD.jsonl (0600) and logs a one-line 'HEALTH ...' summary."
      - "Each snapshot holds RSS, peak RSS, VMS, growth since startup, system MemAvailable/swap, cgroup memory limit/usage/oom_kill counter, CPU time of the process and its children, major page faults, thread count, open file descriptors, child processes, pending asyncio tasks, GC generation counts, load average and free disk space."
      - "AUTO_CODER_HEALTH_DEEP=1 also counts every GC tracked object; AUTO_CODER_HEALTH_TRACEMALLOC=1 starts tracemalloc and logs the top allocation sites with each snapshot."
      - "Files older than AUTO_CODER_HEALTH_RETENTION_DAYS (default 14) are pruned at startup; AUTO_CODER_HEALTH_LOG_ENABLED=0 disables the whole feature."
    warnings:
      - "Warns when cgroup memory usage passes 90% of the container limit or system available memory drops below 10% (imminent OOM kill)."
      - "Warns when the resident set doubles relative to the startup baseline (leak detection) and when free disk space drops below 512MB."
      - "Reports the cgroup oom_kill counter so kernel kills of child processes are visible."
    heartbeats:
      - "The producer loop, worker loops and the synchronous run() loop report their stage (e.g. 'worker-0:processing pr #42')."
      - "When no heartbeat arrives for AUTO_CODER_HEALTH_STALL_SECONDS (default 3600) a stall is logged once with a stack dump of every thread; the next heartbeat records 'stall_recovered'."
    termination_causes:
      - "SIGTERM/SIGHUP/SIGINT/SIGQUIT are logged with the interrupted stack and a final snapshot before the default behaviour is restored (SIGINT still raises KeyboardInterrupt)."
      - "faulthandler writes native crashes to ~/.auto-coder/logs/diagnostics-<pid>.log; 'kill -USR1 <pid>' dumps all thread stacks on demand."
      - "Uncaught exceptions (main thread, worker threads and asyncio callbacks) and interpreter exit are recorded as events."
      - "Producer/worker loops record why they ended; start_automation logs a warning when every task exits on its own instead of stopping silently."
      - "Auto-update restarts (os.execvpe in update_manager) record an 'auto_update_restart' event and a snapshot so the log break is explained."
    cli:
      - "'auto-coder health [--log-dir DIR] [--days N] [--events N]' summarizes snapshot counts, memory trend, container limit, peak file descriptors/child processes and recorded events per health file."
