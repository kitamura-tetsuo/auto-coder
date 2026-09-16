# Cloud Implementation Agent Review-Thread Address-Without-Resolve Protocol

  review_thread_address_without_resolve:
    description: "When Auto-Coder delegates unresolved GitHub PR review-thread feedback to a cloud implementation agent, the agent must fix and explain, never resolve the thread itself; a later independent validation pass owns the resolve decision."
    implementation: |
      REVIEW_ADDRESSED_MARKER, reply_claims_review_addressed
      in src/auto_coder/review_feedback_marker.py,
      issue.action, pr.adversarial_validation_fix,
      codex_cloud.continuation, codex_cloud.ci_review_repair_details
      in src/auto_coder/prompts.yaml
    behavior:
      - "Applies to every prompt that delegates unresolved PR review feedback to a cloud implementation agent (issue-to-PR implementation, Codex Cloud continuation/CI-repair resumes, and the adversarial-validation fix-forward prompt), regardless of which reviewer authored the original finding (Codex GitHub review, Auto-Coder's adversarial reviewer, or any other automated or human reviewer)."
      - "Each of these prompts explicitly instructs the agent to inspect and address actionable review feedback but never to resolve, close, or otherwise mark the corresponding GitHub review thread as resolved; that decision is reserved for a later independent validation pass."
      - "For each thread the agent believes it has successfully addressed, it must reply directly in that same thread with a concise explanation of the implementation change and the validation/regression-test evidence, ending the reply with the stable, versioned marker `<!-- auto-coder-review-addressed:v1 -->` on its own line. The agent must add the marker only when it is actually confident the finding was corrected; if it cannot fix or verify a finding, it leaves the thread unresolved and omits the marker."
      - "Addressing one thread never implies any other unresolved thread was addressed; each claimed resolution is independent and must be posted to its own thread."
      - "`reply_claims_review_addressed()` in src/auto_coder/review_feedback_marker.py detects the marker via a versioned regex and never infers an addressed claim from unrestricted natural-language phrases such as 'fixed', 'done', or 'resolved' alone."
      - "This protocol only changes the cloud-agent instruction and the addressed-claim marker; it does not itself resolve threads, change the existing unresolved-review-thread merge gate, or decide whether a claimed-addressed reply is actually correct."
