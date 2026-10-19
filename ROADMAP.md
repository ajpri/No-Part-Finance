# No Part Finance Roadmap

This file contains future product work. SPEC.md describes the implemented product.

Only Version Next is approved implementation scope. Implement one named Version Next milestone at a time. Version Soon, Version Later, and Deferred Ideas are not approved until explicitly promoted.



## Version Next

### Version 26.11 — Ledger Isolation Foundation and Portable Downloads

#### Purpose and scope

Prepare multiple independent ledgers within one `finance.db`. Financial records belong to a Ledger, independently of the logins allowed to use it. This milestone delivers schema plumbing, enforced authorization, migration, and ledger-scoped web downloads. Normal setup continues to support the existing one Owner and optional read-only Auditor for the initial ledger.

Do not add registration, invitations, additional-ledger provisioning, household sharing, a Member role, multiple-membership management, a ledger-switching dropdown, or new user-management screens. The database must support future multiple memberships and roles without restructuring financial records. These future workflows remain unimplemented.

#### Application version and sign-in display

Set the authoritative application version to the exact string `26.11`. Use one central variable for current-version references, including Settings, command-line output, version-bearing application package filenames, and the sign-in screen. Preserve historical release labels and distinguish database schema versions from application versions.

Display `No Part Finance 26.11` at the bottom center of the sign-in screen, with the version populated from that central variable. Keep it readable on desktop and mobile.

#### Ledger identity and access schema

Add `ledgers` with an independently generated UUID primary key. Preserve existing integer record IDs, including User, Account, Category, transaction, template, Budget, Simulator, integration, and pending-submission IDs. UUID secrecy is not authorization.

Each Ledger has a required nonblank stored `name`. Initialize it once from the initial Owner's username as `<username>’s Finances`, for example `austin’s Finances`. Names need not be globally unique and never determine identity or authorization. Later username changes do not rename the Ledger. Preserve the name in both portable downloads. Naming/editing UI and a ledger dropdown are deferred.

For migration or fresh initialization without an Owner, use the nonblank provisional name `Finances`; replace it with the username-based default atomically when first-time Owner setup succeeds. Do not require an Owner merely to initialize the schema or invent credentials. The UUID and existing financial records remain unchanged during naming.

Keep login identities, unique installation-wide usernames, password hashes, and authentication versions in `users`. Add `ledgerAccess` containing `userId`, `ledgerId`, and `role`, with foreign keys and a unique `(userId, ledgerId)` pair. Move authoritative roles out of the globally unique User role into these access records. Supported roles remain `owner` and `auditor`.

Enforce at most one Owner access record per ledger with a database uniqueness constraint; transactional setup and management ensure exactly one Owner for each usable ledger. Household sharing will retain one Owner and add a future Member role between Owner and Auditor, not additional Owners. Do not impose permanent uniqueness on `ledgerAccess.userId`; the structure must allow a future User to own a Ledger and be a Member or Auditor of others. Future Member permissions are undecided; do not implement that role now. Adding future roles may require an explicit role-constraint migration but must not require financial-table redesign. Retain the current at-most-one-Auditor limit through supported operations without permanently restricting future Auditor memberships.

For this milestone, supported setup and management operations transactionally enforce exactly one Owner and at most one Auditor for the initial ledger, and one membership per configured User. Preserve existing command-line user-management behavior for that ledger. No supported operation provisions additional ledgers or memberships. Owner absence must never permit financial access; preserve the first-time Owner setup path. Future sharing must preserve exactly one Owner, but its management workflow is deferred.

#### Ledger-owned records and constraints

Add a required `ledgerId` to ledger-owned Accounts (including External), Categories, transactions, templates, Sinking Funds, flexible Category budgets, expected Income, planned Credit payments, Simulator plans and rows, integration credentials, and pending submissions. Scope ledger-specific settings by ledger; keep installation paths and deployment configuration installation-wide. Audit all existing settings and financial state so no ledger-owned state remains globally shared by accident.

Scope existing ledger settings only. Preserve USD, calendar-year reporting, and current date/number presentation. New currency, fiscal-year-start, and locale settings are deferred; do not store configuration that the application does not honor.

