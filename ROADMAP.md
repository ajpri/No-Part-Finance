# No Part Finance Roadmap

This file contains future product work. SPEC.md describes the implemented product.

Only Version Next is approved implementation scope. Implement one named Version Next milestone at a time. Version Soon, Version Later, and Deferred Ideas are not approved until explicitly promoted.



## Version Next

### Progressive Web App Support (Online Only)

Purpose: make No Part Finance installable wherever supported while preserving the existing online application. Primary verification targets are desktop Chrome and iPhone Safari. Use standard web capabilities for broad compatibility; installation behavior depends on the browser and operating system.

Approved metadata and launch behavior:

- Full name, short name, and iOS home-screen name: `No Part Finance`. Platforms may truncate the displayed label.
- Description: `A simple, self-hosted personal finance app for tracking accounts, transactions, and spending.` The GitHub repository description has already been updated to this wording.
- Display mode: `standalone`.
- Start URL, scope, and stable manifest identity: `/`, without a hard-coded installation domain.
- Preserve the existing Dashboard launch flow, including Sign In or Owner-configuration handling when required.
- Manifest launch background: `#202124`. This is a browser launch hint, not a guarantee about every platform's launch screen; preserve the application's light-theme background.
- Manifest theme-color fallback: `#202124`. Supported browser/system UI theme color follows the existing effective application theme: `#FFFFFF` for Light and `#202124` for Dark. Honor saved Light/Dark preferences, System changes, and the existing browser-storage fallback and pre-stylesheet theme initialization, including authentication pages.

Implementation scope: add a web app manifest, page metadata, and minimal explicit public static-asset serving. Manifest and icons must be available before sign-in, with appropriate content types and no private information. Expose only intended public files, never arbitrary repository or runtime files. Preserve search-indexing restrictions. Include the assets in portable application packages under existing package permissions. Use native browser installation controls and document desktop Chrome installation and iPhone Safari Add to Home Screen; no custom install UI is required. The approved deployment is HTTPS at the domain root, with HTTPS handled by the existing reverse proxy. Preserve local development behavior and document applicable browser installation limits.

Confirmed asset paths (filenames are final):

| Path | Dimensions | Purpose |
| --- | --- | --- |
| `assets/icon-192.png` | 192 × 192 | Standard manifest icon |
| `assets/icon-512.png` | 512 × 512 | Standard manifest icon |
| `assets/icon-maskable-512.png` | 512 × 512 | Separate maskable icon with an opaque edge-to-edge background |
| `assets/apple-touch-icon.png` | 180 × 180 | iPhone/iPad home-screen icon |

Artwork is intentionally pending. Keep `assets/README.md` as the placeholder and handoff instructions; do not invent artwork or generate placeholder PNGs. A square SVG or high-quality 1024 × 1024 PNG source may be supplied later. Essential maskable artwork stays inside the centered circle with diameter 80% of the image width; do not bake rounded corners into artwork. The user explicitly approved wiring the confirmed icon URLs while artwork is pending; these URLs may return 404 until the artist supplies the exports. Implement metadata and serving now without placeholder images. Icon-dependent acceptance and complete installability remain pending supplied artwork and device verification.

Financial and security invariants: preserve Python authority, integer-cent amounts, balances, transfer atomicity, history, reporting, Owner/Auditor permissions, sessions and invalidation, CSRF protection, and trusted self-hosted hosting assumptions. Installation grants no additional access and changes no ledger or planning state. Add no schema or financial state. Public metadata and assets must disclose no financial records, credentials, configuration secrets, or session values.

Acceptance criteria:

