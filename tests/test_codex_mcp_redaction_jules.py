import json
import unittest
from unittest.mock import MagicMock, patch

from auto_coder.codex_mcp_client import CodexMCPClient
from auto_coder.security_utils import redact_string


class TestCodexMCPClientRedaction(unittest.TestCase):
    @patch("auto_coder.codex_mcp_client.get_llm_config")
    @patch("auto_coder.codex_mcp_client.subprocess.run")
    @patch("auto_coder.codex_mcp_client.subprocess.Popen")
    @patch("auto_coder.codex_mcp_client.logger")
    def test_log_jsonrpc_event_redaction(self, mock_logger, mock_popen, mock_run, mock_get_config):
        # Setup mocks
        mock_process = MagicMock()
        mock_popen.return_value = mock_process
        mock_process.stderr = MagicMock()

        # Mock subprocess.run for version check
        mock_chk = MagicMock()
        mock_chk.returncode = 0
        mock_run.return_value = mock_chk

        client = CodexMCPClient()

        # Test data with secret
        secret = "sk-123456789012345678901234567890123456789012345678"
        params = {"input": f"Use this key: {secret}"}
        result = f"Key {secret} used."

        # Call method
        client._log_jsonrpc_event("test_event", "test_method", params, result)

        # Verify logger call
        # info is called multiple times (init logging + event logging)
        # We need to find the call with our JSON log
        found_call = False
        for call_args in mock_logger.info.call_args_list:
            arg = call_args[0][0]
            try:
                log_data = json.loads(arg)
                if log_data.get("type") == "test_event":
                    found_call = True
                    # Check redaction
                    print(f"Logged params: {log_data.get('params')}")
                    print(f"Logged result: {log_data.get('result')}")

                    # In params (which is a dict), we need to check values, or string representation if we dumped it
                    # The new implementation keeps it as a dict
                    params_str = str(log_data["params"])
                    self.assertNotIn(secret, params_str, "Secret leaked in params")
                    self.assertIn("[REDACTED]", params_str, "Redaction marker missing in params")

                    self.assertNotIn(secret, log_data["result"], "Secret leaked in result")
                    self.assertIn("[REDACTED]", log_data["result"], "Redaction marker missing in result")
            except json.JSONDecodeError:
                continue

        self.assertTrue(found_call, "Could not find expected JSON log call")

    @patch("auto_coder.codex_mcp_client.get_llm_config")
    @patch("auto_coder.codex_mcp_client.subprocess.run")
    @patch("auto_coder.codex_mcp_client.subprocess.Popen")
    @patch("auto_coder.codex_mcp_client.logger")
    def test_log_fallback_event_redaction(self, mock_logger, mock_popen, mock_run, mock_get_config):
        # Setup mocks
        mock_process = MagicMock()
        mock_popen.return_value = mock_process
        mock_process.stderr = MagicMock()

        # Mock subprocess.run for version check
        mock_chk = MagicMock()
        mock_chk.returncode = 0
        mock_run.return_value = mock_chk

        client = CodexMCPClient()

        # Test data with secret
        secret = "sk-123456789012345678901234567890123456789012345678"
        cmd = ["codex", "exec", "--key", secret]
        output = f"Output with {secret}"

        # Call method
        client._log_fallback_event(cmd, output, 0)

        # Verify logger call
        found_call = False
        for call_args in mock_logger.info.call_args_list:
            arg = call_args[0][0]
            try:
                log_data = json.loads(arg)
                if log_data.get("type") == "fallback_exec":
                    found_call = True
                    # Check redaction
                    print(f"Logged command: {log_data.get('command')}")
                    print(f"Logged output: {log_data.get('output')}")

                    self.assertNotIn(secret, log_data["command"], "Secret leaked in command")
                    self.assertNotIn(secret, log_data["output"], "Secret leaked in output")
                    self.assertIn("[REDACTED]", log_data["command"], "Redaction marker missing in command")
                    self.assertIn("[REDACTED]", log_data["output"], "Redaction marker missing in output")
            except json.JSONDecodeError:
                continue

        self.assertTrue(found_call, "Could not find expected JSON log call")


if __name__ == "__main__":
    unittest.main()
