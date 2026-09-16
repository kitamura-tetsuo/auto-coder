# Process-local specification-validation diagnostics

Standalone intake, normal and explicit worker processing, retained-owner
reevaluation, pending-work resumption, and parent/direct-child scheduling route
individual specification jobs through the same structured diagnostic boundary.
Each producer owns a validation execution, and every waiting caller records an
observation referencing the exact decision identity. READY, BLOCKED, ERROR,
cancellation, and disabled bypass evidence remains distinct. The bounded trace is
observational and process-local; it neither changes validation behavior nor
provides restart-readable review history.
