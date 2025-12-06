from src.auto_coder.usage_marker_utils import has_usage_marker_match


def test_matches_json_fragment_with_prefix_text():
    output = "2025-12-07 00:06:46.125 | INFO | auto_coder/claude_client.py:161 in _run_llm_cli - " '{"type":"result","subtype":"success","is_error":true,"result":"Limit reached - resets 2am (Asia/Tokyo)"}'
    marker = {"type": "result", "is_error": True}

    assert has_usage_marker_match(output, [marker])


def test_matches_nested_json_values_with_partial_strings():
    output = '{"error":{"type":"rate_limit_error","message":"Limit reached soon"}}'
    marker = {"error": {"type": "rate_limit_error", "message": "Limit reached"}}

    assert has_usage_marker_match(output, [marker])


def test_falls_back_to_string_contains_check():
    output = "Standard output\nRate LIMIT encountered\n"
    marker = "rate limit"

    assert has_usage_marker_match(output, [marker])


def test_returns_false_when_marker_not_present():
    output = '{"status":"ok","details":{"info":"all good"}}'
    marker = {"error": {"code": 429}}

    assert not has_usage_marker_match(output, [marker])
