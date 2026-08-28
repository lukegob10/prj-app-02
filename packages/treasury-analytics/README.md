# Treasury Analytics local stand-in

This development-only package mirrors the environment-installed
`treasury_analytics.TAConnection` API. It owns the non-secret `PROD` connection
profile and reads the password from `TA_PROD_PASSWORD`.

Managed deployments install the corporate package with the same import and API;
that package owns profiles such as `SDLC`. Agora supplies only `ENV` and the
package's expected runtime credential.
