from auto_coder.cli import main
import sys
import io
from contextlib import redirect_stdout, redirect_stderr

def check_help() -> None:
    old_argv = sys.argv
    sys.argv = ['auto-coder', '--help']
    captured_output = io.StringIO()
    captured_error = io.StringIO()
    try:
        with redirect_stdout(captured_output), redirect_stderr(captured_error):
            main()
    except SystemExit:
        pass  # Click uses sys.exit() which raises SystemExit
    finally:
        sys.argv = old_argv

    output = captured_output.getvalue()
    print('OUTPUT:')
    print(output)
    print('\nCHECKS:')
    print('Contains Usage:', 'Usage:' in output)
    print('Contains Auto-Coder:', 'Auto-Coder' in output)

if __name__ == "__main__":
    check_help()