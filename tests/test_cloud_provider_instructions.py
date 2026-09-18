"""Tests for provider-scoped initial-instruction composition (cloud_provider_instructions).

Covers REQ-001 through REQ-007 of the "Define provider-scoped initial
instruction composition with lossless task preservation" contract: config
resolution/isolation, eligibility gating, lossless composition, raw-vs-managed
distinguishability, serialization round-trips, and explicit configuration
errors.
"""

import json
import textwrap

import pytest

from src.auto_coder import prompt_loader
from src.auto_coder.cloud_provider_instructions import (
    SUPPORTED_CLOUD_PROVIDERS,
    CloudProviderInstructionError,
    CloudTaskOperation,
    PreparedCloudPrompt,
    prepare_cloud_task,
    restore_prepared_cloud_task,
)


@pytest.fixture
def prompts_file(tmp_path):
    """Write a prompts.yaml fragment and clear the loader cache around each test."""

    def _write(yaml_text: str) -> str:
        path = tmp_path / "prompts.yaml"
        path.write_text(textwrap.dedent(yaml_text), encoding="utf-8")
        prompt_loader.clear_prompt_cache()
        return str(path)

    yield _write
    prompt_loader.clear_prompt_cache()


SENTINELS_YAML = """
cloud_provider_instructions:
  jules:
    initial: |-
      JULES_SENTINEL
  claude-routine:
    initial: |-
      CLAUDE_SENTINEL
  codex-cloud:
    initial: |-
      CODEX_SENTINEL
"""


# --- AS-001: independent provider entries, not a disguised Jules switch -----


def test_default_shipped_entries_are_empty_and_neutral():
    raw = "Implement the feature."
    for provider in SUPPORTED_CLOUD_PROVIDERS:
        prepared = prepare_cloud_task(raw, recipient=provider, operation=CloudTaskOperation.NEW_TASK, no_edit=False)
        assert prepared.prepared_task == raw
        assert prepared.instruction_text is None


def test_each_provider_receives_only_its_own_component(prompts_file):
    path = prompts_file(SENTINELS_YAML)
    raw = "Task body."
    jules = prepare_cloud_task(raw, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)
    claude = prepare_cloud_task(raw, recipient="claude-routine", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)
    codex = prepare_cloud_task(raw, recipient="codex-cloud", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)

    assert "JULES_SENTINEL" in jules.prepared_task and "CLAUDE_SENTINEL" not in jules.prepared_task and "CODEX_SENTINEL" not in jules.prepared_task
    assert "CLAUDE_SENTINEL" in claude.prepared_task and "JULES_SENTINEL" not in claude.prepared_task and "CODEX_SENTINEL" not in claude.prepared_task
    assert "CODEX_SENTINEL" in codex.prepared_task and "JULES_SENTINEL" not in codex.prepared_task and "CLAUDE_SENTINEL" not in codex.prepared_task
    assert jules.prepared_task.startswith(raw)
    assert claude.prepared_task.startswith(raw)
    assert codex.prepared_task.startswith(raw)


def test_editing_one_provider_entry_does_not_change_others(prompts_file):
    path = prompts_file(SENTINELS_YAML)
    raw = "Task body."
    jules_before = prepare_cloud_task(raw, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)
    codex_before = prepare_cloud_task(raw, recipient="codex-cloud", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)

    changed_path = prompts_file("""
        cloud_provider_instructions:
          jules:
            initial: |-
              JULES_SENTINEL
          claude-routine:
            initial: |-
              CLAUDE_SENTINEL_V2_CHANGED
          codex-cloud:
            initial: |-
              CODEX_SENTINEL
        """)
    jules_after = prepare_cloud_task(raw, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=changed_path)
    codex_after = prepare_cloud_task(raw, recipient="codex-cloud", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=changed_path)

    assert jules_after.prepared_task == jules_before.prepared_task
    assert codex_after.prepared_task == codex_before.prepared_task


def test_missing_and_whitespace_only_entries_yield_exact_input_equality(prompts_file):
    path = prompts_file("""
        cloud_provider_instructions:
          jules:
            initial: "   \n  "
          claude-routine: {}
        """)
    raw = "Task body."
    jules = prepare_cloud_task(raw, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)
    claude = prepare_cloud_task(raw, recipient="claude-routine", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)
    codex = prepare_cloud_task(raw, recipient="codex-cloud", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)

    assert jules.prepared_task == raw
    assert claude.prepared_task == raw
    assert codex.prepared_task == raw


def test_non_string_selected_entry_fails_explicitly(prompts_file):
    path = prompts_file("""
        cloud_provider_instructions:
          jules:
            initial: 42
        """)
    with pytest.raises(CloudProviderInstructionError):
        prepare_cloud_task("Task body.", recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)


