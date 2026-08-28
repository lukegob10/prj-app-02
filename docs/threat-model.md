# Foundation threat model

Status: approved baseline with persistence, upload, preview, and isolated-delivery controls
Review trigger: any change to origins, identity, authorization, artifact delivery, storage,
upload limits, or render authorization

## Security objective

An authorized user may use a trusted portal shell, while uploaded HTML and CSV remain hostile,
private, immutable artifacts. Hostile content must not gain portal identity, portal API access,
another Dashboard's data, or filesystem paths. Agora reduces browser exfiltration channels, but
does not claim that CSP alone can provide a complete network-isolation boundary for arbitrary
JavaScript.

## Assets and actors

Protected assets are portal sessions, canonical identity, authorization state, Dashboard
metadata, unpublished and Published HTML/CSV, storage keys/paths, render credentials, secrets,
and service availability. Attackers include unauthenticated users, unrelated authenticated
users, malicious owners/authors, malicious granted viewers, compromised uploaded content, and
accidental operator misconfiguration.

Every browser value, identifier, filename, multipart field, uploaded byte, content-origin
script, and render credential is untrusted. Administrator status is not assumed to grant
Dashboard content access.

## Non-negotiable invariants

1. Uploaded HTML executes only as a content-origin navigation. It is never a portal response,
   portal template value, DOM injection, `srcdoc`, or same-origin `blob:`/`data:` document.
2. Portal sessions are host-only and are never required, sent deliberately, or honored by the
   content service.
3. Content authorization cannot authorize portal APIs or a different User, Dashboard, Revision,
   filename, or storage key.
4. Every HTML and CSV fetch is authorized server-side. Identifier entropy is defense-in-depth.
5. Content exposes only narrow read-only GET/HEAD endpoints; portal owns identity and mutations.
6. Filenames are logical display names only. Adapter-generated keys and root-containment checks
   own storage addressing.
7. Artifacts remain private and outside public/static roots; a raw storage URL never exists.
8. CORS is never wildcarded, cookies are never domain-scoped across portal/content, and uploaded
   bytes cannot relax the response-enforced CSP/sandbox.
9. Logs exclude credentials, cookies, authorization headers, render tokens, raw HTML/CSV,
   connection secrets, and sensitive query strings.
10. A Viewer of a Published Revision may retrieve every attached CSV by product contract.
11. Hostile Revisions must not share durable browser storage or ambient credentials. A single
    shared content origin with `allow-same-origin` is not an approved isolation design.
12. Tags, Favorites, viewer state, access requests, and analytics summaries never grant access;
    every read intersects retained state with current project-scoped authorization.
13. Ownership changes only through the atomic transfer service. New Revision and Grant actions
    require the current owner, while immutable historical actor attribution survives transfer.
14. Raw Authorized Opens contain only the authorization key and project/User/release scope. They
    are never a portal/admin query source and cannot contain browser, network, or content telemetry.

## Project-sharing authorization matrix

`Dashboard` is the security boundary. Owners receive implicit management rights only for their
own active account. An active Viewer receives only the current pinned Published Revision of a
Dashboard with an unrevoked grant; the grant does not expose drafts, previews, arbitrary
Revisions, or owner metadata. Revoked, disabled, unrelated, administrator-without-grant, and
unauthenticated principals fail closed, and external project lookups use the same generic not-found
behavior to avoid enumeration. An owner may see retained grant history, including a disabled
target, but that history does not restore access.

Disabling an owner removes that owner's management and viewing access but does not implicitly
unpublish the Dashboard or revoke access for other active granted viewers.

Revoke, disable, and unpublish state are rechecked on every HTML and CSV authorization request.
An already-issued render credential therefore fails at its next authorization check. This cannot
recall bytes that were already streamed to a browser, which is an explicit residual risk.

## Threat register

