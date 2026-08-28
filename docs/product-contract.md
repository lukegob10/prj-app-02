# Agora product contract

Status: **normative foundation and approved-enhancement baseline**
Effective: 2026-08-28
Source of truth: [`tickets/README.md`](../tickets/README.md),
[`AG-001`](../tickets/mvp/AG-001-mvp-foundation.md), and the approved enhancement contract
captured below

This document locks the language and lifecycle used by every Agora ticket. A change to these
rules is a product-contract change and must update the backlog, tests, and security model; it
must not emerge implicitly from implementation. The approved-enhancement foundation establishes
schema and domain behavior only. It does not claim that the later end-user screens exist.

## Terms

| Term | Normative meaning |
|---|---|
| **User** | An administrator-provisioned application identity with an immutable internal identifier and exactly one unique canonical SOEID. A User is active or disabled. Self-registration and user hard deletion are not MVP features. A disabled User cannot authenticate or authorize. |
| **SOEID** | The sole human-facing identity key. It is normalized once at the trusted identity boundary, stored canonically, and never selected from a browser field or untrusted header after authentication. The conservative default is trim surrounding ASCII whitespace, convert to invariant uppercase, then validate `^[A-Z0-9][A-Z0-9._-]{0,63}$`; firm-specific grammar changes require an explicit contract decision. |
| **Dashboard** | The top-level securable resource. It has an opaque stable identifier, exactly one current owner, mutable safe metadata, zero or more Viewer Grants, ordered immutable Revisions, a latest-revision pointer, and an optional published-revision pointer. Ownership may change only through the explicit transfer service; historical actor attribution never changes with it. |
| **Revision** | One atomically committed, immutable version of a Dashboard containing exactly one HTML Artifact and zero or more CSV Attachments. A failed or staged upload is not a Revision. A Revision is never edited or independently deleted in the MVP. |
| **HTML Artifact** | The exact validated bytes of a Revision's sole HTML file. Self-contained means executable presentation dependencies are inline; only documented same-Revision CSV URLs may be runtime data dependencies. It is always hostile active content. |
| **CSV Attachment** | An immutable, validated whole-file artifact belonging to one Revision. Its logical filename is unique after platform-independent normalization. A filename is never a storage path or authorization boundary. |
| **Viewer Grant** | One retained Dashboard-to-User grant epoch, created or revoked by the current owner at the time of that action using canonical SOEID. At most one epoch for a `(Dashboard, User)` pair is unrevoked at a time; revocation closes that row permanently and a later regrant creates a new retained epoch. Its recorded creator and revoker remain immutable even if ownership later changes. An active epoch grants read-only access to the Revision currently published for that Dashboard, including every attached CSV. It grants no draft, preview, management, or arbitrary-Revision access. Owner access is implicit and is not a self-grant. |
| **Dashboard Tag** | One owner-managed plain-text label attached to a Dashboard. The visible label has an application-produced normalized key used for equality and lookup. A Dashboard may have at most five Tags and only one stored Tag per normalized key; attempting a duplicate produces a typed plain-language validation error. A Tag never grants access or creates firmwide discovery. |
| **Favorite** | A User's personal shortcut to one Dashboard. At most one Favorite exists per `(User, Dashboard)`. It never grants access, preserves no access after revocation, and must be intersected with current Dashboard authorization before it is returned. |
| **Dashboard Viewer State** | The compact, single-row `(User, Dashboard)` state used for Recently viewed and the New indicator. It records the most recent successful Authorized Open and the highest Publication Version that User has successfully opened; it is not a raw browsing history. |
| **Access Request** | The single deduplicated request relationship for one `(Dashboard, requester)`. It has `pending`, `approved`, `denied`, and `cancelled` states plus an optional plain-text message of at most 500 characters. It is a request for a Viewer Grant, never authority by itself. An explicit re-request resets the same row rather than appending unbounded request history. |
| **Publication Version** | A Dashboard-local, monotonically increasing release number. Zero means never published. Each successful first publication or republish creates the next positive version, including publication of the same Revision. Unpublish never moves the number backward. Revision number and Publication Version are different concepts. |
| **Authorized Open** | The only usage metric: one successfully created `RenderAuthorization` with the Published Viewer audience. Owner preview, failed authorization, content fetches, iframe loads, and all in-dashboard behavior are not Authorized Opens. |
| **Draft** | Dashboard state for a Dashboard that has never been published. The published pointer is null. It can have zero or more complete Revisions and Grants; Viewers see nothing. |
| **Published** | Dashboard state in which the published pointer references one complete Revision owned by the same Dashboard. Later uploads do not move this pointer. Zero Grants is valid and leaves the publication owner-only. |
| **Unpublished** | Dashboard state for a previously Published Dashboard that was deliberately withdrawn. The published pointer is null; Revisions and Grants remain. Viewers have no access. |
| **Archived** | Retained, non-live, read-only Dashboard state, hidden from ordinary active lists. The published pointer is null and Grants are inactive. Restore never republishes automatically. |
| **Deleted** | Terminal soft-deleted tombstone for ordinary users. The published pointer is null, Grants are permanently inactive, stable identifiers are never reused, and all normal routes fail generically. End-user restore and physical purge are outside the MVP. |

