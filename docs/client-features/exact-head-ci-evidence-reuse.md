# Exact-head CI evidence reuse

Adversarial validation receives the production exact-head CI observation and
uses verified `.github/workflows/pr-tests.yml` success to avoid a redundant
whole-suite check. Dynamic test targets are restricted to `all` or existing
Python files/directories under `tests/`, with optional pytest node selectors;
prose and unsafe targets never reach the test runner.
The initial review and any target-protocol correction receive the immutable CI
subject, read-cycle identity, availability, and independently identified
workflow/check facts rather than an aggregate green/red label. A missing pytest
node or zero-selection exit follows the same single bounded correction path as
a pre-launch target rejection, while collection/import failures and executed
test failures retain their distinct meanings.
The one target-correction budget spans both pre-launch validation and runtime
pytest selection. A corrected but unselectable node therefore terminates as a
protocol error rather than opening a second correction, and a PASS after that
round trip fails closed when current authoritative CI evidence is unavailable.
Focused reruns are suppressed only when a current pass-eligible canonical
workflow fact explicitly reports the exact requested target as successful;
aggregate suite success never implies that target-level fact.
