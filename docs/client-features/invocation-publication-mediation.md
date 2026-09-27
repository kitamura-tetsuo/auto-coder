# Invocation-local publication and credential mediation

The process supervisor can compose a controller-owned publication transport
enforcer with filesystem confinement before releasing a local backend. The
enforcer receives the unique invocation identity, actual backend and effective
mode, private roots, and the controller-selected provider, model, and exact
inference routes. If any of those inputs cannot be mediated, installation fails
before task submission rather than running with a weaker fallback.

The transport enforcer is responsible for pre-effect rejection across the whole
owned process tree, including direct clients and delegated tools. An allow-listed
inference host is not sufficient: authorization includes scheme, port, and path
prefix so another endpoint or redirect on that host cannot become a publication
route. The enforcer owns its privileged rules and denial observations until the
supervisor positively settles all invocation writers.

The child environment removes caller GitHub tokens, ask-pass programs, forwarded
SSH agents, and caller Git configuration. Git prompting and credential helpers are
disabled explicitly. Provider authentication may be supplied again only through
the installed enforcer's controller-selected environment; credential values are
never placed in installation diagnostics. Runtime auth-file and socket visibility,
direct network mediation, and local-path remote protection remain mandatory
responsibilities of the concrete privileged enforcer and filesystem policy rather
than assumptions inferred from environment cleanup.

Successful installation positively records both publication enforcement and
denial-observation availability on the invocation boundary. A reported denial is
sticky and makes the invocation non-promotable even if the backend later exits
successfully. Missing installation or observation evidence cannot authorize a
confined result. This producer adds no dashboard event or outcome type: it fills
existing local-boundary evidence and uses the existing `pre-start-unavailable`
supervisor result, so the production-to-view observability contract is unchanged.
