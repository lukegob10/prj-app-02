# Portal UI conventions

Agora's portal is a trusted, server-rendered Django surface. Templates live under
`src/agora/portal/templates/portal/`, and the shared visual language is committed in
`src/agora/portal/static/portal/foundation.css`. There is no portal JavaScript, external font,
remote image, or third-party stylesheet in the foundation.

The enhancement foundation defines the interaction contract for later screens but does not add
or promise those routes, templates, or controls in this lane. A later screen must satisfy this
contract without weakening the portal/content-origin boundary or project-scoped authorization.

## Zero-training interaction contract

Every workflow must be understandable at first use without a tour, tutorial, remembered gesture,
or administrator explanation.

- Give each page or decision state one obvious primary action. Repeated row-level Open/Favorite
  controls may form one consistent action family, but they must not compete with several
  differently styled page-level calls to action. Secondary navigation is visually subordinate;
  dangerous actions are separated from routine work.
- Use familiar verbs and the User's outcome: **Add tag**, **Add to favorites**, **Request access**,
  **Give access**, **Deny request**, **Publish update**, **Confirm freshness**, **Republish this
  version**, and **Transfer ownership**. Do not expose model names, raw status codes, tokens,
  checkpoints, or migration language.
- Choose a safe useful default, but never invent authority, data dates, freshness, recipient
  identity, or publication intent. Keep advanced context behind a clearly named native
  `<details>` disclosure or a separate server-rendered page; do not hide a required fact there.
- Put short help beside the decision it qualifies. Empty states explain what the User can do now
  and offer the one real next action. A tutorial may supplement a workflow later, but the
  workflow must remain complete without it.
- Render the complete state, validation errors, confirmations, and success result on the server.
  Mutations are CSRF-protected POSTs followed by a redirect where practical. Back, reload,
  duplicate submission, keyboard-only use, and disabled JavaScript must remain safe.
- Preserve semantic headings, landmarks, native controls, visible focus, descriptive error text,
  and status announcements. Dates use a visible timezone and a machine-readable `<time>` value.
  Icons, placement, and color may reinforce words but never replace them.

Progressive disclosure reduces initial choices; it does not defer security impact, complete-CSV
disclosure, loss of ownership, freshness uncertainty, or the meaning of analytics.

## Bounded-list and job conventions

Every list has a small default page or top-N limit, a server-enforced maximum, and deterministic
indexed ordering. A top-N summary may deliberately stop; a workflow that promises more results
must provide keyset/cursor pagination. Never offer an unbounded **Show all**, raw-event export, or
arbitrary sort/filter combination without a matching bounded query contract. Pagination uses a
named `<nav>` and native links, preserves supported GET filters, and does not depend on scroll or
script state. A collection whose schema has a tiny hard maximum, such as five Tags, needs no
continuation control.

Do not compute exact unbounded totals during a request. Use a maintained aggregate, a clearly
labelled capped value, or omit the total. Tag results, Favorites, Recently viewed, New items,
access-request queues, grants, Revisions, and usage summaries must use their dedicated indexed
query interface rather than loading a parent collection and filtering it in Python.

Maintenance and analytics work advances in bounded checkpointed batches outside the page request.
If a later operations page exposes a run, it shows bounded summary/progress state and a manual
safe retry; the page never performs an all-User, all-Dashboard, all-Grant, or all-event job while
rendering. A SPA, Redis, queue, or new service is not a UI prerequisite and requires measured need
plus an approved architecture change.

## Shell contract

Extend `portal/base.html` for every portal page. It provides the document language, title and
description slots, same-origin stylesheet, first-focusable skip link, header, primary navigation,
main landmark, and footer. The page template owns one descriptive `h1`; section headings begin at
`h2` and proceed without skipped levels.

The shell renders only navigation items supplied by the context processor. Authenticated users
receive Projects; administrators additionally receive Users. The account menu identifies the
canonical SOEID and submits logout through a CSRF-protected POST. Do not add links for a route
until that route exists. The public Home header owns the single sign-in link, while the sign-in
form owns the single submit action on its screen.

## Product navigation

The authenticated landing page is the Projects workspace. It preserves **My projects** and
**Shared with me** as explicit tabs rather than merging their permissions into one ambiguous list,
and provides bounded prefix search and signed keyset pagination on that same screen. Do not add a
separate authenticated Home destination that duplicates the Projects workspace. The retired
`/projects/` path redirects to the authenticated root while preserving supported GET filters.