Owner, granted viewer, and administrator describe relationships or capabilities; they are not
mutually exclusive User types. Administrator status controls User administration only and
does not implicitly disclose Dashboard metadata or artifacts.

## Dashboard lifecycle

Publication is a pointer on Dashboard; it never mutates a Revision.

| Current state | Action | Next state | Required invariant |
|---|---|---|---|
| none | Create | Draft | Private; published pointer is null. |
| Draft, Published, Unpublished | Upload | unchanged | Only latest Revision changes; published pointer is unchanged. |
| Draft, Unpublished, Published | Publish complete owned Revision | Published | Atomically set the pointer and create the next Publication Version, even when the selected Revision is already pinned. |
| Published | Republish complete owned Revision | Published | Atomically create the next Publication Version and swap one valid pointer for another. An earlier Revision is valid and is the rollback mechanism. |
| Published | Unpublish | Unpublished | Clear pointer; retain Revisions and Grants. |
| Draft, Published, Unpublished | Archive | Archived | Clear pointer atomically; retain artifacts and inactive Grants. |
| Archived | Restore | Draft or Unpublished | Draft if never published, otherwise Unpublished; never auto-publish. |
| any non-Deleted state | Delete | Deleted | Clear pointer; deactivate Grants; no MVP outbound transition. |

All unlisted transitions are invalid. An Archived or Deleted Dashboard cannot publish. The
phrase "deleted revision" in AG-008 means a Revision whose Dashboard is Deleted; independent
Revision deletion is not part of this contract.

## Personal organization and discovery

Tags, Favorites, Recently viewed, and New are navigation aids; none is an authorization source.
Every read intersects current project-scoped policy before returning Dashboard metadata. A stale
Favorite or Dashboard Viewer State row may remain retained after access is lost, but it is
invisible and cannot make a stable URL succeed. Deleting a Dashboard may remove dependent
preference rows as a referential cleanup without changing retained security or audit history.

- Owners manage no more than five Tags on a Dashboard. The sole normalizer applies Unicode NFKC,
  rejects control/format/surrogate characters, collapses Unicode whitespace to one ASCII space,
  and requires a 1–40-code-point display label. Its key applies casefold then NFKC and is at most
  80 code points. A duplicate `(Dashboard, key)` is a typed validation error, never a second row
  or an uncaught Oracle error. Five constrained slots provide deterministic display order without
  exposing slot numbers to Users. Exact normalized-key lookup is bounded and indexed. Tags do not
  expose private, unpublished, archived, or unrelated Dashboards.
- Favoriting and unfavoriting are idempotent. A User can create a Favorite only while authorized
  to see the Dashboard. Favorites are ordered through a bounded, indexed recent read; they are
  not evidence that access still exists.
- Recently viewed uses `DashboardViewerState.last_viewed_at`, never a raw-event scan. Only a
  successful Authorized Open advances it. A list view, failed open, owner preview, HTML/CSV
  request, refresh of an existing content authorization, or analytics aggregation does not.
- A currently visible Published Dashboard is New to a User when no Dashboard Viewer State exists
  or its seen Publication Version is lower than the current Publication Version. Successful
  Authorized Open records the current version using monotonic-max semantics so a delayed older
  request cannot move the seen version backward. Merely rendering a list never clears New.

## Access-request lifecycle

An eligible requester is an active authenticated User who is not the current owner, has no active
Viewer Grant, and targets a Published Dashboard whose current owner is active. The request does
not reveal Dashboard metadata, owner identity, publication state, or even whether an unrelated
identifier exists. Invalid, hidden, ineligible, and already-authorized targets use the same
external response; only a valid eligible target creates or updates its deduplicated row. The
optional message is escaped text visible only in the authorized owner queue; it is not HTML, an
identity claim, or an authorization input.

