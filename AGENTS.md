# AGENTS.md: Codex Instructions

## 1. Mission

Build the application defined in `SPEC.md` and the approved `Version Next` milestone in `ROADMAP.md`.

`SPEC.md` is the source of truth for implemented behavior. `ROADMAP.md` is the source of truth for future work, and only its `Version Next` section is approved for implementation.

Do not redesign the product while implementing it.

The objective is a small, understandable, reliable personal finance application rather than an impressive software architecture.

## 2. Work One Milestone at a Time

Before coding:

1. Read all of `SPEC.md` and `ROADMAP.md`.
2. Identify the currently requested milestone. When `Version Next` contains one milestone, `Implement Version Next` means its complete scope. When it contains multiple named milestones, implement only the explicitly requested milestone.
3. Implement only that milestone.
4. Run the relevant tests.
5. Verify its acceptance criteria.
6. Report what changed and any unresolved issue.
7. Stop.

Do not automatically begin the next milestone.

Do not prebuild features from later milestones.

After the requested milestone is successfully implemented and verified, merge its enduring behavior into the appropriate canonical sections of `SPEC.md` and remove only its completed entry from `ROADMAP.md`. Preserve other Version Next milestones and do not begin them automatically. Do not promote Version Soon automatically.

This project is intentionally split into milestones to reduce wasted implementation effort and Codex usage.

Version Next may contain multiple independently scoped feature milestones. Complete and verify one requested milestone at a time, normally recording each feature and its corresponding SPEC.md and ROADMAP.md updates in one logical commit. Multiple completed feature commits may be batched and pushed upstream together. A batch push does not authorize starting another milestone. Move completed behavior into SPEC.md and remove its completed roadmap entry after verification, without waiting for the batch to be pushed.

Keep one logical change per commit. If a small nit surfaces right after committing and it hasn't been pushed yet, fold it in with `git commit --amend` instead of a second commit. Never amend or rewrite anything already on GitHub.

## 3. Handling Ambiguity

If a small implementation detail is unspecified, choose the simplest behavior consistent with `SPEC.md`.

If ambiguity affects:

```text
Money math
Account balances
Transaction signs
Import matching
Database integrity
Historical data
Expense reporting
```

do not guess.

Surface the ambiguity before implementing that part.

Do not invent new product features to resolve uncertainty.

## 4. Engineering Order

Use this order when making implementation decisions:

1. Question unnecessary requirements.
2. Remove unnecessary parts.
3. Simplify what remains.
4. Optimize only when there is evidence of a problem.
5. Accelerate after the design is stable.
6. Automate last.

Avoid speculative architecture.

Avoid future proofing that adds complexity today.

## 5. Technical Defaults

Use:

```text
Python
Flask
sqlite3
HTML
CSS
Vanilla JavaScript
```

Flask should be the only major application dependency unless a concrete requirement justifies another.

Do not use:

```text
ORMs
React
Vue
Angular
TypeScript
Tailwind
Bootstrap
Node.js build tooling
Bundlers
State management libraries
Chart libraries
Database servers
Docker as a requirement
```

Use native browser functionality when it is sufficient.

## 6. Keep the Repository Shallow

Prefer a small and obvious repository structure.

A reasonable structure is:

```text
app.py
database.py
index.html
styles.css
app.js
requirements.txt
SPEC.md
AGENTS.md
tests/
```

`finance.db` is runtime data and must not be committed.

A small shared `database.py` is acceptable for schema and financial logic.

Do not create layers such as:

```text
repositories
services
managers
providers
factories
adapters
DTO layers
```

unless an actual problem requires one.

## 7. Database Rules

SQLite is authoritative.

Use Python's built in `sqlite3`.

Enable foreign key enforcement.

Use parameterized SQL.

Use database transactions for multirow writes.

Transfers must be created, edited, and deleted atomically.

Do not store current account balances.

A balance must always be derived from `balanceOffsetCents + SUM(transaction amounts for account)`; never store the current balance.

Do not use floating point values for money.

Store money as integer cents.

Use ISO calendar dates:

```text
YYYY-MM-DD
```

Keep one shared schema initialization path.

If schema versioning becomes necessary, prefer SQLite `PRAGMA user_version` and small explicit migration functions.

Do not introduce a migration framework such as Alembic for version 1.

## 8. Business Logic Location

Important financial rules should live on the Python side.

Do not independently reimplement money calculations in multiple places.

The frontend should primarily:

