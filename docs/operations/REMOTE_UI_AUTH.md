# Remote UI/API authentication

The web host is local-only by default. To bind it outside loopback, configure all of the following:

1. Set `FORGE_ALLOW_NONLOCAL_UI=1`.
2. Configure either one shared token or a separate token for every operator.
3. Terminate TLS at a trusted reverse proxy or equivalent network boundary. Do not send Basic/Bearer credentials over cleartext HTTP on an untrusted network.
4. Start the host with the intended non-loopback `--host` address.

The server rejects non-loopback startup if the opt-in or a sufficiently long token is missing. When remote mode is enabled, the application checks every HTTP request, including HTML/static assets and API routes. API clients may use `Authorization: Bearer <token>`. For a shared-token deployment, set `FORGE_UI_API_TOKEN` to a private token with at least 32 characters. Every holder of that token is deliberately treated as the same `default` principal; this mode does not isolate users from each other.

For a multi-user deployment, configure a distinct token per operator with `FORGE_UI_API_TOKENS`, as a JSON object whose keys are principal names and whose values are private tokens of at least 32 characters, for example `{"alice":"<alice-secret>","bob":"<bob-secret>"}`. A Bearer token maps to its configured principal. HTTP Basic uses the principal name as username and that principal's token as password. Run cancellation and stream lookup/resume/delete are scoped to this authenticated principal. Caller-supplied `user_id` and `X-Forge-User` remain session labels and cannot grant ownership.

Keep the token outside source control and logs. Rotating the environment value takes effect after restarting the host. Local loopback mode does not require this token.
