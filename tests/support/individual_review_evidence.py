"""Read the same self-contained evidence supplied at the semantic boundary."""

from auto_coder.individual_evidence_envelope import decode_individual_evidence


def prompt_evidence(prompt):
    assert "Individual Evidence Envelope (transport version 1)" in prompt
    payload = prompt.rsplit("```json\n", 1)[1].split("\n```", 1)[0]
    return decode_individual_evidence(payload)