| Threat | Required controls | Verification / owning ticket |
|---|---|---|
| **Uploaded HTML crosses into portal** | Separate sites and service compositions; portal URLconf contains no artifact route; never template/mark safe/`srcdoc` uploaded bytes; content response CSP plus iframe sandbox; no portal session middleware on content. | Portal/content integration tests assert the exact split and browser fixtures exercise hostile content against the same policy primitives. |
| **Malicious CSV executes or surprises viewers** | Treat bytes as opaque hostile data; validate encoding/type/size without evaluating formulas; `text/csv`, `nosniff`, safe response filenames; never render cells as portal HTML; visibly disclose whole-CSV access. | Upload cases in AG-006; MIME/formula and authorization cases in AG-007/AG-011. Spreadsheet formula execution after a user deliberately opens a download cannot be eliminated; author/viewer guidance must warn. |
| **IDOR / cross-Dashboard access** | Central default-deny policy on metadata, HTML, and every CSV request; resolve authorization before artifact metadata; scope by principal, Dashboard, lifecycle, pinned Revision, and attachment; generic external failures. Retained ViewerGrant epochs are checked for the exact Dashboard and viewer, with active-only uniqueness and indexed lookups. | Full owner/viewer/unrelated/administrator/disabled/unauthenticated matrix in AG-004/AG-011, including altered Dashboard, Revision, attachment, filename, and key. |
| **Path traversal / key confusion** | Exact ASCII storage-key grammar generated independently of filenames; never join request values to paths; Unicode comparison keys remain metadata only; absolute private root with ancestor/reparse checks; no-clobber atomic operations. | AG-002 unit/integration cases cover traversal/key forms, Unicode collisions, symlink/reparse handling, collisions, and partial writes. AG-006 still owns multipart filename/content validation. |
| **Render-token replay or widening** | A 256-bit random bearer is stored only as a digest, expires after five minutes, and is scoped to principal, auth version, Dashboard, Revision, and audience. Every file request rechecks active User, Grant/ownership, publication, revision, expiry, and revocation. TLS, `no-store`, `no-referrer`, and disabled/redacted path logging are mandatory. The current multi-request bearer remains replayable inside its short window so referenced CSV requests can work. | Integration tests cover expiry, alteration, wrong audience, revocation, disablement, unpublish, missing attachment, and cross-scope denial. A one-use bootstrap/content-session exchange remains a hardening option before claiming strict non-shareability. |
| **Transfer preserves stale authority or corrupts attribution** | Only the current active owner may invoke transfer; Dashboard is locked before Users; the incoming owner's active Grant is revoked; a chained immutable marker authorizes the owner change. Revision/Grant triggers check the current owner for new actions but never require historical actors to equal that owner. Every render and management operation rechecks current ownership. | Domain, migration-contract, reversal-safety, and adversarial tests cover prior-owner denial, incoming-owner authority, retained historical actors, Grant epochs, and fail-safe reversal refusal after a real transfer. |
| **Navigation/workflow state becomes an authorization oracle** | Favorite, viewer-state, Tag, and AccessRequest rows are inert. Query services intersect current owner/active Grant and publication state, use generic external failures, escape messages, and scope owner request queues to one exact Dashboard. | Service/query tests cover revoked, unpublished, transferred, unrelated, inactive, and malformed cases plus bounded slices and index alignment. |
| **Usage telemetry widens or leaks behavior** | Count only successful Published Viewer authorization creation. Capture one idempotent row with no IP, agent, referrer, click, filter, iframe, fetch, or content data; do not also write `dashboard.view_started`. Portal/admin modules consume bounded aggregates only. Raw deletion requires completed aggregation, a monotonic checkpoint, and 90-day age. | Schema/source-boundary and analytics tests cover preview/denial exclusion, idempotence, no double write, project-scoped aggregates, bounded jobs, checkpoint monotonicity, and retention guards. |
| **Oversized or deceptive upload** | Edge and application caps; stream while counting independently of `Content-Length`; per-file/count/aggregate limits; bounded parse time; atomic commit and orphan cleanup; reject malformed multipart/type confusion. | AG-006/AG-011 test limit and limit+1, missing/false/chunked lengths, too many files, aggregate overflow, disconnect, and no visible Revision/orphan. |
| **Clickjacking / top navigation** | Portal `frame-ancestors 'none'` plus `X-Frame-Options: DENY`; content exact portal ancestor only; sandbox omits top-navigation, popup, modal, form, and download capabilities; trusted shell labels user content. | AG-001 portal headers. Cross-origin frame/navigation cases in AG-007/AG-011. |
| **Cross-Dashboard browser storage** | Conservative baseline omits `allow-same-origin`, giving sandboxed documents an opaque origin. If AG-007 instead needs same-origin behavior, it must isolate each Dashboard/Revision on a distinct origin or prove an equivalent partition that prevents cookie, cache, service-worker, IndexedDB, and Web Storage sharing. | AG-007/AG-011 use two hostile Dashboards to attempt cross-Revision storage and cache communication in every supported browser. |
| **External data exfiltration** | Content CSP blocks common external `connect`, image, media, font, frame, form, and worker channels; no external allowlist or portal credentials; sandbox omits navigation/popup/form capabilities; exact content routes. | AG-007/AG-011 fixtures attempt fetch/XHR/WebSocket/EventSource/beacon/image/CSS/font/form/popup/worker, document navigation, WebRTC/STUN, and DNS-prefetch channels. CSP is not a complete egress boundary: browser-dependent navigation, WebRTC, and DNS effects require supported-browser evidence and may require network egress controls. |
| **Browser denial of service** | Size/count caps; deny workers; load one frame only after explicit preview/view; keep stop/reload/back controls outside the iframe; time out/recover without losing the portal session. | AG-009/AG-011 hostile infinite-loop, huge DOM, timer, and large-data cases. Guaranteed prevention of hostile JavaScript CPU abuse is explicitly out of scope. |
| **Secret/config leakage** | Typed required startup configuration, no usable defaults/placeholders, ignored `.env`, externally injected production secrets, redacted errors/logging, distinct signing purposes, SHA-pinned CI actions. | AG-001 config tests cover missing/malformed/production/redaction; repository secret/dependency scanning arrives in AG-010. |

