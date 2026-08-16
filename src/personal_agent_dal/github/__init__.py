"""GitHub intake and facts-adapter boundaries (DAL-014/015, G2).

The two operation handlers in this package are the Development Agent Loop's
GitHub-facing business policy, kept outside ADK and outside any model SDK:

- :mod:`personal_agent_dal.github.webhook` — ``OP-GH-EVENT-001``
  (``accept_github_intake``): judge a webhook/poll event against the single-repo
  permission matrix, refusing forks, unknown repositories, unknown senders,
  edited events and replayed deliveries with zero writes.
- :mod:`personal_agent_dal.github.git_base` — ``OP-GIT-BASE-001``
  (``verify_git_mutation_preconditions``): judge a git read-back against the
  approved base/PR-head SHA and the clean-index/clean-worktree preconditions,
  surfacing drift and conflict as a block signal.

Both are pure policy: they validate a closed input shape and return a decision.
The transition they imply (``block_feature``) is applied by the trusted resolver
and the deterministic engine, never by these modules.
"""
