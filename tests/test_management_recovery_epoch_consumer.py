"""Real consumer custody with a wire issuer, not complete recovery authority.

The independent issuer owns actual flocks and creates the socket pair, so the
consumer's SO_PEERCRED/SO_PEERPIDFD name that real issuer process. The fixture
supplies protocol grant bytes; it does not claim Docker/lineage/epoch acceptance.
The separate peer tests exercise the issuer's authenticated consumer checks.
"""

from __future__ import annotations

import array
import copy
import fcntl
import json
import os
import select
import signal
import socket
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

import pytest

from auto_coder.management.recovery_epoch_consumer import ConsumerConfiguration, OriginalFence, TargetOwner, TransferredResource, connect_recovery_epoch, parse_consumer_configuration
from auto_coder.management.recovery_epoch_peer import PeerUnavailable

pytestmark = pytest.mark.usefixtures("_use_real_commands", "_use_real_sleep")

_ISSUER = r"""
import array,fcntl,json,os,secrets,socket,sys
from pathlib import Path
from auto_coder.management.recovery_epoch_peer import OFFER_SCHEMA, RECEIPT_SCHEMA, encode_peer_message, receive_peer_message
from auto_coder.management.recovery_epoch_consumer import GRANT_SCHEMA
mailbox_fd, paths_json, config_json, mode = sys.argv[1:]
paths, configuration = json.loads(paths_json), json.loads(config_json)
held = {}
for name, operation in (('original', fcntl.LOCK_SH), ('controller', fcntl.LOCK_EX)):
    held[name] = os.open(paths[name], os.O_RDONLY|os.O_NOFOLLOW)
    fcntl.flock(held[name], operation|fcntl.LOCK_NB)
if any(item['kind']=='network-namespace' for item in configuration['transferred_resources']):
    held['namespace'] = os.open('/proc/self/ns/net',os.O_RDONLY|os.O_CLOEXEC)
connection, consumer_endpoint = socket.socketpair(socket.AF_UNIX,socket.SOCK_STREAM)
mailbox=socket.socket(fileno=int(mailbox_fd))
mailbox.sendmsg([b'c'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[consumer_endpoint.fileno()]))])
consumer_endpoint.close();mailbox.close()
print('issuer-ready',flush=True)
request,_=receive_peer_message(connection,timeout=10)
print('challenge-read',flush=True)
if mode=='cancel-before-offer':
    try:
        receive_peer_message(connection,timeout=10)
    except Exception:
        print('consumer-closed-before-offer',flush=True)
        sys.stdin.readline()
        raise SystemExit(0)
    raise RuntimeError('unexpected pre-offer consumer message')
if sys.stdin.readline().strip()!='offer': raise SystemExit(0)
nonce=secrets.token_hex(32)
resources=[]
for item in configuration['original_fences']:
    resources.append({'name':item['name'],'kind':'original-sh','device':item['device'],'inode':item['inode'],'transfer_index':None})
owner=configuration['target_owner']
resources.append({'name':owner['name'],'kind':'target-ex','device':owner['device'],'inode':owner['inode'],'transfer_index':None})
transferred=[]
for item in configuration['transferred_resources']:
    resources.append({**item,'transfer_index':len(transferred)})
    transferred.append(held[item['name']])
offer={key:request[key] for key in ('request_id','authorization_id','target_id','challenge')}
offer.update(schema=OFFER_SCHEMA,receipt_nonce=nonce,resources=resources)
if mode=='offer-wrong-nonce': offer['challenge']='0'*64
if mode=='offer-omitted': resources.pop()
if mode=='offer-extra': resources.append({'name':'undeclared','kind':'transferred-ex','device':1,'inode':2,'transfer_index':0})
if mode=='offer-wrong-inode': resources[-1]['inode']+=1
if mode=='offer-original-transfer': resources[0]['transfer_index']=0
if mode=='offer-extra-fd': transferred.append(held['original'])
if mode=='offer-unheld-ex': fcntl.flock(held['controller'],fcntl.LOCK_UN)
connection.sendmsg([encode_peer_message(offer)],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',transferred))])
try:
    receipt,_=receive_peer_message(connection,timeout=10)
except Exception:
    print('receipt-unavailable',flush=True)
    sys.stdin.readline()
    raise SystemExit(0)
assert receipt['schema']==RECEIPT_SCHEMA and receipt['receipt_nonce']==nonce
assert {item['name'] for item in receipt['descriptors']}=={item['name'] for item in resources}
print('receipt-read',flush=True)
if sys.stdin.readline().strip()!='grant': raise SystemExit(0)
grant={key:request[key] for key in ('request_id','authorization_id','target_id','challenge')}
grant.update(schema=GRANT_SCHEMA,granted='recovery-epoch-consumed',receipt_nonce=nonce,inventory_sha256='1'*64,binding_sha256='2'*64,consumed_sha256='3'*64,receipt_sha256='4'*64,host_boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),host_pid=os.getpid(),host_start_token=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19])
if mode=='grant-wrong-target': grant['target_id']='another-target'
if mode=='grant-stale-nonce': grant['receipt_nonce']='0'*64
if mode=='grant-missing-consumption': grant['consumed_sha256']=''
if mode=='grant-wrong-host': grant['host_pid']+=1
if mode=='grant-extra-field': grant['another_authority']=True
connection.sendall(encode_peer_message(grant))
print('grant-sent',flush=True)
for command in sys.stdin:
    command=command.strip()
    if command=='exit': break
    if command=='close-channel': connection.close()
    elif command=='unlock-controller': fcntl.flock(held['controller'],fcntl.LOCK_UN)
    else: raise RuntimeError('unsupported issuer command')
    print(command,flush=True)
"""

