# No Part Finance Roadmap

This file contains future product work. SPEC.md describes the implemented product.

Only Version Next is approved implementation scope. A request to develop Version Next authorizes its current milestones as one coordinated implementation batch; a request naming one milestone authorizes only that milestone. Plan at most one schema-version increment for the complete authorized batch. Version Soon, Version Later, and Deferred Ideas are not approved until explicitly promoted.



## Version Next

No items remain.

## Version Soon

No items remain.

## Version Later

These ideas preserve product direction but are not approved implementation scope. Promote and fully design one before implementation.

### Shared Persistent Device Labels for Sessions and Notifications

Consider editable labels in Sessions and one shared device label across the Sessions and Notification devices tables for the same signed-in User and explicitly linked browser installation. Editing from either table would update both, retaining browser/OS identification as secondary text. Match only by the existing installation ID, never by browser name or IP; separate browsers/profiles and legacy unlinked records remain separate. Preserve the current-row labels, first-row ordering, and darker backgrounds.

Persist device labels independently of notification enrollments so they survive server restart and remain available when notifications were never enabled or have been disabled. Keep device-label persistence independent of the configured login-persistence behavior; this idea authorizes no changes to session lifetime or authentication. Plan the persistent storage/schema change and preservation of existing notification labels before promotion.

The installation ID is cookie-based, not a permanent hardware identity. Define behavior when its cookie expires or is cleared, and avoid automatic identity merging or guaranteed recovery of labels. Resolve label ownership, editing permissions, cleanup, backup/export behavior, and validation/persistence/isolation tests before promotion. This remains an unapproved Version Later design idea; no implementation is authorized.

### Ledger Ownership Transfer

Design an explicit transfer of the sole Owner role to another User. Preserve exactly one Owner through an atomic operation and retain financial records and audit attribution. Define approval, eligibility, previous-Owner membership, disabled-account/integration behavior, session effects, and export consequences before implementation. This does not authorize Owner or ledger deletion.

### Currency, Fiscal Year, and Locale Options

Consider ledger-specific currency, fiscal-year-start, and locale configuration only after defining their actual effects. Resolve supported currency precision and financial calculations, calendar versus fiscal reporting, historical-data treatment, and date/number presentation before promotion. Do not add inert settings or silently reinterpret existing USD amounts and calendar-year reports. These remain deferred design candidates, not committed behavior.


### Automatic Backups

Consider one consistent SQLite backup before the first successful write of each server-local day, with integrity verification, bounded retry, feature-owned retention, and a persistent failure banner that never blocks the authoritative write. Manual and migration backups remain separate. Tests use explicit temporary paths.

### New Transaction URL

Prefer a #new fragment on the current page so opening, closing, refresh, Back, and Forward preserve the underlying page state. Apply it only to ordinary transaction creation and reuse the existing form. Decide separately whether /new redirects, remains compatible, or is retired.


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
