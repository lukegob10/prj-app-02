# Threat model

Uploaded dashboard files are hostile. The trusted portal, user identity, authorization state,
database metadata, private files, credentials, and service availability must remain protected
from uploaded code and unauthorized users.

## Required invariants

1. Uploaded HTML is served only by the content service. It is never inserted into portal HTML,
   `srcdoc`, or a same-origin `blob:` or `data:` document.
2. Portal cookies are host-only and are not used by the content service.
3. Every dashboard file request is authorized server-side for one user, project, revision, and
   credential audience.
4. Filenames are display metadata. Generated keys and root-containment checks own filesystem
   addressing.
5. Artifacts stay private, immutable, outside static roots, and outside the repository.
6. CORS is never wildcarded and uploaded bytes cannot weaken response-enforced CSP or sandboxing.
7. Disablement, revocation, unpublication, transfer, and expiry fail closed on the next request.
8. Logs and public errors do not expose credentials, tokens, cookies, filesystem paths, uploaded
   bytes, or internal exception details.

## Principal controls

| Risk | Control |
|---|---|
| Cross-project access | Central authorization checks scope every query and file response; external failures are generic. |
| Path traversal or overwrite | Strict flat filenames, generated storage keys, containment checks, and no-clobber writes. |
| Uploaded code reaching the portal | Separate hosts, settings, middleware, URL configurations, and CSP policies. |
| Credential replay | Random short-lived credentials stored as digests and bound to user, project, revision, audience, and grant epoch. |
| Browser exfiltration | Restrictive content CSP, opaque iframe sandbox, no portal credentials, no external asset allowlist, and exact CORS responses. |
| Oversized uploads | Independent per-file, file-count, and aggregate limits before metadata becomes visible. |
| Clickjacking | Portal frame denial; content permits only the exact portal origin needed for its sandboxed frame. |
| Authorization or identity leakage | Canonical SOEIDs, default-deny queries, generic not-found responses, and redacted errors. |
| Stale authority after state changes | Authorization state is rechecked for every package fetch. |

## Content response policy

Dashboard HTML runs in an iframe sandbox that grants scripts but not same-origin identity,
navigation, popups, forms, downloads, or workers. Its response policy denies all resources by
default and permits only the minimum same-revision styles, images, fonts, media, and fetches.
Responses are private and non-cacheable, use `nosniff`, send no referrer, and allow framing only
by the configured portal origin.

Supporting-file CORS may allow the exact opaque sandbox origin only after the same server-side
authorization used for direct delivery. An `Origin: null` value is never authorization by itself.

## Residual risk

Authorized users can copy bytes they receive, and revocation cannot recall them. Browser sandbox
and CSP controls reduce common exfiltration paths but do not replace a network firewall. Hostile
JavaScript may still consume browser CPU or memory. Spreadsheet software may interpret formulas
in downloaded CSV files. Local privileged users can interfere with local files despite Python's
containment checks.

Revisit this document whenever origins, identity, authorization, upload limits, artifact storage,
or render credentials change.
