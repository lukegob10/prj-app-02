# Browser-security verification baseline

This document records the NOR-11/NOR-15 browser-security fixture slice. It characterizes the
same policy primitives used by the artifact-delivery implementation. Production now has exact
authorized HTML/CSV routes in front of a catch-all `404`; the browser fixtures intentionally do
not create real uploads or render credentials, while Django integration tests cover that server
authorization and delivery surface.

## Stack and execution

The suite uses the official Playwright Python pytest plugin with the exact package versions
locked in `pyproject.toml` and `uv.lock`:

- `playwright==1.62.0`
- `pytest-playwright==0.9.0`
- bundled Chromium, installed with `python -m playwright install chromium`

The fixture server binds only to loopback and exposes three distinct hostnames: the trusted
portal, the content origin, and an attacker/sink origin. Chromium resolves those names to
loopback through a launch argument, so a developer hosts-file edit is not needed for this suite.
The fixture code records only method, path, and cookie presence; it never records request bodies,
query strings, uploaded bytes, or credentials.

Run the focused suite with:

```powershell
uv run --locked pytest tests/browser --browser chromium --no-cov
```

The ordinary `scripts/check.py` gate also runs `pytest`, which includes the browser tests after
the pinned Chromium binary has been installed. CI performs that install before the same gate.

## Browser-proven guarantees

| Boundary | Verification |
|---|---|
| Portal/content separation | The portal fixture frames a distinct content hostname and the content response uses the exact portal `frame-ancestors` origin. |
| Iframe privilege | The production helper emits `sandbox="allow-scripts"` and `referrerpolicy="no-referrer"`; it does not emit `allow-same-origin` or other capability tokens. |
| Response policy | The content response overwrites weaker headers with the approved CSP, `private, no-store`, `nosniff`, `no-referrer`, restrictive `Permissions-Policy`, and DNS-prefetch-off headers. Content never emits `X-Frame-Options`. |
| Clickjacking | `frame-ancestors 'none'` plus `X-Frame-Options: DENY` prevents the attacker fixture from embedding the portal; the content policy rejects an attacker ancestor while allowing the portal ancestor. |
| Portal DOM and identity | The hostile document cannot mutate the portal DOM; a host-only portal cookie is not sent to the content host; content response cookies are stripped. |
| Navigation and capability restrictions | Hostile attempts at top navigation, popups, forms, nested frames, workers, and service-worker registration fail or produce no sink request. |
| Common exfiltration | The attacker sink receives no requests from external script/style/image/font/media, CSS URLs, fetch, XHR, WebSocket, EventSource, beacon, prefetch, form, popup, navigation, or worker attempts. |
| Hostile-document storage | Two sandboxed hostile documents cannot share Web Storage, Cache Storage, or IndexedDB state; opaque-origin cookie access is denied/empty and worker creation is blocked. |
| Revision CSV access | An opaque sandbox can fetch a Revision-relative CSV only when the authorized CSV response returns exact `Access-Control-Allow-Origin: null`; no credentialed or wildcard CORS is enabled. |
| Default deny | Django client coverage exercises arbitrary content paths, wrong audiences, altered/expired/revoked credentials, missing CSV names, and unsupported methods against the production URLconf. |

The browser observations are intentionally behavioral. For example, a sandboxed Chromium document
may serialize `location.origin` as its URL origin while its cookie/storage access throws because
the `allow-same-origin` flag is absent. The suite asserts those enforceable storage and DOM
boundaries plus the actual request headers. `Origin: null` is only a narrowly allowed transport
condition after the server has independently authorized the render token and exact CSV; it is
never treated as authority.

## Residual risks and non-claims

- CSP and sandboxing are browser policy layers, not a network firewall. WebRTC/STUN, DNS effects,
  browser bugs, extensions, proxy behavior, and OS-level egress are not proven absent by an HTTP
  sink test. `X-DNS-Prefetch-Control: off` is a defense-in-depth hint, not a DNS boundary.
- Permissions Policy denies the listed device capabilities; it does not make generic WebRTC
  network isolation a guarantee. Hard no-egress requirements need browser policy, a proxy,
  firewall, or isolated networking.
- No browser CPU or memory quota is promised. Infinite loops, huge DOMs, and allocation abuse
  require future size limits, outer stop/reload recovery, and deployment-level containment.
- This initial suite runs bundled Chromium only. Firefox/WebKit coverage must be added to the
  explicit supported-browser matrix with their own hostname/TLS setup before claiming
  cross-engine acceptance.
- The fixture server intentionally does not prove artifact authorization, CSV MIME handling,
  render-token expiry/revocation, or publication behavior. Oracle-backed integration tests now
  prove those server decisions, but a production-route browser test remains before claiming the
  full multi-browser AG-007/AG-011 acceptance matrix.

The normative policy source remains [`docs/threat-model.md`](./threat-model.md), especially its
future content response policy and residual-risk sections. The sandbox and origin rules are based
on the [WHATWG HTML iframe sandbox specification](https://html.spec.whatwg.org/multipage/iframe-embed-object.html)
and [W3C CSP Level 3](https://www.w3.org/TR/CSP3/).
