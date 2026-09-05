"""Validation gates used by release/beta artifact workflows."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

RELEASE_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
TAG_ABSENT_EXIT_CODE = 3
MANIFEST_ACCEPT = ", ".join(("application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json", "application/vnd.docker.distribution.manifest.v2+json"))


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
    output = result.stdout.strip()
    if result.returncode == 0:
        try:
            digest = json.loads(output)
        except json.JSONDecodeError:
            digest = None
        if isinstance(digest, str) and DIGEST_PATTERN.fullmatch(digest):
            print(digest)
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


def create_history_if_absent(target: str, source_digest: str) -> None:
    """Atomically create a registry tag using the HTTP conditional-write contract."""
    registry, remainder = target.split("/", 1)
    repository, tag = remainder.rsplit(":", 1)
    base = os.environ.get("REGISTRY_API_BASE", f"https://{registry}")
    username = os.environ.get("REGISTRY_USERNAME", "")
    password = os.environ.get("REGISTRY_PASSWORD", "")
    bearer_token = ""

    def request(method: str, reference: str, data: bytes | None = None, extra: dict[str, str] | None = None):
        nonlocal bearer_token
        url = f"{base}/v2/{repository}/manifests/{reference}"
        headers = {"Accept": MANIFEST_ACCEPT, **(extra or {})}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        elif username or password:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        registry_request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            return urllib.request.urlopen(registry_request)
        except urllib.error.HTTPError as error:
            challenge = error.headers.get("WWW-Authenticate", "")
            if error.code != 401 or not challenge.lower().startswith("bearer ") or bearer_token:
                raise
            parameters = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
            token_url = parameters.pop("realm") + "?" + urllib.parse.urlencode(parameters)
            token_headers = {"Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()}
            with urllib.request.urlopen(urllib.request.Request(token_url, headers=token_headers)) as token_response:
                bearer_token = json.loads(token_response.read())["token"]
            headers["Authorization"] = f"Bearer {bearer_token}"
            return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method))

    try:
        with request("GET", source_digest) as response:
            manifest = response.read()
            content_type = response.headers.get_content_type()
        with request("PUT", tag, manifest, {"Content-Type": content_type, "If-None-Match": "*"}):
            return
    except urllib.error.HTTPError as error:
        if error.code == 412:
            return
        raise SystemExit(f"Refusing deployment: atomic history creation failed for {target}: HTTP {error.code}") from error
    except (OSError, ValueError) as error:
        raise SystemExit(f"Refusing deployment: atomic history creation failed for {target}: {error}") from error


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "check",
        choices=(
            "inspect-digest",
            "create-history-if-absent",
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
    if args.check == "create-history-if-absent":
        if args.authoritative is None:
            parser.error("create-history-if-absent requires a source digest")
        create_history_if_absent(args.candidate, args.authoritative)
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
