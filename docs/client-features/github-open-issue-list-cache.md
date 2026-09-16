# GitHub open Issue list cache

Complete, unfiltered open Issue lists are cached per repository for one hour
(3600 seconds). At one hour, the next request refreshes the list. Label-filtered
queries continue to bypass this cache, and existing mutation invalidation remains
active. This TTL change is observability-neutral: it changes only list reuse
duration, with no changes to admission gates, trace schemas, processing outcomes,
or durable retry deadlines.
