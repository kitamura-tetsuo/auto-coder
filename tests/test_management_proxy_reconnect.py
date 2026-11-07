import json
import multiprocessing
import os
import select
import stat
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from auto_coder.management.proxy_reconnect import ProxyBinding, ProxyReconnect


class Driver:
    def __init__(self, binding: ProxyBinding) -> None:
        self.binding = binding
        self.manager = {"id": binding.manager_id, "image": binding.manager_image, "running": True, "pid": 10, "started": "one", "network": "bridge"}
        self.proxy = {"id": binding.proxy_id, "image": binding.proxy_image, "running": True, "pid": 20, "started": "one", "network": "container:" + binding.manager_id, "entrypoint": ["caddy"], "command": ["run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]}
        self.namespaces = {10: "net:[2]", 20: "net:[1]"}
        self.effects: list[str] = []
        self.config = binding.config_sha256
        self.fail = ""
        self.change_after_stop = False

    def inspect(self, container: str) -> dict[str, object]:
        return dict(self.manager if container == self.binding.manager_id else self.proxy)

    def namespace(self, pid: int) -> str:
        return self.namespaces[pid]

    def config_digest(self, _proxy: str) -> str:
        return self.config

    def effect(self, action: str, _proxy: str) -> None:
        self.effects.append(action)
        if self.fail == action:
            raise TimeoutError("uncertain delivery")
        if action == "stop":
            self.proxy.update(running=False, pid=0)
            if self.change_after_stop:
                self.manager.update(pid=11, started="two")
                self.namespaces[11] = "net:[3]"
        else:
            self.proxy.update(running=True, pid=21, started="two")
            self.namespaces[21] = self.namespaces[int(self.manager["pid"])]


def fixture(tmp_path: Path) -> tuple[ProxyReconnect, Driver]:
    binding = ProxyBinding("a" * 64, "sha256:" + "b" * 64, "c" * 64, "sha256:" + "d" * 64, "e" * 64)
    driver = Driver(binding)
    return ProxyReconnect(tmp_path, binding, driver), driver  # type: ignore[arg-type]


def test_stale_namespace_reconnects_once_and_replay_does_not_dispatch(tmp_path: Path) -> None:
    controller, driver = fixture(tmp_path)
    assert controller.tick() == "ATTACHED"
    assert driver.effects == ["stop", "start"]
    assert controller.tick() == "ATTACHED"
    assert driver.effects == ["stop", "start"]
    driver.manager.update(pid=11, started="two")
    driver.namespaces[11] = "net:[3]"
    assert controller.tick() == "ATTACHED"
    assert driver.effects == ["stop", "start", "stop", "start"]


@pytest.mark.parametrize("effect", ["stop", "start"])
def test_uncertain_effect_never_retries_after_controller_reconstruction(tmp_path: Path, effect: str) -> None:
    controller, driver = fixture(tmp_path)
    driver.fail = effect
    with pytest.raises(TimeoutError):
        controller.tick()
    calls = list(driver.effects)
    reconstructed = ProxyReconnect(tmp_path, driver.binding, driver)  # type: ignore[arg-type]
    assert reconstructed.tick() == "NEEDS_ATTENTION"
    assert driver.effects == calls


def test_manager_change_after_confirmed_stop_never_starts_proxy(tmp_path: Path) -> None:
    controller, driver = fixture(tmp_path)
    driver.change_after_stop = True
    assert controller.tick() == "NEEDS_ATTENTION"
    assert driver.effects == ["stop"]
    assert controller.tick() == "NEEDS_ATTENTION" and driver.effects == ["stop"]


def test_reconstruction_after_durably_confirmed_stop_starts_once(tmp_path: Path) -> None:
    controller, driver = fixture(tmp_path)
    manager = controller._manager()
    driver.effect("stop", driver.binding.proxy_id)
    controller._write(manager, "stopped")
    reconstructed = ProxyReconnect(tmp_path, driver.binding, driver)  # type: ignore[arg-type]
    assert reconstructed.tick() == "ATTACHED"
    assert driver.effects == ["stop", "start"]
    assert reconstructed.tick() == "ATTACHED" and driver.effects == ["stop", "start"]


def test_unexpected_stopped_proxy_and_wrong_image_never_issue_effects(tmp_path: Path) -> None:
    controller, driver = fixture(tmp_path)
    driver.proxy.update(running=False, pid=0)
    assert controller.tick() == "NEEDS_ATTENTION" and driver.effects == []
    driver.proxy.update(running=True, pid=20, image="sha256:" + "e" * 64)
    with pytest.raises(ValueError):
        controller.tick()
    assert driver.effects == []


def test_signal_gated_proxy_is_never_treated_as_reconnected(tmp_path: Path) -> None:
    controller, driver = fixture(tmp_path)
    driver.proxy.update(entrypoint=["/bin/sh"], command=["-c", "wait for signal"])
    with pytest.raises(ValueError):
        controller.tick()
    assert driver.effects == []


def test_changed_proxy_configuration_refuses_without_effect(tmp_path: Path) -> None:
    controller, driver = fixture(tmp_path)
    driver.config = "f" * 64
    with pytest.raises(ValueError):
        controller.tick()
    assert driver.effects == []


@pytest.mark.parametrize("effect", ["stop", "start"])
def test_operator_observation_preserves_unknown_and_never_replays_it(tmp_path: Path, effect: str) -> None:
    controller, driver = fixture(tmp_path)
    driver.fail = effect
    with pytest.raises(TimeoutError):
        controller.tick()
    prior = controller._read()
    calls = list(driver.effects)
    if effect == "stop":
        driver.proxy.update(running=False, pid=0)
    else:
        driver.proxy.update(running=True, pid=21, started="two")
        driver.namespaces[21] = driver.namespaces[10]
    assert controller.reconcile() == ("STOP_OBSERVED" if effect == "stop" else "ATTACHMENT_OBSERVED")
    records = list(tmp_path.glob("proxy-resolution-*.json"))
    assert len(records) == 1 and json.loads(records[0].read_text())["prior"] == prior
    assert driver.effects == calls
    driver.fail = ""
    assert controller.tick() == "ATTACHED"
    assert driver.effects == (calls + ["start"] if effect == "stop" else calls)


def test_operator_refuses_changed_incarnation(tmp_path: Path) -> None:
    controller, driver = fixture(tmp_path)
    driver.fail = "stop"
    with pytest.raises(TimeoutError):
        controller.tick()
    prior = controller._read()
    driver.proxy.update(running=False, pid=0)
    driver.manager.update(pid=11, started="two")
    driver.namespaces[11] = "net:[3]"
    assert controller.reconcile() == "NEEDS_ATTENTION"
    assert controller._read() == prior and not list(tmp_path.glob("proxy-resolution-*.json"))


def test_resolution_record_survives_crash_before_transition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller, driver = fixture(tmp_path)
    driver.fail = "stop"
    with pytest.raises(TimeoutError):
        controller.tick()
    prior = controller._read()
    driver.proxy.update(running=False, pid=0)
    original = controller._write

    def crash(_manager: dict[str, object], _phase: str) -> None:
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(controller, "_write", crash)
    with pytest.raises(RuntimeError):
        controller.reconcile()
    assert controller._read() == prior and len(list(tmp_path.glob("proxy-resolution-*.json"))) == 1
    monkeypatch.setattr(controller, "_write", original)
    assert controller.reconcile() == "STOP_OBSERVED"
    assert len(list(tmp_path.glob("proxy-resolution-*.json"))) == 1 and driver.effects == ["stop"]


def test_persistent_watchers_contend_on_one_host_lock(tmp_path: Path) -> None:
    _, driver = fixture(tmp_path)
    path = tmp_path / "watch.json"
    path.write_text(json.dumps({"state_root": str(tmp_path), **asdict(driver.binding)}), encoding="utf-8")
    path.chmod(0o600)
    command = [sys.executable, "-m", "auto_coder.management.proxy_watch_cli", "--config", str(path)]
    first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert first.stdout is not None and select.select([first.stdout], [], [], 15)[0]
        assert first.stdout.readline().startswith("UNAVAILABLE:")
        second = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
        assert second.returncode != 0 and "BlockingIOError" in second.stderr
    finally:
        first.terminate()
        first.wait(timeout=10)


def stopped_old_attachment(tmp_path: Path) -> tuple[ProxyReconnect, Driver]:
    controller, driver = fixture(tmp_path)
    controller._write(controller._manager(), "stopped")
    driver.manager.update(pid=11, started="two")
    driver.namespaces[11] = "net:[3]"
    driver.proxy.update(pid=21, started="two")
    driver.namespaces[21] = "net:[3]"
    return controller, driver


def expected_stopped_attachment(driver: Driver) -> dict[str, object]:
    return {
        "prior": {"binding": asdict(driver.binding), "manager": {"id": driver.binding.manager_id, "pid": 10, "started": "one", "namespace": "net:[2]"}, "phase": "stopped"},
        "manager": {"id": driver.binding.manager_id, "pid": 11, "started": "two", "namespace": "net:[3]"},
        "proxy": dict(driver.proxy),
        "proxy_namespace": "net:[3]",
    }


def test_explicit_stopped_successor_observation_preserves_prior_without_effect(tmp_path: Path) -> None:
    controller, driver = stopped_old_attachment(tmp_path)
    prior = controller._read()
    assert controller.tick() == "NEEDS_ATTENTION"
    assert controller.reconcile() == "ATTACHMENT_OBSERVED"
    saved = controller._read()
    assert saved is not None and saved["phase"] == "done" and saved["manager"] == controller._manager()
    records = list(tmp_path.glob("proxy-resolution-*.json"))
    expected = expected_stopped_attachment(driver)
    assert expected["prior"] == prior
    assert len(records) == 1 and json.loads(records[0].read_text()) == expected
    archived = records[0].read_bytes()
    assert controller.tick() == "ATTACHED"
    assert controller.reconcile() == "NEEDS_ATTENTION"
    assert records[0].read_bytes() == archived
    assert len(list(tmp_path.glob("proxy-resolution-*.json"))) == 1 and driver.effects == []


@pytest.mark.parametrize("fault", ["namespace", "stopped", "image", "config", "identity", "same_lifetime", "unknown"])
def test_stopped_successor_refuses_mismatched_evidence(tmp_path: Path, fault: str) -> None:
    controller, driver = stopped_old_attachment(tmp_path)
    if fault == "namespace":
        driver.namespaces[21] = "net:[4]"
    elif fault == "stopped":
        driver.proxy.update(running=False, pid=0)
    elif fault == "image":
        driver.proxy["image"] = "sha256:" + "f" * 64
    elif fault == "config":
        driver.config = "f" * 64
    elif fault == "identity":
        driver.manager["id"] = "f" * 64
    elif fault == "same_lifetime":
        controller._write(controller._manager(), "stopped")
    else:
        controller._write({"id": driver.binding.manager_id, "pid": 10, "started": "one", "namespace": "net:[2]"}, "start_unknown")
    prior = controller._read()
    if fault in {"image", "config", "identity"}:
        with pytest.raises(ValueError):
            controller.reconcile()
    else:
        assert controller.reconcile() == "NEEDS_ATTENTION"
    assert controller._read() == prior and driver.effects == []
    assert not list(tmp_path.glob("proxy-resolution-*.json"))


def test_stopped_successor_interrupted_resolution_retries_without_effect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller, driver = stopped_old_attachment(tmp_path)
    prior = controller._read()
    original = controller._write

    def crash(_manager: dict[str, object], _phase: str) -> None:
        raise RuntimeError("simulated interruption")

    monkeypatch.setattr(controller, "_write", crash)
    with pytest.raises(RuntimeError):
        controller.reconcile()
    assert controller._read() == prior
    record = next(tmp_path.glob("proxy-resolution-*.json"))
    archived = record.read_bytes()
    assert json.loads(archived) == expected_stopped_attachment(driver)
    monkeypatch.setattr(controller, "_write", original)
    assert controller.reconcile() == "ATTACHMENT_OBSERVED"
    assert record.read_bytes() == archived
    assert controller.reconcile() == "NEEDS_ATTENTION"
    assert record.read_bytes() == archived
    assert len(list(tmp_path.glob("proxy-resolution-*.json"))) == 1 and driver.effects == []


@pytest.mark.parametrize("fault", ["manager_race", "proxy_race", "journal_race", "resolution_conflict"])
def test_stopped_successor_resolution_conflicts_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    controller, driver = stopped_old_attachment(tmp_path)
    prior = controller._read()
    original = controller._record_resolution

    def change(*args: object) -> None:
        original(*args)  # type: ignore[arg-type]
        if fault == "manager_race":
            driver.manager["started"] = "third"
        elif fault == "proxy_race":
            driver.proxy["started"] = "third"
        elif fault == "journal_race":
            controller._write(controller._manager(), "start_unknown")

    if fault == "resolution_conflict":
        original(prior, controller._manager(), driver.proxy, "net:[3]")  # type: ignore[arg-type]
        record = next(tmp_path.glob("proxy-resolution-*.json"))
        record.write_text("{}")
        with pytest.raises(ValueError, match="resolution differs"):
            controller.reconcile()
    else:
        monkeypatch.setattr(controller, "_record_resolution", change)
        assert controller.reconcile() == "NEEDS_ATTENTION"
    if fault != "journal_race":
        assert controller._read() == prior
    assert driver.effects == []


@pytest.mark.parametrize("key,value", [("id", "f" * 64), ("pid", True), ("pid", 0), ("started", ""), ("namespace", "wrong"), ("extra", "unrecognized")])
def test_corrupt_saved_manager_lifetime_refuses(tmp_path: Path, key: str, value: object) -> None:
    controller, driver = stopped_old_attachment(tmp_path)
    path = tmp_path / "proxy-reconnect.json"
    raw = json.loads(path.read_text())
    raw["manager"][key] = value
    path.write_text(json.dumps(raw))
    original = path.read_bytes()
    with pytest.raises(ValueError, match="manager lifetime"):
        controller.reconcile()
    assert path.read_bytes() == original and driver.effects == []
    assert not list(tmp_path.glob("proxy-resolution-*.json"))


@pytest.mark.parametrize("duplicate", ["phase", "manager", "binding", "manager_pid", "binding_manager_id"])
def test_duplicate_journal_keys_preserve_ambiguous_evidence(tmp_path: Path, duplicate: str) -> None:
    controller, driver = stopped_old_attachment(tmp_path)
    path = tmp_path / "proxy-reconnect.json"
    raw = path.read_text()
    if duplicate == "phase":
        raw = raw.replace('"phase":"stopped"', '"phase":"start_unknown","phase":"stopped"')
    elif duplicate == "manager":
        raw = raw[:-1] + ', "manager": ' + json.dumps(controller._manager()) + "}"
    elif duplicate == "binding":
        raw = raw[:-1] + ', "binding": ' + json.dumps(asdict(driver.binding)) + "}"
    elif duplicate == "manager_pid":
        raw = raw.replace('"pid":10', '"pid":0,"pid":10')
    else:
        raw = raw.replace('"manager_id":"' + driver.binding.manager_id + '"', '"manager_id":"' + "f" * 64 + '","manager_id":"' + driver.binding.manager_id + '"')
    assert raw != path.read_text()
    path.write_text(raw)
    prior = path.read_bytes()
    with pytest.raises(ValueError, match="duplicate proxy journal key"):
        controller.reconcile()
    with pytest.raises(ValueError, match="duplicate proxy journal key"):
        controller.tick()
    assert path.read_bytes() == prior and driver.effects == []
    assert not list(tmp_path.glob("proxy-resolution-*.json"))


@pytest.mark.parametrize("cut", ["created", "before_publish", "after_publish"])
def test_archive_publication_interruption_allows_fresh_controller_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cut: str) -> None:
    controller, driver = stopped_old_attachment(tmp_path)
    original_journal = (tmp_path / "proxy-reconnect.json").read_bytes()
    expected = expected_stopped_attachment(driver)
    effect_marker = tmp_path / "unexpected-effect"

    def interrupt() -> None:
        original_open, original_link = os.open, os.link

        def opened(path, *args, **kwargs):
            descriptor = original_open(path, *args, **kwargs)
            if cut == "created" and str(path).startswith(".proxy-resolution-stage-"):
                os._exit(73)
            return descriptor

        def linked(*args, **kwargs):
            if cut == "before_publish":
                os._exit(73)
            original_link(*args, **kwargs)
            if cut == "after_publish":
                os._exit(73)

        def unexpected(*args):
            effect_marker.write_text("unexpected effect")
            raise AssertionError("reconcile must only observe")

        os.open, os.link = opened, linked
        driver.effect = unexpected
        controller.reconcile()

    process = multiprocessing.get_context("fork").Process(target=interrupt)
    process.start()
    try:
        process.join(10)
        assert process.exitcode == 73
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)
    assert not effect_marker.exists()
    assert (tmp_path / "proxy-reconnect.json").read_bytes() == original_journal
    staged = list(tmp_path.glob(".proxy-resolution-stage-*"))
    assert len(staged) == 1
    records = list(tmp_path.glob("proxy-resolution-*.json"))
    assert len(records) == (1 if cut == "after_publish" else 0)
    if records:
        assert json.loads(records[0].read_bytes()) == expected
    synced = []
    fsync = os.fsync

    def synchronized(fd: int) -> None:
        fsync(fd)
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced.append(True)

    monkeypatch.setattr(os, "fsync", synchronized)
    fresh = ProxyReconnect(tmp_path, driver.binding, driver)  # type: ignore[arg-type]
    write = fresh._write

    def durable_before_done(manager: dict[str, object], phase: str) -> None:
        assert synced
        final = list(tmp_path.glob("proxy-resolution-*.json"))
        assert len(final) == 1 and json.loads(final[0].read_bytes()) == expected
        write(manager, phase)

    monkeypatch.setattr(fresh, "_write", durable_before_done)
    assert fresh.reconcile() == "ATTACHMENT_OBSERVED"
    records = list(tmp_path.glob("proxy-resolution-*.json"))
    assert len(records) == 1 and json.loads(records[0].read_bytes()) == expected
    archived = records[0].read_bytes()
    assert fresh.reconcile() == "NEEDS_ATTENTION" and fresh.tick() == "ATTACHED"
    assert records[0].read_bytes() == archived and driver.effects == []
    assert not effect_marker.exists()
