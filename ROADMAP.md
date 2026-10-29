# No Part Finance Roadmap

This file contains future product work. SPEC.md describes the implemented product.

Only Version Next is approved implementation scope. Implement one named Version Next milestone at a time. Version Soon, Version Later, and Deferred Ideas are not approved until explicitly promoted.



## Version Next

No items remain.

## Version Soon

### Account Management

Approved design for Version Soon only; implementation requires explicit promotion to Version Next. Provide self-service profile/password changes, own-session and notification-device management, and optional web password recovery. No browser system administration, other-User management, username/email editing, login enable/disable, ownership transfer, or User/ledger deletion. Preserve CLI management, ledger isolation, and the trusted deployment boundary.

#### My Account

Add My Account to existing Settings navigation and make it the default section when no explicit fragment is supplied. Preserve direct section links and Back/Forward behavior; do not split Account Settings and Ledger Settings into separate pages.

Always show the authenticated User's full profile name and username/email at least read-only. Username/email is never browser-editable; existing CLI username changes remain available. Account-wide controls concern only the signed-in User across their ledgers, including Owners, Members, Auditors, and enabled Users without ledger access. Preserve restricted verification and required-profile-completion states without granting financial access.

Add installation-wide `allowAccountSelfService`, default true, using existing platform.conf parsing, restart, sample-documentation, and secret-file rules. When false, hide name/password editing and reject direct updates server-side; retain read-only identity, sessions, notification devices, and logout. Required profile completion remains independently available.

When enabled, use separate Save name and Change password actions. Apply existing required first-name and optional last-name validation. Password change requires Current password, New password, and Confirm new password; preserve existing password validation, offline common-password rejection, hashing, Unicode/space handling, and secrecy. Keep the completing session active and invalidate other sessions for that User. Preserve existing profile-change session safeguards, stable identity, memberships, stored ledger names, financial history, historical actor labels, and atomic identity/audit writes.

#### Sessions

Show only the signed-in User's active sessions across their ledgers, with Client | Last IP | Signed in | Last active | Action. Decode user agent into browser/operating-system labels with Unknown client fallback; these labels are not verified device identities. Mark This session. Never expose usable session tokens.

Offer individual Log out and Log out other sessions. Individual revocation denies subsequent protected requests immediately; current-session logout returns to sign-in. Log out other sessions retains the requesting session. Preserve fixed 168-hour expiry without activity extension and process-local sessions ending on server restart.

Last IP reflects the latest authenticated request, including background requests, using existing configured trusted-proxy handling rather than trusting arbitrary forwarding headers. Last active reflects intentional page navigation and requests triggered by User actions, excluding automatic polling, background refreshes, and service-worker requests. Purely local interactions need not update it; do not add mouse/keystroke tracking. Viewing the session table must not mark every session active.

#### Notification Devices

Provide a separate Notification devices table with Client | Ledger | Action, individual Disable notifications, and Disable all notifications for the signed-in User. Enrollments belong to a User/browser installation/ledger and remain listed and independently removable after session expiry or server restart. Do not expose endpoints or push secrets as UI identifiers.

Explicit session logout, including Log out other sessions, also removes that browser installation's notification enrollments for the User across their ledgers. Establish explicit session-to-notification-installation association; matching IP or user-agent text is insufficient. Password-change invalidation uses the same cleanup for other sessions. Natural expiry and server restart preserve enrollments. Preserve other Users' sessions/enrollments.

Removal stops future server sends; it cannot recall already-sent notifications, erase remote content, or revoke native permission. Re-enabling requires explicit action on that device and existing role, ledger, session, CSRF, and native-permission checks. Preserve delivery-time authorization, generic payloads, post-commit delivery, and per-ledger enrollment semantics. Own-enrollment removal grants no new enrollment or financial privileges.

#### Optional Web Password Recovery and CLI Recovery Trust

