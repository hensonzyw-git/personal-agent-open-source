# Deployment reference templates

These files illustrate the original service separation and rollback/backup design.
They are not an installer for the public snapshot. Domains, hosts and signing
identities are placeholders. Review every script and supply your own protected
configuration before any separately authorized deployment.

The Mac-side code deployment script requires `ECS_HOST` (host or IP) and
`ECS_SSH_KEY` (path to a protected SSH key); `DEPLOY_USER` defaults to the neutral
example account `deploy`. Set `DEPLOY_USER` consistently for installation and
verification on your own server. These placeholders do not grant access to the
author's infrastructure.

The API, Finance MCP and DAL use separate service identities and data directories.
The home Worker initiates outbound connections; no home ingress is required.
See [architecture](../docs/overview/architecture.md) and [verification](../docs/verification.md).
No production connection, service operation or deployment was performed to prepare
this public snapshot. Historical operational evidence is not distributed.