```text
Collect user input
Send requests
Display returned data
Format values
Manage simple UI state
```

The backend should own:

```text
Amount sign conversion
Balance calculations
Transfers
Adjustments
Expense calculations
Rollups
Period calculations
Validation
Database writes
```

Avoid having JavaScript and Python calculate the same financial concept differently.

## 9. Transaction Integrity

Normal transactions follow the sign rules in `SPEC.md`.

Transfers always contain two linked rows whose sum is zero.

Opening balances for newly created accounts initialize `balanceOffsetCents` without creating a transaction or requiring a date. Preserve imported historical `Opening Balance` transactions.

Reconciliation updates `balanceOffsetCents` without creating a transaction or affecting income and expense reporting. Preserve historical `Balance Adjustment` transactions.

Refunds are normal categorized transactions using the defined entry sign convention.

For integration proposals, respect explicit Category Reporting Roles: positive Expense entry becomes negative ledger cents, an Expense refund uses negative entry and positive ledger cents, positive Income entry becomes positive ledger cents, and an Income reversal uses negative entry and negative ledger cents. Never infer signs from names, credit-balance wording, or projections. Preserve valid proposed fields and flag malformed values or changed Reporting Roles for human correction and explicit final approval; do not silently blank valid fields, remap IDs, or reverse signs. Keep the public agent documentation clear about these conventions. Python remains authoritative, and human review does not replace backend validation or atomicity.

Do not add a second accounting ledger within a Ledger. Preserve explicit ledger isolation; additional provisioning and sharing workflows require approved scope.

Do not add a stored balance field as a shortcut.

Do not add a transfer table unless a concrete limitation of `transferId` is demonstrated.

## 10. Coding Style

Use double quotes wherever the language permits.

Use lowerCamelCase for variables and functions.

Use PascalCase for classes when classes are genuinely useful.

Use clear descriptive names.

Do not use vague names such as:

```text
data
info
thing
stuff
temp
params
```

when the value has a specific meaning.

Add comments where they explain:

```text
Financial rules
Nonobvious sign behavior
Data integrity constraints
Why a simple looking workaround is intentional
```

Do not add comments that merely restate obvious code.

## 11. Debug Output

Include useful runtime visibility.

The application should log concise information such as:

```text
Application startup
Database path
Schema version
Critical write failures
Unexpected exceptions
```

Do not spam one log line for every normal page render.

## 14. UI Rules

The UI is deliberately plain.

Use:

```text
System fonts
Native buttons
Native selects
Native date inputs
Simple modal dialogs
Tables
Subtle borders
Normal spacing
Clear labels
```

Avoid:

```text
Gradients
Animated cards
Glass effects
Large hero areas
Decorative charts
Excessive shadows
Icon libraries
Custom dropdown implementations
Hidden gestures
Floating buttons
Unnecessary transitions
```

Do not make plain data rows appear clickable when they are not.

Account rows on the dashboard are not interactive.

Category rows on the dashboard are not interactive.

Primary actions should look like actions.

## 15. Responsive Rules

Do not build separate desktop and mobile applications.

Use the same information and workflows.

Desktop can display more columns.

Mobile can stack a row when necessary.

Recent transactions:

```text
Desktop: 5
Mobile: 5
```

Transaction entry must work comfortably on a phone.

Use appropriate native input modes for numeric fields.

Do not add a mobile framework.

## 16. Forms

Prefer one straightforward form over multistep workflows.

Do not create a wizard for transaction entry.

Do not add confirmation screens after normal successful saves.

Destructive actions may require confirmation.

Date defaults to today.

Do not remember the previous transaction category.

Do not remember the previous account unless `SPEC.md` is changed later.

Template selection may prefill account, description, category, and amount.

The user can edit all prefilled values.

## 17. Accessibility and Clarity

Use semantic HTML.

Every form control needs a clear label.

Keyboard navigation should work with normal browser behavior.

Use sufficient contrast.

Do not depend on color alone to communicate positive versus negative values.

Avoid unnecessary custom keyboard behavior.

`Enter` may submit a valid transaction form.

`Escape` may close a modal using normal browser dialog behavior.

## 18. Security

Use parameterized SQL everywhere.

Never interpolate user input into SQL strings.

When inserting user supplied text into the DOM, use safe text APIs rather than treating it as HTML.

