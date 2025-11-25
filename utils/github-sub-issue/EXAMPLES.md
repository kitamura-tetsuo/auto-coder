# Usage Examples

## Basic Workflow

### 1. Create parent issue

```bash
# Create parent issue via GitHub CLI
gh issue create --title "Feature: User Authentication System" --body "Implement a complete authentication system"
# Created issue #100
```

### 2. Create sub-issues

```bash
# Design database schema
github-sub-issue create --parent 100 --title "Design database schema" --label "database"

# Implement JWT tokens
github-sub-issue create --parent 100 --title "Implement JWT tokens" --label "backend"

# Create login UI
github-sub-issue create --parent 100 --title "Create login UI" --label "frontend"
```

### 3. Add existing issue as sub-issue

```bash
# Add single sub-issue
github-sub-issue add 100 95

# Add multiple existing issues as sub-issues (batch operation)
github-sub-issue add 100 95 96 97 98

# Mix of issue numbers and URLs
github-sub-issue add 100 95 https://github.com/owner/repo/issues/96
```

### 4. Check progress

```bash
# Display all sub-issues
github-sub-issue list 100 --state all

# Example output:
# 📋 Sub-issues (4 total):
# ─────────────────────────────
# ✅ #101  Design database schema              [closed]
# ✅ #95   Security audit checklist            [closed]
# 🔵 #102  Implement JWT tokens                [open]   @alice
# 🔵 #103  Create login UI                     [open]   @bob
```

### 5. Remove unnecessary sub-issues

```bash
# Remove sub-issue #95
github-sub-issue remove 100 95

# Remove multiple sub-issues
github-sub-issue remove 100 95 96 97 --force
```

## Advanced Usage Examples

### Batch Operations: Adding Multiple Sub-issues

```bash
# Add multiple sub-issues in one command
github-sub-issue add 100 101 102 103 104 105

# Add multiple sub-issues with mixed formats
github-sub-issue add 100 \
  101 \
  102 \
  https://github.com/owner/repo/issues/103 \
  https://github.com/owner/repo/issues/104

# Add sub-issues from different repositories
github-sub-issue add https://github.com/owner/repo1/issues/100 \
  https://github.com/owner/repo1/issues/200 \
  https://github.com/owner/repo2/issues/300

# Example output:
# ✅ Added sub-issue #101 to parent #100
#    Parent: Feature: User Authentication System
#    Sub-issue: Design database schema
# ✅ Added sub-issue #102 to parent #100
#    Parent: Feature: User Authentication System
#    Sub-issue: Implement JWT tokens
# ✅ Added sub-issue #103 to parent #100
#    Parent: Feature: User Authentication System
#    Sub-issue: Create login UI
#
# ✅ Successfully added 3 sub-issue(s).
```

### Efficient Project Management with Batch Operations

```bash
# Create a parent issue for a sprint
SPRINT_ISSUE=$(gh issue create --title "Sprint 15: Feature X" \
  --body "Implement Feature X across the stack" | grep -oP '\d+$')

# Add existing issues as sub-issues in batch
github-sub-issue add "$SPRINT_ISSUE" 450 451 452 453 454

# Or create new sub-issues and add existing ones together
github-sub-issue create --parent "$SPRINT_ISSUE" --title "API Design" --label "backend"
github-sub-issue create --parent "$SPRINT_ISSUE" --title "Database Schema" --label "backend"
github-sub-issue add "$SPRINT_ISSUE" 440 441  # Add existing issues
```

### Cross-repository sub-issues

```bash
# Add issue from another repository as sub-issue
github-sub-issue add https://github.com/owner/repo1/issues/100 \
  https://github.com/owner/repo2/issues/200
```

### Automation using JSON output

```bash
# Get sub-issues in JSON format
github-sub-issue list 100 --json | jq '.[] | select(.state == "OPEN") | .number'

# Example output:
# 102
# 103
```

### Usage in scripts

```bash
#!/bin/bash

# Create parent issue
PARENT=$(gh issue create --title "Sprint 1" --body "Sprint 1 tasks" | grep -oP '\d+$')

# Create sub-issues from task list
while IFS= read -r task; do
  github-sub-issue create --parent "$PARENT" --title "$task" --label "sprint-1"
done < tasks.txt

# Display progress
github-sub-issue list "$PARENT"
```

### Usage in CI/CD

```yaml
# .github/workflows/create-sub-issues.yml
name: Create Sub-issues

on:
  issues:
    types: [labeled]

jobs:
  create-sub-issues:
    if: github.event.label.name == 'epic'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install github-sub-issue
        run: |
          cd utils/github-sub-issue
          pip install -e .

      - name: Create sub-issues
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          ISSUE_NUMBER=${{ github.event.issue.number }}

          # Create sub-issues from task list
          github-sub-issue create --parent "$ISSUE_NUMBER" \
            --title "Task 1: Design" --label "design"

          github-sub-issue create --parent "$ISSUE_NUMBER" \
            --title "Task 2: Implementation" --label "implementation"

          github-sub-issue create --parent "$ISSUE_NUMBER" \
            --title "Task 3: Testing" --label "testing"
```

## Troubleshooting

### Error: "Failed to get current repository"

If the current directory is not a GitHub repository, use the `--repo` option:

```bash
github-sub-issue list 123 --repo owner/repo
```

### Error: "The provided sub-issue does not exist"

The issue ID might not be correctly obtained. Check debug information with the `--verbose` option:

```bash
github-sub-issue --verbose add 123 456
```

### Error: "authentication required"

Make sure GitHub CLI is authenticated:

```bash
gh auth status
gh auth login
```

## Best Practices

### 1. Use batch operations for efficiency

```bash
# Add multiple sub-issues in a single command instead of running multiple commands
github-sub-issue add 100 101 102 103 104 105

# This is more efficient than:
# github-sub-issue add 100 101
# github-sub-issue add 100 102
# github-sub-issue add 100 103
# github-sub-issue add 100 104
# github-sub-issue add 100 105
```

### 2. Create sub-issues with appropriate granularity

- Avoid sub-issues that are too large (ideal size: can be completed in 1-3 days)
- Avoid sub-issues that are too small (checklists may be sufficient)

### 3. Use labels effectively

```bash
github-sub-issue create --parent 100 \
  --title "Implement API endpoints" \
  --label "backend,api,priority-high"
```

### 4. Clearly assign issues

```bash
github-sub-issue create --parent 100 \
  --title "Frontend implementation" \
  --assignee "@me"
```

### 5. Check progress regularly

```bash
# Daily progress check
github-sub-issue list 100 --state all

# Calculate progress rate in JSON format
github-sub-issue list 100 --json | \
  jq '[.[] | select(.state == "CLOSED")] | length' | \
  awk '{print "Completion rate: " ($1/4)*100 "%"}'
```
