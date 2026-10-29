"""Explicit trusted CLI deletion, with a verified pre-delete recovery database."""
from datetime import datetime
from pathlib import Path
import sqlite3
from uuid import uuid4

from database import ValidationError, bindLedger, connectDatabase


def migration(connection):
    with connection:
        if "deleted" not in {row["name"] for row in connection.execute("PRAGMA table_info(users)")}:
            connection.execute("ALTER TABLE users ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0, 1))")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS deletedUserImmutable BEFORE UPDATE ON users WHEN OLD.deleted = 1
            BEGIN SELECT RAISE(ABORT, 'Deleted identities cannot be changed or re-enabled'); END""")
        connection.execute("PRAGMA user_version = 30")


def databaseBackup(databasePath, backupsDirectory):
    """Caller holds BEGIN IMMEDIATE; a second reader backs up the committed state."""
    backupsDirectory = Path(backupsDirectory)
    backupsDirectory.mkdir(parents=True, exist_ok=True)
    backupPath = backupsDirectory / f"pre-delete-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{uuid4().hex[:8]}.db"
    # Exclusive creation prevents overwriting an administrator's recovery file.
    backupPath.touch(exist_ok=False)
    try:
        with sqlite3.connect(Path(databasePath).resolve().as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(backupPath) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or destination.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValidationError("Pre-deletion backup verification failed")
        return backupPath
    except Exception:
        backupPath.unlink(missing_ok=True)
        raise


def targetSummary(connection, kind, targetId):
    if kind == "user":
        user = connection.execute("SELECT id, username FROM users WHERE id = ? AND deleted = 0", (targetId,)).fetchone()
        if not user:
            raise ValidationError("User does not exist")
        owned = connection.execute("SELECT l.name, l.id FROM ledgers l JOIN ledgerAccess a ON a.ledgerId = l.id WHERE a.userId = ? AND a.role = 'owner'", (targetId,)).fetchall()
        if owned:
            raise ValidationError("Delete the owned ledgers first: " + "; ".join(f"{row['name']} ({row['id']})" for row in owned))
        return f"User {user['username']} (ID {user['id']}): remove login and all memberships; preserve historical records as Deleted user."
    if kind != "ledger":
        raise ValidationError("Choose User or Ledger")
    ledger = connection.execute("SELECT name FROM ledgers WHERE id = ?", (targetId,)).fetchone()
    if not ledger:
        raise ValidationError("Ledger does not exist")
    owner = connection.execute("SELECT u.username FROM users u JOIN ledgerAccess a ON a.userId = u.id WHERE a.ledgerId = ? AND a.role = 'owner'", (targetId,)).fetchone()
    counts = []
    for table in ("transactions", "accounts", "pendingSubmissions", "simulatorPlans", "auditEvents", "ledgerAccess"):
        counts.append(f"{table}: {connection.execute(f'SELECT COUNT(*) FROM {table} WHERE ledgerId = ?', (targetId,)).fetchone()[0]}")
    return f"Ledger {ledger[0]} (ID {targetId}), Owner: {owner[0] if owner else 'None'}. Permanently delete ALL ledger contents and audit history. " + ", ".join(counts) + ". Users and other ledgers remain."


def deleteLedger(connection, ledgerId):
    bindLedger(connection, ledgerId, "system", "cli", "Command line")
    # Only this trusted atomic operation may remove an Owner and whole audit history.
    # Restore the exact protections before commit; rollback also restores their DDL.
    guards = connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name IN ('auditEventsNoDELETE', 'preserveOwnerMembership')").fetchall()
    for guard in guards:
        connection.execute(f'DROP TRIGGER "{guard["name"]}"')
    for table in ("pushSubscriptions", "sharingInvitations", "simulatorRows", "simulatorPlans", "pendingSubmissions", "sinkingFundPlans", "flexibleCategoryBudgets", "expectedIncomePlans", "plannedCreditPayments", "transactionTemplates", "transactions", "applicationSettings", "integrationSecrets"):
        connection.execute(f"DELETE FROM {table} WHERE ledgerId = ?", (ledgerId,))
    connection.execute("DELETE FROM categories WHERE ledgerId = ? AND parentCategoryId IS NOT NULL", (ledgerId,))
    connection.execute("DELETE FROM categories WHERE ledgerId = ?", (ledgerId,))
    connection.execute("DELETE FROM accounts WHERE ledgerId = ?", (ledgerId,))
    connection.execute("DELETE FROM auditEvents WHERE ledgerId = ?", (ledgerId,))
    connection.execute("DELETE FROM ledgerAccess WHERE ledgerId = ?", (ledgerId,))
    connection.execute("DELETE FROM ledgers WHERE id = ?", (ledgerId,))
    for guard in guards:
        connection.execute(guard["sql"])


def deleteUser(connection, userId):
    from users import recordUserChange, userSnapshot
    before = userSnapshot(connection, userId)
    recordUserChange(connection, userId, "delete", before, {"id": userId, "username": "Deleted user", "loginEnabled": False})
    connection.execute("DELETE FROM ledgerAccess WHERE userId = ?", (userId,))
    connection.execute("DELETE FROM pushSubscriptions WHERE userId = ?", (userId,))
    for table in ("verificationChallenges", "verificationState", "verificationSendAttempts"):
        connection.execute(f"DELETE FROM {table} WHERE userId = ?", (userId,))
    connection.execute("UPDATE sharingInvitations SET status = 'Cancelled' WHERE recipientUserId = ? AND status = 'Pending'", (userId,))
    connection.execute("""UPDATE users SET deleted = 1, username = 'Deleted user', usernameKey = ?, passwordHash = '',
        firstName = '', lastName = '', enabled = 0, passwordSetupRequired = 1, authVersion = authVersion + 1,
        lastLoginAt = NULL, registrationCreatedAt = NULL, registrationExpiresAt = NULL,
        verificationStatus = 'accepted', acceptanceMethod = 'deleted'
        WHERE id = ? AND deleted = 0""", (f"\x00deleted:{userId}:{uuid4()}", userId))


def deleteTarget(databasePath, kind, targetId, confirmation, backupsDirectory=None):
    if confirmation != str(targetId):
        raise ValidationError("Deletion cancelled: target ID did not match")
    connection = connectDatabase(databasePath)
    previousContext = connection.ledgerId
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            targetSummary(connection, kind, targetId)
            backupPath = databaseBackup(databasePath, backupsDirectory or Path(__file__).resolve().parent / "backups")
            if kind == "ledger":
                deleteLedger(connection, targetId)
            else:
                deleteUser(connection, targetId)
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise ValidationError("Deletion would violate database integrity")
        return backupPath
    finally:
        connection.ledgerId = previousContext
        connection.close()


def confirmDeletion(connection, kind, targetId, backupsDirectory=None):
    print(targetSummary(connection, kind, targetId))
    print("A full database backup containing credentials will be saved to backups/ before deletion. This cannot be undone without restoring a backup.")
    confirmation = input(f"Type the {kind} ID {targetId} to permanently delete (anything else cancels): ").strip()
    if confirmation != str(targetId):
        print("Deletion cancelled.")
        return
    databasePath = connection.execute("PRAGMA database_list").fetchone()[2]
    backupPath = deleteTarget(databasePath, kind, targetId, confirmation, backupsDirectory)
    print(f"Deleted {kind} {targetId}. Verified database backup: {backupPath}")
