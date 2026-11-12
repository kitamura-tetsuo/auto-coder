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

`SCENARIOS` and `required_cases()` in
`scripts/test_management_recovery_epoch_host.py` define the finite scenario
oracle. All before/after publication cases run on independent fresh VMs. They
use real ptrace syscall stops around the actual exclusive record/head creation
and successful file/directory fsync, with a pinned SIGKILL and no syscall-result
replacement. The native process oracles also inspect real peer custody, original
store preservation, unrelated worker lifetimes and effect requests, and absence
of a replacement child/admission. Component guard tests cannot substitute for
these executions.

The guest read credential is deleted on exit. The outer input credential must
also be deleted after the run; the CI workflow does this even on failure.
Artifacts contain the result, bounded diagnostics and guest/serial logs, not the
original OAuth stores or a reusable authority inventory. The fixture is not an
incident-recovery procedure and cannot fill missing historical evidence.
