# Durable local conflict-repair execution

An admitted local merge-conflict job prepares a private repository from its
retained pull-request head and inspected base commits. The job performs the real
no-commit merge there, preserves conflict-marker contents in stage zero for the
editor, and repeats the strict open, unmerged, exact-head, exact-base, and local
route check immediately before editor entry.

The adapter never commits, pushes, merges the pull request, starts a cloud
follow-up, or mutates the controller checkout. A complete clean merge is recorded
as controller-produced evidence without claiming provider entry. Edited output is
successful only when Git observation is complete, the index has no unmerged paths,
and no changed file retains conflict markers. Missing output, `CANNOT_FIX`, Git
failures, stale authority, and incomplete inspection remain nonpublishing results.

Both clean and edited results use the shared PR repair manifest. It binds the
execution incarnation, retained H/B identities, observed merge lineage, file
identities, and completion source for a later independently authorized publisher.
Focused real-Git coverage is in `tests/test_local_conflict_repair_job.py`.
