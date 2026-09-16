from src.auto_coder.usage_marker_utils import has_http_429_marker, has_usage_marker_match


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


def test_has_http_429_marker_detects_unambiguous_status_references():
    assert has_http_429_marker("429 Too Many Requests")
    assert has_http_429_marker("HTTP 429 too many requests")
    assert has_http_429_marker("openai api streaming error: 429 provider returned error")
    assert has_http_429_marker("Error 429: Too many requests")
    assert has_http_429_marker("status: 429\nToo Many Requests\n")


def test_has_http_429_marker_ignores_unrelated_numbers():
    assert not has_http_429_marker("")
    assert not has_http_429_marker("normal error message")
    assert not has_http_429_marker("error: 400 model access denied.")
    assert not has_http_429_marker("429 tests passed")
    assert not has_http_429_marker('Traceback (most recent call last):\n  File "app.py", line 429, in run')
    assert not has_http_429_marker("connected to port 8429")
