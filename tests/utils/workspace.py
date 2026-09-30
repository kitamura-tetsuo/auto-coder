"""Target-repository test scripts used by private-workspace regressions."""

from pathlib import Path


def write_target_test_script(repository: Path, contents: str = "#!/bin/bash\nexit 0\n") -> Path:
    script = repository / "scripts" / "test.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(contents, encoding="utf-8")
    return script
