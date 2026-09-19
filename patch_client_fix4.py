with open("src/auto_coder/claude_routine_client.py", "r") as f:
    content = f.read()

# Replace exactly
old_code = """            # REQ-005: Clean task prompt echoes out of stdout/stderr before checking markers
            stdout_clean = result.stdout or ""
            stderr_clean = result.stderr or ""

            # Remove verbatim matches of the message to avoid false positive marker triggers
            if message_prepared:
                stdout_clean = stdout_clean.replace(message_prepared, "")
                stderr_clean = stderr_clean.replace(message_prepared, "")

            combined_clean = stdout_clean + "\\n" + stderr_clean

            is_limit = False
            if config_backend and config_backend.usage_markers:
                is_limit = has_usage_marker_match(combined_clean, config_backend.usage_markers)

            if is_limit:
                # If exit zero but usage limit marker found, it's indeterminate
                # If non-zero and usage limit marker, it's not sent
                certainty = DeliveryCertainty.INDETERMINATE if result.returncode == 0 else DeliveryCertainty.NOT_SENT"""

new_code = """            # REQ-005: Clean task prompt echoes out of stdout/stderr before checking markers
            stdout_clean = result.stdout or ""
            stderr_clean = result.stderr or ""

            # Remove verbatim matches of the message to avoid false positive marker triggers
            if message_prepared:
                stdout_clean = stdout_clean.replace(message_prepared, "")
                stderr_clean = stderr_clean.replace(message_prepared, "")

            combined_clean = stdout_clean + "\\n" + stderr_clean

            is_limit = False
            # Determine if it's a provider limit independently of optional markers,
            # using regex on stdout and stderr independently to prevent task content interference.
            # Must distinguish diagnostic records from task content.
            # Since marker check handles `provider_usage_markers` when configured:

            # First check if the error matches the required independent provider usage limits
            import re

            # Helper to check for json rate limit errors in structured logs
            def _has_structured_rate_limit(text: str) -> bool:
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("{") and line.endswith("}"):
                        try:
                            import json
                            data = json.loads(line)
                            # Structured provider `rate_limit_error`
                            if isinstance(data, dict):
                                err = data.get("error")
                                if isinstance(err, dict) and err.get("type") == "rate_limit_error":
                                    return True
                        except Exception:
                            pass
                return False

            if _has_structured_rate_limit(stdout_clean) or _has_structured_rate_limit(stderr_clean):
                is_limit = True

            # Explicit provider usage/quota-exhausted diagnostic
            if not is_limit:
                if re.search(r"\\b(?:error|exception|diagnostic)[^\\n]*\\b(?:quota|usage\\s*limit\\s*exceeded|rate\\s*limit)\\b", combined_clean, re.IGNORECASE):
                    is_limit = True
                elif re.search(r"exhausted\\s*quota", combined_clean, re.IGNORECASE) or re.search(r"quota\\s*exhausted", combined_clean, re.IGNORECASE):
                    is_limit = True

            # Then check configured markers
            if not is_limit and config_backend and config_backend.usage_markers:
                is_limit = has_usage_marker_match(combined_clean, config_backend.usage_markers)

            if is_limit:
                # If exit zero but usage limit marker found, it's indeterminate
                # If non-zero and usage limit marker, it's not sent
                certainty = DeliveryCertainty.INDETERMINATE if result.returncode == 0 else DeliveryCertainty.NOT_SENT"""

content = content.replace(old_code, new_code)

with open("src/auto_coder/claude_routine_client.py", "w") as f:
    f.write(content)
