# Client Features Documentation

This directory is the authoritative location for client-feature documentation.
It replaces the former monolithic `docs/client-features.yaml`, which has been
removed. There is no generated aggregate of this directory's contents, and
none should be created or maintained; this README does not restate or
duplicate any fragment's behavioral documentation.

## What lives here

Each file documents one feature or responsibility as prose, in the same style
as the former monolith. A fragment is independently readable: it starts with
its own top-level `#` heading and does not depend on surrounding fragments to
be understood.

## How to find the relevant fragment(s)

There is no index, category system, status field, or feature-ID scheme to
look up. Discover fragments the same way you discover any other documentation
in this repository:

* Search this directory by filename for the feature, component, or behavior
  name you are working on (filenames are descriptive, e.g.
  `grep -ril <keyword> docs/client-features/` or a filename glob).
* Grep fragment contents for identifiers that appear in the code you are
  changing (module names, config keys, CLI flags, class/function names).
* Follow references from nearby code, tests, or other documentation (such as
  `docs/dashboard-observability.md`) that name a specific fragment or
  behavior.

## How to update documentation

When you change behavior that a fragment documents, edit that fragment
directly. When you add a new feature or responsibility that is not yet
documented, add a new fragment file here with a descriptive filename and its
own top-level heading, following the style of existing fragments. Do not
create a new aggregate document, a generator, or a synchronization step that
mirrors these fragments elsewhere.