Authenticated workspace screens use the wider app canvas shared by Home. Page headers, scope tabs,
tables, status summaries, and history sections are contained surfaces that span the available
canvas; form workflows retain a wider but bounded reading measure. This treatment applies to
Projects, Shared with me, Project Detail, and user administration.

The anonymous landing page is one focused surface: a concise product promise, one sign-in action,
and three short safeguards. Detailed workflow education belongs after authentication rather than
on the gateway. The Sign in page is deliberately smaller: a centered split card keeps access
guidance beside a compact credential form without recreating a second page-sized hero.

Creating a project records safe metadata as a private Draft and opens Project Detail. From there
an owner builds one upload queue by choosing or dropping one HTML entry point and optional CSV,
CSS, image, and font Supporting Artifacts. The whole drop surface is the native multi-file input,
and portal-level file-drop handling prevents a missed drop from opening or rendering a local file
in the browser. Files may be added in batches; the newest queued file replaces an earlier file
with the same normalized name. Submitting creates an immutable Revision,
then opens
an exact private preview. Preview and published-view pages keep the portal controls outside a
labelled cross-origin iframe. A viewer sees only a currently Published project with an active
Grant and opens the stable project URL, never a raw Revision, filename, storage key, or token.
Project links are dashboard-first: an owner opens the latest private preview, a recipient opens the
published view, and only an owner project without a revision falls back to its setup screen. The
dashboard's **Details** panel describes the project, revision, renderer boundary, and available
files, then exposes the deeper project-information screen as an explicit action. Metadata, uploads,
and revision history therefore remain one deliberate step behind the dashboard.

Dashboard preview and published-view routes use a viewport-sized app shell. They replace the
standard portal header and decorative footer with one compact Agora dashboard bar, remove the
normal content width cap, and let the cross-origin iframe consume all remaining viewport space.
Renderer safety, credential expiry, and attached-file disclosure remain available from the bar's
native Details control without permanently reducing the dashboard canvas.

## Approved-enhancement interaction contracts

These are future-screen requirements over the stable domain interfaces, not an instruction to add
feature pages in the foundation lane.

### Tags, Favorites, Recently viewed, and New

- Display Tag labels as escaped text. **Add tag** is the owner action; explain the five-Tag limit
  next to the field and, at the limit, replace the unavailable control with plain guidance to
  remove a Tag first. Explain the 1–40-character normalized label limit. Duplicate normalized
  labels return a typed plain-language inline error; they never appear as a second Tag or an
  Oracle error.
- **Add to favorites** and **Remove from favorites** are idempotent POST actions. Use visible text
  even if a star icon is present, and expose the current state with `aria-pressed`. A Favorite is
  described as a shortcut, never as saved access. Put the action in the published dashboard bar as
  well as eligible discovery rows so Users do not have to leave the dashboard to find it. After
  current authorization is lost it must not appear or make the Dashboard route behave differently.
- **Recently viewed** is a bounded list of still-authorized Published Dashboards ordered by the
  compact Dashboard Viewer State, not an activity log. Opening its destination is the obvious
  row action.
- Render **New** as a textual badge, optionally with an icon. It means the current Publication
  Version has not been successfully opened by this User; it does not mean newly uploaded, newly
  shared, recently edited, or fresh. Rendering the list does not clear it. A successful published
  open does.

Tag lookup and all personal lists remain within current project authorization. Their empty states
must not suggest firmwide discovery or reveal private Dashboard names.

### Request access

An unrelated authenticated User receives the same generic unavailable treatment for invalid,
hidden, and request-eligible stable identifiers. If that treatment offers **Request access**, the
form and response must remain indistinguishable across those cases: do not reveal Dashboard name,
owner, lifecycle, prior decisions, or identifier validity. Safe confirmation copy is: **Request
received. If this dashboard can accept requests, its owner will see it.** Repeating a pending
request is a success, not a duplicate or an existence signal. If offered, **Message (optional)**
is a plain-text field limited to 500 characters; explain that the owner can read it, do not render
markup, and never echo it on the generic confirmation page.

The owner's **Access requests** queue is a bounded list inside one currently owned Dashboard; do
not present a globally sorted inbox that would scan every owned Dashboard. Each item uses the
canonical requester SOEID and request time for the already-authorized Dashboard. **Give access**
warns that, while Published, the User can view the entire pinned Revision
and every CSV;
it atomically approves the request and creates or preserves the Grant. On an Unpublished Dashboard,
plainly say that the Grant remains dormant until the owner republishes. **Deny request** resolves
without access. A requester may use **Cancel request** only where showing their own valid pending
state does not create a new existence signal. Deny and Cancel remain available for a retained
Pending request even when publication later changes. Neither a Pending row nor its confirmation is
ever described as access. Every action rechecks the actor, current ownership, lifecycle, and
entitlement required for that action. A stale item fails without changing request or Grant and
tells the authorized actor what can be safely corrected.

