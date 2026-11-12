#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Export exact reviewed source/images; preparation does not launch a guest.
if [[ "$#" != 6 || ! "$1" =~ ^/var/tmp/ac-recovery-vm-[a-zA-Z0-9_-]+$ || ! "$2" =~ ^[a-zA-Z0-9_.:-]{1,128}$ || ! "$3" =~ ^sha256:[0-9a-f]{64}$ || ! "$4" =~ ^sha256:[0-9a-f]{64}$ || ! "$6" =~ ^[a-z][a-z-]{1,63}$ ]]; then
    echo 'Usage: prepare.sh /var/tmp/ac-recovery-vm-UNIQUE AUTHORIZATION_ID MANAGER_IMAGE_ID CADDY_IMAGE_ID READ_TOKEN_FILE SCENARIO' >&2
    exit 2
fi
fixture_root=$1
fixture_authorization=$2
manager_image=$3
proxy_image=$4
source_token_file=$5
fixture_scenario=$6
tooling_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_root=$(git -C "$tooling_root" rev-parse --show-toplevel)
cd -- "$repo_root"
"$repo_root/.venv/bin/python" - "$fixture_scenario" <<'PY'
import sys
from scripts.test_management_recovery_epoch_host import required_cases

required_cases(sys.argv[1])
PY
fixture_accelerator=tcg
fixture_kvm_gid=0
fixture_compose_files="$tooling_root/compose.yaml"
if [[ -c /dev/kvm ]]; then
    fixture_accelerator=kvm
    fixture_kvm_gid=$(stat -c '%g' /dev/kvm)
    fixture_compose_files="$fixture_compose_files:$tooling_root/compose.kvm.yaml"
fi
test -f "$source_token_file"
if [[ -e "$fixture_root" || -L "$fixture_root" ]]; then
    echo 'The recovery fixture directory must not exist.' >&2
    exit 2
fi
if [[ -n "$(git -C "$repo_root" status --porcelain --untracked-files=normal)" ]]; then
    echo 'Commit reviewed source before exporting the recovery VM inputs.' >&2
    exit 2
fi
for selected in "$manager_image" "$proxy_image"; do
    test "$(docker image inspect --format '{{.Id}}' "$selected")" = "$selected"
done
mkdir -m 700 "$fixture_root" "$fixture_root/input" "$fixture_root/output" "$fixture_root/disk"
exec > >(tee -a "$fixture_root/output/preparation.log") 2>&1
git -C "$repo_root" archive --format=tar.gz HEAD | tee "$fixture_root/input/source.tar.gz" > /dev/null
docker image save "$manager_image" "$proxy_image" | tee "$fixture_root/input/images.tar" > /dev/null
cp "$tooling_root/guest.sh" "$fixture_root/input/guest.sh"
install -m 600 "$source_token_file" "$fixture_root/input/source-token"
fixture_commit=$(git -C "$repo_root" rev-parse HEAD)
python3 - "$fixture_root" "$fixture_authorization" "$fixture_commit" "$manager_image" "$proxy_image" "$fixture_scenario" "$fixture_accelerator" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
inputs = root / "input"
manifest = dict(zip(("authorization_id", "commit", "manager_image", "proxy_image", "scenario", "accelerator"), sys.argv[2:]))
manifest["schema"] = "auto-coder-recovery-vm-input-v1"
manifest["ready_source_sha"] = "e742338b3f311e6d30a22a8f5341b8072513d813"
manifest["inputs_sha256"] = {}
for name in ("source.tar.gz", "images.tar", "guest.sh"):
    with (inputs / name).open("rb") as stream:
        manifest["inputs_sha256"][name] = hashlib.file_digest(stream, "sha256").hexdigest()
(inputs / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
(inputs / "meta-data").write_text("instance-id: " + manifest["authorization_id"] + "\nlocal-hostname: ac-recovery-fixture\n")
PY
tee "$fixture_root/input/user-data" >/dev/null <<'YAML'
#cloud-config
users: []
disable_root: true
ssh_pwauth: false
runcmd:
  - [mkdir, -p, /mnt/fixture-input, /mnt/fixture-output]
  - [mount, -t, 9p, -o, "trans=virtio,version=9p2000.L,ro", fixture-input, /mnt/fixture-input]
  - [mount, -t, 9p, -o, "trans=virtio,version=9p2000.L", fixture-output, /mnt/fixture-output]
  - [bash, /mnt/fixture-input/guest.sh]
power_state:
  mode: poweroff
  delay: now
  timeout: 30
  condition: true
YAML
tee "$fixture_root/compose.env" >/dev/null <<ENV
RECOVERY_VM_TOOLING=$tooling_root
RECOVERY_VM_ROOT=$fixture_root
RECOVERY_VM_UID=$(id -u)
RECOVERY_VM_GID=$(id -g)
RECOVERY_KVM_GID=$fixture_kvm_gid
COMPOSE_FILE=$fixture_compose_files
ENV
printf 'Prepared exact recovery source %s in %s with QEMU %s.\n' "$fixture_commit" "$fixture_root" "$fixture_accelerator"
