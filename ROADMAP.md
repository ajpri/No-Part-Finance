# No Part Finance Roadmap

This file contains future product work. SPEC.md describes the implemented product.

Only Version Next is approved implementation scope. Implement one named Version Next milestone at a time. Version Soon, Version Later, and Deferred Ideas are not approved until explicitly promoted.



## Version Next

### Optional platform.conf, Email Usernames, and Account Names

Approved for Version Next; implementation begins only when explicitly requested.

Restore optional installation-wide `platform.conf`. Missing files, omitted settings, and commented-out settings use documented defaults. Read configuration at startup; editing the file requires a server restart before changes apply. Use a plain `key = value` syntax alongside the application, with `#` comment lines and `enforceEmailUsernames = false` as the documented example. An invalid supplied value stops startup with a clear message rather than silently falling back. Keep this separate from per-ledger settings. Do not add unrelated configuration settings or change database/credential exclusion rules for portable web downloads.

Treat the real `platform.conf` as server-side secrets even if its initial setting is not itself confidential. Never commit it, serve its contents through browser/static/API routes, or include it in any browser-generated package, with or without a database. CLI packaging and Windows/Linux packaging shortcuts may include it when present as part of the full installation package. Browser packages start with configuration defaults. Never log file contents or configuration values; startup feedback may identify an invalid setting without echoing its contents. Verify exclusions against nested/archive inclusion paths as well as ordinary file lists, using synthetic configuration fixtures.

The first setting is `enforceEmailUsernames`, displayed/described as `Enforce Email Usernames`. Its default is `false`. When false, preserve existing username rules. When true, require email-like usernames for account setup and username changes. This is a username-format requirement, not email ownership verification or email delivery. Email-like validation requires exactly one `@`, no whitespace, a nonempty local part, and a dotted domain with nonempty labels, such as `example.com`. Retain the existing username maximum length. Preserve installation-wide normalized username uniqueness and identify Users by their existing stable IDs.

After enforcement is enabled and the server restarted, an existing User whose username does not meet the requirement signs in using their existing username and password, then receives a username-migration dialog at their next successful login. Compliant usernames do not require username migration. Require username migration to complete before ordinary application access; the limited authenticated migration state permits only the necessary migration operations and logout, with server-side enforcement. Validate the replacement on the server and preserve entered fields with clear feedback on invalid or already-used usernames. Migration must not create another User, change ownership or memberships, reassign integration creators, or alter financial history. Preserve historical audit attribution and atomically record the account change.

Collect a required first name and optional last name during account setup. Prompt every existing User missing the required first name at their next successful login, including when email enforcement is false; include the required first-name and optional last-name fields in the same migration dialog when username migration is also required. Require completion of the first name before ordinary application access. An intentionally blank last name is valid and does not trigger repeated migration prompts. Trim surrounding whitespace, require a first name of 1–40 characters, and allow a last name of 0–40 characters. Allow Unicode names, spaces, hyphens, and apostrophes; reject control characters. Change the initial default ledger name from the username-based format to `<First Name> <Last Name>’s Finances`, for example `Jane Smith’s Finances`; omit the last name and joining space when it is blank, producing `Jane’s Finances`. Use the User's preferred possessive convention on the individual's full displayed name: when the name ends in s (case-insensitive), append only `’`, producing `James’ Finances` and `James Jones’ Finances`; otherwise append `’s`, producing `Jane Smith’s Finances`. Do not insert a space before the apostrophe or infer a plural family name from an individual's surname. Preserve apostrophes within names, for example `Jane O’Neill’s Finances`. Names are account-wide profile fields; the ledger name remains independently stored and Owner-editable. Preserve existing ledger names unless a separate migration-renaming decision is approved. General Account Management and Web Registration are not implicitly promoted by this item.

