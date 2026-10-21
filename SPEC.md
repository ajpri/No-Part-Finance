# SPEC.md: No Part Finance

## Status

This is the product specification for No Part Finance. It describes implemented behavior by feature rather than by release order. Future work and the approved next milestone live in `ROADMAP.md`.

The authoritative application version is the exact string `26.10.21`, defined once by `applicationVersion` in `packageApplication.py`. Settings, the bottom-center sign-in footer, served asset-version references, OpenAPI version metadata, version-bearing package filenames, and command-line version output use that same value. Release History groups the October changes under `26.10`.

## Product Intent

Build a simple, self hosted personal finance web application that replaces two existing Google Sheets systems:

1. Bank account balance tracking
2. Expense tracking and reporting

CLI provisioning supports independent personal ledgers in one database, each with exactly one Owner and any number of read-only Auditors. Each User has exactly one membership, including a disabled membership; browser sign-in resolves only that authorized ledger. Sharing, Members, and multiple-ledger login workflows remain future scope.

The application should feel closer to a clean spreadsheet or older Google web application than a modern fintech dashboard.

Data is more important than visual decoration.

The core philosophy is:

> Everything is a transaction.

New opening balances and reconciliation are the specified exceptions: they update account balance offsets without creating ledger transactions. Imported historical opening-balance and adjustment transactions remain intact.

Accounts answer:

> Where is the money?

Transactions answer:

> What happened?

Categories answer:

> What was it for?

Reports are calculated from transactions.

There should be as little separately maintained financial state as possible.

### Engineering Algorithm

Apply the engineering algorithm to product and implementation decisions in this order:

1. Question every requirement.
2. Remove unnecessary parts or processes.
3. Simplify what remains.
4. Improve speed only after the design is correct.
5. Automate only after the process is stable.

> The best part is no part.

This does not authorize removing explicit requirements, safeguards, financial history, or data-integrity rules. The goal is to eliminate unnecessary concepts, state, workflows, dependencies, and maintenance while preserving correctness.

### Generic Design Rule

All future feature design must support a generic implementation rather than depending on one user's specific account names, category names, descriptions, institutions, or habits. When behavior has financial meaning, represent that meaning explicitly instead of inferring it from a user-facing label.

Accounts and categories are user-owned configuration. A new database starts without user-owned Accounts or Categories; it creates the built-in External System Account as specified below, and production initialization must never seed, recreate, rename, delete, archive, reorder, or otherwise repair categories based on expected names. Users may create and configure them in Settings without silently breaking generic behavior. Do not add new name-based special cases.

Apply this rule incrementally when designing or changing features. It does not authorize speculative abstractions or additional multi-user workflows beyond the ledger-isolation foundation specified here.

### Shared Acceptance Standard

Each feature must be verified at the level of risk it introduces. Tests should cover its primary success path, validation and boundary rules, relevant failure or rollback behavior, persistence and refresh behavior, and the financial or historical invariants it could affect. Backend-authoritative calculations require Python tests; important user workflows require focused browser or integration coverage.

Financial tests use integer cents and include applicable sign, zero, refund, transfer, date-boundary, and archived-record cases. Schema changes must prove migration preservation and integrity. Responsive controls must retain semantic DOM order, keyboard access, readable narrow-screen layout, and safe text rendering. Tests must use explicit temporary database and backup paths and never initialize, migrate, or write the real `finance.db`.

### Shared Scope Standard

A feature adds only the state, routes, and concepts explicitly required here. Unless specified otherwise, it must not create a second ledger, store derived balances or totals, automate or match transactions, infer financial meaning from names, or change unrelated calculations and workflows. Feature-specific exclusions below are retained only when they clarify a boundary that is not covered by this rule or by the Explicit Non-Goals.

## Technology

Use:

```text
Python
Flask
SQLite
Plain HTML
Plain CSS
Plain JavaScript
```

Use Python's built in `sqlite3` library.

Do not use an ORM.

Do not use React, Vue, Angular, TypeScript, Tailwind, Bootstrap, or another frontend framework.

Do not require Node.js or a frontend build process.

Docker must not be required.

The application should be runnable with a simple Python command.

SQLite is the permanent application data store.

The database is a portable file such as:

```text
finance.db
```

No separate database server is required.

### Backend Authority

For future feature decisions, keep financial and planning meaning authoritative in the Python API: validation, money/sign rules, balances, reporting totals, schedule occurrence, Budget calculations, and suggestion eligibility. API responses should provide the semantic values and flags needed for presentation.

The browser collects input, derives browser-local default dates where required, renders and formats returned values, and manages simple interaction state such as dialogs and the active section. Do not independently reimplement financial or planning rules in JavaScript. This is an enduring implementation principle, not authorization for a broad cleanup or architectural refactor.

## Explicit Non-Goals

The implemented application does not include:

1. Buckets
2. Envelopes
3. Sinking fund allocations
4. Planned-versus-actual budgeting
5. Recurring transaction automation
6. Separate loan accounting behavior or amortization
7. Principal and interest splitting
8. Split transactions
9. Net worth tracking
10. Savings goals
11. Receipt uploads or transaction attachments
12. Merchant normalization
13. Transaction tags or notes
14. Bank synchronization
15. Continuing CSV import inside the web app
16. Household Members, invitations, multiple-ledger login workflows, or browser-based user management
17. Charts unless explicitly added later

Savings goals may be designed as a separate feature in the future.

Transaction templates are not recurring transactions.

## Ledger Identity, Isolation, and Audit History

One `finance.db` contains UUID-identified Ledgers, Users, and `ledgerAccess` relationships. Existing integer User and financial record IDs remain unchanged. Each ledger has exactly one Owner for usable access; a partial unique database index prevents a second Owner. CLI setup permits independent ledgers, each with one Owner and multiple Auditors. Missing initial Owner setup remains supported without financial access. No web registration, sharing, invitations, Member role, ledger dropdown, or additional memberships for an existing User are implemented.

Each Ledger has a required nonblank stored name initialized once as `<username>’s Finances`. Before Owner setup, its provisional name is `Finances`; successful first-time Owner creation updates that name atomically. Later username changes do not rename it in this release. Names need not be unique and never grant access. Both portable web downloads preserve the name and UUID. Independent naming UI is deferred.

Users retain credentials, unique normalized installation-wide usernames, and authentication versions. Roles belong to `ledgerAccess`, whose unique `(userId, ledgerId)` relationship supports future multi-ledger memberships without changing financial tables. Supported roles remain Owner and Auditor; future household sharing will use one Owner and a separately designed Member role. Multiple memberships in ordinary browser use fail closed rather than choosing a ledger implicitly.

All Accounts (including one External per ledger), Categories, transactions, templates, Budget records, Simulator plans/rows, integration credentials, pending submissions, and ledger-specific settings have a required `ledgerId`. Installation paths and deployment settings remain installation-wide. Existing USD, calendar-year, date, and number rules remain unchanged; currency, fiscal-year, and locale options are deferred. Names, External identity, and setting keys are unique within a ledger where applicable; usernames and integration secret values remain globally unique.

Composite foreign keys enforce the same ledger across transaction Account/Category, Category parent, template references, Budget references, Simulator plan/Account/Category/executed-transaction references, and pending integration/resulting-transaction references. Preserve existing archival, deletion, cascade, and unlinking behavior without clearing a surviving record's ledger identity. Ledger assignment is immutable. Transfer groups cannot span ledgers and retain atomic two-row zero-sum behavior.

Python resolves browser authorization from the server-side session and current database membership on every request. Missing, invalid, or ambiguous access is denied; submitted User/Owner/Ledger IDs never grant access. Role changes and membership removal take effect on the next request. Every financial function requires explicit authorized connection context, kept local to the request. Scoped SQLite views restrict reads; write predicates, scoped defaults, and database write guards restrict changes. An omitted context fails rather than querying every ledger. Inaccessible record IDs behave like nonexistent IDs, without disclosing their existence or content.

This boundary applies to Dashboard, transaction history/search/filters, all financial/configuration/planning writes, transfers, reconciliation, reports, helper operations, settings, integration reads/proposals/review, and downloads. Totals include only authorized records. Creates assign ledger identity server-side; all supplied references are validated in that ledger. Mixed local/foreign references fail atomically. Concurrent requests cannot share an active-ledger value. Database Status counts authorized ledger records and displays its latest audited change in server-local time, or `Not recorded`, rather than exposing another ledger's file-write timing.

Each integration credential is permanently assigned to one ledger. Authentication derives its context from the stored credential, not a request selector. Its reads, proposals, durable retry identities, and Pending review are restricted to that ledger; accessing another requires a separate key. Preserve existing financial signs, validation, human approval, and browser/integration credential separation.

### Audit Recording

Persistent ledger changes produce append-only audit events with ledger, UTC change time, trusted actor identity and retained label, action, affected record identity, and relevant before/after values. Record transactions, transfers, reconciliation, Accounts/Categories/templates, Budget, Simulator, ledger settings, integration lifecycle changes, proposal submission/review, and supported CLI user management. The integration submission and approving Owner remain separately attributable through proposal identity and events. Labels and events survive affected-record deletion or renaming.

Audit inserts commit in the same SQLite transaction as the change; audit failure rolls back the change and write failure leaves no successful event. User-management events record safe identity/role/version fields rather than credential contents. Never log passwords, password hashes, session/CSRF tokens, or integration secret values. Authentication's last-login metadata, ordinary views, searches, and unsaved previews do not create ledger audit events. Audit history affects no financial calculations. Logging begins at installation of this feature; migration does not fabricate historical events. An audit viewer and retention/deletion UI are deferred.

### Ledger Schema Upgrade

Explicit schema version 22 migrates existing records into one UUID Ledger, links existing Users through their saved roles, and preserves IDs, financial cents/dates/signs, offsets, transfer/event identities, archives, reporting roles, planning state and execution links, password hashes/authentication versions, integration values/revocation, and Pending content/revisions/decisions. Rebuild affected tables and restore indexes/triggers transactionally, checking foreign keys before commit. A verified SQLite pre-migration backup is required; failures restore the verified original and stop startup as specified in Safe automatic upgrades. Restart does not duplicate Ledgers or memberships. Fresh initialization follows the same final schema path, with no default credentials or personal financial configuration. Upgrade/restart discards browser sessions.

## Accounts

### Account Model

There are two user-owned financial account classes:

```text
cash
credit
```

There is also one NPF-defined `system` Account: `External`. It is a read-only transfer boundary for money moving between tracked Accounts and money outside NPF. Its stable system identity, rather than its display name, defines its behavior.

Cash accounts may have an optional descriptive subtype:

```text
checking
savings
other
```

Subtype is descriptive only.

Checking versus savings must not cause different accounting behavior.

This intentionally supports accounts that do not fit neatly into traditional banking labels, such as Apple Cash or X Money.

Credit accounts may have an optional descriptive subtype. Initially supported user-facing subtypes are `Credit Card`, `Personal Loan`, and `Other`. Subtype is descriptive only and does not change ledger math. Credit cards and personal loans intentionally share the same underlying `credit` account behavior.

### Account fields

Conceptually:

```text
Account

id
name
accountClass
accountSubtype
active
displayOrder
balanceOffsetCents
creditLimitCents
pinnedForSelection
systemIdentity
```

Every account has a stable internal ID.

Renaming an account must never affect its historical transactions.

An archived account remains attached to all historical transactions but is removed from normal transaction and transfer selectors.

### Current balances

Current balance is always:

```text
balanceOffsetCents + SUM(transaction amounts for account)
```

`balanceOffsetCents` defaults to `0` and stores financial history outside the tracked transaction ledger.

It has no date or category and does not appear in transaction history or affect income and expense reporting.

### External System Account

NPF creates exactly one active System Account named `External` per ledger. It is not user-owned and cannot be created, renamed, edited, archived, deleted, reconciled, pinned, sorted, or given an opening balance, correction, limit, minimum, or subtype. `External` is reserved against user-created Account names without relying on the display name for its behavior. A database upgrade stops with a clear instruction if a user-owned Account named External already exists, rather than converting or renaming it.

Settings displays `External` in a read-only `System Accounts` section with the explanation `Transfer boundary for money outside NPF`. It does not count as an Account for financial-entry empty states.

External is available only in transfer-capable From and To selectors, in a native `System` group after Cash and Credit Cards and Loans. It is excluded from ordinary transaction, template, reimbursement, Budget, bulk-entry, opening-balance, reconciliation, and non-transfer Simulator selectors. Historical Account filters include it as `System > External`.

Transfers to or from External use the ordinary atomic two-row transfer model and do not affect Income, Expenses, Categories, or reports. External retains its derived ledger balance solely to preserve transfer zero-sum integrity, but its balance is never displayed or included in Dashboard, Cash or Credit totals, alerts, Hide zeros, or Simulator Results. In Simulator, External is allowed only on Transfer rows; its projected transfer changes only the displayed tracked Account side.

### Cash Account Behavior

Positive internal amount:

```text
money entered the account
```

Negative internal amount:

```text
money left the account
```

Example:

```text
Paycheck                 +1519.66
McDonalds                  -11.73
```

The dashboard displays the calculated balance normally.

### Credit Account Behavior

Credit cards use the exact same transaction ledger.

A purchase is internally negative.

A payment or refund is internally positive.

Example:

```text
McDonalds                  -11.73
Credit card payment       +500.00
```

Internally, a card that owes $153 may therefore have:

```text
balance = -153.00
```

The dashboard must not display:

```text
Discover    -$153.00
```

It should display:

```text
Discover    $153.00
```

If a credit account has a positive internal balance because the card has been overpaid, display:

```text
Discover    $20.00 credit
```

This display rule does not change the underlying ledger math.

### Credit limits and utilization

Only a credit account with subtype `Credit Card` may have an optional credit limit. Store it as nullable positive integer cents. It may be cleared, and changing the account class or subtype away from Credit Card clears it. Credit limits are informational and never restrict transactions or alter balances.

For each active Credit Card with a limit, amount owed is `max(0, -internal balance)` and utilization is amount owed divided by credit limit. Display two decimal places with readable text and accessible labeling. Use compact solid badges: green below 30%, yellow from 30% through below 50%, and red at 50% or higher. Use dark text on green and yellow and white text on red with sufficient contrast. Keep the percentage and accessible label so color is not the only signal, and keep badges visually secondary to balances. Overpaid cards show 0.00%; utilization may exceed 100%.

Python classifies individual Dashboard cards, total revolving utilization, and Simulator projected-card badges using the exact integer ratio before rounding the displayed percentage to two decimals: owedCents * 100 < limitCents * 30 is Green, below limitCents * 50 is Yellow, and otherwise Red. Thus 29.996% displays 30.00% with Green and 49.996% displays 50.00% with Yellow. The Dashboard returns account utilization and totalUtilization status objects containing state, display label, basis points, and accessible label; ineligible or missing-limit statuses are null. JavaScript renders backend states without recalculating ratios or badge thresholds. Account-specific Utilization Warning comparisons and helper rounding remain unchanged.

Total revolving utilization divides eligible active cards' summed amount owed by their summed limits. Exclude cards without limits, Personal Loans, and Other credit accounts. This does not change the combined credit-account debt Total.

### Opening Balances

Creating a new account may optionally include:

```text
Opening Balance
```

For a cash account, a $500 opening balance initializes:

```text
balanceOffsetCents = +50000
```

For a credit account, the UI treats the opening balance as the amount currently owed.

An entered credit opening balance of:

```text
153.00
```

initializes:

```text
balanceOffsetCents = -15300
```

Opening balances for newly created accounts do not create transactions and do not require a date.

They represent financial history outside the tracked transaction ledger and do not affect income or expense reporting.

Existing imported historical `Opening Balance` transactions are preserved and are not rewritten into offsets.

### Account Management

Location:

```text
Settings
Accounts
```

Required functionality:

```text
Add account
Rename account
Set account class
Set optional subtype
Archive account
Create opening balance
Adjust balance
```

Do not directly delete an account that has transactions.

Archived accounts remain available in historical reports and filters.

New transactions and transfers should normally use active accounts only.

#### Reconcile Balance Within Account Editing

Remove the standalone `Reconcile Balance` action from the Settings Accounts table. Account rows retain `Edit` as the entry point for both account configuration and reconciliation.

Add a clearly separated `Reconcile Balance` group inside the existing Edit Account dialog. Do not open a second modal or add a separate page. The group shows the account's current calculated balance, an `Actual current balance` input, and its own `Reconcile Balance` button.

Keep reconciliation independent from the dialog's normal account-settings save action. Saving changes to the account name, class, subtype, credit limit, minimum, or other account configuration must not reconcile the balance. Pressing `Reconcile Balance` performs only reconciliation and must not implicitly save unrelated unsaved account-setting changes.

Preserve the existing authoritative reconciliation behavior. Python calculates the current balance and offset adjustment. Cash accounts use the normal Cash balance convention. For Credit accounts, the user enters the friendly positive amount currently owed, which the backend converts to the appropriate internal balance before updating `balanceOffsetCents`. Reconciliation does not create a transaction, add a date or category, or affect income and expense reporting.

After successful reconciliation, keep the Edit Account dialog open, clear the Actual current balance input, refresh the displayed current calculated balance and affected account data, and provide the existing accessible success feedback. Validation or write failures leave the entered value available for correction and do not partially update the account.

Do not change reconciliation math, account balance calculation, transaction history, or reporting behavior.

#### Alphabetical Account Groups

Discontinue user-managed Account Sort Order in the interface without changing the database schema. Remove Sort Order from Account creation and editing and remove its column from Settings. User-facing Account collections that previously used Account `displayOrder` become grouped by Account class where applicable and ordered case-insensitively by Account name within each group, using stable Account ID as the deterministic tie breaker. Preserve any view's stronger existing semantic sort when it is not an Account-order list.

Dashboard continues to show separate `Cash Accounts` and `Credit Cards and Loans` sections, but each section is alphabetical. Settings Accounts uses one shared `New Account` action followed by two alphabetical tables:

```text
Cash Accounts
Name | Subtype | Minimum | Pinned | Actions

Credit Cards and Loans
Name | Subtype | Credit Limit | Utilization Warning | Pinned | Actions
```

Preserve the existing active and archived presentation and actions within the appropriate class section. Keep one shared Account editor with class-specific fields. Changing an Account class moves it naturally to the other alphabetical group.

Every maintained Account selector that genuinely includes both classes uses native optgroups in this order when eligible values exist:

```text
Cash
Credit Cards and Loans
```

Apply each control's existing active, archived, editing, and Account-type eligibility before grouping. Alphabetize eligible Accounts case-insensitively within each group, use Account ID as the deterministic tie breaker, omit empty groups, and keep control-specific placeholders outside optgroups. A selector deliberately restricted to one class remains one plain alphabetical list without a redundant optgroup.

This applies to every mixed-class Account selector. Preserve each control's eligibility, selection, editing behavior, and archived labeling.

Account lists in Budget summaries, Simulator Results, and other informational views use applicable Cash-first, Credit-second alphabetical ordering unless that view has a stronger specified semantic ordering. These informational lists ignore selector pins. Do not add automatic defaults, remembered Accounts, drag-and-drop, automatic renumbering, separate Cash and Credit order fields, or another manual ordering mechanism.

Do not remove, rewrite, reset, or migrate the existing Account `displayOrder` database column or any stored value. Stop exposing it as editable product configuration and stop using it for user-facing Account ordering, but preserve it unchanged for safe future removal. This behavior requires no schema migration.

Python returns eligible Accounts with class, Boolean pin state, and authoritative alphabetical ordering metadata; JavaScript must not infer eligibility or restore display-order behavior.

#### Account Selector Pins

`pinnedForSelection` defaults to false. Account editing provides `Pin in account selectors`. Only active user-owned Cash or Credit Accounts can be pinned. Archiving atomically clears the pin; archived and System Accounts cannot be pinned. Renaming or changing the class of an active Account retains its pin. Settings shows the Boolean state in each Cash/Credit table while retaining the class sections; the read-only System table has no Pinned column.

Every operational Account selector used to create or modify ledger or planning input applies its existing eligibility first, then groups eligible pinned Accounts under `Pinned`, followed by unpinned `Cash`, `Credit Cards and Loans`, and, where already eligible, `System`. A pinned Account appears once. Alphabetize each group case-insensitively with stable Account ID as tie breaker, omit empty groups, and keep placeholders outside optgroups. A restricted selector includes only its eligible Accounts; a single-class selector without pins remains a plain alphabetical list. Preserve archived-edit behavior without making an archived Account pinnable.

Historical and reporting filters ignore pins and retain their normal class groups and active/archive eligibility. Dashboard, Budget summaries, Simulator Results, and other informational Account lists retain their alphabetical ordering. Pinning changes neither financial calculations nor the selected value, eligibility, defaults, remembered selections, or legacy `displayOrder`.

## Transactions

### Transaction Model

Conceptually:

```text
Transaction

id
accountId
date
description
amountCents
categoryId
transferId
pinned
```

`categoryId` may be null for transfers and imported nonreportable activity, including historical opening balance and balance adjustment transactions preserved from earlier versions.

`transferId` is null for ordinary transactions.

`pinned` defaults to false. It only controls whether an ordinary transaction appears in Pinned Transactions and never changes accounting or reporting behavior. Pinned ordinary transactions are excluded from Dashboard Recent Transactions so they do not appear twice; unpinning restores normal recent eligibility.

Money must be stored as integer cents.

Do not store money using floating point values.

Dates are calendar dates only.

Store them consistently as:

```text
YYYY-MM-DD
```

Time of day is unnecessary.

### Normal Transaction Entry

The normal transaction form should be extremely fast.

Field order:

```text
Template
Date
Account
Description
Category
Amount
```

Template is optional.

Date defaults to the user's local calendar date. It must be derived from browser-local date parts rather than by converting the current time to UTC first.

Account has no remembered default.

Category has no remembered default.

Description is required.

Category is required for a normal transaction.

Amount is required.

There is no Notes field.

The form should open from:

```text
+ Transaction
```

Use a simple modal dialog.

No wizard.

No intermediate confirmation page.

After a successful save, close the dialog and refresh the affected dashboard data.

The canonical `/new` route displays the normal Dashboard with this same modal already open. Saving returns to `/`; Cancel, Close, Escape, and safe backdrop dismissal also return to `/`. Refreshing `/new` opens a fresh form. Do not add `/transactions/new` or URL prefill parameters.

### User Facing Amount Convention

The transaction form follows the mental model of the existing expense tracker.

Ordinary expenses are entered as positive numbers.

Income and reimbursements are also normally entered as positive numbers.

Refunds against an expense category are entered as negative numbers.

Examples:

```text
McDonalds
Category: Wants
Reporting Role: Expense
Entered amount: 11.73
Stored ledger amount: -11.73
```

```text
Paycheck
Category: Income/Reimbursement
Reporting Role: Income
Entered amount: 1519.66
Stored ledger amount: +1519.66
```

```text
Walmart Refund
Category: Groceries/Gas/Household
Reporting Role: Expense
Entered amount: -17.27
Stored ledger amount: +17.27
```

The rule is:

```text
Income Reporting Role:
storedAmount = enteredAmount

Expense Reporting Role:
storedAmount = enteredAmount multiplied by negative one
```

This same rule works for both cash and credit accounts.

### Transfers

Transfers are a dedicated operation.

The user selects:

```text
Date
From Account
To Account
Amount
Description
```

Date defaults to today.

Amount is entered as a positive number.

Description defaults to:

```text
Transfer
```

When the destination is a credit account, the same transfer operation is presented as a payment. The primary button reads `Make Payment` and helper text reads `This will be recorded as a payment to <Account Name>.` A `Credit Card` destination defaults to `<Account Name> Card Payment`, except that an account name already ending in `Card` defaults to `<Account Name> Payment` to avoid a duplicated `Card Card` phrase. A `Personal Loan` or `Other` Credit destination defaults to `<Account Name> Payment`. Cash and External destinations use `Transfer to <Destination Account Name>`. These defaults apply on destination changes in new entry; opening an existing transfer preserves its saved description.

The transfer/payment dialog's primary button reads `Make Payment` for every Credit destination, including Other Credit, and `Save` otherwise, for new and edited events. This presentation does not expand payoff-helper eligibility.

This presentation does not create a separate payment transaction type. The user may edit the default description before saving.

Example:

```text
From: Chase Checking
To: Apple Savings
Amount: 500.00
```

creates:

```text
Chase Checking    -500.00
Apple Savings     +500.00
```

Both rows receive the same nonnull `transferId`.

