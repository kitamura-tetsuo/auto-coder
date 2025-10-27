"""
Test Watcher Tool - Manages continuous test execution and result collection.
"""

import os
import subprocess
import threading
import time
import json
from typing import Dict, Any, List, Optional, Set
from datetime import datetime
from pathlib import Path
from loguru import logger
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent
import pathspec


class GitIgnoreFileHandler(FileSystemEventHandler):
    """File system event handler that respects .gitignore patterns."""

    def __init__(self, project_root: Path, gitignore_spec: pathspec.PathSpec, callback):
        self.project_root = project_root
        self.gitignore_spec = gitignore_spec
        self.callback = callback
        self.last_event_time: Dict[str, float] = {}
        self.debounce_seconds = 0.5

    def should_ignore(self, path: str) -> bool:
        """Check if path should be ignored based on .gitignore."""
        try:
            rel_path = Path(path).relative_to(self.project_root)
            return self.gitignore_spec.match_file(str(rel_path))
        except (ValueError, Exception):
            return True

    def on_modified(self, event: FileSystemEvent):
        """Handle file modification events."""
        if event.is_directory:
            return

        # Debounce events
        now = time.time()
        if event.src_path in self.last_event_time:
            if now - self.last_event_time[event.src_path] < self.debounce_seconds:
                return

        self.last_event_time[event.src_path] = now

        if not self.should_ignore(event.src_path):
            logger.debug(f"File modified: {event.src_path}")
            self.callback(event.src_path)


