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
        systemctl cat docker.service docker.socket containerd.service
        systemctl show docker.service containerd.service -p ExecStart -p PrivateMounts -p MainPID -p ActiveState -p LoadState -p UnitFileState -p FragmentPath -p DropInPaths
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
# This fresh VM has no previous engine. Prevent package auto-start before the
# selected ordinary service profile exists; no original history exists yet.
test ! -e /usr/bin/dockerd
test ! -e /run/docker.pid
test ! -e /var/lib/docker
for fixture_unit in docker.service docker.socket containerd.service; do
    test ! -e "/etc/systemd/system/$fixture_unit"
    test ! -L "/etc/systemd/system/$fixture_unit"
    ln -s /dev/null "/etc/systemd/system/$fixture_unit"
done
systemctl daemon-reload
apt-get update
apt-get install -y --no-install-recommends ca-certificates cron curl docker.io docker-compose-v2 git python3-venv
mkdir -p /etc/systemd/system/docker.service.d
test ! -e /etc/systemd/system/docker.service.d/recovery-fixture.conf
tee /etc/systemd/system/docker.service.d/recovery-fixture.conf > /dev/null <<'UNIT'
[Service]
ExecStart=
ExecStart=/usr/bin/dockerd -H fd://
PrivateMounts=yes
UNIT
# Docker starts its normal managed containerd process in this same namespace.
# A standalone containerd in a different mount namespace could not see its
# overlay/netns objects. Keep that unused packaged service masked throughout.
for fixture_unit in docker.service docker.socket; do
    test "$(readlink "/etc/systemd/system/$fixture_unit")" = /dev/null
    rm "/etc/systemd/system/$fixture_unit"
done
systemctl daemon-reload
systemctl start docker
python3 - "$output/docker-profile.json" <<'PY'
import json
import os
from pathlib import Path
import subprocess
import sys


def properties(unit):
    names = ("MainPID", "ActiveState", "LoadState", "PrivateMounts")
    raw = subprocess.check_output(["systemctl", "show", unit, "--property=" + ",".join(names)], text=True)
    result = dict(line.split("=", 1) for line in raw.splitlines())
    assert set(result) == set(names)
    return result


def process(pid):
    proc = Path("/proc") / str(pid)
    fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
    assert fields[0] not in ("Z", "X", "x") and int(fields[19]) > 0
    return {
        "pid": pid,
        "parent": int(fields[1]),
        "start": fields[19],
        "namespaces": {name: os.readlink(proc / "ns" / name) for name in ("pid", "user", "net", "mnt")},
    }


service, standalone = properties("docker.service"), properties("containerd.service")
assert service["ActiveState"] == "active" and service["PrivateMounts"] == "yes"
assert standalone["LoadState"] == "masked" and standalone["ActiveState"] == "inactive" and standalone["MainPID"] == "0"
pid = int(service["MainPID"])
assert pid > 1 and Path("/run/docker.pid").read_text().strip() == str(pid)
assert os.path.samefile(f"/proc/{pid}/exe", "/usr/bin/dockerd")
assert Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") == [b"/usr/bin/dockerd", b"-H", b"fd://", b""]
daemon = process(pid)
host = process(1)
assert all(daemon["namespaces"][name] == host["namespaces"][name] for name in ("pid", "user", "net"))
assert daemon["namespaces"]["mnt"] != host["namespaces"]["mnt"]
children = {int(value) for task in Path(f"/proc/{pid}/task").iterdir() for value in (task / "children").read_text().split()}
managed = [child for child in children if os.path.samefile(f"/proc/{child}/exe", "/usr/bin/containerd")]
assert len(managed) == 1
containerd = process(managed[0])
assert containerd["parent"] == pid and containerd["namespaces"] == daemon["namespaces"]
assert subprocess.check_output(["docker", "info", "--format", "{{.CgroupDriver}}"], text=True).strip() == "systemd"
assert subprocess.check_output(["docker", "ps", "--all", "--quiet"], text=True).strip() == ""
assert process(pid) == daemon and process(managed[0]) == containerd
result = {"schema": "auto-coder-recovery-vm-docker-profile-v1", "profile": "ordinary-system-docker-managed-containerd-private-mounts", "daemon": daemon, "managed_containerd": containerd, "standalone_service": standalone}
with Path(sys.argv[1]).open("x") as stream:
    json.dump(result, stream, sort_keys=True)
