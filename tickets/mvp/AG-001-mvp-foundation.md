# AG-001 — Lock the MVP contract and establish the application foundation

- Milestone: MVP
- Epic: Foundation
- Priority: P0
- Size: L
- Depends on: None

## Goal

Turn the agreed product into a runnable, reproducible application skeleton with explicit security boundaries and no hidden scope expansion.

## Scope

- Confirm the terms and lifecycle for User, SOEID, Dashboard, Revision, HTML Artifact, CSV Attachment, Viewer Grant, Draft, Published, Unpublished, Archived, and Deleted.
- Confirm one self-contained HTML file plus zero or more CSV attachments per immutable revision.
- Select the backend, UI approach, metadata database, migration tool, test stack, and supported runtime versions.
- Define separate portal and content origins and the authentication, storage, and rendering trust boundaries.
- Threat-model uploaded HTML, malicious CSV, IDOR, path traversal, token replay, oversized uploads, clickjacking, data exfiltration, and browser denial of service.
- Scaffold dependency locking, configuration, formatting, static checks, tests, and CI.

## Acceptance criteria

- [ ] Product terminology, lifecycle states, whole-CSV viewer access, and deferred scope are approved.
- [ ] The selected stack and supported runtime versions are recorded.
- [ ] Portal, content, identity, data, and storage boundaries are diagrammed, including local-development hostnames.
- [ ] Uploaded HTML is prohibited from executing in the portal origin or DOM.
- [ ] A new developer can install dependencies, start the development environment, and run checks from documented commands.
- [ ] CI runs install, lint/static analysis, tests, and build checks.
- [ ] Configuration fails clearly when required values are missing and no secrets are committed.

## Out of scope

- Business-feature implementation.
- ECS topology and production high availability.