class TestWatcherTool:
    """Tool for watching and managing test execution."""

    def __init__(self, project_root: Optional[str] = None):
        """
        Initialize the test watcher.

        Args:
            project_root: Root directory of the project to watch. Defaults to current directory.
        """
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.lock = threading.Lock()

        # Test results storage by test type
        self.test_results: Dict[str, Dict[str, Any]] = {
            "unit": {"status": "idle", "tests": []},
            "integration": {"status": "idle", "tests": []},
            "e2e": {"status": "idle", "tests": []},
        }

        # Playwright process management
        self.playwright_process: Optional[subprocess.Popen] = None
        self.playwright_running = False

        # File watcher
        self.observer: Optional[Observer] = None
        self.gitignore_spec = self._load_gitignore()

        # Failed tests tracking for --last-failed
        self.last_failed_tests: Set[str] = set()

        logger.info(f"TestWatcherTool initialized with project root: {self.project_root}")

    def _load_gitignore(self) -> pathspec.PathSpec:
        """Load .gitignore patterns."""
        gitignore_path = self.project_root / ".gitignore"
        patterns = []

        if gitignore_path.exists():
            with open(gitignore_path, "r") as f:
                patterns = f.read().splitlines()

        # Add common patterns to ignore
        patterns.extend([
            ".git/",
            "node_modules/",
            "__pycache__/",
            "*.pyc",
            ".venv/",
            "venv/",
            "dist/",
            "build/",
            ".pytest_cache/",
            "test-results/",
            "playwright-report/",
        ])

        return pathspec.PathSpec.from_lines("gitwildmatch", patterns)
    
    def start_watching(self) -> Dict[str, Any]:
        """
        Start file watching and automatic test execution.

        Returns:
            Status of the watcher startup
        """
        with self.lock:
            if self.observer is not None:
                return {
                    "status": "already_running",
                    "message": "File watcher is already running"
                }

            try:
                handler = GitIgnoreFileHandler(
                    self.project_root,
                    self.gitignore_spec,
                    self._on_file_changed
                )

                self.observer = Observer()
                self.observer.schedule(handler, str(self.project_root), recursive=True)
                self.observer.start()

                logger.info("Started file watcher")

                return {
                    "status": "started",
                    "message": "File watcher started successfully",
                    "project_root": str(self.project_root)
                }

            except Exception as e:
                logger.error(f"Failed to start file watcher: {e}")
                return {
                    "status": "error",
                    "error": str(e)
                }

    def stop_watching(self) -> Dict[str, Any]:
        """
        Stop file watching.

        Returns:
            Status of the watcher shutdown
        """
        with self.lock:
            if self.observer is None:
                return {
                    "status": "not_running",
                    "message": "File watcher is not running"
                }

            try:
                self.observer.stop()
                self.observer.join(timeout=5)
                self.observer = None

                logger.info("Stopped file watcher")

                return {
                    "status": "stopped",
                    "message": "File watcher stopped successfully"
                }

            except Exception as e:
                logger.error(f"Failed to stop file watcher: {e}")
                return {
                    "status": "error",
                    "error": str(e)
                }

    def _on_file_changed(self, file_path: str):
        """
        Callback when a file is changed.

        Args:
            file_path: Path to the changed file
        """
        logger.info(f"File changed: {file_path}")

        # Trigger Playwright test run
        threading.Thread(
            target=self._run_playwright_tests,
            args=(True,),  # last_failed=True
            daemon=True
        ).start()
    
    def _run_playwright_tests(self, last_failed: bool = False):
        """
        Run Playwright tests (one-shot execution).

        Args:
            last_failed: Whether to run only last failed tests
        """
        with self.lock:
            # Kill existing Playwright process if running
            if self.playwright_process and self.playwright_process.poll() is None:
                logger.info("Terminating existing Playwright process")
                self.playwright_process.terminate()
                try:
                    self.playwright_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.playwright_process.kill()

            self.test_results["e2e"]["status"] = "running"

        try:
            # Build command
            cmd = ["npx", "playwright", "test", "--reporter=json"]

            if last_failed and self.last_failed_tests:
                # Add specific test files that failed
                for test_file in self.last_failed_tests:
                    cmd.append(test_file)

            logger.info(f"Running Playwright tests: {' '.join(cmd)}")

            # Run tests
            process = subprocess.Popen(
                cmd,
                cwd=str(self.project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            with self.lock:
                self.playwright_process = process

            stdout, stderr = process.communicate()

            # Parse JSON report
            report = self._parse_playwright_json_report(stdout)

            with self.lock:
                self.test_results["e2e"] = report
                self.playwright_process = None

                # If all tests passed and we ran with --last-failed, run all tests
                if last_failed and report["failed"] == 0 and self.last_failed_tests:
                    logger.info("All failed tests passed, running full test suite")
                    self.last_failed_tests.clear()
                    # Schedule full test run
                    threading.Thread(
                        target=self._run_playwright_tests,
                        args=(False,),
                        daemon=True
                    ).start()
                elif report["failed"] > 0:
                    # Update failed tests list
                    self.last_failed_tests = {
                        test["file"] for test in report["tests"] if test["status"] == "failed"
                    }

            logger.info(f"Playwright tests completed: {report['passed']} passed, {report['failed']} failed")

        except Exception as e:
            logger.error(f"Error running Playwright tests: {e}")
            with self.lock:
                self.test_results["e2e"] = {
                    "status": "error",
                    "error": str(e),
                    "passed": 0,
                    "failed": 0,
                    "flaky": 0,
                    "skipped": 0,
                    "tests": []
                }
                self.playwright_process = None
    
    def _parse_playwright_json_report(self, json_output: str) -> Dict[str, Any]:
        """
        Parse Playwright JSON report.

        Args:
            json_output: JSON output from Playwright

        Returns:
            Parsed test results
        """
        try:
            data = json.loads(json_output)

            passed = 0
            failed = 0
            flaky = 0
            skipped = 0
            tests = []

            for suite in data.get("suites", []):
                for spec in suite.get("specs", []):
                    for test in spec.get("tests", []):
                        test_info = {
                            "file": spec.get("file", ""),
                            "title": spec.get("title", ""),
                            "status": "",
                            "error": None
                        }

                        # Determine status
                        results = test.get("results", [])
                        if not results:
                            continue

                        result = results[0]
                        status = result.get("status", "")

                        if status == "passed":
                            passed += 1
                            test_info["status"] = "passed"
                        elif status == "failed":
                            failed += 1
                            test_info["status"] = "failed"
                            # Extract error message
                            error = result.get("error", {})
                            if error:
                                test_info["error"] = error.get("message", str(error))
                        elif status == "skipped":
                            skipped += 1
                            test_info["status"] = "skipped"
                        elif status == "flaky":
                            flaky += 1
                            test_info["status"] = "flaky"
                            # Extract error from first failed attempt
                            for r in results:
                                if r.get("status") == "failed":
                                    error = r.get("error", {})
                                    if error:
                                        test_info["error"] = error.get("message", str(error))
                                    break

                        tests.append(test_info)

            return {
                "status": "completed",
                "passed": passed,
                "failed": failed,
                "flaky": flaky,
                "skipped": skipped,
                "total": passed + failed + flaky + skipped,
                "tests": tests,
                "last_updated": datetime.now().isoformat()
            }

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Playwright JSON report: {e}")
            return {
                "status": "error",
                "error": f"JSON parse error: {e}",
                "passed": 0,
                "failed": 0,
                "flaky": 0,
                "skipped": 0,
                "tests": []
            }
        except Exception as e:
            logger.error(f"Error parsing Playwright report: {e}")
            return {
                "status": "error",
                "error": str(e),
                "passed": 0,
                "failed": 0,
                "flaky": 0,
                "skipped": 0,
                "tests": []
            }

    def query_test_results(self, test_type: str = "all") -> Dict[str, Any]:
        """
        Query test results.

        Args:
            test_type: Type of tests to query (unit/integration/e2e/all)

        Returns:
            Test results with counts and error details
        """
        with self.lock:
            if test_type == "all":
                # Aggregate all test types
                total_passed = sum(self.test_results[t].get("passed", 0) for t in ["unit", "integration", "e2e"])
                total_failed = sum(self.test_results[t].get("failed", 0) for t in ["unit", "integration", "e2e"])
                total_flaky = sum(self.test_results[t].get("flaky", 0) for t in ["unit", "integration", "e2e"])
                total_skipped = sum(self.test_results[t].get("skipped", 0) for t in ["unit", "integration", "e2e"])

                # Check if any tests are running
                running = any(self.test_results[t].get("status") == "running" for t in ["unit", "integration", "e2e"])

                if running:
                    return {
                        "status": "running",
                        "message": "Tests are currently running. Please wait."
                    }

                # Collect all failed tests
                all_failed_tests = []
                for test_type_key in ["unit", "integration", "e2e"]:
                    tests = self.test_results[test_type_key].get("tests", [])
                    all_failed_tests.extend([t for t in tests if t.get("status") == "failed"])

                # Collect all flaky tests
                all_flaky_tests = []
                for test_type_key in ["unit", "integration", "e2e"]:
                    tests = self.test_results[test_type_key].get("tests", [])
                    all_flaky_tests.extend([t for t in tests if t.get("status") == "flaky"])

                result = {
                    "status": "completed",
                    "test_type": "all",
                    "summary": {
                        "passed": total_passed,
                        "failed": total_failed,
                        "flaky": total_flaky,
                        "skipped": total_skipped,
                        "total": total_passed + total_failed + total_flaky + total_skipped
                    },
                    "failed_tests": {
                        "count": len(all_failed_tests),
                        "tests": all_failed_tests
                    },
                    "flaky_tests": {
                        "count": len(all_flaky_tests),
                        "tests": all_flaky_tests
                    }
                }

                # Add first failed test
                if all_failed_tests:
                    result["first_failed_test"] = all_failed_tests[0]

                # Add first flaky test
                if all_flaky_tests:
                    result["first_flaky_test"] = all_flaky_tests[0]

                return result

            else:
                # Single test type
                if test_type not in self.test_results:
                    return {
                        "status": "error",
                        "error": f"Invalid test type: {test_type}. Must be one of: unit, integration, e2e, all"
                    }

                test_data = self.test_results[test_type]

                if test_data.get("status") == "running":
                    return {
                        "status": "running",
                        "test_type": test_type,
                        "message": f"{test_type} tests are currently running. Please wait."
                    }

                failed_tests = [t for t in test_data.get("tests", []) if t.get("status") == "failed"]
                flaky_tests = [t for t in test_data.get("tests", []) if t.get("status") == "flaky"]

                result = {
                    "status": test_data.get("status", "idle"),
                    "test_type": test_type,
                    "summary": {
                        "passed": test_data.get("passed", 0),
                        "failed": test_data.get("failed", 0),
                        "flaky": test_data.get("flaky", 0),
                        "skipped": test_data.get("skipped", 0),
                        "total": test_data.get("total", 0)
                    },
                    "failed_tests": {
                        "count": len(failed_tests),
                        "tests": failed_tests
                    },
                    "flaky_tests": {
                        "count": len(flaky_tests),
                        "tests": flaky_tests
                    }
                }

                # Add first failed test
                if failed_tests:
                    result["first_failed_test"] = failed_tests[0]

                # Add first flaky test
                if flaky_tests:
                    result["first_flaky_test"] = flaky_tests[0]

                return result
    
    def get_status(self) -> Dict[str, Any]:
        """
        Get overall status of the test watcher.

        Returns:
            Status information
        """
        with self.lock:
            return {
                "file_watcher_running": self.observer is not None,
                "playwright_running": self.playwright_process is not None and self.playwright_process.poll() is None,
                "project_root": str(self.project_root),
                "test_results": {
                    "unit": {
                        "status": self.test_results["unit"].get("status", "idle"),
                        "passed": self.test_results["unit"].get("passed", 0),
                        "failed": self.test_results["unit"].get("failed", 0),
                    },
                    "integration": {
                        "status": self.test_results["integration"].get("status", "idle"),
                        "passed": self.test_results["integration"].get("passed", 0),
                        "failed": self.test_results["integration"].get("failed", 0),
                    },
                    "e2e": {
                        "status": self.test_results["e2e"].get("status", "idle"),
                        "passed": self.test_results["e2e"].get("passed", 0),
                        "failed": self.test_results["e2e"].get("failed", 0),
                        "flaky": self.test_results["e2e"].get("flaky", 0),
                    }
                }
            }