Convert relevant global uniqueness rules to ledger-local rules, including Account, Category, and template names, External system identity, and ledger-setting keys. Each ledger has its own External System Account. Keep login usernames and integration secret values unique installation-wide.

Use composite foreign keys and supporting uniqueness constraints to enforce matching ledger identities across references: transaction Account and Category; Category parent; template Account and Category; Budget references; Simulator plan, Accounts, Category, and executed transaction; pending submission integration and resulting transaction. Child `ledgerId` must agree with its parent. Preserve existing deletion, archival, and Simulator unlinking behavior without clearing a surviving record's ledger identity.

Both rows of a transfer must belong to the same ledger, remain atomically managed, and sum to zero. No transfer may cross ledgers. Preserve all existing financial meaning, dates, cents, offsets, reporting roles, calculations, and historical behavior.

#### Server-side authorization and request isolation

Resolve the authorized ledger from the authenticated User's current database access relationship and server-side session. Never trust a submitted User, Owner, or Ledger ID as permission. No client-controlled active ledger or selection workflow is introduced. Missing, invalid, or ambiguous access fails closed; never fall back to the first ledger or an installation-wide view. Invalidate existing sessions on upgrade so the new authorization context is established after sign-in.

Every financial read and write must use this authorized context: Dashboard, transaction history/search/filter/detail, transfers, reconciliation, Account/Category/template management, Budget, Simulator, linked-ledger actions, reports, helper operations, settings, integrations, pending review, and downloads. Aggregates include only authorized records. Creates obtain their ledger identity server-side. Edits and deletes scope the target by ledger. Validate every supplied reference within that ledger before writing; mixed local and foreign references fail atomically. An inaccessible record receives the same not-found response as a nonexistent one without disclosing its existence or content.

Each integration key is permanently assigned to one ledger; reassignment is unsupported. Access to another ledger requires a separate key. Integration requests resolve their ledger from their authenticated credential. Submitted IDs cannot override that context. Scope integration reads, proposals, retry identity, and review to the credential's ledger. Preserve the existing human approval boundary and browser/integration credential separation.

Ledger context is mandatory for financial functions: an absent context must fail rather than run an unscoped query. Resolve current membership and role on each request so access removal or role changes affect existing sessions on the next request. Keep request context local to each request; never use a shared mutable active-ledger value. Audit settings, temporary files, and in-memory state for accidental cross-ledger sharing.

Preserve random opaque sessions, password handling, authentication-version checks, expiration, logout, CSRF, and Auditor read-only restrictions. The future multi-ledger and self-hosted forms use the same application and database structure with deployment configuration differences; do not create separate product implementations or change the trusted deployment boundary.

#### Per-ledger audit recording

Record all persistent ledger changes starting when this feature is installed: transactions, transfers, reconciliation, Account/Category/template management, Budget, Simulator, ledger settings, integration management, and proposal submission and review. Do not reconstruct historical events or record ordinary views, searches, and unsaved previews. An audit viewer is deferred.

Each audit event records its ledger, change time, actor identity and retained actor label, action, affected record identity, and relevant before/after values. Identify integration submissions and the approving Owner separately where both participate. Record actors from trusted execution context, including authenticated Users, integrations, and explicit command-line/system operations; never trust submitted actor IDs. Audit events are append-only through supported operations and survive deletion or renaming of the affected record or actor.

Commit audit events in the same SQLite transaction as their associated persistent changes. Audit failure rolls back the change, and change failure leaves no successful audit event. A transfer and its audit events remain atomic. Never record passwords, password hashes, session/CSRF tokens, integration secret values, or other authentication secrets in audit content; record credential lifecycle actions without secret values. Audit records do not affect financial balances or reports.

Scope audit records and references to their ledger and include that ledger's audit history in both portable downloads. Exclude other ledgers' audit content. Export-only disabling must not modify live audit history. No audit viewer, activity feed, retention/deletion UI, or new attribution fields on every financial table are introduced.

#### Sign-in hardening