### Ownership transfer

Ownership transfer has a dedicated server-rendered flow separate from Edit, sharing, User
administration, and bulk actions. First identify one active incoming owner by canonical SOEID;
then show a separate confirmation page that names that SOEID and the selected Dashboard. The page
has one primary **Transfer ownership** POST, a subordinate Cancel link, and a required visible
acknowledgement of all effects:

- the current owner immediately loses management, preview, published-owner view, access-request
  queue, aggregate usage, and existing render authority on the next check;
- the incoming owner's active Viewer Grant is revoked because owner access is implicit;
- a Pending request from that incoming owner is Approved because ownership satisfies it, while all
  other requests continue to the new owner's queue;
- the stable URL, publication, Revisions, other Grants, Tags, and history remain; and
- the action cannot be undone by rewriting history; transferring back is a new action by the new
  owner and does not recreate the revoked Grant.

Never confirm transfer in a transient modal, infer the target from a display name, preselect a
User, or call the action **Move**, **Replace**, or **Save**. The server rechecks both active Users
and current ownership at POST time, and concurrent/stale confirmations fail with an actionable
generic conflict rather than transferring a different state.

### Publication, freshness, and rollback-by-republish

Treat a successful publish or republish as a new Published update. The owner sees the selected
Revision, the next release intent, and viewer impact before submitting the one primary **Publish
update** action. The optional **Publication note** is short plain text. **Data as of** is optional
and has no invented default; upload time and publish time are not substitutes. Enforce and explain
the 240-character Publication note limit at the field.

Freshness is an optional owner claim, not a system assertion. If supplied, the form requires a
positive **Check again after** interval and an explicit acknowledgement such as **I confirm this
published data is current now**. The server records **Freshness confirmed at** and derives
**Freshness check due**; do not ask the owner to manufacture a timestamp. The interval, recorded
confirmation time, and derived due time are stored or cleared together. Accept whole-second
durations from one second through one year, using familiar units while enforcing that exact bound
on the server.
Use these exact viewer-facing states:

| Condition | Visible label | Required supporting detail |
|---|---|---|
| freshness claim absent | **Freshness not provided** | Do not imply Current or Stale. |
| current time before `stale_after` | **Current as confirmed** | Show confirmation and next-check times; label this owner-provided, not system-verified. |
| current time at/after `stale_after` | **Freshness check due** | Show when the check became due; do not claim the content is false. |

Never persist or render from a mass-updated `is_stale` flag. A standalone **Confirm freshness**
action updates the claim boundary; it does not publish, increment Publication Version, or clear
New for anyone. **Data as of** remains independent and must not decide freshness: leaving it
untouched preserves the existing value, while replacing or clearing it requires an explicit
choice. A missing **Data as of** value never blocks freshness confirmation.

History offers **Republish this version**, not **Restore**, **Revert history**, or a destructive
**Rollback**. Its confirmation explains that Agora will pin the earlier immutable Revision as a
new Publication Version, keep later Revisions and history, keep the stable link and current Grants,
and mark the update New until opened. Do not imply that ownership, Grants, prior publication notes,
or actions are rolled back.

Show **Published update N** when Users benefit from the release number; keep **Revision N**
separate. A Publication note belongs to the current Published update. Unpublishing removes viewer
access but does not reset either number or promise automatic republish. A later republish starts
with an empty Publication note and **Freshness not provided**; never silently carry the old
freshness claim into the new update. **Data as of** has no invented or silently inherited value.

### Authorized-open analytics

Call the metric **Authorized opens**, never generic **views**, **engagement**, **visitors**, or
**activity**. Place this explanation beside every summary: **An authorized open is counted when
Agora successfully authorizes the published view. It does not show whether the dashboard loaded
or what someone did inside it.** Owner preview, failures, iframe/artifact requests, clicks,
scrolling, filters, IP addresses, user agents, and referrers are outside the metric.

