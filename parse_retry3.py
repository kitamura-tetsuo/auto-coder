import re

with open("src/auto_coder/claude_routine_client.py", "r") as f:
    content = f.read()

retry_logic = r"""            # REQ-007: Parse blocking window resets if all blockers have valid resets
            if quota.reason and "could not be retrieved" not in quota.reason:
                import re
                from datetime import datetime, timezone

                blocks = [b.strip() for b in quota.reason.split(";")]
                all_have_resets = True
                earliest_reset_ts = float('inf')

                for block in blocks:
                    if not block:
                        continue
                    # Skip if just extra usage disabled or rate limit error without reset
                    if "Extra usage disabled:" in block or "rate limit error" in block.lower() or "malformed" in block.lower():
                        all_have_resets = False
                        break

                    m = re.search(r"resets at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", block)
                    if m:
                        try:
                            dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                            ts = dt.timestamp()
                            if ts < earliest_reset_ts:
                                earliest_reset_ts = ts
                        except ValueError:
                            all_have_resets = False
                            break
                    else:
                        all_have_resets = False
                        break

                if all_have_resets and earliest_reset_ts != float('inf'):
                    retry_not_before = max(obs_time + 60, earliest_reset_ts)

            # Use reliable explicit Retry-After if provided
            retry_after_secs = getattr(quota, "retry_after_seconds", None)
            if retry_after_secs is not None and isinstance(retry_after_secs, (int, float)) and retry_after_secs > 0:
                retry_not_before = max(retry_not_before, obs_time + retry_after_secs)"""

old_str = r"""            # Use reliable explicit Retry-After if provided
            retry_after_secs = getattr(quota, "retry_after_seconds", None)
            if retry_after_secs is not None and isinstance(retry_after_secs, (int, float)) and retry_after_secs > 0:
                retry_not_before = max(retry_not_before, obs_time + retry_after_secs)
            else:
                # REQ-007: Parse blocking window resets if all blockers have valid resets
                if quota.reason and "could not be retrieved" not in quota.reason:
                    import re
                    from datetime import datetime, timezone

                    blocks = [b.strip() for b in quota.reason.split(";")]
                    all_have_resets = True
                    earliest_reset_ts = float('inf')

                    for block in blocks:
                        if not block:
                            continue
                        # Skip if just extra usage disabled or rate limit error without reset
                        if "Extra usage disabled:" in block or "rate limit error" in block.lower():
                            all_have_resets = False
                            break

                        m = re.search(r"resets at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", block)
                        if m:
                            try:
                                dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                                ts = dt.timestamp()
                                if ts < earliest_reset_ts:
                                    earliest_reset_ts = ts
                            except ValueError:
                                all_have_resets = False
                                break
                        else:
                            all_have_resets = False
                            break

                    if all_have_resets and earliest_reset_ts != float('inf'):
                        retry_not_before = max(obs_time + 60, earliest_reset_ts)"""

content = content.replace(old_str, retry_logic)

with open("src/auto_coder/claude_routine_client.py", "w") as f:
    f.write(content)