- Verify manifest JSON, content type, identity, names, description, standalone display, root start URL/scope, colors, and page links on relevant application and authentication pages. Verify only intended public files are accessible before sign-in.
- After artwork is supplied, verify all four exact asset paths, actual PNG dimensions and content types, standard versus maskable purposes, iOS touch-icon metadata, maskable composition, and portable-package inclusion.
- Verify theme-color initialization and updates for Light, Dark, and System, including operating-system changes, refresh, Sign In, Owner configuration, and unavailable browser storage, without changing existing theme behavior.
- Verify installation and launch in desktop Chrome and on a real iPhone through Safari: name/icon, standalone presentation, Dashboard launch, authentication when needed, logout, and ordinary navigation/forms. Record browser/device versions and any unavailable manual checks; never claim unperformed device verification. Ordinary browser access remains available where installation is unsupported.
- Verify online reads and an authorized save retain existing backend validation, authorization, and CSRF protection. Disconnected launch or requests use ordinary browser/network failure behavior, with no cached Dashboard snapshot, queued writes, or automatic replay. Introduce no service worker or PWA-specific financial cache.
- Run focused metadata/static-route/package and theme checks plus relevant existing regression tests. Application tests use synthetic fixtures, explicit temporary database and backup paths, and import `tests` before application modules to enable the test database guard. Never read, initialize, migrate, or write real `finance.db` during verification.
- Update maintained installation documentation. Merge enduring behavior into canonical SPEC.md sections only after implementation and verification. Missing artwork or device verification remains explicitly unresolved rather than being marked complete.

Explicit non-goals: offline support, service workers, offline snapshots, Cache API storage, write queues/retries, push notifications, background sync, app stores, custom splash images, custom install banners/buttons, new hosting or certificate management, subpath deployment support, new dependencies/build tooling, or unrelated redesign.

Implementation status: online-only manifest, public asset routes, shared theme metadata, package inclusion, and installation guidance are implemented and tested with synthetic fixtures. Browser smoke checks verified existing sign-in, an online save, Light/Dark theme-color changes, logout, and theme persistence. The milestone remains open for final icon artwork/exports, PNG dimension/composition validation, and actual installation/launch verification in desktop Chrome and on a real iPhone. Missing icons intentionally return 404 with user approval; no placeholder PNGs are shipped.

### Dashboard Response Fields Required by the OpenAPI Contract

Purpose: make Dashboard JSON responses satisfy their published OpenAPI contract so integrations can interpret Account eligibility and displayed events without guessing. Public Release 5 Part 3 found required ordinaryEligible and transferEligible absent from Dashboard Account records and required combinedTransfer absent from pinned quickUpdates records.

Approved behavior: return the promised Boolean fields rather than remove or weaken their required status in the published contract. Dashboard Account records expose ordinaryEligible and transferEligible from the existing authoritative Account eligibility rules. Active/archive status and System boundaries remain authoritative: External is ineligible for ordinary entry and eligible only under existing transfer rules wherever an applicable response includes it. Do not add External to Dashboard financial Account lists to expose these fields.

Dashboard recent-event and pinned-event records expose combinedTransfer according to existing event grouping. A valid combined transfer event has true; an ordinary pinned transaction has false. Visible invalid linked rows remain underlying rows rather than being falsely marked as combined events. Preserve existing response collections, event grouping, IDs, fields, and eligibility semantics; reuse established backend metadata rather than add a second eligibility calculation.

User-visible behavior: integrations receive the documented yes/no answers consistently. The browser Dashboard retains its existing appearance and workflows. These fields describe existing eligibility and grouping; they do not grant permission or replace backend write validation.

Financial and data invariants: preserve balances, integer-cent ledger and display signs, reports, transfer zero-sum integrity, Account eligibility, active/archive behavior, External exclusions, recent/pinned inclusion and ordering, and existing role permissions. Financial JSON never exposes integration secrets, user password hashes, session tokens, or other credentials. Reading Dashboard performs no database or planning-state mutation.

Meaningful edge cases: populated and empty responses; active Cash and Credit Accounts; archived Accounts where an existing response legitimately includes them; System Account boundaries; ordinary Income/Expense/refund/reversal events; valid combined transfers/payments; invalid linked rows; ordinary pinned events; Owner, Auditor, and Bearer read access under their existing permissions. Missing optional configuration must not omit these required Boolean fields.

Acceptance criteria: validate populated Dashboard Account, recent-event, and pinned quickUpdates records against the published required fields and Boolean types, including ordinary events and valid combined transfers. Verify values agree with existing authoritative eligibility/grouping rules and that invalid linked rows remain visible without combined classification. Verify empty responses, External exclusions, unchanged financial values and browser presentation, read-only behavior, permissions, and credential isolation. Test the integration Dashboard response against the approved OpenAPI contract and applicable shared Dashboard response paths using synthetic fixtures, explicit temporary database and backup paths, and the test database guard; never test against real finance.db. Align maintained public documentation after implementation without weakening the contract.

