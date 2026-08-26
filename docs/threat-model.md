# Foundation threat model

Status: approved security baseline with AG-002 persistence/storage controls
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

## Threat register

| Threat | Required controls | Verification / owning ticket |
|---|---|---|
| **Uploaded HTML crosses into portal** | Separate sites and service compositions; portal URLconf contains no artifact route; never template/mark safe/`srcdoc` uploaded bytes; content response CSP plus iframe sandbox; no portal session middleware on content. | AG-001 smoke tests assert host split, portal CSP, and empty fail-closed content route. Malicious browser fixtures belong to AG-007/AG-011. |
| **Malicious CSV executes or surprises viewers** | Treat bytes as opaque hostile data; validate encoding/type/size without evaluating formulas; `text/csv`, `nosniff`, safe response filenames; never render cells as portal HTML; visibly disclose whole-CSV access. | Upload cases in AG-006; MIME/formula and authorization cases in AG-007/AG-011. Spreadsheet formula execution after a user deliberately opens a download cannot be eliminated; author/viewer guidance must warn. |
| **IDOR / cross-Dashboard access** | Central default-deny policy on metadata, HTML, and every CSV request; resolve authorization before artifact metadata; scope by principal, Dashboard, lifecycle, pinned Revision, and attachment; generic external failures. | Full owner/viewer/unrelated/disabled/unauthenticated matrix in AG-004/AG-011, including altered Dashboard, Revision, attachment, filename, and key. |
| **Path traversal / key confusion** | Exact ASCII storage-key grammar generated independently of filenames; never join request values to paths; Unicode comparison keys remain metadata only; absolute private root with ancestor/reparse checks; no-clobber atomic operations. | AG-002 unit/integration cases cover traversal/key forms, Unicode collisions, symlink/reparse handling, collisions, and partial writes. AG-006 still owns multipart filename/content validation. |
| **Render-token replay or widening** | Prefer a random one-use bootstrap stored only as a digest and atomically consumed; short-lived render state scoped to principal, Dashboard, Revision, audience, and exact content origin; recheck active User, Grant, and publication; TLS, `no-store`, no referrer/token logging. A signed bearer token alone is replayable. | AG-007/AG-011 test second use, expiry, alteration, wrong audience/origin, identifier change, revocation, disablement, unpublish, and log redaction. |
| **Oversized or deceptive upload** | Edge and application caps; stream while counting independently of `Content-Length`; per-file/count/aggregate limits; bounded parse time; atomic commit and orphan cleanup; reject malformed multipart/type confusion. | AG-006/AG-011 test limit and limit+1, missing/false/chunked lengths, too many files, aggregate overflow, disconnect, and no visible Revision/orphan. |
| **Clickjacking / top navigation** | Portal `frame-ancestors 'none'` plus `X-Frame-Options: DENY`; content exact portal ancestor only; sandbox omits top-navigation, popup, modal, form, and download capabilities; trusted shell labels user content. | AG-001 portal headers. Cross-origin frame/navigation cases in AG-007/AG-011. |
| **Cross-Dashboard browser storage** | Conservative baseline omits `allow-same-origin`, giving sandboxed documents an opaque origin. If AG-007 instead needs same-origin behavior, it must isolate each Dashboard/Revision on a distinct origin or prove an equivalent partition that prevents cookie, cache, service-worker, IndexedDB, and Web Storage sharing. | AG-007/AG-011 use two hostile Dashboards to attempt cross-Revision storage and cache communication in every supported browser. |
| **External data exfiltration** | Content CSP blocks common external `connect`, image, media, font, frame, form, and worker channels; no external allowlist or portal credentials; sandbox omits navigation/popup/form capabilities; exact content routes. | AG-007/AG-011 fixtures attempt fetch/XHR/WebSocket/EventSource/beacon/image/CSS/font/form/popup/worker, document navigation, WebRTC/STUN, and DNS-prefetch channels. CSP is not a complete egress boundary: browser-dependent navigation, WebRTC, and DNS effects require supported-browser evidence and may require network egress controls. |
| **Browser denial of service** | Size/count caps; deny workers; load one frame only after explicit preview/view; keep stop/reload/back controls outside the iframe; time out/recover without losing the portal session. | AG-009/AG-011 hostile infinite-loop, huge DOM, timer, and large-data cases. Guaranteed prevention of hostile JavaScript CPU abuse is explicitly out of scope. |
| **Secret/config leakage** | Typed required startup configuration, no usable defaults/placeholders, ignored `.env`, externally injected production secrets, redacted errors/logging, distinct signing purposes, SHA-pinned CI actions. | AG-001 config tests cover missing/malformed/production/redaction; repository secret/dependency scanning arrives in AG-010. |

## Future content response policy

AG-007 must enforce an iframe `sandbox` attribute and a response CSP. The starting policy is:

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
an opaque origin and cannot share the content site's durable origin storage. Revision-relative
CSV authorization must be designed and browser-tested with that `Origin: null` behavior. If
AG-007 cannot satisfy the product contract without `allow-same-origin`, it must introduce
per-Dashboard or per-Revision origin isolation (or an independently reviewed equivalent) before
granting it; a different parent site does not isolate hostile documents from one another on a
shared content origin. Content also sends `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, `Cache-Control: private, no-store`, restrictive
`Permissions-Policy`, and `X-DNS-Prefetch-Control: off`. It must not send
`X-Frame-Options: DENY` or `SAMEORIGIN`, which would block the legitimate cross-origin portal
frame.

The exact policy is verified against supported browsers in AG-007/AG-011. It is not activated
for uploaded content in AG-001 because no content delivery route exists.

## Residual and deferred risk

- Foundation local HTTP contains no authentication or artifact data. Local TLS is a release gate
  before those browser flows are accepted.
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