For every transfer:

```text
SUM(linked transaction amounts) = 0
```

A transfer never changes total money.

A transfer has no expense category.

Transfers do not count as expenses or income.

External may be either side of a transfer but cannot transfer to itself. A transfer from a tracked Account to External moves money outside tracked balances without becoming an Expense; a transfer from External to a tracked Cash Account is not Income. A transfer to a tracked Credit or Loan Account retains the normal destination-based Payment presentation.

Creating, editing, or deleting a transfer must update both rows atomically.

Editing either side of a transfer opens the transfer operation rather than allowing one side to be changed independently.

### Credit card payments

A credit card payment is a transfer.

Example:

```text
From: Chase Checking
To: Discover
Amount: 500.00
```

creates:

```text
Chase Checking    -500.00
Discover          +500.00
```

The positive Discover transaction reduces the card balance owed.

### Credit Card Payoff Helper

For a new transfer/payment, show the payoff action only when both active accounts are selected, the destination is a Credit Card or Personal Loan with a positive amount owed, and the Cash source's current authoritative balance covers the full payoff. Derive the amount from `balanceOffsetCents + SUM(transaction amounts)` and use the keyboard-accessible `Use $X.XX (Payoff)` action specified in Transfer Amount Helpers. Activating it replaces the editable Amount field with `max(0, -internal balance)` in integer cents.

Do not automatically change Amount when an account is selected. Refresh the helper when the destination changes, and continue allowing payments smaller or larger than the payoff amount. The informational current-balance line remains available for a zero balance or credit, but there is no payoff action because transfers require a positive amount. Do not show the helper for cash destinations, Other credit accounts, or while editing an existing transfer or payment. Do not change credit balance calculations or create a separate payment type.

### Transfer Account Balances

In the existing Transfer / Payment form, show each selected account's current calculated balance immediately below its respective Account select: From Account below From, and To Account below To. Use the exact compact label format `Current Balance: $X.XX`. Show nothing below either control until that account is selected. Both displayed values are informational only and refresh whenever their selected account changes.

Calculate the balance in Python from the authoritative account state:

```text
balanceOffsetCents + SUM(transaction amounts for account)
```

Display Cash balances normally. Display Credit balances using the established friendly Credit convention: use an unsigned amount without `owed` or an internal negative sign, and use `credit` only for a positive internal Credit overpayment. The To Account current-balance line replaces the payoff helper's separate destination-balance display. For any Credit destination, place the existing payment note directly below that current-balance line, using wording such as `This will be recorded as a payment to Discover.` Do not call it a debt payment. The existing new-transfer payoff helper remains available for a destination with a positive amount owed only when the selected From account is Cash and its current balance fully covers that payoff. It contributes only the `Use $X.XX (Payoff)` action beneath the payment note. Derive the balance line and helper from the same authoritative account balances. A zero or overpaid destination, a non-Cash source, or a Cash source that cannot cover the full payoff shows no payoff action. Cash destinations show only their current-balance line. Other Credit destinations also show the payment note but never gain a payoff helper.

Do not calculate a post-transfer balance, compare either balance with a Cash-account minimum, block submission, create a warning, reserve money, alter the transfer form's validation, or change atomic transfer behavior. The source-coverage comparison controls only payoff-shortcut visibility and never validates or blocks an entered transfer amount. This applies to both new and edited transfers and payments, while the existing selected account remains active and valid. The payoff helper retains its existing new-transfer-only behavior.

The backend provides display-ready current balances and payoff-helper eligibility; JavaScript must not independently calculate them.

### Cash-to-Cash Transfer Descriptions

When selecting a Cash account as the To Account for a new Transfer / Payment form or a Simulator Transfer / Payment row, set the Description or Simulator Label to `Transfer to <Destination Account Name>`, for example `Transfer to Chase Checking`. This is the Cash-destination default; it does not change the existing Credit Card or Personal Loan payment-description defaults, other transfer presentation, ledger signs, or atomicity.

Do not track whether the user has overridden a prior generated value. Each selection of a Cash destination sets the Description or Label to that destination's `Transfer to <Destination Account Name>` value. Editing an existing transfer or payment preserves its saved description and never replaces it automatically.

### Transfer Amount Helpers

Label the new-payment payoff-helper action `Use $X.XX (Payoff)`. `Payoff` identifies that the amount clears the eligible Credit destination's amount owed; do not label it `Balance`. Preserve two decimal places.

Retain every existing payoff-helper eligibility rule. Show it only for a new transfer/payment when the destination is a Credit Card or Personal Loan with a positive amount owed, the selected source is Cash, and the source's current authoritative balance fully covers the payoff. A zero or overpaid destination, a non-Cash source, an uncovered payoff, and transfer editing show no payoff action. Clicking the helper only prefills or replaces Amount and never submits, validates, reserves money, or changes either account.

For a new Cash-to-Cash transfer, after both active Accounts are selected, show `Use $X.XX (Sinking Fund per paycheck)` when Budget's global authoritative Sinking Funds per-paycheck total is greater than zero. Preserve two decimal places. Clicking it only prefills or replaces Amount; it does not submit the transfer, select Accounts, compare against the source balance, warn, or block an otherwise permitted transfer.

The Sinking Fund helper uses only the existing global per-paycheck total across active Sinking Fund plans. It does not associate the transfer with a Budget plan or individual Sinking Fund, mark a contribution complete, track saved or reserved funds, change Budget data, or create an account-level target. Do not show it when the total is zero or unavailable, either selected Account is not Cash, or an existing transfer is being edited.

Cash-destination and Credit-destination amount helpers are mutually exclusive. Python returns display-ready helper wording, the authoritative input amount, and eligibility; JavaScript only renders the returned helper and copies its amount into the existing Amount field. Do not add another field, confirmation, automatic transfer, or Budget-to-ledger relationship.

### Balance Reconciliation

The application provides a `Reconcile Balance` operation showing the calculated balance and asking for the actual current balance. It does not ask for a date.

The backend calculates:

```text
difference = actual current balance - currently calculated balance
balanceOffsetCents = balanceOffsetCents + difference
```

Reconciliation does not create a transaction. For Cash accounts, the entered value follows normal Cash display. For Credit accounts, the user enters the friendly positive amount owed, which the backend converts to the appropriate negative internal balance before calculating the offset.

The offset represents history outside the tracked ledger. It has no date or category, does not appear in transaction history, and does not affect income or expense reporting. Preserve imported historical `Balance Adjustment` transactions rather than rewriting them as offsets.

### Transactions Page

There is one full transaction history.

Its top entry actions are `+ Transaction`, `+ Transfer`, and `Bulk Entry`; the two plus-prefixed financial actions use the shared solid green background with black text. Use an explicit app-defined appearance for all buttons so mobile browsers do not substitute rounded native button styling.

Do not create separate pages for this month, last month, or category queries.

Required filters:

```text
Period
Type
Account
Category
Description search
```

Type provides All, Transactions (`transferId` is null), and Transfers (`transferId` is nonnull). Represent each valid two-row transfer as one visual event while preserving its two authoritative ledger rows. Use the same validity rules as Dashboard Recent Transactions; invalid linked data remains visible as its underlying rows so inconsistent financial data is never hidden.

For a combined transfer event, display `From Account → To Account`, its shared description, `Transfer`, and the unsigned amount. A payment to a credit account remains a transfer rather than becoming a separate transaction type. Opening the event uses the existing transfer editor, and editing or deleting it updates both ledger rows atomically.

Group valid transfers before applying filters. All includes ordinary transactions and combined transfer events. Transactions includes ordinary transactions only. Transfers includes combined transfer events and visible invalid underlying transfer rows. An Account filter matches either side and shows the event once. A Category filter excludes transfers because they have no category. Description search matches the combined description. Combine Type with every other filter.

Active filters are represented in the URL query string using readable `period`, `type`, `account`, `category`, and `search` parameters. Custom ranges also preserve their start and end dates.

New Account and Category filters use stable IDs in the existing parameters, such as `account=id:42` and `category=id:84`. Names remain display labels; renaming a record does not change a stable-ID filter. Historical filters may reference archived records.

Legacy name slugs are accepted only when they uniquely identify one current record. An ambiguous, malformed, or unavailable reference remains visible as an invalid filter with concise correction guidance; display no transaction results or summary totals until it is corrected or explicitly cleared. Never select the first match, remap a reference, or silently broaden the query. Applying another control retains an unresolved reference. New URLs generated from valid selections always use the `id:` format. Existing financial API filters continue to use their integer `accountId` and `categoryId` contracts.

The URL is the filter source of truth. Loading or refreshing a filtered URL restores the same controls and results. Browser Back and Forward restore previous filter states naturally. Editing or deleting a transaction returns to the same filtered URL.

Useful period presets include:

```text
This Month
Last Month
Year to Date
```

Desktop columns:

```text
Date
Account
Description
Category
Amount
```

Categorized ordinary transactions display the user-facing entered amount derived from their Category Reporting Role, as specified in User-Facing Transaction Amount Display. Historical uncategorized activity and invalid linked-transfer rows retain their actual ledger signs.

Examples:

```text
Paycheck (Income)          +1519.66
Fictional purchase (Expense) +11.73
Expense refund              -11.73
```

Provide edit and delete functionality.

Date, Account, Description, Category, and Amount headers are sortable using readable `sort` and `direction` URL parameters. Date defaults descending. First Date and Amount sort is descending; first text sort is ascending. Show an arrow only on the active header. Text sorts case-insensitively, null categories remain last, and transaction ID is the deterministic secondary key. Preserve filters and sorting through reload, browser navigation, editing, and deletion.

For combined transfer events, Date sorts by transfer date, Account by From Account then To Account case-insensitively, Description by the shared description, Category as `Transfer`, and Amount by the unsigned transfer amount. Use stable transfer identifiers as deterministic tie breakers.

Deleting an ordinary transaction requires confirmation.

Deleting a transfer deletes both linked rows after confirmation.

No pagination is required for version 1 unless actual performance proves it necessary.

#### Transaction History Controls

**Default period.**

When the Transactions page is opened without an explicit Period filter, default it to `Last 30 Days` instead of All Time. Keep All Time available as an explicit Period choice, and preserve an explicitly supplied period in a shared or bookmarked Transactions URL. Do not add a user preference or additional stored state for this default.

**Custom date range.**

Match the Dashboard period-control behavior: show Start and End date fields only when Transactions Period is `Custom Range`; hide them, rather than merely disabling them, for every other period.

When changing a bounded period to `Custom Range`, prefill Start and End with that period's exact current date range. For example, February 2026 becomes `2026-02-01` through `2026-02-28`. When changing from All Time, leave both date fields blank because it has no finite range. Preserve normal validation for a Custom Range.

**Combined transfers.**

Display each valid two-row transfer as one event on the full Transactions page, consistent with Dashboard Recent Transactions, while retaining both authoritative ledger rows. Show `From Account → To Account`, the shared description, `Transfer`, and the unsigned amount. Use one Edit action opening the atomic transfer editor, whose confirmed Delete action removes both rows, as specified in Delete Within Transaction Editing. Preserve the same information in a compact mobile record.

Group before filtering so Period, Type, Account, Category, and Description filters behave as defined in the Transactions Page specification. Sort combined events by their displayed semantics, preserve readable URL filter and sort state through navigation and edits, and keep invalid linked data visible as underlying rows. Do not change the transfer schema, atomicity, ledger signs, reporting exclusion, or account balances.

#### Duplicate Transactions

The Transactions page provides a `Duplicate` action for eligible ordinary transaction rows and valid combined transfer events. In both cases, use the row-action order `Duplicate | Edit`; the existing-event editor contains the red confirmed Delete action. Duplicate opens the existing authoritative entry dialog over the current Transactions page; it never immediately saves, copies a ledger row, creates a relationship to the source event, or changes the source event.

For an ordinary transaction, open the normal New Transaction dialog with its Account, Description, Category, and user-facing entered Amount prefilled from the selected transaction. Set Date from browser-local calendar date parts to today, regardless of the source transaction date. Do not select a Template, preserve a pinned state, recreate an optional reimbursement, or infer any other behavior from the source transaction. The user may edit every prefilled value. Existing normal-transaction validation, category-role sign conversion, reimbursement behavior when deliberately selected, and save behavior remain authoritative.

For a valid combined transfer event, open the existing transfer/payment dialog with From Account, To Account, Description, and positive Amount prefilled from the source transfer. Set Date from browser-local calendar date parts to today. Preserve the existing transfer-versus-payment presentation based on the selected destination account. Saving creates a new, independent, atomic two-row transfer with a new `transferId`; it never reuses, alters, or links to the source transfer. The user may edit every prefilled value before saving.

Show Duplicate only when the source event can be represented by the existing creation form: an ordinary transaction requires an active Account and active selectable Category, while a transfer requires a valid two-row transfer with both Accounts active. Do not offer it for invalid linked transfer rows, historical uncategorized activity, or records that reference archived configuration unavailable to normal creation. Existing Edit behavior and confirmed deletion within an existing-event editor remain available as applicable for those records.

Cancel, Close, Escape, and safe backdrop dismissal discard the duplicate draft and leave the source event unchanged. After a successful save, close the dialog, refresh the current Transactions results, and retain the existing readable filter and sort URL. No duplicate-specific route, URL prefill parameters, confirmation screen, new transaction type, duplicate history, bulk operation, or Dashboard action is added.


#### Delete Within Transaction Editing

Remove the direct `Delete` action from Transactions-page rows. Eligible ordinary transaction rows use `Duplicate | Edit`, and valid combined transfer events use `Duplicate | Edit`. Historical or invalid records that are ineligible for Duplicate retain `Edit` as applicable. Moving Delete does not make an otherwise uneditable record editable.

Place the destructive action inside the existing authoritative ledger editor. When editing an existing persisted ordinary transaction, show a red `Delete Transaction` action separated at the lower left of the dialog from the normal Cancel and Save actions. When editing a valid persisted transfer or payment, show `Delete Transfer` in the corresponding atomic transfer/payment editor. Retain the existing confirmation requirement, backend validation, and atomic two-row transfer deletion.

Show Delete based on editing an existing authoritative ledger event, not based on the page that opened the shared form. Do not show it for New Transaction, New Transfer / Payment, Duplicate drafts, Simulator recording forms, or any other prefilled but unsaved event. Do not create separate editors for Dashboard, Transactions, or Simulator.

An existing ledger event opened from Dashboard Recent Transactions or Pinned Transactions may be deleted through its editor. After successful deletion, close the editor and refresh the Dashboard. When opened from Transactions, close the editor, refresh the results, and preserve the current readable filter and sort URL. A cancelled or failed deletion leaves the ledger event unchanged; a failure keeps the editor open and provides accessible error feedback.

Preserve the existing Simulator one-way execution link and treat linked-ledger deletion as one financial-integrity operation. `Edit linked transaction` continues to open the authoritative ordinary-transaction or atomic transfer/payment editor, where the appropriate Delete action is available because the event already exists. Its confirmation must clearly state that deleting the ledger event will return its linked Simulator row to Planned. On confirmation, delete the authoritative ordinary transaction or full two-row transfer and atomically clear that Simulator row's `executedTransactionId` and set `executed` false, using all existing Payoff-conversion behavior. Never leave an executed Simulator row pointing at a deleted ledger event, and never delete the Simulator row itself as a side effect.

After a successful linked-ledger deletion, close the ledger editor, refresh Simulator from authoritative state, and show the restored Planned row using its retained planner configuration. The Simulator row modal's own Delete action remains distinct: it deletes only the Simulator row and never deletes its linked ledger event. A failed linked deletion changes neither ledger nor Simulator execution state.

Deletion stays within the existing edit workflow; there is no trash, deletion history, bulk deletion, or Dashboard delete action.

#### Transactions Filter Order

Reorder the existing Transactions-page filter controls to:

```text
Period
Type
Account
Description
Category
```

This aligns corresponding filters with the existing table information order `Date | Account | Description | Category | Amount | Actions` while retaining Type as the global event-kind filter. Description remains before Category because it is the more specific recognition and search field. Give Description search the available flexible width on desktop where practical.

Change only presentation order. Preserve every existing filter, URL parameter, matching rule, combined-transfer behavior, reload and Back/Forward restoration, and post-edit or post-delete return behavior. Keep the table columns unchanged, keep Description before Category in narrow-screen transaction records, and stack filter controls in the same DOM order on narrow screens. Do not use CSS-only visual reordering that diverges from keyboard or screen-reader order.

#### User-Facing Transaction Amount Display

Display categorized ordinary transactions using the application's existing user-facing entered-amount convention rather than their internal ledger sign. Derive the semantic display amount authoritatively in Python from the Category Reporting Role:

```text
Income role:
displayAmount = storedAmount

Expense role:
displayAmount = storedAmount multiplied by negative one
```

Examples:

```text
Normal Expense stored as -$11.73 displays as $11.73
Normal Income stored as +$1,519.66 displays as $1,519.66
Expense refund stored as +$17.27 displays as -$17.27
Income reversal stored as -$50.00 displays as -$50.00
```

Do not add a leading plus sign to normal positive Expense or Income presentation. A negative displayed value therefore communicates a refund, reversal, or other negative user-facing entry rather than ordinary spending. This is not a rule that only an Expense refund may mathematically be negative; preserve a legitimate negative Income-role correction.

Apply this convention consistently to Dashboard Recent Transactions, Dashboard Pinned Transactions, the Transactions page, and any maintained ordinary-transaction preview that claims to show the entered Amount. Valid combined transfers and payments retain their existing unsigned event presentation. Historical uncategorized activity preserves its actual ledger sign because it has no Reporting Role to interpret. Visible invalid linked-transfer rows also preserve their underlying ledger signs so inconsistent financial data is not disguised.

Make Transactions-page Amount sorting use the same displayed user-facing amount for categorized ordinary transactions. Retain existing combined-transfer sorting by unsigned transfer amount and raw-ledger sorting for uncategorized or invalid underlying rows. Keep deterministic tie breakers and URL sorting behavior unchanged. Transactions Amount filtering remains separately magnitude-based and is not altered by this display convention.

The API returns the semantic display amount or flags needed for presentation and sorting; JavaScript must not infer signs from Category names or independently reproduce Reporting Role math. Do not change stored amounts, account balances, reporting, refunds, editing conversion, transfer atomicity, transaction creation, or database schema.

#### Transactions Amount Filtering

Extend the Transactions-page filters to the final order:

```text
Period
Type
Account
Description
Category
Amount
```

Amount uses a native mode selector with exactly these options:

```text
Any amount
Exact
Less than
Greater than
Between
Dollar range (00–99¢)
```

Show the minimum required shared USD money input or inputs for the selected mode. On desktop, always place the Amount mode, its visible Value, Minimum, or Maximum inputs, and Apply together on a dedicated second filter row so changing modes never moves Amount conditionally between rows. On narrow screens, stack that group naturally at full width. `Any amount` shows no Amount input and is the default. `Exact` matches one cent amount exactly. `Less than` and `Greater than` use strict comparisons. `Between` requires two values and includes both endpoints; reject a range whose minimum exceeds its maximum. `Dollar range (00–99¢)` requires one nonnegative whole-dollar value and matches that dollar through the following 99 cents, for example `$19` matches `$19.00` through `$19.99`; reject a cents-bearing Dollar-range value instead of silently truncating it. Do not show a dynamic range hint beside or beneath the Amount controls. Keep the compatible URL mode value `withinDollar`.

For filtering only, compare the absolute magnitude of each authoritative ledger event amount in integer cents. An Expense, Income, negative Income correction, and Expense refund with the same absolute amount all match the same Amount filter. A valid combined transfer uses its existing unsigned event amount. Visible invalid linked-transfer rows use each underlying row's absolute amount. This does not change stored signs, reporting meaning, transaction-history display, or Amount sorting.

Parse Amount filter values through the shared authoritative Python USD string-to-cents parser and apply integer-cent comparisons without floating point. Filter eligibility and results remain authoritative in Python; JavaScript only manages mode-dependent control visibility, input presentation, and readable URL state. Combine Amount with every existing Period, Type, Account, Description, and Category filter.

Represent active Amount filtering in the Transactions URL with readable `amountMode` plus `amount`, or `amountMin` and `amountMax` for Between. Loading, refreshing, Back and Forward, sorting, editing, deleting, duplicating, and Send to Simulator preserve the complete filter state. Returning to `Any amount` removes every Amount parameter. Invalid or incomplete URL values retain understandable controls and produce concise validation feedback rather than silently applying a different filter.

Do not add separate debit, credit, positive, negative, Income, Expense, refund, or transfer Amount modes. Do not change Amount sorting.

Below the Transactions table, show a compact summary using the same label-and-value presentation as the Dashboard and Simulator. Show exactly `Expenses`, `Income`, and `Transfers / Payments`; do not show an inflated grand total. Calculate all three values authoritatively in Python after applying every active filter and after combining valid transfers. Categorized Expense-role transactions use their displayed entered-value semantics, so refunds reduce Expenses. Categorized Income-role transactions use their displayed entered-value semantics, so reversals reduce Income. Valid combined transfers and payments use their positive unsigned displayed amounts and are counted once in Transfers / Payments. Keep uncategorized activity and visible invalid linked-transfer rows in the table but exclude them from these semantic totals. Sorting changes row order only and never changes the summary. Return the summary only when requested, without changing the default Transactions API list response used by existing consumers. Show stable zero values and let the three values wrap cleanly on narrow screens. Do not store the totals or change balances, reporting, ledger signs, or transfer math.

## Categories

Categories are used for expense reporting.

The hierarchy is intentionally limited to one level.

Conceptually:

```text
Category

id
name
parentCategoryId
active
displayOrder
offerReimbursement
reportingRole
```

A category may either be:

1. A standalone selectable category
2. A rollup category containing child categories
3. A child category

No deeper hierarchy is permitted.

### Example category configuration

Account and Category names are user-owned labels, not required configuration. The examples below illustrate one possible Expense-category setup; new databases do not seed these records.

An example rollup named `Needs` could contain these direct children:

```text
Recurring Needs
Groceries/Gas/Household
Random Needs
Interest
```

In this example, transactions are assigned to the child category, not directly to the rollup.

The rollup is calculated from its children regardless of its name.

### Standalone categories

Example standalone category names include:

```text
Wants
Experiences
Giving
Savings/Future
Pass-Through
Income/Reimbursement
```

The legacy data also contains `Utilities`.

The original migration preserved that category rather than silently changing historical data. It may remain standalone unless deliberately changed through category management.

### Category selection

A category with children is not selectable when entering a transaction.

A standalone category is selectable.

A child category is selectable.

### Category management

Settings must allow:

```text
Add category
Rename category
Choose or change parent
Set display order
Archive category
```

Archiving a category does not alter historical transactions.

Changing a category's parent intentionally changes how historical reporting rolls up.

A boolean `Offer reimbursement shortcut` setting defaults off. Existing migrated configuration, including the enabled setting on an existing Pass-Through category, is preserved. When an enabled selectable category is chosen during new ordinary transaction entry, the existing optional reimbursement fields appear. Saving atomically creates the ordinary transaction and an independent Income-role transaction against the configured Reimbursement Category. Do not create a reimbursement transaction type or link the rows. A rollup may retain the setting but it has no effect while nonselectable.

An active or archived category may be permanently deleted after named confirmation only when no transaction references it, it has no children, it is not referenced by a Flexible Category Budget, and it is not the configured Reimbursement Category. Check these rules on the backend immediately before deletion. Atomically clear any template category references and delete the category. Never cascade-delete, recategorize, or reparent financial history.

### Category initialization

Creating a brand-new database creates no category records. When an account or categories are missing, show a simple empty-state message directing the user to create them in Settings before entering transactions.

Normal startup, initialization, and generic repair logic for an existing database must never create a missing category or recreate one that the user renamed or deleted. Explicit schema migrations may add or transform required fields and settings, but must not invent replacement configuration. If a safe migration cannot identify an existing record, fail clearly. Existing categories and every transaction that references them must remain unchanged across normal application restarts.

### Reporting Roles and Reimbursement Category

Each category has a required `Reporting Role` with the values `Expense` and `Income`. New categories default to Expense, and multiple Income categories are allowed. Python uses the explicit role for financial meaning; category names are presentation only.

Use Reporting Role for authoritative sign conversion and reporting:

```text
Income: storedAmount = enteredAmount
Expense: storedAmount = enteredAmount multiplied by negative one
```

