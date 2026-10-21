# No Part Finance Roadmap

This file contains future product work. SPEC.md describes the implemented product.

Only Version Next is approved implementation scope. Implement one named Version Next milestone at a time. Version Soon, Version Later, and Deferred Ideas are not approved until explicitly promoted.



## Version Next

No implementation milestones remain.

## Version Soon

### Settings Ledger Sharing: Ledger Name

Add a new Settings section for the current authorized ledger, with `Ledger Sharing` as the proposed section label pending discussion. Initially it contains a compact `Ledger name` field displaying the stored name. The Owner can edit it and explicitly save using a Save button. Other authorized roles see the same name in a disabled read-only field, without an enabled save action. Enforce Owner-only changes on the backend with existing session, CSRF, ledger-isolation, and atomic audit protections.

Naming is independent of the Owner's username: preserve the existing initial naming rule and leave a saved ledger name unchanged by later username changes. Renaming changes only the stored display name, preserving UUID, memberships, financial records, and historical audit labels. Portable ledger downloads retain the saved name. This consolidates the former Independent Ledger Naming candidate from Version Later; it remains Version Soon design scope, not approved implementation work.

Before promotion, settle the section label, name validation, save/error feedback, and acceptance criteria. This initial section does not authorize invitations, membership management, role changes, new roles, additional memberships, ownership transfer, or ledger deletion. Those remain separately scoped roadmap work.

### Ledger Sharing, Members, and Invitations

Consolidate Household Sharing and Member Permissions with Invitations and Ledger Access Management. Approved for Version Soon design scope only; implementation remains unapproved until promotion to Version Next. Remaining permission and lifecycle recommendations still require resolution, together with the separate ledger-switching and shared-download designs. Grant other existing Users access to the current authorized ledger rather than exposing other ledgers for arbitrary access.

Under Settings Ledger Sharing, below the ledger-name section, show the ledger's access list with Username, Access control level, and Options. List exactly one Owner first, visually muted with sufficient contrast and no editable role or revoke action. List only Users associated with this ledger, not an installation-wide user directory. Only the Owner manages access, roles, and invitations; other roles' access-list visibility must be settled before promotion. Support multiple Auditors and unlimited future Members. Member is more permissive than read-only Auditor. Members may create, edit, and delete ordinary transactions and transfers/payments, including events created by other Users, using existing backend validation, confirmations, atomic transfer operations, linked-Simulator integrity, and audit attribution. Members cannot rename the ledger or manage access. Resolve remaining reconciliation, move-to-Pending, planning/configuration, Pending-decision, and audit-viewing permissions explicitly before promotion; do not infer these permissions from transaction editing rights.

Provide invitation entry as a permanent hanging creation row at the bottom of the access table, consistent with the existing Simulator/integration table-entry pattern: labeled Username input, native Member/Auditor selector, and Send invitation action in the corresponding columns. Use a manually entered existing username without autocomplete or username suggestions. Successful creation clears the entry row and refreshes the pending invitation presentation; validation failures retain entered values. Only the ledger Owner may invite or revoke access; Owner invitations, registration, and ownership transfer are excluded. Invitation creation grants no access. The recipient sees a Dashboard invitation banner naming the ledger and proposed role and linking to a separate Invitations table on the existing Pending page. That table identifies the ledger, inviter, and proposed role and offers individual Accept/Decline actions. Reuse the Pending page presentation, not financial transaction rows or financial batch-approval actions. Invitations are recipient-scoped across ledgers and disclose no target-ledger financial records before acceptance; financial Pending remains separately ledger-scoped with its existing authorization and totals.

Acceptance establishes the invited membership only after authoritative recipient, invitation-state, role, and ledger checks and atomic audit recording; decline creates no membership. Owner revocation disables access immediately while preserving Users, membership identity, role history, and audit attribution. Restoring access after Owner revocation requires a new invitation and explicit recipient acceptance; reuse the retained membership rather than creating a duplicate. Do not offer a web Enable action that bypasses renewed consent. This web sharing rule is distinct from the previously approved trusted CLI re-enable operation. No sharing operation deletes financial history or credentials belonging to another User. Existing one-membership-only sign-in cannot support acceptance into another ledger: coordinate with the Multiple-Ledger Memberships and Ledger Dropdown milestone before enabling usable shared access.

Members may create Accounts and Categories, rename them, edit ordinary configuration, and archive/reactivate them under existing validation and reference protections. Owner-only operations are reconciliation, changing opening-balance offsets, changing an existing Category's hierarchy or Reporting Role, and permanent Category deletion. These restrictions must be enforced on the backend as well as in controls. Defining a new Category's role and parent during creation must preserve existing hierarchy/selectability safeguards; creation cannot bypass restrictions on changing existing Categories. Finalize the exact ordinary-configuration fields before promotion so restricted offset or historical-reporting changes cannot pass through a general edit endpoint.

