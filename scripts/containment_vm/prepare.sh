#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Preparation exports images and source, but never launches QEMU or a container.
if [[ "$#" != 2 || ! "$1" =~ ^/var/tmp/ac-pr2829-vm-[a-zA-Z0-9_-]+$ || ! "$2" =~ ^[a-zA-Z0-9_.:-]{1,128}$ ]]; then
    echo 'Usage: bash prepare.sh /var/tmp/ac-pr2829-vm-UNIQUE AUTHORIZATION_ID' >&2
    exit 2
fi
fixture_root=$1
fixture_authorization=$2
tooling_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_root=$(git -C "$tooling_root" rev-parse --show-toplevel)
test -c /dev/kvm
if [[ -e "$fixture_root" || -L "$fixture_root" ]]; then
    echo 'The fixture directory must not exist.' >&2
    exit 2
fi
if [[ -n "$(git -C "$repo_root" status --porcelain --untracked-files=normal)" ]]; then
    echo 'Commit reviewed source before exporting the VM input archive.' >&2
    exit 2
fi
ordinary_image=sha256:adf8635ad6fccf60a0565408c0e9d7f209746763f01bc9144fbde7649acf3459
quarantine_image=sha256:bf88d2ea40f894d4bbc308be1694d8a1ed5dcf2d890a8606c4a6f6376ab5c750
proxy_image=sha256:504a44d7d74bd2bc28a66afc4774912fc21223ca8a67734af1c54ab2f999b489
for selected in "$ordinary_image" "$quarantine_image" "$proxy_image"; do
    test "$(docker image inspect --format '{{.Id}}' "$selected")" = "$selected"
done
mkdir -m 700 "$fixture_root" "$fixture_root/input" "$fixture_root/output" "$fixture_root/disk"
exec > >(tee -a "$fixture_root/output/preparation.log") 2>&1
git -C "$repo_root" archive --format=tar.gz HEAD > "$fixture_root/input/source.tar.gz"
docker image save "$ordinary_image" "$quarantine_image" "$proxy_image" > "$fixture_root/input/images.tar"
cp "$tooling_root/guest.sh" "$fixture_root/input/guest.sh"
fixture_commit=$(git -C "$repo_root" rev-parse HEAD)
python3 - "$fixture_root" "$fixture_authorization" "$fixture_commit" "$ordinary_image" "$quarantine_image" "$proxy_image" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
inputs = root / "input"
manifest = dict(zip(("authorization_id", "commit", "ordinary_image", "quarantine_image", "proxy_image"), sys.argv[2:]))
manifest["schema"] = "auto-coder-containment-vm-input-v1"
manifest["inputs_sha256"] = {}
for name in ("source.tar.gz", "images.tar", "guest.sh"):
    with (inputs / name).open("rb") as stream:
        manifest["inputs_sha256"][name] = hashlib.file_digest(stream, "sha256").hexdigest()
(inputs / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
(inputs / "meta-data").write_text("instance-id: " + manifest["authorization_id"] + "\nlocal-hostname: ac-containment-fixture\n")
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
CONTAINMENT_VM_TOOLING=$tooling_root
CONTAINMENT_VM_ROOT=$fixture_root
CONTAINMENT_VM_UID=$(id -u)
CONTAINMENT_VM_GID=$(id -g)
CONTAINMENT_KVM_GID=$(stat -c '%g' /dev/kvm)
ENV
printf 'Prepared source %s in %s. Launch requires matching disposable-fixture authorization.\n' "$fixture_commit" "$fixture_root"
