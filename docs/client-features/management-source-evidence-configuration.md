# Operator configuration of management source evidence

The immutable management YAML and fresh-initialization intent remain unchanged.
An optional operator-owned file named `<management-yaml-name>.source-evidence.json`
in the same directory configures only source-evidence repository/credential reads.
It is not a general configuration revision, lifecycle authorization, source
eligibility receipt or readiness result. Original generation, mounted-root,
runtime, prior-owner fence and OAuth/current/prior journal checks still apply.

The extension has exactly these fields:

```json
{
  "schema_version": 1,
  "management_config_sha256": "exact-original-YAML-sha256",
  "source_repository": "kitamura-tetsuo/auto-coder",
  "source_evidence_token_file": "/bootstrap/source_evidence_read"
}
```

The token path must be `source_evidence_read` in the YAML's own directory. No
environment variable, alternate repository, executable, URL, OAuth property,
policy or journal path can be supplied. Both settings are required together.
An extension conflicts with either inline source setting, including an explicit
null. Without an extension the existing inline configuration remains supported.
Deleting an extension disables this optional path on the next load; it never
creates default credentials or enables unauthenticated source deployment.

All extension inputs must have canonical nonsymlink paths, regular single-link
files owned by the process's Unix operator, and operator-owned parent directories
without group/world write permission. Extension and token modes must be exactly
0600; base YAML must not be group/world writable. The reader opens the token to
verify readability and metadata but does not read its contents. The existing
source-evidence client remains responsible for reading the raw token, GitHub
authentication and exact source proof validation. Malformed/duplicate/unknown
fields, stale YAML binding, missing token, or insecure inputs fail settings load
closed. Do not log configuration contents or credential values.

## Preparation and activation

First deploy a reviewed bootstrap/application containing this extension using
the ordinary immutable-image deployment procedure. An older bootstrap cannot
learn this feature from a new JSON file. Its fixed application closure cannot be
hot-patched, and its original initialization intent must never be rewritten.
For a fresh-instance cutover, independently review OAuth retention, current and
all prior operation journals, owner fencing, worker identity preservation and
proxy namespace attachment. This feature does not implement that cutover.

Under the approved host credential-provisioning procedure, create the raw token
at the fixed destination without exposing it in arguments, output or logs. Prefer
a dedicated token with Contents/Actions/Metadata read access only to auto-coder;
reusing a wider token requires explicit operator acceptance of its wider access.
The following command only prints non-secret prepared JSON and makes no writes:

```sh
python -I -B -m auto_coder.management.source_evidence_config \
  --config /bootstrap/management.yml \
  --repository kitamura-tetsuo/auto-coder \
  --token-file /bootstrap/source_evidence_read
```

Run preparation against canonical paths as seen by the management process, or
stage the same container-visible layout offline. Preserve the exact prepared
bytes for review. Install as `management.yml.source-evidence.json`, owner matching
the serving process, mode 0600, through the approved protected host configuration
procedure. Keep the directory bind read-only to the service and avoid shadowing
mounts. Host/container paths differ: do not substitute a host path in the JSON.
Do not overwrite or edit management.yml or any initialization/state record.

The extension is read at settings load, not hot-reloaded. After separate activation
approval, drain and restart only management through the supported lifecycle;
maintain the proxy attachment and retain all state/credential files. Check the
protected 401 challenge, authenticated whoami, unchanged worker supervisor/run/
deployment identities and exact source evidence readback. Preparation, process
startup and successful GitHub authentication alone do not prove deployed-beta
health. No worker restart or beta deployment is performed by this feature.

For rollback, remove the optional extension through the approved operator
procedure and reactivate management with the original YAML. Preserve the extension
as protected rollback evidence and retain the token rather than printing/copying
it. Never clear initialization, OAuth, journal, watcher or operation records.

This configuration path emits no worker scheduler/provider/dashboard processing
events; it only supplies settings before the existing source reader and typed
deployment admission. It does not change the dashboard trace schema.
