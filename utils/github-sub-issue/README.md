# github-sub-issue

A Python utility for operating GitHub's sub-issues feature.

## Features

- ✅ **Uses correct GraphQL API**: Uses GitHub's official sub-issues API
- 🔗 **Add existing issues as sub-issues**: Link existing issues to a parent issue
- ➕ **Create new sub-issues**: Create new issues and link them to a parent
- 📋 **List sub-issues**: Display sub-issues of a parent issue
- ❌ **Remove sub-issues**: Remove sub-issues from a parent issue
- 🎨 **Multiple output formats**: Supports TTY (colored), plain text, JSON output

## Prerequisites

- Python 3.11 or higher
- GitHub CLI (`gh`) must be installed and authenticated

## Installation

```bash
# From the repository root
cd utils/github-sub-issue
pip install -e .
```

## Usage

### Add existing issue as sub-issue

```bash
# Add single sub-issue using issue numbers
github-sub-issue add 123 456

# Add multiple sub-issues at once (new feature)
github-sub-issue add 123 456 457 458

# Using URL
github-sub-issue add https://github.com/owner/repo/issues/123 456 457

# Specify repository
github-sub-issue add 123 456 457 --repo owner/repo

# Mix of issue numbers and URLs
github-sub-issue add 123 456 https://github.com/owner/repo/issues/457
```

### Create new sub-issue

```bash
# Basic usage
github-sub-issue create --parent 123 --title "Implement user authentication"

# Add description and labels
github-sub-issue create --parent 123 \
  --title "Add login endpoint" \
  --body "Implement POST /api/login endpoint" \
  --label "backend,api" \
  --assignee "@me"

# Using parent issue URL
github-sub-issue create \
  --parent https://github.com/owner/repo/issues/123 \
  --title "Write API tests"
```

### List sub-issues

```bash
# Basic listing
github-sub-issue list 123

# Show all states (open, closed)
github-sub-issue list 123 --state all

# JSON output
github-sub-issue list 123 --json

# Using URL
github-sub-issue list https://github.com/owner/repo/issues/123
```

### Remove sub-issues

```bash
# Remove single sub-issue
github-sub-issue remove 123 456

# Remove multiple sub-issues
github-sub-issue remove 123 456 457 458

# Skip confirmation
github-sub-issue remove 123 456 --force

# Using URL
github-sub-issue remove https://github.com/owner/repo/issues/123 456
```

## Development

### Running tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=gh_sub_issue --cov-report=html
```

### Code formatting

```bash
# Format
black .
isort .

# Lint
flake8
mypy .
```

## License

MIT License

## Technical Details

### Using GraphQL API

This tool uses GitHub's GraphQL API to operate sub-issues. Important points:

1. **GraphQL-Features header required**: All sub-issues related API calls require the `GraphQL-Features: sub_issues` header
2. **Use issue ID**: Must use issue ID (e.g., `I_kwDOOakzpM6yyU6H`) instead of issue number
3. **Use mutations**: Use GraphQL mutations to add/remove sub-issues

### Differences from yahsan2/gh-sub-issue

[yahsan2/gh-sub-issue](https://github.com/yahsan2/gh-sub-issue) is a similar tool implemented in Go, but this tool differs in the following points:

- **Python implementation**: Easy integration with the auto-coder project
- **Independent from main body**: Independent utility without auto-coder dependencies
- **Uses correct GraphQL API**: Uses GitHub's official sub-issues API and creates sub-issues correctly recognized by GraphQL

## References

- [GitHub Sub-issues Public Preview](https://github.com/orgs/community/discussions/148714)
- [GitHub GraphQL API - Sub-issues](https://docs.github.com/en/graphql/reference/mutations#addsubissue)
- [Create GitHub issue hierarchy using the API](https://jessehouwing.net/create-github-issue-hierarchy-using-the-api/)
- [yahsan2/gh-sub-issue](https://github.com/yahsan2/gh-sub-issue)
