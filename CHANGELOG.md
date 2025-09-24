# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]
- CLI: Added new PR processing option `--skip-main-update/--no-skip-main-update` to control whether to merge the PR's base branch into a PR branch before attempting fixes when PR checks are failing.
- Behavior change: The default is now to skip merging the base branch before fixes (equivalent to `--skip-main-update`). Use `--no-skip-main-update` to perform the merge step first (previous behavior).
- Logging: Improved clarity in `process-issues` command output by explicitly displaying the current policy for base branch update when PR checks fail.
- Auggie backend: Added a persisted daily quota (20 calls per day) that raises `AutoCoderUsageLimitError` before invoking the CLI and rotates to fallback backends.

## [0.1.0] - Initial release
- Initial public release with CLI commands: `process-issues`, `create-feature-issues`, and `auth-status`.