| Current state | Action | Next state | Required behavior |
|---|---|---|---|
| none or resolved, without current entitlement | Request access | Pending | Create or explicitly reset the one relationship; clear prior resolution fields and audit the action. |
| Pending | Request access again | Pending | Idempotent; do not create another row or notify repeatedly. |
| Pending | Current owner gives access | Approved | While Published or Unpublished, create or preserve the Viewer Grant and resolve atomically; the Grant is dormant until republished when Unpublished. |
| Pending | Current owner transfers ownership to requester | Approved | Resolve before transfer; ownership itself satisfies access and no self-Grant remains. |
| Pending | Current owner denies | Denied | Resolve without creating authority. |
| Pending | Requester cancels | Cancelled | Resolve without creating authority. |
| any | User already has current access | unchanged | Return the same generic already-has-access outcome and do not create or reopen a request. |

`pending` requires empty resolution time and actor fields. `approved`, `denied`, and `cancelled`
require both. Those outcomes describe the current singleton relationship, not an unbounded
notification history. A later request explicitly resets the same row, while the append-only audit
stream retains who did what. The queue is read only for one selected Dashboard after checking its
current owner, so transfer changes queue authority without rewriting every retained request or
scanning all owned Dashboards. Archive, delete, User
disablement, and other ineligible states fail closed; they never turn a request into access.
Give access rechecks active requester, active current owner, current ownership, Published or
Unpublished lifecycle, and entitlement under the same transaction. A failed recheck creates
neither a Grant nor a resolved status. A concurrent active Grant may be preserved while Pending
resolves Approved. Deny and requester cancellation remain available for any retained Pending row.
Transfer-to-requester follows its dedicated atomic Approved exception above.

Each owner queue is Dashboard-scoped, bounded, deterministically ordered, and indexed by exact
Dashboard, request state, request time, and request identifier. A future cross-Dashboard inbox
would require a separate bounded materialization; it must not join and sort every owned Dashboard
at request time. Resolving one item never scans all requests, grants, Dashboards, or Users.

## Ownership transfer

Ownership changes only through `transfer_dashboard_ownership`. It is not a field on a generic
metadata form, a mass-assignment option, or an administrator shortcut. The transactional service
requires the active current owner, an active distinct incoming owner, and a Dashboard in Draft,
Published, or Unpublished state. At commit it must:

1. recheck current ownership and lock the affected Dashboard, Grant, and Access Request rows;
2. resolve any Pending Access Request from the incoming owner as Approved and revoke that User's
   active Viewer Grant, all attributed to the transferring owner;
3. replace only the current `Dashboard.owner` relationship;
4. preserve the stable URL, lifecycle, publication, Revisions, Tags, preferences, pending access
   requests from other Users, and all other Viewer Grant epochs; and
5. append immutable, chained transfer evidence and an audit event identifying the actor and
   incoming owner without rewriting any Revision creator or Viewer Grant creator/revoker.

The old owner receives no automatic Viewer Grant and loses management, preview, published-owner
view, access-request queue, aggregate analytics, and render authority at the next server-side
check. The new owner receives implicit owner authority only after the same committed check. Other
active Viewer Grants remain unchanged.
Owner-mode render credentials are bound to the current transfer epoch: a credential issued before
a transfer never becomes valid again merely because ownership is later transferred back.

A real transfer cannot be safely undone by reversing a migration, restoring old composite foreign
keys, or rewriting actor history. The retained transfer chain is the fail-safe proof that blocks
unsafe schema reversal after a real transfer. A transfer back is a new, explicitly authorized
transfer by the then-current owner. It cannot recreate the incoming owner's revoked grant epoch,
recall bytes, cancel actions taken while ownership was changed, or erase either audit event.
Operational reversal must therefore fail safe and preserve history.

## Publication releases, freshness, and rollback

Publication metadata describes the current release and is owner-supplied plain text/data. It does
not alter immutable Revision content and is returned only through an already-authorized Dashboard
query.

- `publication_version` starts at zero and increases exactly once for each successful first publish
  or republish, including rollback-by-republish. It never decreases or reuses a number.
- `last_published_at` records the successful action that created the current Publication Version.
  `first_published_at` remains the immutable first-publication time.
