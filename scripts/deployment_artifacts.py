"""Validation gates used by release/beta artifact workflows."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

RELEASE_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
TAG_ABSENT_EXIT_CODE = 3


def require_equal(expected: str, actual: str, description: str) -> None:
    """Fail closed unless an authoritative value exactly matches a candidate."""
    if not expected or expected != actual:
        raise SystemExit(f"Refusing deployment: {description} mismatch ({expected!r} != {actual!r})")


def require_release_sha(value: str) -> None:
    """Reject values that cannot be an immutable full commit identifier."""
    if RELEASE_SHA_PATTERN.fullmatch(value) is None:
        raise SystemExit(f"Refusing deployment: invalid release commit SHA {value!r}")


def inspect_digest(reference: str) -> None:
    """Print a registry digest, distinguishing confirmed absence from errors."""
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", reference, "--format", "{{json .Manifest.Digest}}"],
        capture_output=True,
        text=True,
    )
    output = result.stdout.strip().strip('"')
    if result.returncode == 0 and DIGEST_PATTERN.fullmatch(output):
        print(output)
        return

    diagnostic = result.stderr.strip()
    # Buildx does not expose registry error codes separately. Only diagnostics
    # that explicitly identify this exact reference as missing are absence;
    # authentication, transport, and all unknown failures remain fatal.
    confirmed_absence = "manifest unknown" in diagnostic.lower() or re.search(
        rf"(?:^|\s){re.escape(reference)}:\s+not found(?:\s|$)",
        diagnostic,
        flags=re.IGNORECASE,
    )
    if result.returncode != 0 and confirmed_absence:
        print(f"Registry tag is confirmed absent: {reference}", file=sys.stderr)
        raise SystemExit(TAG_ABSENT_EXIT_CODE)
    detail = diagnostic or f"invalid digest output {result.stdout.strip()!r}"
    raise SystemExit(f"Refusing deployment: unable to inspect {reference}: {detail}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "check",
        choices=(
            "inspect-digest",
            "require-latest-successful-run",
            "require-tested-beta",
            "require-release-history",
            "require-release-postcondition",
            "require-release-sha",
        ),
    )
    parser.add_argument("candidate")
    parser.add_argument("authoritative", nargs="?")
    args = parser.parse_args()
    if args.check == "inspect-digest":
        inspect_digest(args.candidate)
        return
    if args.check == "require-release-sha":
        require_release_sha(args.candidate)
        return
    if args.authoritative is None:
        parser.error(f"{args.check} requires an authoritative value")
    descriptions = {
        "require-latest-successful-run": "latest successful beta run",
        "require-tested-beta": "tested beta digest",
        "require-release-history": "immutable release history digest",
        "require-release-postcondition": "release tag digest",
    }
    description = descriptions[args.check]
    require_equal(args.candidate, args.authoritative, description)


if __name__ == "__main__":
    main()
