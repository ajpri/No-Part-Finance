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

Outstanding input: final icon artwork/exports. All other product decisions above are approved. This entry is approved Version Next scope; this planning change does not implement it.

### Set Application Version to 26.10.18

Purpose: set the authoritative application version to the exact string `26.10.18` as a separate release-version milestone, recorded in one logical commit independent of feature commits. Completed feature commits and this version commit may be pushed upstream together.

Approved scope: update the authoritative version and any maintained current-version references or test expectations so Settings, version-bearing portable-package filenames and contents, and command-line version output consistently use `26.10.18`. Preserve the existing shared version source rather than adding independent version constants. Update SPEC.md's implemented version after verification and remove only this completed milestone. Preserve historical release identifiers and the existing grouped `26.10` Release History presentation; do not invent release notes or add a separate patch-version history entry.

Acceptance criteria: verify the exact version in Settings/API metadata, applicable command-line output, and both portable-package variants and their version-bearing contents/filenames. Update and run relevant existing version/package tests with synthetic fixtures and explicit temporary database and backup paths, importing `tests` before application modules to enable the test database guard. Never read, initialize, migrate, or write real `finance.db` during verification. Confirm remaining old-version references are intentionally historical rather than stale current-version values. Keep all version changes, corresponding test/documentation changes, and this milestone's completion update in one commit.

Explicit non-goals: implementing other Version Next milestones, changing database schema versions, changing financial or authentication behavior, redesigning Release History, or introducing automatic versioning/release tooling. This is a planned milestone, not an instruction to change the application version immediately.

Unresolved decisions: none for this scope. Approved for Version Next implementation when explicitly requested.

### Historical Category Activity When a Category Becomes a Rollup

Purpose: prevent direct historical Expense activity from disappearing from the category breakdown when a formerly standalone category gains children. Public Release 5 Part 2 reproduced a $10 parent purchase remaining in total Expenses while the new rollup and child each displayed $0.

Approved planning decision: a category may become a rollup only when zero transactions reference it. An existing unused category is eligible; it need not be newly created. Zero means no transaction references, not a zero net amount, zero selected-period activity, or zero current balance. Historical activity, including purchases and offsetting refunds, disqualifies the category regardless of date or active/archive presentation.

User-visible behavior: reject adding or moving a child under a category with direct transaction history, with concise guidance to create an empty parent and move the existing category beneath it instead. Preserve the existing one-level Expense hierarchy and compatible parent/child rules. A rollup remains nonselectable for new transaction assignments and derives its reporting amount, percentage, and per-day values from its direct children only. Do not add a Direct activity row.

Financial and data invariants: enforce the zero-reference check authoritatively in Python within the same SQLite transaction as the hierarchy change. Never recategorize, delete, or rewrite transaction history to make a parent eligible. Preserve stable IDs, ledger cents and signs, account balances, overall Income/Expense totals, and offsets. Deliberately moving an existing Expense category under an empty parent changes only its hierarchy and the corresponding rollup presentation, as already specified; retain archived history, refunds, and ordinary Hide zeros behavior. Maintain the invariant when validating new transaction assignments as well as hierarchy changes.

Meaningful edge cases: a purchase and full refund still constitute transaction history; activity outside the selected reporting period still blocks conversion; archived historical references still count. Both creating a child and changing an existing child's parent require the same check. A rejected hierarchy change leaves all configuration and history unchanged. Concurrent transaction creation and hierarchy changes must not allow direct activity to appear on a newly established rollup.

Acceptance criteria: verify that a new empty parent and an existing unused compatible category can become rollups; categories with any direct transaction references cannot, including net-zero, refunded, old, and archived activity. Verify both child creation and reparenting, clear correction guidance, unchanged state on rejection, and concurrent-write protection. Grouping an existing category beneath an empty parent preserves its transactions and IDs and produces correct child-derived rollup amounts, percentages, per-day values, refunds, archived activity, and Hide zeros behavior. Use explicit temporary database and backup paths with the test database guard; never test against real finance.db. Verify that Template and Flexible Category Budget references block conversion without changing or clearing those references. Verify that an existing rollup with direct transaction references produces a clear category-identifying reporting error instead of incomplete rollup figures. Verify that deliberate correction through existing transaction editing preserves normal financial validation and that rollup reporting resumes after all direct references are removed. Include old, archived, and net-zero direct activity; the check is not limited to the selected reporting period. Unaffected views remain usable.

