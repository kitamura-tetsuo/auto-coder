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