Extend the existing CLI user-management workflow to collect the required first name and optional last name when creating accounts and to set/update those fields for existing Users, allowing account setup or required profile completion through the CLI. Apply the same validation and configured email-username policy to CLI account creation and username changes. CLI completion satisfies the corresponding browser migration requirements at the next login; a noncompliant username still requires correction while email enforcement is enabled. Preserve stable User identity, historical audit labels, existing ledger names, and atomic account/audit writes. This does not promote the broader web Account Management milestone.

On successful browser migration, keep the completing login active and invalidate the User's other sessions. CLI identity/profile changes require a fresh login. Apply these effects atomically with the account changes while preserving password credentials and existing authorization rules.

When email-username enforcement is enabled, require at least 6 characters overall, allowing short email-like usernames such as `a@b.co`. Do not apply this email-specific minimum to unrestricted usernames when enforcement is false.

Acceptance must cover absent/partial/commented configuration, invalid supplied values, browser/static/API non-disclosure and all browser-package exclusions, CLI/shortcut inclusion, restart-only changes, false-to-true and true-to-false changes, compliant/noncompliant existing usernames, email-format and length boundaries, unique replacement validation, enabled/disabled login, session invalidation and completing-session continuity, required first-name completion, optional/blank last names without repeat prompts, CLI creation/profile updates and migration completion, full-name and first-name-only default ledger naming, singular possessives including names ending in s and names containing apostrophes, preserved existing identities/credentials/memberships/ledger names/history, CSRF and migration access restrictions, atomic account/audit writes, and guarded synthetic-only tests.

### Trusted Reverse Proxy Support

Approved for Version Next application scope; implementation begins only when explicitly requested. Depends on the optional `platform.conf` foundation. Configuring pfSense, NGINX, routing, certificates, firewalls, or other external infrastructure is the User's personal work and is outside application scope.

Add optional installation-wide trusted-proxy address configuration in `platform.conf`, defaulting to no trusted proxies when absent, omitted, or commented out. Apply changes only after server restart and retain all existing server-secret and package-exclusion rules. Configure the intended reverse proxy addresses explicitly; do not automatically trust every private-network or loopback caller.

Only honor forwarded client IP and original HTTP/HTTPS scheme when the actual directly connected peer is a configured trusted proxy. Support the single HTTP reverse-proxy arrangement discussed: pfSense does port forwarding and NGINX forwards HTTP requests to the application. Treat only the configured nearest proxy's appended/overwritten forwarding values as trusted; do not accept an arbitrary client-supplied leftmost address. Ignore forwarded headers from untrusted peers and retain normal direct-connection behavior. Validate forwarded addresses and schemes, and preserve the actual peer address for the trust decision. This scope does not add forwarded-host, forwarded-prefix, arbitrary proxy-chain discovery, or a new hosting architecture.

Use the resolved client IP consistently for registration and existing per-IP sign-in throttling without changing their approved limits. Honor the trusted original HTTPS scheme for existing secure-cookie/request behavior. Forwarded headers do not grant identity, roles, ledger access, or exemptions from authentication and CSRF.

Acceptance covers configuration defaults and restart behavior; trusted and untrusted peers; direct access; spoofed forwarding headers and attacker-supplied extra chain entries; valid IPv4/IPv6 addresses; malformed address/scheme handling; independent outside-client rate-limit buckets behind a shared proxy; original HTTPS secure-cookie behavior; preserved authentication/ledger isolation; and guarded synthetic request/configuration fixtures without modifying external infrastructure.

### Web Registration

Design in progress at the User's request; not yet approved for implementation. This is a separate milestone depending on the approved platform configuration, username, and account-name rules above.

Add installation-wide `allowWebRegistration` to the optional server-side `platform.conf`, defaulting to `false` when the file or setting is missing or the setting is commented out. Apply changes only after a server restart, using the same parsing, invalid-value handling, secret-file protection, and CLI-versus-browser packaging rules as the configuration milestone.

