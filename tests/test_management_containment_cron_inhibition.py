"""Protocol and real filesystem regressions for the finite stock-cron proof.

Synthetic cgroup files test readback/refusal rules only. Native service, PAM,
job and cgroup acceptance is owned by the separate real host fixture.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_coder.management import containment_cron_inhibition as cron
from auto_coder.management import containment_route_manifest as routes
from auto_coder.management import containment_route_sources as sources

pytestmark = pytest.mark.usefixtures("_use_real_commands")


@pytest.fixture
def native_sources() -> dict[str, str]:
    return json.loads((Path(__file__).parent / "fixtures/management-stock-cron-sources.json").read_text())


def _write_group(path: Path, *, processes: str = "", threads: str = "", populated: str = "0") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "cgroup.procs").write_text(processes)
    (path / "cgroup.threads").write_text(threads)
    (path / "cgroup.events").write_text("populated " + populated + "\nfrozen 0\n")


@pytest.fixture
def scope_tree(tmp_path: Path):
    root, proc, mountinfo = tmp_path / "cgroup", tmp_path / "proc", tmp_path / "mountinfo"
    _write_group(root)
    _write_group(root / "system.slice")
    proc.mkdir()
    info = root.stat()
    device = str(os.major(info.st_dev)) + ":" + str(os.minor(info.st_dev))
    mountinfo.write_text("42 30 " + device + " / " + str(root) + " rw - cgroup2 cgroup rw\n")
    return root, proc, mountinfo


def _scope(tree, units: tuple[str, ...] = ("cron.service",)) -> cron.CronCgroupScope:
    root, proc, mountinfo = tree
    return cron._cgroup_snapshot(root=root, proc=proc, mountinfo=mountinfo, inhibited_units=units)


def _stock_entry(proof: cron.CronInhibitedSource) -> routes.CronEntry:
    path = "/etc/cron.daily/apport"
    return routes.CronEntry(path, "regular", 1, 2, 3, 0o755, 0, cron.STOCK_INPUTS[path][1], True, inhibited_source=proof)


def _recovery_binding(entry: routes.CronEntry) -> tuple[routes.CronEntry, ...]:
    return routes._cron_approval_binding((entry,), recovery=True)


@contextmanager
def _approved_cron_routes(monkeypatch: pytest.MonkeyPatch, entry: routes.CronEntry, profile: str = "recovery"):
    from tests.test_management_containment_route_manifest import _closed_routes, _closed_v2_routes

    fixture_type = _closed_routes if profile == "v1" else _closed_v2_routes
    with fixture_type(monkeypatch) as fixture:
        fixture.facts["cron.service"] = routes.SystemdRouteFact("cron.service", "a" * 64, "masked", "inactive", 0, True)
        fixture.catalog = tuple(sorted(fixture.facts))
        if profile == "recovery":
            fixture.ids += ("f" * 64,)
            fixture.docker = tuple(SimpleNamespace(container_id=item) for item in fixture.ids)
            selected = fixture.approval.enrollment
            scope = routes.RecoveryRouteScope(selected.deployment_id, selected.enrollment_id, selected.authorization_id, "ordinary-recovery-v1", fixture.ids, selected.original_files)
            fixture.approval = routes.RecoveryRouteSelection(scope, fixture.approval.enrollment_sha256)
            fixture.manifest = replace(fixture.manifest, schema="approved-management-recovery-routes-v1", profile=scope.profile, docker_container_ids=fixture.ids)
        fixture.manifest = replace(fixture.manifest, cron_entries=(entry,), cron_restarter_paths=(entry.path,), systemd_routes=tuple(sorted((*fixture.manifest.systemd_routes, routes.SystemdRoute("cron.service", "a" * 64, "restarter")), key=lambda item: item.unit)))
        fixture.save()
        yield fixture


@pytest.fixture
def closed_proof(scope_tree) -> cron.CronInhibitedSource:
    properties = {name: "" for name in cron._PROPERTIES}
    properties.update(Id="cron.service", LoadState="masked", UnitFileState="masked", ActiveState="inactive", SubState="dead", MainPID="0", ControlPID="0", NeedDaemonReload="no", FragmentPath="/etc/systemd/system/cron.service", Delegate="no")
    files = [sources.CommandSourceFact(path, expected[0], "file", 1, 2, 3, stat.S_IFREG | 0o644, 0, expected[1], 1, 2, 3) for path, expected in cron._PROFILE_FILES.items()]
    files.append(sources.CommandSourceFact("/usr/sbin/cron", "/usr/sbin/cron", "file", 1, 2, 3, stat.S_IFREG | 0o755, 0, "c" * 64, 1, 2, 3))
    files.extend(sources.CommandSourceFact(path, path, "missing", 0, 0, 0, 0, 0, "") for path in cron._OTHER_EXECUTABLES)
    return cron.CronInhibitedSource(
        cron.PROFILE, "01234567-89ab-cdef-0123-456789abcdef", "cron.service", "a" * 64, tuple(sorted(properties.items())), tuple(sorted(files, key=lambda item: item.path)), "/etc/systemd/system/cron.service", (1, 2, 3, stat.S_IFLNK | 0o777, 0, 0), _scope(scope_tree), (("cron.service", "a" * 64),)
    )


def test_five_exact_native_stock_sources_are_the_only_opaque_inputs(native_sources: dict[str, str]) -> None:
    assert set(cron.STOCK_INPUTS) == {"/etc/cron.d/e2scrub_all", "/etc/crontab", "/etc/cron.daily/apport", "/etc/cron.daily/apt-compat", "/etc/cron.daily/dpkg"}
    for name, (kind, digest) in cron.STOCK_INPUTS.items():
        data = native_sources[name].encode()
        assert hashlib.sha256(data).hexdigest() == digest
        assert cron.exact_stock_source(Path(name), data, kind)
        assert not cron.exact_stock_source(Path(name), data + b"\n", kind)
        assert not cron.exact_stock_source(Path(name + ".copy"), data, kind)
        assert not cron.exact_stock_source(Path(name), data, "user-crontab")
    for name, (_, digest) in cron._PROFILE_FILES.items():
        assert hashlib.sha256(native_sources[name].encode()).hexdigest() == digest


def test_missing_cron_and_user_scopes_are_explicitly_bound(scope_tree) -> None:
    first = _scope(scope_tree)
    assert first.required_empty_roots == ("/system.slice/cron.service", "/user.slice")
    assert first.missing_roots == first.required_empty_roots
    assert tuple(item.path for item in first.directories) == ("/", "/system.slice")
    assert first.empty_groups == ()
    assert cron.CronCgroupScope.parse(json.loads(json.dumps(asdict(first)))) == first
    assert _scope(scope_tree) == first


@pytest.mark.parametrize("change", ("added", "removed", "recreated"))
def test_recovery_approval_allows_only_unrelated_directory_lifecycle(scope_tree, closed_proof, change: str) -> None:
    root, _, _ = scope_tree
    target = root / "system.slice" / ("docker-" + "a" * 64 + ".scope")
    if change != "added":
        _write_group(target, processes="1234\n", threads="1234\n", populated="1")
    approved = _stock_entry(replace(closed_proof, cgroups=_scope(scope_tree)))
    if change != "added":
        target.rename(root.parent / "retained-old-target-scope")
    if change != "removed":
        _write_group(target, processes="1234\n", threads="1234\n", populated="1")
    current_scope = _scope(scope_tree)
    assert _scope(scope_tree) == current_scope
    current = _stock_entry(replace(closed_proof, cgroups=current_scope))
    assert current != approved
    assert _recovery_binding(current) == _recovery_binding(approved)
    assert routes._cron_approval_binding((current,), recovery=False) != routes._cron_approval_binding((approved,), recovery=False)
    assert current.inhibited_source.cgroups == current_scope


@pytest.mark.parametrize("relative", ("", "system.slice", "system.slice/cron.service", "system.slice/cron.service/child", "other.slice", "other.slice/cron.service", "user.slice"))
def test_recovery_approval_retains_every_inhibited_directory_and_ancestor(scope_tree, closed_proof, relative: str) -> None:
    root, _, _ = scope_tree
    for path in ("system.slice/cron.service/child", "other.slice/cron.service", "user.slice"):
        _write_group(root / path)
    for path in ("system.slice/cron.service", "other.slice"):
        _write_group(root / path)
    approved = _stock_entry(replace(closed_proof, cgroups=_scope(scope_tree)))
    target = root / relative
    target.chmod(stat.S_IMODE(target.stat().st_mode) ^ stat.S_IXGRP)
    current = _stock_entry(replace(closed_proof, cgroups=_scope(scope_tree)))
    assert _recovery_binding(current) != _recovery_binding(approved)


@pytest.mark.parametrize("change", ("mount-id", "mount-device", "required-roots", "missing-roots", "empty-groups", "boot", "scheduler", "controller", "vendor", "mask", "inhibited-services", "cron-bytes", "cron-inode", "missing-proof"))
def test_recovery_approval_retains_other_scope_and_source_bindings(closed_proof, change: str) -> None:
    approved = _stock_entry(closed_proof)
    scope = closed_proof.cgroups
    if change == "mount-id":
        current = _stock_entry(replace(closed_proof, cgroups=replace(scope, mount_id=scope.mount_id + 1)))
    elif change == "mount-device":
        current = _stock_entry(replace(closed_proof, cgroups=replace(scope, mount_device="0:999")))
    elif change == "required-roots":
        current = _stock_entry(replace(closed_proof, cgroups=replace(scope, required_empty_roots=("/other.slice/cron.service", *scope.required_empty_roots))))
    elif change == "missing-roots":
        current = _stock_entry(replace(closed_proof, cgroups=replace(scope, missing_roots=())))
    elif change == "empty-groups":
        current = _stock_entry(replace(closed_proof, cgroups=replace(scope, empty_groups=(cron.EmptyCgroupFact("/user.slice", (), (), 0, 0),))))
    elif change == "boot":
        current = _stock_entry(replace(closed_proof, boot_id="00000000-0000-4000-8000-000000000001"))
    elif change == "scheduler":
        current = _stock_entry(replace(closed_proof, scheduler_sha256="b" * 64))
    elif change == "controller":
        current = _stock_entry(replace(closed_proof, controller_properties=tuple((key, "system.slice" if key == "Slice" else value) for key, value in closed_proof.controller_properties)))
    elif change == "vendor":
        current = _stock_entry(replace(closed_proof, sources=(replace(closed_proof.sources[0], inode=99), *closed_proof.sources[1:])))
    elif change == "mask":
        current = _stock_entry(replace(closed_proof, mask_identity=(1, 99, *closed_proof.mask_identity[2:])))
    elif change == "inhibited-services":
        current = _stock_entry(replace(closed_proof, inhibited_services=(("cron.service", "b" * 64),)))
    elif change == "cron-bytes":
        current = replace(approved, sha256="f" * 64)
    elif change == "cron-inode":
        current = replace(approved, inode=99)
    else:
        current = replace(approved, inhibited_source=None)
    assert _recovery_binding(current) != _recovery_binding(approved)


@pytest.mark.parametrize("profile", ("recovery", "v1", "v2"))
def test_only_recovery_route_reader_allows_unrelated_scope_and_keeps_full_facts(scope_tree, closed_proof, monkeypatch: pytest.MonkeyPatch, profile: str) -> None:
    approved = _stock_entry(closed_proof)
    with _approved_cron_routes(monkeypatch, approved, profile) as fixture:
        original = fixture.path.read_bytes()
        target = scope_tree[0] / "system.slice" / ("docker-" + "a" * 64 + ".scope")
        _write_group(target, processes="1234\n", threads="1234\n", populated="1")

        def read_catalog(**kwargs):
            return (_stock_entry(replace(closed_proof, cgroups=_scope(scope_tree))),)

        monkeypatch.setattr(routes, "_cron_catalog", read_catalog)
        if profile != "recovery":
            with pytest.raises(routes.RouteManifestUnavailable, match="changed cron/at route"):
                fixture.observe()
        else:
            observation = fixture.observe()
            assert observation.cron_entries == read_catalog()
            assert observation.cron_entries != observation.manifest.cron_entries
            assert "/system.slice/" + target.name in {item.path for item in observation.cron_entries[0].inhibited_source.cgroups.directories}
        assert fixture.path.read_bytes() == original


def test_recovery_fresh_route_repeat_still_refuses_unrelated_scope_change(scope_tree, closed_proof, monkeypatch: pytest.MonkeyPatch) -> None:
    approved = _stock_entry(closed_proof)
    with _approved_cron_routes(monkeypatch, approved) as fixture:
        calls = 0

        def read_catalog(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                _write_group(scope_tree[0] / "system.slice/docker-new.scope")
            return (_stock_entry(replace(closed_proof, cgroups=_scope(scope_tree))),)

        monkeypatch.setattr(routes, "_cron_catalog", read_catalog)
        with pytest.raises(routes.RouteManifestUnavailable, match="installed ordinary routes changed during observation"):
            fixture.observe()
        assert calls == 2


@pytest.mark.parametrize("state", ("leader", "thread", "nested", "event-only", "user-session", "alternative-slice"))
def test_masked_controller_cannot_hide_real_scope_population(scope_tree, state: str) -> None:
    root, _, _ = scope_tree
    assert _scope(scope_tree).missing_roots
    target = root / "system.slice/cron.service"
    if state == "nested":
        _write_group(target)
        target /= "job-child"
    elif state == "user-session":
        target = root / "user.slice/user-0.slice/session-7.scope"
        _write_group(root / "user.slice")
        _write_group(root / "user.slice/user-0.slice")
    elif state == "alternative-slice":
        target = root / "other.slice/cron.service"
        _write_group(root / "other.slice")
    _write_group(target, processes="1089\n" if state != "thread" and state != "event-only" else "", threads="1090\n" if state == "thread" else "", populated="1" if state == "event-only" else "0")
    with pytest.raises(cron.CronScopeRetainsTasks, match="inhibited stock cron scope retains tasks") as failure:
        _scope(scope_tree)
    expected_scope = "/user.slice" if state == "user-session" else "/other.slice/cron.service" if state == "alternative-slice" else "/system.slice/cron.service"
    assert failure.value.scope == expected_scope
    assert failure.value.public_reason == "inhibited stock cron scope retains tasks: " + expected_scope


def test_empty_nested_and_alternative_scopes_keep_all_directory_identities(scope_tree) -> None:
    root, _, _ = scope_tree
    for relative in ("system.slice/cron.service", "system.slice/cron.service/child", "other.slice", "other.slice/cron.service", "user.slice"):
        _write_group(root / relative)
    first = _scope(scope_tree)
    assert first.missing_roots == ()
    assert first.required_empty_roots == ("/other.slice/cron.service", "/system.slice/cron.service", "/user.slice")
    assert {item.path for item in first.empty_groups} == {"/other.slice/cron.service", "/system.slice/cron.service", "/system.slice/cron.service/child", "/user.slice"}
    assert cron.CronCgroupScope.parse(json.loads(json.dumps(asdict(first)))) == first


def test_proc_zombie_membership_cannot_become_an_empty_scope(scope_tree) -> None:
    _, proc, _ = scope_tree
    task = proc / "1089/task/1089"
    task.mkdir(parents=True)
    (task / "stat").write_text("1089 (job) Z " + "0 " * 18 + "777 0\n")
    (task / "cgroup").write_text("0::/system.slice/cron.service\n")
    with pytest.raises(cron.CronScopeRetainsTasks, match="state=Z start=777") as failure:
        _scope(scope_tree)
    assert failure.value.public_reason == "inhibited stock cron scope retains tasks: /system.slice/cron.service"


def test_deleted_or_unreadable_cgroup_authority_does_not_become_empty(scope_tree) -> None:
    root, _, _ = scope_tree
    target = root / "system.slice/cron.service"
    _write_group(target)
    (target / "cgroup.threads").unlink()
    with pytest.raises(OSError):
        _scope(scope_tree)


def test_changed_cgroup_directory_identity_refuses(scope_tree, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _, _ = scope_tree
    target = root / "system.slice/cron.service"
    _write_group(target)
    read = cron._empty_group

    def replace_after_empty_read(path: Path, relative: str, **kwargs):
        result = read(path, relative, **kwargs)
        target.rename(root / "retained-old-group")
        _write_group(target)
        return result

    monkeypatch.setattr(cron, "_empty_group", replace_after_empty_read)
    with pytest.raises(cron.CronInhibitionUnavailable, match="identity changed"):
        _scope(scope_tree)


@pytest.mark.parametrize("mount", ("cgroup", "tmpfs"))
def test_unified_mount_authority_is_required(scope_tree, mount: str) -> None:
    _, _, mountinfo = scope_tree
    mountinfo.write_text(mountinfo.read_text().replace(" - cgroup2 ", " - " + mount + " "))
    with pytest.raises(cron.CronInhibitionUnavailable, match="unified cgroup2"):
        _scope(scope_tree)


@pytest.mark.parametrize("kind", ("missing", "enabled", "other-scheduler", "reload"))
def test_real_inhibited_installed_scheduler_is_not_a_caller_boolean(closed_proof, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    unit = routes.SystemdRouteFact("cron.service", "a" * 64, "masked", "inactive", 0, True)
    facts = (unit,)
    if kind == "missing":
        facts = ()
    elif kind == "enabled":
        facts = (replace(unit, inhibited=False),)
    elif kind == "reload":
        facts = (replace(unit, needs_daemon_reload=True),)
    else:
        facts += (replace(unit, unit="atd.service"),)
    monkeypatch.setattr(cron, "_snapshot", lambda digest, services: pytest.fail("invalid controller reached kernel profile"))
    with pytest.raises(cron.CronInhibitionUnavailable, match="real inhibited installed scheduler"):
        cron.observe_inhibited_stock_cron_scope(facts)


def test_profile_reobserves_complete_scope_and_rejects_races(closed_proof, monkeypatch: pytest.MonkeyPatch) -> None:
    unit = routes.SystemdRouteFact("cron.service", "a" * 64, "masked", "inactive", 0, True)
    values = [closed_proof, closed_proof]
    monkeypatch.setattr(cron, "_snapshot", lambda digest, services: values.pop(0))
    assert cron.observe_inhibited_stock_cron_scope((unit,)) == closed_proof
    assert not values
    values.extend((closed_proof, replace(closed_proof, mask_identity=(1, 99, 3, stat.S_IFLNK | 0o777, 0, 0))))
    with pytest.raises(cron.CronInhibitionUnavailable, match="changed during observation"):
        cron.observe_inhibited_stock_cron_scope((unit,))


def _write_proc_task(proc: Path, *, pid: str = "1089", tid: str = "1089", group: str = "/system.slice/unrelated.service") -> Path:
    task = proc / pid / "task" / tid
    task.mkdir(parents=True)
    (task / "stat").write_text(tid + " (fixture) S " + "0 " * 18 + "777 0\n")
    (task / "cgroup").write_text("0::" + group + "\n")
    if not (proc / pid / "cmdline").exists():
        (proc / pid / "cmdline").write_bytes(b"fixture\0")
    if not (proc / pid / "exe").is_symlink():
        (proc / pid / "exe").symlink_to("/usr/bin/sleep")
    return task


def _filesystem_cron_observer(scope_tree, closed_proof, monkeypatch: pytest.MonkeyPatch):
    """Keep the real paired observer and filesystem scope reader in this component test."""
    root, proc, mountinfo = scope_tree
    state = SimpleNamespace(source=cron._CronSourceBinding.from_observation(closed_proof), source_values=[], source_reads=0, scope_reads=0, completed=[], before_scope=lambda: None)
    read_scope = cron._cgroup_snapshot

    def source():
        state.source_reads += 1
        return state.source_values.pop(0) if state.source_values else state.source

    def scope(*, inhibited_units):
        state.scope_reads += 1
        state.before_scope()
        value = read_scope(root=root, proc=proc, mountinfo=mountinfo, inhibited_units=inhibited_units)
        state.completed.append(value)
        return value

    monkeypatch.setattr(cron, "_source_snapshot", source)
    monkeypatch.setattr(cron, "_cgroup_snapshot", scope)
    state.facts = (routes.SystemdRouteFact("cron.service", "a" * 64, "masked", "inactive", 0, True),)
    return state


@pytest.mark.parametrize("incomplete_snapshot", (1, 2))
def test_disappearing_process_discards_the_entire_observation_pair(scope_tree, closed_proof, monkeypatch: pytest.MonkeyPatch, incomplete_snapshot: int) -> None:
    root, proc, _ = scope_tree
    _write_proc_task(proc)
    state = _filesystem_cron_observer(scope_tree, closed_proof, monkeypatch)
    enumerate_directory = Path.iterdir
    injected = False

    def disappear(path):
        nonlocal injected
        if path == proc / "1089/task" and state.scope_reads == incomplete_snapshot and not injected:
            injected = True
            shutil.rmtree(proc / "1089")
            _write_group(root / "system.slice/after-exit.scope")
        return enumerate_directory(path)

    monkeypatch.setattr(Path, "iterdir", disappear)
    observed = cron.observe_inhibited_stock_cron_scope(state.facts)
    assert injected and state.source_reads == state.scope_reads == incomplete_snapshot + 2
    assert state.completed[-2] == state.completed[-1] == observed.cgroups
    assert "/system.slice/after-exit.scope" in {item.path for item in observed.cgroups.directories}
    if incomplete_snapshot == 2:
        assert state.completed[0] != observed.cgroups


def test_disappearing_nonleader_is_reobserved_without_skipping_other_tasks(scope_tree, closed_proof, monkeypatch: pytest.MonkeyPatch) -> None:
    _, proc, _ = scope_tree
    _write_proc_task(proc)
    thread = _write_proc_task(proc, tid="1090")
    state = _filesystem_cron_observer(scope_tree, closed_proof, monkeypatch)
    read = cron._read
    injected = False

    def disappear(path, limit=1024 * 1024):
        nonlocal injected
        if path == thread / "stat" and not injected:
            injected = True
            shutil.rmtree(thread)
        return read(path, limit)

    monkeypatch.setattr(cron, "_read", disappear)
    observed = cron.observe_inhibited_stock_cron_scope(state.facts)
    assert injected and state.source_reads == state.scope_reads == 3
    assert observed.cgroups == state.completed[-2] == state.completed[-1]
    assert (proc / "1089/task/1089/stat").is_file()


def test_repeated_process_churn_has_a_finite_complete_observation_budget(scope_tree, closed_proof, monkeypatch: pytest.MonkeyPatch) -> None:
    _, proc, _ = scope_tree
    state = _filesystem_cron_observer(scope_tree, closed_proof, monkeypatch)
    state.before_scope = lambda: _write_proc_task(proc)
    enumerate_directory = Path.iterdir

    def disappear(path):
        if path == proc / "1089/task":
            shutil.rmtree(proc / "1089")
        return enumerate_directory(path)

    monkeypatch.setattr(Path, "iterdir", disappear)
    with pytest.raises(cron.CronInhibitionUnavailable, match="inventory did not stabilize"):
        cron.observe_inhibited_stock_cron_scope(state.facts)
    assert state.source_reads == state.scope_reads == 3
    assert not state.completed


@pytest.mark.parametrize("fault", ("task-directory", "empty-tasks", "stat", "cgroup", "nonleader-stat", "nonleader-cgroup", "cmdline", "exe", "malformed-stat", "malformed-cgroup", "permission", "eperm"))
def test_live_missing_or_invalid_process_evidence_is_not_retried(scope_tree, closed_proof, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    _, proc, _ = scope_tree
    task = _write_proc_task(proc)
    if fault.startswith("nonleader-"):
        task = _write_proc_task(proc, tid="1090")
    state = _filesystem_cron_observer(scope_tree, closed_proof, monkeypatch)
    if fault == "task-directory":
        shutil.rmtree(task.parent)
    elif fault == "empty-tasks":
        shutil.rmtree(task)
    elif fault in {"stat", "cgroup", "nonleader-stat", "nonleader-cgroup"}:
        (task / fault.removeprefix("nonleader-")).unlink()
    elif fault in {"cmdline", "exe"}:
        (proc / "1089" / fault).unlink()
    elif fault.startswith("malformed-"):
        (task / fault.removeprefix("malformed-")).write_text("unavailable\n")
    else:
        read = cron._read

        def denied(path, limit=1024 * 1024):
            if path == task / "stat":
                raise PermissionError(1 if fault == "eperm" else 13, "fixture task is inaccessible")
            return read(path, limit)

        monkeypatch.setattr(cron, "_read", denied)
    with pytest.raises(cron.CronInhibitionUnavailable):
        cron.observe_inhibited_stock_cron_scope(state.facts)
    assert state.source_reads == state.scope_reads == 1


@pytest.mark.parametrize("kind", ("inhibited-membership", "scheduler"))
def test_positively_observed_inhibited_tasks_are_never_retried(scope_tree, closed_proof, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    _, proc, _ = scope_tree
    _write_proc_task(proc, group="/system.slice/cron.service" if kind == "inhibited-membership" else "/system.slice/unrelated.service")
    if kind == "scheduler":
        (proc / "1089/exe").unlink()
        (proc / "1089/exe").symlink_to("/usr/sbin/cron")
    state = _filesystem_cron_observer(scope_tree, closed_proof, monkeypatch)
    with pytest.raises(cron.CronScopeRetainsTasks):
        cron.observe_inhibited_stock_cron_scope(state.facts)
    assert state.source_reads == state.scope_reads == 1


def test_new_nonleader_in_an_inhibited_scope_is_found_by_the_fresh_complete_scan(scope_tree, closed_proof, monkeypatch: pytest.MonkeyPatch) -> None:
    _, proc, _ = scope_tree
    _write_proc_task(proc)
    state = _filesystem_cron_observer(scope_tree, closed_proof, monkeypatch)
    enumerate_directory = Path.iterdir
    task_reads = 0

    def add_thread(path):
        nonlocal task_reads
        if path == proc / "1089/task":
            task_reads += 1
            if task_reads == 2:
                _write_proc_task(proc, tid="1090", group="/system.slice/cron.service")
        return enumerate_directory(path)

    monkeypatch.setattr(Path, "iterdir", add_thread)
    with pytest.raises(cron.CronScopeRetainsTasks, match="tid=1090"):
        cron.observe_inhibited_stock_cron_scope(state.facts)
    assert state.source_reads == state.scope_reads == 2
    assert not state.completed


@pytest.mark.parametrize("field", ("boot_id", "controller_properties", "sources", "mask_path", "mask_identity"))
def test_changed_source_binding_cannot_be_hidden_by_a_second_snapshot_disappearance(scope_tree, closed_proof, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    _, proc, _ = scope_tree
    _write_proc_task(proc)
    state = _filesystem_cron_observer(scope_tree, closed_proof, monkeypatch)
    changes = {
        "boot_id": "11111111-89ab-cdef-0123-456789abcdef",
        "controller_properties": tuple((key, "changed" if key == "FragmentPath" else value) for key, value in state.source.controller_properties),
        "sources": (replace(state.source.sources[0], inode=99), *state.source.sources[1:]),
        "mask_path": "/run/systemd/system/cron.service",
        "mask_identity": (1, 99, 3, stat.S_IFLNK | 0o777, 0, 0),
    }
    state.source_values = [state.source, replace(state.source, **{field: changes[field]})]
    enumerate_directory = Path.iterdir

    def disappear(path):
        if path == proc / "1089/task" and state.scope_reads == 2:
            shutil.rmtree(proc / "1089")
        return enumerate_directory(path)

    monkeypatch.setattr(Path, "iterdir", disappear)
    with pytest.raises(cron.CronInhibitionUnavailable, match="source/controller authority changed"):
        cron.observe_inhibited_stock_cron_scope(state.facts)
    assert state.source_reads == state.scope_reads == 2
    assert len(state.completed) == 1


def test_first_incomplete_snapshot_retains_its_protected_source_refusal_anchor(scope_tree, closed_proof, monkeypatch: pytest.MonkeyPatch) -> None:
    _, proc, _ = scope_tree
    _write_proc_task(proc)
    state = _filesystem_cron_observer(scope_tree, closed_proof, monkeypatch)
    state.source_values = [state.source, replace(state.source, mask_identity=(1, 99, 3, stat.S_IFLNK | 0o777, 0, 0))]
    enumerate_directory = Path.iterdir

    def disappear(path):
        if path == proc / "1089/task" and state.scope_reads == 1:
            shutil.rmtree(proc / "1089")
        return enumerate_directory(path)

    monkeypatch.setattr(Path, "iterdir", disappear)
    with pytest.raises(cron.CronInhibitionUnavailable, match="source/controller authority changed"):
        cron.observe_inhibited_stock_cron_scope(state.facts)
    assert state.source_reads == state.scope_reads == 2
    assert len(state.completed) == 1


def test_vendor_source_read_binds_actual_protected_file_and_refuses_change(native_sources: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory(prefix="ac-cron-vendor-", dir=pwd.getpwuid(os.geteuid()).pw_dir) as directory:
        path = Path(directory) / "cron.service"
        original = native_sources["/usr/lib/systemd/system/cron.service"]
        path.write_text(original)
        path.chmod(0o600)
        digest = hashlib.sha256(original.encode()).hexdigest()
        first = cron._profile_source(path, expected=(str(path), digest), uid=os.geteuid())
        assert first.sha256 == digest
        assert first.inode == path.stat().st_ino
        path.write_text(original.replace("KillMode=process", "KillMode=control-group"))
        with pytest.raises(cron.CronInhibitionUnavailable, match="profile differs"):
            cron._profile_source(path, expected=(str(path), digest), uid=os.geteuid())
        path.write_text(original)
        path.chmod(0o666)
        with pytest.raises(ValueError, match="writable"):
            cron._profile_source(path, expected=(str(path), digest), uid=os.geteuid())


def test_typed_proof_survives_json_and_rejects_missing_scope_or_changed_pam(closed_proof) -> None:
    saved = json.loads(json.dumps(asdict(closed_proof)))
    assert cron.CronInhibitedSource.parse(saved) == closed_proof
    saved["cgroups"]["required_empty_roots"].remove("/user.slice")
    with pytest.raises(cron.CronInhibitionUnavailable):
        cron.CronInhibitedSource.parse(saved)
    saved = json.loads(json.dumps(asdict(closed_proof)))
    next(item for item in saved["sources"] if item["path"] == "/etc/pam.d/cron")["sha256"] = "f" * 64
    with pytest.raises(cron.CronInhibitionUnavailable, match="PAM source profile"):
        cron.CronInhibitedSource.parse(saved)


@pytest.mark.parametrize("stock_path", tuple(sorted(cron.STOCK_INPUTS)))
def test_exact_stock_entry_keeps_raw_identity_and_explicit_restarter_proof(tmp_path: Path, native_sources, closed_proof, monkeypatch: pytest.MonkeyPatch, stock_path: str) -> None:
    path = tmp_path / "stock-source"
    data = native_sources[stock_path]
    kind = cron.STOCK_INPUTS[stock_path][0]
    path.write_text(data)
    path.chmod(0o600)
    digest = hashlib.sha256(data.encode()).hexdigest()
    monkeypatch.setitem(cron.STOCK_INPUTS, str(path), (kind, digest))
    calls = []

    def observe(facts):
        calls.append(facts)
        return closed_proof

    monkeypatch.setattr(routes, "observe_inhibited_stock_cron_scope", observe)
    unit = routes.SystemdRouteFact("cron.service", "a" * 64, "masked", "inactive", 0, True)
    kwargs = {"roots": (path,), "inspect_local_inputs": True, "trusted_uid": os.geteuid(), "source_formats": ((path, kind),), "systemd_facts": (unit,)}
    first = routes._cron_catalog(**kwargs)[0]
    assert first.sha256 == digest
    assert first.inode == path.stat().st_ino
    assert first.management_candidate is True
    assert first.inhibited_source == closed_proof
    assert first.command_sources == ()
    assert routes.CronEntry.parse(json.loads(json.dumps(asdict(first)))) == first
    assert calls == [(unit,)]
    path.chmod(0o666)
    with pytest.raises(routes.RouteManifestUnavailable, match="owner-protected"):
        routes._cron_catalog(**kwargs)
    path.chmod(0o600)
    extra_command = "\n* * * * * root maintenance\n" if kind == "system-crontab" else "\nmaintenance\n"
    path.write_text(data + extra_command)
    with pytest.raises(sources.CommandSourceUnavailable):
        routes._cron_catalog(**kwargs)
    assert calls == [(unit,)]


def test_stock_profile_does_not_change_direct_script_caller_inspection(tmp_path: Path, native_sources) -> None:
    path = tmp_path / "ordinary-call.sh"
    path.write_text(native_sources["/etc/cron.daily/apport"])
    with pytest.raises(sources.CommandSourceUnavailable, match="dispatch"):
        sources.observe_command_sources(str(path), uid=os.geteuid(), cues=cron.CRON_ROUTE_CUES)


def test_run_parts_roots_and_resolved_scheduler_aliases_remain_relevant(tmp_path: Path) -> None:
    approval = SimpleNamespace(enrollment=SimpleNamespace(original_files=(), management_id="a" * 64, proxy_id="b" * 64))
    cues = routes._deployment_cues(approval, ())
    assert set(cron.CRON_ROUTE_CUES) <= set(cues)
    assert sources.observe_command_sources("/usr/bin/run-parts /etc/cron.daily", uid=os.geteuid(), cues=cues)[1] is True
    alias = tmp_path / "scheduler-link"
    alias.symlink_to("/usr/bin/true")
    resolved = str(Path("/usr/bin/true").resolve())
    facts, candidate = sources.observe_command_sources(str(alias), uid=os.geteuid(), cues=(resolved,))
    assert candidate is True
    assert facts[0].resolved == resolved


def test_unapproved_draft_derives_scheduler_role_from_fresh_stock_proof(closed_proof, monkeypatch: pytest.MonkeyPatch) -> None:
    from auto_coder.management.containment_user_routes import UserControllerInventory

    unit = routes.SystemdRouteFact("cron.service", "a" * 64, "masked", "inactive", 0, True, False)
    path = "/etc/cron.daily/apport"
    entry = routes.CronEntry(path, "regular", 1, 2, 3, 0o755, 0, cron.STOCK_INPUTS[path][1], True, inhibited_source=closed_proof)
    enrollment = SimpleNamespace(deployment_id="fixture", enrollment_id="fixture", authorization_id="fixture", profile="ordinary-v1", management_id="a" * 64, proxy_id="b" * 64, original_files=())
    approval = SimpleNamespace(enrollment=enrollment, enrollment_sha256="c" * 64)
    monkeypatch.setattr(routes, "_deployment_root_bindings", lambda *args, **kwargs: ())
    monkeypatch.setattr(routes, "_systemd_catalog", lambda: ("cron.service",))
    monkeypatch.setattr(routes, "_systemd_unit", lambda *args, **kwargs: unit)
    monkeypatch.setattr(routes, "_user_controllers", lambda *args, **kwargs: (UserControllerInventory((), (), (), (), ()), ()))
    calls = []

    def read_catalog(**kwargs):
        calls.append(kwargs["systemd_facts"])
        return (entry,)

    monkeypatch.setattr(routes, "_cron_catalog", read_catalog)
    draft = routes.propose_unapproved_route_draft(approval, (), closed_profile=True)
    assert calls == [(unit,)]
    assert draft["complete_ordinary_routes_attested"] is False
    assert draft["systemd_routes"][0]["role"] == "restarter"
    assert draft["cron_restarter_paths"] == [path]
    assert routes.CronEntry.parse(json.loads(json.dumps(draft["cron_entries"][0]))) == entry


@pytest.mark.parametrize("parent,nested", (("system.slice", False), ("other.slice", False), ("other.slice", True)))
def test_masked_alternative_service_binds_scope_and_refuses_retained_job(scope_tree, closed_proof, monkeypatch: pytest.MonkeyPatch, parent: str, nested: bool) -> None:
    root, _, _ = scope_tree
    scheduler = routes.SystemdRouteFact("cron.service", "a" * 64, "masked", "inactive", 0, True)
    alternative = replace(scheduler, unit="ac-other-scheduler.service", sha256="b" * 64, management_candidate=False)
    facts = (alternative, scheduler)
    _write_group(root / parent)
    controller = root / parent / alternative.unit
    _write_group(controller)
    target = controller
    if nested:
        target /= "retained-job"
        _write_group(target)
    calls = []

    def read_snapshot(digest, services):
        calls.append(services)
        return replace(closed_proof, scheduler_sha256=digest, inhibited_services=services, cgroups=_scope(scope_tree, tuple(unit for unit, _ in services)))

    monkeypatch.setattr(cron, "_snapshot", read_snapshot)
    positive = cron.observe_inhibited_stock_cron_scope(facts)
    assert positive.inhibited_services == ((alternative.unit, alternative.sha256), (scheduler.unit, scheduler.sha256))
    assert calls == [positive.inhibited_services, positive.inhibited_services]
    assert "/system.slice/" + alternative.unit in positive.cgroups.required_empty_roots
    assert "/" + parent + "/" + alternative.unit in positive.cgroups.required_empty_roots
    assert cron.CronInhibitedSource.parse(json.loads(json.dumps(asdict(positive)))) == positive
    changed_fact = replace(alternative, sha256="d" * 64)
    changed = cron.observe_inhibited_stock_cron_scope((changed_fact, scheduler))
    assert changed.inhibited_services != positive.inhibited_services
    assert changed.cgroups == positive.cgroups
    assert changed.sources == positive.sources

    # The daemon has exited and both controller facts stay masked/PID0. Its
    # existing child still makes both fresh observations refuse this profile.
    _write_group(target, processes="1089\n", threads="1089\n", populated="1")
    for _ in range(2):
        with pytest.raises(cron.CronScopeRetainsTasks, match="inhibited stock cron scope retains tasks") as failure:
            cron.observe_inhibited_stock_cron_scope(facts)
        assert failure.value.public_reason == "inhibited stock cron scope retains tasks: /" + parent + "/" + alternative.unit


def test_alternative_service_mapping_uses_exact_components(scope_tree) -> None:
    root, _, _ = scope_tree
    unit = "ac-other-scheduler.service"
    units = (unit, "cron.service")
    _write_group(root / "other.slice")
    for name in ("prefix-" + unit, unit + "-suffix"):
        _write_group(root / "other.slice" / name, processes="1089\n", threads="1089\n", populated="1")
    observed = _scope(scope_tree, units)
    assert observed.required_empty_roots == ("/system.slice/" + unit, "/system.slice/cron.service", "/user.slice")
    assert observed.missing_roots == observed.required_empty_roots
    assert cron.CronCgroupScope.parse(json.loads(json.dumps(asdict(observed))), inhibited_units=units) == observed


@pytest.mark.parametrize("unit", ("foo@.service", "foo@bar.service", r"foo\x2dbar.service", "_foo.service", ".foo.service", "cgroup.foo.service", "cpu.service", "memory.extra.service", "bpf-socket-bind.service"))
def test_unsupported_masked_service_names_refuse_before_inhibited_filter(monkeypatch: pytest.MonkeyPatch, unit: str) -> None:
    scheduler = routes.SystemdRouteFact("cron.service", "a" * 64, "masked", "inactive", 0, True)
    # Templates already have inhibited=False in the real unit reader. That
    # cannot silently discard a masked controller's potential instance scope.
    alternative = replace(scheduler, unit=unit, inhibited=False)
    monkeypatch.setattr(cron, "_snapshot", lambda *args: pytest.fail("unsupported mapping reached kernel profile"))
    with pytest.raises(cron.CronInhibitionUnavailable, match="cgroup mapping is unsupported"):
        cron.observe_inhibited_stock_cron_scope((scheduler, alternative))


def test_typed_service_set_cannot_omit_scope_or_change_scheduler_binding(scope_tree, closed_proof) -> None:
    units = ("ac-other-scheduler.service", "cron.service")
    proof = replace(closed_proof, inhibited_services=((units[0], "b" * 64), (units[1], "a" * 64)), cgroups=_scope(scope_tree, units))
    value = json.loads(json.dumps(asdict(proof)))
    assert cron.CronInhibitedSource.parse(value) == proof
    value["inhibited_services"].pop(0)
    with pytest.raises(cron.CronInhibitionUnavailable, match="required execution scopes"):
        cron.CronInhibitedSource.parse(value)
    value = json.loads(json.dumps(asdict(proof)))
    value["inhibited_services"][1][1] = "f" * 64
    with pytest.raises(cron.CronInhibitionUnavailable, match="scheduler binding differs"):
        cron.CronInhibitedSource.parse(value)
    value = json.loads(json.dumps(asdict(proof)))
    value["inhibited_services"].append(value["inhibited_services"][0])
    with pytest.raises(cron.CronInhibitionUnavailable, match="cgroup mapping is unsupported"):
        cron.CronInhibitedSource.parse(value)


def test_proc_alternative_controller_child_refuses_when_cgroup_files_look_empty(scope_tree) -> None:
    _, proc, _ = scope_tree
    task = proc / "1089/task/1089"
    task.mkdir(parents=True)
    (task / "stat").write_text("1089 (job) Z " + "0 " * 18 + "777 0\n")
    (task / "cgroup").write_text("0::/other.slice/ac-other-scheduler.service/retained-job\n")
    with pytest.raises(cron.CronScopeRetainsTasks, match="state=Z start=777") as failure:
        _scope(scope_tree, ("ac-other-scheduler.service", "cron.service"))
    assert failure.value.public_reason == "inhibited stock cron scope retains tasks: /other.slice/ac-other-scheduler.service"


@pytest.mark.parametrize("scope", (None, "/private/ENV_SECRET_TOKEN/cron.service", "/system.slice/cron.service\nENV_SECRET_TOKEN", "/system.slice/cpu.service", "/system.slice/foo@.service", "/" + "s" * 513 + ".slice/cron.service"))
def test_public_task_reason_does_not_export_unvalidated_scope_or_raw_detail(scope: str | None) -> None:
    error = cron.CronScopeRetainsTasks("private command/environment diagnostic ENV_SECRET_TOKEN", scope=scope)
    assert error.public_reason == "inhibited stock cron scope retains tasks"
