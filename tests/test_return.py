import contextlib
import io
import platform

from aider.coders import Coder
from aider.io import InputOutput
from aider.models import Model


def test_return_value(monkeypatch):
    print("Testing return value...")

    # Mock platform.platform to avoid subprocess calls
    monkeypatch.setattr(platform, "platform", lambda: "Linux-Test-Mock")

    io_obj = InputOutput(yes=True)
    model = Model("gpt-3.5-turbo")
    fnames = []

    # Mock Model.send to return a fixed string
    def mock_send(*args, **kwargs):
        return "This is a mocked response."

    model.send = mock_send

    # Capture stdout just in case
    with contextlib.redirect_stdout(io.StringIO()):
        coder = Coder.create(main_model=model, fnames=fnames, io=io_obj, map_tokens=0)
        # We need to set up the coder to actually run.
        # For 'help', it bypasses LLM. We need a real prompt.
        # But without API key, it might check.
        # However, we mocked send.

        # We also need to mock coder.check_model_availability or similar if it checks on init.
        # Coder.create might check.

        ret = coder.run("Hello", preproc=False)  # preproc=False to skip git checks etc if possible?

    print(f"Return value type: {type(ret)}")
    if ret:
        print(f"Return value length: {len(ret)}")
        print("Return value content:", ret)
    else:
        print("Return value is None or empty")


if __name__ == "__main__":
    # Note: running this directly won't work with monkeypatch fixture
    # Use pytest tests/test_return.py
    pass