Preserve the existing per-client-IP sign-in rate limit and add a temporary per-normalized-username limit so distributed attempts against one login are also constrained. Five consecutive invalid credential attempts trigger a five-minute cooldown for that normalized username. Successful sign-in resets its failure count; expiry clears the cooldown and count, and attempts during cooldown do not extend it. Apply equivalent handling to existing and nonexistent usernames and retain generic failure responses, such as `Unable to sign in. Try again later.` Limits and counters are server-authoritative and concurrency-safe; bounded expiry cleans up old entries. Preserve the current temporary IP behavior. Temporary cooldown is distinct from administrative account disabling; do not add permanent account lockout, CLI enable/disable controls, registration, sign-up workflows, or new authentication dependencies in this milestone.

Add a nullable last-successful-login timestamp to each User. On successful credential authentication and session establishment, record the server's current time as a UTC timestamp; failed attempts, cooldown responses, ordinary session activity, and logout do not update it. Existing and newly created Users start with no recorded login until their first successful sign-in after implementation. Never infer prior login history. Preserve this User metadata in ledger-scoped downloads. Exposing this timestamp in CLI user management is Version Soon scope. Do not add a last-login display or new user-management UI in this milestone. This is authentication metadata, not a financial transaction or an event for ordinary ledger viewing. Administrative account disabling remains separate Version Soon design work.

#### Integration secret enable and disable controls

Expose reversible `Disable` and `Enable` actions in the Owner-only Integration Secrets table, with a clear textual status of Enabled, Disabled, or Revoked. New secrets start Enabled. Disabling retains the secret value, label, proposal attribution, and pending submissions but immediately denies authentication with that secret; enabling restores authentication using the same value. Actions require normal server-side Owner authorization, ledger scoping, and CSRF protection. No secret is revealed as part of a state change.

Preserve Revoke as a separate permanent action. Revoked secrets cannot be enabled. Distinguish reversible disabled state from revocation in the schema and API; migrate existing inactive/revoked secrets as Revoked, never as re-enableable Disabled. Preserve pending-review behavior and history; changing credential state neither approves nor rejects proposals and creates no financial activity. Update public integration documentation to explain these states.

#### Migration and initialization

Use the shared schema initialization path and explicit `PRAGMA user_version` migrations. Before changing an existing database, create a consistent SQLite backup; backup failure prevents migration.

Atomically create one Ledger, attach the existing Owner and optional Auditor, assign all existing financial records and ledger settings to it, rebuild affected tables, recreate indexes and triggers, and validate before committing. Preserve all existing IDs, amounts, dates, descriptions, offsets, transfer links, archives, reporting roles, Budget configuration, Simulator execution links, credentials, integration enabled/disabled states, and pending-review state. Do not manufacture or rewrite financial history.

A database with no Owner must retain first-time setup and attach its Owner to the initial ledger before access. Fresh installation reaches the same final schema. Failed migration rolls back completely; restarting must not duplicate ledgers or memberships. Verify foreign-key integrity and unchanged financial totals.

#### Owner-only portable web downloads

Both existing web download actions remain Owner-only. Auditor accounts are included in the ledger export but cannot initiate either download.

- Download Backup returns a standalone SQLite database containing only the authenticated Owner's current ledger.
- Download Application includes application files plus that same ledger-scoped database, ready for independent self-hosting under the existing deployment instructions.

Use one shared ledger-export implementation for both actions. Include the ledger UUID, all ledger-owned financial records and settings, the current Owner and optional Auditor User records and access relationships, password hashes and authentication versions, integration credentials with their original secret values and enabled/disabled states, and pending submissions. Preserve IDs and relationships. Exclude every other ledger, User, membership, and financial record, active sessions, and installation-specific paths or deployment secrets. No secret rotation, revocation, disabling, or source-database mutation occurs during export.

Provide a clearly labeled native checkbox, `Disable integration keys in downloaded copy`, for both database-inclusive web downloads. Default it to unchecked. When unchecked, preserve every secret's state. When checked, change only Enabled secrets to Disabled in the delivered database; retain values and labels, keep already Disabled secrets Disabled and Revoked secrets Revoked. Never modify the live instance. Both download actions use the same option and export behavior. This is an explicit exception to default state preservation above. Revoked keys cannot be restored through this option.

