# Local authentication and user administration

AG-003 provides local SOEID authentication on the trusted portal origin. Accounts are
administrator-provisioned; self-registration, email recovery, MFA, directory synchronization,
and reverse-proxy SSO are not enabled.

## Identity boundary

The only application identity key is the SOEID stored on `persistence.User`. Login and
administrator forms pass their value through the same canonicalizer: surrounding ASCII
whitespace is trimmed, ASCII letters are uppercased, and the value must match
`^[A-Z0-9][A-Z0-9._-]{0,63}$`. The database stores only that canonical form and enforces
uniqueness.

Agora never selects an authenticated user from a hidden field, URL, cookie value, forwarded
identity, `REMOTE_USER`, `X-SOEID`, or another request header. The portal uses Django's local
password backend only. The content composition has no session or authentication middleware and
cannot consume a portal session.

## First administrator bootstrap

Run the bootstrap command after applying migrations and only from a protected operator terminal:

```powershell
uv run python manage.py bootstrap_admin --soeid ASSIGNED_SOEID
```

The command prompts for the password and confirmation without echoing them. It accepts no
password argument, refuses to run once any `User` row exists (including disabled users), and
serializes concurrent attempts with a PostgreSQL transaction advisory lock. The success message
does not repeat the SOEID or password. If bootstrap is no longer available, use an approved,
separately reviewed break-glass procedure; do not add a web bootstrap route.

## Administrator workflow

After signing in, an active administrator can use `/admin/users/` to:

- create a regular or administrator account with a validated password;
- list canonical SOEIDs and active/disabled status;
- disable another account after an explicit confirmation;
- re-enable a retained account; and
- replace a user's password.

Passwords are hashed with Django's configured framework hasher and are never rendered, logged,
placed in a URL, or stored in an audit event. Passwords must be at least 12 characters and pass
the configured Django validators. Initial and replacement passwords must be delivered through an
approved secure channel outside Agora.

The current administrator cannot disable their own account. The service locks active
administrator rows in a stable order before checking the count, so concurrent requests cannot
disable the last active administrator. A disabled account is rejected by Django authentication
and becomes anonymous on its next request. An authentication version is advanced on disable,
re-enable, and password reset; therefore a stale session cannot become valid again after an
account is re-enabled. This is the documented session-revocation window: one subsequent request
through the portal.

## Sessions, CSRF, and abuse controls

Portal sessions use a `__Host-` cookie with no `Domain`, `Path=/`, `Secure`, `HttpOnly`, and
`SameSite=Lax` attributes. The CSRF cookie uses the same host-only and transport protections.
Successful login rotates the anonymous session and Django rotates the CSRF token. Logout is a
CSRF-protected POST that flushes the session; GET never logs a user out. Login `next` values are
accepted only as relative portal paths. Absolute, scheme-relative, content-origin, control-
character, and backslash variants fall back to the portal home.

Failed login submissions are intentionally generic for unknown, malformed, wrong-password, and
disabled accounts. The portal does not trust proxy forwarding headers when determining the
source. Database-backed throttle buckets use HMAC digests rather than raw SOEIDs or addresses:
five failures in a fifteen-minute window trigger a one-minute bucket block, with separate source
and known-account buckets. A successful login clears its buckets. Failed attempts are not copied
into the append-only identity audit stream, which avoids making the audit table an attacker-sized
request log.

## Audit and logging boundary

The identity service writes fixed, metadata-free events for bootstrap, successful login, logout,
provisioning, disablement, re-enablement, and password reset. Events retain actor and target
relationships but never include passwords, password hashes, session identifiers, CSRF tokens,
cookies, authorization headers, raw form values, addresses, or request bodies. Application code
must continue to avoid logging credential-bearing request data.

## Deferred SSO boundary

Reverse-proxy SSO remains deferred. No middleware consumes `REMOTE_USER`, `X-Forwarded-*`, or a
firm identity header, and no header can replace the local authenticated principal. A future SSO
implementation must be a separately reviewed identity adapter with an explicit trusted proxy
configuration, canonical SOEID mapping, lifecycle handling, session rotation, audit events, and
tests for header spoofing and proxy bypass before it is enabled.

Browser authentication requires TLS. Local HTTP remains suitable only for non-authenticated
foundation checks; production configuration already requires HTTPS origins.
