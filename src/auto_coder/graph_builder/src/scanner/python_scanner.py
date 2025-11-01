"""
Python code scanner using ast module
"""

import ast
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class CodeNode:
    """Represents a code node in the graph"""

    id: str
    kind: str
    fqname: str
    sig: str
    short: str
    complexity: int
    tokens_est: int
    tags: List[str] = field(default_factory=list)
    unresolved: bool = False
    file: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None


@dataclass
class CodeEdge:
    """Represents an edge between code nodes"""

    from_id: str
    to_id: str
    type: str
    count: int = 1
    locations: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class GraphData:
    """Container for graph nodes and edges"""

    nodes: List[CodeNode] = field(default_factory=list)
    edges: List[CodeEdge] = field(default_factory=list)


def generate_id(fqname: str, sig: str) -> str:
    """Generate unique ID from fqname and signature"""
    hash_obj = hashlib.sha1((fqname + sig).encode())
    return hash_obj.hexdigest()[:16]


def generate_file_id(path: str) -> str:
    """Generate unique ID for a file"""
    hash_obj = hashlib.sha1(path.encode())
    return hash_obj.hexdigest()[:16]


def estimate_tokens(text: str) -> int:
    """Estimate token count (simple heuristic: chars / 4)"""
    if not text:
        return 0
    return (len(text) + 3) // 4


def calculate_complexity(node: ast.AST) -> int:
    """Calculate cyclomatic complexity"""
    complexity = 1

    for child in ast.walk(node):
        if isinstance(
            child, (ast.If, ast.For, ast.While, ast.ExceptHandler, ast.With, ast.BoolOp)
        ):
            complexity += 1

    return complexity


def synthesize_short_summary(
    docstring: Optional[str], name: str, params: List[str]
) -> str:
    """Generate short summary from docstring or function name"""
    # Priority 1: Use first line of docstring
    if docstring:
        first_line = docstring.strip().split("\n")[0].strip()
        if first_line:
            return truncate_to_token_limit(first_line, 80)

    # Priority 2: Generate from function name
    summary = generate_summary_from_name(name, params)
    return truncate_to_token_limit(summary, 80)


def generate_summary_from_name(name: str, params: List[str]) -> str:
    """Generate summary from function name"""
    # Convert snake_case to words
    words = name.replace("_", " ")

    # Common verb patterns
    verb_patterns = [
        ("get", "gets {object}"),
        ("set", "sets {object}"),
        ("create", "creates {object}"),
        ("delete", "deletes {object}"),
        ("update", "updates {object}"),
        ("fetch", "fetches {object}"),
        ("find", "finds {object}"),
        ("search", "searches {object}"),
        ("validate", "validates {object}"),
        ("process", "processes {object}"),
        ("handle", "handles {object}"),
        ("calculate", "calculates {object}"),
        ("compute", "computes {object}"),
        ("is", "checks if {object}"),
        ("has", "checks if has {object}"),
    ]

    for prefix, template in verb_patterns:
        if words.startswith(prefix):
            obj = words[len(prefix) :].strip() or "value"
            return template.replace("{object}", obj)

    return words or "performs operation"


def truncate_to_token_limit(text: str, max_tokens: int) -> str:
    """Truncate text to token limit"""
    estimated_tokens = estimate_tokens(text)
    if estimated_tokens <= max_tokens:
        return text

    max_chars = max_tokens * 4
    return text[: max_chars - 3] + "..."


def detect_tags(code: str, sig: str) -> List[str]:
    """Detect side-effect tags from code patterns"""
    tags = []

    # IO operations
    if any(keyword in code for keyword in ["open(", "read(", "write(", "file"]):
        tags.append("IO")

    # Database operations
    if any(
        keyword in code
        for keyword in [
            "query",
            "execute",
            "select",
            "insert",
            "update",
            "delete",
            "db.",
            "database",
        ]
    ):
        tags.append("DB")

    # Network operations
    if any(
        keyword in code
        for keyword in ["requests.", "urllib", "http", "socket", "fetch", "ajax"]
    ):
        tags.append("NETWORK")

    # Async operations
    if "async " in code or "await " in code:
        tags.append("ASYNC")

    # Pure function heuristic
    if not tags:
        tags.append("PURE")

    return tags


