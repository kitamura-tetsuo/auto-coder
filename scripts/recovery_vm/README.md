# Native recovery epoch fixture

This runner boots a fresh Ubuntu VM with its own Linux kernel, initial namespaces,
systemd, Docker engine and boot identity. It does not run the host issuer on the
outer CI runner or use a privileged Docker container as a substitute for the VM.
The driver exercises original provenance, current containment, genuine installed
READY and the production issuer/consumer entrypoints. A run is acceptance only
when its exact scenario oracle succeeds and the VM exits successfully.

Preparation selects KVM when the outer host exposes `/dev/kvm`; its separate
Compose override passes that exact device and its actual group to the unprivileged
QEMU process. The preparing shell does not need membership in the device group.
If that device is absent, preparation explicitly selects QEMU TCG CPU emulation
with the `max` CPU model. The selected accelerator is bound in the input manifest.
Both modes boot the same real kernel and execute the same native oracles. A KVM
permission or execution failure is an error; the runner does not silently switch
an already prepared KVM input to another mode.

The outer QEMU container has a read-only root, no capabilities, no new privileges,
2 CPUs, 6 GiB memory and a 256-process limit. Only prepared input, the new guest
disk and bounded output are mounted. Each scenario gets a new directory and new
32 GiB sparse guest disk; an existing disk or original epoch is never reset.

Preparation also binds the actual source Docker image store (`overlay2` or the
`overlayfs` containerd snapshotter). The fresh guest configures the same store
before its first daemon start and verifies both imported images retain their
exact approved IDs. Importing between these stores can change the meaning of
Docker's image ID; a changed ID is a preparation failure, not a reason to select
another image or weaken the production source checks.

## Reproduce a reviewed scenario

Use a clean, committed checkout with the locked test dependencies installed. The
read credential is used only to verify the actual immutable tested-source tag and
successful GitHub test run for the installed READY. It needs repository contents
and Actions read access, not publication authority. Keep it in an existing private
file; do not put its value on the command line.

```sh
uv sync --locked --dev --extra test
uv run python scripts/prepare_management_test_image.py
docker pull caddy:2
export APPROVED_AUTHORIZATION_ID=reviewed-recovery-fixture-unique
RECOVERY_FIXTURE=/var/tmp/ac-recovery-vm-unique
MANAGER_IMAGE_ID=$(docker image inspect --format '{{.Id}}' auto-coder:management-fixture-$(git rev-parse --short=12 HEAD))
CADDY_IMAGE_ID=$(docker image inspect --format '{{.Id}}' caddy:2)
READ_TOKEN_FILE=/absolute/path/to/private-read-token
bash scripts/recovery_vm/prepare.sh "$RECOVERY_FIXTURE" "$APPROVED_AUTHORIZATION_ID" "$MANAGER_IMAGE_ID" "$CADDY_IMAGE_ID" "$READ_TOKEN_FILE" grant
docker compose --env-file "$RECOVERY_FIXTURE/compose.env" build
docker compose --env-file "$RECOVERY_FIXTURE/compose.env" run --rm fixture
```

The prepared `compose.env` selects the exact reviewed Compose files, including the
KVM-only device override when applicable. Do not add a separate `-f` argument
that would discard this selection. The VM-only authorization marker binds the
actual new machine, boot and engine. The driver verifies it before provisioning
any management fixture. It refuses a populated engine or earlier fixture roots.

`ACCEPTANCE_SCENARIOS` and `required_cases()` in
`scripts/test_management_recovery_epoch_host.py` define the complete finite
scenario oracle. CI derives its matrix directly from this list and requires
every matrix job to pass. The two `kill-after-*` debug aliases select boundaries
already present in the matrix and do not add independent cases. All before/after
publication cases and all ten mutations after the original intent head run on
independent fresh VMs. They
use real ptrace syscall stops around the actual exclusive record/head creation
and successful file/directory fsync, with a pinned SIGKILL and no syscall-result
replacement. The native process oracles also inspect real peer custody, original
store preservation, unrelated worker lifetimes and effect requests, and absence
of a replacement child/admission. Component guard tests cannot substitute for
these executions.

The `grant` scenario first requires all four actual protocol refusals implemented
in `scripts/recovery_epoch_native_peer.py`: queued rights without a receipt,
received rights without a receipt, a replaced target lifetime, and an SH lock
whose acquiring process differs from the target. Each must preserve an entirely
unreserved epoch before a later independent invocation can grant. Its successful
path also holds the original issuer at the durable reservation head while an
overlapping contender is refused. The separate `lost-reply` scenario requires an
actual successful grant send while the receiver is stopped, then verifies the
issuer's pidfd is dead before resuming the receiver; consumption must remain and
the receiver must refuse current custody. The companion helper tests validate
the fixture's native message parsing and guards; they are not VM acceptance.

The guest read credential is deleted on exit. The outer input credential must
also be deleted after the run; the CI workflow does this even on failure.
Artifacts contain the result, bounded diagnostics and guest/serial logs, not the
original OAuth stores or a reusable authority inventory. The fixture is not an
incident-recovery procedure and cannot fill missing historical evidence.