Income is the sum of all Income-role categories. Expenses and account-level expense reporting use only Expense-role categories. Refunds remain positive ledger amounts against Expense categories and reduce spending rather than becoming income. Income categories belong in the Income summary and not the expense breakdown. Transaction APIs must return the category role needed for editing; frontend code must not infer entry signs from category names.

Income categories must be standalone selectable categories and cannot have a parent or child categories. Expense categories retain the existing standalone, parent, and child behavior. Once a category has any transaction, its Reporting Role cannot change. An unused category may change roles only when its hierarchy is compatible. Do not silently reclassify historical reporting.

Settings provides one control labeled `Reimbursement Category` that lists active standalone Income-role categories. Store the selection by stable category ID. The reimbursement shortcut creates its independent income transaction against this selected category. Renaming the selected category retains its identity. Changing the selection affects future reimbursements only and never rewrites history.

The configured Reimbursement Category cannot be archived, deleted, moved under a parent, or changed to Expense until another destination is selected. If reimbursement shortcuts are enabled but no valid destination exists, block reimbursement creation with a clear error. There is no separate reimbursement destination for each expense category.

Deletion protection uses stable references and the safety rules above, never a category name. Categories with transactions remain protected.

The intentional legacy Reporting Role migration assigns Income to the existing `Income/Reimbursement` category and Expense to every other existing category, and selects that Income category as the initial reimbursement destination when present. The earlier reimbursement-shortcut migration enables the existing `Pass-Through` category. These mappings preserve the legacy financial meaning, transactions, and stable IDs; they are not startup defaults or generic repair rules. Normal restarts preserve existing category configuration, roles, reimbursement settings, and historical classifications without reapplying name-based mappings or inventing replacement categories.

### Category Rollups

Any top-level Expense-role category with one or more children is automatically a rollup. Derive its amount, percentage, and per-day values from all of its direct children, keep it nonselectable, and render its children indented beneath it using the existing one-level hierarchy. Its name has no financial meaning.

A prospective parent may become a rollup only when no transaction, Template, or Flexible Category Budget references it, including old, archived, inactive, and net-zero references. Reject child creation or reparenting with clear guidance to create an empty parent for a category with history, or manually change/remove planning references. Python checks references within the hierarchy write transaction, and transaction/configuration assignment paths revalidate selectability under their write lock. Preserve IDs, history, balances, and existing one-level hierarchy.

If a legacy rollup already has direct transaction references, the Expense by Category breakdown shows a category-identifying error until the user deliberately corrects all direct references through existing transaction editing. Never omit direct activity silently or present incomplete rollup figures as valid. The Dashboard response returns categories as an empty array with categoryReportingError text; normal responses return null for that field. Other breakdowns, authoritative totals, history, and correction workflows remain available. This check includes references outside the selected period and net-zero activity; do not automatically rewrite history or add a repair wizard or Direct activity row.

Render every rollup and its children generically in Settings-defined display order. Apply `Hide zeros` to each child normally and hide a rollup when all of its children have zero selected-period amounts. Renaming a rollup or creating another compatible parent category must not change or break rollup behavior.

Preserve `Needs` and its current children in existing databases as ordinary user-owned categories, but do not seed them into new databases. Do not add deeper hierarchy, selectable parent categories, special rollup types, stored rollup balances, or name-based exceptions. Production code must not compare a category name to `Needs`.

### System-Wide Category Role Groups

For every maintained native Category selector that genuinely includes both Reporting Roles, group eligible options with native HTML optgroups in this order:

```text
Expense
Income
```

Use `<optgroup label="Expense">` and `<optgroup label="Income">`. Preserve each eligible Category's Settings-defined display order within its role group. Omit a group when it has no eligible options. Keep control-specific placeholders such as `Select category…` and `All categories` outside the optgroups. Use the authoritative backend-provided Reporting Role and stable Category ID; never infer grouping from Category names.

This applies to every mixed-role Category selector. Preserve each control's selection, active/archive eligibility, and refresh behavior.

Expense rollups remain nonselectable in creation and editing forms. Preserve the existing visual parent/child context for selectable Expense children within the Expense optgroup without adding deeper hierarchy or a custom select. Multiple Income categories appear normally in Settings-defined order. Renaming a Category never changes its group.

Historical filters continue to include archived Categories according to their existing eligibility and archived labeling, grouped by their stored Reporting Role. Do not add a redundant optgroup to a role-specific selector, including Reimbursement Category, Flexible Category Budget, compatible Category Parent selection, or another control deliberately restricted to only Income or only Expense.


## Income, Expenses, and Refunds

Ordinary transactions use their Category Reporting Role for sign conversion and reporting, regardless of the Category name. Positive Income entry becomes positive ledger cents; negative Income entry is a reversal and becomes negative ledger cents. Positive Expense entry becomes negative ledger cents; negative Expense entry is a refund and becomes positive ledger cents.

A refund is not a special transaction type.

A refund is a negative user entered amount against the original expense category.

Example:

```text
Target Purchase
Entered: 50.00
Stored: -50.00
Category: Random Needs
Reporting Role: Expense
```

```text
Target Refund
Entered: -20.00
Stored: +20.00
Category: Random Needs
Reporting Role: Expense
```

Net spending in the category becomes:

```text
30.00
```

Refunds reduce spending.

They do not become income.

## Expense Reporting

Expense reporting uses the same transaction ledger as account balances.

There is no second expense ledger.

Reporting includes categorized, nontransfer transactions.

Transactions with no category do not affect expense reporting.

This excludes:

```text
Opening Balance
Balance Adjustment
Transfers
Uncategorized historical bank activity
```

`Opening Balance` and `Balance Adjustment` in this list refer to existing imported historical transactions. New opening balances and reconciliations use `balanceOffsetCents` and never enter expense reporting.

### Income

For the selected period:

```text
Income =
SUM(stored amount for all Income-role categories)
```

### Expenses

For the selected period:

```text
Expenses =
SUM(stored amount for all Expense-role categories) multiplied by negative one
```

Because refunds have a positive stored amount, they reduce this total.

### Net

```text
Net = Income - Expenses
```

This is cash flow for the selected period.

It is not net worth.

### Category amount

```text
Category Amount =
net expense amount for that category
```

### Category percentage

```text
Category Percentage =
Category Amount / Total Expenses
```

This represents the category's share of spending.

If total expenses are zero or negative, display the percentage as unavailable rather than dividing by zero.

### Expense category rollups

```text
Rollup Amount =
SUM(Category Amount for every direct child)
```

### Spending relative to income

The dashboard should also preserve the useful signal from the spreadsheet:

```text
Spending Percentage =
Expenses / Income
```

Label this clearly as spending relative to income.

If income is zero or negative, display it as unavailable.

### Per day

For every category and for total spending:

```text
Per Day =
Amount / number of calendar days in selected period
```

For `This Month`, use the number of days elapsed in the month through today.

Reporting totals for `This Month` include every transaction dated from the first through the last calendar day of the current month, including future-dated transactions later in that month.

For a completed calendar month, use every day in that month.

For `Year to Date`, use the number of days elapsed in the current year through today.

For `Last 30 Days`, use 30.

For a custom range, use the inclusive number of calendar dates in the range.

All displayed dollar amounts use two decimal places.

Percentages use two decimal places.

Per day amounts use two decimal places.

## Reporting Period Selector

The expense dashboard uses one period at a time.

Default:

```text
This Month
```

Required options:

```text
This Month
Last Month
Previous calendar months
Year to Date
Last 30 Days
Custom Range
```

Previous calendar months should be available without requiring separate spreadsheet style tabs.

They may be labeled using actual month names.

Example:

```text
July 2026
June 2026
May 2026
```

Selecting `Custom Range` reveals start and end date controls.

The same period concept should be reused for transaction filtering where practical.

## Dashboard

The dashboard is the landing page.

It combines the useful parts of the old bank account sheet and the old expense dashboard.

Required order:

```text
Header and primary actions

Cash Accounts

Credit Cards and Loans

Expenses

Pinned Transactions when present

Recent Transactions
```

Desktop may use two columns where doing so improves density.

Mobile stacks the same information.

There is no permanent sidebar.

### Header

Simple navigation:

```text
Dashboard
Transactions
Settings
```

Primary actions:

```text
+ Transaction
+ Transfer
```

Use a solid green background with black text for every primary `+ Transaction` and `+ Transfer` action throughout Dashboard, Transactions, Budget, and Simulator.

### Cash Accounts

Display:

```text
Account Name
Balance
```

Show:

```text
Cash Total
```

Cash Total is the sum of all cash account balances.

Provide:

```text
Hide zeros
```

Zero balances are hidden by default.

When zeros are shown, place exact-zero accounts after nonzero accounts independently in Cash Accounts and Credit Cards and Loans. Preserve alphabetical Account order within each class among nonzero accounts and among zero accounts. This ordering applies only to these dashboard sections.

When enabled, the same control also hides active credit accounts with a zero calculated balance, expense categories whose selected-period amount is exactly zero, and zero rows in the expense By Account table. Each Expense rollup is hidden when all of its children are zero. This is presentation filtering only and never hides transactions from the full ledger.

Account rows are informational.

Clicking an account balance does nothing.

### Credit Cards and Loans

Display each active credit account and its friendly balance. Credit card and personal loan subtypes appear together in this section because they share the same underlying `credit` account behavior.

Example:

```text
Discover        $153.00 balance
Apple Card      $430.20 balance
```

Credit account rows are informational.

Clicking them does nothing.

Show `Total` beneath the active credit accounts.

Calculate it using each active credit account's current balance:

```text
balanceOffsetCents + SUM(transaction amounts for account)
```

An internal negative credit balance contributes its amount owed as a positive value. An internal positive balance represents a credit and subtracts from the total owed.

Keep `Balance` as the column header. Display ordinary amounts owed as unsigned dollar values without repeating the word `balance` on every row. An overpaid card or net overpayment may retain the word `credit` for clarity. Cash accounts are not included.

### Transaction Breakdown

At the top of the section:

```text
Period selector
Expenses
Income
Net
Spending relative to income
Total spending per day
```

Show controls in this order on desktop: `Breakdown | Period | Start | End`. Breakdown is a labeled native select. Start and End remain associated with Period and are visible only for Custom Range. Stack the controls naturally on narrow screens. Breakdown options are:

```text
Expense by Category
Expense Details by Category
Expense by Account
Expense Details by Account
Income by Account
Net Transactions by Account
Balance Change by Account
```

Default to Expense by Category and show one breakdown table at a time. Preserve the selected view when Period or Hide zeros refreshes the Dashboard.

The Expense by Category table uses these desktop columns:

```text
Category
Amount
%
/ Day
```

Show every Expense rollup and its indented direct children.

Show standalone expense categories separately.

All Income-role categories belong in the Income summary rather than the expense category list.

Category rows are informational.

Clicking a category does nothing.

Do not add charts.

Expense by Account uses `Account`, `Amount`, and `%`. It uses the selected reporting period and the same reportable transactions, sign rules, total-expense denominator, and percentage rounding as category reporting. Include archived accounts that have reportable activity and active accounts with zero activity; Hide zeros controls presentation of exactly-zero rows.

Expense Details by Account uses exactly `Account / Category | Amount`. Group the same reportable Expense-role, nontransfer transactions first by Account and then by their exact assigned selectable Expense Category. Each Account heading shows its net Expense subtotal, followed by indented Category rows that sum exactly to it. Refunds reduce both values, including to a visible negative net. Show child Categories using their current one-level path, such as `Needs › Recurring Needs`, without adding a duplicative parent-rollup row; show standalone Categories normally.

Order Account groups exactly as Expense by Account and Category rows by Settings-defined hierarchy order. Include archived Accounts or Categories with selected-period activity and do not invent unrepresented Account/Category combinations. Hide zeros removes exact-zero Category rows and hides a group only when no Category rows remain; retain a zero Account subtotal when its visible positive and negative Categories offset. When zeros are shown, include represented exact-zero Category nets. Account headings and Category rows remain informational. Do not add percentages, per-day values, charts, collapse or sorting controls, an inverse view, configuration, another route, or a separate mobile model. Python owns grouping, paths, signs, subtotals, ordering, and inclusion; JavaScript only renders the returned two-column hierarchy.

Expense Details by Category mirrors that presentation using exactly `Category / Account | Amount`. Group first by the exact assigned Category and then by Account. Each Category heading shows its net Expense subtotal followed by indented Account rows that sum exactly to it. Use the same Category path, Settings hierarchy order, Account order, archived-activity inclusion, refund and negative-net handling, represented-combination rule, Hide zeros behavior, informational styling, and responsive two-column layout as Expense Details by Account. A zero Category subtotal remains visible when nonzero Account values offset; hide its group only when Hide zeros removes every represented Account row. Do not add parent-rollup duplication, percentages, per-day values, sorting, collapse controls, or another financial calculation. Both detailed views share one presentation style and are alternative groupings of the same authoritative Python results. Keep group headings bold against the normal page background, without a darker grey fill, matching the restrained Budget grouped-table presentation.

Income by Account uses the same columns, selected reporting period, account inclusion, and Hide zeros rules as Expense by Account, but includes only Income-role transactions and uses total Income as its percentage denominator.

Net Transactions by Account uses exactly `Account | Net Change`. For each Account, calculate the selected-period sum of categorized, nontransfer ledger amounts. Income-role transactions increase Net Change, Expense-role transactions decrease it, Expense refunds increase it, and Income reversals decrease it. This is equivalently categorized Income minus categorized Expenses for that Account and period. Use integer cents and do not derive the result from formatted values.

Exclude Transfers and Payments because they move money without being Income or Expense. Also exclude uncategorized historical activity, `balanceOffsetCents`, opening offsets, and reconciliation offsets. A debt payment therefore does not make a Credit Account appear to have negative reportable activity; an active Credit Account with no categorized activity has an exact-zero result.

Use the same authoritative Account order and active-versus-archived selected-period inclusion as the existing Account breakdowns. Include active Accounts with zero reportable activity and archived Accounts only when they have reportable selected-period activity. Group the same two-column table into `Cash Accounts` followed by `Credit Cards and Loans`. Show each bold, unshaded group heading with its authoritative Net Change subtotal beside it, followed by indented Account rows. Do not add a separate subtotal row or combined grand total. Both classes use the same Income-minus-Expenses sign meaning; a negative Credit subtotal represents net categorized Expense activity, not a debt reduction.

Hide zeros removes exact-zero Account rows using the backend-provided flag and hides a group when it has no visible rows. Retain a zero group subtotal when visible nonzero Accounts offset. Hidden zero rows do not alter the subtotal. Keep headings and rows informational and preserve the selected Breakdown across Period and Hide zeros refreshes. Do not add percentages, component rows, charts, drill-down behavior, stored values, another route, or configuration. Python performs all grouping, subtotals, inclusion, and integer-cent calculations; JavaScript only applies authoritative zero flags and renders the hierarchy.

Balance Change by Account is a separate breakdown using exactly `Account | Balance Change`. It answers how much each tracked Account's displayed balance changed through all dated ledger activity in the selected inclusive period. Include ordinary transactions, refunds, Transfers and Payments, uncategorized historical activity, and any other dated ledger row that changes an Account balance. Each transfer contributes its corresponding ledger side independently to each represented Account. Exclude `balanceOffsetCents`, including opening and reconciliation offsets, because those changes have no ledger date.

For Cash Accounts, use the period sum of signed ledger amounts. For Credit and Loan Accounts, reverse that internal change so the value follows friendly displayed-balance direction: a purchase that increases the amount owed is positive, while a payment that reduces debt is negative. Display an explicit `+` for positive change, a minus sign for negative change, and normally formatted zero. Show only Accounts with selected-period ledger activity, including archived Accounts with activity; do not add no-activity rows. Use the existing authoritative Account order.

Within the same two-column table, group the rows into `Cash Accounts` followed by `Credit Cards and Loans`. Show each group name as a bold heading with that class's authoritative Balance Change subtotal beside it in the Amount column, followed by indented Account rows. Do not add a separate subtotal row or a combined grand total because Cash movement and friendly debt movement have different meanings. Hide zeros removes exact-zero Account rows and hides a group when it has no remaining visible Account rows. If nonzero Account changes offset to a zero group subtotal, retain the heading and visible rows. Hidden exact-zero rows do not change a subtotal. Reuse the restrained detailed-breakdown group style with no darker grey fill. Group headings and Account rows remain informational. Do not alter Net Transactions by Account, Income, Expense, balances, transfers, reporting totals, or stored data. Python owns grouping, subtotals, inclusion, signs, and integer-cent calculation; JavaScript only applies the backend zero flags and renders the returned hierarchy.

### Recent Transactions

Display:

```text
5 rows on every form factor
```

Sort newest first and show five events on every form factor. Use date and transaction ID as the deterministic order when multiple transactions share a date. Exclude pinned ordinary transactions.

In Dashboard Recent Transactions, combine a valid two-row transfer as one event showing `From Account → To Account`, its description, `Transfer`, and the unsigned amount. It opens the existing transfer editor and counts as one event. Invalid linked data remains visible as underlying rows.

Show enough information to recognize the transaction:

```text
Date
Account
Description
Category
Amount
```

Mobile may collapse the same data into compact stacked rows.

A recent transaction may be opened for editing.

Provide:

```text
View all transactions
```

### Dashboard Zero Filtering

Keep the existing Dashboard-wide `Hide zeros` checkbox checked by default and without browser-storage persistence. Move it from the Cash Accounts area to the Dashboard's top action row beside `+ Transaction`, `+ Transfer`, and `Bulk Entry`, styled as a visually secondary control. Its existing zero-hiding behavior remains unchanged across all Dashboard sections.

### Credit Utilization Display

On the Dashboard Credit Cards and Loans table, place each eligible Credit Card utilization badge immediately after its account name in the Account column. Keep the Balance column for the friendly account balance only. Personal Loans, Other credit accounts, and cards without credit limits have no individual badge.

Move total revolving utilization outside and immediately below the table as a compact, visually secondary `Credit Utilization` label with its existing status badge, aligned with the balance column. The table's `Total` remains only the total credit-account balance, which may include non-revolving credit accounts. Preserve all existing utilization calculations, eligibility rules, thresholds, solid badge colors, accessible labels, and responsive behavior.

### Reusable Status Banners

Use one shared status-banner design for page-level messages on any application page. A banner uses a solid background, normal border and spacing, no gradient, icon, animation, or shadow, and spans the full available content width of its page area. Its text must state the status clearly; color is never the only signal.

Use Green for positive status, Yellow for caution, and Red for a problem or unavailable state. Use black text on Green and Yellow backgrounds and white text on Red backgrounds, with sufficient contrast. Reuse this visual and semantic pattern without creating a JavaScript component framework or a notification history. A temporary post-save toast is not a status banner and does not use this page-level layout.

Dashboard page alerts appear below the action buttons and before the account grid, in severity order: Red problem alerts, then Yellow cautions, then Green positive statuses. Keep the Auditor access banner above page-specific banners. Temporary bottom notifications retain their separate layout.

### Cash Account Minimums

Cash accounts may have an optional `Minimum Balance` as an informational financial guardrail. The minimum never changes the authoritative account balance, hides the real balance, creates a spendable or available balance, reserves money, changes reporting, or creates ledger activity.

Add nullable `minimumBalanceCents` and `minimumCautionPercent` fields to Cash accounts through a small explicit schema migration. `minimumBalanceCents` stores a positive integer number of cents. A blank or zero Minimum Balance clears both minimum fields and means that the account has no configured minimum; negative values are invalid. `minimumCautionPercent` stores a nonnegative whole-number percentage. It applies only when the account has a minimum and defaults to 25% when a minimum is entered. Only Cash accounts may have either field. The Account create and edit form provides optional `Minimum Balance` and `Caution Range` fields for Cash accounts only and states `Leave blank or enter $0 to clear.` When Minimum Balance is blank or zero, Caution Range is inapplicable and not shown as active configuration. Changing an account from Cash to Credit clears both fields; changing a Cash account's subtype does not. An archived account retains its configured values but is not shown in normal active-account Dashboard presentation.

When Minimum Balance and Caution Range contain a valid combination in the Account create or edit form, show compact read-only helper text beneath them describing the derived status ranges. Do not show the helper while either field is blank or invalid. With a $1,000.00 minimum and 25% Caution Range, show:

```text
Green: $1,250.00 and above
Yellow: $1,000.00 through $1,249.99
Red: $999.99 and below
```

Calculate the Green threshold in cents by rounding upward to the next whole cent:

```text
greenThresholdCents = ceiling(
  minimumBalanceCents * (100 + minimumCautionPercent) / 100
)
```

Yellow spans from the configured minimum through one cent below the rounded-up Green threshold; Red is one cent below the configured minimum and lower. With a zero Caution Range, omit Yellow and show only Green at or above the minimum and Red below it. The backend returns the display-ready helper ranges as valid form values change; JavaScript must not independently calculate or round them.

The authoritative balance remains:

```text
balanceOffsetCents + SUM(transaction amounts for account)
```

For a Cash account with a minimum, calculate:

```text
differenceFromMinimum = currentOrProjectedBalance - minimumBalanceCents
```

On the Dashboard, show the actual calculated Cash balance unchanged. For every active Cash account with a configured minimum, place a compact accessible status badge immediately after the account name, using the same visually secondary treatment as Credit utilization badges. The badge communicates the dollar difference and must not rely on color alone:

```text
Green:  current balance is at least its minimum plus that account's Caution Range;
        when Caution Range is 0%, balance at the minimum is also green
        "$X.XX above minimum"

Yellow: Caution Range is greater than 0%, and current balance is at or above
        its minimum but below the green threshold
        "$X.XX above minimum", or "At minimum" when equal

Red:    current balance is below its minimum
        "$X.XX below minimum"
```

Each account's Caution Range determines its green threshold and equals that configured percentage of the account's minimum. Compare this threshold using integer arithmetic; do not use floating point. With the default 25% range, a $1,000.00 minimum is green at $1,250.00 or more, yellow from $1,000.00 through $1,249.99, and red below $1,000.00. With a zero range, the yellow band disappears: a balance at or above its minimum is green, with the exact-minimum badge reading `At minimum`; a balance below its minimum is red. Caution Range is presentation configuration only: it does not alter an account's minimum, account balances, ledger data, plans, or reports. Each badge's accessible label includes the account name, actual balance, configured minimum, and status.

When no active Cash account is below its configured minimum, show no Dashboard warning. When one or more are below, show one full-width Red shared status banner headed `Minimum balance alerts`, with one readable line per affected account, for example:

```text
Chase Checking is $120.00 below its $1,000.00 minimum.
Emergency Fund is $45.00 below its $500.00 minimum.
```

Minimum alerts are informational only. They do not block transactions, reserve money, suggest transfers, create notifications or history, or require dismissal.

In Simulator Results, for every Cash account that already appears in the Results table and has a configured minimum, show the same badge immediately after the account name, matching the Dashboard placement of Credit utilization badges. Compare the final projected internal balance with the configured minimum using the same difference, text, colors, and accessibility rules as the Dashboard. Do not add otherwise unrelated accounts to Results merely because they have a minimum, add a separate Simulator warning area, or infer temporary balance dips: the Simulator has no row dates or execution ordering and evaluates only its final projected balance.

The Python backend owns validation and all minimum-balance calculations. API responses provide the calculated status and display-ready difference required by the browser; JavaScript must not independently implement minimum arithmetic or threshold classification.

### Hide Zeros Minimum Exception

On Dashboard Cash Accounts, an active Cash Account that is below its configured Minimum remains visible even when `Hide zeros` is enabled and its current calculated balance is exactly zero. A violated minimum overrides only the exact-zero visibility filter so the Account responsible for an active minimum warning is never concealed.

Apply this presentation rule authoritatively using the backend-provided minimum-violation flag:

```text
Hide an exact-zero active Cash Account only when
Hide zeros is enabled and the Account is not below its configured Minimum.
```

An Account with no configured Minimum or a zero Minimum is not in violation at a zero balance and remains eligible to be hidden. Nonzero Cash Accounts already remain visible under the existing behavior. Preserve alphabetical Account ordering within each class and exact-zero placement after nonzero Accounts; do not promote, reorder, or otherwise style a zero-balance violation through this exception.

Do not change Cash Total, minimum calculation or banner behavior, Credit-account zero filtering, Expense-category filtering, breakdown filtering, archived-account behavior, or any financial calculation. JavaScript must not independently compare balances and minimums.

### Credit Utilization Warning

