# Authoritative Jules candidate observations

Speculative Jules candidate artifacts are observed by reading the exact session
bound in the durable competition ledger and then validating every advertised pull
request with a strict GitHub read. The adapter checks the canonical session and
source repository, preserves every supported output entry, reports malformed or
unavailable evidence explicitly, and keeps verified membership durably even when a
later provider read is empty or unavailable.

Provider state is retained independently from output availability; the supported
Jules states remain distinct and unknown values never imply success or failure.
Repository, generation, candidate, and monotonically ordered read identities fence
publication, so a response older than a published observation or invalidation is
diagnostic only.

Classification uses durable generation selection and retirement state. It
distinguishes selected, active-unselected, retired, suspected, legacy, and blocked
artifacts. A shared pull request or writable head across candidates is conflicting
provenance and grants neither adoption nor cleanup authority. Absence from current
outputs does not erase membership, and unresolved Jules-origin artifacts remain
suspected while speculative history exists. The adapter never starts or stops a
session, mutates GitHub, selects a winner, advances attempts, or lists provider
accounts.
