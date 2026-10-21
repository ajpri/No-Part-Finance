"""Trusted local user management. Passwords never appear in output or audit values."""

import getpass
import json
import sqlite3
import unicodedata
import warnings
from uuid import uuid4

from werkzeug.security import check_password_hash, generate_password_hash

from database import (
    ValidationError, connectDatabase, bindInitialLedger, bindLedger, requireLedger,
    createExternalSystemAccount,
    localTimestamp,
)


def validateUsername(username):
    if not isinstance(username, str):
        raise ValidationError("Username is required")
    username = username.strip()
    if not 1 <= len(username) <= 64 or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in username):
        raise ValidationError("Username must contain 1 through 64 characters without control characters")
    return username


def passwordHash(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        raise ValidationError("Password must contain 8 through 128 characters")
    return generate_password_hash(password, method="scrypt")


def insertUser(connection, role, username, hashedPassword):
    """Caller holds the provisioning write transaction and explicit ledger context."""
    if connection.execute("SELECT 1 FROM users WHERE usernameKey = ?", (username.casefold(),)).fetchone():
        raise ValidationError("That username is already configured")
    if role == "owner" and connection.execute("SELECT 1 FROM scopedusers WHERE role = 'owner'").fetchone():
        raise ValidationError("That Owner is already configured")
    if role == "auditor" and not connection.execute("SELECT 1 FROM scopedusers WHERE role = 'owner'").fetchone():
        raise ValidationError("Create the ledger Owner before adding Auditors")
    userId = connection.execute(
        "INSERT INTO users (username, usernameKey, passwordHash) VALUES (?, ?, ?)",
        (username, username.casefold(), hashedPassword),
    ).lastrowid
    connection.execute("INSERT INTO ledgerAccess (userId, ledgerId, role) VALUES (?, currentLedgerId(), ?)", (userId, role))
    if role == "owner":
        connection.execute("UPDATE ledgers SET name = ? WHERE id = currentLedgerId()", (f"{username}’s Finances",))
    recordUserChange(connection, userId, "create", None, userSnapshot(connection, userId))
    return userId


def createUser(connection, role, username, password):
    """Create a new login in an explicitly selected ledger (including initial setup)."""
    requireLedger(connection)
    if role not in {"owner", "auditor"}:
        raise ValidationError("User role is invalid")
    username = validateUsername(username)
    hashedPassword = passwordHash(password)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        return insertUser(connection, role, username, hashedPassword)


def createOwner(connection, username, password):
    """Provision a separate ledger; only first setup claims the initial unowned ledger."""
    username = validateUsername(username)
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
                connection.execute("INSERT INTO ledgers (id, name) VALUES (?, ?)", (ledgerId, f"{username}’s Finances"))
            bindLedger(connection, ledgerId)
            if not initial:
                createExternalSystemAccount(connection)
                # Other defaults are intentionally absent settings, as on a fresh install.
            userId = insertUser(connection, "owner", username, hashedPassword)
            return userId
    finally:
        connection.ledgerId, connection.actorKind, connection.actorId, connection.actorLabel = previousContext


def userSnapshot(connection, userId):
    requireLedger(connection)
    user = connection.execute("SELECT u.id, u.username, u.authVersion, u.enabled AS loginEnabled, a.role, a.enabled AS accessEnabled FROM users u JOIN ledgerAccess a ON a.userId = u.id WHERE a.ledgerId = currentLedgerId() AND u.id = ?", (userId,)).fetchone()
    if not user or connection.execute("SELECT COUNT(*) FROM ledgerAccess WHERE userId = ?", (userId,)).fetchone()[0] != 1:
        raise ValidationError("User must have exactly one membership in the selected ledger")
    return dict(user)


def recordUserChange(connection, userId, action, before, after):
    connection.execute("""INSERT INTO auditEvents
        (ledgerId, changedAt, actorKind, actorId, actorLabel, action, recordType, recordId, beforeValues, afterValues)
        VALUES (currentLedgerId(), strftime('%Y-%m-%dT%H:%M:%fZ','now'), auditActorKind(), auditActorId(), auditActorLabel(), ?, 'users', ?, ?, ?)""",
        (action, str(userId), json.dumps(before) if before is not None else None, json.dumps(after) if after is not None else None))


def resetPassword(connection, userId, password):
    hashedPassword = passwordHash(password)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        before = userSnapshot(connection, userId)
        connection.execute("UPDATE users SET passwordHash = ?, authVersion = authVersion + 1 WHERE id = ?", (hashedPassword, before["id"]))
        recordUserChange(connection, userId, "password-reset", before, userSnapshot(connection, userId))


def renameUser(connection, userId, username):
    username = validateUsername(username)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        before = userSnapshot(connection, userId)
        if connection.execute("SELECT 1 FROM users WHERE usernameKey = ? AND id != ?", (username.casefold(), userId)).fetchone():
            raise ValidationError("That username is already configured")
        if before["username"] == username:
            return
        connection.execute("UPDATE users SET username = ?, usernameKey = ? WHERE id = ?", (username, username.casefold(), userId))
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
    username = input(f"{role.title()} username: ")
    createUser(connection, role, username, confirmedPassword())
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
        raise ValidationError("User must have exactly one ledger membership")
    bindLedger(connection, memberships[0]["ledgerId"])
    while True:
        user = userSnapshot(connection, userId)
        ledgerName = connection.execute("SELECT name FROM ledgers WHERE id = currentLedgerId()").fetchone()[0]
        print(f"\nUser: {user['username']}")
        print(f"Login: {'Enabled' if user['loginEnabled'] else 'Disabled'} (administrative status; sign-in cooldown is separate)")
        lastLogin = connection.execute("SELECT lastLoginAt FROM users WHERE id = ?", (userId,)).fetchone()[0]
        print(f"Last successful login: {localTimestamp(lastLogin, includeOffset=True)}")
        print("Ledger | Role | Access")
        print(f"{ledgerName} | {user['role'].title()} | {'Enabled' if user['accessEnabled'] else 'Disabled'}")
        print("1. Change username\n2. Reset password")
        if user["role"] == "auditor":
            print(f"3. {'Disable' if user['accessEnabled'] else 'Enable'} ledger access")
        print(f"4. {'Disable' if user['loginEnabled'] else 'Enable'} login")
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
        else:
            print("Choose a listed option.")


def manageUsers(databasePath):
    connection = connectDatabase(databasePath)
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
                    username = input("Owner username: ")
                    createOwner(connection, username, confirmedPassword())
                    print("Owner and ledger configured.")
                elif choice == "3":
                    ledgerId = selectRecord(connection, "ledgers")
                    if ledgerId is not None:
                        bindLedger(connection, ledgerId)
                        promptCreateUser(connection, "auditor")
                else:
                    print("Choose a listed option.")
            except (ValidationError, sqlite3.Error) as error:
                print(str(error))
            except (EOFError, KeyboardInterrupt, OSError, getpass.GetPassWarning):
                print("\nUser management closed. If password input was unavailable, retry in an interactive Command Prompt or terminal. Run the same manage-users command to retry.")
                return
    finally:
        connection.close()