Show a clear notice beside the web download actions, equivalent to:

> This download contains login password hashes and integration keys. Store it securely. If you run this copy, existing integration keys will also work on that instance.

The copy may be retained offline, used as a backup, or run independently. Existing Owner and Auditor passwords continue working after startup; everyone must sign in again. The individual instance and a centrally hosted installation use the same product and schema. Integration keys retain their original state on both copies.

Create a consistent source snapshot using SQLite's supported backup mechanism, then build the delivered database fresh from only authorized records. Do not distribute a whole-installation database with other ledgers merely deleted, because deleted content can remain in SQLite pages. Keep temporary snapshots and export files outside downloadable paths and remove them after success or failure. Validate integrity, foreign keys, the exact exported ledger/User set, and financial preservation before delivering; failures produce no partial download.

The exported file is a supported application database that can be placed at the configured database path of a stopped independent instance and opened with a compatible application version. No import wizard, merge into a populated installation, synchronization, or new restore UI is included. Full-installation command-line backups continue to include all ledgers and Users using SQLite's supported backup mechanism. Future shared-ledger exports will need explicit credential-export policy before sharing is enabled; do not guess how other members' credentials should be handled.

#### Verification and acceptance

Use explicit temporary database and backup paths with synthetic fixtures and the existing in-process production-database guard. Do not read, initialize, migrate, or write real `finance.db` during automated verification.

Verify:

- Migration preserves IDs, relationships, credentials, history, and exact financial totals; backup failure and forced migration failure leave no partial upgrade.
- Fresh setup, existing Owner/Auditor management, repeat startup, session invalidation, and financial regression tests pass.
- Two synthetic ledgers and their Owners/Auditors remain isolated across all reads, writes, searches, aggregates, helpers, and integration operations.
- Substituting known foreign IDs, injecting ledger IDs, and mixing local and foreign references disclose nothing and change nothing. Database constraints independently reject cross-ledger references; multirow failures roll back.
- Synthetic access records can represent one User accessing several ledgers and one Owner and an Auditor accessing a ledger without financial-table changes; a second Owner for that ledger is rejected by the database. Unsupported ambiguous browser memberships fail closed; no switching or sharing UI is added.
- Both web downloads are Owner-only and contain exactly the current ledger, its Owner, and optional Auditor, including unchanged password hashes, integration keys, enabled states, pending submissions, and financial relationships. No other ledger content or installation secrets appear in the delivered files.
- Export operates from a consistent snapshot, cleans up temporary files on success/failure, and leaves the source unchanged. An exported database and application package start in an isolated temporary instance; existing credentials work and integration keys retain their state unless explicitly disabled in the downloaded copy. Full-installation backups still retain all ledgers.
- Revocation and role changes affect existing sessions on the next request; missing ledger context fails closed. Concurrent requests from different Owners never inherit another request's ledger context.
- Integration Enable/Disable persists across refresh and restart, denies/restores authentication with the same key, and is restricted to the authorized Owner. Existing revoked secrets remain permanently revoked; failures preserve state and proposal history.
- The download checkbox defaults unchecked. Both downloads preserve states by default and optionally disable only enabled keys in the exported copy, without changing live states or enabling revoked keys.
- Audit events cover persistent changes across every listed area, identify the trusted actor and relevant before/after values, survive record deletion, exclude authentication secrets, and remain ledger-isolated. Forced audit-write and financial-write failures prove atomic rollback. Portable downloads retain only their ledger's audit history; no historical events are fabricated.
- Integration keys cannot be reassigned between ledgers. Existing settings remain isolated without adding currency, fiscal-year, or locale behavior.
- Sign-in limits work per client IP and per normalized username, including concurrent attempts and attempts from multiple IPs. Cooldown recovery works without permanent lockout; nonexistent usernames receive equivalent generic behavior. Tests use a controllable clock rather than real waits.
- The username cooldown starts after five consecutive invalid attempts, expires after five minutes without extension by blocked attempts, and resets after success or expiry; the existing IP limit remains independent. Last successful login records server UTC only after successful sign-in, remains nullable until first successful sign-in, survives restart/export, and is unchanged by failures or ordinary session activity.
- Ledger names initialize once from the existing or newly configured Owner, remain independent of later username changes, allow duplicates across ledgers, and survive both portable downloads. Owner-less initialization uses the provisional name until successful Owner setup; no naming UI is added.
- The download notice is present, and current-version references all use the central value. The sign-in version remains readable at desktop and mobile widths.

