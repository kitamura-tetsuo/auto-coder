import re

with open('src/auto_coder/automation_engine.py', 'r') as f:
    content = f.read()

# Make sure we actually enqueue the review work if the decision is missing
old_str_lane = """        # We must observe durable decisions instead of invoking reviewer backend inline
        decision = validator.store.get(identity)
        if decision is not None:"""

new_str_lane = """        # We must observe durable decisions instead of invoking reviewer backend inline
        decision = validator.store.get(identity)
        if decision is None:
            from .entity_invalidation import EntityIdentity
            self.invalidations.invalidate(EntityIdentity(repo_name, "issue", issue_number))
        if decision is not None:"""

content = content.replace(old_str_lane, new_str_lane)

old_str_eager2 = """            decomposition_decision = decomposition_validator.store.get(decomposition_validator.identity(*authoritative_set)) if decomposition_validator else None

            joined_child_decisions = {}"""

new_str_eager2 = """            decomposition_decision = decomposition_validator.store.get(decomposition_validator.identity(*authoritative_set)) if decomposition_validator else None
            if decomposition_validator and decomposition_decision is None:
                from .entity_invalidation import EntityIdentity
                self.invalidations.invalidate(EntityIdentity(repo_name, "issue", int(authoritative_set[0]["number"])))

            joined_child_decisions = {}"""

content = content.replace(old_str_eager2, new_str_eager2)

old_str_eager3 = """                if decomposition_enabled:
                    decomposition_decision = decomposition_validator.store.get(decomposition_validator.identity(*authoritative_set))
                    if decomposition_decision is None:"""

new_str_eager3 = """                if decomposition_enabled:
                    decomposition_decision = decomposition_validator.store.get(decomposition_validator.identity(*authoritative_set))
                    if decomposition_decision is None:
                        from .entity_invalidation import EntityIdentity
                        self.invalidations.invalidate(EntityIdentity(repo_name, "issue", int(authoritative_set[0]["number"])))"""

content = content.replace(old_str_eager3, new_str_eager3)

old_str_eager4 = """                    parent_decision = None
                    if self._is_issue_decomposition_validation_enabled(repo_name, config):
                        parent_validator = self._get_decomposition_validator(repo_name)
                        parent_decision = parent_validator.store.get(parent_validator.identity(*parent_submission_set))

                    child_decisions = {}"""

new_str_eager4 = """                    parent_decision = None
                    if self._is_issue_decomposition_validation_enabled(repo_name, config):
                        parent_validator = self._get_decomposition_validator(repo_name)
                        parent_decision = parent_validator.store.get(parent_validator.identity(*parent_submission_set))
                        if parent_decision is None:
                            from .entity_invalidation import EntityIdentity
                            self.invalidations.invalidate(EntityIdentity(repo_name, "issue", int(parent_submission_set[0]["number"])))

                    child_decisions = {}"""

content = content.replace(old_str_eager4, new_str_eager4)

# Replace the ValidationAdmissionDeferred throwing
old_str_deferred = """        if failures:
            raise ValidationAdmissionDeferred(f"validation batch incomplete while processing child invalidation: {', '.join(failures)}")"""
new_str_deferred = """        if failures:
            from .exceptions import ParentSpecificationError
            raise ParentSpecificationError(f"validation batch incomplete while processing child invalidation: {', '.join(failures)}")"""
content = content.replace(old_str_deferred, new_str_deferred)

with open('src/auto_coder/automation_engine.py', 'w') as f:
    f.write(content)
