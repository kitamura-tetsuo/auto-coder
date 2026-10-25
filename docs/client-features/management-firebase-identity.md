# Firebase management identity and operator admission

`FirebaseOperatorIdentity` is a synchronous library boundary for trusted management
consumers. It does not expose an HTTP listener or issue OAuth grants. Importing the
management package does not initialize Firebase or affect ordinary CLI/provider
authentication.

Configure an explicit Firebase project ID and a controller-selected UTF-8 policy
file. The intended deployment path is
`~/.auto-coder/management/operators.json`; no file or initial operator is created
automatically. Application Default Credentials must be available to the Firebase
Admin SDK. Production startup must not set `FIREBASE_AUTH_EMULATOR_HOST`.

```json
{
  "project_id": "configured-firebase-project",
  "allowed_uids": ["firebase-uid-of-operator"]
}
```

The policy project must exactly match the configured project and every UID must be
a nonempty string. The complete file is read on every operation. Missing, malformed,
unreadable, or mismatched policy is unavailable; an empty list deliberately admits
nobody. There is no cached-policy, email, or first-user fallback.

The adapter returns typed `PERMITTED`, `DENIED`, `INVALID`, or `UNAVAILABLE`
decisions with bounded reason codes. Only `PERMITTED` carries a principal, consisting
of the configured project, default-namespace Firebase UID, and the login token's
original `auth_time`. Raw tokens and SDK exception payloads are neither returned nor
logged.

`verify_login` cryptographically verifies a Firebase ID token, checks its current
default-namespace user record and policy, and admits only Google sign-in identities.
`recheck_identity` accepts trusted previously verified principal evidence and checks
the current account, original authentication-time revocation boundary, and freshly
read policy. `check_operator_policy` reads only the policy and performs no Firebase
lookup; its success says nothing about current Firebase disablement or revocation.
