# Nginx examples

The `agent.example.invalid*.conf` files are non-deployable examples. Configure
your own domain, certificates, upstream services and authentication first.
`default-deny-443.conf` illustrates rejecting unrecognized hosts; the upstream
snippets illustrate API and DAL separation. Loopback is not authentication.
Do not run certificate or deployment scripts as an offline verification step.