_CONSUMER = r"""
import fcntl,json,os,socket,sys
from dataclasses import asdict
from pathlib import Path
from auto_coder.management.recovery_epoch_consumer import RecoveryEpochConsumer,parse_consumer_configuration
fd,config_path,target_path=sys.argv[1:]
connection=socket.socket(fileno=int(fd))
configuration=parse_consumer_configuration(Path(config_path).read_bytes())
target=os.open(target_path,os.O_RDONLY|os.O_NOFOLLOW)
fcntl.flock(target,fcntl.LOCK_EX|fcntl.LOCK_NB)
try:
    handle=RecoveryEpochConsumer(connection,configuration,target,timeout=10,configuration_file=Path(config_path))
except Exception as error:
    print(json.dumps({'status':'refused','error':type(error).__name__,'detail':str(error)}),flush=True)
    sys.stdin.readline()
    raise SystemExit(0)
print(json.dumps({'status':'received','grant':asdict(handle.grant),'retained':len(handle.retained_descriptors)}),flush=True)
for command in sys.stdin:
    command=command.strip()
    if command=='exit': handle.close();break
    if command=='close':
        handle.close();print(json.dumps({'status':'closed'}),flush=True);continue
    if command!='check': raise RuntimeError('unsupported consumer command')
    try:
        handle.check_current()
        result={'status':'current'}
    except Exception as error:
        result={'status':'unavailable','error':type(error).__name__,'retained':len(handle.retained_descriptors),'all_open':all(os.fstat(fd).st_ino>0 for fd in handle.retained_descriptors)}
    print(json.dumps(result),flush=True)
"""