Integration secrets belong to one ledger and one creating User. Members may create, list, reveal, copy, relabel, enable/disable, and revoke only their own secrets in the current authorized ledger. They cannot discover or manage another User's secrets, including that User's keys in another ledger. Owners manage all secrets within their ledger. Enforce both User ownership and ledger scoping on every credential operation; submitted IDs never grant access. Preserve secret protection, audit recording, and the human-review boundary. This does not authorize integration callers to approve proposals or grant Members financial Pending approval. Persist trusted creator attribution for Member-created secrets. Their effective authorization requires the creating Member to retain Enabled login and Enabled Member access to that ledger, in addition to the existing Owner-login gate and the key's own Enabled/non-Revoked state. Losing that access immediately blocks authentication using those keys without changing their stored individual status; another Member or Owner editing or enabling a key cannot bypass its creator-access gate. Restoration must still respect the key state and renewed access/consent rules. Preserve existing keys without inventing historical creators; finalize migration treatment and subsequent creator-role-change behavior before promotion. Database backups and database-inclusive application downloads remain Owner-only; application-only downloads remain available to authorized roles. A safe shared-membership export credential policy is still required before Owner exports can include shared Users.

Record invitation sent, cancelled, accepted, and declined events and access revoked/restored events in the affected ledger's append-only audit history. Include role-change events if role changes are included in the final sharing scope; this audit requirement does not independently authorize role-change functionality. Audit records must commit atomically with the associated state change and retain trusted actor and affected-User attribution, old/new role or access values where applicable, and enough recorded context for historical display without exposing credentials. Preserve invitation privacy: receiving an invitation alone grants no access to the inviting ledger's audit history.

Extend the existing Settings Audit Log viewer in this sharing milestone to render these sharing events readably using its existing Date/time, User, Action, Type, Old value, and New value columns and system timestamp behavior. Display recorded identities and context without reconstructing missing historical values. These remain ledger-scoped events, not a platform-wide User directory or an access-management UI. Verify authorized visibility, recipient isolation before acceptance, immediate loss of viewing access after revocation, readable lifecycle events, historical attribution, and audit/state rollback together.

Unresolved design decisions: remaining role permission matrix and exact editable Account/Category fields; safe Owner export credentials for Users with memberships elsewhere; legacy-key attribution and creator-role-change effects; exact access-list visibility and role-change workflow; username lookup privacy and invitation abuse limits; duplicate/self/already-member/disabled-user handling; expiry, cancellation, reinvitation, and stale/concurrent decisions; recipient Pending-page access for Auditor/Member and no-active-ledger cases; invitation-specific counts/banner behavior; pagination; and switching/draft/session behavior. No unrelated-ledger User/password hashes may be exported. Current shared-membership export refusal remains until an explicit replacement policy is approved.

Before promotion, finalize these decisions and acceptance criteria covering backend role authorization, immediate revocation, ledger isolation, no pre-acceptance access, recipient privacy, retry/concurrency safety, atomic rollback/auditing, migration preservation, credential-safe exports, and unchanged financial Pending behavior. Tests must use guarded temporary databases and synthetic records only.

### Multiple-Ledger Memberships and Ledger Dropdown

Approved for Version Soon; implementation remains unapproved until promotion to Version Next. Allow one login to own a ledger and be a Member or Auditor of others, and to own multiple independent ledgers. Coordinate supported roles and accepted invitations with the Ledger Sharing milestone. Preserve exactly one Owner per ledger and one membership per User-Ledger pair.

Place a labeled native Ledger dropdown in the shared top bar, on a row below the primary navigation links. List only the current User's accessible ledgers using their stored names; names never authorize access. When exactly one ledger is accessible, show its name as plain text in the same position. With multiple accessible ledgers, selecting an option immediately requests a switch without an Enter, Apply, or Go button.

Switching preserves the current application section rather than always returning to Dashboard. Reload that section using the destination ledger's records and permissions; reset previous-ledger record selections and financial state. If the destination role cannot access the section, return to Dashboard with a brief explanation. Confirm before discarding unsaved changes; cancellation preserves the original ledger and draft. Separate browser tabs retain independent ledger selections. A failed switch keeps the original ledger selected and shows an error; do not present the destination as active before authorization succeeds.

At first sign-in, select the User's owned ledger; if none exists, select the first accessible ledger alphabetically. If a User owns multiple ledgers, define a deterministic choice among them before implementation rather than relying on database row order. Refresh preserves the selected ledger. Browser Back/Forward restores the appropriate ledger and section, rechecking authorization. If selected access is revoked or disabled, block that ledger and offer remaining accessible choices. A User with no accessible ledgers receives no financial access; coordinate an invitation-only state with the sharing design.