Later portal pages consume only bounded daily rollups, Dashboard/viewer summaries, and Dashboard
snapshots through aggregate query interfaces. They must not import, query, list, export, or link to
raw `AuthorizedOpen` rows, including for administrators. Current project ownership gates every
aggregate; transfer removes the old owner's access on the next check. Popular-Dashboard treatment
starts from a bounded set of the viewer's currently granted Published Dashboards, uses the bounded
snapshot only to order that authorized set, and must not imply quality, endorsement, or firmwide
visibility.

## Component vocabulary

| Partial | Purpose | Key context |
|---|---|---|
| `components/page-header.html` | Page eyebrow, `h1`, intro, optional action | `title`, `intro`, `eyebrow`, optional `action_url`/`action_label` |
| `components/status-card.html` | Card with a status badge | `title`, `text`, optional `badge_label`/`badge_tone` |
| `components/card.html` | Simple text/list card | `title`, `text`, optional `items` |
| `components/status-badge.html` | Textual lifecycle/status treatment | `label`, optional `tone` |
| `components/alert.html` | Informational, success, warning, or error message | `message`, optional `heading`, `tone` |
| `components/empty-state.html` | No-results state with optional real action | `title`, `message`, optional `heading_level`, `action_url`/`action_label` |
| `components/error-state.html` | Recoverable failure state | `title`, `message`, optional retry action |
| `components/loading-state.html` | Non-blocking loading announcement | optional `message` |
| `components/data-table.html` | Overflow-safe table with caption and column headers | `caption`, `headers`, `rows` |
| `components/form-field.html` | Labelled input or textarea with help/error text | `field_id`, `field_name`, `label`, optional `error` |
| `components/form-actions.html` | Consistent submit/cancel actions | `submit_label`, optional `cancel_url` |
| `components/destructive-action.html` | Explicit confirmation before an irreversible POST | `title`, `message`, `action_url`, `action_label` |

Context values are escaped by Django. `page-header` is the page-level `h1`; card partials accept
an integer `heading_level` from 2 through 6 and default to `h2`, so callers choose the level that
matches their surrounding page. Pass a unique `heading_id` when a named region needs an explicit
`aria-labelledby` relationship. State and destructive partials intentionally do not invent IDs;
their visible headings name the content, and caller-supplied IDs may be added when a region needs
one. Do not pass uploaded HTML, CSV bytes, or arbitrary markup to these components, and do not add
`safe` rendering to make a component work. Uploaded content is never a portal template value,
portal response, `srcdoc`, or same-origin document.

## Visual language

Use the namespaced `--portal-*` CSS custom properties in `foundation.css` for color, spacing, type,
radii, shadows, and content widths. Prefer the semantic component classes (`portal-card`, `portal-button`,
`portal-alert`, and so on) over new one-off values. Keep content readable, use system fonts only,
and keep page-specific rules namespaced under `portal-*` classes.

Brand artwork lives under `static/portal/brand/`. Use the colored wordmark through the shared
`partials/brand-wordmark.html` include for the portal header, dashboard bar, and footer; use the
compact mark only where a wordmark cannot fit. Browser and installed-app icons have dedicated
web-sized files. Navy and white marks are retained as theme-ready alternatives, so a future color
change should update the shared include or selected asset rather than duplicating brand markup.

Use a native link for navigation and a native button for an action. Every button has an explicit
`type`; destructive actions use `portal-button--danger` and require a visible confirmation
checkbox plus a server-side CSRF-protected POST. Never make a whole non-interactive card
clickable.

## Accessibility and resilience

- Keep the skip link first in the body and preserve its `#main-content` target.
- Give every form control a visible label. Pair help and error copy with `aria-describedby` and
  expose invalid controls with `aria-invalid="true"`.
- Use `role="status"` for progress/success updates and `role="alert"` for errors or warnings;
  never communicate state through color alone.
- Give tables a caption when the surrounding page does not already provide one, use `scope="col"`
  on header cells, and preserve horizontal scrolling on narrow screens.
- Use the native `<details>` navigation disclosure on small screens. It remains keyboard usable
  and returns focus to its summary without a script.
- Keep `:focus-visible`, reduced-motion, `prefers-contrast`, and `forced-colors` behavior intact.
- Test at narrow widths, with keyboard navigation, and with browser high-contrast/forced-colors
  settings before adding a workflow page.

## Security and CSP

The portal CSP permits the same-origin stylesheet and the configured isolated content frame while
blocking scripts and external connections. Keep templates free of inline scripts, event-handler
attributes, inline styles, external assets, and uploaded-content interpolation. If a later
workflow needs client behavior, it must first add a separately reviewed same-origin asset and
update CSP intentionally; do not weaken the foundation policy from a template.
