# No Part Finance Roadmap

This file contains future product work. SPEC.md describes the implemented product.

Only Version Next is approved implementation scope. Implement one named Version Next milestone at a time. Version Soon, Version Later, and Deferred Ideas are not approved until explicitly promoted.



## Version Next

No implementation milestones remain.

## Version Soon

These milestones expose the 26.10.21 ledger foundation through command-line user management. They remain deferred until explicitly promoted. Each login has one membership in this release, each ledger has exactly one Owner and at most one optional Auditor, and sign-in automatically resolves that ledger. Do not repeat the Version Next migration or add web registration/account management, standalone ledger renaming, household Members, multiple-ledger memberships, or a ledger dropdown. No Owner or ledger deletion is introduced.

### CLI Multi-Owner Provisioning and User Management

Extend existing command-line user management to list Owners alongside their stored ledger names and select the intended ledger before managing its Owner or optional Auditor. Provide Create Owner. Keep password entry protected and preserve existing password validation and confirmation behavior.

Creating an Owner atomically creates their login, a fresh UUID Ledger named `<username>’s Finances`, Owner access, the ledger's External System Account, and default ledger settings. Start without user-owned Accounts, Categories, templates, transactions, Budget plans, Simulator plans, or integrations. Do not copy another ledger's configuration or financial records. Failures leave no partial User, Ledger, access relationship, or initialization records.

After selecting a ledger, support its Owner's username/password management and creation, username/password management, and removal of its optional Auditor using the existing CLI workflow. No operation may accidentally target another ledger. Creating an Auditor creates a separate login linked only to the selected ledger; reusing a User already assigned elsewhere is unsupported in this release. Removing an Auditor invalidates their sessions and removes their access while preserving financial history and audit attribution.

Enforce installation-wide username uniqueness with existing normalization rules for creation and renaming across both roles, including disabled logins. Changing an Owner's username atomically renames their Ledger to `<username>’s Finances`; changing an Auditor's username does not rename the Ledger. This explicitly supersedes 26.10.21's placeholder-name rule that later username changes leave ledger names unchanged. Do not expose standalone ledger renaming. Password resets invalidate all sessions for the affected User and leave other Users' sessions unaffected.

Keep persistent ledger changes audited with trusted CLI actor attribution. Existing migrated Users, credentials, and records remain usable. Sign-in and all financial operations automatically use the login's only authorized ledger; the 26.10.21 isolation and portable-download rules continue to apply across independently provisioned ledgers.

Verify atomic Owner provisioning and rollback, fresh-ledger contents, optional Auditor limits and removal, explicit target selection, duplicate/normalized username conflicts, atomic Owner/ledger renaming, Auditor rename independence, password-reset invalidation, audit preservation, and unchanged existing ledgers. Exercise two CLI-provisioned Owners and their Auditors through browser/API reads and writes, integration authentication, settings, and both portable downloads to confirm isolation. Use guarded temporary databases and synthetic records only.

### CLI Login Enable and Disable

Expose Enable/Disable controls for the explicitly selected ledger's Owner and optional Auditor. Show administrative Enabled/Disabled status separately from the temporary sign-in cooldown. Disabling a User invalidates their sessions and blocks login until explicitly re-enabled, without deleting records or changing their password. Disabling an Auditor affects only that login; it does not disable the Owner or integrations.

Disabling an Owner also immediately blocks integration authentication for that Owner's ledger. Implement this as an effective authorization gate, preserving each key's own Enabled/Disabled/Revoked state. Re-enabling the Owner restores use of keys that are still individually Enabled; individually Disabled or Revoked keys remain blocked. Do not automatically disable the Auditor. Keep these operations ledger-scoped and audited; failures roll back atomically. Existing Users default Enabled when this feature is installed. Preserve administrative status in portable downloads; disabling keys only in a downloaded copy must not change User status.

Verify session invalidation, persistence across restart/export, blocked browser and integration requests, unchanged key states, successful re-enable requiring fresh browser sign-in, Auditor independence, and isolation from other ledgers.

### CLI Last Successful Login Display

Expose each User's last-successful-login timestamp in CLI user management alongside their administrative login status, using the metadata recorded by 26.10.21. Display the server's local time with its timezone identified, and `Not recorded` when no timestamp exists. This is a read-only display; viewing user management must not update login metadata. Verify Owner and Auditor listings, missing timestamps, timezone presentation, and unchanged stored values.

## Version Later

These ideas preserve product direction but are not approved implementation scope. Promote and fully design one before implementation.

### Per-Ledger Audit Log Viewer

Expose the audit events recorded by 26.10.21 in an authorized ledger-scoped viewer. Show who changed what and when, including relevant before/after values and integration submission/approval attribution. Design viewing permissions, filters, and presentation before implementation. Preserve secret exclusion and historical audit integrity; a viewer does not authorize editing or deleting audit events.

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
