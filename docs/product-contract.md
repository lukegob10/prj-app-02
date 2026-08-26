# Agora MVP product contract

Status: **normative foundation baseline**
Effective: 2026-08-26
Source of truth: [`tickets/README.md`](../tickets/README.md) and
[`AG-001`](../tickets/mvp/AG-001-mvp-foundation.md)

This document locks the language and lifecycle used by every MVP ticket. A change to these
rules is a product-contract change and must update the backlog, tests, and security model; it
must not emerge implicitly from implementation.

## Terms

| Term | Normative meaning |
|---|---|
| **User** | An administrator-provisioned application identity with an immutable internal identifier and exactly one unique canonical SOEID. A User is active or disabled. Self-registration and user hard deletion are not MVP features. A disabled User cannot authenticate or authorize. |
| **SOEID** | The sole human-facing identity key. It is normalized once at the trusted identity boundary, stored canonically, and never selected from a browser field or untrusted header after authentication. The conservative default is trim surrounding ASCII whitespace, convert to invariant uppercase, then validate `^[A-Z0-9][A-Z0-9._-]{0,63}$`; firm-specific grammar changes require an explicit contract decision. |
| **Dashboard** | The top-level securable resource. It has an opaque stable identifier, exactly one owner, mutable safe metadata, zero or more Viewer Grants, ordered immutable Revisions, a latest-revision pointer, and an optional published-revision pointer. |
| **Revision** | One atomically committed, immutable version of a Dashboard containing exactly one HTML Artifact and zero or more CSV Attachments. A failed or staged upload is not a Revision. A Revision is never edited or independently deleted in the MVP. |
| **HTML Artifact** | The exact validated bytes of a Revision's sole HTML file. Self-contained means executable presentation dependencies are inline; only documented same-Revision CSV URLs may be runtime data dependencies. It is always hostile active content. |
| **CSV Attachment** | An immutable, validated whole-file artifact belonging to one Revision. Its logical filename is unique after platform-independent normalization. A filename is never a storage path or authorization boundary. |
| **Viewer Grant** | One unique Dashboard-to-User relationship, created or revoked by the owner using canonical SOEID. It grants read-only access to the Revision currently published for that Dashboard, including every attached CSV. It grants no draft, preview, management, or arbitrary-Revision access. Owner access is implicit and is not a self-grant. |
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
| Draft, Unpublished, Published | Publish complete owned Revision | Published | Atomically set the pointer; publishing the same Revision is idempotent. |
| Published | Republish another complete owned Revision | Published | Atomically swap one valid pointer for another. |
| Published | Unpublish | Unpublished | Clear pointer; retain Revisions and Grants. |
| Draft, Published, Unpublished | Archive | Archived | Clear pointer atomically; retain artifacts and inactive Grants. |
| Archived | Restore | Draft or Unpublished | Draft if never published, otherwise Unpublished; never auto-publish. |
| any non-Deleted state | Delete | Deleted | Clear pointer; deactivate Grants; no MVP outbound transition. |

All unlisted transitions are invalid. An Archived or Deleted Dashboard cannot publish. The
phrase "deleted revision" in AG-008 means a Revision whose Dashboard is Deleted; independent
Revision deletion is not part of this contract.

## Publication and stable URL

The stable, shareable URL is a portal-origin viewer-shell URL containing a collision-resistant
Dashboard identifier. It never contains a Revision identifier, storage key, filename,
credential, or render token. After server-side authorization, the portal resolves the pinned
Revision and frames a short-lived, Revision-scoped content-origin navigation.

The stable URL does not change across upload, republish, unpublish/re-publish, Grant changes,
archive/restore, or content-token renewal. Preview is owner-only, short-lived, Revision-scoped,
non-shareable, rendered through the same isolated content boundary, and never changes
publication state.

## Whole-CSV access

An active Viewer Grant to a Published Dashboard authorizes the viewer to retrieve **every CSV
Attachment in the pinned Published Revision**, including files the HTML does not reference.
There is no row-, column-, file-, or purpose-level filtering. Filenames provide no secrecy.

- A Grant automatically applies when a future Revision is explicitly published.
- A Grant alone exposes no Draft, Unpublished, Archived, or arbitrary Revision.
- Each HTML and CSV request still requires server-side authorization or narrowly scoped render
  authorization; opaque identifiers are not access control.
- Revoke, disable, unpublish, archive, delete, or republish ends subsequent authorization
  within the documented render-authorization window. Already received bytes cannot be recalled.
- Owner and Viewer UX must clearly explain complete-CSV visibility and user-created content.

## Role expectations

- **Owner:** manages only owned Dashboards, Revisions, Grants, preview, publication, archive,
  restore, and delete.
- **Granted viewer:** discovers only currently Published Dashboards with an active Grant and may
  view only the pinned Revision and all of its CSVs.
- **Administrator:** provisions, disables, re-enables, and resets Users. Dashboard content access
  still requires ownership or a Grant. A later break-glass operation must be separate and audited.
- **Unauthenticated, disabled, or unrelated User:** receives no metadata or existence signal;
  public-facing failures are generic.

A disabled owner loses personal access, but disabling the owner does not automatically
unpublish content for other authorized viewers. Destructive actions require explicit
confirmation and must explain viewer impact.

## Deferred scope

Deferred beyond the MVP:

- editors, co-owners, ownership transfer, groups, firmwide access, and anonymous/public links;
- self-registration, enterprise SSO, directory sync, and federated lifecycle management;
- row/column/per-attachment filtering, data masking, or download prevention;
- multiple HTML files, mutable Revisions, attachment replacement, and Revision deletion;
- external asset/network allowlists, public signed links, service workers, and CDN delivery;
- scheduled publication, approvals, collaboration, diffing, and dedicated rollback UI;
- ECS topology, production high availability, formal penetration testing, and malware services;
- guaranteed prevention of hostile browser CPU exhaustion.

Deferred from AG-001 but planned in later MVP tickets: persistence models, authentication,
authorization, Dashboard management, upload processing, artifact storage, rendering,
publishing, viewer workflows, audit/operations hardening, end-to-end verification, and release
operations.
