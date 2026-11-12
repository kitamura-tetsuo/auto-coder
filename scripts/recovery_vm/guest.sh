#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
input=/mnt/fixture-input
output=/mnt/fixture-output
export PYTHONDONTWRITEBYTECODE=1
exec > >(tee -a "$output/guest.log") 2>&1

finish() {
    result=$?
    trap - EXIT
    set +e
    rm -f /run/recovery-fixture-source-token
    {
        systemctl cat cloud-final.service cloud-config.service cloud-init.service cloud-init-local.service
        systemctl show cloud-final.service cloud-config.service cloud-init.service cloud-init-local.service -p ExecStart -p ActiveState -p UnitFileState
        docker version
        docker compose version
    } > "$output/guest-platform.txt" 2>&1
    printf '%s\n' "$result" | tee "$output/guest-exit-code" > /dev/null
    sync
    systemctl poweroff --no-block
    exit "$result"
}
trap finish EXIT
python3 - "$input" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
manifest = json.loads((root / "manifest.json").read_text())
if manifest.get("schema") != "auto-coder-recovery-vm-input-v1":
    raise ValueError("Unsupported recovery fixture input")
for name, digest in manifest["inputs_sha256"].items():
    with (root / name).open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError("Recovery fixture input changed: " + name)
PY
export DEBIAN_FRONTEND=noninteractive
# Match the source store before the new daemon starts; cross-store imports can
# change Docker's image IDs even when their layers are identical.
mkdir -p /etc/docker
test ! -e /etc/docker/daemon.json
test ! -L /etc/docker/daemon.json
install -m 600 "$input/docker-daemon.json" /etc/docker/daemon.json
apt-get update
apt-get install -y --no-install-recommends ca-certificates cron curl docker.io docker-compose-v2 git python3-venv
systemctl start docker
docker info --format 'Actual guest image store: {{.Driver}} {{json .DriverStatus}}'
python3 -m venv /opt/fixture-tools
/opt/fixture-tools/bin/pip install uv==0.13.6
mkdir -m 700 /opt/recovery-fixture /opt/reviewed-source
tar --extract --gzip --file "$input/source.tar.gz" --directory /opt/recovery-fixture
cd /opt/recovery-fixture
/opt/fixture-tools/bin/uv sync --locked --no-editable --no-dev --extra test --link-mode copy
verify_installed_watcher() {
.venv/bin/python - <<'PY'
from pathlib import Path
import auto_coder.management.proxy_watch_cli as watcher

source = Path(watcher.__file__)
source.chmod(0o600)
identity = source.stat()
assert identity.st_uid == 0 and identity.st_nlink == 1
print("Installed watcher source is a private, independently copied file.")
PY
}
verify_installed_watcher
docker compose version
install -m 600 "$input/source-token" /run/recovery-fixture-source-token
mapfile -t selected < <(python3 - "$input" <<'PY'
import json
import pathlib
import sys

manifest = json.loads((pathlib.Path(sys.argv[1]) / "manifest.json").read_text())
for key in ("authorization_id", "manager_image", "proxy_image", "ready_source_sha", "scenario", "mutation_control"):
    print(manifest[key])
PY
)
driver_module=scripts.test_management_recovery_epoch_host
driver_prefix=()
case "${selected[5]}" in
    none) ;;
    omit-post-consumption-verifier)
        test "${selected[4]}" = post-receipt-ready-mutation
        .venv/bin/python -m scripts.recovery_epoch_mutation_control prepare \
            --source /opt/recovery-fixture \
            --destination /opt/recovery-mutation-control \
            --evidence "$output/mutation-source.json"
        cd /opt/recovery-mutation-control
        /opt/fixture-tools/bin/uv sync --locked --no-editable --no-dev --extra test --link-mode copy
        verify_installed_watcher
        driver_module=scripts.recovery_epoch_mutation_control
        driver_prefix=(run --evidence "$output/mutation-source.json" --)
        ;;
    *) echo 'Unsupported recovery mutation control.' >&2; exit 2 ;;
esac
fixture_uv_version=$(.venv/bin/python - <<'PY'
import subprocess
from auto_coder.management.recovery_epoch_authorization import validate_uv_version

banner = subprocess.check_output(["/opt/fixture-tools/bin/uv", "--version"], text=True).strip()
if not banner.startswith("uv "):
    raise ValueError("The selected fixture uv did not return its version banner")
version = validate_uv_version(banner.removeprefix("uv "))
if version.split(" ", 1)[0] != "0.13.6":
    raise ValueError("The installed fixture uv does not match the reviewed release pin")
print(version)
PY
)
printf 'uv %s\n' "$fixture_uv_version" | tee "$output/uv-version.txt"
git -C /opt/reviewed-source init
git -C /opt/reviewed-source remote add origin https://github.com/kitamura-tetsuo/auto-coder.git
git -C /opt/reviewed-source fetch --no-tags origin "${selected[3]}"
git -C /opt/reviewed-source checkout --detach FETCH_HEAD
test "$(git -C /opt/reviewed-source rev-parse --is-shallow-repository)" = false
docker image load --input "$input/images.tar"
docker image ls --no-trunc
for selected_image in "${selected[1]}" "${selected[2]}"; do
    printf 'Verifying imported fixture image %s\n' "$selected_image"
    test "$(docker image inspect --format '{{.Id}}' "$selected_image")" = "$selected_image"
done
python3 - "$input" <<'PY'
import json
import os
import pathlib
import subprocess
import sys

manifest = json.loads((pathlib.Path(sys.argv[1]) / "manifest.json").read_text())
marker = {
    "schema": "auto-coder-disposable-containment-host-v1",
    "authorization_id": manifest["authorization_id"],
    "machine_id": pathlib.Path("/etc/machine-id").read_text().strip(),
    "boot_id": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    "engine_id": subprocess.check_output(["docker", "info", "--format", "{{.ID}}"], text=True).strip(),
    "synthetic_only": True,
}
fd = os.open("/etc/auto-coder-disposable-containment-host.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    json.dump(marker, stream, sort_keys=True)
PY
export AUTOCODER_FRESH_TEST_BASE_IMAGE="${selected[1]}"
export AUTOCODER_RECOVERY_CADDY_IMAGE="${selected[2]}"
export PATH="/opt/fixture-tools/bin:$PATH"
.venv/bin/python -m "$driver_module" "${driver_prefix[@]}" \
    --authorization-id "${selected[0]}" \
    --source-repository /opt/reviewed-source \
    --source-token-file /run/recovery-fixture-source-token \
    --source-sha "${selected[3]}" \
    --scenario "${selected[4]}" \
    --uv /opt/fixture-tools/bin/uv --uv-version "$fixture_uv_version" \
    --output "$output/recovery-epoch-result.json"
