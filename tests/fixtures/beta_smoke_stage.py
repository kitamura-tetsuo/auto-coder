"""Install two exact source revisions and a distinguishable same-SHA runtime."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from loguru import logger

from auto_coder.worker_versions import PrepareIntent, WorkerVersionConfig, WorkerVersionPreparer


def main() -> None:
    logger.remove()
    layout = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {file}:{function}:{line} | {message}"
    logger.add(sys.stdout, format=layout)
    logger.add("/runtime/staging.log", rotation="10 MB", format=layout)
    outputs = []
    for index, (revision, root) in enumerate(((sys.argv[1], "worker-versions"), (sys.argv[2], "worker-versions"), (sys.argv[1], "alternate-versions"))):
        preparer = WorkerVersionPreparer(WorkerVersionConfig(source_repository=Path("/source"), version_root=Path("/runtime") / root, python_executable=Path(sys.executable), uv_executable=Path("/usr/local/bin/uv"), uv_version="0.7.22"))
        prepared = preparer.prepare(PrepareIntent(source_sha=revision, attempt_id=f"smoke-{index}"))
        receipt = prepared.receipt
        ready = Path(receipt.environment_path).parent / "READY.json"
        assert preparer.read_ready(ready) == receipt
        # Inspect the installed distribution in its own interpreter. Neither
        # the source checkout nor a PYTHONPATH overlay is the worker package.
        program = (
            "import importlib.metadata,json,pathlib,sys,auto_coder; "
            "d=importlib.metadata.distribution('auto-coder'); "
            "u=json.loads(d.read_text('direct_url.json') or '{}'); "
            "print(json.dumps({'prefix':sys.prefix,'module':str(pathlib.Path(auto_coder.__file__).resolve()),'editable':u.get('dir_info',{}).get('editable',False)}))"
        )
        installed = json.loads(subprocess.run([receipt.interpreter_path, "-I", "-c", program], check=True, capture_output=True, text=True).stdout)
        assert installed["editable"] is False
        assert Path(installed["module"]).is_relative_to(Path(receipt.environment_path))
        outputs.append({"receipt": asdict(receipt), "ready_path": str(ready), "installed": installed})
    assert outputs[0]["receipt"]["source_sha"] != outputs[1]["receipt"]["source_sha"]
    assert outputs[0]["receipt"]["installed_runtime_id"] != outputs[2]["receipt"]["installed_runtime_id"]
    assert outputs[0]["receipt"]["content_identity"] != outputs[2]["receipt"]["content_identity"]
    print(json.dumps(outputs, sort_keys=True))


if __name__ == "__main__":
    main()
