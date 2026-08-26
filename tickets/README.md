# Agora implementation backlog

Agora is a shared internal application for uploading, rendering, publishing, and sharing self-contained HTML dashboards with optional CSV attachments.

## MVP product contract

- Agora is one shared web application; it does not launch a container per dashboard.
- A dashboard revision contains exactly one self-contained HTML file and zero or more CSV attachments.
- Uploaded HTML may contain inline CSS and JavaScript but executes only on an isolated content origin under a restrictive browser sandbox and content security policy.
- Attached CSV files are immutable within a revision and available through documented revision-scoped relative URLs.
- Anyone allowed to view a dashboard can retrieve every CSV attached to its published revision. Row-level security is deferred.
- Local accounts are keyed by canonical SOEID. Administrators provision accounts; self-registration is disabled.
- Each dashboard has one owner. MVP sharing grants view access to explicit SOEIDs; editors, groups, firmwide access, and anonymous access are deferred.
- Publishing promotes one immutable revision to a stable URL. Later uploads stay private until explicitly published.
- HTML, CSV, and metadata are private and authorized on every request.
- ECS-specific deployment and reverse-proxy SSO are deferred.

## MVP outcome

The MVP is complete when an administrator can provision users, an authenticated owner can create a dashboard, upload and preview HTML with optional CSV attachments, grant and revoke viewer access by SOEID, and publish a pinned revision that authorized viewers can safely render while unauthorized users cannot retrieve its HTML or CSV content.

## Consolidated MVP tickets

The MVP is intentionally capped at 12 end-to-end tickets.

| ID | Title | Priority | Depends on |
|---|---|---|---|
| [AG-001](./mvp/AG-001-mvp-foundation.md) | Lock the MVP contract and establish the application foundation | P0 | — |
| [AG-002](./mvp/AG-002-metadata-artifact-storage.md) | Implement metadata persistence and private artifact storage | P0 | AG-001 |
| [AG-003](./mvp/AG-003-soeid-auth-admin.md) | Implement local SOEID authentication and user administration | P0 | AG-001, AG-002 |
| [AG-004](./mvp/AG-004-authorization-sharing.md) | Implement authorization and viewer sharing by SOEID | P0 | AG-002, AG-003 |
| [AG-005](./mvp/AG-005-dashboard-management.md) | Build dashboard ownership and management | P0 | AG-002, AG-004 |
| [AG-006](./mvp/AG-006-upload-revisions.md) | Implement HTML/CSV upload and immutable revisions | P0 | AG-002, AG-005 |
| [AG-007](./mvp/AG-007-secure-renderer.md) | Build the isolated HTML renderer and CSV delivery boundary | P0 | AG-001, AG-004, AG-006 |
| [AG-008](./mvp/AG-008-preview-publishing.md) | Implement preview, publishing, and stable dashboard URLs | P0 | AG-005, AG-006, AG-007 |
| [AG-009](./mvp/AG-009-viewer-experience.md) | Build Shared with Me and the dashboard viewer experience | P0 | AG-004, AG-007, AG-008 |
| [AG-010](./mvp/AG-010-governance-hardening.md) | Add auditability, observability, and security hardening | P0 | AG-003–AG-009 |
| [AG-011](./mvp/AG-011-automated-verification.md) | Add automated functional and security verification | P0 | AG-003–AG-010 |
| [AG-012](./mvp/AG-012-release-documentation.md) | Package, document, and accept the MVP release | P0 | AG-001–AG-011 |

The primary build path is AG-001 → AG-002 → AG-003 → AG-004 → AG-005 → AG-006 → AG-007 → AG-008/AG-009 → AG-010 → AG-011 → AG-012.

## Global definition of done

- Acceptance criteria are automated where practical.
- Authorization is enforced server-side, not only hidden in the UI.
- Persistence changes include migrations and rollback considerations.
- Failures are actionable and do not expose secrets, paths, or stack traces.
- Logs exclude passwords, sessions, render tokens, raw HTML, and CSV contents.
- Changed UI is keyboard accessible and checked for serious accessibility issues.
- Operator and author documentation is updated.
