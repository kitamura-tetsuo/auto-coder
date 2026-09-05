"""Validation gates used by release/beta artifact workflows."""

from __future__ import annotations

import argparse
import re


RELEASE_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


def require_equal(expected: str, actual: str, description: str) -> None:
    """Fail closed unless an authoritative value exactly matches a candidate."""
    if not expected or expected != actual:
        raise SystemExit(f"Refusing deployment: {description} mismatch ({expected!r} != {actual!r})")


def require_release_sha(value: str) -> None:
    """Reject values that cannot be an immutable full commit identifier."""
    if RELEASE_SHA_PATTERN.fullmatch(value) is None:
        raise SystemExit(f"Refusing deployment: invalid release commit SHA {value!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "check",
        choices=(
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
