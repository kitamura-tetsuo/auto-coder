# Repository-owned pending-work execution

A serving pending-work scheduler is explicitly bound to the `owner/repository`
target passed to `AutomationEngine.start_automation`. Repository names are compared
after surrounding ASCII whitespace is removed and ASCII letters are lowercased;
malformed names are rejected. The retained identity text is never rewritten.

The bound store view limits due work, interrupted recovery, next-deadline selection,
status snapshots, manual retries, and identity mutations to that repository. The
scheduler also checks ownership immediately before claiming an obligation, while all
six production handlers check it again before repository reads or stage effects.
Foreign records remain untouched in the shared legacy SQLite database and remain
available to a controller bound to their repository.

Ownership refusals identify both the configured and obligation repositories through
the normal Loguru sinks. An unbound scheduler may exist during engine construction,
but cannot enter its serving loop; production installs the immutable binding and all
handlers before starting recovery.
