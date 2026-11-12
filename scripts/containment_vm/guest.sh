#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
input=/mnt/fixture-input
output=/mnt/fixture-output
exec > >(tee -a "$output/guest.log") 2>&1

finish() {
    result=$?
    trap - EXIT
    set +e
    python3 - "$output" <<'PY'
import json
import pathlib
import shutil
import sys

output = pathlib.Path(sys.argv[1])
results = pathlib.Path("/var/lib/auto-coder/containment-closed-host-results")
for path in sorted(results.glob("*.json")):
    shutil.copy2(path, output / path.name)
    index = json.loads(path.read_text())
    for number, source in enumerate(index.get("artifact_paths", [])):
        artifact = pathlib.Path(source)
        root = pathlib.Path("/var/lib/auto-coder/containment-readback-fixtures")
        if not artifact.is_file() or not artifact.resolve().is_relative_to(root) or artifact.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Fixture artifact is outside the bounded diagnostic output")
        shutil.copy2(artifact, output / (str(number) + "-" + artifact.name))
PY
    if [[ "$?" != 0 ]]; then
        result=1
    fi
    printf '%s\n' "$result" > "$output/guest-exit-code"
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
if manifest.get("schema") != "auto-coder-containment-vm-input-v1":
    raise ValueError("Unsupported fixture input")
for name, digest in manifest["inputs_sha256"].items():
    with (root / name).open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError("Fixture input changed: " + name)
PY
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates cron curl docker.io git python3-venv
systemctl start docker
python3 -m venv /opt/fixture-tools
/opt/fixture-tools/bin/pip install uv==0.13.6
mkdir -m 700 /opt/containment-fixture /opt/reviewed-original
tar --extract --gzip --file "$input/source.tar.gz" --directory /opt/containment-fixture
cd /opt/containment-fixture
/opt/fixture-tools/bin/uv sync --locked --no-editable --no-dev
.venv/bin/python - <<'PY'
from pathlib import Path
import auto_coder.management.proxy_watch_cli as watcher

Path(watcher.__file__).chmod(0o600)
PY
git -C /opt/reviewed-original init
git -C /opt/reviewed-original remote add origin https://github.com/kitamura-tetsuo/auto-coder.git
git -C /opt/reviewed-original fetch --no-tags origin 1b23f3be30ca47926462e7d261ef98e79bb82728
git -C /opt/reviewed-original checkout --detach FETCH_HEAD
test "$(git -C /opt/reviewed-original rev-parse --is-shallow-repository)" = false
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
mapfile -t selected < <(python3 - "$input" <<'PY'
import json
import pathlib
import sys

manifest = json.loads((pathlib.Path(sys.argv[1]) / "manifest.json").read_text())
for key in ("authorization_id", "ordinary_image", "quarantine_image", "proxy_image"):
    print(manifest[key])
PY
)
.venv/bin/python -m scripts.test_management_containment_closed_host \
    --authorization-id "${selected[0]}" --ordinary-image "${selected[1]}" \
    --quarantine-image "${selected[2]}" --caddy-image "${selected[3]}" \
    --source-repository /opt/reviewed-original
