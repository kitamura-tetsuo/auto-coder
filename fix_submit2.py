import re

with open('src/auto_coder/dashboard_adjudication.py', 'r') as f:
    content = f.read()

submit_start = content.find('async def submit(req: Request, payload: SubmitRequest):')
if submit_start != -1:
    validation_code = """
            valid_pairs = [("UPHOLD", "FIX"), ("OVERRULE", "NO_CHANGE"), ("UNDECIDED", "NONE")]
            if (payload.verdict, payload.directive) not in valid_pairs:
                raise HTTPException(status_code=409, detail="Invalid verdict/directive pair")
            if not payload.rationale or payload.rationale.strip() == "":
                raise HTTPException(status_code=409, detail="Rationale must be nonblank")
"""

    insert_pos = content.find('session, config = require_auth(req, self)', submit_start)
    if insert_pos != -1:
        last_newline = content.find('\n', insert_pos)
        content = content[:last_newline] + validation_code + content[last_newline:]

with open('src/auto_coder/dashboard_adjudication.py', 'w') as f:
    f.write(content)
