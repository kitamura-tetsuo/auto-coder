# Complete source-publication test prerequisites

`Publish Beta` and `Test Source for Runtime` prepare the required management test
image before running their existing complete test selection. The preparation
script builds only Dockerfile's locked `python-environment` stage, never the
OpenCode/Codex production runtime stage. It requires a clean complete checkout,
passes its authenticated build identity to the existing build backend, resolves
a strict immutable Docker image ID, and verifies the installed source/version
inside that exact read-only, network-disabled image before exporting
`AUTOCODER_FRESH_TEST_BASE_IMAGE`. A failed or mismatched prerequisite exports
nothing and fails the gate. No Docker credential or registry publication is added.

The readonly-bind/shared-owner-lock container regression then uses that immutable
fixture. Its inode, readonly mount and exclusivity assertions remain unchanged.
The real preparer crash regression holds its actual controller after the real
`uv venv` process returns and before installation/READY. An atomic barrier proves
the partial environment exists before SIGKILL, and the test proves the exact kill
outcome, absence of partial READY, retained attempt and disjoint reconstruction.
It no longer races an already completed fast preparer. Existing deadlines and
publication selections are unchanged; child cleanup on failure owns only this
test's process group.

Verification: `pytest -q tests/test_management_fixture_preparation.py tests/test_worker_versions_integration.py::test_actual_preparer_crash_retains_partial_attempt_and_reconstruction_uses_disjoint_path`;
a clean checkout can run `python scripts/prepare_management_test_image.py`, then
`AUTOCODER_FRESH_TEST_BASE_IMAGE=<verified immutable ID> pytest -q tests/test_management_retained_startup_container.py` for the real container oracle.

Observability is neutral to the application: this changes only CI prerequisites
and a deterministic fault-injection boundary. Existing Actions exits and test
reports remain authoritative; no runtime event schema, dashboard projection,
tested-source annotation, source selector or adoption assertion changes.
