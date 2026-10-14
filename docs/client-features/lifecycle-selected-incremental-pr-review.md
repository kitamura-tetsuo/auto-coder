# Lifecycle-selected incremental PR review

Adversarial PR validation selects its initial or incremental review policy from
the accepted review lifecycle, independently of whether the current provider
can resume an earlier conversation. An accepted lifecycle with a prior reviewed
head therefore retains incremental review after a process restart, local
workspace release, or provider continuation fallback. With no accepted
lifecycle, validation uses the initial-review policy.

Provider ownership and resumability continue to control transport only. A
retained session is continued when its existing safety checks permit that;
otherwise validation uses the supported fresh no-edit invocation without
forcing an old local workspace to resume.

Every incremental prompt is self-contained. It supplies the current
Requirements manifest, current head and patch/test evidence, accepted prior
head, persisted canonical test-oracle gaps, and applicable accepted Strong
findings. It does not assume access to an earlier conversation or invent prior
requirement-coverage conclusions. When reusable coverage evidence is not
explicitly available, the reviewer establishes coverage from current evidence
while retaining incremental discovery and canonical gap-closure limits.