_BOOTSTRAP = r"""
import json,os,socket,sys
from pathlib import Path
from auto_coder.management import recovery_epoch_bootstrap as bootstrap
from auto_coder.management.bootstrap_supervisor import BootstrapConfig,RuntimeSpec
from auto_coder.management.recovery_epoch_consumer import CONSUMER_CONFIG,RecoveryEpochConsumer,parse_consumer_configuration
fd,config_path,target_path=sys.argv[1:]
connection=socket.socket(fileno=int(fd))
configuration=parse_consumer_configuration(Path(config_path).read_bytes())
state=Path(target_path).parent
root=state.parent
for name in ('journal','oauth'):
    (root/name).mkdir(mode=0o700)
config=BootstrapConfig('target-one',state,root/'oauth',root/'journal','http://127.0.0.1:8080','never-launched',(RuntimeSpec('never-launched','sha256:'+'1'*64,Path(sys.executable),('-c','raise RuntimeError("must not launch")')),),verification_timeout=10,recovery_epoch_config=CONSUMER_CONFIG)
# Component seam: use the real connected wire fixture, preserving the actual
# bootstrap ownership acquisition, signal handlers, consumer, and cleanup.
def connected_fixture(config_path,owner,*,timeout):
    assert config_path==CONSUMER_CONFIG
    return RecoveryEpochConsumer(connection,configuration,owner,timeout=timeout)
bootstrap.connect_recovery_epoch=connected_fixture
status=bootstrap.run_recovery_epoch_bootstrap(config)
print(json.dumps({'bootstrap_status':status}),flush=True)
raise SystemExit(status)
"""


@dataclass
class Exchange:
    issuer: subprocess.Popen[str]
    consumer: subprocess.Popen[str]
    configuration: ConsumerConfiguration
    configuration_path: Path
    paths: dict[str, str]


def line(process: subprocess.Popen[str]) -> str:
    assert process.stdout is not None
    assert select.select([process.stdout], [], [], 15)[0], "independent process did not reach the required barrier"
    value = process.stdout.readline().strip()
    assert value, (process.poll(), process.stderr.read() if process.poll() is not None and process.stderr is not None else "")
    return value


def command(process: subprocess.Popen[str], value: str) -> None:
    assert process.stdin is not None
    process.stdin.write(value + "\n")
    process.stdin.flush()


def configuration(root: Path, *, namespace: bool = False, bootstrap: bool = False) -> tuple[ConsumerConfiguration, dict[str, str]]:
    paths = {name: str(root / name) for name in ("original", "target", "controller")}
    if bootstrap:
        state = root / "state"
        state.mkdir(mode=0o700)
        paths["target"] = str(state / ".supervisor-owner.lock")
    for path in paths.values():
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
    original_info, target_info, controller_info = (Path(paths[name]).stat() for name in ("original", "target", "controller"))
    directory = root.stat()
    transferred = [TransferredResource("controller", "transferred-ex", controller_info.st_dev, controller_info.st_ino)]
    if namespace:
        info = Path("/proc/self/ns/net").stat()
        transferred.append(TransferredResource("namespace", "network-namespace", info.st_dev, info.st_ino))
    return (
        ConsumerConfiguration(
            1, "request-one", "authorization-one", "target-one", os.geteuid(), directory.st_dev, directory.st_ino, (OriginalFence("original", paths["original"], original_info.st_dev, original_info.st_ino),), TargetOwner("target", target_info.st_dev, target_info.st_ino), tuple(transferred)
        ),
        paths,
    )


