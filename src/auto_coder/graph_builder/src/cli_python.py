#!/usr/bin/env python3
"""
CLI for graph-builder (Python version)
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from emitter.python_emitter import emit_csv, emit_diff_json, emit_json
from scanner.python_scanner import GraphData, scan_python_project


def scan_command(args):
    """Scan project and extract graph data"""
    project_path = Path(args.project).resolve()
    output_dir = Path(args.out).resolve()

    print(f"Scanning project: {project_path}")
    print(f"Output directory: {output_dir}")
    print(f"Mode: {args.mode}")

    # Extract repository name from project path for Neo4j labeling
    repo_name = project_path.name
    print(f"Repository: {repo_name}")

    # Parse languages option
    languages = []
    if hasattr(args, "languages") and args.languages:
        languages = [lang.strip() for lang in args.languages.split(",")]
    else:
        languages = ["python"]  # Default to Python only

    print(f"Languages: {', '.join(languages)}")

    graph_data = GraphData(nodes=[], edges=[])

    # Scan Python files if requested
    if "python" in languages:
        print("Scanning Python files...")
        python_data = scan_python_project(str(project_path), args.limit)
        graph_data.nodes.extend(python_data.nodes)
        graph_data.edges.extend(python_data.edges)
        print(f"Found {len(python_data.nodes)} Python nodes")

    # Scan TypeScript/JavaScript files if requested
    if "typescript" in languages or "javascript" in languages:
        # Check if TypeScript CLI is available
        ts_cli = Path(__file__).parent.parent / "dist" / "cli.js"
        if ts_cli.exists():
            print("Scanning TypeScript/JavaScript files using TypeScript CLI...")
            ts_languages = ",".join(
                [lang for lang in languages if lang in ["typescript", "javascript"]]
            )
            try:
                result = subprocess.run(
                    [
                        "node",
                        str(ts_cli),
                        "scan",
                        "--project",
                        str(project_path),
                        "--out",
                        str(output_dir),
                        "--languages",
                        ts_languages,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )

                if result.returncode == 0:
                    # Read the generated graph-data.json
                    ts_data_path = output_dir / "graph-data.json"
                    if ts_data_path.exists():
                        with open(ts_data_path, "r", encoding="utf-8") as f:
                            ts_data = json.load(f)
                            # Merge TypeScript/JavaScript data
                            for node in ts_data.get("nodes", []):
                                from scanner.python_scanner import CodeNode

                                graph_data.nodes.append(CodeNode(**node))
                            for edge in ts_data.get("edges", []):
                                from scanner.python_scanner import CodeEdge

                                graph_data.edges.append(
                                    CodeEdge(
                                        from_id=edge["from"],
                                        to_id=edge["to"],
                                        type=edge["type"],
                                        count=edge.get("count", 1),
                                        locations=edge.get("locations", []),
                                    )
                                )
                            print(
                                f"Found {len(ts_data.get('nodes', []))} TypeScript/JavaScript nodes"
                            )
                else:
                    print(
                        f"Warning: TypeScript CLI failed with return code {result.returncode}"
                    )
                    print(f"stderr: {result.stderr}")
            except Exception as e:
                print(f"Warning: Failed to run TypeScript CLI: {e}")
        else:
            print(
                f"Warning: TypeScript CLI not found at {ts_cli}, skipping TypeScript/JavaScript scan"
            )

    print(f"Total: {len(graph_data.nodes)} nodes, {len(graph_data.edges)} edges")

    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save intermediate data
    data_path = output_dir / "graph-data.json"
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "repository": repo_name,  # Store repository name for Neo4j labeling
                "nodes": [
                    {
                        "id": n.id,
                        "kind": n.kind,
                        "fqname": n.fqname,
                        "sig": n.sig,
                        "short": n.short,
                        "complexity": n.complexity,
                        "tokens_est": n.tokens_est,
                        "tags": n.tags,
                        "file": n.file,
                        "start_line": n.start_line,
                        "end_line": n.end_line,
                    }
                    for n in graph_data.nodes
                ],
                "edges": [
                    {
                        "from": e.from_id,
                        "to": e.to_id,
                        "type": e.type,
                        "count": e.count,
                        "locations": e.locations,
                    }
                    for e in graph_data.edges
                ],
            },
            f,
            indent=2,
        )

    print(f"Saved graph data to {data_path}")
    print(f"Total nodes: {len(graph_data.nodes)}")
    print(f"Total edges: {len(graph_data.edges)}")


def emit_csv_command(args):
    """Emit CSV files for Neo4j import"""
    output_dir = Path(args.out).resolve()
    data_path = output_dir / "graph-data.json"

    if not data_path.exists():
        print(f"Graph data not found at {data_path}. Run 'scan' first.")
        sys.exit(1)

    with open(data_path, "r", encoding="utf-8") as f:
        data_dict = json.load(f)

    # Get repository name from the data for Neo4j labeling
    repo_name = data_dict.get("repository")

    # Convert dict back to GraphData
    from scanner.python_scanner import CodeEdge, CodeNode

    graph_data = GraphData(
        nodes=[CodeNode(**n) for n in data_dict["nodes"]],
        edges=[
            CodeEdge(
                from_id=e["from"],
                to_id=e["to"],
                type=e["type"],
                count=e["count"],
                locations=e.get("locations", []),
            )
            for e in data_dict["edges"]
        ],
    )

    emit_csv(graph_data, str(output_dir), repo_name=repo_name)
    print("CSV files generated successfully")


def emit_json_command(args):
    """Emit JSON batch file"""
    output_dir = Path(args.out).resolve()
    data_path = output_dir / "graph-data.json"

    if not data_path.exists():
        print(f"Graph data not found at {data_path}. Run 'scan' first.")
        sys.exit(1)

    with open(data_path, "r", encoding="utf-8") as f:
        data_dict = json.load(f)

    # Convert dict back to GraphData
    from scanner.python_scanner import CodeEdge, CodeNode

    graph_data = GraphData(
        nodes=[CodeNode(**n) for n in data_dict["nodes"]],
        edges=[
            CodeEdge(
                from_id=e["from"],
                to_id=e["to"],
                type=e["type"],
                count=e["count"],
                locations=e.get("locations", []),
            )
            for e in data_dict["edges"]
        ],
    )

    emit_json(graph_data, str(output_dir))
    print("JSON batch file generated successfully")


def diff_command(args):
    """Generate diff from git changes"""
    output_dir = Path(args.out).resolve()
    since = args.since

    print(f"Generating diff since {since}...")

    try:
        # Get changed files
        result = subprocess.run(
            ["git", "diff", "--name-only", since],
            capture_output=True,
            text=True,
            check=True,
        )
        changed_files = [
            f for f in result.stdout.strip().split("\n") if f.endswith(".py")
        ]

        print(f"Found {len(changed_files)} changed Python files")

        # Get current commit
        commit_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        commit_hash = commit_result.stdout.strip()

        diff_data = {
            "meta": {
                "commit": commit_hash,
                "files": changed_files,
                "timestamp": __import__("datetime").datetime.now().isoformat(),
            },
            "added": {"nodes": [], "edges": []},
            "updated": {"nodes": [], "edges": []},
            "removed": {"nodes": [], "edges": []},
        }

        emit_diff_json(diff_data, str(output_dir), commit_hash[:8])
        print("Diff JSON generated successfully")

    except subprocess.CalledProcessError as e:
        print(f"Error running git command: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Python code analyzer for Neo4j graph database"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Scan command
    scan_parser = subparsers.add_parser(
        "scan", help="Scan project and extract graph data"
    )
    scan_parser.add_argument("--project", default=".", help="Project path")
    scan_parser.add_argument("--out", default="./out", help="Output directory")
    scan_parser.add_argument(
        "--mode", default="full", choices=["full", "diff"], help="Scan mode"
    )
    scan_parser.add_argument("--since", help="Git reference for diff mode")
    scan_parser.add_argument(
        "--limit", type=int, help="Limit number of files to process"
    )
    scan_parser.add_argument(
        "--languages",
        default="python",
        help="Languages to scan (comma-separated): typescript,javascript,python",
    )

    # Emit CSV command
    csv_parser = subparsers.add_parser(
        "emit-csv", help="Emit CSV files for Neo4j import"
    )
    csv_parser.add_argument("--out", default="./out", help="Output directory")

    # Emit JSON command
    json_parser = subparsers.add_parser("emit-json", help="Emit JSON batch file")
    json_parser.add_argument("--out", default="./out", help="Output directory")

    # Diff command
    diff_parser = subparsers.add_parser("diff", help="Generate diff from git changes")
    diff_parser.add_argument(
        "--since", default="HEAD~1", help="Git reference to compare against"
    )
    diff_parser.add_argument("--out", default="./out", help="Output directory")

    args = parser.parse_args()

    if args.command == "scan":
        scan_command(args)
    elif args.command == "emit-csv":
        emit_csv_command(args)
    elif args.command == "emit-json":
        emit_json_command(args)
    elif args.command == "diff":
        diff_command(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
