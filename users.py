"""Trusted local user management. Passwords never appear in output or audit values."""

import getpass
import json
import sqlite3
import secrets
import time
import math
import unicodedata
import warnings
from uuid import uuid4
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from database import (
    ValidationError, connectDatabase, bindInitialLedger, bindLedger, requireLedger,
    createExternalSystemAccount,
    localTimestamp,
)


def validateUsername(username, enforceEmail=False):
    if not isinstance(username, str):
        raise ValidationError("Username is required")
    username = username.strip()
    if not 1 <= len(username) <= 64 or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in username):
        raise ValidationError("Username must contain 1 through 64 characters without control characters")
    if enforceEmail and not emailLikeUsername(username):
        raise ValidationError("Username must be an email address of 6–64 characters, such as a@b.co")
    return username


def emailLikeUsername(username):
    if not isinstance(username, str) or not 6 <= len(username) <= 64 or username.count("@") != 1 or any(character.isspace() or unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in username):
        return False
    localPart, domain = username.split("@")
    return bool(localPart) and "." in domain and all(domain.split("."))


def validateProfile(firstName, lastName=""):
    if not isinstance(firstName, str) or not isinstance(lastName, str):
        raise ValidationError("First and last names must be text")
    firstName, lastName = firstName.strip(), lastName.strip()
    if not 1 <= len(firstName) <= 40 or len(lastName) > 40 or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in firstName + lastName):
        raise ValidationError("First name requires 1–40 characters; last name allows 0–40. Control characters are not allowed.")
    return firstName, lastName


def defaultLedgerName(firstName, lastName=""):
    firstName, lastName = validateProfile(firstName, lastName)
    fullName = " ".join(part for part in (firstName, lastName) if part)
    return fullName + ("’" if fullName.lower().endswith("s") else "’s") + " Finances"


def configuredUsername(connection, username):
    return validateUsername(username, getattr(connection, "configuration", {}).get("enforceEmailUsernames", False))