def generate_signature(node: ast.FunctionDef) -> str:
    """Generate function signature"""
    params = []
    for arg in node.args.args:
        if arg.annotation:
            param_type = ast.unparse(arg.annotation)
        else:
            param_type = "Any"
        params.append(param_type)

    if node.returns:
        return_type = ast.unparse(node.returns)
    else:
        return_type = "Any"

    return f"({','.join(params)})->{return_type}"


class PythonScanner(ast.NodeVisitor):
    """AST visitor for scanning Python code"""

    def __init__(self, file_path: str, module_name: str):
        self.file_path = file_path
        self.module_name = module_name
        self.nodes: List[CodeNode] = []
        self.edges: List[CodeEdge] = []
        self.current_class: Optional[str] = None
        self.current_function: Optional[str] = None
        self.edge_map: Dict[str, CodeEdge] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Visit function definition"""
        if self.current_class:
            fqname = f"{self.module_name}:{self.current_class}.{node.name}"
            kind = "Method"
        else:
            fqname = f"{self.module_name}:{node.name}"
            kind = "Function"

        sig = generate_signature(node)
        docstring = ast.get_docstring(node)
        params = [arg.arg for arg in node.args.args]
        short = synthesize_short_summary(docstring, node.name, params)
        complexity = calculate_complexity(node)
        code = ast.unparse(node)
        tags = detect_tags(code, sig)

        node_id = generate_id(fqname, sig)
        tokens_est = estimate_tokens(short) + estimate_tokens(sig)

        code_node = CodeNode(
            id=node_id,
            kind=kind,
            fqname=fqname,
            sig=sig,
            short=short,
            complexity=complexity,
            tokens_est=tokens_est,
            tags=tags,
            file=self.file_path,
            start_line=node.lineno,
            end_line=node.end_lineno,
        )
        self.nodes.append(code_node)

        # Save current function for call tracking
        prev_function = self.current_function
        self.current_function = fqname
        self.generic_visit(node)
        self.current_function = prev_function

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Visit async function definition"""
        self.visit_FunctionDef(node)  # type: ignore

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Visit class definition"""
        fqname = f"{self.module_name}:{node.name}"
        sig = f"class {node.name}"
        docstring = ast.get_docstring(node)
        short = docstring.split("\n")[0] if docstring else f"Class {node.name}"

        node_id = generate_id(fqname, sig)
        tokens_est = estimate_tokens(short) + estimate_tokens(sig)

        code_node = CodeNode(
            id=node_id,
            kind="Class",
            fqname=fqname,
            sig=sig,
            short=short,
            complexity=0,
            tokens_est=tokens_est,
            file=self.file_path,
            start_line=node.lineno,
            end_line=node.end_lineno,
        )
        self.nodes.append(code_node)

        # Visit methods
        prev_class = self.current_class
        self.current_class = node.name
        self.generic_visit(node)
        self.current_class = prev_class

    def visit_Call(self, node: ast.Call) -> None:
        """Visit function call"""
        if self.current_function:
            # Try to resolve the called function
            callee_name = None
            if isinstance(node.func, ast.Name):
                callee_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                callee_name = node.func.attr

            if callee_name:
                # Create edge (may be unresolved)
                callee_fqname = f"{self.module_name}:{callee_name}"
                edge_key = f"{self.current_function}-{callee_fqname}-CALLS"

                if edge_key in self.edge_map:
                    self.edge_map[edge_key].count += 1
                    self.edge_map[edge_key].locations.append(
                        {
                            "file": self.file_path,
                            "line": node.lineno,
                        }
                    )
                else:
                    self.edge_map[edge_key] = CodeEdge(
                        from_id=self.current_function,
                        to_id=callee_fqname,
                        type="CALLS",
                        count=1,
                        locations=[
                            {
                                "file": self.file_path,
                                "line": node.lineno,
                            }
                        ],
                    )

        self.generic_visit(node)


def scan_python_file(file_path: str, module_name: str) -> GraphData:
    """Scan a single Python file"""
    with open(file_path, "r", encoding="utf-8") as f:
        source = f.read()

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as e:
        print(f"Syntax error in {file_path}: {e}")
        return GraphData()

    scanner = PythonScanner(file_path, module_name)
    scanner.visit(tree)

    # Add file node
    file_id = generate_file_id(file_path)
    file_node = CodeNode(
        id=file_id,
        kind="File",
        fqname=file_path,
        sig="",
        short=f"File: {file_path}",
        complexity=0,
        tokens_est=estimate_tokens(file_path),
        file=file_path,
    )

    # Add CONTAINS edges from file to top-level definitions
    for node in scanner.nodes:
        if node.kind in ("Function", "Class"):
            scanner.edge_map[f"{file_id}-{node.id}-CONTAINS"] = CodeEdge(
                from_id=file_id,
                to_id=node.id,
                type="CONTAINS",
                count=1,
            )

    return GraphData(
        nodes=[file_node] + scanner.nodes,
        edges=list(scanner.edge_map.values()),
    )


def find_python_project_roots(project_path: str) -> List[str]:
    """
    Find Python project roots in a directory (monorepo support).

    A directory is considered a Python project root if it contains:
    - setup.py
    - pyproject.toml
    - requirements.txt
    - setup.cfg

    Returns list of project root paths.
    """
    project_root = Path(project_path)
    project_roots = []

    # Markers that indicate a Python project root
    python_markers = ["setup.py", "pyproject.toml", "requirements.txt", "setup.cfg"]

    # Check if the root itself is a Python project
    if any((project_root / marker).exists() for marker in python_markers):
        project_roots.append(str(project_root))
        return project_roots  # If root is a Python project, use only that

    # Otherwise, search for Python projects in subdirectories (monorepo support)
    # Exclude common directories
    exclude_dirs = {
        "node_modules",
        "dist",
        "build",
        ".git",
        ".svelte-kit",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "venv",
        "env",
        ".venv",
        ".env",
        "site-packages",
    }

    for root, dirs, files in os.walk(project_root):
        # Remove excluded directories from search
        dirs[:] = [d for d in dirs if d not in exclude_dirs]

        # Check if this directory contains any Python project markers
        if any(marker in files for marker in python_markers):
            project_roots.append(root)
            # Don't search subdirectories of a found project
            dirs.clear()

    return project_roots


def scan_python_project(project_path: str, limit: Optional[int] = None) -> GraphData:
    """Scan entire Python project (with monorepo support)"""
    all_nodes = []
    all_edges = []

    # Find all Python project roots
    project_roots = find_python_project_roots(project_path)

    if not project_roots:
        # No specific project markers found, scan entire directory
        project_roots = [project_path]

    print(f"Found {len(project_roots)} Python project(s)")

    for project_root_path in project_roots:
        project_root = Path(project_root_path)
        print(f"Scanning Python project: {project_root}")

        # Exclude common directories
        exclude_patterns = {
            "node_modules",
            "dist",
            "build",
            ".git",
            ".svelte-kit",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            "venv",
            "env",
            ".venv",
            ".env",
            "site-packages",
        }

        python_files = []
        for py_file in project_root.rglob("*.py"):
            # Check if file is in an excluded directory
            if any(excluded in py_file.parts for excluded in exclude_patterns):
                continue
            python_files.append(py_file)

        if limit:
            python_files = python_files[:limit]

        for py_file in python_files:
            # Generate module name from file path
            try:
                rel_path = py_file.relative_to(project_root)
                module_name = str(rel_path.with_suffix("")).replace(os.sep, ".")

                graph_data = scan_python_file(str(py_file), module_name)
                all_nodes.extend(graph_data.nodes)
                all_edges.extend(graph_data.edges)
            except Exception as e:
                print(f"Error scanning {py_file}: {e}")

        print(
            f"  Found {len([n for n in all_nodes if n.file and str(project_root) in n.file])} nodes from this project"
        )

    return GraphData(nodes=all_nodes, edges=all_edges)
