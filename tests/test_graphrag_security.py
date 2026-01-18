import os
import subprocess
import unittest
from unittest.mock import MagicMock, mock_open, patch

# Import the function to test
# We need to mock the imports that might fail in the test environment
with patch("src.auto_coder.logger_config.get_logger", return_value=MagicMock()):
    from src.auto_coder.cli_commands_graphrag import run_graphrag_setup_mcp_programmatically


class TestGraphRAGSetupSecurity(unittest.TestCase):

    @patch("src.auto_coder.cli_commands_graphrag.subprocess.run")
    @patch("src.auto_coder.cli_commands_graphrag.Path")
    @patch("src.auto_coder.cli_commands_graphrag.shutil")
    @patch("src.auto_coder.cli_commands_graphrag.os")
    @patch("urllib.request.urlopen")
    @patch("tempfile.NamedTemporaryFile")
    def test_uv_installation_avoids_shell_true(self, mock_tempfile, mock_urlopen, mock_os, mock_shutil, mock_path, mock_subprocess_run):
        # Setup mocks to trigger the uv installation path

        # 1. First subprocess.run check for 'uv --version' should fail
        def side_effect(*args, **kwargs):
            cmd = args[0]
            if cmd == ["uv", "--version"]:
                raise FileNotFoundError("uv not found")
            # This captures the execution of the downloaded script
            return MagicMock(returncode=0)

        mock_subprocess_run.side_effect = side_effect

        # Mock temp file creation
        mock_temp_obj = MagicMock()
        mock_temp_obj.name = "/tmp/mock_installer.sh"
        mock_tempfile.return_value.__enter__.return_value = mock_temp_obj

        # Mock file existence for cleanup
        mock_os.path.exists.return_value = True

        # Run the function
        run_graphrag_setup_mcp_programmatically(silent=True, skip_clone=True)

        # Verify urllib was used to download
        mock_urlopen.assert_called()
        self.assertIn("astral.sh/uv/install.sh", mock_urlopen.call_args[0][0])

        # Verify subprocess.run was called for the script
        calls = mock_subprocess_run.call_args_list
        script_exec_found = False
        shell_false_verified = False

        for call in calls:
            args, kwargs = call
            cmd = args[0]
            if isinstance(cmd, list) and "/tmp/mock_installer.sh" in cmd and cmd[0] == "sh":
                script_exec_found = True
                # Ensure shell=True is NOT passed (default is False)
                if not kwargs.get("shell"):
                    shell_false_verified = True

        if not script_exec_found:
            print("Test failed: Executed script call not found in subprocess calls.")
            # print([c[0][0] for c in calls])

        self.assertTrue(script_exec_found, "Should attempt to run the downloaded script with sh")
        self.assertTrue(shell_false_verified, "Should NOT use shell=True for the script execution")

        # Verify chmod was called to make it executable
        mock_os.chmod.assert_called_with("/tmp/mock_installer.sh", 0o700)

        # Verify cleanup
        mock_os.unlink.assert_called_with("/tmp/mock_installer.sh")


if __name__ == "__main__":
    unittest.main()