Explicit non-goals: new eligibility rules, fields granting write authority, direct integration ledger writes, changing Dashboard collections or financial calculations, exposing External balances, new API operations, schema changes, credential disclosure, or unrelated contract cleanup.

Unresolved decisions: none for this scope. Promoted to Version Next and approved for implementation by user approval.

## Version Soon

No milestones are currently listed.

## Version Later

These ideas preserve product direction but are not approved implementation scope. Promote and fully design one before implementation.

### Read-Only Offline Dashboard (Separate from PWA Installation)

Deferred and not approved as part of online-only PWA support. Consider a read-only offline Dashboard snapshot. Offline mode would cache the latest balances, reporting state, and seven days of recent events on the current browser, clearly label stale data, disable writes and unavailable features, and recover only after a real API refresh. It must not queue writes or create server-side state. HTTPS remains the reverse proxy's responsibility unless separately designed.

### Automatic Backups and Production Server

Consider one consistent SQLite backup before the first successful write of each server-local day, with integrity verification, bounded retry, feature-owned retention, and a persistent failure banner that never blocks the authoritative write. Manual and migration backups remain separate. Tests use explicit temporary paths.

Replace Flask's development server with Waitress for normal startup while retaining portable Windows and Linux launch scripts and existing binding and reverse-proxy behavior.

### Sensitive Financial Information

Do not store full card, routing, ACH, or CVV credentials as ordinary fields. Prefer a password manager and retain only non-secret references such as institution, nickname, last four digits, or payment URL. Any in-application vault requires complete authentication, encryption, key-management, recovery, display, backup, and deployment-security design.

### Primary Navigation

Consider a plain, accessible hamburger menu shared by desktop and mobile while preserving Settings and all destinations. Do not introduce a persistent sidebar, bottom navigation, or separate mobile model.

### New Transaction URL

Prefer a #new fragment on the current page so opening, closing, refresh, Back, and Forward preserve the underlying page state. Apply it only to ordinary transaction creation and reuse the existing form. Decide separately whether /new redirects, remains compatible, or is retired.


### Future Integration Extensions

Authenticated financial JSON reads, ordinary transaction proposals, durable retry identity, public agent documentation, and Owner review are already implemented in SPEC.md. Do not duplicate them with a direct-to-ledger transaction API. Any additional integration operations require separate scope and approval while preserving the current human-review boundary.

### Telegram and Daily Summaries

Consider opt-in Telegram notifications after selected successful ledger writes. Delivery occurs only after commit; failure never rolls back or casts doubt on the financial write. Credentials require masking and backup disclosure. Simulator-only activity is excluded, and the initial design has no retry queue, history, worker, or automatic retry.

Scheduled Telegram, SMTP, or companion-service summaries require separate decisions about content, cadence, timezone, credentials, privacy, and reliability.

### Purchase Financing

Consider informational tracking for 0% financing or installments. It must not count proceeds as income, double-count repayment expense, create another ledger, or automatically alter transactions.

### Merchant Grouping and Description Type-Ahead

Consider user-approved reporting rules that group full descriptions under merchant names without changing the source text. Fuzzy matching may suggest but never apply rules. Description type-ahead may suggest prior full descriptions but must not rewrite text or add a required field.

### Save Transaction as Template

An ordinary transaction editor may open the existing template dialog with Account, Description, Category, and user-facing Amount prefilled. Template Name remains required, all prefills remain editable, archived references are omitted, and saving never changes or links the source transaction. Transfers are ineligible.

### Statements and Export

Consider printable and CSV statements for selected accounts and an inclusive range. Each account includes its starting balance, matching transactions, and ending balance, including balanceOffsetCents. Multi-account CSV includes Account.

### Savings Goals

Savings goals may eventually replace parts of the old bucket concept. No design is committed.
## Deferred Ideas

The following are not current implementation requirements unless deliberately promoted into a future release:

```text
Additional expense analysis
Additional reporting periods
Net worth
Bank integrations
Recurring transaction automation
Other financial tools
```

Do not build scaffolding for deferred features unless a committed release actually requires it.
