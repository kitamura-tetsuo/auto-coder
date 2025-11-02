"""
Custom setup.py to build TypeScript graph-builder before packaging.

This ensures that the TypeScript version of graph-builder is compiled
before the Python package is built, so that the dist/ directory is
included in the package.
"""

import os
import subprocess
import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.develop import develop
from setuptools.command.sdist import sdist


class BuildGraphBuilder:
    """Mixin class to build TypeScript graph-builder."""

    def build_graph_builder(self) -> None:
        """Build TypeScript graph-builder using npm."""
        graph_builder_dir = Path(__file__).parent / "src" / "auto_coder" / "graph_builder"

        if not graph_builder_dir.exists():
            print(
                f"Warning: graph_builder directory not found at {graph_builder_dir}",
                file=sys.stderr,
            )
            return

        package_json = graph_builder_dir / "package.json"
        if not package_json.exists():
            print(
                f"Warning: package.json not found at {package_json}",
                file=sys.stderr,
            )
            return

        # Check if we're in CI environment - skip build if CI is detected
        if os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true":
            print(
                "CI environment detected. Skipping TypeScript build. "
                "Assuming pre-built dist/ directory is available.",
                file=sys.stderr,
            )
            return

        print("=" * 70)
        print("Building TypeScript graph-builder...")
        print("=" * 70)

        # Check if dist directory exists and has recent builds
        dist_dir = graph_builder_dir / "dist"
        cli_file = dist_dir / "cli.js"
        bundle_file = dist_dir / "cli.bundle.js"

        if dist_dir.exists() and cli_file.exists() and bundle_file.exists():
            print("TypeScript build artifacts already exist. Skipping build.")
            return

        # Check if npm is available
        try:
            subprocess.run(
                ["npm", "--version"],
                check=True,
                capture_output=True,
                cwd=graph_builder_dir,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            print(
                "Warning: npm not found. Skipping TypeScript build.",
                file=sys.stderr,
            )
            print(
                "The TypeScript version of graph-builder will not be available.",
                file=sys.stderr,
            )
            return

        # Install dependencies if node_modules doesn't exist
        node_modules = graph_builder_dir / "node_modules"
        if not node_modules.exists():
            print("Installing npm dependencies...")
            try:
                subprocess.run(
                    ["npm", "install"],
                    check=True,
                    cwd=graph_builder_dir,
                )
            except subprocess.CalledProcessError as e:
                print(
                    f"Warning: npm install failed: {e}",
                    file=sys.stderr,
                )
                return

        # Build TypeScript
        print("Compiling TypeScript...")
        try:
            subprocess.run(
                ["npm", "run", "build"],
                check=True,
                cwd=graph_builder_dir,
            )
            print("TypeScript build completed successfully!")
        except subprocess.CalledProcessError as e:
            print(
                f"Warning: TypeScript build failed: {e}",
                file=sys.stderr,
            )
            print(
                "The TypeScript version of graph-builder will not be available.",
                file=sys.stderr,
            )


class CustomBuildPy(build_py, BuildGraphBuilder):
    """Custom build_py command that builds TypeScript before building Python package."""

    def run(self) -> None:
        """Run the build."""
        self.build_graph_builder()
        super().run()


class CustomDevelop(develop, BuildGraphBuilder):
    """Custom develop command that builds TypeScript in development mode."""

    def run(self) -> None:
        """Run the develop installation."""
        self.build_graph_builder()
        super().run()


class CustomSdist(sdist, BuildGraphBuilder):
    """Custom sdist command that builds TypeScript before creating source distribution."""

    def run(self) -> None:
        """Run the source distribution creation."""
        self.build_graph_builder()
        super().run()


# Use setup() with cmdclass to override build commands
setup(
    cmdclass={
        "build_py": CustomBuildPy,
        "develop": CustomDevelop,
        "sdist": CustomSdist,
    }
)

