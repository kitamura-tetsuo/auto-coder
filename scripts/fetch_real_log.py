#!/usr/bin/env python3
"""Script to fetch actual GitHub Actions logs"""

import io
import subprocess
import sys
import zipfile


def fetch_job_logs(job_id: str):
    """Fetch logs for the specified job_id"""
    cmd = ["gh", "api", f"repos/kitamura-tetsuo/outliner/actions/jobs/{job_id}/logs"]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60)

        if result.returncode != 0:
            print(f"Error fetching logs: {result.stderr.decode()}", file=sys.stderr)
            return None

        # First try to parse as a ZIP file
        try:
            with zipfile.ZipFile(io.BytesIO(result.stdout), "r") as zf:
                print(f"ZIP contains {len(zf.namelist())} files:")
                for name in zf.namelist():
                    info = zf.getinfo(name)
                    print(f"  - {name} ({info.file_size} bytes)")

                # Display contents of each file
                for name in zf.namelist():
                    if name.lower().endswith(".txt"):
                        print(f"\n{'='*80}")
                        print(f"File: {name}")
                        print("=" * 80)
                        with zf.open(name, "r") as fp:
                            content = fp.read().decode("utf-8", errors="ignore")
                            # Display first 100 lines and last 100 lines
                            lines = content.split("\n")
                            if len(lines) <= 200:
                                print(content)
                            else:
                                print("\n".join(lines[:100]))
                                print(f"\n... ({len(lines) - 200} lines omitted) ...\n")
                                print("\n".join(lines[-100:]))

                return result.stdout
        except zipfile.BadZipFile:
            # If not ZIP, treat as text
            print("Response is plain text, not ZIP")
            content = result.stdout.decode("utf-8", errors="ignore")
            lines = content.split("\n")
            print(f"Total lines: {len(lines)}")

            # Display first 100 lines and last 100 lines
            if len(lines) <= 200:
                print(content)
            else:
                print("\n".join(lines[:100]))
                print(f"\n... ({len(lines) - 200} lines omitted) ...\n")
                print("\n".join(lines[-100:]))

            return result.stdout

    except subprocess.TimeoutExpired:
        print("Error: Command timed out", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return None


if __name__ == "__main__":
    job_id = "53715705095"
    if len(sys.argv) > 1:
        job_id = sys.argv[1]

    print(f"Fetching logs for job {job_id}...")
    fetch_job_logs(job_id)
