#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Only an operator with disposable-fixture authorization starts this service.
expected=$(python3 -c 'import json; print(json.load(open("/input/manifest.json"))["authorization_id"])')
if [[ -z "${APPROVED_AUTHORIZATION_ID:-}" || "$APPROVED_AUTHORIZATION_ID" != "$expected" ]]; then
    echo 'A matching approved disposable-fixture authorization is required.' >&2
    exit 2
fi
if [[ -e /disk/guest.qcow2 ]]; then
    echo 'A used guest disk cannot be reused as fresh acceptance evidence.' >&2
    exit 2
fi
cloud-localds /disk/seed.img /input/user-data /input/meta-data
qemu-img create -f qcow2 -F qcow2 -b /opt/base.img /disk/guest.qcow2 24G
set +e
timeout --signal=TERM --kill-after=30s 90m qemu-system-x86_64 \
    -accel kvm -cpu host -smp 2 -m 3072 -nodefaults -no-reboot \
    -display none -serial stdio -monitor none \
    -drive file=/disk/guest.qcow2,if=virtio,format=qcow2 \
    -drive file=/disk/seed.img,if=virtio,format=raw,readonly=on \
    -netdev user,id=public-bootstrap -device virtio-net-pci,netdev=public-bootstrap \
    -virtfs local,path=/input,mount_tag=fixture-input,security_model=none,readonly=on \
    -virtfs local,path=/output,mount_tag=fixture-output,security_model=none \
    2>&1 | tee /output/serial.log
qemu_status=${PIPESTATUS[0]}
set -e
if [[ "$qemu_status" != 0 || ! -f /output/guest-exit-code ]]; then
    echo "The guest did not complete; QEMU exit status: $qemu_status" >&2
    exit 1
fi
result=$(cat /output/guest-exit-code)
if [[ "$result" != 0 ]]; then
    echo "The guest acceptance driver failed with status $result." >&2
    exit 1
fi
