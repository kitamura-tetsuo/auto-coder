# Read-only implementation-slot snapshots

`ImplementationSlotRepository.snapshot()` exposes an immutable, typed view of
the repository instance's selected durable implementation-owner store. A known
snapshot reports the effective store path, repository, observation time,
configured normal limit, logical-owner rows, recorded execution/PR/provider
membership, admission flags, and normal/emergency occupancy derived from one
complete state image. Missing legacy fields retain their documented absence or
empty/default meaning without migration.

Observation never creates or repairs the state or coordination files, performs
lifecycle cleanup, probes liveness, or contacts an external service. It reads one
descriptor of the atomically replaced state file without acquiring writer
coordination locks, so slow admission hierarchy lookups cannot prevent
observation even when the last published usage is zero. Pending admissions remain
visible as recorded evidence; the observation does not authorize new work.
Invalid or unreadable projected state returns a typed unavailable result with a
source-aware diagnostic and no fabricated occupancy or observation timestamp.