Verify target membership server-side on every request. An in-flight response, stale form, cached state, URL, or another tab must never cause records to be read or written under the wrong ledger. Switching must not leak financial records, settings, credentials, permissions, or planning state. Verify immediate switching, section preservation and fallback, single-ledger display, first-sign-in selection, refresh and Back/Forward, draft confirmation/cancellation, failed switches, independent tabs, concurrent requests, and revoked/disabled access using guarded temporary databases and synthetic records only. Shared-ledger portable downloads remain a separate design discussion.

### Shared-Ledger Portable Downloads

Extend ledger-scoped backups and database-inclusive application packages for shared ledgers and Users with memberships elsewhere. Keep database-inclusive web downloads Owner-only. Include only the selected ledger's Owner, Members, and Auditors and their memberships in that ledger; exclude other ledgers and memberships even when an included User has access elsewhere. Preserve financial records, ledger identity/name, roles, administrative and membership-access status, retained User identity, and historical audit attribution. Application-only downloads contain no User records or credentials.

Both Database Backup and Download with database must omit all source password hashes and integration secret values, without a configuration setting or download override. Do not introduce platform.conf for credential export. Include User records and selected-ledger memberships but prevent every included User, including the Owner, from signing in until a new password is explicitly set through trusted CLI user management on the downloaded installation. Never provide a default or blank usable password. Password reset clears only the credential-setup requirement; it must not enable a Disabled login or membership. Browser password setup/reset remains future scope.

Integration secret values must be absent from delivered files. Preserve non-secret integration metadata and attribution needed by Pending submissions and audit history without leaving a usable credential or generating a replacement automatically. Existing exported key records without secrets cannot authenticate or become usable through Enable; use a non-authenticating representation that preserves referenced metadata and permits fresh-key creation without restoring exported secret values. The former disable-keys-in-downloaded-copy option is superseded for these web exports because no exported key retains a secret. Exclude Pending sharing invitations from ledger-scoped downloads because recipient acceptance has not established a membership. Do not export recipient-only User records merely because an invitation exists, create memberships from invitations, or permit invitation acceptance in the copy to grant access. Sharing must be invited again on that independent installation. Preserve historical invitation audit attribution without retaining actionable Pending invitations. Existing financial Pending transactions/transfers remain included under their separate financial export rules.

Build the deliverable database in fresh pages from an authorized consistent snapshot, omitting credentials during population. Never copy a complete database and merely delete hashes/secrets: SQLite pages may retain removed values. Ensure no source password hashes or integration secret values survive in the delivered database, application ZIP, sidecars, configuration, or exported audit/proposal fields. Source credentials, sessions, memberships, financial history, and audit records remain unchanged. The copy authenticates independently and grants no access to the source.

This changes ledger-scoped web downloads into credential-free portable copies that require CLI password setup. Command-line full-installation packaging and migration backups retain their existing separate recovery behavior; do not imply this policy sanitizes those artifacts. Preserve consistent snapshots, foreign-key/integrity verification, historical attribution, rollback/failure cleanup, and standalone portability. Verify credential absence, all-role login blocking until CLI reset, preserved administrative/membership restrictions, nonfunctional exported integration keys, selected-ledger-only contents, source-instance isolation, and no partial exports using guarded temporary databases and synthetic records only. Approved for Version Soon; implementation remains unapproved until promotion to Version Next.

Local credential setup must work after transfer through either database-inclusive application ZIP or database-only download. Reuse trusted CLI user management to set passwords for retained Users without requiring an old password. Present credential-setup requirement distinctly from administrative login status and ledger membership access. A successful reset permits sign-in only when the other access checks pass, without requiring a server restart. Other Users remain blocked until their own passwords are set. The reviewed command-line walkthrough is illustrative only: do not prescribe exact commands, menu numbering, prompts, startup messages, or ordering beyond the existing supported entry points and these behavioral requirements.

## Version Later

These ideas preserve product direction but are not approved implementation scope. Promote and fully design one before implementation.

### Web Registration and Account Management

Bring registration and relevant CLI user-management workflows into the web UI. Design registration eligibility (open, invited, or administrator-provisioned), initial Owner/ledger creation, username and password management, and administrative enable/disable permissions before implementation. Do not assume a new installation-administrator role or unrestricted public registration. Preserve ledger isolation, authentication controls, and the trusted deployment boundary.

### Optional platform.conf — Deferred

Defer the optional installation configuration-file idea until a concrete separate use is designed and approved. No file syntax, settings, download-credential override, or implementation is approved. Ledger-scoped web downloads use the explicit credential-exclusion policy in the shared-download design instead.

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