Credit Card Accounts with a configured Credit Limit may have an optional account-specific `Utilization Warning (%)` as an informational guardrail. Store it as nullable integer `utilizationWarningBasisPoints`, where one percentage point is 100 basis points. Accept positive percentages from `0.01` through `100.00` with zero, one, or two entered decimal places; for example, `29.99%` stores as `2999`. Blank means no account-specific Dashboard alert. Clearing the input and saving the Account stores null, disables only that card's account-specific utilization banner alert, clears the helper text while retaining its reserved layout space, and leaves the Credit Limit and normal utilization badge behavior unchanged. Entered `0` is invalid and is not an alternate clearing convention. Reject more than two decimal places and never parse, store, or compare the percentage using floating point. Do not default it silently to 30%; configuring a persistent warning is explicit.

Show the field in the existing Credit Card configuration group in Account creation and editing only when a Credit Limit is configured. Clearing the Credit Limit clears the warning. Changing the Account class or subtype away from Credit Card also clears it. Personal Loan and Other Credit Accounts cannot configure it. Do not add a `Use 30%` button or store a derived dollar warning amount.

When Credit Limit and Utilization Warning contain a valid combination, show compact read-only helper text beneath them:

```text
Warning begins at a $X.XX balance.
```

Calculate the first whole-cent amount owed that reaches the configured percentage authoritatively in Python:

```text
warningBalanceCents = ceiling(
  creditLimitCents * utilizationWarningBasisPoints / 10000
)
```

Round the derived helper balance upward because it identifies the first representable whole-cent balance that actually reaches the configured percentage. The authoritative threshold comparison below remains unrounded; the helper does not replace it.

Refresh the helper safely as either field changes. Reserve stable vertical space for the maximum helper presentation using the same established approach as the Cash Minimum helper so the Account dialog does not flash, collapse, or change height while a recalculation is pending or when helper text is cleared. Do not add loading text, a spinner, animation, or repeated placeholder characters. Keep the previous valid helper visible until the latest valid response replaces it where doing so does not misrepresent an invalid current form value; otherwise clear its text while retaining the reserved blank space. Ignore stale out-of-order responses with a request sequence or equivalent small guard. Do not show helper text while either field is blank or invalid. JavaScript must not independently calculate or round the warning balance.

For every active Credit Card with a Credit Limit and configured warning, trigger an alert when its amount owed is positive and its authoritative utilization is at or above the configured percentage. Compare with integer arithmetic rather than rounded display percentages or floating point:

```text
amountOwedCents * 10000 >= creditLimitCents * utilizationWarningBasisPoints
```

When no active card has reached its configured warning, show no new banner. When one or more have reached it, show one full-width persistent Red shared status banner immediately below `Minimum balance alerts` and above the Dashboard account grid. Use the heading and one compact line per affected Account in alphabetical Account order:

```text
Credit utilization alerts
Discover: $2,617.00 (52.34%) — 40% warning reached
```

Use the established unsigned friendly Credit balance and existing two-decimal utilization display. Display the configured warning without unnecessary trailing decimal zeros, such as `30%` or `29.99%`. The final phrase works both at and above the configured threshold. An overpaid or zero-balance card never triggers. The banner is not dismissible and disappears after authoritative refreshed utilization falls below every configured warning. If the planned automatic-backup banner is implemented, its established order remains above minimum alerts; utilization alerts remain below minimum alerts.

This account-specific warning does not change the existing utilization badge's Green below 30%, Yellow from 30% through below 50%, and Red at 50% or higher thresholds. Add no configurable Yellow state. Do not change Total revolving utilization, add a total-utilization warning, show a Simulator warning banner, block or warn inside transaction entry, suggest a payment, create a notification history or Telegram event, or alter balances, limits, transactions, Simulator calculations, or reporting.

Implement the smallest explicit schema migration preserving all Accounts and ledger data. Python owns validation, basis-point parsing, helper calculation, utilization eligibility, threshold comparison, balance and percentage presentation, alert ordering, and banner lines.

## Transaction Templates

Templates are saved transaction form presets.

They are not recurring transactions.

They never automatically create transactions.

### Template fields

Conceptually:

```text
TransactionTemplate

id
name
accountId
description
categoryId
enteredAmountCents
prefillAmount
active
displayOrder
```

Template amount follows the user facing entry convention rather than the internally stored ledger sign.

### Template management

Location:

```text
Settings
Transaction Templates
```

Display a simple table:

```text
Sort Order
Name
Account
Category
Amount
Actions
```

`New Template` appears above the table. Each row has Edit; the editor provides Active status for archiving or reactivating and a confirmed Delete action, subject to linked-plan protections.

Template form:

```text
Template Name
Account
Description
Category
Amount
Prefill Amount
Sort Order
Active
```

Template name and transaction description are separate fields. Account, Description, Category, and Amount are optional; blank values display as `—`. A configured amount must be nonzero. Applying a partial template clears omitted transaction fields while preserving Date. Active and archived templates may be permanently deleted after named confirmation without affecting transactions.

Example:

```text
Template Name: Car Insurance
Description: Allstate Payment
```

No recurrence fields are allowed.

Do not add:

```text
Frequency
Due date
Day of month
Automatic posting
Notifications
```

### Using a template

The transaction form begins with:

```text
Template
```

Default:

```text
None
```

Selecting a template prefills:

```text
Account
Description
Category
Amount
```

Applying a template preserves the form's selected Date, which defaults to browser-local today when a new transaction form opens.

`Prefill Amount` is checked by default, including for templates migrated from earlier schemas. When checked, the saved template amount prefills transaction Amount. When unchecked, the saved amount remains available for Budget calculations but transaction entry leaves Amount blank. Account, Description, and Category prefills are unchanged.

Every prefilled field remains editable before saving.

If the template selector is changed back to `None`, clear Account, Description, Category, and Amount. Preserve Date unchanged.

### Bulk transaction entry

Bulk Entry is a horizontally contained ordinary-transaction table with one trailing browser-local-dated entry row instead of an Add Row button. The trailing row has all ordinary bulk-entry fields and no Remove action. Once its Date, Account, Description, Category, and Amount are all filled, turn it into a normal removable row and immediately append a fresh trailing entry row. Rows may use different values. Ignore blank and default-date-only rows. Report partial rows with row numbers without clearing entered values. This browser behavior controls only row availability; Python remains authoritative for active accounts, selectable active categories, dates, descriptions, nonzero integer-cent amounts, normal sign rules, and inserting the entire valid batch atomically. Transfers, templates, pins, and reimbursement shortcuts are excluded.

### Required Template Guidance

In the existing New Recurring Budget Item and New Expected Monthly Income forms, show this normal inline instructional text directly above the required Template selector:

```text
Note: A transaction template is required to create this item. Configure templates in Settings — Templates.
```

Render `Settings — Templates` as a normal link to `/settings#templates`. Show the note whenever either creation form is open, not only when no eligible template exists. Immediately beside or below either Settings link, show compact plain text: `Opening Settings will close this form and discard unsaved changes.`

In Edit Recurring Budget Item and Edit Expected Monthly Income, an existing selected required Template adds a safely rendered direct link immediately below the selector: `Edit "<Template name>" in Settings — Templates`. It opens `/settings?highlightTemplate=<stable template ID>#templates`. Update the link when the selected Template changes. Edit forms with no selected Template retain only generic guidance. Optional Template forms, including ordinary entry, receive no direct edit link.

Settings scrolls an eligible targeted Template row into view before applying one restrained temporary yellow highlight. This affects presentation only and does not select or open an editor, write configuration, or change any linked Budget item. Malformed, missing, archived, or ineligible target IDs show normal Settings Templates with no highlight or new error state. The row's ordinary Edit action remains the only editing flow.

Following either link navigates normally and discards the Budget-form draft, without confirmation, saving, preserving or restoring modal state, or a special return flow. Back, Forward, reload, Escape, Cancel, keyboard navigation, and normal Settings navigation retain their ordinary behavior. Keep stable-ID references, eligibility, validation, and persistence unchanged; saving a Template does not automatically change a linked Budget item's saved fields. Do not add inline editing or a generic Settings-row highlighting mechanism.

## UI Design Rules

The UI must be intentionally plain.

Target feel:

```text
Google Sheets
Older Google web applications
Simple administrative tools
Clean data tables
Native forms
```

Avoid:

```text
Gradients
Glass effects
Large decorative cards
Huge typography
Animated dashboards
Decorative charts
Excessive icons
Hidden gestures
Floating action buttons
Fancy transitions
Custom controls when native controls work
```

Use system fonts.

Use normal borders and spacing.

When the active primary-navigation item already identifies Dashboard, Transactions, Budget, Simulator, or Settings, omit a redundant page-title heading. Begin the page content with its actions, subsection navigation, or primary section heading instead. Preserve meaningful section and dialog headings.

Prefer tables and straightforward forms.

Do not make informational data look clickable.

No unnecessary animation.

Dialogs use normal accessible native behavior. Clicking or tapping the grey backdrop closes an open modal only when both pointer-down and pointer-up occur on the backdrop. Dragging from inside must not dismiss it. Backdrop dismissal behaves exactly like Cancel or Close, discards unsaved changes, and never confirms a destructive operation. Preserve explicit Close controls and Escape behavior.

Data density is desirable.

Desktop should take advantage of width.

Mobile should stack the same information rather than becoming a different application.

### Destructive Action Styling

Use a consistent solid red `Delete` button for actions that actually delete a record. Keep archive or status changes, Cancel, and non-destructive actions neutral. Preserve all deletion confirmations and do not add new destructive behaviors.

For configuration and planning records, place the red destructive Delete action at the bottom of the Edit modal followed by the existing confirmation, rather than in the table row. This applies to categories, templates, Budget plans, and any configuration or planning record that has deletion behavior. Accounts retain their existing archive behavior without adding deletion. Ledger transactions and transfers use the confirmed red Delete action within their existing-event editors, as specified in Delete Within Transaction Editing.

### Temporary Ledger Notifications

Extend the shared status-message visual language with a temporary notification presentation while preserving the existing persistent page-level banner. A persistent banner remains in normal document flow at the top of its relevant page area until its underlying condition changes. A temporary notification spans the full width of the application content area, is fixed to the bottom of the viewport without shifting page layout, uses a compact two- or three-line layout with a strong top border, and automatically disappears. Do not add a gradient, icon, animation, countdown bar, shadow, notification center, history, queue, or persistence.

For this release, show a temporary notification only after a successful authoritative ledger write:

```text
Ordinary transaction created
Ordinary transaction modified
Ordinary transaction deleted
Transfer or payment created
Transfer or payment modified
Transfer or payment deleted
```

Use concise action-specific text and backend-provided authoritative post-write balances. An ordinary ledger operation uses two lines:

```text
Saved - Transaction added
Discover: $836.59
```

Use `Saved - Transaction updated` or `Deleted - Transaction deleted` as appropriate. Show the affected Account's resulting balance. If an ordinary edit moves the transaction to a different Account, use three lines with the current/new Account first and the previous Account second, showing both post-write balances.

A transfer or payment operation uses exactly three lines:

```text
Saved - Transfer added
Chase: $123.45
Discover: $836.59
```

Use `Saved - Transfer updated` or `Deleted - Transfer deleted` as appropriate. Always show the transfer's From Account first and To Account second, including after deletion. For an edit that changes an Account selection, show the final/current From and To Accounts rather than expanding the notification beyond three lines. Deduplicate neither side because transfer direction remains meaningful. Use the established friendly Credit display convention without an internal negative sign or the word `owed`; retain `credit` for a positive Credit overpayment.

Do not repeat Description, Category, or transaction Amount in the notification. A successful deletion is Green because the notification communicates a successful outcome; Red continues to mean a problem or unavailable state rather than a destructive action. Do not show a success notification for cancellation, validation failure, an unchanged edit, or an unsuccessful write. Keep errors as persistent inline or dialog feedback rather than automatically dismissing them.

Provide one small shared JavaScript method with this public shape:

```js
sendNotification(message, {
    time: 5,
    hasCloseButton: true,
    color: "green"
});
```

The options object is optional, and each option uses the displayed default when omitted. `message` is required plain text and must be inserted with safe text APIs such as `textContent`; do not accept or render an HTML string. `time` is the number of seconds before automatic dismissal. A nonpositive, nonnumeric, or otherwise invalid value falls back to five seconds and must not create a persistent notification. `hasCloseButton` controls an explicit accessible `Close` button. `color` accepts only `green`, `yellow`, or `red`, case-sensitively; an omitted or invalid value falls back to Green. Initial successful ledger notifications all use Green.

Display only one temporary notification at a time. A newer call replaces the visible notification and restarts its dismissal timer. Pause automatic dismissal while the notification or its Close button has pointer hover or keyboard focus, then resume the remaining time when interaction ends. Closing it removes it immediately. Use `role="status"` with a polite live announcement, never move keyboard focus to it, and ensure removal does not leave focus on a nonexistent control.

On narrow screens, retain the full application-content width, respect the device safe area, preserve the two- or three-line structure, and avoid horizontal overflow. Do not display the notification over an active virtual keyboard or obscure the focused form control.

### Shared USD Money Inputs

Replace the application's user-editable native number-style money controls with one shared plain USD money-input pattern. Apply it deliberately to every user-entered dollar field, including ordinary and reimbursement transaction entry, Bulk Entry, transfers/payments, templates, account opening balances, reconciliation, credit limits, Cash minimums, Budget, Simulator, and any other maintained money input found during the required inventory. Computed money displays and noneditable table values are not inputs and retain their existing display formatting.

Each money control uses a visible `$` prefix immediately beside a text input with `inputmode="decimal"` for a phone-friendly numeric keyboard. Keep every existing field-specific label and make its dollar meaning accessible; mark the visual prefix as decorative so it is not announced redundantly. Do not add a custom numeric keypad, spinner, mask library, frontend framework, or dependency.

Use one authoritative Python string-to-integer-cents parser for all money inputs. It accepts either one trimmed USD value or a bounded addition/subtraction expression. Each term uses the same strict USD syntax; optional whitespace is permitted around operators. It accepts these values and expressions:

```text
1000
1,000
1000.5
1,000.50
0.50
.50
-17.27
$1,000.00
532+110
1,000-125.50
$1,000+$50
```

When a sign is permitted by the consuming field, accept it before the optional dollar sign, so both `-17.27` and `-$17.27` are valid while `$-17.27` is malformed. This preserves the application's existing negative-refund convention without ambiguity. Commas, when present, must use proper three-digit grouping. Accept at most two decimal places. Expressions support only addition and subtraction, reject repeated signs and all other expression syntax, and have bounded length and term count. Reject malformed grouping, repeated currency signs, multiple decimal points, scientific notation, accounting parentheses, currency codes, descriptive text, and any value that cannot be represented safely as integer cents. Never parse or convert money through binary floating point.

Parsing only converts a syntactically valid USD string to integer cents. After parsing, retain every existing field-specific rule for required versus optional values, blank handling, sign, nonzero or positive constraints, Cash and Credit conventions, refunds, opening balances, reconciliation, limits, minimums, Budget calculations, Simulator calculations, templates, transfers, and reimbursements. An optional blank remains null or absent according to its existing contract and must not silently become zero. Python remains authoritative; JavaScript must not independently define financial validity or cents conversion.

Do not insert grouping punctuation on every keystroke. On blur, a nonblank field is evaluated by the shared Python parser and, when valid, replaced with its comma-grouped two-decimal result. Preserve a leading negative sign for fields where negative input is valid. Invalid input remains exactly as entered and receives concise form feedback. Submission does not wait for blur evaluation: it submits the raw value and the consuming Python write path parses it again before applying field-specific rules. An optional blank remains blank and does not become zero.

### Friendly Calendar-Date Display

Every read-only human-readable calendar date uses one browser-local current-year formatter. Current-year ISO dates display as `Mon D`; dates in another year display as `Mon D, YYYY`. The formatter parses ISO year, month, and day components directly and never converts a date-only value through a JavaScript `Date`, so its calendar day remains unchanged in every timezone.

Read-only ranges compact same-year context when applicable and retain both years for cross-year ranges. Native date inputs, database and API values, URL parameters, exports, filtering, sorting, recurrence, reporting periods, and all other calendar calculations remain ISO `YYYY-MM-DD`. Server-local timestamps retain their timestamp presentation.

Use concise errors that distinguish malformed USD syntax, excess decimal places, and the field's established semantic constraint where practical. Do not remember values, change defaults, alter form field order, redesign a workflow, or change stored/API money representation beyond accepting the documented user-input strings and continuing to return authoritative integer cents and display-ready values as already required.


### Application Version and Release Summary

Settings System displays the authoritative application version above Database Status. The existing Release History modal shows `26.10` first with three brief, user-facing bullets: Owner and read-only Auditor sign-in; integrations submitting transactions and transfers for human review and approval; and shared Pending review, account selector pins, and Budget template shortcuts. Combine the former `26.09` and `26.08` summaries under one `Earlier versions` heading with two bullets covering accounts, transactions, transfers, reporting, and backups; and Budget and Simulator planning, templates, and bulk entry. Do not show separate August or September headings. Keep implementation details out of these summaries and do not add a separate patch-version history entry, extra controls, or another page. Keep the displayed version and summaries synchronized.

The Release History modal ends with the `About the Name` quotation “The best part is no part, the best process is no process, it weighs nothing, costs nothing, can't go wrong.”, attributed to Elon Musk and identified as the inspiration for No Part Finance. Do not show the version in the application bar or add external loading, tracking, or links.

### Settings Sections

**Sections.**

Replace the crowded single Settings page with one Settings route containing six URL-fragment-backed sections:

```text
Accounts | Categories | Templates | Budget | Integrations | System
```

Use normal accessible navigation links rather than a custom tab widget. The selected link uses an appropriate current-state indicator. Fragments are navigation state, not query input: `#accounts`, `#categories`, `#templates`, `#budget`, `#integrations`, and `#system`. Loading Settings without a fragment shows Accounts. Hash changes and browser Back and Forward switch the visible section without a full page reload; direct fragment URLs restore the corresponding section.

Accounts contains the accounts table and `New Account`. Categories contains category management and reimbursement-category configuration. Templates contains transaction-template management. Budget contains Paychecks Per Year, suggestion rounding, and template suggestion windows. Integrations is Owner-only and contains the transfer-description setting and Integration Secrets. Hide its navigation link from Auditors and resolve unauthorized direct fragments to Accounts. System groups existing controls in this order: Appearance (Theme), Backup & Downloads (shared export checkbox and credential notice beside Database Backup and Application Package), and Application (version, Release History, and Database Status). Keep the two Settings checkboxes compact and left-aligned like Dashboard Hide zeros; use no nested tabs or new pages. `Log Out` appears above Accounts and ends only the current browser session. Auditor views show configuration values while hiding and disabling database-writing controls, credential controls, and downloads containing the database; Theme, Release History, and application-only downloads remain available. Keep account, category, and template creation and editing in their existing dialogs. The sign-in and Owner-configuration pages use the same browser-local Light, Dark, or System theme preference as the application. Apply saved Light/Dark preferences before loading the stylesheet; System, missing/invalid preferences, and unavailable browser storage fall back to the operating system theme.

Show only the active section using normal accessible hidden-state behavior so inactive controls cannot receive focus. Preserve the current fragment after saves, edits, deletes, dialog cancellation, and reloads. On narrow screens, keep the section navigation compact and horizontally contained without causing page-level overflow. Wrap the full-size links onto additional lines as needed; do not truncate, clip, shrink, or horizontally scroll them.

Allow each table to use normal page scrolling within its own section. Do not add bounded vertical table scrolling, pagination, search, virtualization, a settings height preference, extra Settings routes, or a separate mobile Settings application.

**Configuration tables.**

Categories and Transaction Templates use `Sort Order` as their leftmost table column and in their editors. Accounts have no user-managed Sort Order control or column; preserve the stored Account `displayOrder` field for compatibility, as specified in Alphabetical Account Groups.

Display the class-specific Account tables and the read-only System Accounts section specified in Alphabetical Account Groups and External System Account. Account names retain archived labeling; Edit provides configuration and reconciliation.

Remove the `Reimbursement` column from the Settings Categories table. Keep `Offer reimbursement shortcut` visible and editable only inside the category create and edit dialog. Preserve all stored values and reimbursement behavior; do not add a replacement icon, badge, summary, or hover text to the table.

Display the remaining category table columns in this order:

```text
Sort Order | Name | Parent | Reporting Role | Status | Actions
```

Keep Transaction Templates' existing `Sort Order` leftmost column and terminology unchanged.

## Responsive Behavior

The application must work well on both desktop and phone.

The data and workflows remain the same.

Desktop may use wider tables.

Mobile may turn table rows into compact stacked records where necessary.

Do not create a separate mobile feature set.

Transaction entry must remain comfortable on a phone.

Amount fields should invoke an appropriate numeric keyboard where supported.

On mobile, preserve the same compact spreadsheet columns used on desktop: Category, Amount, Percentage, and Per Day. The expense table may scroll horizontally inside its own container on narrow screens. It must not make the entire page overflow horizontally or stack each category into multiple lines. Preserve Expense-rollup child indentation.

## User Accounts and Hosting Assumptions

Each usable ledger requires exactly one Owner and permits multiple read-only Auditors. A User has exactly one ledger membership in this release. Ledger access and schema isolation follow Ledger Identity, Isolation, and Audit History. The Owner has normal full access. The Auditor may view application pages and use existing nonpersistent filters, sorting, planning views, projections, and browser Theme controls, but cannot persist financial or ledger-configuration changes, retrieve integration credentials, review proposals, or download backups or packages containing the database. Python rejects direct restricted requests as well as hiding the controls. A persistent full-width `Read-only Auditor access` banner precedes all page-specific banners.

User management is command-line only. `startWindows.bat manage-users`, `./startLinux.sh manage-users`, or `python app.py manage-users` opens a numbered menu to list/manage Users, create an Owner and ledger, create an Auditor for an explicitly selected ledger, or exit. List usernames only in case-insensitive alphabetical pages of 20, with stable User ID tie breaking, Next/Previous, partial-username search, and change/clear search. Ledger choices also use pages of 20 and search across ledger name and Owner username. User details show username and Ledger | Role | Access. Rename and password reset target one stable User identity, revalidated in its sole selected membership; renaming never changes the ledger name or UUID. Username uniqueness is installation-wide, including Users with disabled access. Usernames are trimmed, contain 1–64 characters without control characters, and are unique case-insensitively; spelling is preserved. Passwords contain 8–128 characters, support spaces and ordinary Unicode, and are never trimmed, truncated, echoed, or displayed. Creation and resets require protected password confirmation. A password reset invalidates all sessions for only that User.

Owner creation atomically provisions a new login, UUID Ledger named `<username>’s Finances`, Owner membership, External System Account, and audit records. Other ledger settings use the same absent-setting defaults as fresh initialization; no personal configuration or records are copied. Initial Owner creation claims the existing unowned initial ledger and preserves any records. Multiple-ledger startup does not require an implicit management selection. Auditor creation always creates a new User in the explicitly selected ledger and requires its Owner to exist. Failed writes or audit recording leave no partial provisioning.

Auditor ledger access may be disabled or enabled in CLI User details with confirmation. Retain the User, membership, role, password, username reservation, and history. Disabled access blocks sign-in and subsequent protected requests immediately; both status changes invalidate existing sessions, so re-enabling requires fresh sign-in. Owner membership cannot be disabled. Existing memberships migrate to Enabled through explicit schema version 23 under the verified-backup upgrade path, preserving all existing identities and financial/audit history. No User or membership deletion is offered.

Startup binds and starts the existing development server before offering command-line Owner setup and optional Auditor creation. Protected terminal input stays on the main thread while the server serves requests independently. Protected pages and APIs expose only concise command-line configuration instructions until an Owner exists, including while a setup prompt waits for input. Saving the Owner allows sign-in on browser reload without a server restart, even while optional Auditor setup remains open. Declining, interrupted input, or unavailable input leaves the server running. A created Owner remains valid if optional Auditor setup is skipped or fails. Setup is offered again at the next startup when needed. A port-binding failure stops startup before offering setup. After setup, Ctrl+C stops the server and closes its listening socket. Financial records carry the explicit ledger identity described above. Browser password changes, recovery flows, invitations, and additional roles remain unsupported.

