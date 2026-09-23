# Deployment reference templates

These files illustrate the original service separation and rollback/backup design.
They are not an installer for the public snapshot. Domains, hosts and signing
identities are placeholders. Review every script and supply your own protected
configuration before any separately authorized deployment.

The API, Finance MCP and DAL use separate service identities and data directories.
The home Worker initiates outbound connections; no home ingress is required.
See [architecture](../docs/overview/architecture.md) and [verification](../docs/verification.md).
No production connection, service operation or deployment was performed to prepare
this public snapshot. Historical operational evidence is not distributed.
