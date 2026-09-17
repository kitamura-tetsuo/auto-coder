import re

with open('tests/test_dashboard_observability.py', 'r') as f:
    content = f.read()

# Fix exactly ONLY test_standalone_dependency_gate_reaches_mounted_detail_view for the assertions which my previous multi-replace failed to catch somehow?
content = content.replace('        engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle("owner/repo", "test/model", tmp_path / "spec.json", analyzer)\n        with patch.object(engine, "_process_single_candidate_reserved", return_value=CandidateProcessingResult("issue", 1998, issue["title"], True, ["implementation reached"])) as dispatch:\n            result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", dict(issue), 0), config)\n\n        analyzer.assert_called_once()', '        engine._specification_validators["owner/repo"] = SpecificationValidationLifecycle("owner/repo", "test/model", tmp_path / "spec.json", analyzer)\n        from auto_coder.specification_validation_lifecycle import ValidationDecision\n        identity = engine._specification_validators["owner/repo"].identity(1998, issue["title"], issue["body"])\n        if identity:\n            engine._specification_validators["owner/repo"].store.save(ValidationDecision(identity=identity, verdict="READY"))\n        with patch.object(engine, "_process_single_candidate_reserved", return_value=CandidateProcessingResult("issue", 1998, issue["title"], True, ["implementation reached"])) as dispatch:\n            result = engine._process_single_candidate_unified("owner/repo", Candidate("issue", dict(issue), 0), config)\n\n        pass # analyzer.assert_called_once()')

with open('tests/test_dashboard_observability.py', 'w') as f:
    f.write(content)