Unauthenticated browsers see a plain Sign In form with a generic invalid-credentials message. A login creates a concurrent-device-capable session with a fixed 168-hour lifetime that activity cannot extend. Logout ends that browser's session; password reset invalidates all sessions for that user. Server restart discards sessions. Sessions use opaque process-local tokens and database user invalidation versions. Passwords use salted slow scrypt hashing. Session cookies are HttpOnly and SameSite=Lax, with Secure on HTTPS. State-changing browser requests require CSRF protection; login replaces previous session state and uses concurrency-safe in-memory per-IP and normalized-username throttling. Five consecutive invalid username attempts trigger a five-minute cooldown. Successful sign-in resets failures; expiry clears cooldown and count; blocked attempts do not extend it. Generic responses apply to existing and nonexistent usernames. Old counters expire through bounded cleanup. This temporary cooldown does not administratively disable a User. Python authorizes each protected route and API. Browser routes reject Bearer integration headers, with a case-insensitive authentication scheme. Non-Bearer Authorization headers forwarded by a reverse proxy are ignored for application authentication; they do not grant access or replace the browser session, role, or CSRF requirements.

Successful sign-in stores server time in a nullable UTC `lastLoginAt` User field. Existing and new Users have no value until their first successful sign-in after this feature; never reconstruct earlier logins. Failed attempts, cooldown responses, ordinary activity, and logout leave it unchanged. Portable downloads preserve it. CLI display and administrative login enable/disable controls remain future scope. `python app.py --version` prints the central version without opening a database.

The deployment is assumed to be on a trusted self hosted environment. Authentication does not make the built-in server suitable for direct public internet exposure. Preserve existing HTTP and HTTPS behavior; remote integration access requires HTTPS through the installation's deployment boundary. Certificate management, HTTPS enforcement, trusted-proxy configuration, and new hosting architecture are not application features.

The built-in Flask server binds to `0.0.0.0` by default so another device on the same trusted LAN can connect using the host computer's LAN IP and application port. Do not hardcode a particular LAN IP.

Do not design the application as though it will be directly exposed to the public internet without an external access control layer.

Repository-root `startWindows.bat` and executable `startLinux.sh` launch from their own directory, find Python, create `.venv` when missing, install `requirements.txt`, and invoke the environment Python directly. Retain documented direct Python instructions.

Repository-root `packageWindows.bat` and executable `packageLinux.sh` launch from their own directory, find Python similarly to the startup scripts, pass arguments unchanged, and invoke one shared standard-library Python packaging implementation. Both support `--help`, `--output PATH`, and `--no-db`; relative output paths resolve from the repository root, while the default destination is repository-root `packages`. Database inclusion remains the default.

### Progressive Web App Metadata

The application provides public `/pwa.webmanifest`, `/theme.js`, and only the four approved PNG paths beneath `/assets/`, readable with GET and HEAD before sign-in or Owner setup. They expose no financial or credential data and retain existing indexing restrictions. Name, short name, and iOS home-screen title are `No Part Finance`; description is `A simple, self-hosted personal finance app for tracking accounts, transactions, and spending.` Manifest identity, start URL, and scope are `/`, with standalone display and no hard-coded deployment domain. Launch preserves existing Dashboard, Sign In, and Owner-configuration behavior. HTTPS at the domain root remains the reverse proxy's responsibility.

The manifest background and fallback theme color are `#202124`. Shared pre-stylesheet theme initialization follows the existing saved Light, Dark, or System preference on application and authentication pages, updating supported browser/system theme color to `#FFFFFF` for Light or `#202124` for Dark, including System changes and unavailable browser storage. Launch colors are platform hints, not a guarantee about every operating-system launch screen.

The exact icon paths are `assets/icon-192.png` (192 × 192 standard), `assets/icon-512.png` (512 × 512 standard), `assets/icon-maskable-512.png` (512 × 512 maskable), and `assets/apple-touch-icon.png` (180 × 180 iOS). The manifest and touch-icon metadata reference these paths. Supplied artwork uses an opaque dark green (`#123D2C`) background and white bold sans-serif lettering, without baked-in rounded corners. The 180px and 192px icons use `NPF`; the 512px icons use `No Part` above a larger `Finance`. The maskable export keeps essential artwork inside the centered 80%-diameter safe circle. Missing files return 404 without substitutes. Portable packages include manifest, shared theme script, asset source/export instructions, and only the approved icon exports when present; missing artwork does not prevent packaging. Icon dimensions and maskable composition are verified. The user confirmed successful iOS and browser installation on 2026-10-18. Browser and operating-system versions were not supplied; this confirmation does not imply verification on every platform.

Use native browser installation controls. There is no service worker, offline snapshot, financial cache, write queue, automatic replay, custom install prompt, push notification, or background sync. The server and network connection remain required; disconnected operations use ordinary browser/network failure behavior and never claim a successful save without the existing authoritative response. Installation does not change permissions, sessions, CSRF, or ledger behavior.

### Portable Startup Dependency Check

Update both `startWindows.bat` and `startLinux.sh` so that, after locating or creating `.venv`, they verify that the environment's Python can run `-m pip --version` before installing requirements. When pip is missing, attempt the standard-library repair through `-m ensurepip --upgrade`, then verify pip again. If it remains unavailable, stop with a concise platform-appropriate explanation that the installed Python lacks virtual-environment or pip support and must be repaired or installed with its venv component.

Preserve existing Python discovery on both platforms, reuse healthy existing environments, and continue installing requirements through the environment Python. Do not install packages globally, invoke system package managers, require administrator or root permissions, or delete an existing environment automatically.

### Fresh Installation and First Use

README.md provides one concise installation path using the existing public application-only ZIP, whose filename contains `-no-db-`. Users extract all files into a new writable folder, install Python (3.12 is the documented verification baseline), and open the appropriate Windows or Linux start script. Normal startup needs no manual dependency commands. Document Python discovery, virtual-environment/pip prerequisites, initial dependency-download internet access, and actionable installation-failure guidance without deleting existing environments or databases. Keep direct Python commands as an alternative. Distinguish application-only downloads from private database-inclusive packages containing financial data and credentials; database inclusion remains the private packaging default.

Fresh startup creates the database at the configured path, with no default credentials, user-owned Accounts, Categories, or financial history. The existing External System Account is not personal financial configuration. Command-line setup explains required Owner access, optional read-only Auditor access, hidden password typing, the unchanged 8–128 character password requirement, and password confirmation. Skipped or interrupted setup explains whether an Owner is still missing or the successfully saved Owner can sign in, and provides existing command-line retry instructions. Protected password input remains required. Exiting user management does not start the server; normal startup tells users to open `http://127.0.0.1:5000` once the server is running and keep its terminal open. Startup asks users to complete Owner setup and reload only when no Owner is configured; otherwise it directs them to sign in.

The signed-in Dashboard gives contextual guidance through existing `Settings — Accounts` and `Settings — Categories` links. A missing active ordinary-entry Account prompts Account configuration; External and archived Accounts do not count. A missing active selectable Category prompts Category configuration; archived Categories and Expense rollups do not count. Use the same authoritative catalog metadata and eligibility as existing ordinary-entry selectors. One eligible Account and one eligible Category are sufficient; neither both Reporting Roles nor optional configuration become additional prerequisites.

Settings Account and Category empty states explain creation or activation through existing controls. Account creation describes the starting balance before recorded transactions and its existing Cash versus friendly Credit convention; opening balances continue to use offsets, create no transaction, and affect no Income or Expense totals. Category guidance explains Expense and Income Reporting Roles, fictional user-owned example labels, standalone Parent None, and the existing role lock after transactions. Ordinary-entry guidance explains positive Expense and Income amounts, negative Expense refunds and Income reversals, and optional Template None. Missing prerequisites in the New Transaction dialog link to the existing Settings sections and explain that navigation discards the unsaved form; historical editing retains its existing archived-reference behavior.

Recent Transactions shows `No recent transactions` when empty and ordinary-entry guidance when configuration is usable. Auditor guidance asks the Owner to configure missing Accounts or Categories, without exposing write actions. Credit Accounts, Templates, reimbursement, Budget, and Simulator remain optional. First-use guidance introduces no wizard, seeded configuration, stored progress, onboarding route, financial calculation, or new persistence behavior.

### Everyday Operation and Manual Updates

README.md documents normal startup through the existing Windows or Linux launcher, host-computer access at `http://127.0.0.1:5000`, and trusted-LAN access using the host's LAN IP. Users keep the server terminal open, stop with Ctrl+C, and restart through the same launcher. Closing the browser does not stop the server. Saved users and financial state persist in SQLite; browser sessions do not survive restart and users sign in again.

The default database is `finance.db` beside `app.py`. `FINANCE_DATABASE_PATH` selects an alternative path and must be supplied consistently for startup and user management. Relative overrides resolve from the launcher's directory, or the current working directory for direct Python invocation. Normal startup logs the configured database path. A missing selected file creates a new database, so operation and recovery instructions require checking the path rather than continuing unexpected Owner setup.

Manual updates use an independent manual database backup, retained previous application files, and a separately extracted application-only ZIP. With the server stopped, users replace only the packaged application files in the existing installation, preserving the database, environment, backups, and local configuration. Environment overrides and deployment configuration are not packaged; deliberate launcher customizations must be reapplied after replacement. The normal launcher installs requirements and startup uses the shared schema-upgrade path. Users check startup errors, the configured path, application version, balances, and transaction history after signing in again. Installing older application files does not downgrade SQLite, and a migration backup does not guarantee compatibility with an older application.

The documented serving arrangement remains host-computer or trusted-LAN use of Flask's built-in development server. Authentication does not authorize direct public internet exposure. The supported public access-control and production-serving arrangement, including whether separate production-server work is required before public release, remains unresolved; these instructions add no hosting infrastructure.

### Disallow Search Indexing

Add a public `/robots.txt` route that supports normal `GET` and `HEAD`, requires no authentication, exposes no application or financial data, returns `text/plain` with the appropriate UTF-8 content type, and contains exactly the crawler-wide exclusion:

```text
User-agent: *
Disallow: /
```

Add the HTTP response header `X-Robots-Tag: noindex, nofollow, noarchive` to every application HTML response, including Sign In, unconfigured-installation, normal application, error, and other maintained HTML pages. Add `<meta name="robots" content="noindex, nofollow, noarchive">` to the shared HTML document shell as a presentation fallback. Do not publish or reference a sitemap.

Do not inspect User-Agent strings, attempt to classify robotic behavior, block requests heuristically, return alternate application content to suspected bots, or add a crawler denylist. Unknown routes retain the normal safe 404 behavior. Existing APIs and non-HTML asset behavior remain unchanged except that `/robots.txt` is publicly readable.

Crawler directives are not authentication or access control, and compliant indexing cannot be assumed. Do not present this work as protection for financial data or use it to weaken the approved User Accounts authentication scope.

## Database Backup

SQLite portability is an intentional design feature.

Provide a simple way to create or download a consistent backup of `finance.db`.

Use SQLite's backup capability rather than copying a database while a write is in progress.

Backups include the saved user password hashes, recoverable integration secrets, revocation state, and pending-submission state. Treat every database backup and package containing it as both financial data and credentials. Secrets are stored recoverably in SQLite without a separate encryption key. No complicated restore wizard is required.

### Ledger-Scoped Portable Web Downloads

`GET /api/backup` and database-inclusive `GET /api/package` are Owner-only and share one export implementation. Download Backup returns only the authorized ledger database; Download with database adds application files for independent self-hosting. Include its Ledger identity/name, financial records/settings, current Owner and all Auditors, including disabled memberships, with membership status and password hashes/authentication versions/last-login metadata, integration values and states, Pending submissions, and audit history. Exclude other Ledgers, Users, memberships and financial/audit content, active sessions, installation paths, and deployment secrets. Refuse ambiguous/shared membership credential exports until their future policy is designed.

Take a consistent SQLite backup into private temporary staging, then populate a fresh deliverable database using only authorized records. Never redact and deliver the whole source database, since deleted pages can retain unrelated information. Verify integrity, foreign keys, row preservation, and the authorized ledger/User set. Failures deliver no partial file; clean up temporary snapshots/exports after success or failure and never mutate the source. Command-line packaging and pre-migration backups remain complete-installation backups.

A shared native checkbox, `Disable integration keys in downloaded copy`, defaults unchecked and applies to both database-inclusive web actions through the Boolean `disableIntegrationKeys` query option. Default exports preserve key values and Enabled/Disabled/Revoked state. Selecting it changes only Enabled keys to Disabled in the exported database; already Disabled and Revoked keys stay blocked, and live states and audit history are unchanged. Reject unsupported download options and malformed Boolean values.

Show a clear notice that downloads contain password hashes and integration keys, must be stored securely, and enabled keys will also work on a running copy unless disabled there. Preserve existing passwords; everyone signs in again on the independent copy. Both single-ledger and centrally hosted forms use the same application/schema and configured deployment boundary. No merge/import wizard, cross-instance synchronization, new restore UI, encryption, or automatic secret rotation is introduced. Application-only downloads remain available to Owner and Auditor and contain no database or credentials.

### Manual Backup and Recovery

The Owner downloads a consistent SQLite snapshot through Settings System's Database Backup action (`GET /api/backup`), named `finance-backup-YYYY-MM-DD.db`, while the application runs. The download uses SQLite's backup API rather than a live file copy. Users keep the completed download outside the installation folder. An Auditor cannot download database backups.

Manual restoration stops the application first and preserves the existing database under a separate name or location before replacement. Any associated SQLite sidecars are preserved with the old database rather than left beside the replacement. Users keep the backup unchanged and copy it to the exact configured database path and filename, which may differ from the download filename or the default `finance.db`. Application files and environment configuration remain in place. Normal startup applies supported older-schema upgrades and refuses newer unsupported schemas.

After restart, users sign in with credentials saved in the snapshot and check the configured path, Database Status, familiar balances, history, and Owner-only proposal and credential state. Restoration replaces saved state with the snapshot: later financial changes and password resets are undone; saved proposal corrections and review versions are recovered. An older snapshot can reactivate secrets revoked after it was taken, so the Owner must revoke those again before resuming integration use. Browser sessions do not survive restart. Recovery failures leave the installation stopped while users preserve files and errors for troubleshooting; README.md documents upgrade and recovery failure handling.

Manual downloads are distinct from pre-migration backups and portable packages. A database-inclusive package contains a consistent snapshot whose extracted `finance.db` can be restored through the same stopped-application procedure. An application-only package contains no database or credentials and cannot recover saved state. Database-only recovery does not replace application files. These mechanisms do not schedule automatic backups or provide a restore wizard.

### Portable application packages

One portable ZIP snapshot contains the maintained source, HTML, CSS, JavaScript, requirements, Windows and Linux startup and packaging scripts, project documentation, tests, and a consistent `finance.db`, all beneath one top-level `No-Part-Finance` directory. Use an explicit inclusion list rather than recursively including arbitrary repository contents. Exclude version-control and virtual-environment data, caches, existing packages and backups, SQLite sidecar files, retired legacy CSV files, temporary and diff artifacts, private keys, and secret configuration.

Command-line packaging selects `finance.db` beside `packageApplication.py` and does not use `FINANCE_DATABASE_PATH`. Settings' database-inclusive download uses the application's configured database path; use that download when packaging an installation with a database override.

Archive names contain the authoritative application version and local creation timestamp. Never overwrite an archive; use a deterministic suffix for a same-second collision. Create missing output directories and fail clearly when the selected directory is not writable.

Before packaging, run `PRAGMA integrity_check` against the source database opened without writes. Create a temporary consistent database with SQLite's backup API, then verify its `integrity_check` and `foreign_key_check` before adding it as `finance.db`. An absent, unreadable, or invalid source fails without publishing a partial archive. Never modify or directly copy the real database, and clean up only packaging-owned temporary staging data.

Settings includes a compact `Application Package` section with separate `Download with database` and `Download application only` actions and a clear explanation of their contents. `GET /api/package` creates the complete package with a verified database backup. `GET /api/package-no-db` creates the same maintained application package without `finance.db` and does not require a source database. Both browser downloads use private temporary staging and return a non-cacheable `application/zip` attachment without retaining an archive under `packages`. Neither accepts an output-path HTTP parameter. Command-line packages retain their explicit/default output-directory behavior. A validation or packaging failure returns a concise non-success response and retains no partial archive.

Command-line success output identifies the archive path, application version, local timestamp, database inclusion state, archive size, the applicable financial-data message, and the matching generic download example. Help documents the archive behavior, `--output PATH`, `--no-db`, the sensitive-data warning, and Windows and Linux examples. A database-free filename includes `-no-db-` so its contents are unambiguous. Packages are not uploaded, encrypted, restored, updated, deleted, rotated, or scheduled.

Manual Backup and Recovery defines the restore procedure for packaged database snapshots. Application-only packages contain neither the database nor its credentials. Browser database backups and packages containing the database are Owner-only.

### Safe automatic upgrades

Use the shared `PRAGMA user_version` path. Before upgrading an older compatible database, run `PRAGMA integrity_check` and create a consistent SQLite backup in a sibling `backups` directory with source version, target version, and timestamp in its unique filename. Run only required migrations in order, then verify `foreign_key_check`, `integrity_check`, and final version. If backup, migration, or verification fails, stop startup clearly, preserve the backup, and do not continue with a partial upgrade. Refuse a database newer than the application schema. New and current databases receive no migration backup.

A pre-upgrade backup becomes eligible for recovery only after SQLite backup completion, integrity verification, and verification of its source schema version and required Account schema. Creation, completion, or verification failure stops startup before migrations and leaves the original database unchanged; an incomplete, empty, or invalid candidate is never restored. Migration or post-migration verification failure restores the verified snapshot, retains it, and stops startup. If recovery also fails, startup stays stopped, retains the verified snapshot for manual recovery, and reports both the upgrade and recovery errors without claiming restoration succeeded. Keep an independent manual backup before updates.

### System Database Status

Within Settings System’s Application group, show a compact read-only `Database Status` area with total Transactions, total Accounts, database Schema version, and the authorized ledger's latest audited change in server-local time (`Not recorded` when absent). Transactions are authoritative ledger rows; clarify that each transfer counts as two rows. Accounts include archived accounts. Refresh the values when Settings loads; do not poll, add stored state, or alter the database.

## Pending Entries and Agent JSON Integration

### Credentials and permissions

Owner Settings Integrations provides `Integration Secrets`: a required label, generated strong secret, and compact `Label | Masked Secret | Status | Actions` table. A permanent creation row sits below saved secrets in the same table: a compact required Label input, blank Masked Secret cell, and Create Secret action. The input has an accessible label, remains bounded to its cell on narrow screens, and uses the table's normal horizontal overflow. Successful creation clears the input and refreshes saved rows above it; validation failures retain the entered label and show the existing form error. Multiple secrets are independently revocable without a fixed product limit. Normal presentation masks each secret; the Owner can Edit Label, Reveal, Copy, Disable/Enable, and Revoke it later. Status explicitly reads Enabled, Disabled, or Revoked. New keys start Enabled. Disable/Enable reversibly blocks/restores authentication using the same secret; Revoke remains permanent and a revoked key cannot be enabled or disabled. Existing inactive credentials migrate as Revoked, never as re-enableable Disabled. State changes are Owner-only, ledger-scoped, CSRF-protected, persistent, and audited without revealing secrets; they neither decide proposals nor create financial activity. Labels are trimmed, required text of at most 128 characters. Editing the label updates its displayed attribution on existing linked proposals without changing the secret, revocation state, submission identity, or financial records; revoked integrations may also be relabeled. Secret values are persistently recoverable in SQLite, never included in financial JSON, URLs, documentation examples, or logs, and never disclosed to an Auditor or integration caller.

`Authorization: Bearer <secret>` grants financial JSON reads, submission of ordinary transaction or transfer/payment proposals, reading and updating every current Pending item, and status reads for that secret's own submissions. Revocation immediately blocks subsequent requests and persists across restart, while existing proposals retain their submitting label. Bearer credentials cannot log in as a browser user, manage credentials, decide or delete proposals, create Manual entries, move ledger events to Pending, retrieve another source’s terminal submission history, or invoke existing ledger/configuration write APIs. A configured Owner is required before integration access is enabled.

### Public contract and financial reads

`/docs` and `/openapi.json` are public and contain only fictional examples. They document the exact operations, fields, filters, pagination, dates, money signs, status, errors, and setup. An OpenAPI document does not promise automatic compatibility with a particular AI vendor.

The read endpoints are `/api/integration/read/accounts`, `/categories`, `/templates`, `/settings`, `/report-periods`, `/dashboard`, `/transactions`, `/budget`, and `/simulator`, using the same `/api/integration/read` prefix. Catalogues include stable IDs, classes or Reporting Roles, hierarchy, active/archive state, and eligibility. Account balances are authoritative integer cents; External has no exposed internal financial balance. Templates include their presets and current eligibility. Reporting, Budget, and Simulator reuse Python's existing calculations, with current balances distinct from projected balances and saved Simulator execution state distinct from ledger activity. Integration and Auditor reads never perform Simulator missing-link repair writes.

Dashboard Account records include Boolean ordinaryEligible and transferEligible from the same authoritative rules as the Account catalogue. Dashboard recent and pinned events include Boolean combinedTransfer from shared grouping: valid combined transfers are true, ordinary events and visible invalid linked rows are false. External remains excluded from Dashboard financial Account lists, and these descriptive fields never grant write permission.

Catalogue, settings, and report-period reads accept no filters. Dashboard accepts `period`, `startDate`, and `endDate`; Budget accepts `date`; Simulator accepts `plan`. Transaction history accepts `period`, `startDate`, `endDate`, `type`, `accountId`, `categoryId`, `description`, `sort`, `direction`, `amountMode`, `amount`, `amountMin`, `amountMax`, and `includeSummary`, plus `limit` and `offset`. Unsupported filters are rejected. History defaults to 100 results with a maximum of 500 and a nonnegative offset; its envelope includes total and nextOffset, and optional summary covers all matching events. Catalogue and planning snapshots are complete and unpaginated.

History accountId and categoryId filters are optional positive integers using the existing integer query syntax. Omitted IDs apply no filter; supplied blank, zero, negative, fractional, or nonnumeric IDs return the existing JSON validation error with HTTP 400. A valid positive ID without matches returns the normal HTTP 200 empty history envelope, total zero, null nextOffset, and zero optional summary. Invalid IDs never silently clear or broaden results.

History includes archived records and groups valid transfers as one event with explicit source and destination IDs; an Account filter matches either side. Grouping requires exactly two zero-sum, opposite-signed rows with matching date and description, distinct Accounts, and no Category. Invalid linked transfers remain visible as signed ledger rows. Reporting signs, date boundaries, and External exclusions remain authoritative under the existing rules.

### Integration Transfer Default Descriptions

Owner Settings Integrations includes `Always use default descriptions for integration transfers`, a native checkbox and Save action. It defaults to off, persists in SQLite, and may be changed only by the Owner through session authentication and CSRF protection. Its existing helper text reads: `Automatically uses default descriptions for transfers submitted or updated by integrations. Your own entries and Pending edits keep the description you enter. Transfers still require approval.`

When enabled, Python replaces the Description on each new integration transfer submission and each integration Pending-transfer update with the existing destination-based `Use default description` naming rule: Cash/External uses `Transfer to <Destination Account Name>`; Credit uses the shared Card/Payment convention. It silently normalizes the saved review content before Pending displays it, while preserving the immutable original submission for retry identity. Required request shape and types, Account eligibility, dates, amounts, stale-review protection, and explicit Owner approval remain enforced. Integration updates apply this rule to any Pending transfer they update, including Manual entries.

Changing the setting never rewrites existing Pending or historical records. Ordinary transactions, Manual creation (including Save to Pending), Owner edits and approval retain their normal description behavior and preserve the entered description. Save remains enabled for Owner entries and edits. Every Pending transfer retains its existing `Use default description` button and editable draft behavior. Automatic normalization writes no ledger rows and changes no amounts, signs, balances, or reports.

### Integration proposals and sign conventions

Submission and Pending-update request bodies retain a 1 MiB (1,048,576-byte) maximum. Authenticated oversized bodies return HTTP 413 with the documented JSON Error shape and a concise size-limit message, with no proposal or ledger writes. Bodies exactly at the limit undergo normal parsing and validation. Existing authentication and Owner-configuration failure precedence is preserved.

An ordinary `POST /api/integration/submissions` request contains exactly `submissionId`, `date`, `accountId`, `description`, `categoryId`, and `amount`. The caller-generated ID is a 1–128 character string, IDs are positive integers, Date is an explicit ISO calendar date, and Amount is a user-facing money string parsed by the shared Python USD parser. Missing, malformed, or unsupported fields fail without a partial write. Zero, future dates, leap days, System Accounts, and eligibility follow existing ordinary-transaction rules; no agent-local date is guessed.

