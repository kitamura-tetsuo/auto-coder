"""Protocol and real filesystem regressions for the finite stock-cron proof.

Synthetic cgroup files test readback/refusal rules only. Native service, PAM,
job and cgroup acceptance is owned by the separate real host fixture.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import stat
import tempfile
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
    with pytest.raises(cron.CronInhibitionUnavailable, match="inhibited stock cron scope retains tasks"):
        _scope(scope_tree)


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
    with pytest.raises(cron.CronInhibitionUnavailable, match="state=Z start=777"):
        _scope(scope_tree)


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

    def replace_after_empty_read(path: Path, relative: str):
        result = read(path, relative)
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
        with pytest.raises(cron.CronInhibitionUnavailable, match="inhibited stock cron scope retains tasks"):
            cron.observe_inhibited_stock_cron_scope(facts)


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
    with pytest.raises(cron.CronInhibitionUnavailable, match="state=Z start=777"):
        _scope(scope_tree, ("ac-other-scheduler.service", "cron.service"))
