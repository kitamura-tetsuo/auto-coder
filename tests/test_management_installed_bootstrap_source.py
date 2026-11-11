"""Unit protocol tests for the reviewed ordinary bootstrap source component.

The fixture bytes are not original deployment evidence or positive acceptance.
"""

from __future__ import annotations

import hashlib
import io
import tarfile

import pytest

from auto_coder.management import installed_bootstrap_source as source
from auto_coder.management.containment_enrollment import ApprovedEnrollment, EnrollmentObservation, OriginalFile


def _record(digest: str) -> EnrollmentObservation:
    original = OriginalFile("bootstrap-source", "/private/fixture-source", digest, 1, 2)
    enrollment = ApprovedEnrollment(
        "approved-current-containment-enrollment-v1",
        "fixture-deployment",
        "fixture-enrollment",
        "fixture-authorization",
        "ordinary-v1",
        "fixture-engine",
        "a" * 64,
        "sha256:" + "a" * 64,
        "b" * 64,
        "sha256:" + "b" * 64,
        (original,),
        "/private/previous-evidence",
        "0" * 64,
        True,
    )
    return EnrollmentObservation(enrollment, "0" * 64, 1, 2, 3, ((original.purpose, digest),), "0" * 64)


def _archive(content: bytes, *, name: str = "bootstrap_supervisor.py", kind: bytes | None = None) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:") as archive:
        entry = tarfile.TarInfo(name)
        entry.size = len(content)
        if kind is not None:
            entry.type = kind
        archive.addfile(entry, io.BytesIO(content))
    return output.getvalue()


def test_exact_pinned_source_is_rechecked_twice_without_original_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = b"fixture ordinary bootstrap source with an owner lock"
    digest = hashlib.sha256(fixture).hexdigest()
    monkeypatch.setattr(source, "_REVIEWED_ORDINARY_SOURCE_SHA256", digest)
    calls = []

    def copy(identifier: str) -> bytes:
        calls.append(identifier)
        return fixture

    monkeypatch.setattr(source, "_copy_source", copy)
    observed = source.inspect_installed_bootstrap_source(_record(digest))
    assert calls == ["a" * 64, "a" * 64]
    assert observed.management_id == "a" * 64
    assert observed.source_sha256 == digest
    assert observed.source_bytes == len(fixture)
    assert observed.reviewed_lock_before_launch is True


def test_unreviewed_operator_source_cannot_select_a_new_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source, "_copy_source", lambda _: pytest.fail("No Docker copy permitted for unreviewed source"))
    with pytest.raises(ValueError, match="differs from reviewed ordinary-v1 bytes"):
        source.inspect_installed_bootstrap_source(_record("0" * 64))


def test_source_change_between_reads_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = b"fixture source"
    digest = hashlib.sha256(fixture).hexdigest()
    monkeypatch.setattr(source, "_REVIEWED_ORDINARY_SOURCE_SHA256", digest)
    values = iter((fixture, fixture + b" altered"))
    monkeypatch.setattr(source, "_copy_source", lambda _: next(values))
    with pytest.raises(ValueError, match="differs from the pinned reviewed bytes"):
        source.inspect_installed_bootstrap_source(_record(digest))


@pytest.mark.parametrize("name,kind", [("elsewhere.py", None), ("bootstrap_supervisor.py", tarfile.SYMTYPE)])
def test_archive_refuses_substituted_member_or_link(monkeypatch: pytest.MonkeyPatch, name: str, kind: bytes | None) -> None:
    archive = _archive(b"source", name=name, kind=kind)

    def run(*_args: object, **_kwargs: object) -> object:
        from subprocess import CompletedProcess

        return CompletedProcess((), 0, archive)

    monkeypatch.setattr(source.subprocess, "run", run)
    with pytest.raises(ValueError, match="archive is ambiguous"):
        source._copy_source("a" * 64)


def test_archive_exact_regular_file_is_read_without_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    content = b"fixture-source"
    archive = _archive(content)

    def run(*_args: object, **_kwargs: object) -> object:
        from subprocess import CompletedProcess

        return CompletedProcess((), 0, archive)

    monkeypatch.setattr(source.subprocess, "run", run)
    assert source._copy_source("a" * 64) == content


def test_remote_docker_context_refuses_before_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://192.0.2.1:2375")
    monkeypatch.setattr(source.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("Remote Docker must not be queried"))
    with pytest.raises(ValueError, match="remote Docker endpoint is unsupported"):
        source._copy_source("a" * 64)
