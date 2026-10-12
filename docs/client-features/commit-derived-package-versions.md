# Commit-derived package versions

Auto-Coder's PEP 517 builds derive their distribution version from the selected
Git commit's deterministic build identity. Source distributions carry that
identity so wheels rebuilt without Git retain the same metadata. Installed
runtime code reads distribution metadata and never consults Git.

Ordinary `uv sync`, `uv run`, and `uv pip install -e .` cache on the commit.
After changing tracked files without committing, explicitly refresh editable
metadata with `uv pip install --reinstall -e .`.