When enabled, show a `Register` link on the sign-in page. Clicking it opens one accessible native modal collecting Username, required First name, optional Last name, Password, and Confirm password. Require matching password and confirmation values on the server; never store or audit the confirmation. Apply the approved `enforceEmailUsernames` behavior, including the 6-character email-like minimum when enabled, installation-wide case-insensitive username uniqueness, and the approved profile-name validation. Use the existing password rules: 8–128 characters, ordinary Unicode and spaces allowed, with no trimming or truncation. Preserve entered non-password fields with accessible validation feedback. Cancellation creates no records. Do not add a wizard.

When disabled, hide the registration link and deny direct registration submissions server-side; hiding the link alone is insufficient. Registration eligibility comes from the server configuration, never a submitted role, User ID, ledger ID, or browser flag. New registrants receive no access to other Users' ledgers or installation-administrator privileges. Existing sign-in, CLI account creation, sharing invitation eligibility, and enabled/disabled User safeguards remain authoritative. Email verification is specified separately in the Web Registration Verification Codes milestone below. Do not add password recovery or general web Account Management implicitly.

Enforce server-side validation, CSRF protection, secure password hashing, and atomic account/profile and any approved ledger/membership/audit creation. Protect passwords from logs, audit snapshots, and responses. Retain the trusted self-hosted deployment boundary; this setting does not itself establish a public hosting arrangement.

After successful registration and any required email verification, sign the new User in automatically using the existing browser-session protections, then explicitly ask `Would you like to create your own ledger?` with Yes/No choices. Never create a ledger automatically as a side effect of registering. Yes opens the existing Create Ledger form with the approved name-based default suggested and editable; explicit creation uses the existing atomic ledger/Owner/External Account/audit workflow. No leaves the User without a ledger, with the existing No ledger access state and Create Ledger action available for later use. Registration remains successful if the User declines, cancels creation, or ledger creation fails.

Apply registration limits per client IP address: at most 5 attempts per rolling 10 minutes and 20 attempts per rolling 24 hours. Count successful and failed admitted registration submissions; opening or cancelling the modal does not count. Enforce both limits atomically on the server and persist counters across server restarts. Once either limit is reached, reject further registration attempts with clear retry feedback before account creation or credential hashing; blocked retries do not extend the cooldown. Use the server's established trusted client-address handling rather than accepting spoofable forwarding headers directly. These limits do not change sign-in or invitation limits.

Decision still to settle:

- Compatibility with fresh-install initial Owner setup.

Acceptance must cover enabled/disabled/missing/commented configuration and restart behavior; link/modal accessibility; direct-request denial when disabled; username/email/name/password boundaries and duplicate-user races; cancellation and validation feedback; CSRF and request throttling; no supplied role or ledger escalation; atomic rollback and audit secrecy; the approved initial-ledger and post-registration sign-in behavior; fresh-install compatibility; and guarded synthetic-only tests.

### Optional SMTP Configuration

Design in progress at the User's request; not yet approved for implementation. This is a separate milestone depending on the approved optional `platform.conf` foundation.

Add optional installation-wide SMTP server and authentication settings to server-side `platform.conf`. SMTP is unconfigured by default. Add a separate master `smtpEnabled` switch defaulting to `false` when missing or commented out, including when the file itself is absent. Merely entering SMTP connection or authentication settings never enables email. With the master switch off, make no SMTP connection or email delivery attempt. Apply edits only after restarting the server.

Configuration covers the SMTP server host, port, transport security, authentication username/password, sender email address, and optional sender display name. Keep authentication directly in the protected configuration file as requested, with no browser credential editor. Apply the previously approved server-secret treatment: never commit the real file, expose its contents through browser/static/API routes, include it in any browser-generated package, or log its contents or values. CLI installation packaging and Windows/Linux packaging shortcuts may include it when present. Never copy SMTP credentials into ledger data, audit snapshots, or browser responses. Examples/documentation use placeholders only.

