#!/usr/bin/env python3
"""
Helper script to monitor a directory and launch 'auto-coder process-issues' for each subdirectory.
This script watches for new directories and starts an auto-coder process if the directory is a git repository.
"""

import os
import sys
import subprocess
import argparse
import time
import signal
import logging
from typing import Dict, List

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    print("Error: 'watchdog' package is not installed.")
    print("Please install it with: pip install watchdog")
    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class AutoCoderWatcher(FileSystemEventHandler):
    def __init__(self, command_args: List[str], log_dir: str):
        self.command_args = command_args
        self.processes: Dict[str, subprocess.Popen] = {}
        self.log_dir = os.path.abspath(log_dir)
        os.makedirs(self.log_dir, exist_ok=True)

    def on_created(self, event):
        if event.is_directory:
            # Short delay to allow directory content (like .git) to be created
            # if the directory was created via 'git clone' or similar
            time.sleep(2)
            self.start_process(event.src_path)

    def is_git_repo(self, path: str) -> bool:
        """Check if the directory is a git repository."""
        return os.path.isdir(os.path.join(path, ".git"))

    def start_process(self, path: str):
        """Start an auto-coder process for the given directory if not already running."""
        abs_path = os.path.abspath(path)
        
        # Skip hidden directories
        if os.path.basename(abs_path).startswith('.'):
            return

        # We only start processes for git repositories
        if not self.is_git_repo(abs_path):
            logger.debug(f"Skipping non-git directory: {abs_path}")
            return

        if abs_path in self.processes:
            # Check if process is still running
            if self.processes[abs_path].poll() is None:
                logger.debug(f"Process already running for {abs_path}")
                return
        
        dir_name = os.path.basename(abs_path)
        log_file = os.path.join(self.log_dir, f"{dir_name}.log")
        
        logger.info(f"Starting 'auto-coder process-issues' for: {abs_path}")
        logger.info(f"Logging to: {log_file}")
        
        # Command to run. Note: 'process-issues' is the correct subcommand.
        cmd = ["auto-coder", "process-issues"] + self.command_args
        
        try:
            with open(log_file, "a") as f:
                f.write(f"\n--- Started at {time.ctime()} ---\n")
                f.write(f"Command: {' '.join(cmd)}\n")
                f.write(f"Working Directory: {abs_path}\n\n")
                f.flush()
                
                # Start the process in the background
                proc = subprocess.Popen(
                    cmd,
                    cwd=abs_path,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True  # Prevent signals to parent from killing children immediately
                )
                self.processes[abs_path] = proc
        except Exception as e:
            logger.error(f"Failed to start process in {abs_path}: {e}")

    def cleanup_finished(self):
        """Clean up references to processes that have finished."""
        finished = []
        for path, proc in self.processes.items():
            ret = proc.poll()
            if ret is not None:
                logger.info(f"Process for {path} finished with return code {ret}")
                finished.append(path)
        for path in finished:
            del self.processes[path]

    def stop_all(self, signum=None, frame=None):
        """Terminate all background processes."""
        if signum:
            logger.info(f"Received signal {signum}. Shutting down...")
        else:
            logger.info("Shutting down...")

        for path, proc in self.processes.items():
            if proc.poll() is None:
                logger.info(f"Terminating process for {path}")
                proc.terminate()
        
        # Wait up to 5 seconds for termination
        start_wait = time.time()
        while time.time() - start_wait < 5:
            if all(proc.poll() is not None for proc in self.processes.values()):
                break
            time.sleep(0.5)
        
        # Kill any remaining processes
        for path, proc in self.processes.items():
            if proc.poll() is None:
                logger.info(f"Killing process for {path}")
                proc.kill()
        
        if signum:
            sys.exit(0)

def main():
    parser = argparse.ArgumentParser(
        description="Watch a directory for subdirectories and launch 'auto-coder process-issues' for each.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example:
  python3 scripts/process_issues_watcher.py /workspaces/projects --opts "--disable-graphrag --verbose"
        """
    )
    parser.add_argument("watch_path", nargs="?", default=".", help="Directory to watch (default: current directory)")
    parser.add_argument("--opts", help="Options to pass to auto-coder (as a single string)", default="")
    parser.add_argument("--log-dir", default="logs/auto-coder-watcher", help="Directory where logs will be stored (default: logs/auto-coder-watcher)")
    
    args = parser.parse_args()
    
    watch_path = os.path.abspath(args.watch_path)
    if not os.path.isdir(watch_path):
        print(f"Error: {watch_path} is not a directory.")
        sys.exit(1)
        
    # Split options string while respecting quotes if necessary
    import shlex
    auto_coder_opts = shlex.split(args.opts) if args.opts else []
    
    event_handler = AutoCoderWatcher(auto_coder_opts, args.log_dir)
    
    # Register signal handlers for clean exit
    signal.signal(signal.SIGINT, event_handler.stop_all)
    signal.signal(signal.SIGTERM, event_handler.stop_all)
    
    # Initial scan of existing directories
    logger.info(f"Performing initial scan of {watch_path}")
    try:
        for dir_entry in os.scandir(watch_path):
            if dir_entry.is_dir():
                event_handler.start_process(dir_entry.path)
    except Exception as e:
        logger.error(f"Error during initial scan: {e}")
            
    # Setup and start the observer
    observer = Observer()
    observer.schedule(event_handler, watch_path, recursive=False)
    observer.start()
    logger.info(f"Monitoring {watch_path} for new directories using watchdog...")
    
    try:
        while True:
            time.sleep(10)
            event_handler.cleanup_finished()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"Error in main loop: {e}")
    finally:
        observer.stop()
        observer.join()
        event_handler.stop_all()

if __name__ == "__main__":
    main()
