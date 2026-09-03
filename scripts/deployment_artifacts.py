"""Validation gates used by release/beta artifact workflows."""

from __future__ import annotations

import argparse


def require_equal(expected: str, actual: str, description: str) -> None:
    """Fail closed unless an authoritative value exactly matches a candidate."""
    if not expected or expected != actual:
        raise SystemExit(f"Refusing deployment: {description} mismatch ({expected!r} != {actual!r})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("check", choices=("require-latest-successful-run", "require-tested-beta"))
    parser.add_argument("candidate")
    parser.add_argument("authoritative")
    args = parser.parse_args()
    description = "latest successful beta run" if args.check == "require-latest-successful-run" else "tested beta digest"
    require_equal(args.candidate, args.authoritative, description)


if __name__ == "__main__":
    main()
