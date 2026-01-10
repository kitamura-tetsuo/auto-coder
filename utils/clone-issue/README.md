# clone-issue

A utility to clone GitHub issues and their sub-issues recursively.

## Installation

```bash
cd utils/clone-issue
pipx install -e .
```

## Usage

```bash
# Clone a single issue by number
clone-issue 123

# Clone a single issue by URL
clone-issue https://github.com/owner/repo/issues/123

# Clone multiple issues
clone-issue 123 456

# Dry run (print what would happen without creating issues)
clone-issue 123 --dry-run
```

## How it works

1.  Fetches the details (title, body) of the specified issue(s).
2.  Creates a new issue with the same title and body.
3.  Checks if the original issue has sub-issues.
4.  Recursively clones any sub-issues.
5.  Links the new sub-issues to the new parent issue using `github-sub-issue`.
