from unittest.mock import patch

from src.auto_coder.codex_mcp_client import CodexMCPClient


class DummyStream:
    def readline(self):
        return b""  # immediately EOF

    def close(self):
        pass


class DummyProc:
    def __init__(self):
        self.pid = 12345
        self.stdout = DummyStream()
        self.stderr = DummyStream()
        self.terminated = False

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.terminated = True


@patch("src.auto_coder.codex_mcp_client.subprocess.Popen", return_value=DummyProc())
@patch("src.auto_coder.codex_mcp_client.subprocess.run", return_value=type("R", (), {"returncode": 0})())
def test_codex_mcp_client_starts_and_close_terminates(mock_run, mock_popen):
    client = CodexMCPClient()
    # Popen called for codex mcp
    mock_popen.assert_called()
    args, kwargs = mock_popen.call_args
    assert args[0][:2] == ["codex", "mcp"]

    # close should terminate the persistent process
    proc: DummyProc = mock_popen.return_value  # type: ignore
    assert proc.terminated is False
    client.close()
    assert proc.terminated is True

