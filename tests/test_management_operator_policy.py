import json

import pytest

from auto_coder.management.operator_policy import PolicyUnavailable, read_operator_policy


def test_policy_is_read_fresh_and_bound_to_project(tmp_path):
    path = tmp_path / "operators.json"
    path.write_text(json.dumps({"project_id": "project", "allowed_uids": ["one"]}), encoding="utf-8")
    assert read_operator_policy(path, "project").allowed_uids == {"one"}

    path.write_text(json.dumps({"project_id": "project", "allowed_uids": []}), encoding="utf-8")
    assert not read_operator_policy(path, "project").allowed_uids

    path.write_text(json.dumps({"project_id": "other", "allowed_uids": ["one"]}), encoding="utf-8")
    with pytest.raises(PolicyUnavailable):
        read_operator_policy(path, "project")


@pytest.mark.parametrize(
    "contents",
    ["not-json", "{}", '{"project_id":"project","allowed_uids":[""]}', '{"project_id":"project","allowed_uids":"one"}', '{"project_id":"project","allowed_uids":[],"extra":true}'],
)
def test_policy_rejects_unusable_schema(tmp_path, contents):
    path = tmp_path / "operators.json"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(PolicyUnavailable):
        read_operator_policy(path, "project")
