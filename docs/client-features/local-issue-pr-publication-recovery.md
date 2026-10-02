# Local Issue PR publication recovery

After local implementation, a clean working tree does not imply that a pull
request has been published. If there are no new changes to commit, Auto-Coder
fetches the intended PR base from origin and checks the effective diff from that
base's merge point to HEAD. When existing committed changes remain, it pushes
the work branch through the centralized Git helper and creates the missing PR
using the normal Issue PR publication path. An existing PR is reused and retains
implementation ownership just like a newly created PR.

Failed fetches, diff inspection, or pushes never authorize PR creation. If the
branch has no effective diff, Auto-Coder reports that a PR cannot be created and
leaves the Issue open; it does not manufacture an empty PR. Implementation prose
such as "already implemented", "invalid", or "closed" never closes an Issue or
produces a fabricated close/comment action. The PR's closing directive remains
the normal completion mechanism when the implementation is merged.

The existing `issue.local-commit-push` stage reports `COMPLETED` after existing
changes are pushed, `FAILED` on publication preparation errors, and `SKIPPED`
for an empty diff. An empty diff also emits `issue.pr-publication` as `BLOCKED`
with its reason. PR creation/reuse retains the existing publication-stage
outcomes and trace schema. Regression coverage is in
`tests/test_issue_processor.py::TestKeepLabelOnPRCreation`.
