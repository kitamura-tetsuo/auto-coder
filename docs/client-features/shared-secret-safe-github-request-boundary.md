# Shared, secret-safe GitHub request boundary

The production GhApi/hishel boundary exposes a typed outcome for each actual
wire attempt and separately records local cache results. Outcomes retain HTTP
status, evidence-based authentication/throttling classification, provenance,
request and attempt identities, safe rate-limit metadata, and delivery
certainty. Optional admission and observation hooks operate at the transport
boundary, including cache revalidation, so a mutation can be refused before it
is sent. Diagnostics are emitted through the existing colored console and
bounded rotating-file loguru sinks without request payloads, credentials,
signed queries, GraphQL documents or arbitrary successful response bodies.
Controller-owned strict reads, startup and pagination reads, authentication
verification, reviewer GitHub App operations, Actions-secret publication, and
Actions artifact API requests use the same process-wide admission/observation
seam while retaining separate credentials and uncached authority semantics.
Every API-origin redirect hop is independently governed. Cross-origin artifact
downloads preserve their bytes but are excluded from GitHub quota observations,
and authorization is not forwarded by the redirecting HTTP client.

If Hishel cache storage or response-body processing raises after the transport
recorded response headers, both CachedGhApi and the direct caching adapter
consume and deliver those known wire observations before propagating the
original exception (including cancellation). Status, delivery certainty and
available header throttle evidence remain intact; no decoded body or successful
caller operation is inferred, no failed request is replayed, and a failure before
wire evidence creates no receipt. Consumed evidence cannot attach to a later
cache hit. The diagnostic event `wire_response_before_cache_failure` includes
only the existing secret-safe wire outcome, with no exception text or payload.

A bounded regression uses actual Hishel SQLite storage and a second real SQLite
write lock acquired after fixture HTTP response headers. It is a production
cache/governor boundary test with fixture HTTP, not an external GitHub test or
proof of any unrecorded historical cache exception. Run
`bash scripts/test.sh tests/test_gh_cache_exception_observations.py`.

The exceptional boundary also marks a retryable original transport exception when
actual response headers were received. The existing outer retry wrapper refuses
to replay that known-response request, including GraphQL review-thread mutations;
the same exception object still propagates. Unobserved raw transport failures
retain the existing bounded retry behavior. The production resolve-review-thread
regression uses the real caching adapter and Governor blocking admission, proves
one mutation send and one matching reservation settlement, then permits only a
separately requested operation. Both adapters' body-failure cases also traverse
the actual outer retry wrapper. This is not a successful mutation receipt or
permission to replay an uncertain operation.

If the failure-path diagnostic sink or observation callback itself raises,
finalization still attempts the observation once and the original request error
or cancellation propagates unchanged. Secondary failures neither trigger a
request replay nor expose their exception text through additional diagnostics.
A callback invocation is not proof of durable settlement if that callback failed
before retaining its evidence. Regression coverage exercises both adapters with
real received headers, a failing body, ordinary exceptions and cancellation from
the observer, optional diagnostic-sink failure, one mutation send, and retained
429 cooldown evidence.
