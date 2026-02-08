from datetime import datetime

import pytest

from src.auto_coder.dashboard import prepare_log_rows


def test_prepare_log_rows_sorts_newest_first():
    # Setup logs with timestamps (Oldest first, as TraceLogger provides)
    logs = [
        {"timestamp": 1700000001, "category": "Cat1", "message": "Msg1", "details": {"foo": "bar"}},
        {"timestamp": 1700000002, "category": "Cat2", "message": "Msg2", "details": {"baz": "qux"}},
        {"timestamp": 1700000003, "category": "Cat3", "message": "Msg3", "details": None},
    ]

    # Call function
    rows = prepare_log_rows(logs)

    # Assert rows are reversed (Newest first)
    assert len(rows) == 3
    assert rows[0]["message"] == "Msg3"
    assert rows[1]["message"] == "Msg2"
    assert rows[2]["message"] == "Msg1"

    # Verify formatting
    assert rows[0]["category"] == "Cat3"
    assert rows[0]["details"] == "None"

    assert rows[2]["time"] == datetime.fromtimestamp(1700000001).strftime("%H:%M:%S")
    assert rows[2]["details"] == "{'foo': 'bar'}"


def test_prepare_log_rows_empty():
    assert prepare_log_rows([]) == []


def test_prepare_log_rows_missing_details():
    logs = [{"timestamp": 1700000001, "category": "Cat1", "message": "Msg1"}]  # details key missing
    rows = prepare_log_rows(logs)
    assert rows[0]["details"] == ""
