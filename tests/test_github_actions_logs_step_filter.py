import io
import json
import zipfile
from unittest.mock import Mock, patch

from src.auto_coder.automation_engine import AutomationEngine


def _make_zip_bytes(files):
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return bio.getvalue()


def test_only_failing_step_is_output_when_available(mock_github_client, mock_gemini_client):
    engine = AutomationEngine(mock_github_client, mock_gemini_client, dry_run=True)
    # URL components extracted by get_github_actions_logs_from_url
    owner = 'kitamura-tetsuo'
    repo = 'outliner'
    run_id = '17006383413'
    job_id = '48216559181'
    url = f"https://github.com/{owner}/{repo}/actions/runs/{run_id}/job/{job_id}"

    # Prepare a ZIP with multiple step logs; only the failing step should be emitted
    files = {
        '1_Setup Node.js.txt': 'Setup done\nAll good\n',
        '2_e2e tests for github reporting.txt': (
            'Run e2e tests for github reporting\n'
            'Error:   1) [report] › e2e/report/github-reporting.spec.ts:10:1 › reports to GitHub\n\n'
            '    Error: expect(received).toContain(expected)\n\n'
            '    Expected substring: "GITHUB_STEP_SUMMARY"\n'
            '    Received string:    "-- no summary --"\n'
        )
    }
    zip_bytes = _make_zip_bytes(files)

    def fake_run(cmd, capture_output=True, text=False, timeout=60, cwd=None):
        # jobs list for run -> include target job
        if cmd[:3] == ['gh', 'run', 'view'] and '--json' in cmd:
            jobs_obj = {'jobs': [{'databaseId': int(job_id), 'name': 'CI / e2e', 'conclusion': 'failure'}]}
            return Mock(returncode=0, stdout=json.dumps(jobs_obj), stderr='')
        # job details -> include steps with one failure
        if cmd[:2] == ['gh', 'api'] and cmd[2].endswith(f'actions/jobs/{job_id}'):
            job_obj = {
                'id': int(job_id),
                'name': 'CI / e2e',
                'conclusion': 'failure',
                'steps': [
                    {'name': 'Setup Node.js', 'status': 'completed', 'conclusion': 'success'},
                    {'name': 'e2e tests for github reporting', 'status': 'completed', 'conclusion': 'failure'},
                ],
            }
            return Mock(returncode=0, stdout=json.dumps(job_obj), stderr='')
        # job logs (zip)
        if cmd[:2] == ['gh', 'api'] and cmd[2].endswith(f'actions/jobs/{job_id}/logs'):
            return Mock(returncode=0, stdout=zip_bytes, stderr=b'')
        return Mock(returncode=1, stdout=b'' if not text else '', stderr=b'unknown')

    with patch('subprocess.run', side_effect=fake_run):
        out = engine.get_github_actions_logs_from_url(url)

    assert '=== Job CI / e2e' in out
    # Should include failing step section
    assert 'e2e tests for github reporting' in out
    assert 'Expected substring: "GITHUB_STEP_SUMMARY"' in out
    # Should NOT include non-failing step section name anywhere
    assert 'Setup Node' not in out

