# No Part Finance Roadmap

This file contains future product work. SPEC.md describes the implemented product.

Only Version Next is approved implementation scope. Implement one named Version Next milestone at a time. Version Soon, Version Later, and Deferred Ideas are not approved until explicitly promoted.



## Version Next

No items remain.

## Version Soon

No items remain.

## Version Later

These ideas preserve product direction but are not approved implementation scope. Promote and fully design one before implementation.

### Account Management

Bring relevant CLI user-management workflows into the web UI, including username and password management and administrative enable/disable operations. Design authorization, self-service versus administrative actions, credential/session effects, and cross-ledger User implications before implementation. Do not assume a new installation-administrator role. Preserve ledger isolation, authentication controls, and the trusted deployment boundary. Optional Web Registration is implemented in SPEC.md; broader Account Management remains deferred.

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