Add independent installation-wide `allowWebPasswordRecovery`, default false. Enabled recovery requires enabled, valid SMTP; missing dependencies stop startup with a sanitized configuration error. Apply existing configuration/sample/restart/packaging rules. When disabled, hide Forgot password and deny direct recovery requests. Trusted CLI password reset remains available regardless of either self-service setting.

Forgot password on Sign In asks for the email username and always returns the same public response for existing, nonexistent, and ineligible accounts. Eligibility requires the current email username to have been verified by emailed code or explicitly trusted for recovery through CLI. Email-like formatting and existing manual registration acceptance alone are insufficient. Non-email usernames retain CLI recovery.

Add a CLI action to trust the selected User's current email username for recovery with explicit confirmation. Record administrator trust distinctly from mailbox verification. Bind eligibility to stable User identity and exact email username. CLI username changes clear eligibility until the new address is verified or explicitly trusted; do not infer verification from old acceptance status.

Email a short-lived single-use reset link. Collect New password and Confirm password using existing authoritative validation/hashing. Successful recovery invalidates all the User's sessions, removes their notification enrollments, and requires fresh sign-in. It never enables Disabled login, changes memberships, grants ledger access, or bypasses required registration verification/profile completion. Preserve financial history and attribution. Password update, single-use consumption, invalidation, and safe audit recording require atomic failure handling.

Reset links expire exactly 15 minutes after issuance using server-authoritative time. Require 30 seconds between recovery emails, with at most 5 send attempts per rolling 10 minutes and 20 per rolling 24 hours, enforced per account and per client IP using existing trusted-proxy address handling. Require an explicit externally reachable public application URL in platform.conf for recovery links; arbitrary request Host/forwarding values must not determine their origin. A newly issued link invalidates the previous link. Successful password changes, password resets (including CLI resets), and username changes invalidate all outstanding recovery links for that User. Require safe token storage, no token/password disclosure in logs/audits, concurrent single-use consumption, generic eligibility responses, and fake SMTP tests.

#### Acceptance

Verify defaults and disabled-state backend denial; Settings default/direct-fragment behavior; read-only identity and hidden editing; every role and no-ledger Users; name/password validation and current-password checks; completing-session continuity, own-session-only access/revocation, fixed expiry/restart behavior, trusted IP and background-activity exclusions; explicit notification-installation association, logout cleanup, preserved enrollments after expiry/restart, individual/all removal and cross-User isolation; CLI reset/trust attribution and address-change invalidation; recovery SMTP dependencies, eligibility, generic responses, expiry, rate limits, invalid/superseded/consumed links, concurrency, rollback, and disabled/verification safeguards. Preserve CSRF, server authorization, ledger isolation, safe atomic auditing, financial history, offline-access limits, and credential-free exports. Use guarded temporary databases, synthetic fixtures, and fake SMTP only.

## Version Later

These ideas preserve product direction but are not approved implementation scope. Promote and fully design one before implementation.

### Ledger Ownership Transfer

Design an explicit transfer of the sole Owner role to another User. Preserve exactly one Owner through an atomic operation and retain financial records and audit attribution. Define approval, eligibility, previous-Owner membership, disabled-account/integration behavior, session effects, and export consequences before implementation. This does not authorize Owner or ledger deletion.

### Currency, Fiscal Year, and Locale Options

Consider ledger-specific currency, fiscal-year-start, and locale configuration only after defining their actual effects. Resolve supported currency precision and financial calculations, calendar versus fiscal reporting, historical-data treatment, and date/number presentation before promotion. Do not add inert settings or silently reinterpret existing USD amounts and calendar-year reports. These remain deferred design candidates, not committed behavior.


### Automatic Backups and Production Server

Consider one consistent SQLite backup before the first successful write of each server-local day, with integrity verification, bounded retry, feature-owned retention, and a persistent failure banner that never blocks the authoritative write. Manual and migration backups remain separate. Tests use explicit temporary paths.

Replace Flask's development server with Waitress for normal startup while retaining portable Windows and Linux launch scripts and existing binding and reverse-proxy behavior.

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
