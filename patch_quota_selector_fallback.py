import sys

def modify_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    old_block = """    eligible_evals = [e for e in evaluations if e.is_eligible]
    if not eligible_evals:
        logger.warning("No candidate high-score backends were eligible under quota checks; falling back to configured order")
        return backend_names"""

    new_block = """    eligible_evals = [e for e in evaluations if e.is_eligible]
    if not eligible_evals:
        if strategy == "burst":
            # For burst mode, never fall back to an explicitly exhausted backend
            non_exhausted = [e.backend_name for e in evaluations if not ("exhausted" in (e.reason or "").lower())]
            if non_exhausted:
                logger.warning("No candidate high-score backends were eligible; falling back to non-exhausted configured order")
                return non_exhausted
            logger.warning("All candidate high-score backends are exhausted; returning empty list")
            return []
        else:
            logger.warning("No candidate high-score backends were eligible under quota checks; falling back to configured order")
            return backend_names"""

    content = content.replace(old_block, new_block)

    with open(filepath, 'w') as f:
        f.write(content)

modify_file('src/auto_coder/quota_selector.py')