Support both STARTTLS and TLS-on-connect, selected explicitly through configuration, with certificate and hostname validation required in both modes. Allow SMTP relays without credentials: when both authentication username and password are unset, connect without attempting SMTP authentication. When credentials are supplied, require both username and password and authenticate; reject partial credentials rather than silently treating them as an unauthenticated relay. Never fall back to unauthenticated delivery after authentication fails. Do not fall back to an unencrypted connection when TLS negotiation or validation fails. Enabling SMTP requires valid complete connection and sender settings for the chosen transport/authentication mode; do not silently treat unusable enabled SMTP as operational. Use the shared configuration validation and sanitized startup feedback. The master switch governs every application SMTP email use. Configuring SMTP does not by itself approve registration verification, password reset, invitations by email, summaries, automated notifications, a test-email UI, queues, workers, or delivery retries; define any actual sending workflows separately.

Decisions still to settle:

- Exact configuration option names and default ports.
- No further SMTP sending workflow is implied beyond the separately specified Web Registration Verification Codes milestone below.

Acceptance must cover missing/partial/commented configuration; explicit enable/disable and restart-only behavior; no SMTP activity while disabled; invalid/incomplete enabled settings and sanitized errors; supported transport/authentication validation; configuration secrecy and all browser-download exclusions versus CLI/shortcut inclusion; no credential copying into database/audit/browser responses; and synthetic configuration fixtures with fake SMTP endpoints only.

### Web Registration Verification Codes

Approved for Version Next, including code expiry and retry/resend limits, restricted pending-account login, email-username migration verification, CLI acceptance/bypass, and assumed acceptance for generated packages and trusted backup restores. Implementation begins only when explicitly requested. Depends on Web Registration, SMTP configuration, and the optional platform configuration/username foundation. This is the first explicitly requested SMTP email workflow. Resolve the remaining operational decision listed below before implementing the affected behavior.

Add a separate installation-wide `requireRegistrationVerification` boolean in server-side `platform.conf`, defaulting to `false` when missing or commented out, including when the file is absent. Apply changes only after restart and preserve the approved configuration-secret and packaging rules. Treat these as a dependency chain: web registration must be enabled for browser account creation; within that flow, required email-code verification is active only when `enforceEmailUsernames`, configured/enabled SMTP, and `requireRegistrationVerification` are all enabled. An inactive prerequisite makes the verification setting inactive without automatically enabling another setting or blocking otherwise-allowed registration; use the normal non-verification registration flow instead. Retain inactive configured values so they take effect after the prerequisites are enabled and the server restarted. This does not relax the SMTP milestone's validation of invalid or incomplete settings when SMTP itself is enabled. Existing accounts are not retroactively required to verify solely because this setting changes.

When active, generate a cryptographically random six-digit decimal code, preserving leading zeros, and email it to the submitted email username. Use plain text only, with subject `No Part Finance- your code is {code}` and a basic body such as `Welcome to No Part Finance. Your account has been created. Your verification code is {code}.` Use the configured sender; do not add HTML templates, attachments, tracking, or marketing content. SMTP's master switch remains authoritative.

Collect the code in an accessible verification dialog after the registration form. Keep the new account pending verification and prevent normal authenticated application access, ledger creation, invitation acceptance, and financial access until the required verification succeeds or the trusted CLI explicitly accepts the account. Before completion, permit only the narrowly necessary verification/recovery operations and logout/cancellation. Completing verification continues the approved automatic-sign-in flow and optional ledger-creation question. Wrong or expired codes must not accept an account. Consume a successful code once, atomically with the account's acceptance, and preserve stable User identity.

Expire each code exactly 10 minutes after issuance using server-authoritative UTC time. Permit resend only after 30 seconds, with at most 5 send attempts per rolling 10 minutes and 20 per rolling 24 hours, enforced per account and per resolved client IP and including the initial email. Five wrong-code submissions trigger a 10-minute verification cooldown. Resend, fresh browser sessions, sign-out/sign-in, and server restart do not reset failed-attempt counters or bypass cooldowns. Persist challenge expiry, consumption, supersession, send limits, and verification cooldowns. Any replacement code invalidates the previous code.