print("Verified ordinary Docker with its managed containerd in one private mount namespace before original fixture creation.", flush=True)
PY
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
.venv/bin/python - "$output/maintenance-profile.json" <<'PY'
import json
import os
from dataclasses import asdict
from pathlib import Path
import subprocess
import sys

from auto_coder.logger_config import get_logger, setup_logger
from auto_coder.management.containment_route_manifest import _systemd_unit

MAINTENANCE_UNITS = ("systemd-tmpfiles-clean.timer", "systemd-tmpfiles-clean.service")
MASK_ROOT = Path("/etc/systemd/system")
logger = get_logger(__name__)


def inhibit_maintenance(output):
    """Settle only this fresh VM's measured maintenance pair before originals."""
    evidence = {"schema": "auto-coder-recovery-vm-maintenance-profile-v1", "status": "preparing"}
    try:
        masks = tuple(MASK_ROOT / unit for unit in MAINTENANCE_UNITS)
        if any(path.exists() or path.is_symlink() for path in masks):
            raise ValueError("The fresh maintenance mask paths are not empty")
        initial = tuple(_systemd_unit(unit) for unit in MAINTENANCE_UNITS)
        evidence["initial"] = [asdict(fact) for fact in initial]
        if any(not fact.unit_file_state or fact.active_state not in {"active", "inactive"} or fact.needs_daemon_reload for fact in initial):
            raise ValueError("The original installed maintenance pair is unavailable")
        # Stop the timer first, then wait for its service to stop. Never reset
        # failed units or change an already approved recovery environment.
        for unit in MAINTENANCE_UNITS:
            subprocess.run(("systemctl", "stop", "--", unit), check=True, timeout=30)
        stopped = tuple(_systemd_unit(unit) for unit in MAINTENANCE_UNITS)
        evidence["stopped"] = [asdict(fact) for fact in stopped]
        if any(fact.active_state != "inactive" or fact.main_pid != 0 or fact.needs_daemon_reload for fact in stopped):
            raise ValueError("The actual maintenance pair did not stop")
        subprocess.run(("systemctl", "mask", "--", *MAINTENANCE_UNITS), check=True, timeout=30)
        subprocess.run(("systemctl", "daemon-reload"), check=True, timeout=30)
        observed = tuple(_systemd_unit(unit) for unit in MAINTENANCE_UNITS)
        evidence["observed"] = [asdict(fact) for fact in observed]
        if any(not fact.inhibited or fact.unit_file_state != "masked" or fact.active_state != "inactive" or fact.main_pid != 0 or fact.needs_daemon_reload for fact in observed):
            raise ValueError("The actual maintenance pair is not masked and inactive")
        evidence["masks"] = []
        for path in masks:
            info = path.lstat()
            if not path.is_symlink() or os.readlink(path) != "/dev/null":
                raise ValueError("The maintenance pair lacks its actual /dev/null masks")
            evidence["masks"].append({"unit": path.name, "path": str(path), "device": info.st_dev, "inode": info.st_ino, "ctime_ns": info.st_ctime_ns, "mode": info.st_mode, "uid": info.st_uid, "target": os.readlink(path)})
        if tuple(_systemd_unit(unit) for unit in MAINTENANCE_UNITS) != observed:
            raise ValueError("The inhibited maintenance pair changed during verification")
        evidence["status"] = "inhibited"
        logger.info("Verified the fixed maintenance timer/service pair is masked and inactive before original fixture creation")
    except Exception as error:
        evidence.update(status="failed", failure_type=type(error).__name__)
        logger.error("Fresh recovery VM maintenance preparation failed: {}", type(error).__name__)
        raise
    finally:
        with output.open("x") as stream:
            json.dump(evidence, stream, sort_keys=True)


if __name__ == "__main__":
    output = Path(sys.argv[1])
    setup_logger(stream=sys.stdout, log_file=str(output.with_suffix(".log")))
    inhibit_maintenance(output)
    logger.complete()
PY
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