## Active content response policy

Every uploaded HTML response enforces this iframe `sandbox` and CSP policy:

```text
default-src 'none';
base-uri 'none';
object-src 'none';
script-src 'unsafe-inline';
style-src 'unsafe-inline';
img-src data: blob:;
font-src data:;
media-src data: blob:;
connect-src 'self';
frame-src 'none';
worker-src 'none';
manifest-src 'none';
form-action 'none';
frame-ancestors <exact portal origin>;
sandbox allow-scripts
```

The matching iframe sandbox initially grants only `allow-scripts`, so hostile documents receive
an opaque origin and cannot share the content site's durable origin storage. A successfully
authorized CSV response may therefore return the exact `Access-Control-Allow-Origin: null` value,
with `Vary: Origin` and without credentialed CORS, so the sandboxed document can fetch its own
Revision-relative CSV. The render credential and exact artifact scope provide authorization;
`Origin: null` never does. HTML, denied requests, non-null origins, and unrelated routes receive
no CORS grant. If AG-007 cannot satisfy the product contract without `allow-same-origin`, it must introduce
per-Dashboard or per-Revision origin isolation (or an independently reviewed equivalent) before
granting it; a different parent site does not isolate hostile documents from one another on a
shared content origin. Content also sends `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, `Cache-Control: private, no-store`, restrictive
`Permissions-Policy`, and `X-DNS-Prefetch-Control: off`. It must not send
`X-Frame-Options: DENY` or `SAMEORIGIN`, which would block the legitimate cross-origin portal
frame.

The policy primitives are verified in Chromium fixtures. Production-route integration tests cover
authorization and exact-byte delivery; Firefox/WebKit and real-route browser coverage remain
before claiming the complete AG-007/AG-011 browser matrix.

## Residual and deferred risk

- Local authenticated portal and content flows use TLS on separate loopback hostnames. Ignored
  development certificates are never deployment credentials.
- Browser CPU/memory containment is imperfect; recovery is promised, prevention is not.
- CSP and iframe sandboxing block common browser exfiltration mechanisms but are not a complete
  network firewall. AG-007 must resolve and test document-navigation, WebRTC/STUN, and DNS
  behavior; environments requiring a hard no-egress guarantee need defense-in-depth network or
  browser policy.
- Opaque-origin CSV access is the conservative baseline. Any later same-origin grant is blocked
  on a reviewed cross-Dashboard storage-isolation design.
- Authorized viewers can copy bytes they receive; revocation cannot recall them.
- CSV formula interpretation is controlled by downstream spreadsheet software, not Agora.
- Malware scanning, enterprise egress controls, formal penetration testing, ECS/HA, and SIEM
  integration are outside AG-001.
- The AG-002 adapter repeatedly checks path components and requires service-only filesystem ACLs,
  but portable Python cannot eliminate a race against a privileged local actor replacing names.
  Windows directory-entry durability and network/virtual filesystem atomicity require deployment
  qualification; expired durable reservations preserve cleanup ownership after process failure.

## Primary references

- [WHATWG iframe sandbox](https://html.spec.whatwg.org/multipage/iframe-embed-object.html)
- [W3C Content Security Policy Level 3](https://www.w3.org/TR/CSP3/)
- [RFC 6454: Web Origin Concept](https://www.rfc-editor.org/rfc/rfc6454)
- [RFC 6265: HTTP State Management Mechanism](https://www.rfc-editor.org/rfc/rfc6265)
- [OWASP File Upload Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html)
- [OWASP Authorization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)
- [OWASP IDOR Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Insecure_Direct_Object_Reference_Prevention_Cheat_Sheet.html)
- [OWASP Directory Traversal Testing](https://owasp.org/www-project-web-security-testing-guide/latest/4-Web_Application_Security_Testing/05-Authorization_Testing/01-Testing_Directory_Traversal_File_Include)
- [OWASP CSV Injection](https://owasp.org/www-community/attacks/CSV_Injection)
- [OWASP Clickjacking Defense](https://cheatsheetseries.owasp.org/cheatsheets/Clickjacking_Defense_Cheat_Sheet.html)
- [OWASP Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
- [OWASP Logging](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html)