Explicit non-goals: direct-parent activity reporting rows, selectable rollups, deeper hierarchy, automatic recategorization or configuration repair, new category types, stored rollup totals, and unrelated reporting changes.

Approved reference protection: block conversion of a prospective parent referenced by any Template or Flexible Category Budget, including inactive or archived references where present. Explain that the user must manually change or remove those references before converting the category. Enforce the checks in the same backend transaction as the hierarchy change; never silently clear or remap references.

Approved existing-rollup handling: if an existing rollup has any directly assigned transactions, require deliberate manual correction through existing transaction editing before reporting that rollup normally. Affected reporting must show a clear error identifying the category instead of silently omitting direct activity or presenting incomplete rollup figures as valid. Do not display a normal rollup breakdown until its direct references are removed, including references outside the selected period or with net-zero amounts. Keep transaction history and existing editing available so the user can intentionally select valid categories. Never automatically move, recategorize, delete, or rewrite transactions, and do not add a repair wizard or Direct activity row. Preserve unaffected views and existing backend validation; once no direct transaction references remain, resume ordinary child-derived reporting.

Unresolved decisions: none for this scope. Promoted to Version Next and approved for implementation by user approval.

### External Transfers in Simulator Allocation

Purpose: make External transfers follow the Simulator's existing selected Cash Live Balance pool allocation rule. Public Release 5 Part 2 reproduced $90 Starting Cash plus a $50 incoming External transfer projecting Cash at $140 while incorrectly reducing Unallocated Money to $40.

Approved user-visible behavior: an External-to-Cash transfer increases Unallocated Money by its positive Amount only when the tracked destination Cash Account is selected by a Live balance row. A selected Live balance Cash Account-to-External transfer decreases Unallocated Money by Amount. External transfers involving a Cash Account outside the selected Live Balance pool do not change the allocation summary. In every case, the tracked Account's projected balance still reflects its signed transfer side. With no other planned rows, $90 Starting Cash plus $50 External-to-selected-Cash produces $140 projected Cash and $140 Unallocated Money; a $50 selected-Cash-to-External transfer produces $40 for both.

Use the existing signed Transfers/Payments summary contribution: incoming money to the selected Cash pool contributes negative Amount to that deduction, outgoing money contributes positive Amount, and an unselected Cash side contributes zero. Do not classify External inflows as Planned Income or outflows as Planned Spending. External-to-Credit payments retain the existing Credit-payment allocation rule; this item changes only the External/Cash selected-pool treatment, without changing other transfer or Payoff rules.

Financial and data invariants: Python owns selected-pool membership, projected balances, integer-cent signs, summary contributions, and Unallocated Money. Preserve atomic two-row zero-sum ledger transfers, positive entered transfer Amount, distinct eligible Accounts, existing External selector boundaries, reporting exclusions, and the distinction between planning and authoritative ledger activity. External never appears in Results or exposed balance totals. Executed financial rows remain excluded from planned calculations; their recorded effects appear through refreshed live balances. Store no derived balances or allocation totals.

Meaningful edge cases: both directions with selected and unselected Cash Accounts; no Live balance rows; multiple selected Cash Accounts; adding or removing a Live balance selection; multiple incoming and outgoing transfers that offset; negative Unallocated Money; executed rows; archived saved references under existing rules; External-to-Credit payments and unaffected ordinary Cash/Credit transfers. Never infer pool membership from names or from merely appearing in Results.

Acceptance criteria: verify explicit expected projected balances, signed transfer-summary contributions, Starting Cash, Planned Income, Planned Spending & Buffer, and Unallocated Money in both directions and selected/unselected cases. Adding or removing the tracked Cash Account's Live balance row changes the allocation basis consistently without altering saved transfer fields or ledger data. Verify refresh and persistence, executed-row exclusion, existing Credit-payment behavior, External exclusion from Results and financial reporting, and unchanged transfer signs and zero-sum atomicity. Use synthetic fixtures with explicit temporary database and backup paths and the test database guard; never test against real finance.db.

Explicit non-goals: new allocation pools or controls, automatically selecting Live balance Accounts, treating External transfers as Income/Expenses, exposing External balances, changing the summary label, new ledger operations, stored totals, automatic recording, matching, or unrelated transfer rules.

Unresolved decisions: none for this scope. Promoted to Version Next and approved for implementation by user approval.

### Dashboard and Simulator Utilization Threshold Rounding

