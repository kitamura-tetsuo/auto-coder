# Legacy `@auto-coder` label semantic inertness

  legacy_auto_coder_label_semantic_inertness:
    description: "Makes the exact retired `@auto-coder` label semantically inert at webhook invalidation, label-to-prompt selection, and semantic PR-label resolution, without removing it as a lifecycle processing lock (Issue #1792)."
    implementation: |
      LEGACY_AUTO_CODER_LABEL, filter_legacy_auto_coder_label, remove_legacy_auto_coder_label in src/auto_coder/label_manager.py,
      get_semantic_labels_from_issue filtering, resolve_pr_labels_with_priority in src/auto_coder/label_manager.py,
      _resolve_label_priority filtering in src/auto_coder/prompt_loader.py,
      _is_legacy_auto_coder_label_change, process_github_payload in src/auto_coder/webhook_server.py,
      issue/PR label extraction in src/auto_coder/issue_processor.py, src/auto_coder/pr_processor.py, src/auto_coder/gemini_client.py
    behavior:
      - "A `labeled` or `unlabeled` GitHub webhook delivery whose changed label name is exactly `@auto-coder` never creates, enqueues, or advances a durable entity invalidation, regardless of who made the change; every other label change, including near-misses such as `auto-coder` or `@auto-coder-old`, continues through the normal invalidation and delivery-deduplication path unaffected."
      - "Before entity labels are supplied to any label-to-prompt selector, interpolated into any LLM prompt label list or label variable, or supplied to semantic PR-label resolution (`get_semantic_labels_from_issue`, `resolve_pr_labels_with_priority`, `_resolve_label_priority`), every exact raw `@auto-coder` label is removed from that consumer's input set, before normalization, alias lookup, fuzzy matching, priority ordering, or string rendering."
      - "A configured alias, label-prompt mapping, or PR-label mapping that names `@auto-coder` directly can never select a prompt template or propagate a semantic label from it, because the label is excluded from the input set before any lookup occurs."
      - "Two authoritative entity states that are identical except that one carries the exact `@auto-coder` label produce identical label-derived LLM prompt content, prompt-template selection, and propagated semantic PR labels."
      - "This filtering never adds, removes, renames, or otherwise mutates an existing `@auto-coder` label on GitHub, and never changes matching, normalization, alias, priority, or propagation semantics for any other label."