## Version Soon

These milestones expose the Version Next ledger foundation through command-line user management. They remain deferred until explicitly promoted. Each login has one membership in this release, each ledger has exactly one Owner and at most one optional Auditor, and sign-in automatically resolves that ledger. Do not repeat the Version Next migration or add web registration/account management, standalone ledger renaming, household Members, multiple-ledger memberships, or a ledger dropdown. No Owner or ledger deletion is introduced.

### CLI Multi-Owner Provisioning and User Management

Extend existing command-line user management to list Owners alongside their stored ledger names and select the intended ledger before managing its Owner or optional Auditor. Provide Create Owner. Keep password entry protected and preserve existing password validation and confirmation behavior.

Creating an Owner atomically creates their login, a fresh UUID Ledger named `<username>’s Finances`, Owner access, the ledger's External System Account, and default ledger settings. Start without user-owned Accounts, Categories, templates, transactions, Budget plans, Simulator plans, or integrations. Do not copy another ledger's configuration or financial records. Failures leave no partial User, Ledger, access relationship, or initialization records.

After selecting a ledger, support its Owner's username/password management and creation, username/password management, and removal of its optional Auditor using the existing CLI workflow. No operation may accidentally target another ledger. Creating an Auditor creates a separate login linked only to the selected ledger; reusing a User already assigned elsewhere is unsupported in this release. Removing an Auditor invalidates their sessions and removes their access while preserving financial history and audit attribution.

Enforce installation-wide username uniqueness with existing normalization rules for creation and renaming across both roles, including disabled logins. Changing an Owner's username atomically renames their Ledger to `<username>’s Finances`; changing an Auditor's username does not rename the Ledger. This explicitly supersedes Version Next's placeholder-name rule that later username changes leave ledger names unchanged. Do not expose standalone ledger renaming. Password resets invalidate all sessions for the affected User and leave other Users' sessions unaffected.

Keep persistent ledger changes audited with trusted CLI actor attribution. Existing migrated Users, credentials, and records remain usable. Sign-in and all financial operations automatically use the login's only authorized ledger; the Version Next isolation and portable-download rules continue to apply across independently provisioned ledgers.

Verify atomic Owner provisioning and rollback, fresh-ledger contents, optional Auditor limits and removal, explicit target selection, duplicate/normalized username conflicts, atomic Owner/ledger renaming, Auditor rename independence, password-reset invalidation, audit preservation, and unchanged existing ledgers. Exercise two CLI-provisioned Owners and their Auditors through browser/API reads and writes, integration authentication, settings, and both portable downloads to confirm isolation. Use guarded temporary databases and synthetic records only.

### CLI Login Enable and Disable

Expose Enable/Disable controls for the explicitly selected ledger's Owner and optional Auditor. Show administrative Enabled/Disabled status separately from the temporary sign-in cooldown. Disabling a User invalidates their sessions and blocks login until explicitly re-enabled, without deleting records or changing their password. Disabling an Auditor affects only that login; it does not disable the Owner or integrations.

Disabling an Owner also immediately blocks integration authentication for that Owner's ledger. Implement this as an effective authorization gate, preserving each key's own Enabled/Disabled/Revoked state. Re-enabling the Owner restores use of keys that are still individually Enabled; individually Disabled or Revoked keys remain blocked. Do not automatically disable the Auditor. Keep these operations ledger-scoped and audited; failures roll back atomically. Existing Users default Enabled when this feature is installed. Preserve administrative status in portable downloads; disabling keys only in a downloaded copy must not change User status.

