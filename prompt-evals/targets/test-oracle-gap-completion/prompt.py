"""Render the shipped focused gap-completion prompt for advisory semantic evaluation.

These cases use a real model only when the selective Promptfoo workflow is run.
No real model was executed while authoring their deterministic fixtures.
"""

from auto_coder.prompt_loader import render_prompt


def render(context):
    return (
        render_prompt(
            "pr.adversarial_validation_gap_completion",
            repo_name="owner/repo",
            pr_number=42,
            head_sha="head-h2",
            base_sha="base-b",
            validation_snapshot="snapshot-h2",
            requirement_manifest="Manifest mode: CURRENT\n- REQ-001: Preserve rejected candidate state.",
            completion_gap_ids='["TOG-example"]',
            completion_gaps=context["vars"]["recorded_gap"],
            prior_adjudication=context["vars"]["scenario"],
            changed_tests=context["vars"]["changed_tests"],
            changed_files="src/grid.py, tests/test_grid.py",
            ci_execution_evidence=context["vars"]["ci_evidence"],
        )
        + "\n\nReturn only the complete adversarial JSON response."
    )
