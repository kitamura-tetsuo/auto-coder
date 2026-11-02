# Recorded open targets in the dashboard

The dashboard uses advisory observations from the worker's existing complete
startup enumeration and successful strict target reads. Startup therefore shows
known Issue/PR identities immediately, even before the worker has obtained their
titles. Later strict open observations supply titles; strict closed and 404
observations remove that target. Author allowlists and admission decisions do not
hide observed targets or grant execution from this view. No dashboard refresh
causes a GitHub request, and no discovery timer is added.

The status projection includes `open_target_observation` with enumeration
availability, its successful observation timestamp and diagnostic source. Before
a successful complete enumeration, the UI reports unavailable enumeration rather
than an empty repository. Failed enumeration retains prior observations with a
stale indication. Complete empty observations are distinct. The list is recorded
evidence, not a live repository listing; individual transport or malformed reads
do not erase targets. A strict observation made during an enumeration wins over
that older enumeration, including a closure/404 tombstone.

Legacy polling snapshots remain a fallback for legacy callers while complete
observations are unavailable. They do not manufacture complete-enumeration
availability. This in-memory view is reset on restart and never drives durable
invalidation completion, implementation ownership, validation or merge authority.
The public diagnostics API allowlist is unchanged.

Run `pytest -q tests/test_open_target_observations.py tests/test_entity_invalidation.py`
for production reconciliation/strict observation/status/presentation regressions.
The initial two regression cases fail against the previous engine: one returns
an empty set after successful nonempty enumeration, and the other lacks an
availability observation. The UI calls the tested `open_targets_notice` projection.