def passwordHash(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        raise ValidationError("Password must contain 8 through 128 characters")
    commonPasswords = set((Path(__file__).resolve().parent / "common-passwords.txt").read_text(encoding="utf-8").splitlines())
    if password.casefold() in commonPasswords:
        raise ValidationError("That password is commonly used. Choose a less predictable password or passphrase.")
    return generate_password_hash(password, method="scrypt")


def insertUser(connection, role, username, hashedPassword, firstName, lastName):
    """Caller holds the provisioning write transaction and explicit ledger context."""
    releaseExpiredRegistrations(connection, username, int(time.time()))
    if connection.execute("SELECT 1 FROM users WHERE usernameKey = ?", (username.casefold(),)).fetchone():
        raise ValidationError("That username is already configured")
    if role == "owner" and connection.execute("SELECT 1 FROM scopedusers WHERE role = 'owner'").fetchone():
        raise ValidationError("That Owner is already configured")
    if role == "auditor" and not connection.execute("SELECT 1 FROM scopedusers WHERE role = 'owner'").fetchone():
        raise ValidationError("Create the ledger Owner before adding Auditors")
    userId = connection.execute(
        "INSERT INTO users (username, usernameKey, passwordHash, firstName, lastName, acceptanceMethod) VALUES (?, ?, ?, ?, ?, 'cli-bypass')",
        (username, username.casefold(), hashedPassword, firstName, lastName),
    ).lastrowid
    connection.execute("INSERT INTO ledgerAccess (userId, ledgerId, role) VALUES (?, currentLedgerId(), ?)", (userId, role))
    if role == "owner":
        connection.execute("UPDATE ledgers SET name = ? WHERE id = currentLedgerId()", (defaultLedgerName(firstName, lastName),))
    recordUserChange(connection, userId, "create", None, userSnapshot(connection, userId))
    return userId


def createUser(connection, role, username, password, firstName="", lastName=""):
    """Create a new login in an explicitly selected ledger (including initial setup)."""
    requireLedger(connection)
    if role not in {"owner", "auditor"}:
        raise ValidationError("User role is invalid")
    username = configuredUsername(connection, username)
    firstName, lastName = validateProfile(firstName, lastName)
    hashedPassword = passwordHash(password)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        return insertUser(connection, role, username, hashedPassword, firstName, lastName)


def createOwner(connection, username, password, firstName="", lastName=""):
    """Provision a separate ledger; only first setup claims the initial unowned ledger."""
    username = configuredUsername(connection, username)
    firstName, lastName = validateProfile(firstName, lastName)
    ledgerName = defaultLedgerName(firstName, lastName)
    hashedPassword = passwordHash(password)
    previousContext = (connection.ledgerId, connection.actorKind, connection.actorId, connection.actorLabel)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            ledgers = connection.execute("SELECT id FROM ledgers").fetchall()
            initial = len(ledgers) == 1 and not connection.execute("SELECT 1 FROM ledgerAccess WHERE role = 'owner'").fetchone()
            if initial:
                ledgerId = ledgers[0]["id"]
            else:
                ledgerId = str(uuid4())
                connection.execute("INSERT INTO ledgers (id, name) VALUES (?, ?)", (ledgerId, ledgerName))
            bindLedger(connection, ledgerId)
            if not initial:
                connection.execute("""INSERT INTO auditEvents
                    (ledgerId, changedAt, actorKind, actorId, actorLabel, action, recordType, recordId, afterValues)
                    VALUES (currentLedgerId(), strftime('%Y-%m-%dT%H:%M:%fZ','now'), auditActorKind(), auditActorId(), auditActorLabel(), 'create', 'ledgers', ?, ?)""",
                    (ledgerId, json.dumps({"name": ledgerName})))
                createExternalSystemAccount(connection)
                # Other defaults are intentionally absent settings, as on a fresh install.
            userId = insertUser(connection, "owner", username, hashedPassword, firstName, lastName)
            return userId
    finally:
        connection.ledgerId, connection.actorKind, connection.actorId, connection.actorLabel = previousContext


def userSnapshot(connection, userId):
    user = connection.execute("""SELECT u.id, u.username, u.firstName, u.lastName, u.authVersion,
        u.enabled AS loginEnabled, u.verificationStatus, u.acceptanceMethod, u.registrationExpired,
        a.role, a.enabled AS accessEnabled FROM users u LEFT JOIN ledgerAccess a
        ON a.userId = u.id AND a.ledgerId = ? WHERE u.id = ?""", (connection.ledgerId, userId)).fetchone()
    if not user:
        raise ValidationError("User does not exist")
    return dict(user)


def recordUserChange(connection, userId, action, before, after):
    # No-ledger registration/verification needs an installation audit trail too.
    # Snapshots are explicit allowlists; credentials and code verifiers never enter it.
    connection.execute("""INSERT INTO userEvents
        (userId, changedAt, actorKind, actorId, actorLabel, action, beforeValues, afterValues)
        VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), auditActorKind(), auditActorId(), auditActorLabel(), ?, ?, ?)""",
        (userId, action, json.dumps(before) if before else None, json.dumps(after) if after else None))
    if not hasattr(connection, "pendingUserLogs"):
        connection.pendingUserLogs = []
    username = (after or before or {}).get("username", "Unknown user")
    safeUsername = "".join(character if unicodedata.category(character) not in {"Cc", "Cf", "Cs"} else " " for character in username)
    outcome = {
        "create": "account created through the trusted CLI without an email code",
        "registration": "account registered",
        "rename": "username changed; fresh sign-in required",
        "profile-update": "first/last name updated; other sessions ended",
        "password-reset": "password reset; previous sessions ended",
        "registration-expired": "unverified registration expired; a new registration is needed",
        "email-verified": "email verification completed",
        "verification-cli-bypass": "verification accepted through the trusted CLI; mailbox ownership was not proven",
        "verification-policy-bypass": "account accepted because email verification is disabled; mailbox ownership was not proven",
    }.get(action, action.replace("-", " ") + " completed")
    connection.pendingUserLogs.append(f"User {safeUsername} (ID {userId}): {outcome}.")
    membershipOnly = action in {"access-enable", "access-disable", "create"}
    memberships = connection.execute("SELECT ledgerId, role, enabled FROM ledgerAccess WHERE userId = ?" + (" AND ledgerId = ?" if membershipOnly else ""), (userId, connection.ledgerId) if membershipOnly else (userId,)).fetchall()
    for membership in memberships:
        def snapshotForMembership(snapshot):
            return None if snapshot is None else {**snapshot, "role": membership["role"], "accessEnabled": membership["enabled"]}
        connection.execute("""INSERT INTO auditEvents
            (ledgerId, changedAt, actorKind, actorId, actorLabel, action, recordType, recordId, beforeValues, afterValues)
            VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), auditActorKind(), auditActorId(), auditActorLabel(), ?, 'users', ?, ?, ?)""",
            (membership["ledgerId"], action, str(userId), json.dumps(snapshotForMembership(before)) if before else None, json.dumps(snapshotForMembership(after)) if after else None))


def resetPassword(connection, userId, password):
    hashedPassword = passwordHash(password)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        before = userSnapshot(connection, userId)
        connection.execute("UPDATE users SET passwordHash = ?, passwordSetupRequired = 0, authVersion = authVersion + 1 WHERE id = ?", (hashedPassword, before["id"]))
        recordUserChange(connection, userId, "password-reset", before, userSnapshot(connection, userId))


def renameUser(connection, userId, username):
    username = configuredUsername(connection, username)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        before = userSnapshot(connection, userId)
        if before["registrationExpired"]:
            raise ValidationError("Expired registrations cannot be changed")
        releaseExpiredRegistrations(connection, username, int(time.time()))
        if connection.execute("SELECT 1 FROM users WHERE usernameKey = ? AND id != ?", (username.casefold(), userId)).fetchone():
            raise ValidationError("That username is already configured")
        if before["username"] == username:
            return
        connection.execute("UPDATE users SET username = ?, usernameKey = ?, authVersion = authVersion + 1 WHERE id = ?", (username, username.casefold(), userId))
        connection.execute("DELETE FROM verificationChallenges WHERE userId = ?", (userId,))
        recordUserChange(connection, userId, "rename", before, userSnapshot(connection, userId))


def setAuditorAccess(connection, userId, enabled):
    if type(enabled) is not bool:
        raise ValidationError("Access status must be Enabled or Disabled")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        before = userSnapshot(connection, userId)
        if before["role"] != "auditor":
            raise ValidationError("Owner membership cannot be disabled")
        if before["accessEnabled"] == int(enabled):
            return
        connection.execute("UPDATE ledgerAccess SET enabled = ? WHERE userId = ? AND ledgerId = currentLedgerId()", (int(enabled), userId))
        connection.execute("UPDATE users SET authVersion = authVersion + 1 WHERE id = ?", (userId,))
        recordUserChange(connection, userId, "access-enable" if enabled else "access-disable", before, userSnapshot(connection, userId))


def setUserEnabled(connection, userId, enabled):
    if type(enabled) is not bool:
        raise ValidationError("Login status must be Enabled or Disabled")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        before = userSnapshot(connection, userId)
        if before["loginEnabled"] == int(enabled):
            return
        connection.execute("UPDATE users SET enabled = ?, authVersion = authVersion + 1 WHERE id = ?", (int(enabled), userId))
        recordUserChange(connection, userId, "login-enable" if enabled else "login-disable", before, userSnapshot(connection, userId))


def confirmedPassword():
    print("Use 8–128 characters. Spaces are allowed. Password typing shows no characters; enter the same password twice.")
    # getpass otherwise falls back to echoed input when no protected terminal exists.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        password = getpass.getpass("Password: ")
        confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise ValidationError("Passwords do not match")
    return password


def promptCreateUser(connection, role):
    print("Trusted CLI creation accepts this account without an email code; it does not prove mailbox ownership.")
    username = input(f"{role.title()} username: ")
    firstName = input("First name (required): ")
    lastName = input("Last name (optional): ")
    createUser(connection, role, username, confirmedPassword(), firstName, lastName)
    print(f"{role.title()} configured.")


def configureOnStartup(connection):
    if connection.execute("SELECT 1 FROM ledgerAccess WHERE role = 'owner'").fetchone():
        return
    bindInitialLedger(connection)
    ownerConfigured = False
    print("An Owner is required to sign in and use No Part Finance. There are no default credentials.")
    try:
        if input("Create the required Owner now? [y/N; Enter skips] ").strip().lower() != "y":
            print("Owner setup skipped. The server remains running, but you cannot sign in until an Owner is created.")
            print("Restart to retry Owner setup, or run startWindows.bat manage-users (Windows) or ./startLinux.sh manage-users (Linux) from the application folder.")
            return
        promptCreateUser(connection, "owner")
        ownerConfigured = True
        if input("Create an optional read-only Auditor? [y/N; Enter skips] ").strip().lower() == "y":
            promptCreateUser(connection, "auditor")
        else:
            print("Auditor setup skipped. Your Owner is saved and can sign in.")
    except (EOFError, KeyboardInterrupt, OSError, ValidationError, sqlite3.Error, getpass.GetPassWarning) as error:
        print("\nSetup stopped.")
        if isinstance(error, getpass.GetPassWarning):
            print("Protected password input is unavailable. Retry in an interactive Command Prompt or terminal; passwords must not be echoed.")
        elif str(error):
            print(str(error))
        if ownerConfigured:
            print("Your Owner is saved and can sign in. Optional Auditor setup did not complete.")
        else:
            print("No Owner was created. The server remains running, but you cannot sign in. Restart to retry Owner setup.")
        print("To retry user setup, run startWindows.bat manage-users (Windows) or ./startLinux.sh manage-users (Linux) from the application folder.")


def selectRecord(connection, kind):
    """Page numbers are display positions only; subsequent writes revalidate stable IDs."""
    page = 1
    search = ""
    while True:
        if kind == "users":
            records = connection.execute("SELECT id, username AS label FROM users WHERE instr(usernameKey, ?) > 0 ORDER BY usernameKey, id LIMIT 20 OFFSET ?", (search.casefold(), (page - 1) * 20)).fetchall()
            total = connection.execute("SELECT COUNT(*) FROM users WHERE instr(usernameKey, ?) > 0", (search.casefold(),)).fetchone()[0]
        else:
            records = connection.execute("SELECT l.id, l.name, u.username FROM ledgers l JOIN ledgerAccess a ON a.ledgerId = l.id AND a.role = 'owner' JOIN users u ON u.id = a.userId ORDER BY lower(l.name), l.id").fetchall()
            records = [dict(row, label=f"{row['name']} — Owner: {row['username']}") for row in records if search.casefold() in row["name"].casefold() or search.casefold() in row["username"].casefold()]
            total = len(records)
            records = records[(page - 1) * 20:page * 20]
        lastPage = max(1, (total + 19) // 20)
        if page > lastPage:
            page = lastPage
            continue
        print(f"\n{kind.title()} — page {page} of {lastPage} — {total} matches")
        if not records:
            print("No matches.")
        for index, record in enumerate(records, 1):
            print(f"{index}. {record['label']}")
        print("Select a number. S. Search  C. Clear search  0. Back")
        if page > 1:
            print("P. Previous page")
        if page < lastPage:
            print("N. Next page")
        choice = input("Choose: ").strip().lower()
        if choice == "0":
            return None
        if choice == "s":
            search = input("Search: ").strip()
            page = 1
        elif choice == "c":
            search, page = "", 1
        elif choice == "n" and page < lastPage:
            page += 1
        elif choice == "p" and page > 1:
            page -= 1
        elif choice.isdecimal() and 1 <= int(choice) <= len(records):
            return records[int(choice) - 1]["id"]
        else:
            print("Choose a listed option.")


def manageSelectedUser(connection, userId):
    memberships = connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (userId,)).fetchall()
    if len(memberships) != 1:
        if not memberships:
            return manageIdentityWithoutLedger(connection, userId)
    if len(memberships) > 1:
        print("Select a membership for access management. Username and password changes affect this login across all its ledgers.")
        for index, membership in enumerate(memberships, 1):
            label = connection.execute("SELECT name FROM ledgers WHERE id = ?", (membership["ledgerId"],)).fetchone()[0]
            print(f"{index}. {label}")
        choice = input("Membership (0 to cancel): ").strip()
        if not choice.isdecimal() or not 1 <= int(choice) <= len(memberships):
            return
        selectedMembership = memberships[int(choice) - 1]
    else:
        selectedMembership = memberships[0]
    bindLedger(connection, selectedMembership["ledgerId"])
    while True:
        user = userSnapshot(connection, userId)
        ledgerName = connection.execute("SELECT name FROM ledgers WHERE id = currentLedgerId()").fetchone()[0]
        print(f"\nUser: {user['username']}")
        print(f"Login: {'Enabled' if user['loginEnabled'] else 'Disabled'} (administrative status; sign-in cooldown is separate)")
        lastLogin = connection.execute("SELECT lastLoginAt FROM users WHERE id = ?", (userId,)).fetchone()[0]
        print(f"Last successful login: {localTimestamp(lastLogin, includeOffset=True)}")
        print("Ledger | Role | Access")
        print(f"{ledgerName} | {user['role'].title()} | {'Enabled' if user['accessEnabled'] else 'Disabled'}")
        setupRequired = connection.execute("SELECT passwordSetupRequired FROM users WHERE id = ?", (userId,)).fetchone()[0]
        print(f"Credential setup: {'Password required through CLI reset' if setupRequired else 'Ready'}")
        print("1. Change username\n2. Reset password")
        if user["role"] == "auditor":
            print(f"3. {'Disable' if user['accessEnabled'] else 'Enable'} ledger access")
        print(f"4. {'Disable' if user['loginEnabled'] else 'Enable'} login")
        print(f"Profile: {user['firstName']} {user['lastName']}")
        print(f"Verification: {user['verificationStatus']} ({user['acceptanceMethod']})")
        print("5. Set first/last name\n6. Manually accept verification")
        print("0. Back")
        choice = input("Choose: ").strip()
        if choice == "0":
            return
        if choice == "1":
            renameUser(connection, userId, input("New username: "))
            print("Username saved. Ledger name unchanged.")
        elif choice == "2":
            resetPassword(connection, userId, confirmedPassword())
            print("Password reset. Existing sessions invalidated.")
        elif choice == "3" and user["role"] == "auditor":
            if input(f"Change ledger access for {user['username']} in {ledgerName}? [y/N] ").strip().lower() == "y":
                setAuditorAccess(connection, userId, not user["accessEnabled"])
                print("Access saved. Fresh sign-in required.")
        elif choice == "4":
            if input(f"Change login status for {user['username']}? [y/N] ").strip().lower() == "y":
                setUserEnabled(connection, userId, not user["loginEnabled"])
                print("Login status saved. Fresh sign-in required.")
        elif choice == "5":
            updateProfile(connection, userId, input("First name (required): "), input("Last name (optional): "))
            print("Profile saved. Fresh sign-in required.")
        elif choice == "6":
            if input("Accept without proving mailbox ownership? [y/N] ").strip().lower() == "y":
                acceptUserManually(connection, userId)
                print("Accepted through trusted CLI. Fresh sign-in required.")
        else:
            print("Choose a listed option.")


def manageUsers(databasePath, configuration=None, operationalLogs=None):
    connection = connectDatabase(databasePath)
    if configuration is None:
        from platformConfiguration import loadConfiguration
        configuration = loadConfiguration(Path(databasePath).resolve().parent)
    connection.configuration = configuration
    connection.operationalLogs = operationalLogs
    try:
        print("Exactly one Owner per ledger; multiple read-only Auditors are supported. User management does not start the server.")
        print("After exiting, run startWindows.bat (Windows) or ./startLinux.sh (Linux) without manage-users if the server is not running.")
        while True:
            print("\n1. List / manage users\n2. Create Owner and ledger\n3. Create Auditor for a ledger\n0. Exit")
            try:
                choice = input("Choose: ").strip()
                if choice == "0":
                    return
                if choice == "1":
                    userId = selectRecord(connection, "users")
                    if userId is not None:
                        manageSelectedUser(connection, userId)
                elif choice == "2":
                    print("Trusted CLI creation accepts this account without an email code; it does not prove mailbox ownership.")
                    username = input("Owner username: ")
                    firstName = input("First name (required): ")
                    lastName = input("Last name (optional): ")
                    createOwner(connection, username, confirmedPassword(), firstName, lastName)
                    print("Owner and ledger configured.")
                elif choice == "3":
                    ledgerId = selectRecord(connection, "ledgers")
                    if ledgerId is not None:
                        bindLedger(connection, ledgerId)
                        promptCreateUser(connection, "auditor")
                else:
                    print("Choose a listed option.")
            except (ValidationError, sqlite3.Error) as error:
                if operationalLogs:
                    operationalLogs.write("userManagement", "A user-management change could not be saved. Review the terminal feedback and correct the input or database problem before trying again.")
                print(str(error))
            except (EOFError, KeyboardInterrupt, OSError, getpass.GetPassWarning):
                print("\nUser management closed. If password input was unavailable, retry in an interactive Command Prompt or terminal. Run the same manage-users command to retry.")
                return
    finally:
        connection.close()


class IdentityRateLimit(ValidationError):
    def __init__(self, message, retryAfter):
        super().__init__(message)
        self.retryAfter = retryAfter


def auditIdentity(connection, userId, action, before=None):
    recordUserChange(connection, userId, action, before, userSnapshot(connection, userId))


def registrationIsExpired(user, now=None):
    now = int(time.time()) if now is None else now
    return bool(user["registrationExpired"] or (user["verificationStatus"] == "pending" and user["registrationExpiresAt"] is not None and now >= user["registrationExpiresAt"]))


def releaseExpiredRegistrations(connection, username, now):
    user = connection.execute("SELECT * FROM users WHERE usernameKey = ?", (username.casefold(),)).fetchone()
    if user and registrationIsExpired(user, now) and not user["registrationExpired"]:
        before = userSnapshot(connection, user["id"])
        # A reserved control-character prefix cannot collide with a valid username.
        # Keep the original spelling for history, but never reuse the old User ID.
        connection.execute("UPDATE users SET usernameKey = ?, registrationExpired = 1, authVersion = authVersion + 1 WHERE id = ?", (f"\x00expired:{user['id']}", user["id"]))
        connection.execute("DELETE FROM verificationChallenges WHERE userId = ?", (user["id"],))
        auditIdentity(connection, user["id"], "registration-expired", before)


def requireUsableIdentity(connection, userId, now=None):
    user = connection.execute("SELECT * FROM users WHERE id = ?", (userId,)).fetchone()
    if not user or not user["enabled"] or user["passwordSetupRequired"] or registrationIsExpired(user, now):
        raise ValidationError("Account is unavailable. Sign in again or register a new account if registration expired.")
    return user


def checkAttemptLimits(attempts, now, label):
    retryAfter = 0
    for window, maximum in ((600, 5), (86400, 20)):
        recent = sorted(attempt for attempt in attempts if attempt > now - window)
        if len(recent) >= maximum:
            retryAfter = max(retryAfter, recent[-maximum] + window - now)
    if retryAfter:
        raise IdentityRateLimit(f"{label} limit reached. Try again in {math.ceil(retryAfter / 60)} minutes.", retryAfter)


def admitRegistration(connection, address, now=None):
    now = int(time.time()) if now is None else now
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        attempts = [row[0] for row in connection.execute("SELECT attemptedAt FROM registrationAttempts WHERE address = ? AND attemptedAt > ?", (address, now - 86400))]
        checkAttemptLimits(attempts, now, "Registration")
        connection.execute("DELETE FROM registrationAttempts WHERE attemptedAt <= ?", (now - 86400,))
        connection.execute("INSERT INTO registrationAttempts VALUES (?, ?)", (address, now))


def registerUser(connection, username, firstName, lastName, password, confirmation, now=None):
    now = int(time.time()) if now is None else now
    configuration = connection.configuration
    if not configuration["allowWebRegistration"]:
        raise ValidationError("Web registration is disabled")
    username = configuredUsername(connection, username)
    firstName, lastName = validateProfile(firstName, lastName)
    if password != confirmation:
        raise ValidationError("Passwords do not match")
    hashedPassword = passwordHash(password)
    pending = configuration["requireRegistrationVerification"]
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if not connection.execute("SELECT 1 FROM ledgerAccess WHERE role = 'owner'").fetchone():
            raise ValidationError("Complete initial Owner setup through the CLI first")
        releaseExpiredRegistrations(connection, username, now)
        if connection.execute("SELECT 1 FROM users WHERE usernameKey = ?", (username.casefold(),)).fetchone():
            raise ValidationError("That username is already configured")
        userId = connection.execute("""INSERT INTO users
            (username, usernameKey, passwordHash, firstName, lastName, verificationStatus,
             acceptanceMethod, registrationCreatedAt, registrationExpiresAt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (username, username.casefold(), hashedPassword, firstName, lastName,
             "pending" if pending else "accepted", "awaiting-email" if pending else "verification-not-required",
             now, now + 86400 if pending else None)).lastrowid
        connection.actorKind, connection.actorId, connection.actorLabel = "user", str(userId), username
        auditIdentity(connection, userId, "registration")
    return userId


def acceptPendingForPolicy(connection, userId, now=None, expectedVersion=None):
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        user = requireUsableIdentity(connection, userId, now)
        if expectedVersion is not None and user["authVersion"] != expectedVersion:
            raise ValidationError("Your account changed. Sign in again.")
        if connection.configuration["requireRegistrationVerification"] or user["verificationStatus"] == "accepted":
            return
        before = userSnapshot(connection, userId)
        connection.execute("UPDATE users SET verificationStatus = 'accepted', acceptanceMethod = 'configuration-policy-bypass', authVersion = authVersion + 1 WHERE id = ?", (userId,))
        connection.execute("DELETE FROM verificationChallenges WHERE userId = ?", (userId,))
        auditIdentity(connection, userId, "verification-policy-bypass", before)


def identityNeedsProfile(user, configuration):
    return not user["firstName"].strip() or (configuration["enforceEmailUsernames"] and not emailLikeUsername(user["username"]))


def saveIdentity(connection, userId, username, firstName, lastName):
    """Caller owns the transaction; identity, invalidation, and audit commit together."""
    username = configuredUsername(connection, username)
    firstName, lastName = validateProfile(firstName, lastName)
    user = requireUsableIdentity(connection, userId)
    releaseExpiredRegistrations(connection, username, int(time.time()))
    if connection.execute("SELECT 1 FROM users WHERE usernameKey = ? AND id != ?", (username.casefold(), userId)).fetchone():
        raise ValidationError("That username is already configured")
    before = userSnapshot(connection, userId)
    connection.execute("UPDATE users SET username = ?, usernameKey = ?, firstName = ?, lastName = ?, authVersion = authVersion + 1 WHERE id = ?", (username, username.casefold(), firstName, lastName, userId))
    auditIdentity(connection, userId, "profile-update", before)


def updateProfile(connection, userId, firstName, lastName):
    firstName, lastName = validateProfile(firstName, lastName)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        before = userSnapshot(connection, userId)
        if before["registrationExpired"]:
            raise ValidationError("Expired registrations cannot be changed")
        connection.execute("UPDATE users SET firstName = ?, lastName = ?, authVersion = authVersion + 1 WHERE id = ?", (firstName, lastName, userId))
        connection.execute("DELETE FROM verificationChallenges WHERE userId = ? AND purpose = 'migration'", (userId,))
        auditIdentity(connection, userId, "profile-update", before)


def acceptUserManually(connection, userId):
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        user = connection.execute("SELECT * FROM users WHERE id = ?", (userId,)).fetchone()
        if not user or registrationIsExpired(user):
            raise ValidationError("Expired registrations cannot be accepted; register a new account")
        before = userSnapshot(connection, userId)
        challenge = connection.execute("SELECT * FROM verificationChallenges WHERE userId = ? AND purpose = 'migration' AND consumedAt IS NULL", (userId,)).fetchone()
        if challenge:
            # Trusted manual migration acceptance still validates the proposed name/address.
            username = configuredUsername(connection, challenge["address"])
            firstName, lastName = validateProfile(challenge["firstName"], challenge["lastName"])
            if connection.execute("SELECT 1 FROM users WHERE usernameKey = ? AND id != ?", (username.casefold(), userId)).fetchone():
                raise ValidationError("That username is already configured")
            connection.execute("UPDATE users SET username = ?, usernameKey = ?, firstName = ?, lastName = ? WHERE id = ?", (username, username.casefold(), firstName, lastName, userId))
        connection.execute("UPDATE users SET verificationStatus = 'accepted', acceptanceMethod = 'cli-manual-bypass', authVersion = authVersion + 1 WHERE id = ?", (userId,))
        connection.execute("DELETE FROM verificationChallenges WHERE userId = ?", (userId,))
        auditIdentity(connection, userId, "verification-cli-bypass", before)


def issueVerification(connection, userId, clientAddress, purpose="registration", firstName=None, lastName=None, now=None, proposedUsername=None, expectedVersion=None):
    now = int(time.time()) if now is None else now
    configuration = connection.configuration
    if not configuration["requireRegistrationVerification"] or not configuration["smtpEnabled"]:
        raise ValidationError("Email verification is not enabled")
    code = f"{secrets.randbelow(1000000):06d}"
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        user = requireUsableIdentity(connection, userId, now)
        if expectedVersion is not None and user["authVersion"] != expectedVersion:
            raise ValidationError("Your account changed. Sign in again.")
        challenge = connection.execute("SELECT * FROM verificationChallenges WHERE userId = ?", (userId,)).fetchone()
        if purpose == "registration":
            if user["verificationStatus"] != "pending":
                raise ValidationError("This account is already accepted")
            username, firstName, lastName = user["username"], user["firstName"], user["lastName"]
        elif purpose == "migration":
            if firstName is None and challenge and challenge["purpose"] == "migration":
                username, firstName, lastName = challenge["address"], challenge["firstName"], challenge["lastName"]
            else:
                username = configuredUsername(connection, proposedUsername)
                firstName, lastName = validateProfile(firstName, lastName)
            if connection.execute("SELECT 1 FROM users WHERE usernameKey = ? AND id != ?", (username.casefold(), userId)).fetchone():
                raise ValidationError("That username is already configured")
        else:
            raise ValidationError("Verification purpose is invalid")
        configuredUsername(connection, username)
        state = connection.execute("SELECT * FROM verificationState WHERE userId = ?", (userId,)).fetchone()
        if state and now < state["cooldownUntil"]:
            raise IdentityRateLimit("Verification is cooling down. Try again later.", state["cooldownUntil"] - now)
        accountAttempts = [row[0] for row in connection.execute("SELECT attemptedAt FROM verificationSendAttempts WHERE userId = ? AND attemptedAt > ?", (userId, now - 86400))]
        if accountAttempts and now < max(accountAttempts) + 30:
            raise IdentityRateLimit("Wait 30 seconds between verification emails.", max(accountAttempts) + 30 - now)
        checkAttemptLimits(accountAttempts, now, "Verification email")
        ipAttempts = [row[0] for row in connection.execute("SELECT attemptedAt FROM verificationSendAttempts WHERE address = ? AND attemptedAt > ?", (clientAddress, now - 86400))]
        checkAttemptLimits(ipAttempts, now, "Verification email")
        verifier = generate_password_hash(code, method="scrypt")
        connection.execute("DELETE FROM verificationSendAttempts WHERE attemptedAt <= ?", (now - 86400,))
        connection.execute("INSERT INTO verificationSendAttempts VALUES (?, ?, ?)", (userId, clientAddress, now))
        connection.execute("""INSERT INTO verificationChallenges
            (userId, purpose, address, verifier, issuedAt, expiresAt, consumedAt, firstName, lastName)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
            ON CONFLICT(userId) DO UPDATE SET purpose=excluded.purpose, address=excluded.address,
                verifier=excluded.verifier, issuedAt=excluded.issuedAt, expiresAt=excluded.expiresAt,
                consumedAt=NULL, firstName=excluded.firstName, lastName=excluded.lastName""",
            (userId, purpose, username, verifier, now, now + 600, firstName, lastName))
    # Delivery is deliberately outside the write transaction. A timeout can mean
    # the server accepted the message: keep the issued challenge usable until expiry.
    return sendVerificationEmail(configuration, username, code, purpose, getattr(connection, "operationalLogs", None))


def verifyIdentityCode(connection, userId, code, now=None, expectedVersion=None):
    now = int(time.time()) if now is None else now
    failure = None
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        user = requireUsableIdentity(connection, userId, now)
        if expectedVersion is not None and user["authVersion"] != expectedVersion:
            raise ValidationError("Your account changed. Sign in again.")
        challenge = connection.execute("SELECT * FROM verificationChallenges WHERE userId = ?", (userId,)).fetchone()
        state = connection.execute("SELECT * FROM verificationState WHERE userId = ?", (userId,)).fetchone()
        if state and now < state["cooldownUntil"]:
            raise IdentityRateLimit("Verification is cooling down. Try again later.", state["cooldownUntil"] - now)
        if not challenge or challenge["consumedAt"] is not None or now >= challenge["expiresAt"]:
            raise ValidationError("The code has expired or is unavailable. Request a new code.")
        purpose = challenge["purpose"]
        if (purpose == "registration" and (user["verificationStatus"] != "pending" or challenge["address"] != user["username"])) or (purpose == "migration" and not emailLikeUsername(challenge["address"])):
            raise ValidationError("The verification challenge does not match this account")
        valid = isinstance(code, str) and len(code) == 6 and code.isascii() and code.isdecimal() and check_password_hash(challenge["verifier"], code)
        if not valid:
            failures = (state["failedAttempts"] if state and not state["cooldownUntil"] else 0) + 1
            cooldown = now + 600 if failures >= 5 else 0
            connection.execute("""INSERT INTO verificationState VALUES (?, ?, ?)
                ON CONFLICT(userId) DO UPDATE SET failedAttempts=excluded.failedAttempts, cooldownUntil=excluded.cooldownUntil""", (userId, failures, cooldown))
            failure = IdentityRateLimit("Too many incorrect codes. Try again in 10 minutes.", 600) if cooldown else ValidationError("Incorrect verification code")
        else:
            before = userSnapshot(connection, userId)
            if purpose == "migration":
                saveIdentity(connection, userId, challenge["address"], challenge["firstName"], challenge["lastName"])
            else:
                connection.execute("UPDATE users SET verificationStatus = 'accepted', acceptanceMethod = 'email-code', authVersion = authVersion + 1 WHERE id = ?", (userId,))
            if purpose == "migration":
                connection.execute("UPDATE users SET acceptanceMethod = 'email-code' WHERE id = ?", (userId,))
            connection.execute("UPDATE verificationChallenges SET consumedAt = ? WHERE userId = ?", (now, userId))
            connection.execute("DELETE FROM verificationState WHERE userId = ?", (userId,))
            auditIdentity(connection, userId, "email-verified", before)
    if failure:
        raise failure
    return purpose


def sendVerificationEmail(configuration, recipient, code, purpose, logs):
    import smtplib
    import ssl
    from email.message import EmailMessage
    from email.utils import formataddr

    if not configuration["smtpEnabled"]:
        raise ValidationError("SMTP is disabled")
    message = EmailMessage()
    message["Subject"] = "No Part Finance verification code"
    message["From"] = formataddr((configuration["smtpFromName"], configuration["smtpFromAddress"]))
    message["To"] = recipient
    contextText = "verification" if purpose == "registration" else "email address change verification"
    message.set_content(f"Your No Part Finance {contextText} code is {code}. It expires in 10 minutes.")
    mailClient = None
    try:
        context = ssl.create_default_context()
        clientType = smtplib.SMTP_SSL if configuration["smtpSecurity"] == "tls" else smtplib.SMTP
        arguments = {"timeout": configuration["smtpTimeoutSeconds"]}
        if clientType is smtplib.SMTP_SSL:
            arguments["context"] = context
        mailClient = clientType(configuration["smtpHost"], configuration["smtpPort"], **arguments)
        if configuration["smtpSecurity"] == "starttls":
            mailClient.ehlo()
            mailClient.starttls(context=context)
            mailClient.ehlo()
        if configuration["smtpUsername"]:
            mailClient.login(configuration["smtpUsername"], configuration["smtpPassword"])
        mailClient.send_message(message)
        if logs:
            logs.write("email", "Verification email accepted by the mail server. Inbox delivery is not guaranteed.")
        return "Verification email sent. Check your inbox."
    except (OSError, smtplib.SMTPException, ValueError) as error:
        # SMTP exceptions can echo credentials, message text, or provider responses.
        # Deliberately emit neither the exception nor the message/recipient.
        if logs:
            if isinstance(error, TimeoutError):
                explanation = "The mail server did not respond before the timeout. Check the connection or increase smtpTimeoutSeconds before trying Resend."
            elif isinstance(error, smtplib.SMTPAuthenticationError):
                explanation = "The mail server rejected the application sign-in. Check the SMTP username/password before trying Resend."
            elif isinstance(error, ssl.SSLCertVerificationError):
                explanation = "The mail server certificate could not be verified. Check its hostname and certificate; do not disable certificate validation."
            else:
                explanation = "The mail server may be unavailable or rejected the request. Check SMTP connection and security settings before trying Resend."
            logs.write("email", "Verification email delivery could not be confirmed. " + explanation)
        return "We couldn’t confirm that your verification email was sent. Check your inbox, or try Resend when available."
    finally:
        if mailClient is not None:
            try:
                mailClient.close()
            except (OSError, smtplib.SMTPException):
                pass


def manageIdentityWithoutLedger(connection, userId):
    while True:
        user = userSnapshot(connection, userId)
        print(f"\nUser: {user['username']} (no ledger membership)")
        print(f"Profile: {user['firstName']} {user['lastName']}")
        print(f"Verification: {user['verificationStatus']} ({user['acceptanceMethod']})")
        print(f"Login: {'Enabled' if user['loginEnabled'] else 'Disabled'}")
        print("1. Change username\n2. Reset password\n4. Enable/disable login\n5. Set first/last name\n6. Manually accept verification\n0. Back")
        choice = input("Choose: ").strip()
        if choice == "0":
            return
        if choice == "1":
            renameUser(connection, userId, input("New username: "))
        elif choice == "2":
            resetPassword(connection, userId, confirmedPassword())
        elif choice == "4":
            if input("Change administrative login status? [y/N] ").strip().lower() == "y":
                setUserEnabled(connection, userId, not user["loginEnabled"])
        elif choice == "5":
            updateProfile(connection, userId, input("First name (required): "), input("Last name (optional): "))
        elif choice == "6":
            if input("Accept without proving mailbox ownership? [y/N] ").strip().lower() == "y":
                acceptUserManually(connection, userId)
