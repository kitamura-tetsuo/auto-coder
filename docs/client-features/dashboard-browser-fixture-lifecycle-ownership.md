# Dashboard browser fixture lifecycle ownership

The slots browser regressions keep a real module dashboard server active while
some cases need a separate engine, durable slot store and listening socket.
NiceGUI owns one process-global event loop, task registry and slot stack. A
second `Server.run` thread cannot provide independent NiceGUI state: startup
replaces the global loop, pruning can discard another loop's active slots and
shutdown can cancel the parent fixture's tasks.

A temporary server borrows an already running, started NiceGUI lifecycle and
runs its own Uvicorn serving task on that same owner loop. Only the additional
socket has lifespan disabled; the existing owner already performed startup and
retains shutdown responsibility. With no running owner, the standalone fixture
owns ordinary startup and shutdown. Fixture configuration and restoration run
on the owner loop, preserving the parent's route and middleware registrations.
Setup and teardown retain finite bounds and fail when the server does not start
or stop. No pruning, watchdog, browser requirement or timeout is relaxed.

`test_standalone_server_preserves_parent_loop_and_live_client` drives real
Chromium pages for the parent and temporary repository, checks the actual loop
identity, closes the temporary server, then observes new slot data in the
original live parent client and reloads it against the restored parent routes.
The original fixture fails the loop-identity assertion. The pre-existing scroll,
DOM identity, rendering rollback, single-flight and unavailable-state recovery
assertions remain intact.

Run with provisioned Chromium and `AUTO_CODER_REQUIRE_BROWSER=1`:
`bash scripts/test.sh tests/test_dashboard_detail_scroll_stability.py tests/test_dashboard_slots_scroll_stability.py`.
These bounded results establish harness and rendering behavior; they do not
replace full source/publication CI or diagnose the separate native exit139.
Production dashboard behavior, event schema, admission and public projections
are unchanged because only test fixture lifecycle ownership is modified.