Supported CLI setup uses independent personal ledgers, each with exactly one required Owner and multiple read-only Auditors. Each User has one membership in this release, including a Disabled membership. Preserve the ledger-aware schema and server-side isolation defined in `SPEC.md`; financial operations require authorized ledger context, and submitted IDs never grant access.

Preserve the implemented authentication, command-line user management, backend role authorization, and CSRF protection. Do not expose additional-ledger provisioning, roles, memberships, switching, or new authentication workflows without approved roadmap scope. Preserve per-ledger audit recording and atomic financial/audit writes.

Authentication does not make the built-in development server suitable for direct public internet exposure. Preserve the trusted self-hosted deployment boundary in `SPEC.md`; public access requires an external access-control and production-serving arrangement.

## 19. Testing Philosophy

Testing should focus on financial correctness rather than coverage percentages.

Use Python's built in `unittest` unless another dependency becomes clearly necessary.

Do not add a large testing framework simply for convention.

At minimum test:

```text
Cash balance calculation
Credit balance calculation
Expense sign conversion
Income sign conversion
Refund sign conversion
Transfer zero sum
Transfer atomic behavior
Opening balance behavior
Balance adjustment behavior
Expense totals
Income totals
Net calculation
Generic Expense-category rollups
Category percentages
Per day calculations
Period boundaries
```

Use historical data for integration validation where appropriate, through a read-only snapshot copied into an explicit temporary database. Never initialize, migrate, or write to the real financial database during testing. Import `tests` to enable the in-process test database guard before importing application modules, and use explicit temporary database and backup paths. If the task prohibits reading the real database, use synthetic fixtures instead.

## 20. Money Formatting

Financial math uses integer cents.

Convert to formatted dollars only at display boundaries.

Display USD with:

```text
$1,234.56
```

Normal categorized transaction history displays the Category Reporting Role's entry sign: Expense purchases and Income are positive, Expense refunds and Income reversals are negative. Uncategorized history and invalid linked rows retain the raw ledger sign. Examples of signed display amounts:

```text
+$1,519.66
-$11.73
```

Credit card dashboard balances use friendly wording from `SPEC.md`.

Never use binary floating point for financial calculation.

## 21. Date Calculations

Use calendar dates, not timestamps, for transactions.

`This Month` totals include the entire current calendar month, from its first through last day, including future-dated transactions. Its per-day divisor counts elapsed calendar days through today.

`Last Month` means the previous complete calendar month.

`Year to Date` begins January 1 and ends today.

`Last 30 Days` is a rolling 30 calendar day period.

Custom ranges are inclusive.

Per day calculations use calendar day counts specified in `SPEC.md`.

Write tests around month boundaries, leap years, and January transitions.

## 22. Archive Behavior

Archive rather than destroy historical structure.

Archived accounts, categories, and templates should disappear from normal creation selectors.

Historical transactions remain valid.

Historical filters may still reference archived accounts or categories.

Do not cascade delete financial history when archiving a management object.

## 23. Backup Behavior

Use SQLite's supported backup mechanism for a live database backup.

Do not implement backup by blindly copying an actively written SQLite file.

Keep backup UI and code simple.

No restore wizard is needed.

## 24. Performance

Do not prematurely optimize.

The initial dataset is small enough that simple SQL queries and normal HTML rendering are appropriate.

Add database indexes where they clearly support common queries, especially:

```text
transaction date
accountId
categoryId
transferId
```

Do not add caches unless profiling demonstrates a need.

Do not add background workers.

## 25. Dependency Discipline

Before adding any dependency, ask:

1. What code does this remove?
2. Is that removed code genuinely difficult or risky?
3. Does the dependency create more operational complexity than it removes?
4. Can the standard library or browser already do this?

If the dependency does not clearly simplify the system, do not add it.

## 26. Refactoring Discipline

Do not refactor unrelated working code while implementing a milestone.

Do not rename working concepts merely because another naming convention is preferred.

Do not perform architectural rewrites without an identified problem.

Prefer deleting unnecessary code over abstracting it.

## 27. Completion Report

At the end of each milestone, report:

```text
Files changed
What was implemented
Tests run
Acceptance criteria verified
Known issues
Any decision that needs user input
```

Keep the report concise.

Do not offer to continue automatically.

Stop and wait for the next milestone instruction.

## 28. Final Rule

When a simple implementation and a clever implementation both satisfy `SPEC.md`, choose the simple implementation.

Correct financial data matters more than elegant architecture.

A boring application that is easy to understand, repair, back up, and self host is the desired outcome.