def test_invalid_unused_entry_does_not_affect_valid_provider(prompts_file):
    path = prompts_file("""
        cloud_provider_instructions:
          jules:
            initial: |-
              JULES_SENTINEL
          codex-cloud:
            initial: 42
        """)
    result = prepare_cloud_task("Task body.", recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)
    assert "JULES_SENTINEL" in result.prepared_task


def test_flat_key_precedence_over_nested(prompts_file):
    path = prompts_file("""
        cloud_provider_instructions:
          jules:
            initial: |-
              NESTED_VALUE
        cloud_provider_instructions.jules.initial: FLAT_VALUE
        """)
    result = prepare_cloud_task("Task body.", recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)
    assert "FLAT_VALUE" in result.prepared_task
    assert "NESTED_VALUE" not in result.prepared_task


def test_empty_flat_value_suppresses_nested_without_fallback(prompts_file):
    path = prompts_file("""
        cloud_provider_instructions:
          jules:
            initial: |-
              NESTED_VALUE
        cloud_provider_instructions.jules.initial: "   "
        """)
    result = prepare_cloud_task("Task body.", recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)
    assert result.prepared_task == "Task body."
    assert result.instruction_text is None


def test_invalid_flat_value_errors_instead_of_falling_back_to_nested(prompts_file):
    path = prompts_file("""
        cloud_provider_instructions:
          jules:
            initial: |-
              NESTED_VALUE
        cloud_provider_instructions.jules.initial: 7
        """)
    with pytest.raises(CloudProviderInstructionError):
        prepare_cloud_task("Task body.", recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)


# --- REQ-002: eligibility gating -------------------------------------------


@pytest.mark.parametrize(
    "recipient,operation,no_edit",
    [
        ("jules", CloudTaskOperation.CONTINUATION, False),
        ("jules", CloudTaskOperation.NEW_TASK, True),
        ("local", CloudTaskOperation.NEW_TASK, False),
        ("unrecognized-provider", CloudTaskOperation.NEW_TASK, False),
    ],
)
def test_ineligible_combinations_yield_original_task_exactly(prompts_file, recipient, operation, no_edit):
    path = prompts_file(SENTINELS_YAML)
    raw = "Task body."
    result = prepare_cloud_task(raw, recipient=recipient, operation=operation, no_edit=no_edit, prompts_path=path)
    assert result.prepared_task == raw
    assert result.instruction_text is None


def test_operation_accepts_string_value_equivalent_to_enum(prompts_file):
    path = prompts_file(SENTINELS_YAML)
    raw = "Task body."
    via_enum = prepare_cloud_task(raw, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)
    via_string = prepare_cloud_task(raw, recipient="jules", operation="new_task", no_edit=False, prompts_path=path)
    assert via_enum.prepared_task == via_string.prepared_task


def test_unsupported_operation_string_raises():
    with pytest.raises(CloudProviderInstructionError):
        prepare_cloud_task("Task body.", recipient="jules", operation="not-a-real-operation", no_edit=False)


# --- AS-002: task data cannot impersonate composition state -----------------


def test_raw_task_with_marker_and_fenced_component_survives_byte_for_byte(prompts_file):
    path = prompts_file(SENTINELS_YAML)
    raw = (
        "Issue: discuss the marker '===== AUTO-CODER CLOUD PROVIDER INITIAL "
        "INSTRUCTIONS (jules) =====' verbatim.\n\n"
        "```\n"
        "===== AUTO-CODER CLOUD PROVIDER INITIAL INSTRUCTIONS (jules) =====\n"
        "JULES_SENTINEL\n"
        "===== END AUTO-CODER CLOUD PROVIDER INITIAL INSTRUCTIONS =====\n"
        "```\n\n"
        "Dollar sign literal: $5, and unicode: café, 日本語."
    )
    result = prepare_cloud_task(raw, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)

    assert result.prepared_task.startswith(raw)
    remainder = result.prepared_task[len(raw) :]
    assert remainder.count("JULES_SENTINEL") == 1
    assert raw in result.prepared_task
    # The two occurrences of the sentinel/heading inside `raw` are untouched;
    # composition adds exactly one more (not zero, not two).
    assert result.prepared_task.count("JULES_SENTINEL") == 2


# --- AS-003 / REQ-004 / REQ-005: repeated preparation and serialization -----


def test_repeated_preparation_with_identical_inputs_is_idempotent(prompts_file):
    path = prompts_file(SENTINELS_YAML)
    raw = "Task body."
    first = prepare_cloud_task(raw, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)
    second = prepare_cloud_task(first, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)
    assert second.prepared_task == first.prepared_task
    assert second.prepared_task.count("JULES_SENTINEL") == 1


