#!/usr/bin/env python3
"""Report a deterministic build identity for an explicit source checkout."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_coder.build_identity import (  # noqa: E402
    BuildIdentityError,
    resolve_build_identity,
    serialize_build_identity,
    write_build_identity,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="source checkout to identify")
    parser.add_argument("--output", type=Path, required=True, help="external or ignored JSON output path")
    args = parser.parse_args()

    record = resolve_build_identity(args.repo)
    try:
        write_build_identity(record, args.repo, args.output)
    except (BuildIdentityError, OSError) as exc:
        print(f"Failed to write build identity: {exc}", file=sys.stderr)
        return 2
    print(serialize_build_identity(record), end="")
    return 0 if record.state in ("clean", "dirty") else 1


if __name__ == "__main__":
    raise SystemExit(main())