@contextmanager
def exchange(tmp_path: Path, mode: str = "valid", *, namespace: bool = False, bootstrap: bool = False) -> Iterator[Exchange]:
    selected, paths = configuration(tmp_path, namespace=namespace, bootstrap=bootstrap)
    config_path = tmp_path / "configuration.json"
    config_path.write_text(json.dumps(asdict(selected)))
    config_path.chmod(0o600)
    mailbox, issuer_mailbox = socket.socketpair()
    mailbox.settimeout(10)
    issuer = consumer = None
    endpoint = None
    try:
        issuer = subprocess.Popen([sys.executable, "-B", "-c", _ISSUER, str(issuer_mailbox.fileno()), json.dumps(paths), json.dumps(asdict(selected)), mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, pass_fds=(issuer_mailbox.fileno(),))
        issuer_mailbox.close()
        payload, ancillary, flags, _ = mailbox.recvmsg(1, socket.CMSG_SPACE(array.array("i").itemsize), socket.MSG_CMSG_CLOEXEC)
        assert payload == b"c" and not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) and len(ancillary) == 1
        descriptors = array.array("i")
        descriptors.frombytes(ancillary[0][2])
        assert len(descriptors) == 1
        endpoint = descriptors[0]
        assert line(issuer) == "issuer-ready"
        consumer = subprocess.Popen([sys.executable, "-B", "-c", _BOOTSTRAP if bootstrap else _CONSUMER, str(endpoint), str(config_path), paths["target"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, pass_fds=(endpoint,))
        os.close(endpoint)
        endpoint = None
        assert line(issuer) == "challenge-read"
        yield Exchange(issuer, consumer, selected, config_path, paths)
    finally:
        if endpoint is not None:
            os.close(endpoint)
        mailbox.close()
        issuer_mailbox.close()
        for process in (consumer, issuer):
            if process is None:
                continue
            if process.poll() is None:
                try:
                    command(process, "exit")
                except BrokenPipeError:
                    pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)


def grant(case: Exchange) -> dict[str, object]:
    command(case.issuer, "offer")
    assert line(case.issuer) == "receipt-read"
    assert case.consumer.poll() is None
    command(case.issuer, "grant")
    assert line(case.issuer) == "grant-sent"
    return json.loads(line(case.consumer))


@pytest.mark.parametrize("namespace", [False, True])
def test_real_consumer_retains_original_target_controller_and_namespace_custody(tmp_path: Path, namespace: bool) -> None:
    with exchange(tmp_path, namespace=namespace) as case:
        result = grant(case)
        assert result["status"] == "received" and result["retained"] == 2 + int(namespace)
        assert result["grant"]["host_pid"] == case.issuer.pid
        command(case.consumer, "check")
        assert json.loads(line(case.consumer)) == {"status": "current"}
        assert sorted(path.name for path in tmp_path.iterdir()) == ["configuration.json", "controller", "original", "target"]


@pytest.mark.parametrize("mode", ["offer-wrong-nonce", "offer-omitted", "offer-extra", "offer-wrong-inode", "offer-original-transfer", "offer-extra-fd", "offer-unheld-ex"])
def test_consumer_refuses_incomplete_or_wrong_physical_offer(tmp_path: Path, mode: str) -> None:
    with exchange(tmp_path, mode) as case:
        command(case.issuer, "offer")
        result = json.loads(line(case.consumer))
        assert result["status"] == "refused" and result["error"] == "PeerUnavailable"
        assert line(case.issuer) == "receipt-unavailable"


@pytest.mark.parametrize("mode", ["grant-wrong-target", "grant-stale-nonce", "grant-missing-consumption", "grant-wrong-host", "grant-extra-field"])
def test_consumer_refuses_wrong_or_unbound_grant(tmp_path: Path, mode: str) -> None:
    with exchange(tmp_path, mode) as case:
        result = grant(case)
        assert result["status"] == "refused" and result["error"] == "PeerUnavailable"


@pytest.mark.parametrize("failure", ["host-death", "channel-closed", "controller-unlocked", "config-changed", "config-replaced"])
def test_current_failure_preserves_received_custody_until_explicit_close(tmp_path: Path, failure: str) -> None:
    with exchange(tmp_path, namespace=True) as case:
        assert grant(case)["status"] == "received"
        if failure == "host-death":
            case.issuer.kill()
            case.issuer.wait(timeout=5)
        elif failure == "channel-closed":
            command(case.issuer, "close-channel")
            assert line(case.issuer) == "close-channel"
        elif failure == "controller-unlocked":
            command(case.issuer, "unlock-controller")
            assert line(case.issuer) == "unlock-controller"
        elif failure == "config-changed":
            with case.configuration_path.open("ab") as stream:
                stream.write(b"\n")
        else:
            encoded = case.configuration_path.read_bytes()
            case.configuration_path.rename(tmp_path / "preserved-configuration.json")
            case.configuration_path.write_bytes(encoded)
            case.configuration_path.chmod(0o600)
        command(case.consumer, "check")
        result = json.loads(line(case.consumer))
        assert result == {"status": "unavailable", "error": "PeerUnavailable", "retained": 3, "all_open": True}
        command(case.consumer, "close")
        assert json.loads(line(case.consumer)) == {"status": "closed"}


def test_issuer_death_keeps_original_and_transferred_fences_exclusive_until_consumer_closes(tmp_path: Path) -> None:
    with exchange(tmp_path) as case:
        assert grant(case)["status"] == "received"
        case.issuer.kill()
        case.issuer.wait(timeout=5)
        command(case.consumer, "check")
        assert json.loads(line(case.consumer))["status"] == "unavailable"
        contenders = [os.open(case.paths[name], os.O_RDONLY | os.O_NOFOLLOW) for name in ("original", "controller")]
        try:
            for descriptor in contenders:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            command(case.consumer, "close")
            assert json.loads(line(case.consumer)) == {"status": "closed"}
            for descriptor in contenders:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            for descriptor in contenders:
                os.close(descriptor)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_bootstrap_cancellation_before_offer_closes_connection_without_waiting_for_grant(tmp_path: Path, signum: int) -> None:
    with exchange(tmp_path, "cancel-before-offer", bootstrap=True) as case:
        # The observed challenge proves that the actual bootstrap signal handler
        # is installed and its consumer is blocked before any physical offer.
        assert case.consumer.poll() is None
        case.consumer.send_signal(signum)
        assert case.consumer.wait(timeout=5) == 1
        assert line(case.issuer) == "consumer-closed-before-offer"
        stdout, stderr = case.consumer.communicate(timeout=5)
        assert '{"bootstrap_status": 1}' in stdout, (stdout, stderr)
        assert "Recovery custody acknowledged" not in stdout
        assert not any((tmp_path / "journal").iterdir())
        assert sorted(path.name for path in (tmp_path / "state").iterdir()) == [".supervisor-owner.lock", "logs"]
        descriptor = os.open(case.paths["target"], os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)


@pytest.mark.parametrize("change", ["duplicate-name", "duplicate-object", "duplicate-path", "missing-controller", "unknown-kind", "boolean-identity", "extra-field"])
def test_consumer_configuration_is_complete_and_unambiguous(tmp_path: Path, change: str) -> None:
    selected, _ = configuration(tmp_path)
    raw = copy.deepcopy(asdict(selected))
    assert parse_consumer_configuration(json.dumps(raw).encode()) == selected
    if change == "duplicate-name":
        raw["target_owner"]["name"] = "original"
    elif change == "duplicate-object":
        raw["target_owner"].update(device=raw["original_fences"][0]["device"], inode=raw["original_fences"][0]["inode"])
    elif change == "duplicate-path":
        raw["original_fences"] = (*raw["original_fences"], {**raw["original_fences"][0], "name": "another", "inode": 1234})
    elif change == "missing-controller":
        raw["transferred_resources"] = []
    elif change == "unknown-kind":
        raw["transferred_resources"][0]["kind"] = "unverified-ex"
    elif change == "boolean-identity":
        raw["target_owner"]["inode"] = True
    else:
        raw["root_override"] = str(tmp_path)
    with pytest.raises(PeerUnavailable):
        parse_consumer_configuration(json.dumps(raw).encode())


def test_production_factory_rejects_config_path_override_before_connecting(tmp_path: Path) -> None:
    with pytest.raises(PeerUnavailable, match="fixed bootstrap path"):
        connect_recovery_epoch(tmp_path / "configuration.json", -1)