Agents must respect the Category's explicit Reporting Role and the entered-amount convention rather than copying a ledger sign or inferring meaning from a name:

| Reporting Role and event | Entered amount | Ledger amount in cents |
| --- | --- | --- |
| Expense purchase | `50.00` | `-5000` |
| Expense refund | `-20.00` | `2000` |
| Income receipt | `1500.00` | `150000` |
| Income reversal | `-50.00` | `-5000` |

These are ordinary ledger signs even for credit Accounts; friendly debt balance wording and projections are separate display concepts. Documentation guides correct proposals, but the human Owner retains final approval. Do not silently remove valid fields or reverse an amount to resolve uncertainty. Malformed submissions are rejected; malformed Owner corrections remain visible with validation feedback and cannot be approved until corrected.

A transfer/payment submission uses exactly `submissionId`, `type: "transfer"`, `date`, `fromAccountId`, `toAccountId`, `description`, and positive `amount`. It has no Category or ordinary `accountId`. Kind is explicit and fixed; never infer a transfer from an Account name or Description or convert a proposal between kinds during correction. Python applies existing distinct active Account, Cash/Credit/Loan/External eligibility, date, description, and strictly positive integer-cent transfer rules. Credit payments and overpayments need no special type, payoff calculation, balance default, or insufficient-funds restriction.

Submitting creates only Pending state: no ledger row, balance/report change, or Budget/Simulator mutation. Submission identity is scoped to the secret and persists across restart and terminal decisions. Identical original content returns the same submission and its current status; different content with the same ID returns conflict. Owner and integration corrections never replace original retry content. Saved corrections are separate review content, and a monotonically increasing review version prevents a stale view from overwriting, approving, or rejecting a row saved elsewhere. A stale save or decision returns conflict rather than replacing newer corrections. `GET /api/integration/submissions` returns only that secret's submissions, optionally filtered by `submissionId` or `status` (Pending, Approved, Rejected), with the same limit/offset bounds. Status responses retain ordinary identifiers and additionally identify `kind`, `transferId`, and `reviewVersion`. Approved ordinary proposals return `transactionId`; its deletion sets that identifier to null. Approved transfers return the group `transferId` and null ordinary `transactionId`. Pending/Rejected transfers have both identifiers null. A later deletion retains an Approved transfer's group identifier even though a lookup may find no ledger rows. Terminal decisions and deleted ledger events never reset original retry identity or recreate ledger activity. Callers cannot reject, approve, delete, or update terminal proposals.

### Reading and updating Pending items

Authenticated `GET /api/integration/pending` returns every current Pending ordinary transaction and transfer, including submissions from any integration, Manual entries, moved ledger events, and saved Owner corrections. It reuses the Owner review response: `count`, `transactionCount`, `transferCount`, and `rows` with current editable fields, stable proposal ID, fixed kind, source/label, Pin, reviewVersion, and original/current Reporting Role and validation metadata. Each transfer counts once. Empty queues return zero counts and an empty array. This complete unpaginated snapshot accepts no query parameters, performs no writes, and exposes no terminal history, credentials, immutable original payloads, or private source snapshots. The existing caller-specific submission-status endpoint remains separately scoped.

Authenticated `PUT /api/integration/submissions/{proposalId}` replaces one Pending item’s complete editable fields regardless of source or prior Owner edits. Ordinary bodies contain exactly `date`, `accountId`, `description`, `categoryId`, `amount`, `reviewedRole`, and `reviewVersion`. Transfer bodies contain exactly `date`, `fromAccountId`, `toAccountId`, `description`, positive `amount`, and `reviewVersion`. Use the stable proposal ID and latest version from the Pending read; ordinary reviewedRole must match the selected Category’s current authoritative Reporting Role. Query parameters, partial or bulk updates, kind/type/identity changes, Pin changes, and attribution or status fields are rejected.

Python reuses the Owner save validation and SQLite transaction. Every successful save, even unchanged content, increments reviewVersion and returns `{"reviewVersion": <new version>}`. The item remains Pending. Stale saves, stale Owner decisions, terminal targets, and changed reviewed Roles return 409; callers read the current Pending queue and reconcile instead of automatically substituting a newer version. Invalid fields return 400 and missing proposal IDs return the standard JSON error shape with 404. Authentication failures retain 401 and a missing Owner retains 503. Target existence/status and version checks happen inside the save transaction; validation or write failures leave all state unchanged.

API updates change only saved review content and reviewVersion. They preserve original POST/move retry identity, original Role, source and submitting integration, Pin, moved-event snapshots, Account offsets, ledger history, and Simulator state. They generate replacement descriptions only under the Integration Transfer Default Descriptions setting; they never infer signs, approve, reject, delete, recreate moved ledger events, or relink Simulator rows. Existing Owner approval still validates current fields and inserts ordinary or atomic zero-sum transfer activity; approval of a moved event creates new ledger IDs with old Simulator links left cleared. Existing Role-change warnings and explicit human sign review remain in force. Owner edits do not lock an item, and browser-local drafts remain available after a rejected stale save or decision.

### Manual entry

The Owner's existing New Transaction and New Transfer/Payment forms offer secondary `Save as Pending` alongside normal ledger Save. In New Transaction and New Transfer/Payment, the ledger submit action uses the existing green positive-action style and Save as Pending uses the shared Yellow style; Cancel remains neutral. Ledger Save also uses the green positive-action style in persisted transaction and transfer/payment editors. Date defaults to browser-local today, is editable, may be future-dated, and is preserved through approval. Template prefills remain editable and create no recurring or linked planning state. Ordinary entries retain their Pin setting for eventual approval. Python applies the same Reporting Role signs and eligibility as normal entry; transfers require positive Amount and have no Category.

Saving creates one durable proposal with the fixed source label `Manual`, closes the form on success, and gives concise existing success feedback. Validation failures preserve fields. Paired `Also log reimbursement` makes Save as Pending unavailable with an instruction to uncheck it; never omit a selected companion silently. This new-entry action is absent from persisted editors and Bulk Entry. It creates no ledger or Simulator execution link. Pending and Rejected entries affect no balances, Income, Expenses, reporting, Budget, or Simulator, and dates never trigger automatic approval or execution.

Manual creation is Owner/session/CSRF protected, with durable per-request retry identity preventing duplicate creation after repeated clicks or lost responses. Different requests are independent even if their financial fields match. Backend attribution cannot be supplied by an integration; Manual does not use a fake integration credential. Saved corrections, rejected records, and original retry content persist separately.

### Moving ledger events to Pending

Persisted categorized ordinary and valid transfer/payment editors provide `Move to Pending`. In both transaction and transfer/payment editors, Move to Pending uses the shared Yellow style beside the corresponding red Delete action. Both use natural button widths in a wrapping group separate from Cancel and the ledger Save action; the group is absent from new-entry dialogs. Uncommitted edits require Save or Cancel Changes first. Explicit confirmation explains immediate removal from balances and reports, Pending approval, rejection leaving the event outside the ledger, and linked Simulator behavior. Cancellation writes nothing. Only the signed-in Owner can move, with CSRF protection; Auditor and Bearer credentials cannot invoke it.

Capture the persisted Date, Account IDs, Description, entered Amount using current explicit Category Reporting Role, ordinary Category and Pin, and original event identity. A valid transfer captures its complete pair as one Manual proposal, with positive Amount and no Category. Verify exactly two opposite-signed zero-sum rows with matching Date/Description/group, distinct Accounts, and null Categories before moving. Refuse invalid or incomplete pairs and uncategorized imported Opening Balance/Balance Adjustment history. Preserve Account offsets and unrelated history.

Create the proposal, remove the source row or both transfer rows, and reset every Simulator row linked to either row in one SQLite transaction. Keep all Simulator rows and saved fields, including links across plans; clear ledger links and mark not executed. They contribute again under existing projection rules. Later approval does not automatically relink or mark them executed, since reviewed fields may have changed.

Compare the persisted source snapshot and event identity shown for confirmation before removing anything. A stale or changed source fails without partial writes. Durable move-request identity and the immutable original snapshot prevent duplicate proposals or deleting an unrelated event that reuses a numeric ledger ID after a lost response. Approval creates new ledger IDs and leaves old Simulator links cleared. Rejection never restores the original event. Initial Pending Date is the persisted Date; later approval uses the currently reviewed valid Date and fields.

Archived references remain captured by stable ID and visible for explicit correction before approval; never silently blank or remap valid fields. An integration proposal whose Approved ledger event is moved stays Approved with its original content and deleted-ledger identifier treatment. The new Manual item is independent, and integration retries cannot recreate the removed event.

### Human review and atomic decisions

One Owner-only `/pending` page contains plain `Transactions (count)` and `Transfers (count)` tables with shared Approve Selected, Reject Selected, and Approve All Pending controls. Both approval buttons use the existing green positive-action style; Reject Selected uses the existing red destructive-action style. Row Save, Cancel, and default-description buttons remain neutral. Ordinary rows retain Select, Integration/source label, Date, Account, Description, Category, entered Amount, and Actions. Transfer rows show Select, Integration/source label, Date, From Account, To Account, Description, positive Amount, and Actions. Keep detailed sign conventions in the public API documentation; the Pending page uses field labels and inline validation without a sign-convention notice, ledger-preview column, or combined dollar total. Manual attribution is fixed; revoked integrations retain their current Owner-editable labels.

Immediately below the Pending Transactions and Transfers tables and above the shared decision buttons, show two separate values, `Sum of Transactions` and `Sum of Transfers`, in the existing Simulator/Transactions summary style. Include every displayed Pending row regardless of selection, using the currently entered amounts including unsaved edits. Ordinary Expense purchases and Income add; negative Expense refunds and Income reversals subtract. Each transfer proposal contributes its positive entered Amount once. Never combine the two totals into a grand total or interpret them as ledger balances or reporting totals. Empty kinds show `$0.00`. Python parses and sums integer cents using the shared USD parser and returns exact display-ready values, including for addition/subtraction expressions and large sums. JavaScript renders returned values and ignores stale summary responses; it performs no money arithmetic. Keep the currently displayed totals in place while recalculating, then replace them when the latest result arrives. Do not show calculating messages or temporarily replace totals with placeholders during updates. A malformed amount makes that kind's entire total unavailable with concise correction guidance; never silently omit a row or display a partial sum. Other field errors remain subject to normal Pending validation and do not change the entered-amount meaning. Refresh totals after edits, cancellation, row saves, reload, approval, and rejection. Summary evaluation is Owner-only and CSRF protected, writes no SQLite state, and does not change approval validation or atomicity. Below the all-row values, add `Sum of Transactions (Selected)` and `Sum of Transfers (Selected)` in the same summary layout. These start at `$0.00` with no selection and update live on checking or unchecking any proposal. Use the same Python entered-amount calculation, positive once-per-proposal transfer sum, draft/cancel/save behavior, and invalid-amount handling, restricted to selected rows. Invalid unselected rows do not make selected totals unavailable. Clearing the selection immediately restores both selected values to `$0.00`; stale responses cannot overwrite a newer selection. Selection remains nonpersistent and never approves or saves an item. Keep all four summary values readable on narrow screens while retaining contained table scrolling.

Changing To Account in a Pending transfer explicitly replaces its editable Description using the existing transfer-form destination default: the shared Credit payment naming convention or `Transfer to <Destination Account Name>` for Cash/External. Changing From Account alone retains Description. Loading or refreshing review never regenerates saved descriptions, and unavailable destination IDs retain the current text. This changes only the local reviewed draft until an explicit Save Row or approval; Cancel Changes restores the saved Account selections and Description. Each Pending transfer row also provides a native `Use default description` button using the same currently selected destination and naming rule. It changes only that row’s Description draft, remains editable, and uses the normal unsaved-row validation, Save Row, and Cancel Changes behavior. It does not save or approve automatically, change other fields or rows, or rewrite the original integration submission. The button remains visible and is disabled when the Description exactly matches the current destination default or the destination is unavailable. Disabled buttons use theme-aware muted text, background, and border styling. The button always reads `Use default description`, with the same accessible label in enabled and disabled states. Its state updates on loading, editing, saving, and cancelling the row. An unavailable destination leaves the existing text intact. There is no reset-all action.

Both kinds support Save Row, Cancel Changes, Enter-to-save, saved corrections after refresh/restart, inline validation, and stale-review protection. In Actions, Save Row and Cancel Changes stay together before Use default description where applicable. Actions wrap between that pair and the default-description button; the tables contain horizontal overflow on narrow screens like the Simulator worksheet. Unsaved rows use the existing theme-aware Yellow highlight, cleared on save/cancel/restoring saved values. Save writes no ledger activity and leaves the proposal Pending. Cancel restores saved values without writing. Successful row saves, approvals, and rejections use the existing reusable success toast instead of persistent text below the decision buttons. Approving exactly one proposal, including through Approve All, reuses the ledger-save toast with freshly fetched authoritative balances: the selected Account for a transaction or both selected Accounts for a transfer, excluding External and retaining friendly Credit balance wording. Multiple approvals retain the count-only toast; saved Pending edits and rejections show no balances. Validation and failure messages remain inline at the row or shared form; typing, cancellation, and unsuccessful requests do not show a success toast. Saving or deciding another row retains unrelated drafts. Explicit selected approval may use valid current edits without a separate save.

Approve All Pending requires every row saved and valid and confirms the total count plus transaction/transfer breakdown. Cancel writes nothing. Within one SQLite transaction, the backend verifies the complete reviewed queue, fields, revisions, eligibility, and current financial meaning. New arrivals, stale or changed rows, invalid fields, or a write failure reject the entire batch. Hide Approve All only for an empty queue. Use native labeled controls, safe text rendering, and contained scrolling on narrow screens. Leaving the page never approves.

The authoritative queue counts total proposals and separately Transactions/Transfers; each transfer counts once. Dashboard's Yellow banner below actions and Red alerts is headed `Pending review`, states the total and kind breakdown, and links `Review pending items` to the same page. Hide it only when the combined queue is empty. Transactions permanently shows `Review Pending (total)`, including zero. The button is enabled with the shared Yellow background and black text when the combined Pending queue is nonempty. When empty, it is disabled with theme-aware muted styling. Direct Owner access to `/pending` still displays the empty queue. Do not add polling or top-level navigation.

Python revalidates active Accounts and selectable Categories, preserves valid values and stable IDs, and flags unavailable references for explicit correction. Ordinary Reporting Role changes show the current role and ledger effect inline. Approval carries the previewed current role; a subsequent concurrent change returns conflict. Transfers retain their kind, current eligibility, positive Amount, direction, and reporting exclusions.

Approve a selected or complete mixed set in one SQLite transaction spanning all ordinary ledger inserts, each transfer's exactly two linked rows, and proposal transitions. Transfer source is negative cents and destination positive cents with a matching group/Date/Description, null Categories, and zero sum. Reuse shared Python rules without nested commits. Any validation, stale/concurrent decision, or write failure rolls back the whole batch. Repeated approval cannot duplicate activity. Ordinary approval preserves Pin. Rejecting a selection that includes a transfer requires a native confirmation showing the selected transaction/transfer counts and warning that rejection cannot be undone. Cancelling preserves the selection and drafts and sends no decision request. Rejection writes no ledger rows and retains durable identity. Pending/Rejected items have no financial effect; approved transfers remain excluded from Income/Expenses.

The shared schema initialization and explicit version-21 migration preserve financial and planning rows, users, credentials, prior proposal status, original/review content, review versions, stable IDs, and foreign-key links. Pending records store source, fixed kind, immutable retry content, separate saved review fields, and approved identifiers. Manual identities are separately scoped. Ledger event tokens protect stale move snapshots, and new generated ledger IDs do not reuse deleted IDs. Account pins default false. No balances, reporting totals, second ledger, or transfer table are stored. Bulk proposals, automatic approval, matching, planning/configuration integration writes, vendor connectors, workers, and a configurable permission matrix are excluded.

## Historical Legacy Data

The original production database was created from legacy CSV data. The completed one-time importer is retired and no longer shipped or supported. Raw legacy CSV files and SQLite databases are user data and must not be deleted, rewritten, or committed. Normal transaction entry and Bulk Entry are the supported data-entry workflows.

### Optional Fictional Demo Fixture

The repository includes a separate `finance.demo.db` fixture with AI-generated fictional activity from January through October 15, 2026, independent demo credentials, templates, Budget configuration, and Simulator plans. It is created in a fresh database using the shared schema and financial validation paths, rather than sanitizing copied production pages. No personal records, existing users or password hashes, integration secrets, or proposal payloads are copied into it. README screenshots use only this fictional fixture.

Demo use is explicit through the existing `FINANCE_DATABASE_PATH` override, preferably against a disposable working copy. Normal startup still selects `finance.db` and never seeds, replaces, or automatically selects demo content. The fictional fixture is the only database intentionally included in Git; private databases, sidecars, backups, packages, exports, and local agent artifacts remain excluded. Demo credentials are public and must not be used for personal financial data. The existing trusted self-hosted deployment boundary remains unchanged.

## Budget

Budget is an informational planning layer over the authoritative accounts, transactions, categories, templates, and reports. It never creates transactions automatically, tracks paid status, allocates money, or becomes a second ledger.

### Budget Planning: Sinking Funds and Monthly Recurring Plans

`Budget` appears in the primary navigation. Keep a visible `Settings` link on the No Part Finance title line, outside the primary-navigation row, and justify it to the right edge on desktop and narrow screens. When the primary navigation moves to its own line, Dashboard, Transactions, Budget, and Simulator remain unclipped below the title row.

Budget is informational planning layered over the existing financial system. Accounts, transactions, categories, templates, and reporting remain authoritative and independently managed. Budget references them by stable ID and may open the existing transaction workflow, but it never changes balances, creates transactions automatically, tracks paid status, matches transactions, allocates money, or creates a second ledger. Accounts, categories, and templates remain managed in Settings.

Settings includes a `Budget` section alongside Accounts, Categories, Templates, and System. It stores the positive whole-number `Paychecks Per Year` setting, optional suggestion rounding, and nonnegative template suggestion windows described below. Do not show a `Budget Settings` shortcut beside `New Recurring Budget Item`. The contextual setup message for a missing Paychecks Per Year value may link directly to `Settings#budget`. It does not add a last-paycheck date, payday interval, or paycheck calendar.

#### Sinking Fund Plans

Sinking Fund plans support Quarterly and Yearly expenses. An active plan references one eligible active Expense-role template and stores Frequency (`Quarterly` or `Yearly`), a required best-estimate Expected Date, and Active status. The template supplies its name, description, category, positive entered amount, and optional payment account. A missing payment account does not make an otherwise eligible template incomplete; a selected payment account must remain active. One template may have at most one active plan. An actively referenced template cannot be archived, deleted, or made incomplete until its plan is archived or removed.

Quarterly plans recur every three calendar months, and Yearly plans recur on the same month and day. For a day beyond a month's end, use that month's final calendar day; use February 28 for a February 29 Yearly plan in non-leap years. The template account is the expected payment account and may be a Credit Card; it is never a reserve account.

Every plan offers `+ Transaction`, which uses the referenced template but always defaults the actual transaction Date to browser-local today. Prefill every available template value; when the template has no payment account, leave the normal required Account selector blank for the user to choose. Every prefilled value remains editable. Saving creates only a normal transaction and never changes the plan or marks it paid. Variable recurring bills use a representative template amount that the user may change when recording the real transaction.

Use the Sinking Funds table columns `Next Date | Fund | Amount | Set Aside | Target on Selected Date | Actions`. Next Date is the upcoming occurrence calculated from the stored Expected Date schedule anchor, with Frequency shown beneath it as compact secondary text. Set Aside shows Monthly Contribution and Per-Paycheck Contribution as two readable lines. Payment Account and Category remain template details available through the existing plan/template edit flow rather than duplicate plan overrides, but are not table columns.

Default the table to Next Date ascending. Use sortable column headers following the Transactions-page interaction for every data column. Set Aside sorts by its primary Monthly Contribution value. Actions is not sortable, and Sinking Funds have no manual Sort Order setting or behavior. Row Actions contains only `+ Transaction` and `Edit`.

The shared Edit Budget Plan dialog has a destructive `Delete Budget Item` action at its bottom. It uses the normal permanent-deletion confirmation before deleting the plan. Do not show row-level Delete or the destructive action while creating a plan.

Calculate in integer cents:

```text
Quarterly Monthly Contribution = ceiling(Amount / 3)
Yearly Monthly Contribution = ceiling(Amount / 12)
Quarterly Per-Paycheck Contribution = ceiling((Amount * 4) / Paychecks Per Year)
Yearly Per-Paycheck Contribution = ceiling(Amount / Paychecks Per Year)
```

At the top of Sinking Funds, show global totals for `Sinking-fund reserve target on selected date`, `Monthly sinking-fund contributions`, and `Per-paycheck sinking-fund contributions`. Use the selected As of date for the reserve target, as specified in Sinking Fund Target Date; contribution totals are independent of that date. Do not group or compare totals against payment accounts because bills may be charged to Credit Cards while cash is held elsewhere.

Derive the reserve target from actual recurrence dates using the selected As of date and the formula in Sinking Fund Target Date.

The target reaches the full amount on the Expected Date and begins the next cycle on the following day. It assumes the bill was handled on its expected date; there is no payment matching, saved amount, or payment status.

#### Monthly Recurring Plans

The shared Budget plan form supports the `Monthly` Frequency. Monthly plans recur on the same calendar day each month, clamping a day beyond a month's end to that month's final calendar day. They retain the existing template, expected-date schedule anchor, active-status, and `+ Transaction` behavior. They do not have a manual Sort Order and do not require Paychecks Per Year.

Render Monthly Recurring Expenses and its subtotal as the first Budget planning section, above the complete Sinking Funds section and its totals. Use the compact table columns `Next Date | Expense | Amount | Actions`. Under Expense, show Payment Account and Category as compact secondary text when present. Default to Next Date ascending and retain sortable data-column headers; Actions is not sortable.

Monthly plans do not contribute to Sinking Fund reserve targets, monthly Sinking Fund contributions, or per-paycheck Sinking Fund contributions. They remain separate inputs to the combined Monthly Requirement. Their template suggestions follow Smart Template Suggestions and Budget Organization.

#### Flexible Monthly Planning

Budget includes a transparent Monthly Requirement summary with component subtotals in this order: Monthly Recurring Expenses, Flexible Budget Categories, Monthly Sinking-Fund Contributions, Planned Debt Payments, and Expected Monthly Income. Detailed areas use the fragment navigation specified in Smart Template Suggestions and Budget Organization.

Budget Settings includes optional positive-dollar `Round suggestions up to`, blank when disabled. Each active selectable Expense-role category may have one active explicit positive monthly target. Categories retain their Settings-defined order and rollups cannot have direct targets. The suggestion is the previous three complete calendar months' net Expense-role activity divided across all three months, including refunds and zero-spending months while excluding the current partial month. Negative suggestions display as `$0.00`; configured rounding always rounds upward to its increment. Suggestions never change saved targets, and targets never create transactions.

Expected Monthly Income plans reference complete active Income-role templates and recur monthly from their Expected Date. Their full positive template amounts subtract from Monthly Requirement. They retain the existing editable normal-transaction prefill behavior, including a blank required Account selector when the template has no account. They do not annualize rare or irregular income and never create or mark transactions automatically.

Planned Debt Payments are separate monthly plans storing Expected Date, an active Credit Card or Personal Loan destination, description, positive amount, and Active status. The compact table displays Next Date, Payment, Amount, and Actions, with the description as the primary Payment text and the destination Account as secondary text. `+ Transfer` opens the existing Transfer/Payment form with those values prefilled, browser-local today as Date, and the required From Account blank. Saving creates only the existing balanced transfer and never marks a plan paid.

Expected Income and Planned Debt Payment tables default to Next Date ascending and use sortable data-column headers without manual Sort Order. Flexible Category Budgets use `Category | Monthly Target | Three-Month Suggestion | Actions`. The informational total is:

```text
Monthly Requirement =
  Monthly Recurring Expenses
  + Flexible Category Budgets
  + Planned Monthly Debt Payments
  + Monthly Sinking-Fund Contributions
  - Expected Monthly Income
```

Every configured component contributes; no overlap detection is attempted.

#### Smart Template Suggestions and Budget Organization

