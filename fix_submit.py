import re

with open('src/auto_coder/dashboard_adjudication.py', 'r') as f:
    content = f.read()

# Add contract_digest to SubmitRequest
content = content.replace(
    'head_sha: str\n    supersedes: Tuple[str, ...]',
    'head_sha: str\n    contract_digest: str\n    supersedes: Tuple[str, ...]'
)

# Add validation logic to the submit endpoint
# We need to find the submit route and add validation.

submit_start = content.find('def submit(payload: SubmitRequest, repository: str, session: SessionData = Depends(require_auth)):')
if submit_start != -1:
    validation_code = """    valid_pairs = [("UPHOLD", "FIX"), ("OVERRULE", "NO_CHANGE"), ("UNDECIDED", "NONE")]
    if (payload.verdict, payload.directive) not in valid_pairs:
        raise HTTPException(status_code=400, detail="Invalid verdict/directive pair")
    if not payload.rationale or payload.rationale.strip() == "":
        raise HTTPException(status_code=400, detail="Rationale must be nonblank")
"""

    # insert after the docstring or first line of def
    insert_pos = content.find('journal_state', submit_start)
    if insert_pos != -1:
        # Find the line before journal_state
        last_newline = content.rfind('\n', submit_start, insert_pos)
        content = content[:last_newline+1] + validation_code + content[last_newline+1:]

with open('src/auto_coder/dashboard_adjudication.py', 'w') as f:
    f.write(content)
