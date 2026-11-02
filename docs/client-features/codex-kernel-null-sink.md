# Confined Codex kernel null sink

Editable and no-edit Codex launches pin the existing kernel `/dev/null`
character device and verify major/minor 1:3 before installing filesystem policy.
Only sink I/O rights are added for that exact device; the `/dev` directory,
`/dev/zero`, persistent outside files, and device replacement/removal receive no
allowance. Missing or incorrect device identity refuses launch. Repository,
runtime-input, caller and peer-workspace authority is unchanged.

This permits real Git and shell sink operations during an editable correction.
CLI success, sink access and filesystem-policy installation do not establish a
correct implementation or grant review/publication/merge authority.

Run `pytest -q tests/test_filesystem_confinement.py tests/test_local_execution_boundary.py`
for the affected policy and evidence boundaries. The real-process sink/Git
regression and incorrect-device rejection both fail for editable mode before the
fix, while no-edit remains a control. The same child attempts protected caller,
peer and outside regular-file mutations and verifies unchanged bytes afterward.

Observability is neutral: the existing filesystem installation/violation and
writer-settlement producers remain responsible for actual evidence; no event
schema or dashboard authority is added. A denied protected write remains a
recorded denial and cannot be converted into implementation success.
