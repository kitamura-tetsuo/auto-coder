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
apt-get update
apt-get install -y --no-install-recommends ca-certificates cron curl docker.io git python3-venv
systemctl start docker
python3 -m venv /opt/fixture-tools
/opt/fixture-tools/bin/pip install uv==0.13.6
mkdir -m 700 /opt/recovery-fixture /opt/reviewed-source
tar --extract --gzip --file "$input/source.tar.gz" --directory /opt/recovery-fixture
cd /opt/recovery-fixture
/opt/fixture-tools/bin/uv sync --locked --no-editable --no-dev --extra test
install -m 600 "$input/source-token" /run/recovery-fixture-source-token
mapfile -t selected < <(python3 - "$input" <<'PY'
import json
import pathlib
import sys

manifest = json.loads((pathlib.Path(sys.argv[1]) / "manifest.json").read_text())
for key in ("authorization_id", "manager_image", "proxy_image", "ready_source_sha", "scenario"):
    print(manifest[key])
PY
)
git -C /opt/reviewed-source init
git -C /opt/reviewed-source remote add origin https://github.com/kitamura-tetsuo/auto-coder.git
git -C /opt/reviewed-source fetch --no-tags origin "${selected[3]}"
git -C /opt/reviewed-source checkout --detach FETCH_HEAD
test "$(git -C /opt/reviewed-source rev-parse --is-shallow-repository)" = false
docker image load --input "$input/images.tar"
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
.venv/bin/python -m scripts.test_management_recovery_epoch_host \
    --authorization-id "${selected[0]}" \
    --source-repository /opt/reviewed-source \
    --source-token-file /run/recovery-fixture-source-token \
    --source-sha "${selected[3]}" \
    --scenario "${selected[4]}" \
    --uv /opt/fixture-tools/bin/uv --uv-version 0.13.6 \
    --output "$output/recovery-epoch-result.json"
