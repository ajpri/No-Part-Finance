"""Command-line user management. Passwords never appear in output or logs."""

import getpass
import json
import sqlite3
import unicodedata
import warnings

from werkzeug.security import check_password_hash, generate_password_hash

from database import ValidationError, connectInitialLedgerDatabase as connectDatabase, bindInitialLedger, requireLedger


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


def createUser(connection, role, username, password):
    requireLedger(connection)
    if role not in {"owner", "auditor"}:
        raise ValidationError("User role is invalid")
    username = validateUsername(username)
    if connection.execute("SELECT 1 FROM users WHERE usernameKey = ? UNION ALL SELECT 1 FROM ledgerAccess WHERE ledgerId = currentLedgerId() AND role = ?", (username.casefold(), role)).fetchone():
        raise ValidationError("That role or username is already configured")
    hashedPassword = passwordHash(password)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM users WHERE usernameKey = ? UNION ALL SELECT 1 FROM ledgerAccess WHERE ledgerId = currentLedgerId() AND role = ?", (username.casefold(), role)).fetchone():
            raise ValidationError("That role or username is already configured")
        userId = connection.execute(
            "INSERT INTO users (username, usernameKey, passwordHash) VALUES (?, ?, ?)",
            (username, username.casefold(), hashedPassword),
        ).lastrowid
        connection.execute("INSERT INTO ledgerAccess (userId, ledgerId, role) VALUES (?, currentLedgerId(), ?)", (userId, role))
        if role == "owner":
            connection.execute("UPDATE ledgers SET name = ? WHERE id = currentLedgerId()", (f"{username}’s Finances",))
        recordUserChange(connection, userId, "create", role)
        return userId


def resetPassword(connection, role, password):
    requireLedger(connection)
    hashedPassword = passwordHash(password)
    with connection:
        cursor = connection.execute(
            "UPDATE users SET passwordHash = ?, authVersion = authVersion + 1 WHERE id IN (SELECT userId FROM ledgerAccess WHERE ledgerId = currentLedgerId() AND role = ?)",
            (hashedPassword, role),
        )
        if not cursor.rowcount:
            raise ValidationError("That user is not configured")
        userId = connection.execute("SELECT userId FROM ledgerAccess WHERE ledgerId = currentLedgerId() AND role = ?", (role,)).fetchone()[0]
        recordUserChange(connection, userId, "password-reset", role)


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
    if connection.execute("SELECT 1 FROM ledgerAccess WHERE ledgerId = currentLedgerId() AND role = 'owner'").fetchone():
        return
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


def manageUsers(databasePath):
    connection = connectDatabase(databasePath)
    try:
        print("One Owner is required; one read-only Auditor is optional. User management does not start the server.")
        print("After exiting, open the application if it is already running, or run startWindows.bat (Windows) or ./startLinux.sh (Linux) without manage-users.")
        while True:
            print("\n1. List users\n2. Create Owner\n3. Reset Owner password\n4. Create Auditor\n5. Reset Auditor password\n6. Remove Auditor\n7. Exit")
            try:
                choice = input("Choose: ").strip()
                if choice == "1":
                    for user in connection.execute("SELECT a.role, u.username FROM users u JOIN ledgerAccess a ON a.userId = u.id WHERE a.ledgerId = currentLedgerId() ORDER BY a.role DESC"):
                        print(f"{user['role'].title()}: {user['username']}")
                elif choice in {"2", "4"}:
                    promptCreateUser(connection, "owner" if choice == "2" else "auditor")
                elif choice in {"3", "5"}:
                    resetPassword(connection, "owner" if choice == "3" else "auditor", confirmedPassword())
                    print("Password reset.")
                elif choice == "6":
                    with connection:
                        user = connection.execute("SELECT userId FROM ledgerAccess WHERE ledgerId = currentLedgerId() AND role = 'auditor'").fetchone()
                        if user:
                            recordUserChange(connection, user["userId"], "remove", "auditor")
                            connection.execute("DELETE FROM users WHERE id = ?", (user["userId"],))
                    print("Auditor removed.")
                elif choice == "7":
                    return
                else:
                    print("Choose a listed number.")
            except (ValidationError, sqlite3.Error) as error:
                print(str(error))
            except (EOFError, KeyboardInterrupt, OSError, getpass.GetPassWarning):
                print("\nUser management closed. If password input was unavailable, retry in an interactive Command Prompt or terminal. Run the same manage-users command to retry.")
                return
    finally:
        connection.close()


def recordUserChange(connection, userId, action, role):
    user = connection.execute("SELECT username, authVersion FROM users WHERE id = ?", (userId,)).fetchone()
    values = {"username": user["username"], "role": role, "authVersion": user["authVersion"]}
    before = None if action == "create" else dict(values)
    after = None if action == "remove" else values
    if action == "password-reset":
        before["authVersion"] -= 1
    connection.execute("""INSERT INTO auditEvents
        (ledgerId, changedAt, actorKind, actorId, actorLabel, action, recordType, recordId, beforeValues, afterValues)
        VALUES (currentLedgerId(), strftime('%Y-%m-%dT%H:%M:%fZ','now'), auditActorKind(), auditActorId(), auditActorLabel(), ?, 'users', ?, ?, ?)""",
        (action, str(userId), json.dumps(before) if before is not None else None, json.dumps(after) if after is not None else None))
