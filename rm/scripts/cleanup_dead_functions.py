#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bulk-remove unused Python functions reported by vulture, safely via LibCST.

Workflow:
  1) Run vulture to detect "unused function/method".
  2) Parse vulture output and map (file -> {function name, definition line}).
  3) For each file, parse CST and remove matching FunctionDef nodes.
  4) Dry-run by default; apply with --apply.

Caveats:
  - Dynamic usage (framework routes, CLI commands, reflection) requires whitelisting.
  - Always run your test suite after applying changes.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import libcst as cst
from libcst import metadata
from libcst.metadata import PositionProvider
from loguru import logger

VULTURE_LINE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+):\s+unused\s+(function|method)\s+'(?P<name>[^']+)'",
    re.IGNORECASE,
)


def run_vulture(paths: List[str], min_conf: int, exclude: List[str], whitelist: List[str]) -> str:
    """Run vulture and return stdout text."""
    cmd = [sys.executable, "-m", "vulture"]
    cmd += paths
    cmd += whitelist  # vulture accepts whitelist .py as additional "code" files
    if exclude:
        cmd += ["--exclude", ",".join(exclude)]
    cmd += ["--min-confidence", str(min_conf)]
    logger.debug("Running: {}", " ".join(cmd))
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode not in (0, 1, 3):
        # vulture exits 1 when issues found, 3 when issues found with some internal warnings; treat 0/1/3 as normal
        logger.error(
            "vulture failed (code={}):\nSTDOUT:\n{}\nSTDERR:\n{}",
            proc.returncode,
            proc.stdout,
            proc.stderr,
        )
        sys.exit(proc.returncode)
    return proc.stdout


def parse_vulture_output(text: str) -> Dict[Path, List[Tuple[str, int]]]:
    """Extract (file -> [(func_name, line), ...]) from vulture 'unused function/method' lines."""
    hits: Dict[Path, List[Tuple[str, int]]] = {}
    for raw in text.splitlines():
        m = VULTURE_LINE.match(raw.strip())
        if not m:
            continue
        p = Path(m.group("path")).resolve()
        line = int(m.group("line"))
        name = m.group("name")
        hits.setdefault(p, []).append((name, line))
    return hits


class PerFileRemover(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, targets: List[Tuple[str, int]], keep_patterns: List[re.Pattern]):
        # Store (name, line) targets and patterns to keep
        self.targets = targets
        self.keep_patterns = keep_patterns
        self.removed: List[Tuple[str, int]] = []

    def _should_remove(self, node: cst.FunctionDef) -> bool:
        name = node.name.value
        # Never remove dunder methods unless explicitly requested by vulture (rare).
        # You may loosen this rule if you want.
        if name.startswith("__") and name.endswith("__"):
            return False

        # Keep if matches any keep pattern
        for pat in self.keep_patterns:
            if pat.search(name):
                return False

        pos = self.get_metadata(PositionProvider, node)
        start_line = pos.start.line

        for targ_name, targ_line in self.targets:
            if name != targ_name:
                continue
            # Allow small offset because vulture's line may point at decorator or def line
            if abs(start_line - targ_line) <= 2:
                return True
        return False

    def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.CSTNode:
        if self._should_remove(original_node):
            pos = self.get_metadata(PositionProvider, original_node).start.line
            self.removed.append((original_node.name.value, pos))
            return cst.RemoveFromParent()
        return updated_node


def process_file(path: Path, targets: List[Tuple[str, int]], apply: bool, keep: List[re.Pattern]) -> List[Tuple[str, int]]:
    """Remove targeted functions from a single file. Return removed (name, line)."""
    try:
        code = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error("Failed to read {}: {}", path, e)
        return []

    try:
        module = cst.parse_module(code)
        wrapper = metadata.MetadataWrapper(module)
        remover = PerFileRemover(targets, keep_patterns=keep)
        new_module = wrapper.visit(remover)
    except Exception as e:
        logger.exception("LibCST parse/transform failed for {}: {}", path, e)
        return []

    removed = remover.removed
    if not removed:
        return []

    if apply:
        try:
            path.write_text(new_module.code, encoding="utf-8")
            logger.info("Updated {} (removed {} functions).", path, len(removed))
        except Exception as e:
            logger.error("Failed to write {}: {}", path, e)
    else:
        logger.info("Preview: Would update {} (remove {} functions).", path, len(removed))

    for name, ln in removed:
        logger.debug("  - removed {} at line {}", name, ln)
    return removed


def compile_keep_patterns(exprs: List[str]) -> List[re.Pattern]:
    return [re.compile(x) for x in exprs]


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk-remove unused functions reported by vulture, via LibCST.")
    parser.add_argument("paths", nargs="+", help="Code paths to scan (e.g. src/ package/)")
    parser.add_argument("--apply", action="store_true", help="Actually modify files. Default: preview mode.")
    parser.add_argument(
        "--min-confidence",
        type=int,
        default=80,
        help="vulture min confidence (0-100). Default: 80",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Comma-separated glob(s) to exclude (can repeat).",
    )
    parser.add_argument(
        "--whitelist",
        action="append",
        default=[],
        help="Additional vulture whitelist .py files (can repeat).",
    )
    parser.add_argument(
        "--keep-pattern",
        action="append",
        default=[],
        help="Regex of function names to keep (can repeat).",
    )
    parser.add_argument(
        "--log-file",
        default="deadcode.log",
        help="Log file path. Default: deadcode.log",
    )
    args = parser.parse_args()

    # Set up loguru: stdout + file
    logger.remove()
    logger.add(sys.stdout, level="INFO", enqueue=True)
    logger.add(args.log_file, level="DEBUG", rotation="2 MB", retention=5, enqueue=True)

    # Normalize exclude/whitelist
    exclude: List[str] = []
    for e in args.exclude:
        exclude += [x.strip() for x in e.split(",") if x.strip()]

    whitelist: List[str] = []
    for w in args.whitelist:
        whitelist += [x.strip() for x in w.split(",") if x.strip()]
    whitelist = [str(Path(w)) for w in whitelist]

    keep_patterns = compile_keep_patterns(args.keep_pattern)

    # 1) Run vulture
    stdout = run_vulture(
        paths=args.paths,
        min_conf=args.min_confidence,
        exclude=exclude,
        whitelist=whitelist,
    )
    hits = parse_vulture_output(stdout)

    if not hits:
        logger.info(
            "No unused functions/methods found at >= {}% confidence.",
            args.min_confidence,
        )
        sys.exit(0)

    total_removed = 0
    for fpath, targets in hits.items():
        removed = process_file(fpath, targets, apply=args.apply, keep=keep_patterns)
        total_removed += len(removed)

    if args.apply:
        logger.info("Done. Removed {} functions across {} files.", total_removed, len(hits))
    else:
        logger.info(
            "Preview: Would remove {} functions across {} files. Use --apply to write changes.",
            total_removed,
            len(hits),
        )


if __name__ == "__main__":
    main()