An existing pending User may submit their correct username and password to resume a restricted verification session. Show `Your account hasn’t been verified yet. Enter the code emailed to you to continue.` with code entry, Resend, and Log Out. Authorize only the necessary verification operations; block ordinary browser/API access, ledger creation, invitation acceptance, and financial operations server-side. Do not automatically send another code on every login; require an admitted resend action. Expiry of a code does not delete the account. Successful verification completes the normal login flow.

Extend the same code-verification flow to browser migration from an existing username to an email-like username when the email-verification prerequisites are active. Email the code to the proposed new username and require successful verification before completing the migration or granting normal application access. This migration flow remains available even when new web registration is disabled; `allowWebRegistration` controls new account creation, not existing-user migration. Bind the challenge to the stable User and exact proposed address, revalidate uniqueness on completion, and update the username/profile and consume the challenge atomically. Preserve the prior username until migration completes; changing the proposed address invalidates its previous challenge. Reuse approved expiry, send/attempt limits, and CLI manual acceptance/bypass rules. Use context-appropriate plain text for migration rather than falsely announcing a newly created account.

CLI account creation may explicitly skip the code/email step. Extend CLI user management to find pending accounts, including those with no ledger membership, and manually accept a selected account. Record trusted manual acceptance/bypass as such rather than claiming email ownership was proven. Acceptance does not override administrative login disablement, grant installation-administrator privileges, or grant access to another ledger. Do not provide a browser button that bypasses required verification.

Do not log or audit verification codes, include them in browser API responses, or store plaintext codes in the database. Store only an appropriate protected verifier plus the challenge metadata needed for expiry, consumption, and attempt limits. Never send the email inside an uncommitted database write; handle delivery failure without accepting the account or claiming delivery succeeded. Keep account/challenge state consistent across failed or concurrent verification and CLI acceptance. Any resend must invalidate the superseded code and remain subject to server-side limits. Do not introduce a delivery queue, worker, or automatic retry service.

Treat accounts included in generated/downloaded packages and accounts activated through a trusted database-backup restore as already accepted for the email-verification gate, as explicitly approved. Record this as assumed acceptance for packaging/restore rather than claiming mailbox ownership was proven. Apply it only to included/restored accounts and invalidate their outstanding verification challenges; do not change the source installation's live account acceptance merely because a package or backup was created. Preserve exact SQLite backup snapshots and apply restore acceptance through an explicit trusted restore path, not by mutating the backup or accepting all pending accounts during ordinary startup. This does not add a browser restore wizard. Existing password-exclusion/setup requirements, disabled-login/access states, memberships, ledger isolation, credential exclusions, and platform.conf browser-package exclusions remain authoritative. Assumed email acceptance grants no new roles or ledger access and does not waive email-format/name migration requirements.

Decision still to settle:

- Pending-account lifetime/username reservation and the effect of switching verification off on already-pending accounts.

Acceptance must cover configuration combinations and restart behavior; six digits including leading zeros; plain-text subject/body and correct recipient; verified versus pending access; automatic sign-in and optional ledger flow only after acceptance; incorrect/expired/consumed/superseded codes; resend/attempt limits and persistence; SMTP failure; email-username migration verification including when web registration is disabled; unchanged original username until verified, new-address uniqueness races, and challenge/address binding; CLI bypass/manual acceptance including no-ledger accounts; assumed acceptance in generated packages and trusted database restores with preserved source state, exact backups, credential exclusions, password setup and disablement; administrative disablement; atomic concurrent acceptance; password/code secrecy and preserved history; and guarded synthetic fixtures/fake SMTP without real email delivery.

## Version Soon

No items remain.

## Version Later

These ideas preserve product direction but are not approved implementation scope. Promote and fully design one before implementation.

### Account Management

Bring relevant CLI user-management workflows into the web UI, including username and password management and administrative enable/disable operations. Design authorization, self-service versus administrative actions, credential/session effects, and cross-ledger User implications before implementation. Do not assume a new installation-administrator role. Preserve ledger isolation, authentication controls, and the trusted deployment boundary. Web Registration remains separately scoped in Version Soon.

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