Verify session invalidation, persistence across restart/export, blocked browser and integration requests, unchanged key states, successful re-enable requiring fresh browser sign-in, Auditor independence, and isolation from other ledgers.

### CLI Last Successful Login Display

Expose each User's last-successful-login timestamp in CLI user management alongside their administrative login status, using the metadata recorded by Version Next. Display the server's local time with its timezone identified, and `Not recorded` when no timestamp exists. This is a read-only display; viewing user management must not update login metadata. Verify Owner and Auditor listings, missing timestamps, timezone presentation, and unchanged stored values.

## Version Later

These ideas preserve product direction but are not approved implementation scope. Promote and fully design one before implementation.

### Per-Ledger Audit Log Viewer

Expose the audit events recorded by Version Next in an authorized ledger-scoped viewer. Show who changed what and when, including relevant before/after values and integration submission/approval attribution. Design viewing permissions, filters, and presentation before implementation. Preserve secret exclusion and historical audit integrity; a viewer does not authorize editing or deleting audit events.

### Web Registration and Account Management

Bring registration and relevant CLI user-management workflows into the web UI. Design registration eligibility (open, invited, or administrator-provisioned), initial Owner/ledger creation, username and password management, and administrative enable/disable permissions before implementation. Do not assume a new installation-administrator role or unrestricted public registration. Preserve ledger isolation, authentication controls, and the trusted deployment boundary.

### Household Sharing and Member Permissions

Support household collaboration with exactly one Owner per ledger, a future Member role between Owner and Auditor, and read-only Auditor access. Define the complete permission boundaries, including financial writes, configuration, membership management, integrations, downloads, and audit viewing. Do not add multiple Owners to a ledger. Decide whether and how the current one-Auditor limit changes before implementation.

### Invitations and Ledger Access Management

Design how existing or new Users are invited to a ledger, accept access, change roles, and lose access. Preserve one membership per User-Ledger pair and exactly one Owner; permission changes must affect current sessions on the next request. Define invitation identity, expiry, revocation, and authorization rules before implementation. Do not infer permissions from a supplied User or Ledger ID.

### Multiple-Ledger Memberships and Ledger Dropdown

Allow one login to own a ledger and be a Member or Auditor of others, and to own multiple independent ledgers. Show a dropdown containing only authorized ledgers, identified by their stored names. Define initial selection, switching, drafts, navigation, concurrent tabs, and revoked-access behavior before implementation. Verify the selected membership server-side on every request; switching must never leak records, settings, credentials, or cached state between ledgers.

### Independent Ledger Naming

Allow authorized editing of a ledger's stored name independently of its Owner's username. Define naming permissions and validation, and explicitly replace the temporary CLI username-derived renaming rule when introduced. Preserve the ledger UUID, financial records, and access relationships; names remain presentation values rather than security identities.

### Shared-Ledger Portable Downloads

Extend ledger-scoped backups and application packages for shared ledgers and Users with memberships elsewhere. Decide which User records, memberships, password hashes, and credentials may be included and who may download them. Do not expose another member's login credentials by default or copy access to unrelated ledgers. Preserve standalone portability, audit history, integration-state options, and source-instance isolation. Fully resolve credential policy before shared-ledger downloads are enabled.

### Ledger Ownership Transfer

Design an explicit transfer of the sole Owner role to another User. Preserve exactly one Owner through an atomic operation and retain financial records and audit attribution. Define approval, eligibility, previous-Owner membership, disabled-account/integration behavior, session effects, and export consequences before implementation. This does not authorize Owner or ledger deletion.

### Currency, Fiscal Year, and Locale Options

Consider ledger-specific currency, fiscal-year-start, and locale configuration only after defining their actual effects. Resolve supported currency precision and financial calculations, calendar versus fiscal reporting, historical-data treatment, and date/number presentation before promotion. Do not add inert settings or silently reinterpret existing USD amounts and calendar-year reports. These remain deferred design candidates, not committed behavior.

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
