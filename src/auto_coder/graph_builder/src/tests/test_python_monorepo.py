"""
Tests for Python monorepo support
"""

import json
import os
import shutil

# Add parent directory to path
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scanner.python_scanner import find_python_project_roots, scan_python_project


class TestPythonMonorepo(unittest.TestCase):
    """Test Python monorepo support"""

    def setUp(self):
        """Create test monorepo structure"""
        self.test_dir = tempfile.mkdtemp()

        # Create package1 with setup.py
        package1_dir = Path(self.test_dir) / "packages" / "package1"
        package1_dir.mkdir(parents=True)

        (package1_dir / "setup.py").write_text(
            """
from setuptools import setup, find_packages

setup(
    name='package1',
    version='0.1.0',
    packages=find_packages(),
)
"""
        )

        (package1_dir / "module1.py").write_text(
            """
def hello():
    '''Say hello'''
    return 'Hello from package1'

class Service1:
    '''Service class'''
    
    def greet(self, name):
        '''Greet someone'''
        return f'Hello, {name}!'
"""
        )

        # Create package2 with pyproject.toml
        package2_dir = Path(self.test_dir) / "packages" / "package2"
        package2_dir.mkdir(parents=True)

        (package2_dir / "pyproject.toml").write_text(
            """
[project]
name = "package2"
version = "0.1.0"
"""
        )

        (package2_dir / "module2.py").write_text(
            """
def world():
    '''Say world'''
    return 'World from package2'

class Service2:
    '''Service class'''
    
    def farewell(self, name):
        '''Say goodbye'''
        return f'Goodbye, {name}!'
"""
        )

        # Create package3 with requirements.txt
        package3_dir = Path(self.test_dir) / "apps" / "app1"
        package3_dir.mkdir(parents=True)

        (package3_dir / "requirements.txt").write_text(
            """
requests>=2.28.0
pytest>=7.0.0
"""
        )

        (package3_dir / "main.py").write_text(
            """
def main():
    '''Main function'''
    print('Running app1')

if __name__ == '__main__':
    main()
"""
        )

    def tearDown(self):
        """Clean up test directory"""
        shutil.rmtree(self.test_dir)

    def test_find_python_project_roots(self):
        """Test finding Python project roots in monorepo"""
        roots = find_python_project_roots(self.test_dir)

        # Should find 3 projects
        self.assertEqual(len(roots), 3)

        # Check that all expected projects are found
        root_names = [Path(r).name for r in roots]
        self.assertIn("package1", root_names)
        self.assertIn("package2", root_names)
        self.assertIn("app1", root_names)

    def test_find_python_project_roots_single_project(self):
        """Test finding Python project root when root is a project"""
        # Create a single project directory
        single_project_dir = tempfile.mkdtemp()
        try:
            (Path(single_project_dir) / "setup.py").write_text("# setup")
            (Path(single_project_dir) / "module.py").write_text("def foo(): pass")

            roots = find_python_project_roots(single_project_dir)

            # Should find only the root
            self.assertEqual(len(roots), 1)
            self.assertEqual(roots[0], single_project_dir)
        finally:
            shutil.rmtree(single_project_dir)

    def test_scan_python_monorepo(self):
        """Test scanning Python monorepo"""
        graph_data = scan_python_project(self.test_dir)

        # Should have nodes from all packages
        self.assertGreater(len(graph_data.nodes), 0)

        # Check for nodes from package1
        package1_nodes = [
            n for n in graph_data.nodes if n.file and "package1" in n.file
        ]
        self.assertGreater(len(package1_nodes), 0)

        # Check for nodes from package2
        package2_nodes = [
            n for n in graph_data.nodes if n.file and "package2" in n.file
        ]
        self.assertGreater(len(package2_nodes), 0)

        # Check for nodes from app1
        app1_nodes = [n for n in graph_data.nodes if n.file and "app1" in n.file]
        self.assertGreater(len(app1_nodes), 0)

    def test_scan_python_monorepo_functions(self):
        """Test extracting functions from monorepo"""
        graph_data = scan_python_project(self.test_dir)

        functions = [n for n in graph_data.nodes if n.kind == "Function"]
        self.assertGreater(len(functions), 0)

        # Check for specific functions
        hello_func = next((f for f in functions if "hello" in f.fqname), None)
        self.assertIsNotNone(hello_func)

        world_func = next((f for f in functions if "world" in f.fqname), None)
        self.assertIsNotNone(world_func)

        main_func = next((f for f in functions if "main" in f.fqname), None)
        self.assertIsNotNone(main_func)

    def test_scan_python_monorepo_classes(self):
        """Test extracting classes from monorepo"""
        graph_data = scan_python_project(self.test_dir)

        classes = [n for n in graph_data.nodes if n.kind == "Class"]
        self.assertGreater(len(classes), 0)

        # Check for Service classes
        service1_class = next((c for c in classes if "Service1" in c.fqname), None)
        self.assertIsNotNone(service1_class)

        service2_class = next((c for c in classes if "Service2" in c.fqname), None)
        self.assertIsNotNone(service2_class)

    def test_exclude_common_directories(self):
        """Test that common directories are excluded"""
        # Create a venv directory with Python files
        venv_dir = Path(self.test_dir) / "venv" / "lib"
        venv_dir.mkdir(parents=True)
        (venv_dir / "module.py").write_text("def should_be_excluded(): pass")

        # Create a __pycache__ directory
        pycache_dir = Path(self.test_dir) / "packages" / "package1" / "__pycache__"
        pycache_dir.mkdir(parents=True)
        (pycache_dir / "cached.py").write_text("def should_be_excluded(): pass")

        graph_data = scan_python_project(self.test_dir)

        # Should not include nodes from excluded directories
        excluded_nodes = [
            n
            for n in graph_data.nodes
            if n.file and ("venv" in n.file or "__pycache__" in n.file)
        ]
        self.assertEqual(len(excluded_nodes), 0)

    def test_no_python_projects_found(self):
        """Test scanning directory with no Python project markers"""
        # Create a directory with only Python files, no project markers
        no_project_dir = tempfile.mkdtemp()
        try:
            (Path(no_project_dir) / "script.py").write_text("def foo(): pass")

            graph_data = scan_python_project(no_project_dir)

            # Should still scan the directory
            self.assertGreater(len(graph_data.nodes), 0)
        finally:
            shutil.rmtree(no_project_dir)


if __name__ == "__main__":
    unittest.main()