Budget Settings provides nonnegative Monthly, Quarterly, and Annual suggestion windows, defaulting to 3 days, 1 month, and 3 months ahead. The normal New Transaction Template selector evaluates active Budget and Expected Monthly Income plans against the form's selected Date. It groups matching linked templates under `Suggested`, appending `Monthly`, `Quarterly`, `Yearly`, or `Expected income` as a concise parenthetical reason, refreshes when Date changes, and normally shows unlinked active templates without reason labels. Temporary `Show all templates` reveals remaining active linked templates outside their window. Inactive plans do not affect visibility, suggestions never auto-select a template, and no separate annual group is used.

Monthly matching uses the nearest occurrence within the configured number of days. Quarterly matching uses occurrence months within the configured number of previous or upcoming calendar months. Yearly matching uses the current month through the configured number of months ahead. Expected Monthly Income uses the Monthly window, and every window wraps across year-end.

Budget begins with a compact `This Month` overview. Monthly Requirement is the prominent left-aligned answer with its component explanation directly below, ordered as Monthly Recurring Expenses, Flexible Budget Categories, Monthly Sinking-Fund Contributions, Planned Debt Payments, and Expected Monthly Income. `Coming Up — Next 7 Days` lists active dated recurring expenses, planned debt payments, expected income, and Sinking Fund due dates; undated Flexible Category Budgets are excluded.

One top-level `New Recurring Budget Item` action opens the shared plan dialog with `Create Plan` as its creation submit action. Frequency places Monthly plans in Monthly Recurring and Quarterly or Yearly plans in Sinking Funds; there are no duplicate section-level creation actions.

Four normal fragment-backed management links show one detailed area at a time in this order: `Monthly Recurring`, `Flexible`, `Sinking Funds`, and `Payments & Income`. Payments and Expected Income retain separate tables, meanings, actions, and calculations within their shared section. The page remains dense, spreadsheet-like, and responsive without cards, charts, gradients, or decorative treatments.

### Sinking Fund Target Date

Within the Budget Sinking Funds section only, replace the fixed `Sinking-fund reserve target today` basis with a selected date. Immediately below the `Sinking Funds` heading and above the existing totals and table, show one compact standalone control row: `As of date: [date picker]`. The picker defaults from browser-local calendar date parts to today. Selecting a past or future calendar date recalculates only the Sinking Funds reserve-target presentation for that selected date; it does not change plan configuration, dates, transactions, Budget data, account balances, or another Budget section.

Use the existing Sinking Fund reserve-target recurrence calculation with the selected calendar date in place of today. For every active Quarterly or Yearly plan, derive the prior and upcoming expected occurrences surrounding that selected date and calculate:

```text
Reserve Target on Selected Date = ceiling(
  Amount * elapsed calendar days since the prior expected occurrence
  / calendar days between the prior and upcoming expected occurrences
)
```

The target reaches the full Amount on an expected date and begins the next cycle on the following day, including the existing month-end and leap-year occurrence rules. Sum the active plans' derived values for the section's top-level reserve-target total. This is a projected reserve target only; it is not a tracked account balance or amount actually set aside.

Rename the Sinking Funds table column `Target Today` to `Target on Selected Date`, and update every displayed row plus the existing top reserve-target total when the picker changes. Do not add a new summary value or place the picker within the existing summary layout. The existing monthly and per-paycheck contribution totals remain unchanged because they are independent of the selected date. Do not add a date history, saved selection, URL parameter, plan schedule, accounting entry, reserve-account tracking, or behavior outside the Sinking Funds section. The backend returns all derived target values; JavaScript must not independently calculate recurrence or money values.

### Recording Recurring Budget Items

Replace the Budget page's top-level `New Budget Plan` action label with `New Recurring Budget Item`. The action continues to open the existing shared recurring-plan dialog, whose Frequency selection determines whether the saved item appears in Monthly Recurring Expenses or Sinking Funds. It does not create a Flexible Category Budget, Planned Credit Payment, Expected Monthly Income plan, or any new plan type.

Place this one action on its own compact line immediately before the existing fragment-backed detailed-area links. Do not place it on the same line as those links and do not add a `Budget Management` heading, a new section, a toolbar label, or another navigation destination. The links remain on the following line in their existing order:

```text
Monthly Recurring   Flexible   Sinking Funds   Payments & Income
```

On narrow screens, allow the action row and links row to wrap naturally within their own rows without page-level horizontal overflow. Retain one shared creation action only; do not add duplicate creation buttons to Monthly Recurring or Sinking Funds, alter existing plan creation behavior, or rename existing plan types.

### Planned Debt Payment Terminology

Within Budget Payments & Income, rename the user-facing term `Planned Credit Payments` to `Planned Debt Payments`. Apply the new wording consistently to the subsection heading, Monthly Requirement component label, empty states, dialog headings, helper and validation text, accessible labels, and user-facing test expectations. Use `Planned Debt Payments` and `Planned Monthly Debt Payments` where the corresponding existing context requires the monthly form.

This is a terminology-only change. Eligible destinations remain active Credit Card and Personal Loan Accounts; an `Other` Credit Account does not become eligible. Retain the existing Expected Date, Description, positive Amount, Active status, `+ Transfer` action, normal transfer/payment prefill, and authoritative atomic ledger behavior. A planned debt payment remains a transfer and does not create a separate transaction type.

This is a presentation-only terminology change. It does not change schema, stored plans, calculations, account behavior, APIs beyond display labels, or add debt-specific accounting.

### Monthly Recurring Grouping

Add a compact labeled native `Group by` select to Budget's Monthly Recurring section. Provide exactly these options:

```text
None
Payment account
Category
```

Default to `None`. Preserve the selected grouping during normal Budget data refreshes, but do not store it in SQLite, add another route, or add a new planning concept. The existing Monthly Recurring subtotal remains visible and unchanged above every grouping; do not duplicate it within the selected table.

`None` retains the existing authoritative ungrouped Monthly Recurring table, sorting, actions, and columns without redesign:

```text
Next Date | Expense | Amount | Actions
```

`Payment account` groups the same recurring-items table. Each aggregate row shows only the Account name and aggregate Amount without a redundant `Payment Account` prefix, followed by contributing item rows using:

```text
Next Date | Expense | Amount | Actions
```

Include active Monthly Recurring plans only. Group each plan's full positive template amount by the linked template's expected Payment Account. A plan whose template has no Payment Account contributes to `Unassigned`. Show only represented Accounts, order them in alphabetical Cash and Credit class groups, and place `Unassigned` last. Calculate all grouping and sums authoritatively in Python using integer cents. The displayed Account amounts must sum exactly to the existing Monthly Recurring subtotal.

`Payment account` means the specific referenced Account, not a generalized payment method, funding source, cash allocation, reserved balance, or tracked payment status. Cash and Credit Accounts may both appear. Beneath every aggregate row, show compact indented detail rows listing each contributing Monthly Recurring item's Next Date, Expense name, full Amount, and the same `+ Transaction` and `Edit` actions as the ungrouped view. Include details even when only one item contributes, order them using the current recurring-item sort, and require their amounts to sum exactly to the aggregate row. Aggregate rows remain informational and have no Actions. Do not add percentages, additional totals, charts, filtering, configuration, or separate mobile behavior. The grouped four-column table must remain responsive without horizontal page overflow beyond its existing table wrapper.

`Category` uses the same grouped recurring-items presentation. Each Category aggregate row shows the Category name and aggregate Amount, followed by contributing rows using:

```text
Next Date | Expense | Amount | Actions
```

Include the same active Monthly Recurring plans and group each plan's full positive template amount by the exact selectable Expense-role Category referenced by its linked template. Do not independently add the amount to a parent rollup or create a second rollup row. Preserve recognizable parent/child context for child Categories using the application's existing category presentation, and order represented Categories using the existing Settings-defined category and hierarchy order. Show only represented Categories. Calculate the grouping authoritatively in Python using integer cents; the displayed Category amounts must sum exactly to the existing Monthly Recurring subtotal.

Beneath every Category aggregate row, show the same compact indented contributing-item rows with Next Date, Expense name, Amount, and the existing `+ Transaction` and `Edit` actions, including single-item groups and following the current recurring-item sort. Detail amounts must sum exactly to their Category aggregate. Category aggregate rows remain informational and have no Actions. Do not add percentages, additional totals, charts, filtering, or configuration.

## Simulator

Simulator is an independent planning workspace. Only its explicit recording workflows may write to the authoritative ledger.

### Simulator Planning Workspace

Simulator is a top-level planning tool for the allocation of a paycheck, bonus, reimbursement, or other arbitrary money event. It is not a second ledger, budget system, envelope system, reservation system, or recurring-transaction system. It does not reserve money, alter real account balances, or affect one plan from another. Planning itself never writes to the real ledger; the separately specified recording workflow saves ledger events through the existing authoritative forms.

#### Navigation and Page State

Add `Simulator` as a primary-navigation destination after `Budget` and before `Settings`. It is not a Budget subsection. Use the same normal responsive navigation behavior and shared main-content spacing as Dashboard, Transactions, and Budget; do not create a separate mobile navigation experience. Keep the Plan selector and its actions together in a left-aligned toolbar as the first page content.

The route is `/simulator`. It opens directly to a selected plan workspace rather than a separate plan-list page. When the route has no selected plan and one or more plans exist, select the plan with the oldest Plan date. Break equal-date ties by the oldest plan ID. The selected plan is preserved in the readable URL query parameter `plan`. A valid direct `/simulator?plan=<id>` URL opens that plan. An absent, invalid, or deleted plan ID falls back to the oldest plan. The plan selector updates this parameter without losing the rest of the page state.

At the top of the page, show the native control and actions in this order:

```text
Plan: [Plan name select]  New Plan  Duplicate  Delete
```

The native Plan select lists all plans by Plan date, oldest first, then plan ID. The displayed option text is `Plan date — Plan name`, for example `2026-08-21 — August 21 Paycheck`.

When no plans exist, show only a simple `No plans yet` empty state with a `New Plan` action. Do not show empty planning tables, a fake plan, or a default example.

#### Plans

Users may create any number of independent named plans. A plan has only:

```text
id
name
planDate
```

Plan name is required after trimming leading and trailing whitespace; duplicate names are allowed. Plan date is required, uses `YYYY-MM-DD`, and defaults from browser-local calendar date parts. It provides context only: it is not a schedule, does not affect calculations, and does not become authoritative for any real transaction date.

`New Plan` and `Duplicate` use the same straightforward modal form containing `Plan Date` followed by `Plan Name`, then `Create Plan`. For New Plan, Plan Name is blank and Plan Date defaults to browser-local today. For Duplicate, Plan Name is blank and Plan Date also defaults to browser-local today; the user explicitly supplies both values. Do not create names such as `Copy of <Plan Name>`. In an open plan workspace, `Edit Plan` opens the same dialog in edit mode with Plan Date and Plan Name prefilled and `Save` as its submit action. It saves only plan metadata and preserves unrelated worksheet drafts, as specified in Simulator Plan Management and Executed-Row Cleanup.

Saving a duplicate creates a new independent plan with the selected source plan's Live Account Balance selections and planned rows in their existing row order. It opens the new plan immediately. Saving a new plan creates an empty plan and opens it immediately.

`Delete Plan` in the Edit Plan dialog permanently deletes only the selected plan and its simulator rows after named confirmation. It never affects accounts, categories, transactions, transfers, reports, Budget, or another plan. After deletion, select the remaining oldest-dated plan, or show the no-plans empty state.

Creating a plan saves its name and Plan date immediately after the valid Create Plan form is submitted. There is no whole-plan save operation for worksheet rows. Each worksheet row has its own `Save Row` action. Saving a row validates and persists only that row, then refreshes authoritative live balances, Results, and saved-row grouping while preserving every other edited or new unsaved row exactly as entered. The refreshed Results include saved data only; another row's unsaved draft does not affect calculations until that row is saved. Editing a saved row leaves the prior saved row unchanged until that row's next successful Save Row. Once a saved row changes, show `Cancel Changes` beside Save Row; it restores only that row's last saved values locally without refreshing the plan or disturbing other unsaved rows. New unsaved rows use Delete to discard them and do not show Cancel Changes. Saving plan metadata in Edit Plan and deleting another saved row likewise preserve unrelated worksheet drafts. If the user switches plans, navigates away, or reloads while one or more rows have unsaved edits, warn that those unsaved row edits will be discarded and offer Discard or Cancel; do not offer Save All.

#### Page Layout and Row Order

Keep the plan selector, New Plan, Duplicate, and Edit Plan controls above the workspace. The workspace contains Plan Worksheet and Results, with Projected Account Balances inside Results. Plan metadata is edited in the shared plan dialog. Use the responsive layout specified in Simulator Results and the subsequent layout amendments.

Plan Worksheet is one dense, spreadsheet-like native table comparable to Bulk Entry. It is the single place for Live balances, planned income, planned spending, transfers, and payments. Do not separate those types into separate tables or give them competing section headings.

```text
Type | Template | Account(s) | Label | Amount | Actions
```

Type is a required native select with these values:

```text
Live balance
Income
Spend
Transfer
Payoff
Buffer
```

The Simulator Type dropdown uses the concise user-facing label `Transfer`. This is the existing internal transfer row type and continues to represent both transfers and payments; do not rename its backend value, schema representation, API behavior, accounting, or recording flow. References below to Transfer / Payment describe that unchanged financial behavior rather than a separate visible dropdown label.

Each row enables only the cells that apply to its selected Type and shows `—` in inapplicable cells. Type remains editable after a row is created. Changing Type clears only fields that are inapplicable or whose financial role is incompatible with the new Type. Preserve Label and entered Amount when changing among Income, Spend, Transfer / Payment, and Buffer because all four use them. In particular, Spend ↔ Buffer retains Account, Label, and Amount. Switching directly between Transfer / Payment and Payoff preserves From Account, To Account, Label, and Amount. Transfer → Payoff retains the fixed transfer amount only in the unsaved browser row while showing the derived payoff; switching that same row back to Transfer restores the retained fixed amount. Switching a saved Payoff directly to Transfer uses its currently displayed derived payoff as the editable fixed transfer Amount rather than clearing it. Entering or leaving Live balance clears Label and entered Amount because a Live balance has neither and derives its Amount. In Account(s), preserve a selected source account between Live balance, Spend, Buffer, and the From side of Transfer / Payment or Payoff; preserve a selected destination account between Income and the To side of Transfer / Payment or Payoff. Otherwise clear the account selection. Never carry an account from a source role into a destination role or vice versa.

Account(s) is one adaptive worksheet cell rather than separate From and To columns. Live balance, Income, Spend, and Buffer rows show one native account select. Transfer / Payment rows show two native account selects in the same cell, with `→` between From and To. Size the two selects and arrow as one fixed internal grid whose total width exactly matches the single-account control. The second account select appears only for Transfer / Payment.

Template is an overall worksheet column immediately after Type. For Income and Spend rows, it provides a native `Use template` select whose default is `None`. It lists active templates compatible with the selected row Type: an Income-role template is compatible with Income, an Expense-role template is compatible with Spend, and a template with no category is compatible with either. Selecting a template copies its available account, description into Label, configured amount, and category ID into the row. Simulator always copies a configured template amount regardless of the transaction-entry `Prefill Amount` setting; that setting continues to apply only to normal transaction entry. Category remains hidden in the planning workspace but is saved as a stable snapshot for recording-workflow transaction-form prefilling. Keep the selected template visibly selected during the current unsaved edit rather than immediately switching to None, including while another row is saved and the workspace refreshes. The selector returns to None after this row is saved or the page is reloaded because the simulator stores copied values, not a persistent template relationship. Template application never changes the row Type or saves the row automatically. Changing, archiving, or deleting a template never changes an existing plan row. For Live balance, Transfer / Payment, Payoff, and Buffer rows, Template is inapplicable and displays `—`. Buffer also has no Category control or stored category selection.

Keep one permanent type-only composer row at the bottom of the worksheet instead of an `Add Row` button. It shows only the required Type selector and has no Save Row, Duplicate, or Delete actions. Choosing a Type turns it in place into a normal unsaved row and immediately adds a fresh type-only composer beneath it. Give every new, duplicated, or modified unsaved row a distinct background that remains readable in light and dark themes. Every saved row has Edit; eligible row duplication and confirmed deletion are in its row-management modal, as specified in Simulator Recording and Executed Rows and Edit Modal. New unsaved rows have Delete to discard their local draft. `Save Row` appears for an unsaved row or after a saved row changes and remains hidden for an unchanged saved row. A successful Save Row validates and persists that one row, then renders saved rows in this worksheet-group order: Live balance, Income, Spend, Transfer / Payment, Payoff, then Buffer. If an existing row's Type changes, move it to its new group only after that successful Save Row. Preserve creation order within each Type group. Keep typed unsaved rows together above the permanent composer until they are saved; Save Row identifies normal validation errors and retains that row's entered values. Keep every worksheet column visibly present with stable widths while its applicable control changes; leave non-Type cells blank in the composer and use `—` for inapplicable cells in normal rows. Use a clear dark horizontal separator bar between saved row groups; do not create separate tables, repeat table headers, or add competing section headings. Do not animate row reordering. Do not add separate row-creation buttons by Type, drag-and-drop, manual sort order, sorting headers, pagination, search, or grouping controls. Deleting an unsaved local row simply removes it. Deleting a saved row from its management modal uses the existing destructive-action confirmation behavior, then immediately deletes that row and refreshes live balances and Results without deleting a linked ledger event.

Render Projected Account Balances and totals as a compact Results area alongside Plan Worksheet when space permits and below it at the existing responsive breakpoint. Keep the worksheet's natural wide-table layout inside its horizontally contained area, but do not force that width onto Results. Results uses the available content width like the Dashboard: show the five summary values in one row when space allows and let them wrap naturally on narrow screens without horizontal scrolling. Projected Account Balances also uses the available width with the same normal responsive table behavior as Dashboard account data. The worksheet and results table are one plan workspace, not separate applications or a stack of unrelated dashboard sections. Use the existing Dashboard Cash Accounts and Credit Cards and Loans presentation as the visual inspiration for account and friendly-credit-balance display, with only Account and Projected Balance columns, as specified in Simulator Results. Preserve the worksheet columns without causing whole-page horizontal overflow. Controls need visible labels and normal keyboard behavior.

#### Worksheet Row Types

###### Live Balance Rows

For a Live balance row, Account(s) selects the Cash account and Amount displays its derived Current Live Balance. Label is inapplicable. Only active Cash accounts may be newly selected, and an account may occur in only one Live balance row in a plan. The account select excludes accounts already selected by another Live balance row. Credit accounts, including Credit Cards and Personal Loans, must never appear as Live balance inputs because debt is not cash available for allocation.

A Current Live Balance is always derived in Python from the account's current authoritative balance:

```text
balanceOffsetCents + SUM(transaction amounts for account)
```

It is not copied into the simulator database, is never editable, and does not create a transaction. Refresh it whenever the plan is opened and after every successful row save or saved-row deletion. There is no separate Carryover field or Carryover behavior.

Live balance rows are the plan's selected cash available for allocation; they are not a requirement for account projections. Income, Spend, and Transfer / Payment rows may select any eligible active account under their own rules. Every account selected in a Live balance row or referenced by another worksheet row appears automatically in Projected Account Balances using its current authoritative balance as the starting point. Removing a Live balance row never changes another worksheet row; it only removes that Cash account's current balance from the plan's available-money inputs.

###### Income Rows

For an Income row, Account(s), Label, and a positive nonzero Amount are required. Account(s) is the destination account. Amount is parsed and stored as integer cents. Label is trimmed, must not be blank, and is presentation only; do not add label-based behavior. Multiple rows may use the same Label, account, or amount.

An Income row increases the projected balance of its To Account and increases Unallocated Money by its Amount. It does not create an income transaction in the planning workspace.

###### Spend Rows

For a Spend row, Account(s), Label, and a positive nonzero Amount are required. Account(s) is the spending account. Apply the same label and integer-cent rules as Income rows.

A Spend row may be completely arbitrary. For example, `Blackjack` or `Amazon` may be used as the Label. The planning workspace does not display or select a Category. Recording uses the existing authoritative transaction form with its required Category; a compatible stored Template category snapshot may prefill it under Simulator Recording's eligibility rules.

The selected Account may be Cash or Credit. Spending from a Cash account reduces its projected internal balance by Amount. Spending charged to a Credit account also reduces its projected internal balance by Amount, thereby increasing its friendly amount owed under the existing credit-account display rules. A Spend row reduces Unallocated Money by its Amount in either case.

###### Transfer / Payment Rows

For a Transfer / Payment row, both Account(s) selects, Label, and a positive nonzero Amount are required. Category is inapplicable. The From and To accounts must differ. Apply the same trimmed-label and integer-cent rules as the other planned rows. Neither category nor transaction description is inferred beyond the user-entered Label.

Each row is independent. Multiple rows may have the same accounts, label, and amount. Do not deduplicate, merge, or net them. The source account's projected internal balance decreases by Amount and the destination account's projected internal balance increases by Amount. Therefore every planned transfer or payment is zero-sum across projected accounts.

When To Account is Cash, present the row as a planned transfer. When To Account is any Credit Account, including Other Credit, present it as a planned payment. When the user changes To Account to any Credit account, update Label using the normal payment-description convention: `<Account Name> Card Payment` for a Credit Card whose name does not already end in `Card`, and `<Account Name> Payment` for an account name already ending in `Card` or for a Personal Loan or Other Credit Account. When the user changes the destination back to a Cash account, set Label to `Transfer to <Destination Account Name>` as specified for Cash-to-Cash transfers. Do not trigger either update merely by rendering an existing row. This is presentation only; it does not create a new payment type. Saved labels remain unchanged on opening, rendering, refresh, or unrelated edits; only an explicit destination change applies the default. Simulator Type remains Transfer and recording actions remain unchanged.

###### Payoff Rows

Payoff is presented as its own worksheet Type without adding another column. It retains transfer/payment accounting underneath and stores an explicit derived payoff amount mode rather than inferring behavior from Label. From Account must be active Cash, To Account must be active Credit, the accounts must differ, and Label is required. When the user selects a different destination card, update Label using the same payment-description convention as Transfer / Payment; Payoff does not have a separate label convention. Amount is read-only text rather than an input and is not stored as fixed cents.

Python derives the payoff after applying every non-payoff planned change in the plan, regardless of worksheet row order:

```text
payoff amount = max(0, -(live destination balance + all non-payoff planned changes to destination))
```

Apply that derived amount as a Cash-to-Credit transfer so the destination's projected internal balance becomes zero. Include it in Transfers & Payments and reduce Unallocated Money by the derived amount. Allow at most one Payoff row per destination Credit account in a plan. A zero or overpaid projected Credit balance produces a visible `$0.00` payoff rather than creating a negative transfer. Duplication creates an unsaved editable Payoff row, but saving a duplicate for the same destination remains blocked.

###### Buffer Rows

Buffer is a planning-only Cash-account allocation with required Account, Label, and positive nonzero Amount fields. Only active Cash accounts may be selected. Amount is parsed and stored as integer cents. Template and Category are inapplicable. Buffer never has a Record action, Executed state, linked ledger transaction, or other ledger behavior.

A Buffer does not change its selected Cash account's projected balance or Planned Change because it represents an allocation margin rather than money projected to leave the account. It reduces Unallocated Money and is included with Spend rows in `Planned Spending & Buffer`. An account referenced only by a Buffer still appears in Results. For example, a `$1,000.00` Chase live balance with a `$300.00` `Car repairs` Buffer continues to project Chase at `$1,000.00`, while the Buffer reduces Unallocated Money by `$300.00`, without creating or implying a ledger transaction.

Directly below the Projected Account Balances table, provide an unchecked-by-default checkbox labeled `Exclude buffer from projected balances`. This control is transient presentation state and is not stored. While unchecked, display the true projected balances described above. While checked, subtract the sum of each Cash account's configured Buffer rows from only that account's displayed projected balance. Credit balances are unchanged. This display option must not change the underlying projected balances or calculations, Unallocated Money, Planned Spending & Buffer, account inclusion or ordering, minimum-status badge calculations, stored Simulator data, or ledger state. Python returns each result account's Buffer total in integer cents; JavaScript uses it only to select the displayed balance.

Keep normal inline Save Row and Cancel Changes behavior. A saved Buffer row's only ordinary inline action is Edit. Its Edit modal contains Duplicate and the confirmed destructive Delete action, with no execution or linked-transaction controls. Duplicate creates an unsaved planned Buffer row with the same planner values and no ledger state.

#### Results

