# Parent-Issue Metadata Fallback for Sub-issues

  parent_issue_metadata_fallback:
    description: "Declarative fallback in issue bodies (Parent-Issue: #<number>) for sub-issue relationships when native sub-issues cannot be created."
    implementation: |
      parse_parent_issue_number, add_sub_issue, get_parent_issue_details, get_open_issues_json in src/auto_coder/util/gh_cache.py,
      sibling exclusion and _has_open_sub_issues in src/auto_coder/automation_engine.py
    behavior:
      - "Parses `Parent-Issue: #<number>` case-insensitively from issue bodies."
      - "Attempts to promote/register the issue as a native GitHub sub-issue using the GitHub REST API (POST /repos/{owner}/{repo}/issues/{parent_number}/sub_issues) when possible."
      - "If native sub-issue promotion fails or is unsupported in the agent environment, retains the metadata as a fallback parent relationship."
      - "Scans all open issues upfront at the beginning of issue retrieval/processing to detect Parent-Issue relationships before constructing issue candidates, ensuring correct processing order even when a parent issue has a lower number than its sub-issues."
      - "Before implementation admission, native parent, sub-issue, and dependency relationships are refreshed through cache-bypassing REST reads. Parents with open children remain containers; siblings are ordered only by explicit Blocked-By/native dependency edges, while unavailable or malformed hierarchy evidence fails closed and is retried by capacity refill."
      - "Explicit `--only` Issue processing strictly enumerates current open Issues and performs a relationship-only reconciliation of every declaration affecting the target's parent/direct-child set before validation or dispatch, then re-reads the native hierarchy; failures block or defer the target without processing discovered siblings."
