# Durable direct parent/child implementation exclusion

Every production Issue implementation admission obtains cache-bypassing native
parent and complete direct-child evidence while holding the shared durable slot
lock. Each composite hierarchy read is bracketed by cache-bypassing Issue revision
reads, and the revision must remain unchanged before the evidence can authorize
admission. A
present logical owner for either direct side rejects the new owner independently
of normal, explicit, forced, or emergency capacity. Operational or ambiguous
hierarchy reads fail closed and remain distinct from capacity and semantic
validation outcomes. Hierarchy-guarded records retain their pending marker and
admission evidence for their lifetime. Every later execution admission, including
restart recovery, writes its execution before a final hierarchy refresh and must
discard the guarded owner on conflict or changed evidence before it can return to
implementation dispatch. Once an admission has succeeded, later failed execution
attempts restore that established owner, including its executions, PRs, and provider
sessions; only the failed attempt is discarded. Unrelated Issues and siblings retain
existing concurrency.
