"""Reconstruct the real local smoke producer in a fresh manager process."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from loguru import logger

from auto_coder.management.beta_runtime_smoke import BetaRuntimeSmokeProducer, SmokeProfile
from auto_coder.management.operation_store import ManagementOperationStore
from auto_coder.management.source_evidence import BetaEvidenceReader
from auto_coder.management.worker_network_client import WorkerEndpointConfig, WorkerNetworkClient
from auto_coder.util.gh_cache import GitHubGitDataClient


def main() -> None:
    configuration = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    logger.remove()
    layout = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {file}:{function}:{line} | {message}"
    logger.add(sys.stdout, format=layout)
    logger.add(Path(configuration["journal_root"]) / "reconstructed-smoke.log", rotation="10 MB", format=layout)
    network = WorkerNetworkClient(WorkerEndpointConfig("beta-worker", "beta", "fixture/beta", configuration["address"], 8843, base64.b64decode(configuration["secret"])))
    source = BetaEvidenceReader(GitHubGitDataClient("read-only-fixture", "owner/repo", api_url=configuration["source_api"]), "owner/repo")
    # A different configured profile must not reset an already captured window.
    producer = BetaRuntimeSmokeProducer(ManagementOperationStore(Path(configuration["journal_root"])), network, source, SmokeProfile(total_seconds=300, interval_seconds=3))
    observation = producer.read(sys.argv[2]) if sys.argv[3] == "read" else producer.advance(sys.argv[2])
    result = {"state": observation.state.value, "evidence": observation.evidence.document() if observation.evidence else None, "content": observation.evidence.content if observation.evidence else None}
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