- `publication_note` belongs to the new Publication Version and is an optional plain-text
  explanation of at most 240 characters. It is escaped and is never HTML, Markdown, uploaded
  content, or an authorization input.
- `data_as_of` is the owner's statement of the point through which the published data applies. It
  must never silently default to the upload or publication time.
- `freshness_interval_seconds` is absent or a whole-second duration from 1 through 31,536,000
  inclusive. Explicit owner confirmation is recorded as the server timestamp
  `freshness_confirmed_at`; it produces the persisted indexed `stale_after` as confirmation time
  plus interval. Those three freshness-claim fields are all present or all absent. Missing
  freshness information means **Freshness not provided**, not Fresh. At and after `stale_after`,
  the release is **Freshness check due**. Before it, the UI may say **Current as confirmed**, but
  Agora does not claim to have inspected or validated the data.
- Freshness is derived at read time from `stale_after`; there is no persisted `is_stale` flag and
  no clock-driven mass update. Confirming freshness updates the confirmation boundary through the
  owner service but does not itself create a Publication Version or mark content New. The
  confirmation service preserves existing `data_as_of` when the caller omits that input; it may
  explicitly replace or clear it, and a Dashboard with no `data_as_of` remains confirmable.

Unpublish retains the last release metadata internally but there is no current freshness claim
while the Dashboard is Unpublished. Every later republish creates a new Publication Version. Its
safe defaults are an empty Publication note and **Freshness not provided**; old freshness is never
carried forward silently. `data_as_of` also has no invented or silently inherited value.

Rollback selects an earlier complete Revision and republishes it as a new Publication Version.
It does not move history backward, mutate or delete later Revisions, restore prior owner/grant
state, or change the stable URL. Existing active Viewer Grants apply to the newly pinned Revision,
and the new version is New until each User successfully opens it.

## Authorized-open analytics and privacy

Agora measures exactly one thing: Authorized Opens. The successful creation of a Published Viewer
`RenderAuthorization` is the measurement boundary, including an owner who deliberately uses the
published-view route. A unique authorization foreign key makes capture idempotent. The compact
raw `AuthorizedOpen` record is the sole telemetry write for new opens; the former
`dashboard.view_started` `AuditEvent` is not also written. Historical audit rows remain immutable
and are excluded from post-cutover counting unless a separately reviewed reconciliation records a
non-overlapping boundary.

The published-view issuance transaction commits the `RenderAuthorization`, its one
`AuthorizedOpen`, and monotonic `DashboardViewerState` advancement together or commits none of
them. Preview issuance creates none of the latter two records. The event timestamp is the
authorization creation time in UTC, and daily rollups use UTC calendar boundaries; a retry tied to
the same authorization never adds another open.

The same metric may be grouped into bounded `DashboardOpenDaily`,
`DashboardViewerOpenSummary`, and `DashboardOpenSnapshot` records. Those are materializations of
Authorized Open counts, not new metrics. Agora does not track clicks, scrolling, filters, iframe
loads, HTML/CSV fetches, previews, IP addresses, user agents, referrers, or content-origin
behavior. It does not infer attention, completion, endorsement, or data quality from an open.

Raw opens have a fixed 90-day retention window and partition-ready time keys. Aggregation commits
its materializations before advancing a durable `AnalyticsPipelineCheckpoint`. Cleanup deletes at
most 1,000 rows per run and only rows both older than 90 days and at or behind that committed
checkpoint. A one-way marker retained on the source `RenderAuthorization` prevents a purged source
key from being captured again; historical recapture/backfill through the live capture interface is
forbidden. Portal and administrator code is structurally restricted to aggregate query interfaces
and must never query or display raw events. Aggregate access remains project-scoped: the current
owner may receive authorized Dashboard aggregates, administrators do not gain content or usage
access, and the prior owner loses access after transfer.

Popular-Dashboard snapshots are produced off the request path from rollups. A later viewer-facing
ranking starts from at most 100 of that User's most recent active Grants to Published Dashboards
and may use a snapshot only to order those authorized candidates; it is not firmwide discovery or
an exhaustive ranking across an unbounded entitlement history. Creating a
render authorization never synchronously increments a Dashboard popularity counter, scans
history, or updates every summary.

## Bounded-work contract

