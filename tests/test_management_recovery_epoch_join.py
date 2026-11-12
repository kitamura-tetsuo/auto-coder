"""Authority-join component contracts, not original or live host authority.

The lineage and containment dataclasses below are explicit component values.
Their agreement tests the issuer's cross-producer joins, not their provenance.
Consumer-scope tests additionally use real protected files and held kernel FDs;
no fixture result substitutes for production Docker or current T observations.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import pwd
import stat
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterator

import pytest

from auto_coder.management.containment_enrollment import OriginalNetworkPin
from auto_coder.management.containment_observation import DockerObservation
from auto_coder.management.containment_route_manifest import ApprovedRouteManifest, DeploymentRootBinding, RouteManifestObservation
from auto_coder.management.original_attempt_evidence import OriginalGeneration, OriginalLineageEvidence
from auto_coder.management.original_attempts import HistoryBinding, canonical, source_identity
from auto_coder.management.original_genesis import GenesisAnchor, GenesisRoot
from auto_coder.management.recovery_containment import RecoveryContainmentFacts, RecoveryContainmentReport, RecoveryTargetFact
from auto_coder.management.recovery_containment_model import NamedDescriptor, RecoveryEnrollment, RecoveryEnrollmentObservation, RecoveryFile, RecoveryLock, RecoveryOriginal, RecoveryResources, RecoveryTarget
from auto_coder.management.recovery_containment_profile import RecoveryManagerFact
from auto_coder.management.recovery_containment_resources import RecoveryResourceObservation
from auto_coder.management.recovery_epoch import _check_consumer_scope, join_lineage
from auto_coder.management.recovery_epoch_authorization import PEER_ROOT, EpochAuthorization
from auto_coder.management.recovery_epoch_consumer import ConsumerConfiguration, OriginalFence, TargetOwner, TransferredResource
from auto_coder.management.recovery_epoch_peer import HeldFence
from auto_coder.management.recovery_epoch_resources import EpochResources
from auto_coder.management.recovery_epoch_state import EpochInventory, EpochUnavailable, issuer_lifetime

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")
_BOOT = "00000000-0000-4000-8000-000000000001"
_IMAGE = "sha256:" + "a" * 64
_OAUTH = "/component/oauth"


def component_file(path: str, inode: int) -> RecoveryFile:
    return RecoveryFile(path, format(inode, "064x"), 1, inode)


def component_generation(index: int, source: str, *, never: bool = False) -> tuple[OriginalGeneration, RecoveryOriginal, RecoveryManagerFact]:
    manager, proxy = format(2 * index + 1, "064x"), format(2 * index + 2, "064x")
    base = "/component/generation-" + str(index)
    state, journal = base + "/state", base + "/journal"
    bootstrap, management = component_file(base + "/config.json", 100 + index), component_file(base + "/management.yaml", 200 + index)
    owner = None if never else RecoveryLock(state + "/.supervisor-owner.lock", 1, 300 + index)
    mounts = (
        DeploymentRootBinding(manager, "/management-child-state", state, True, 1, 400 + index, stat.S_IFDIR | 0o700),
        DeploymentRootBinding(manager, "/management-journal", journal, True, 1, 500 + index, stat.S_IFDIR | 0o700),
        DeploymentRootBinding(manager, "/management-oauth", _OAUTH, True, 1, 600, stat.S_IFDIR | 0o700),
    )
    generation = OriginalGeneration(
        f"00000000-0000-4000-8000-{index + 10:012d}",
        f"instance-{index}",
        manager,
        _IMAGE,
        bootstrap.sha256,
        management.sha256,
        state,
        1,
        400 + index,
        journal,
        1,
        500 + index,
        _OAUTH,
        1,
        600,
        owner.path if owner else "",
        owner.device if owner else 0,
        owner.inode if owner else 0,
        "never-created" if never else "unknown",
        (),
        proxy if index == 0 else "",
        _IMAGE if index == 0 else "",
    )
    original = RecoveryOriginal(
        f"generation-{index}",
        generation.instance_id,
        manager,
        _IMAGE,
        proxy,
        _IMAGE,
        "never-started" if never else "started",
        "" if never else _BOOT,
        bootstrap,
        management,
        component_file(base + "/Caddyfile", 700 + index),
        mounts,
        owner,
        None if never else component_file(base + "/prior.json", 800 + index),
        None if never else OriginalNetworkPin(42, "12345", _BOOT, 10 + index, f"net:[{900 + index}]", 4, 900 + index),
    )
    fact = RecoveryManagerFact(original.generation_id, generation.instance_id, manager, _IMAGE, original.state, source, "b" * 64, bootstrap.sha256, management.sha256, state, journal, _OAUTH, owner, 18080 + index, mounts)
    return generation, original, fact


def component_docker(identifier: str) -> DockerObservation:
    return DockerObservation("engine-one", identifier, _IMAGE, "c" * 64, "no", 0, False, "exited", 0, "old-start", "old-stop", 0, "d" * 64)


@dataclass(frozen=True)
class JoinCase:
    authorization: EpochAuthorization
    inventory: EpochInventory
    enrollment: RecoveryEnrollmentObservation
    lineage: OriginalLineageEvidence
    containment: RecoveryContainmentReport

    def check(self) -> None:
        join_lineage(self.authorization, self.inventory, self.enrollment, self.lineage, self.containment)

    def scope(self, selected: RecoveryEnrollment) -> JoinCase:
        enrollment = replace(self.enrollment, enrollment=selected)
        return replace(self, enrollment=enrollment, containment=replace(self.containment, facts=replace(self.containment.facts, enrollment=enrollment)))

    def managers(self, values: tuple[RecoveryManagerFact, ...]) -> JoinCase:
        return replace(self, containment=replace(self.containment, facts=replace(self.containment.facts, managers=values)))


def component_join() -> JoinCase:
    source = source_identity()
    history = HistoryBinding("/component/history", "deployment-one", "history-one", source)
    first, second, absent = (component_generation(index, source, never=index == 2) for index in range(3))
    target_id = "f" * 64
    target_roots = (
        DeploymentRootBinding(target_id, "/management-child-state", "/component/target/state", True, 1, 1001, stat.S_IFDIR | 0o700),
        DeploymentRootBinding(target_id, "/management-journal", "/component/target/journal", True, 1, 1002, stat.S_IFDIR | 0o700),
        DeploymentRootBinding(target_id, "/management-oauth", _OAUTH, True, 1, 600, stat.S_IFDIR | 0o700),
    )
    target = RecoveryTarget("target-one", target_id, _IMAGE, component_file("/component/target/config.json", 1101), component_file("/component/target/management.yaml", 1102), target_roots, RecoveryLock("/component/target/state/.supervisor-owner.lock", 1, 1103), 18090)
    selected = RecoveryEnrollment("approved-management-recovery-containment-v1", history.deployment, "enrollment-one", "authorization-one", "ordinary-recovery-v1", "engine-one", source, (first[1], second[1]), target, (), True)
    enrollment = RecoveryEnrollmentObservation(selected, "1" * 64, 1, 1200, 100)
    genesis = GenesisAnchor("ordinary-original-genesis-v1", history, "old-authorization", first[0].instance_id, "engine-one", first[0].management_id, _IMAGE, "2" * 64, first[1].proxy_id, _IMAGE, "3" * 64, (), (GenesisRoot("oauth", _OAUTH, "/management-oauth", 1, 600),), "{}")
    lineage = OriginalLineageEvidence(history, genesis=genesis, frontier="4" * 64, generations=(first[0], second[0], absent[0]))
    target_fact = RecoveryManagerFact(
        "target", target.instance_id, target_id, _IMAGE, "prelaunch-target", source, "5" * 64, target.bootstrap_config.sha256, target.management_config.sha256, "/component/target/state", "/component/target/journal", _OAUTH, target.owner, target.published_port, target_roots
    )
    docker = tuple(component_docker(identifier) for identifier in selected.container_ids)
    manifest = ApprovedRouteManifest("approved-management-recovery-routes-v1", selected.deployment_id, selected.enrollment_id, selected.authorization_id, selected.profile, enrollment.sha256, True, True, (), selected.container_ids, (), (), (), ())
    routes = RouteManifestObservation(manifest, "6" * 64, 1, 1300, 100, (), (), (), selected.container_ids)
    facts = RecoveryContainmentFacts(
        enrollment,
        source,
        docker,
        (first[2], second[2], target_fact),
        (),
        (),
        RecoveryTargetFact(target_id, 101, "12345", _BOOT, "net:[1400]", "pid:[1401]", "/component/target-cgroup", 1, 1402, 1, target.owner.inode, target.published_port),
        RecoveryResourceObservation(102, "12346", _BOOT, ()),
        routes,
        (),
        (),
    )
    containment = RecoveryContainmentReport("current-recovery-containment-v1", "observed-current-recovery-containment", "00000000-0000-4000-8000-000000000050", 102, "12346", _BOOT, 100, 200, facts, "7" * 64)
    authorization = EpochAuthorization(
        "management-recovery-authorization-v1",
        selected.deployment_id,
        selected.authorization_id,
        "new-request",
        "new-preparation",
        target_id,
        selected.enrollment_id,
        history,
        component_file("/component/target/consumer.json", 1500),
        component_file("/component/target/READY.json", 1501),
        "/component/source",
        "/component/source-token",
        "/usr/bin/python3",
        "/usr/bin/uv",
        "0.13.6",
        "a" * 40,
        1,
        1600,
    )
    inventory = EpochInventory(
        2, "/var/lib/auto-coder/recovery-epoch", 1, 1700, 1, 1701, selected.deployment_id, selected.authorization_id, (selected.enrollment_id,), authorization.request_id, authorization.preparation_id, target_id, source.removeprefix("sha256:"), target.bootstrap_config.sha256, "8" * 64
    )
    return JoinCase(authorization, inventory, enrollment, lineage, containment)


def test_agreeing_joins_preserve_unknowns_and_proven_absent_never_created_branch() -> None:
    case = component_join()
    before = asdict(case.lineage)
    case.check()
    assert asdict(case.lineage) == before
    assert [item.outcome for item in case.lineage.generations] == ["unknown", "unknown", "never-created"]
    assert case.lineage.generations[-1].management_id not in case.containment.docker_ids


@pytest.mark.parametrize("change", ["missing-genesis", "missing-generations", "unavailable", "foreign-history", "reused-request", "reused-authorization"])
def test_original_authority_must_be_complete_and_new_recovery_identity_distinct(change: str) -> None:
    case = component_join()
    if change == "missing-genesis":
        case = replace(case, lineage=replace(case.lineage, genesis=None))
    elif change == "missing-generations":
        case = replace(case, lineage=replace(case.lineage, generations=()))
    elif change == "unavailable":
        case = replace(case, lineage=replace(case.lineage, unavailable=("original stores absent",)))
    elif change == "foreign-history":
        case = replace(case, lineage=replace(case.lineage, binding=replace(case.lineage.binding, history="foreign")))
    elif change == "reused-request":
        case = replace(case, authorization=replace(case.authorization, request_id=case.lineage.generations[0].request_id))
    else:
        assert case.lineage.genesis is not None
        case = replace(case, authorization=replace(case.authorization, authorization_id=case.lineage.genesis.authorization_id))
    with pytest.raises(EpochUnavailable):
        case.check()


@pytest.mark.parametrize("change", ["unavailable-status", "claims-grant", "claims-history", "claims-admission", "stale-enrollment"])
def test_containment_is_the_exact_current_nonauthorizing_scope(change: str) -> None:
    case = component_join()
    if change == "stale-enrollment":
        stale = replace(case.enrollment, inode=case.enrollment.inode + 1)
        case = replace(case, containment=replace(case.containment, facts=replace(case.containment.facts, enrollment=stale)))
    else:
        field, value = {"unavailable-status": ("status", "unavailable"), "claims-grant": ("grant_issued", True), "claims-history": ("history_asserted", True), "claims-admission": ("admission_created", True)}[change]
        case = replace(case, containment=replace(case.containment, **{field: value}))
    with pytest.raises(EpochUnavailable, match="non-authorizing"):
        case.check()


@pytest.mark.parametrize("change", ["deployment", "source", "inventory-source", "target", "target-config", "engine"])
def test_current_source_target_and_engine_must_join_original_and_fixed_inventory(change: str) -> None:
    case = component_join()
    if change in ("deployment", "source", "engine"):
        field, value = {"deployment": ("deployment_id", "foreign"), "source": ("source_identity", "sha256:" + "0" * 64), "engine": ("engine_id", "another-engine")}[change]
        case = case.scope(replace(case.enrollment.enrollment, **{field: value}))
    else:
        field = {"inventory-source": "source_sha256", "target": "target_id", "target-config": "config_sha256"}[change]
        case = replace(case, inventory=replace(case.inventory, **{field: "0" * 64}))
    with pytest.raises(EpochUnavailable):
        case.check()


@pytest.mark.parametrize("change", ["omitted-lineage", "omitted-observation", "omitted-started-scope", "duplicate-lineage", "duplicate-observation"])
def test_no_original_generation_or_current_manager_can_disappear_or_alias(change: str) -> None:
    case = component_join()
    if change == "omitted-lineage":
        case = replace(case, lineage=replace(case.lineage, generations=(case.lineage.generations[0], case.lineage.generations[2])))
    elif change == "duplicate-lineage":
        case = replace(case, lineage=replace(case.lineage, generations=(*case.lineage.generations, case.lineage.generations[0])))
    elif change == "duplicate-observation":
        case = case.managers((*case.containment.managers, case.containment.managers[0]))
    else:
        case = case.managers((case.containment.managers[0], case.containment.managers[-1]))
        if change == "omitted-started-scope":
            case = case.scope(replace(case.enrollment.enrollment, originals=case.enrollment.enrollment.originals[:1]))
    with pytest.raises(EpochUnavailable):
        case.check()


@pytest.mark.parametrize("change", ["bootstrap", "proxy", "owner", "root-path", "root-inode", "missing-root"])
def test_original_native_configuration_owner_and_actual_root_objects_must_match(change: str) -> None:
    case = component_join()
    original, fact = case.enrollment.enrollment.originals[0], case.containment.managers[0]
    if change == "bootstrap":
        original = replace(original, bootstrap_config=replace(original.bootstrap_config, sha256="0" * 64))
    elif change == "proxy":
        original = replace(original, proxy_id="0" * 64)
    elif change == "owner":
        assert original.owner is not None
        original = replace(original, owner=replace(original.owner, inode=original.owner.inode + 1))
    elif change == "root-path":
        fact = replace(fact, state_root="/component/different-root")
    elif change == "root-inode":
        fact = replace(fact, mounts=(replace(fact.mounts[0], inode=fact.mounts[0].inode + 1), *fact.mounts[1:]))
    else:
        fact = replace(fact, mounts=fact.mounts[1:])
    case = case.scope(replace(case.enrollment.enrollment, originals=(original, *case.enrollment.enrollment.originals[1:]))).managers((fact, *case.containment.managers[1:]))
    with pytest.raises(EpochUnavailable):
        case.check()


@pytest.mark.parametrize("change", ["docker-object-exists", "owner-exists", "outcome-unknown"])
def test_absent_branch_requires_original_never_created_boundary_without_current_object(change: str) -> None:
    case = component_join()
    absent = case.lineage.generations[-1]
    if change == "docker-object-exists":
        case = replace(case, containment=replace(case.containment, facts=replace(case.containment.facts, docker=(*case.containment.facts.docker, component_docker(absent.management_id)))))
    else:
        absent = replace(absent, **({"owner_inode": 9999} if change == "owner-exists" else {"outcome": "unknown"}))
        case = replace(case, lineage=replace(case.lineage, generations=(*case.lineage.generations[:-1], absent)))
    with pytest.raises(EpochUnavailable, match="absent"):
        case.check()


@pytest.mark.parametrize("change", ["oauth-path", "oauth-inode", "state-reused", "journal-nested", "state-parent"])
def test_target_preserves_original_oauth_and_keeps_new_roots_disjoint(change: str) -> None:
    case = component_join()
    target = case.containment.managers[-1]
    if change == "oauth-path":
        target = replace(target, oauth_root="/component/other-oauth")
    elif change == "oauth-inode":
        target = replace(target, mounts=(*target.mounts[:-1], replace(target.mounts[-1], inode=601)))
    elif change == "state-reused":
        target = replace(target, state_root=case.lineage.generations[0].state_root)
    elif change == "journal-nested":
        target = replace(target, journal_root=case.lineage.generations[-1].journal_root + "/nested")
    else:
        target = replace(target, state_root="/component")
    with pytest.raises(EpochUnavailable):
        case.managers((*case.containment.managers[:-1], target)).check()


@dataclass
class ScopeCase:
    authorization: EpochAuthorization
    enrollment: RecoveryEnrollment
    resources: EpochResources
    configuration: ConsumerConfiguration
    path: Path

    def write_configuration(self, configuration: ConsumerConfiguration) -> None:
        self.configuration = configuration
        self.path.write_bytes(canonical(asdict(configuration)))
        info = self.path.stat()
        self.authorization = replace(self.authorization, consumer_config=RecoveryFile(str(self.path), hashlib.sha256(self.path.read_bytes()).hexdigest(), info.st_dev, info.st_ino))

    def check(self) -> None:
        _check_consumer_scope(self.authorization, self.enrollment, self.resources)


@contextmanager
def component_scope() -> Iterator[ScopeCase]:
    with tempfile.TemporaryDirectory(prefix="ac-recovery-join-", dir=pwd.getpwuid(os.geteuid()).pw_dir) as directory, ExitStack() as held:
        root = Path(directory)
        history_root = root / "history"
        history_root.mkdir(mode=0o700)
        paths = {"history": history_root / "history.lock", "owner-0": root / "owner.lock", "controller-0": root / "controller.lock", "epoch": root / "epoch.lock", "target-owner": root / "target.lock"}
        descriptors = {}
        for name, path in paths.items():
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            held.callback(os.close, descriptor)
            descriptors[name] = descriptor
            if name != "target-owner":
                fcntl.flock(descriptor, (fcntl.LOCK_SH if name in ("history", "owner-0") else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        identities = {name: os.fstat(descriptor) for name, descriptor in descriptors.items()}
        fixture = component_join()
        original = replace(fixture.enrollment.enrollment.originals[0], owner=RecoveryLock(str(paths["owner-0"]), identities["owner-0"].st_dev, identities["owner-0"].st_ino))
        target = fixture.enrollment.enrollment.target
        mounts = [DeploymentRootBinding(target.management_id, "/management-recovery", str(PEER_ROOT), False, 1, 1600, stat.S_IFDIR | 0o700)]
        original_paths = {"history": "/management-recovery-history/history.lock", "owner-0": "/management-recovery-originals/0/owner.lock"}
        mounts.extend(DeploymentRootBinding(target.management_id, destination, str(paths[name]), False, identities[name].st_dev, identities[name].st_ino, stat.S_IFREG | 0o600) for name, destination in original_paths.items())
        target = replace(target, mounts=tuple(mounts), owner=RecoveryLock(str(paths["target-owner"]), identities["target-owner"].st_dev, identities["target-owner"].st_ino))
        enrollment = replace(fixture.enrollment.enrollment, originals=(original,), target=target)
        fences = tuple(HeldFence(name, descriptors[name], "original-sh" if name in original_paths else "target-ex" if name == "target-owner" else "transferred-ex") for name in paths)
        boot, pid, start = issuer_lifetime()
        resources = EpochResources(descriptors["history"], RecoveryResources(pid, start, boot, (NamedDescriptor(original.generation_id, descriptors["owner-0"]),), (), ()), fences, ())
        configuration = ConsumerConfiguration(
            1,
            fixture.authorization.request_id,
            fixture.authorization.authorization_id,
            target.management_id,
            0,
            1,
            1600,
            tuple(OriginalFence(name, original_paths[name], identities[name].st_dev, identities[name].st_ino) for name in original_paths),
            TargetOwner("target-owner", identities["target-owner"].st_dev, identities["target-owner"].st_ino),
            tuple(TransferredResource(name, "transferred-ex", identities[name].st_dev, identities[name].st_ino) for name in ("controller-0", "epoch")),
        )
        path = root / "consumer.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        case = ScopeCase(replace(fixture.authorization, history=replace(fixture.authorization.history, root=str(history_root))), enrollment, resources, configuration, path)
        case.write_configuration(configuration)
        yield case


def test_consumer_scope_joins_exact_real_fds_and_pinned_read_only_mounts() -> None:
    with component_scope() as case:
        case.check()


@pytest.mark.parametrize("change", ["missing-peer", "writable-peer", "foreign-peer-source", "replaced-peer-inode", "duplicate-peer", "writable-original", "foreign-original-source", "replaced-original-inode"])
def test_consumer_scope_requires_exact_read_only_endpoint_and_original_mounts(change: str) -> None:
    with component_scope() as case:
        mounts = list(case.enrollment.target.mounts)
        if change == "missing-peer":
            mounts.pop(0)
        elif change == "duplicate-peer":
            mounts.append(mounts[0])
        else:
            index = 0 if "peer" in change else 1
            if change.startswith("writable"):
                mounts[index] = replace(mounts[index], writable=True)
            elif change.startswith("foreign"):
                mounts[index] = replace(mounts[index], source="/component/foreign")
            else:
                mounts[index] = replace(mounts[index], inode=mounts[index].inode + 1)
        case.enrollment = replace(case.enrollment, target=replace(case.enrollment.target, mounts=tuple(mounts)))
        with pytest.raises(EpochUnavailable, match="read-only"):
            case.check()


@pytest.mark.parametrize("change", ["foreign-target", "omitted-original", "wrong-original-path", "target-owner-inode", "omitted-controller", "transfer-inode", "transfer-kind"])
def test_consumer_scope_cannot_omit_or_retarget_acknowledged_authority(change: str) -> None:
    with component_scope() as case:
        configuration = case.configuration
        if change == "foreign-target":
            configuration = replace(configuration, target_id="e" * 64)
        elif change == "omitted-original":
            configuration = replace(configuration, original_fences=configuration.original_fences[:1])
        elif change == "wrong-original-path":
            configuration = replace(configuration, original_fences=(replace(configuration.original_fences[0], path="/component/foreign"), *configuration.original_fences[1:]))
        elif change == "target-owner-inode":
            configuration = replace(configuration, target_owner=replace(configuration.target_owner, inode=configuration.target_owner.inode + 999))
        elif change == "omitted-controller":
            configuration = replace(configuration, transferred_resources=configuration.transferred_resources[1:])
        elif change == "transfer-inode":
            configuration = replace(configuration, transferred_resources=(replace(configuration.transferred_resources[0], inode=configuration.transferred_resources[0].inode + 999), *configuration.transferred_resources[1:]))
        else:
            configuration = replace(configuration, transferred_resources=(replace(configuration.transferred_resources[0], kind="network-namespace"), *configuration.transferred_resources[1:]))
        case.write_configuration(configuration)
        with pytest.raises(EpochUnavailable):
            case.check()