Purpose: make Dashboard and Simulator Credit Card utilization badges classify the same ratio consistently. Public Release 5 Part 2 found projected utilization rounded before classification in Simulator, while Dashboard classified the exact ratio. An exact 29.996% displayed as 30.00% with different badge colors across the views.

Approved user-visible behavior: classify badges using the exact ratio, then round only the displayed percentage to two decimal places. Green is below 30%; Yellow is at least 30% and below 50%; Red is at least 50%. A ratio just below a boundary may therefore display 30.00% with Green or 50.00% with Yellow. Retain concise visible percentages and descriptive accessible labels; do not add another displayed precision, tooltip, or explanatory control.

Financial and data invariants: Python owns badge eligibility and state for individual Dashboard cards, Dashboard total revolving utilization, and Simulator projected cards. Derive amount owed as max(0, -internalBalanceCents), using current balances on Dashboard and final projected balances in Simulator. Compare integer amounts against limits without floating point or rounded display values: below 30% means amountOwedCents * 100 < creditLimitCents * 30; below 50% means amountOwedCents * 100 < creditLimitCents * 50. Dashboard total uses the existing sums of eligible card amounts owed and limits. Preserve all eligibility, total inclusion, two-decimal percentage formatting, account balances, credit limits, ledger signs, projections, and reporting. The frontend renders the authoritative state instead of independently classifying the displayed percentage.

Account-specific Utilization Warning alerts retain their existing exact integer/basis-point comparisons and helper rounding; this item does not change those thresholds or their configuration.

Meaningful edge cases: exact values below, at, and above 30% and 50%; ratios that display-round across either boundary; zero debt, overpayments, utilization above 100%, missing limits, ineligible subtypes, total revolving utilization, and differences between live and projected debt. Cards absent from Simulator Results remain absent; there is no projected total-utilization badge.

Acceptance criteria: verify exact-ratio classification at both boundaries and near-boundary ratios such as 29.996% and 49.996%, while retaining two-decimal display values. Identical eligible current/projected amounts and limits yield identical states in Dashboard and Simulator. Verify the Dashboard aggregate ratio from summed eligible amounts/limits, frontend use of backend-provided states, readable narrow-screen badges, accessible labeling, unchanged account-specific warning behavior, overpayment handling, and utilization above 100%. Use synthetic fixtures, explicit temporary database and backup paths, and the test database guard; never test against real finance.db.

Explicit non-goals: configurable badge thresholds, more display precision, new warnings or tooltips, total projected utilization, schema changes, changes to account-specific warning comparisons, financial calculations beyond badge classification, or unrelated presentation work.

Unresolved decisions: none for this scope. Promoted to Version Next and approved for implementation by user approval.

### Simulator Transfer Summary Label

Purpose: resolve the conflicting Simulator summary labels Payments & Cash Transfers and Transfers & Payments with one consistent label.

Approved user-visible behavior: use Transfers & Payments for the existing Simulator allocation-summary value. Preserve the five-value order: Starting Cash, Planned Income, Planned Spending & Buffer, Transfers & Payments, Unallocated Money. Use the approved label consistently in maintained Simulator presentation, accessible labels, current user guidance, and canonical specification wording when implementation is completed and verified. Preserve historical release summaries.

Financial and data invariants: wording only. Preserve the existing transfersPaymentsCents API field, integer-cent values, signed selected-Cash-pool contribution, Credit/Payoff payment contributions, executed-row exclusions, and Unallocated Money formula. The summary remains an allocation deduction that can be signed; renaming it must not turn it into a gross unsigned transfer total or change which rows contribute. Preserve the separately approved External allocation rules without implicitly implementing that milestone.

Meaningful edge cases: a zero summary, negative net deduction from incoming selected-pool Cash transfers, offsetting Cash transfers, Credit payments, Payoff rows, External cases, and narrow-screen wrapping. These cases do not require alternative labels.

Acceptance criteria: verify the exact label and five-value order in visible and accessible Simulator presentation, with normal responsive wrapping and no overflow. Verify current documentation is aligned after implementation while historical release summaries remain intact. Assert unchanged API field, summary values, selected-pool direction, Credit/Payoff calculations, and Unallocated Money against existing fixtures; no database writes or schema change are introduced by renaming.

Explicit non-goals: changing allocation math, API field names, transaction-history summary labels, transfer types, stored values, new controls, tooltips, or unrelated presentation.

Unresolved decisions: none for this scope. Promoted to Version Next and approved for implementation by user approval.

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
