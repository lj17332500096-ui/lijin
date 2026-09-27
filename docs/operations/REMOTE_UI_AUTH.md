# Remote UI/API authentication

The web host is local-only by default. To bind it outside loopback, configure all of the following:

1. Set `FORGE_ALLOW_NONLOCAL_UI=1`.
2. Set `FORGE_UI_API_TOKEN` to a private token with at least 32 characters.
3. Terminate TLS at a trusted reverse proxy or equivalent network boundary. Do not send Basic/Bearer credentials over cleartext HTTP on an untrusted network.
4. Start the host with the intended non-loopback `--host` address.

The server rejects non-loopback startup if the opt-in or sufficiently long token is missing. When remote mode is enabled, the application checks every HTTP request, including HTML/static assets and API routes. API clients may use `Authorization: Bearer <token>`. Browsers may use HTTP Basic authentication with any username and the configured token as the password; the browser will prompt after the 401 challenge. `user_id` and `X-Forge-User` remain session labels and are not credentials.

Keep the token outside source control and logs. Rotating the environment value takes effect after restarting the host. Local loopback mode does not require this token.