Results is a compact projected-balance table attached to Plan Worksheet. It contains tracked accounts referenced by Live balance and other non-executed rows; exclude External and accounts referenced only by executed financial rows, as specified in Executed Rows and Edit Modal. Keep one table, grouping all Cash accounts first and all Credit Cards and Loans afterward. Within each account-class group, order accounts case-insensitively by name with Account ID as the tie breaker. Give Credit rows a darker neutral background so the debt section is visually distinct without creating a second table:

```text
Account | Projected Balance
```

Results are calculated in Python with integer cents and returned ready for browser presentation. Do not independently calculate financial or planning meaning in JavaScript.

For each account:

```text
projectedInternalBalance = liveInternalBalance
  + SUM(planned income into account)
  - SUM(planned spending charged to account)
  - SUM(planned transfer/payment amounts from account)
  + SUM(planned transfer/payment amounts to account)
```

For compatibility, the backend retains internal `Planned Change` as projected internal balance minus live internal balance and its display value: Cash uses that signed change, while Credit reverses it to represent change in amount owed. A $250 Credit charge has a display change of `+$250.00`, while a $250 payment has `-$250.00`. The current Results table does not display a Planned Change column. Display an ordinary Credit balance as an unsigned amount without appending the word `balance`; retain the word `credit` for a positive internal overpayment. Never expose an internal negative balance as a user-facing card debt. A planned credit charge must visibly increase the projected amount owed, while a planned payment must visibly decrease it.

Show the five-value summary with Starting Cash first, using the compact responsive presentation specified in Simulator Results. The four remaining values and allocation formula are:

```text
Planned Income
Planned Spending & Buffer
Transfers & Payments
Unallocated Money
```

```text
Unallocated Money = Cash Live Balances + Planned Income - Planned Spending & Buffer - Transfers & Payments
```

Cash Live Balances is the sum of every selected Live balance row. Planned Income is the sum of every Income-row amount. Planned Spending & Buffer is the sum of every Spend-row amount, including Credit charges, plus every Buffer-row amount. Transfers & Payments contains all Credit and Payoff payments plus the signed net effect of Cash transfers crossing the selected Cash Live Balance pool. A Cash transfer between two selected Live Balance accounts is neutral. A transfer out of the pool reduces Unallocated Money; a transfer into the pool increases it. External is outside the selected Cash pool: External-to-selected-Cash contributes negative Amount to the Transfers & Payments deduction and increases Unallocated Money; selected-Cash-to-External contributes positive Amount and decreases it. An External transfer involving unselected Cash contributes zero to the allocation summary, including when no Live balance rows exist, but always changes the tracked Account projection by its signed side. These transfers never become Planned Income or Planned Spending. External-to-Credit retains the existing Credit-payment allocation rule; executed rows remain excluded from planned calculations. This supports a zero-based allocation workflow in which the user assigns all selected available Cash and planned income. A negative Unallocated Money remains visible as a negative amount; do not block the plan, automatically rebalance rows, or invent a warning category.

#### Data Integrity and Archived Configuration

Store plans and their worksheet rows in SQLite using stable IDs and explicit foreign keys. Retain the nullable category reference in the simulator-row schema. the planning workspace does not expose a Category control, but applying an Income or Spend template copies and persists that template's compatible category ID as a stable snapshot for possible the recording workflow transaction prefilling. Preserve an existing stored category reference during unrelated the planning workspace row edits and clear it when the row Type changes or a newly applied template has no category. Store no persistent template ID or relationship. Store an explicit fixed-or-payoff amount mode; fixed rows store integer cents, while Payoff rows store no derived balance or amount. Store only plan configuration: plan name, plan date, row type, row order, labels, account references, optional copied category references, fixed integer-cent amounts, and amount mode. Do not store live balances, projected balances, account totals, ledger transaction IDs, transfer IDs, execution state, or any second-ledger state in the planning workspace.

Use one explicit schema migration and database transactions for plan creation, duplication, plan deletion, row saving, and saved-row deletion. Plans, rows, and their calculations must remain independent.

New selections offer active accounts and active selectable categories only. If a formerly selected account or category is archived later, preserve the plan reference and render it clearly as archived rather than silently replacing, renaming, deleting, or recategorizing it. Archived accounts continue to have a derivable historical current balance and may appear in Results. Do not let a stale or archived reference silently become a different active record.

#### Planning Workspace Boundaries

the planning workspace includes every Simulator behavior described above through Results. It is a read-only planning and decision tool. It does not open a New Transaction form, open a transfer form, create ledger data, or include any Executed status.

### Simulator Recording

The recording workflow adds controlled recording from Simulator into the existing authoritative transaction and transfer forms. It does not add a second ledger or make the ledger aware of Simulator.

#### Execution State and Link

These Simulator-row fields were added through a small explicit migration:

```text
executed
executedTransactionId
```

`executed` is a required Boolean defaulting to false. `executedTransactionId` is optional: it is null for a Planned row and for a row manually marked Executed without recording through Simulator. When a row is recorded through Simulator, it stores one canonical authoritative ledger transaction-row ID:

- Income and Spend rows store their ordinary transaction ID.
- Transfer, Payment, and Payoff rows store the source-side From-account transaction ID. That row's existing `transferId` identifies the authoritative two-row transfer event.

Do not add a separate executed-transfer ID. Do not add a plan ID, execution state, special transaction type, or other Simulator relationship to the ledger. The reference is one-way from Simulator to the existing ledger.

#### Worksheet Actions

The recording workflow preserves the planning workspace's inline worksheet editing. `Save Row` and `Cancel Changes` continue to apply exactly as specified for new and modified rows; do not move ordinary planner-field editing into a modal. Every saved row has an `Edit` action. Row deletion and financial-row duplication are inside that Edit modal; do not retain direct worksheet Delete or Duplicate actions. Live balance rows cannot be duplicated. While a saved financial row has unsaved inline edits, hide its recording action until the row is saved or `Cancel Changes` restores its saved configuration.

```text
Live balance:
Edit

Planned Income:
+ Transaction | Edit

Planned Spend:
+ Transaction | Edit

Planned Transfer / Payment:
+ Transfer | Edit

Planned Payoff:
+ Transfer | Edit

Buffer:
Edit

Executed financial row:
Edit
```

Live balance and Buffer rows are not financial events and never have a Record action or execution state.

For a planned row, `+ Transaction` or `+ Transfer` opens the existing authoritative form with every available value prefilled. Income and Spend use the normal New Transaction form with Account, Label as Description, and Amount. Income defaults Category to the active standalone Income category configured as Settings' Reimbursement Category; if that setting is unavailable or invalid, leave Category blank. Spend uses the hidden the planning workspace template-copied category snapshot when present and otherwise leaves Category blank. The normal transaction form remains responsible for requiring a valid category before it saves. Transfer / Payment and Payoff use the existing transfer/payment form with From Account, To Account, Label as Description, and Amount prefilled. All forms default Date to browser-local today, never Plan date, and the user may change every field.

Cancelling or closing a record form changes neither the ledger nor the Simulator row. A successful record save uses one SQLite transaction to create the authoritative ledger transaction or transfer, store `executedTransactionId`, and set `executed` true. Do not overwrite the Simulator row's underlying planned configuration with the saved ledger values, except for the explicit Payoff conversion rule below. For example, a planned $100 Spend recorded as $125 displays $125 after execution by reading its linked ledger transaction, while the stored planner row remains the original $100 plan.

#### Executed Rows and Edit Modal

Executed financial rows render as greyed-out, read-only text. An executed row with `executedTransactionId` displays its current values directly from the linked authoritative ledger transaction or transfer whenever Simulator loads; do not display a copied historical planner snapshot. This keeps the row accurate when the linked event was later edited from the Transactions screen. An executed row with no link, because it was marked Executed manually, displays its own planner values. Executed rows remain visible for plan context but are excluded from all future Simulator calculations: projected balances, Planned Income, Planned Spending & Buffer, Transfers & Payments, and Unallocated Money. Refreshed live balances contain the real financial effect instead.

Edit always opens a Simulator-row modal; it never directly edits ledger data. Planner configuration remains inline in the worksheet and uses the existing Save Row and Cancel Changes behavior. For a Planned financial row, the Edit modal provides Duplicate, the manual Executed control, and Delete. A Live balance row's modal provides only its Delete action and never shows execution controls or Duplicate. A Buffer row's modal provides Duplicate and Delete, but no execution or linked-ledger controls. An executed financial row's modal clearly states:

```text
This row is marked Executed.
It remains visible in the planner but no longer affects Simulator math.
```

It includes:

```text
Edit linked transaction
Duplicate
Executed [checked]
Delete
```

Show `Edit linked transaction` only when `executedTransactionId` is present. It opens the existing normal transaction editor when its canonical ledger row has no `transferId`, or the existing atomic transfer/payment editor when it has a `transferId`. It is inside the Simulator Edit modal, never an additional worksheet-row action. A successful linked-ledger edit leaves the planner configuration unchanged; the executed row's next render reads the current linked-ledger values while retaining its execution state and canonical ID. When an executed row has no linked transaction, state clearly that it was marked Executed manually and has no linked ledger transaction.

The Executed control may be checked manually for a Planned financial row to exclude it from Simulator math when the user handled the real activity outside Simulator. This sets `executed` true and leaves `executedTransactionId` null. For an executed row with a nonnull `executedTransactionId`, render Executed as an informational checked status rather than a control the user can clear. Do not allow it to return to Planned manually: the user must use `Edit linked transaction` and delete the real ledger transaction or transfer. Its normal ledger deletion automatically returns the Simulator row to Planned, preventing the same financial event from being counted twice through live balances and Simulator math.

For a manually executed row with no `executedTransactionId`, allow the user to clear Executed and return it to Planned after explicit confirmation. State clearly that Simulator has no linked ledger transaction and returning it to Planned may double-count money if the user recorded the activity elsewhere. Clearing it returns the row to Simulator math and never edits, deletes, or reverses any ledger transaction.

Delete is the existing red destructive action at the bottom of every Simulator-row Edit modal and retains normal confirmation. Deleting an Executed row deletes only the Simulator row; it never deletes the linked ledger transaction or transfer.

If a nonnull linked ledger transaction or transfer is deleted through its normal editor, or is found missing while the Simulator plan loads, clear `executedTransactionId`, set `executed` false, and return the Simulator row to Planned so it affects calculations again. Because linked execution does not overwrite its configuration, the restored Planned row uses its original planned values, except for a Payoff row already converted to fixed Transfer / Payment by its explicit recording rule. A manually executed row with no `executedTransactionId` remains Executed.

#### Payoff Recording

For a planned Payoff row, `+ Transfer` opens the existing payment form with its current Python-derived payoff amount prefilled. Every successful Payoff recording converts the Simulator row to a fixed executed Transfer / Payment row using the final saved values, whether or not the user changed the prefilled amount. A payoff is dynamic only while Planned; after recording, it must not drift if later Credit-account activity changes the theoretical payoff amount. If the row is later returned to Planned, it remains the fixed Transfer / Payment row using the recorded amount.

#### Scope Limits

Duplicated plans copy their Simulator configuration, but every duplicated financial row begins Planned with `executed` false and no `executedTransactionId`.

Simulator remains a manual planning workspace. Planning does not automatically create, schedule, match, or allocate transactions. Explicit recording uses the existing authoritative transaction and transfer forms as specified above. Simulator does not store account balances or create another ledger.


### Simulator Projected Credit Utilization

In Simulator Results, show a projected utilization badge immediately after the account name for each Credit Card that appears in Results and has a configured credit limit. Use the same compact solid badge treatment, percentage formatting, accessible labeling, and thresholds as the Dashboard's individual Credit Card utilization badges: green below 30%, yellow from 30% through below 50%, and red at 50% or higher. Use dark text on green and yellow, white text on red, and retain the percentage so color is not the only signal.

Calculate utilization from the final projected internal balance and configured limit in Python:

```text
projectedAmountOwed = max(0, -projectedInternalBalance)
projectedUtilization = projectedAmountOwed / creditLimitCents
```

Display the projected percentage to two decimal places. An overpaid projected card displays 0.00%, and projected utilization may exceed 100%. Do not show a badge for Cash accounts, Personal Loans, Other Credit accounts, Credit Cards without a limit, or accounts absent from Results. Do not add a total projected utilization, a new Results column, a warning area, blocking behavior, or any change to existing account balances, credit limits, or Dashboard utilization.

The backend returns the display-ready projected utilization and status; JavaScript must not independently calculate it.

### Simulator Results

Add `Starting Cash` as the first value in the existing Simulator Results summary, before `Planned Income`, `Planned Spending & Buffer`, `Transfers & Payments`, and `Unallocated Money`.

```text
Starting Cash
Planned Income
Planned Spending & Buffer
Transfers & Payments
Unallocated Money
```

Starting Cash is the signed sum of the current authoritative balances of the Cash accounts selected by Live balance rows in the plan. It includes each selected account's `balanceOffsetCents` and transaction total, refreshes whenever live balances refresh, and is not stored. It does not include Credit accounts, Cash accounts not selected in a Live balance row, planned income, planned spending, Buffers, transfers, payments, or any other account-table value. When no Live balance rows are selected, display `$0.00`.

Use Starting Cash in the existing Unallocated Money calculation:

```text
Unallocated Money =
  Starting Cash
  + Planned Income
  - Planned Spending & Buffer
  - Transfers & Payments
```

Keep the existing compact summary presentation. On widths that allow it, show all five values on one row; on narrow screens, wrap naturally into a compact layout without horizontal page overflow. Do not create a separate Starting Cash table, stored total, account-balance summary beyond the existing Results table, or a new allocation concept.

Reduce the Simulator Results account table to these two columns:

```text
Account | Projected Balance
```

Remove the existing `Live / Starting Balance` and `Planned Change` columns. The Results totals above the table remain the compact allocation summary; Plan Worksheet remains the place to inspect individual planned inputs and outputs.

Retain the existing account inclusion, Cash-before-Credit grouping, and ordering behavior. Keep all authoritative projected-balance calculation in Python, including the existing exclusion of executed rows from future simulation because their real recorded effects are reflected in refreshed live balances. This change hides intermediate per-account math; it does not alter projected-balance calculations, plan data, ledger data, or execution behavior.

Projected Balance remains the primary result and retains every existing display convention: Cash balances display normally; Credit balances display as unsigned friendly amounts without the word `owed` or an internal negative sign, while a positive internal Credit overpayment displays as `credit`.

Keep the Results table responsive in its existing available-width layout. Do not create separate Cash and Credit tables, a drilldown, a chart, additional row interactions, configurable columns, or mobile-specific behavior.

### Post-Action Navigation

- Simulator utilization badges keep concise visible percentages with descriptive accessible labels, and the Results summary and Projected Account Balances use an even 50/50 split when they share a row. They stack at the existing wider breakpoint before either side clips or causes page-level overflow.
- A selected Sinking Fund date changes only Sinking Fund occurrence dates, reserve targets, and their total. Native date commit, focus loss, and Enter apply the date without keystroke-by-keystroke or mouse-leave refreshes.
- Settings Accounts includes the compact `Minimum` column defined above, and Account minimum-helper updates retain backend authority without stale responses or disruptive layout changes.


### Simulator Enter to Save Edited Row

While an existing saved Simulator worksheet row has unsaved inline edits, pressing Enter in a normal text, number, or date input performs the existing `Save Row` action. It must use the same authoritative validation, persistence, recalculation, and rendering path as selecting `Save Row`; do not create a second save implementation.

If validation fails, keep the row in edit mode, preserve every entered value, and show the existing row-level validation feedback. Prevent key repeat or another Enter press from starting a second save while the first request remains pending.

Do not intercept Enter while a native select is open; retain the browser's normal selection behavior. Do not apply this shortcut to a new unsaved worksheet row, the Simulator row-management Edit modal, a linked transaction or transfer editor, a confirmation dialog, or any other modal. The explicit `Save Row` and `Cancel Changes` actions remain available and unchanged.

Enter-to-save never marks a row Executed, records ledger activity, opens a transaction or transfer form, duplicates a row, deletes a row, or changes plan-level state beyond the edited row's normal successful save. Do not add Escape handling or another keyboard shortcut as part of this work.

### Simulator Row Modal Details

At the top of every existing Simulator row-management Edit modal, add a compact read-only summary that clearly identifies the row affected by the modal's management actions. Use a meaningful heading such as `Manage Simulator Row`; do not imply that planner configuration is editable inside the modal.

Show the row type, current status, and every meaningful user-visible planner field applicable to that type. Examples include Account, From Account, To Account, Label, Category when part of the row's visible meaning, Amount, and Planned or Executed status. Live balance and Buffer rows show only their applicable fields. Omit fields that do not apply rather than displaying internal null values. Do not expose database IDs, internal ledger signs, hidden template-copied snapshots that are not otherwise user-visible, or implementation state.

For an executed row with a linked ledger event, retain the existing rule that its visible financial details come from the current authoritative ordinary transaction or combined transfer/payment. Identify the source concisely as a linked ledger transaction or transfer. For a manually executed row without a link, state that it was marked Executed manually and has no linked ledger transaction. Preserve friendly Credit presentation wherever an existing display convention applies.

Keep all summary values read-only. Inline worksheet editing remains the only place to modify planner configuration and continues to use the existing `Save Row`, `Cancel Changes`, and approved Enter-to-save behavior. Do not duplicate planner inputs inside the modal, synchronize two drafts, move worksheet editing into the modal, or add an Edit Fields action. The existing Duplicate, Executed, Edit linked transaction, and Delete actions remain available only according to their current row-type and execution-state rules.

Python returns the semantic row details and flags needed for presentation; JavaScript must not independently derive financial meaning or linked-ledger values.

### Send to Simulator

From Transactions, provide a `Send to Simulator` action beside each eligible ordinary transaction or valid combined transfer event. Do not add this action to Dashboard Recent Transactions or Pinned Transactions.

Selecting the action opens a small `Send to Simulator` dialog containing a required native `Simulator plan` selector, a concise read-only preview of the row that will be copied, `Cancel`, and `Send`. Each plan option displays its formatted Plan Date followed by its name, matching the Simulator workspace selector. Default to no selected plan. Do not allow the dialog to change the generated row type, Account, Label, Amount, or other planner configuration; the independent row may be edited later in Simulator.

`Send` immediately creates one saved Planned row in the selected plan. Map an Income-role ordinary transaction to Planned Income and an Expense-role ordinary transaction to Planned Spend. Map a valid combined transfer event to the existing Transfer row type, including when its destination is a Credit Card or Personal Loan. A payment is still a transfer and must not create a separate Planned Payment type.

Copy the source Account and Description as the planner Account and Label where those concepts apply, and convert its authoritative ledger amount to the existing Simulator row's user-facing positive Amount convention. Exclude expense-category refunds because their positive ledger effect cannot be represented faithfully as Planned Spend. Do not infer another row type for a refund.

Offer the action only when the complete source event can be represented by the existing Simulator row rules using currently active, valid configuration. Exclude invalid linked-transfer rows, uncategorized historical activity, refunds, and records referencing archived or otherwise unavailable Accounts or Categories required for the generated row. Eligibility and mapping are authoritative in Python; JavaScript must not independently infer financial meaning.

The copied Simulator row begins Planned with `executed` false and `executedTransactionId` null. It stores no source ledger ID or other relationship. Changing, executing, deleting, or editing either the ledger event or copied Simulator row never changes the other. Do not create ledger activity, automatic matching, synchronization, or duplicate history.

After success, close the dialog, remain on the current Transactions page, preserve its readable filter and sort URL, and use the shared temporary notification with concise Green text such as `Sent — Added to <Plan Name>.` Insert the user-owned plan name through the notification method's safe plain-text behavior. Do not navigate to Simulator automatically. Cancellation or failure creates no row and shows no success notification; validation failures keep the dialog open for correction.

Send to Simulator handles one eligible event and one selected plan at a time without adding a planner row type or database relationship.

### Simulator Plan Management and Executed-Row Cleanup

Move editing and deletion of the selected Simulator plan behind one `Edit Plan` action. The compact plan toolbar becomes:

```text
Plan: [Plan Date — Plan Name]
New Plan
Duplicate
Edit Plan
```

Remove the inline Plan Date and Plan Name fields, their dirty-state behavior, and the inline `Save Changes` action from the open workspace. The selected native Plan option continues to show `Plan Date — Plan Name`, so the current plan remains identifiable without duplicated editable fields.

Reuse the existing shared plan dialog in Edit mode rather than creating a second form. `Edit Plan` opens it with the selected Plan Date and Plan Name prefilled, `Edit Plan` as its heading, and `Save` as its submit action. Saving validates and updates only those two plan fields and then refreshes the selector and workspace while preserving the selected stable plan ID and unrelated worksheet drafts.

Place a red `Delete Plan` action at the lower left of the Edit Plan dialog, separated from Cancel and Save. Retain the existing named permanent-deletion confirmation and complete plan-deletion behavior. Do not show Delete Plan while creating or duplicating. A cancelled or failed edit or deletion preserves the plan and entered values as appropriate. Do not add another route, plan field, autosave behavior, or change New Plan and Duplicate semantics.

Add a red `Delete Executed Rows` action near the `Plan Worksheet` heading. Show it only when the selected plan contains at least one Executed financial row. Do not show a disabled placeholder when none exist. The action applies to all and only currently Executed financial rows in that selected plan, including manually Executed rows and rows with an `executedTransactionId` linked to an ordinary transaction or transfer/payment. Planned rows, Live balance rows, Buffer rows, other plans, and the plan itself are excluded.

Before deletion, show one explicit confirmation naming the selected plan and authoritative current Executed-row count, with clear text equivalent to:

```text
Delete 4 executed rows from "September Plan"?

Linked transactions and transfers will remain in the ledger.
This cannot be undone.
```

On confirmation, revalidate the selected plan and Executed-row set on the backend, then delete those Simulator rows atomically in one SQLite transaction. Deleting an executed linked row removes only the one-way Simulator reference by deleting the planner row; it must never delete, edit, reverse, or otherwise alter its authoritative ledger transaction or two-row transfer. Do not return a linked row to Planned because that row is being intentionally removed. Do not create tombstones, replacement rows, execution history, or an Undo mechanism.

After success, keep the same plan selected, recalculate and refresh Simulator from authoritative state, preserve unrelated unsaved Planned-row drafts, and hide the action when no Executed rows remain. A cancelled or failed request changes nothing. A backend failure must roll back the complete batch so partial cleanup cannot occur. The existing individual Simulator-row Delete action remains available in its row-management modal and continues to delete only that one Simulator row.

Do not move `Delete Executed Rows` into the Edit Plan dialog: it is a worksheet cleanup action rather than plan metadata. Do not change execution, recording, linked-ledger editing, Payoff conversion, live-balance refresh, projected calculations, or ledger-aware deletion behavior.

## Final Product Principle

When choosing between two implementations, prefer the one with:

```text
Fewer concepts
Fewer clicks
Fewer files when practical
Less duplicated state
Less hidden behavior
Less code
Clearer data
Easier recovery
```

Do not optimize or automate a process until the simple version is working correctly.

### Maintenance and Cleanup Discipline

Cleanup and documentation consolidation must be surgical. Preserve existing code, normative product behavior, acceptance criteria, and user data by default. Remove or rewrite only content explicitly placed in scope.

Code and test removals require explicit approval of the specific candidates after a read-only audit. Preserve required schema migrations and historical compatibility. Tests may be removed only when their behavior is retired or retained assertions preserve the supported behavior and meaningful edge cases, including signs, permissions, rollback, concurrency, date boundaries, and migrations.

Before completing cleanup work, review the exact diff and verify that unrelated normative behavior, current feature requirements, and deferred roadmap items remain unchanged.

Automated tests and development verification must not open, initialize, migrate, or write the real `finance.db` unless the user explicitly authorizes testing against it. Use fresh temporary databases or temporary database copies instead.

Unit, restart, destructive, initialization, and migration tests must use explicit temporary database and backup paths with synthetic fixtures. Before application modules are imported, the test harness must enable an in-process test-only database guard that refuses the workspace `finance.db` even if test code accidentally supplies or falls back to that path. The guard must not use persistent or inherited environment configuration and must never affect normal Windows or Linux application startup. Importing application modules during tests must not initialize the default database, and test application creation must never fall back to the production path.

Real-data integration tests may use production data only when `finance.db` already exists. Open the source through SQLite read-only mode, record and verify its byte hash remains unchanged, write a consistent SQLite backup to an explicit temporary path, and perform all subsequent operations on that temporary copy. If the source is absent, skip clearly without creating it.