Every list, query, and maintenance or analytics job has a server-enforced maximum batch or page
size and deterministic indexed ordering. New discovery interfaces in this foundation are bounded
top-N snapshots and do not promise traversal. Dashboard-scoped owner queues and retained-history
interfaces that promise traversal accept a stable indexed keyset cursor. Tags need no cursor because their
cardinality is constrained to five. A request may touch the selected Dashboard and a bounded page
of related rows; it must not perform work proportional to all Users, Dashboards, Viewer Grants,
Access Requests, or Authorized Opens. Exact unbounded totals and raw event history are not
request-time UI features. Tag lookup, Favorites, Recently viewed, New, owner access queues, and
analytics use their purpose-built summary/state rows and indexes.

The approved baseline remains server-rendered Django over Oracle. A SPA, Redis, queue, or new
service requires a measured bottleneck and an approved architecture change; these enhancements do
not justify one by themselves.

## Publication and stable URL

The stable, shareable URL is a portal-origin viewer-shell URL containing a collision-resistant
Dashboard identifier. It never contains a Revision identifier, storage key, filename,
credential, or render token. After server-side authorization, the portal resolves the pinned
Revision and frames a short-lived, Revision-scoped content-origin navigation.

The stable URL does not change across upload, republish, unpublish/re-publish, Grant changes,
ownership transfer, archive/restore, or content-token renewal. Preview is owner-only, short-lived,
Revision-scoped, non-shareable, rendered through the same isolated content boundary, and never
changes publication state, Dashboard Viewer State, or analytics.

## Whole-CSV access

An active Viewer Grant to a Published Dashboard authorizes the viewer to retrieve **every CSV
Attachment in the pinned Published Revision**, including files the HTML does not reference.
There is no row-, column-, file-, or purpose-level filtering. Filenames provide no secrecy.

- A Grant automatically applies when a future Revision is explicitly published.
- A Grant alone exposes no Draft, Unpublished, Archived, or arbitrary Revision.
- Each HTML and CSV request still requires server-side authorization or narrowly scoped render
  authorization; opaque identifiers are not access control.
- Revoke, disable, unpublish, archive, delete, or republish ends authorization at the next
  server-side HTML or CSV check. Already received bytes cannot be recalled.
- Render credentials are bound to the exact Dashboard, User, Revision, and grant epoch. A
  credential issued for a revoked epoch never revives when that SOEID is granted a new epoch;
  the viewer must receive a newly issued credential for the new epoch.
- Ownership transfer invalidates the prior owner's authority and the incoming owner's former
  Grant-bound credentials at the next check; it does not invalidate other active viewers.
- Owner and Viewer UX must clearly explain complete-CSV visibility and user-created content.

## Role expectations

- **Owner:** manages only currently owned Dashboards, Revisions, Tags, Grants, access requests,
  preview, publication/freshness, transfer, archive, restore, and delete; aggregate usage access
  is also scoped to current ownership.
- **Granted viewer:** discovers only currently Published Dashboards with an active Grant and may
  view only the pinned Revision and all of its CSVs.
- **Administrator:** provisions, disables, re-enables, and resets Users. Dashboard content access
  still requires ownership or a Grant. A later break-glass operation must be separate and audited.
- **Unauthenticated, disabled, or unrelated User:** receives no metadata or existence signal;
  public-facing failures are generic.

A disabled owner loses personal access, but disabling the owner does not automatically
unpublish content for other authorized viewers. Destructive actions require explicit
confirmation and must explain viewer impact.

## Foundation delivery boundary

This enhancement lane supplies constrained persistence, Oracle invariants, and stable typed
service/query contracts. Tags, Favorites, Dashboard Viewer State, Access Requests, transfer,
metadata/freshness, rollback-by-republish, and analytics screens are intentionally not promised or
built here. Later UI lanes must implement the behavior above without schema changes and must meet
[`docs/ui-conventions.md`](ui-conventions.md).

## Deferred scope

Deferred beyond the MVP:

- editors, co-owners, groups, firmwide access, and anonymous/public links;
- self-registration, enterprise SSO, directory sync, and federated lifecycle management;
- row/column/per-attachment filtering, data masking, or download prevention;
- multiple HTML files, mutable Revisions, attachment replacement, and Revision deletion;
- external asset/network allowlists, public signed links, service workers, and CDN delivery;
- scheduled publication, approvals, collaboration, revision diffing, and automatic rollback;
- ECS topology, production high availability, formal penetration testing, and malware services;
- guaranteed prevention of hostile browser CPU exhaustion.

The remaining end-user enhancement workflows and release operations stay in later delivery lanes;
this foundation does not silently broaden their scope.
