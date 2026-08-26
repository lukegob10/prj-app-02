# Portal UI conventions

Agora's portal is a trusted, server-rendered Django surface. Templates live under
`src/agora/portal/templates/portal/`, and the shared visual language is committed in
`src/agora/portal/static/portal/foundation.css`. There is no portal JavaScript, external font,
remote image, or third-party stylesheet in the foundation.

## Shell contract

Extend `portal/base.html` for every portal page. It provides the document language, title and
description slots, same-origin stylesheet, first-focusable skip link, header, primary navigation,
main landmark, and footer. The page template owns one descriptive `h1`; section headings begin at
`h2` and proceed without skipped levels.

The shell renders only navigation items supplied by the view. A future page may provide a
`nav_items` sequence containing `label`, `url`, and optional `current`; do not add links for a
route until that route exists. An optional `account` value can provide `name` and `logout_url`
when authentication is implemented. The current foundation passes only the real home route and
does not invent an account or dashboard state.

## Component vocabulary

| Partial | Purpose | Key context |
|---|---|---|
| `components/page-header.html` | Page eyebrow, `h1`, intro, optional action | `title`, `intro`, `eyebrow`, optional `action_url`/`action_label` |
| `components/status-card.html` | Card with a status badge | `title`, `text`, optional `badge_label`/`badge_tone` |
| `components/card.html` | Simple text/list card | `title`, `text`, optional `items` |
| `components/status-badge.html` | Textual lifecycle/status treatment | `label`, optional `tone` |
| `components/alert.html` | Informational, success, warning, or error message | `message`, optional `heading`, `tone` |
| `components/empty-state.html` | No-results state with optional real action | `title`, `message`, optional `action_url`/`action_label` |
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

The portal CSP permits the same-origin stylesheet and the configured future content frame while
blocking scripts and external connections. Keep templates free of inline scripts, event-handler
attributes, inline styles, external assets, and uploaded-content interpolation. If a later
workflow needs client behavior, it must first add a separately reviewed same-origin asset and
update CSP intentionally; do not weaken the foundation policy from a template.
