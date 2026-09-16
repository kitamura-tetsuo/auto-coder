# Maintained operational scripts

The repository keeps maintained standalone Python utilities under `scripts/`.
The TCP proxy retains its three-argument command-line interface there, and the
comprehensive `make type-check` entry point runs its checker from that location.
Jules session tests protect the automatic pull-request mode and preserve the
caller-provided GitHub repository and starting branch at the HTTP boundary.