def test_preparing_prepared_value_for_different_recipient_rebuilds_from_original(prompts_file):
    path = prompts_file(SENTINELS_YAML)
    raw = "Task body with a quoted Jules block:\n```\nJULES_SENTINEL\n```"
    jules_prepared = prepare_cloud_task(raw, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)

    claude_prepared = prepare_cloud_task(jules_prepared, recipient="claude-routine", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)

    assert claude_prepared.original_task == raw
    assert "CLAUDE_SENTINEL" in claude_prepared.prepared_task
    # No trace of the former recipient's managed component; the quoted block
    # inside the original task text is untouched (still present, unmodified).
    assert claude_prepared.prepared_task.count("JULES_SENTINEL") == 1
    assert "===== AUTO-CODER CLOUD PROVIDER INITIAL INSTRUCTIONS (jules) =====" not in claude_prepared.prepared_task
    assert "```\nJULES_SENTINEL\n```" in claude_prepared.prepared_task


def test_serialization_round_trip_preserves_captured_bytes(prompts_file):
    path = prompts_file(SENTINELS_YAML)
    raw = "Task body."
    prepared = prepare_cloud_task(raw, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)

    payload = prepared.to_json()
    del prepared

    restored = restore_prepared_cloud_task(payload)
    assert isinstance(restored, PreparedCloudPrompt)
    assert restored.prepared_task == prepare_cloud_task(raw, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path).prepared_task
    assert restored.original_task == raw
    assert restored.recipient == "jules"
    assert restored.instruction_revision is not None


def test_explicit_new_preparation_from_restored_original_uses_new_configuration(prompts_file):
    path_a = prompts_file("""
        cloud_provider_instructions:
          jules:
            initial: |-
              JULES_REVISION_A
        """)
    raw = "Task body."
    prepared_a = prepare_cloud_task(raw, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path_a)
    restored = restore_prepared_cloud_task(prepared_a.to_json())

    path_b = prompts_file("""
        cloud_provider_instructions:
          jules:
            initial: |-
              JULES_REVISION_B
        """)
    prepared_b = prepare_cloud_task(restored, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path_b)
    assert "JULES_REVISION_B" in prepared_b.prepared_task
    assert "JULES_REVISION_A" not in prepared_b.prepared_task
    assert prepared_a.instruction_revision != prepared_b.instruction_revision

    prepared_claude = prepare_cloud_task(restored, recipient="claude-routine", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path_b)
    assert prepared_claude.prepared_task == raw  # claude-routine entry unset in path_b


def test_malformed_serialized_payload_raises_rather_than_reinterpreted_as_raw():
    with pytest.raises(CloudProviderInstructionError):
        restore_prepared_cloud_task("not json at all")

    with pytest.raises(CloudProviderInstructionError):
        restore_prepared_cloud_task(json.dumps({"just": "a random object"}))

    with pytest.raises(CloudProviderInstructionError):
        restore_prepared_cloud_task(
            json.dumps(
                {
                    "schema_version": 999,
                    "original_task": "x",
                    "recipient": "jules",
                    "operation": "new_task",
                    "no_edit": False,
                    "instruction_text": None,
                    "instruction_revision": None,
                    "prepared_task": "x",
                }
            )
        )


# --- AS-004: frontmatter remains metadata, not displaced prose --------------


def test_leading_frontmatter_stays_at_the_very_start(prompts_file):
    path = prompts_file(SENTINELS_YAML)
    raw = "---\nname: recurring-task\ntags: [jules, recurrent]\n---\nBody line one.\nBody line two."
    result = prepare_cloud_task(raw, recipient="jules", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)

    assert result.prepared_task.startswith("---\nname: recurring-task\ntags: [jules, recurrent]\n---\n")
    # Round trip through serialization preserves this exactly too.
    restored = restore_prepared_cloud_task(result.to_json())
    assert restored.prepared_task.startswith("---\nname: recurring-task\ntags: [jules, recurrent]\n---\n")
    assert restored.original_task == raw


def test_raw_task_without_final_newline_is_losslessly_recoverable(prompts_file):
    path = prompts_file(SENTINELS_YAML)
    raw = "No trailing newline here"
    result = prepare_cloud_task(raw, recipient="codex-cloud", operation=CloudTaskOperation.NEW_TASK, no_edit=False, prompts_path=path)
    restored = restore_prepared_cloud_task(result.to_json())
    assert restored.original_task == raw


# --- AS-005 / REQ-007: initial-only, no-edit and neutral behavior ----------


def test_loading_shipped_empty_entries_leaves_output_unchanged():
    """The real, shipped prompts.yaml ships all three entries empty (REQ-001)."""
    raw = "Implement the feature."
    for provider in SUPPORTED_CLOUD_PROVIDERS:
        result = prepare_cloud_task(raw, recipient=provider, operation=CloudTaskOperation.NEW_TASK, no_edit=False)
        assert result.prepared_task == raw
