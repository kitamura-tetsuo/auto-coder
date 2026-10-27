# Node.js 24 development and CI policy

The Auto-Coder development image installs Node.js 24 from NodeSource while
retaining its existing Python and developer tooling. Active GitHub Actions jobs
that run `npm` or `npx`, either directly or through a checked-in script, provision
Node.js 24 with `actions/setup-node` in that same job before the consuming step.

`scripts/node24_policy.py` validates the development Dockerfile and all active
workflow files. Its regression tests also exercise missing, misplaced, and
incorrect Node.js setup declarations so a runner default cannot silently become
the CI runtime authority.
