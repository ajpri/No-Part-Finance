"""Shared SQLite schema and initialization for the application.

Balances are derived from an account offset plus its transaction amounts. Money is
stored only as integer cents.
"""

import sqlite3
import logging
import re
import json
import secrets
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from uuid import uuid4


schemaVersion = 21


class ValidationError(ValueError):
    pass

securitySchemaSql = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL UNIQUE CHECK (role IN ('owner', 'auditor')),
    username TEXT NOT NULL,
    usernameKey TEXT NOT NULL UNIQUE,
    passwordHash TEXT NOT NULL,
    authVersion INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS integrationSecrets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL CHECK (length(trim(label)) > 0),
    secret TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
CREATE TABLE IF NOT EXISTS pendingSubmissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    integrationId INTEGER NOT NULL REFERENCES integrationSecrets(id) ON DELETE RESTRICT,
    submissionId TEXT NOT NULL,
    originalContent TEXT NOT NULL,
    reviewContent TEXT,
    reviewVersion INTEGER NOT NULL DEFAULT 0 CHECK (reviewVersion >= 0),
    originalRole TEXT NOT NULL CHECK (originalRole IN ('income', 'expense')),
    status TEXT NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending', 'Approved', 'Rejected')),
    transactionId INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    UNIQUE(integrationId, submissionId)
);
CREATE INDEX IF NOT EXISTS pendingStatusIndex ON pendingSubmissions(status, id);
"""

schemaSql = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    accountClass TEXT NOT NULL CHECK (accountClass IN ('cash', 'credit', 'system')),
    accountSubtype TEXT CHECK (
        (accountClass = 'cash' AND (accountSubtype IS NULL OR accountSubtype IN ('checking', 'savings', 'other')))
        OR (accountClass = 'credit' AND (accountSubtype IS NULL OR accountSubtype IN ('Credit Card', 'Personal Loan', 'Other')))
        OR (accountClass = 'system' AND accountSubtype IS NULL)
    ),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    displayOrder INTEGER NOT NULL DEFAULT 0,
    balanceOffsetCents INTEGER NOT NULL DEFAULT 0 CHECK (typeof(balanceOffsetCents) = 'integer')
    ,creditLimitCents INTEGER CHECK (creditLimitCents IS NULL OR (typeof(creditLimitCents) = 'integer' AND creditLimitCents > 0))
    ,minimumBalanceCents INTEGER CHECK (minimumBalanceCents IS NULL OR (typeof(minimumBalanceCents) = 'integer' AND minimumBalanceCents > 0))
    ,minimumCautionPercent INTEGER CHECK (minimumCautionPercent IS NULL OR (typeof(minimumCautionPercent) = 'integer' AND minimumCautionPercent >= 0))
    ,utilizationWarningBasisPoints INTEGER CHECK (utilizationWarningBasisPoints IS NULL OR (typeof(utilizationWarningBasisPoints) = 'integer' AND utilizationWarningBasisPoints BETWEEN 1 AND 10000))
    ,systemIdentity TEXT UNIQUE CHECK (systemIdentity IS NULL OR systemIdentity = 'external')
    ,CHECK (accountClass != 'system' OR (systemIdentity = 'external' AND name = 'External' AND active = 1 AND balanceOffsetCents = 0 AND creditLimitCents IS NULL AND minimumBalanceCents IS NULL AND minimumCautionPercent IS NULL AND utilizationWarningBasisPoints IS NULL))
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    parentCategoryId INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    displayOrder INTEGER NOT NULL DEFAULT 0,
    offerReimbursement INTEGER NOT NULL DEFAULT 0 CHECK (offerReimbursement IN (0, 1)),
    reportingRole TEXT NOT NULL DEFAULT 'expense' CHECK (reportingRole IN ('expense', 'income')),
    CHECK (parentCategoryId IS NULL OR parentCategoryId != id)
);

CREATE TABLE IF NOT EXISTS applicationSettings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    accountId INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    date TEXT NOT NULL CHECK (
        length(date) = 10
        AND date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND date(date, '+0 days') = date
    ),
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    amountCents INTEGER NOT NULL CHECK (typeof(amountCents) = 'integer'),
    categoryId INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    transferId TEXT,
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1))
);

CREATE TABLE IF NOT EXISTS transactionTemplates (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    accountId INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
    description TEXT CHECK (description IS NULL OR length(trim(description)) > 0),
    categoryId INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    enteredAmountCents INTEGER CHECK (enteredAmountCents IS NULL OR (typeof(enteredAmountCents) = 'integer' AND enteredAmountCents != 0)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    displayOrder INTEGER NOT NULL DEFAULT 0,
    prefillAmount INTEGER NOT NULL DEFAULT 1 CHECK (prefillAmount IN (0, 1))
);

CREATE TABLE IF NOT EXISTS sinkingFundPlans (
    id INTEGER PRIMARY KEY,
    templateId INTEGER NOT NULL REFERENCES transactionTemplates(id) ON DELETE RESTRICT,
    frequency TEXT NOT NULL CHECK (frequency IN ('monthly', 'quarterly', 'yearly')),
    expectedDate TEXT NOT NULL CHECK (
        length(expectedDate) = 10
        AND expectedDate GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        AND date(expectedDate, '+0 days') = expectedDate
    ),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE UNIQUE INDEX IF NOT EXISTS oneActiveSinkingPlanPerTemplate
ON sinkingFundPlans(templateId) WHERE active = 1;

CREATE TABLE IF NOT EXISTS flexibleCategoryBudgets (
    id INTEGER PRIMARY KEY,
    categoryId INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    monthlyTargetCents INTEGER NOT NULL CHECK (typeof(monthlyTargetCents) = 'integer' AND monthlyTargetCents > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE UNIQUE INDEX IF NOT EXISTS oneActiveFlexibleBudgetPerCategory
ON flexibleCategoryBudgets(categoryId) WHERE active = 1;

CREATE TABLE IF NOT EXISTS expectedIncomePlans (
    id INTEGER PRIMARY KEY,
    templateId INTEGER NOT NULL REFERENCES transactionTemplates(id) ON DELETE RESTRICT,
    expectedDate TEXT NOT NULL CHECK (length(expectedDate) = 10 AND date(expectedDate, '+0 days') = expectedDate),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS plannedCreditPayments (
    id INTEGER PRIMARY KEY,
    expectedDate TEXT NOT NULL CHECK (length(expectedDate) = 10 AND date(expectedDate, '+0 days') = expectedDate),
    accountId INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    description TEXT NOT NULL CHECK (length(trim(description)) > 0),
    amountCents INTEGER NOT NULL CHECK (typeof(amountCents) = 'integer' AND amountCents > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS simulatorPlans (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    planDate TEXT NOT NULL CHECK (length(planDate) = 10 AND date(planDate, '+0 days') = planDate)
);

CREATE TABLE IF NOT EXISTS simulatorRows (
    id INTEGER PRIMARY KEY,
    planId INTEGER NOT NULL REFERENCES simulatorPlans(id) ON DELETE CASCADE,
    rowType TEXT NOT NULL CHECK (rowType IN ('live', 'income', 'spend', 'transfer', 'buffer')),
    rowOrder INTEGER NOT NULL,
    fromAccountId INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
    toAccountId INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
    categoryId INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
    label TEXT,
    amountCents INTEGER CHECK (amountCents IS NULL OR (typeof(amountCents) = 'integer' AND amountCents > 0)),
    amountMode TEXT NOT NULL DEFAULT 'fixed' CHECK (amountMode IN ('fixed', 'payoff')),
    executed INTEGER NOT NULL DEFAULT 0 CHECK (executed IN (0, 1)),
    executedTransactionId INTEGER REFERENCES transactions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS simulatorRowsPlanIndex ON simulatorRows(planId, rowOrder);

CREATE TRIGGER IF NOT EXISTS simulatorTransactionUnlink
BEFORE DELETE ON transactions
BEGIN
    UPDATE simulatorRows
    SET executed = 0, executedTransactionId = NULL
    WHERE executedTransactionId = OLD.id;
END;

CREATE INDEX IF NOT EXISTS transactionsDateIndex ON transactions(date);
CREATE INDEX IF NOT EXISTS transactionsAccountIndex ON transactions(accountId);
CREATE INDEX IF NOT EXISTS transactionsCategoryIndex ON transactions(categoryId);
CREATE INDEX IF NOT EXISTS transactionsTransferIndex ON transactions(transferId);
"""

testDatabaseGuardEnabled = False


def enableTestDatabaseGuard():
    global testDatabaseGuardEnabled
    testDatabaseGuardEnabled = True


def connectDatabase(databasePath):
    """Open a connection with the integrity settings required by the ledger."""
    resolvedDatabasePath = Path(databasePath).resolve()
    productionDatabasePath = (Path(__file__).resolve().parent / "finance.db").resolve()
    if testDatabaseGuardEnabled and resolvedDatabasePath == productionDatabasePath:
        raise RuntimeError("Tests may not open the production finance.db through a writable connection")
    connection = sqlite3.connect(databasePath)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initializeDatabase(databasePath):
    """Create or upgrade the schema without changing user-owned configuration."""
    databasePath = Path(databasePath)
    databasePath.parent.mkdir(parents=True, exist_ok=True)

    connection = connectDatabase(databasePath)
    backupPath = None
    try:
        currentVersion = connection.execute("PRAGMA user_version").fetchone()[0]
        hasSchema = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'accounts'"
        ).fetchone()
        if currentVersion > schemaVersion:
            raise RuntimeError(
                f"Database schema version {currentVersion} is newer than supported version {schemaVersion}"
            )
        if hasSchema and currentVersion < schemaVersion:
            integrityResult = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrityResult != "ok":
                raise RuntimeError(f"Pre-migration database integrity check failed: {integrityResult}")
            backupsDirectory = databasePath.parent / "backups"
            backupsDirectory.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            candidateBackupPath = backupsDirectory / f"pre-migration-v{currentVersion}-to-v{schemaVersion}-{timestamp}.db"
            try:
                backupConnection = sqlite3.connect(candidateBackupPath)
                try:
                    connection.backup(backupConnection)
                    backupIntegrity = backupConnection.execute("PRAGMA integrity_check").fetchall()
                    backupVersion = backupConnection.execute("PRAGMA user_version").fetchone()[0]
                    backupHasSchema = backupConnection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'accounts'"
                    ).fetchone()
                    if backupIntegrity != [("ok",)] or backupVersion != currentVersion or not backupHasSchema:
                        raise RuntimeError("Pre-migration backup verification failed")
                finally:
                    backupConnection.close()
            except Exception:
                logging.exception("Pre-migration backup creation or verification failed: %s", candidateBackupPath)
                raise
            # Only a completed, verified snapshot may ever replace the source database.
            backupPath = candidateBackupPath
            logging.info(
                "Upgrading database schema from %s to %s; backup: %s",
                currentVersion, schemaVersion, backupPath,
            )
        if currentVersion < 3 and connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'accounts'"
        ).fetchone():
            migrateToVersion3(connection)
        if currentVersion < 4 and connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transactionTemplates'"
        ).fetchone():
            migrateToVersion4(connection)
        if currentVersion < 5 and hasSchema:
            migrateToVersion5(connection)
        if currentVersion < 6 and hasSchema:
            migrateToVersion6(connection)
        if currentVersion < 7 and hasSchema:
            migrateToVersion7(connection)
        if currentVersion < 8 and hasSchema:
            migrateToVersion8(connection)
        if currentVersion < 9 and hasSchema:
            migrateToVersion9(connection)
        if currentVersion < 10 and hasSchema:
            migrateToVersion10(connection)
        if currentVersion < 11 and hasSchema:
            migrateToVersion11(connection)
        if currentVersion < 12 and hasSchema:
            migrateToVersion12(connection)
        if currentVersion < 13 and hasSchema:
            migrateToVersion13(connection)
        if currentVersion < 14 and hasSchema:
            migrateToVersion14(connection)
        if currentVersion < 15 and hasSchema:
            migrateToVersion15(connection)
        if currentVersion < 16 and hasSchema:
            migrateToVersion16(connection)
        if currentVersion < 17 and hasSchema:
            migrateToVersion17(connection)
        if currentVersion < 18 and hasSchema:
            migrateToVersion18(connection)
        if currentVersion < 19:
            migrateToVersion19(connection)
        if currentVersion < 20:
            migrateToVersion20(connection)
        connection.executescript(schemaSql)
        if currentVersion < 21:
            migrateToVersion21(connection)
        accountColumns = {
            row["name"] for row in connection.execute("PRAGMA table_info(accounts)")
        }
        if "balanceOffsetCents" not in accountColumns:
            connection.execute(
                "ALTER TABLE accounts ADD COLUMN balanceOffsetCents INTEGER NOT NULL DEFAULT 0"
            )
        if not hasSchema:
            createExternalSystemAccount(connection)
        validateExternalSystemAccount(connection)
        connection.execute(f"PRAGMA user_version = {schemaVersion}")
        connection.commit()
        foreignKeyFailure = connection.execute("PRAGMA foreign_key_check").fetchone()
        integrityResult = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finalVersion = connection.execute("PRAGMA user_version").fetchone()[0]
        if foreignKeyFailure or integrityResult != "ok" or finalVersion != schemaVersion:
            raise RuntimeError("Database migration verification failed")
    except Exception as upgradeError:
        connection.rollback()
        if backupPath is not None:
            try:
                restoreConnection = sqlite3.connect(f"{backupPath.resolve().as_uri()}?mode=ro", uri=True)
                try:
                    restoreConnection.backup(connection)
                    connection.commit()
                finally:
                    restoreConnection.close()
            except Exception as recoveryError:
                logging.exception("Database upgrade failed (%s); recovery also failed; verified backup retained: %s", upgradeError, backupPath)
                raise RuntimeError(
                    f"Database upgrade failed: {upgradeError}; recovery failed: {recoveryError}; "
                    f"verified backup retained at {backupPath}"
                ) from upgradeError
            logging.error("Database upgrade failed (%s); original database restored; verified backup retained: %s", upgradeError, backupPath)
        raise
    finally:
        connection.close()


def migrateToVersion3(connection):
    """Add transaction pins and descriptive credit subtypes without touching ledger data."""
    transactionColumns = {
        row["name"] for row in connection.execute("PRAGMA table_info(transactions)")
    }
    if "pinned" not in transactionColumns:
        connection.execute(
            "ALTER TABLE transactions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1))"
        )

    accountSql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'accounts'"
    ).fetchone()["sql"]
    if "Personal Loan" not in accountSql:
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.executescript(
                """
                CREATE TABLE accountsVersion3 (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
                    accountClass TEXT NOT NULL CHECK (accountClass IN ('cash', 'credit')),
                    accountSubtype TEXT CHECK (
                        (accountClass = 'cash' AND (accountSubtype IS NULL OR accountSubtype IN ('checking', 'savings', 'other')))
                        OR (accountClass = 'credit' AND (accountSubtype IS NULL OR accountSubtype IN ('Credit Card', 'Personal Loan', 'Other')))
                    ),
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    displayOrder INTEGER NOT NULL DEFAULT 0,
                    balanceOffsetCents INTEGER NOT NULL DEFAULT 0 CHECK (typeof(balanceOffsetCents) = 'integer')
                );
                INSERT INTO accountsVersion3
                    (id, name, accountClass, accountSubtype, active, displayOrder, balanceOffsetCents)
                SELECT id, name, accountClass,
                       CASE WHEN accountClass = 'credit' AND accountSubtype IS NULL
                            THEN 'Credit Card' ELSE accountSubtype END,
                       active, displayOrder, balanceOffsetCents
                FROM accounts;
                DROP TABLE accounts;
                ALTER TABLE accountsVersion3 RENAME TO accounts;
                """
            )
        finally:
            connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise sqlite3.IntegrityError("Version 3 migration created a foreign key violation")


def migrateToVersion4(connection):
    """Allow partial templates while preserving every existing preset."""
    connection.executescript(
        """
        CREATE TABLE transactionTemplatesVersion4 (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
            accountId INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
            description TEXT CHECK (description IS NULL OR length(trim(description)) > 0),
            categoryId INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
            enteredAmountCents INTEGER CHECK (enteredAmountCents IS NULL OR (typeof(enteredAmountCents) = 'integer' AND enteredAmountCents != 0)),
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            displayOrder INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO transactionTemplatesVersion4
            (id, name, accountId, description, categoryId, enteredAmountCents, active, displayOrder)
        SELECT id, name, accountId, description, categoryId, enteredAmountCents, active, displayOrder
        FROM transactionTemplates;
        DROP TABLE transactionTemplates;
        ALTER TABLE transactionTemplatesVersion4 RENAME TO transactionTemplates;
        """
    )


def migrateToVersion5(connection):
    """Add configurable reimbursement shortcuts and optional credit limits."""
    accountColumns = {row["name"] for row in connection.execute("PRAGMA table_info(accounts)")}
    if "creditLimitCents" not in accountColumns:
        connection.execute(
            "ALTER TABLE accounts ADD COLUMN creditLimitCents INTEGER CHECK (creditLimitCents IS NULL OR (typeof(creditLimitCents) = 'integer' AND creditLimitCents > 0))"
        )
    categoryColumns = {row["name"] for row in connection.execute("PRAGMA table_info(categories)")}
    if "offerReimbursement" not in categoryColumns:
        connection.execute(
            "ALTER TABLE categories ADD COLUMN offerReimbursement INTEGER NOT NULL DEFAULT 0 CHECK (offerReimbursement IN (0, 1))"
        )
        connection.execute(
            "UPDATE categories SET offerReimbursement = 1 WHERE name = ?", ("Pass-Through",)
        )


def migrateToVersion16(connection):
    """Add optional Cash minimum guardrails without changing ledger data."""
    accountColumns = {row["name"] for row in connection.execute("PRAGMA table_info(accounts)")}
    if "minimumBalanceCents" not in accountColumns:
        connection.execute("ALTER TABLE accounts ADD COLUMN minimumBalanceCents INTEGER CHECK (minimumBalanceCents IS NULL OR (typeof(minimumBalanceCents) = 'integer' AND minimumBalanceCents > 0))")
    if "minimumCautionPercent" not in accountColumns:
        connection.execute("ALTER TABLE accounts ADD COLUMN minimumCautionPercent INTEGER CHECK (minimumCautionPercent IS NULL OR (typeof(minimumCautionPercent) = 'integer' AND minimumCautionPercent >= 0))")


def migrateToVersion17(connection):
    """Add optional per-card utilization warnings without changing financial data."""
    accountColumns = {row["name"] for row in connection.execute("PRAGMA table_info(accounts)")}
    if "utilizationWarningBasisPoints" not in accountColumns:
        connection.execute(
            "ALTER TABLE accounts ADD COLUMN utilizationWarningBasisPoints INTEGER "
            "CHECK (utilizationWarningBasisPoints IS NULL OR (typeof(utilizationWarningBasisPoints) = 'integer' "
            "AND utilizationWarningBasisPoints BETWEEN 1 AND 10000))"
        )


def createExternalSystemAccount(connection):
    connection.execute(
        "INSERT INTO accounts (name, accountClass, systemIdentity) VALUES ('External', 'system', 'external')"
    )


def validateExternalSystemAccount(connection):
    external = connection.execute(
        "SELECT id FROM accounts WHERE systemIdentity = 'external' AND accountClass = 'system' AND name = 'External' AND active = 1"
    ).fetchone()
    if not external:
        raise RuntimeError("Required External System Account is missing or invalid; restore a backup or contact support")


def migrateToVersion18(connection):
    """Add the immutable External transfer boundary without altering ledger rows."""
    accountColumns = {row["name"] for row in connection.execute("PRAGMA table_info(accounts)")}
    conflicting = connection.execute(
        "SELECT name FROM accounts WHERE lower(name) = 'external' AND accountClass != 'system'"
    ).fetchone()
    if conflicting:
        raise RuntimeError("Rename the existing user-owned account named External before upgrading")
    existingExternalSystem = "systemIdentity" in accountColumns and connection.execute(
        "SELECT 1 FROM accounts WHERE systemIdentity = 'external' AND accountClass = 'system'"
    ).fetchone()
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.executescript("""
            CREATE TABLE accountsVersion18 (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
                accountClass TEXT NOT NULL CHECK (accountClass IN ('cash', 'credit', 'system')),
                accountSubtype TEXT CHECK (
                    (accountClass = 'cash' AND (accountSubtype IS NULL OR accountSubtype IN ('checking', 'savings', 'other')))
                    OR (accountClass = 'credit' AND (accountSubtype IS NULL OR accountSubtype IN ('Credit Card', 'Personal Loan', 'Other')))
                    OR (accountClass = 'system' AND accountSubtype IS NULL)
                ),
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                displayOrder INTEGER NOT NULL DEFAULT 0,
                balanceOffsetCents INTEGER NOT NULL DEFAULT 0 CHECK (typeof(balanceOffsetCents) = 'integer'),
                creditLimitCents INTEGER CHECK (creditLimitCents IS NULL OR (typeof(creditLimitCents) = 'integer' AND creditLimitCents > 0)),
                minimumBalanceCents INTEGER CHECK (minimumBalanceCents IS NULL OR (typeof(minimumBalanceCents) = 'integer' AND minimumBalanceCents > 0)),
                minimumCautionPercent INTEGER CHECK (minimumCautionPercent IS NULL OR (typeof(minimumCautionPercent) = 'integer' AND minimumCautionPercent >= 0)),
                utilizationWarningBasisPoints INTEGER CHECK (utilizationWarningBasisPoints IS NULL OR (typeof(utilizationWarningBasisPoints) = 'integer' AND utilizationWarningBasisPoints BETWEEN 1 AND 10000)),
                systemIdentity TEXT UNIQUE CHECK (systemIdentity IS NULL OR systemIdentity = 'external'),
                CHECK (accountClass != 'system' OR (systemIdentity = 'external' AND name = 'External' AND active = 1 AND balanceOffsetCents = 0 AND creditLimitCents IS NULL AND minimumBalanceCents IS NULL AND minimumCautionPercent IS NULL AND utilizationWarningBasisPoints IS NULL))
            );
            INSERT INTO accountsVersion18 (id,name,accountClass,accountSubtype,active,displayOrder,balanceOffsetCents,creditLimitCents,minimumBalanceCents,minimumCautionPercent,utilizationWarningBasisPoints,systemIdentity)
            SELECT id,name,accountClass,accountSubtype,active,displayOrder,balanceOffsetCents,creditLimitCents,minimumBalanceCents,minimumCautionPercent,utilizationWarningBasisPoints,
                   CASE WHEN accountClass = 'system' THEN 'external' ELSE NULL END FROM accounts;
            DROP TABLE accounts;
            ALTER TABLE accountsVersion18 RENAME TO accounts;
        """)
        if not existingExternalSystem:
            createExternalSystemAccount(connection)
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise sqlite3.IntegrityError("Version 18 migration created a foreign key violation")


def migrateToVersion6(connection):
    """Make category financial meaning explicit while preserving historical outcomes."""
    categoryColumns = {row["name"] for row in connection.execute("PRAGMA table_info(categories)")}
    if "reportingRole" not in categoryColumns:
        connection.execute("ALTER TABLE categories ADD COLUMN reportingRole TEXT NOT NULL DEFAULT 'expense' CHECK (reportingRole IN ('expense', 'income'))")
    incomeCategory = connection.execute("SELECT id FROM categories WHERE name = ?", ("Income/Reimbursement",)).fetchone()
    if incomeCategory:
        connection.execute("UPDATE categories SET reportingRole = 'income' WHERE id = ?", (incomeCategory["id"],))
    connection.execute("CREATE TABLE IF NOT EXISTS applicationSettings (key TEXT PRIMARY KEY, value TEXT)")
    if incomeCategory:
        connection.execute("INSERT OR IGNORE INTO applicationSettings (key, value) VALUES ('reimbursementCategoryId', ?)", (str(incomeCategory["id"]),))


def migrateToVersion7(connection):
    """Add informational sinking-fund plans without changing the ledger."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS sinkingFundPlans (
            id INTEGER PRIMARY KEY,
            templateId INTEGER NOT NULL REFERENCES transactionTemplates(id) ON DELETE RESTRICT,
            frequency TEXT NOT NULL CHECK (frequency IN ('quarterly', 'yearly')),
            expectedDate TEXT NOT NULL CHECK (
                length(expectedDate) = 10
                AND expectedDate GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                AND date(expectedDate, '+0 days') = expectedDate
            ),
            displayOrder INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS oneActiveSinkingPlanPerTemplate
        ON sinkingFundPlans(templateId) WHERE active = 1;
        """
    )


def migrateToVersion8(connection):
    """Remove manual Sinking Fund ordering while preserving schedule anchors."""
    connection.executescript(
        """
        CREATE TABLE sinkingFundPlansVersion8 (
            id INTEGER PRIMARY KEY,
            templateId INTEGER NOT NULL REFERENCES transactionTemplates(id) ON DELETE RESTRICT,
            frequency TEXT NOT NULL CHECK (frequency IN ('quarterly', 'yearly')),
            expectedDate TEXT NOT NULL CHECK (
                length(expectedDate) = 10
                AND expectedDate GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                AND date(expectedDate, '+0 days') = expectedDate
            ),
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
        );
        INSERT INTO sinkingFundPlansVersion8
            (id, templateId, frequency, expectedDate, active)
        SELECT id, templateId, frequency, expectedDate, active
        FROM sinkingFundPlans;
        DROP TABLE sinkingFundPlans;
        ALTER TABLE sinkingFundPlansVersion8 RENAME TO sinkingFundPlans;
        CREATE UNIQUE INDEX oneActiveSinkingPlanPerTemplate
        ON sinkingFundPlans(templateId) WHERE active = 1;
        """
    )


def migrateToVersion9(connection):
    """Allow Monthly recurring plans while preserving every existing Budget plan."""
    connection.executescript(
        """
        CREATE TABLE sinkingFundPlansVersion9 (
            id INTEGER PRIMARY KEY,
            templateId INTEGER NOT NULL REFERENCES transactionTemplates(id) ON DELETE RESTRICT,
            frequency TEXT NOT NULL CHECK (frequency IN ('monthly', 'quarterly', 'yearly')),
            expectedDate TEXT NOT NULL CHECK (
                length(expectedDate) = 10
                AND expectedDate GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                AND date(expectedDate, '+0 days') = expectedDate
            ),
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
        );
        INSERT INTO sinkingFundPlansVersion9
            (id, templateId, frequency, expectedDate, active)
        SELECT id, templateId, frequency, expectedDate, active
        FROM sinkingFundPlans;
        DROP TABLE sinkingFundPlans;
        ALTER TABLE sinkingFundPlansVersion9 RENAME TO sinkingFundPlans;
        CREATE UNIQUE INDEX oneActiveSinkingPlanPerTemplate
        ON sinkingFundPlans(templateId) WHERE active = 1;
        """
    )


def migrateToVersion10(connection):
    """Add informational flexible monthly planning without changing the ledger."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS flexibleCategoryBudgets (
            id INTEGER PRIMARY KEY,
            categoryId INTEGER NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
            monthlyTargetCents INTEGER NOT NULL CHECK (typeof(monthlyTargetCents) = 'integer' AND monthlyTargetCents > 0),
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS oneActiveFlexibleBudgetPerCategory
        ON flexibleCategoryBudgets(categoryId) WHERE active = 1;
        CREATE TABLE IF NOT EXISTS expectedIncomePlans (
            id INTEGER PRIMARY KEY,
            templateId INTEGER NOT NULL REFERENCES transactionTemplates(id) ON DELETE RESTRICT,
            expectedDate TEXT NOT NULL CHECK (length(expectedDate) = 10 AND date(expectedDate, '+0 days') = expectedDate),
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
        );
        CREATE TABLE IF NOT EXISTS plannedCreditPayments (
            id INTEGER PRIMARY KEY,
            expectedDate TEXT NOT NULL CHECK (length(expectedDate) = 10 AND date(expectedDate, '+0 days') = expectedDate),
            accountId INTEGER NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
            description TEXT NOT NULL CHECK (length(trim(description)) > 0),
            amountCents INTEGER NOT NULL CHECK (typeof(amountCents) = 'integer' AND amountCents > 0),
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
        );
        """
    )


def migrateToVersion11(connection):
    """Add the transaction-entry amount-prefill preference to existing templates."""
    templateColumns = {row["name"] for row in connection.execute("PRAGMA table_info(transactionTemplates)")}
    if "prefillAmount" not in templateColumns:
        connection.execute(
            "ALTER TABLE transactionTemplates ADD COLUMN prefillAmount INTEGER NOT NULL DEFAULT 1 CHECK (prefillAmount IN (0, 1))"
        )


def migrateToVersion12(connection):
    """Add independent, configuration-only Money Simulator plans and rows."""
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS simulatorPlans (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL CHECK (length(trim(name)) > 0),
            planDate TEXT NOT NULL CHECK (length(planDate) = 10 AND date(planDate, '+0 days') = planDate)
        );
        CREATE TABLE IF NOT EXISTS simulatorRows (
            id INTEGER PRIMARY KEY,
            planId INTEGER NOT NULL REFERENCES simulatorPlans(id) ON DELETE CASCADE,
            rowType TEXT NOT NULL CHECK (rowType IN ('live', 'income', 'spend', 'transfer')),
            rowOrder INTEGER NOT NULL,
            fromAccountId INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
            toAccountId INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
            categoryId INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
            label TEXT,
            amountCents INTEGER CHECK (amountCents IS NULL OR (typeof(amountCents) = 'integer' AND amountCents > 0)),
            amountMode TEXT NOT NULL DEFAULT 'fixed' CHECK (amountMode IN ('fixed', 'payoff'))
        );
        CREATE INDEX IF NOT EXISTS simulatorRowsPlanIndex ON simulatorRows(planId, rowOrder);
    """)


def migrateToVersion13(connection):
    """Add an explicit derived payoff amount mode without changing existing rows."""
    rowColumns = {row["name"] for row in connection.execute("PRAGMA table_info(simulatorRows)")}
    if "amountMode" not in rowColumns:
        connection.execute(
            "ALTER TABLE simulatorRows ADD COLUMN amountMode TEXT NOT NULL DEFAULT 'fixed' "
            "CHECK (amountMode IN ('fixed', 'payoff'))"
        )


def migrateToVersion14(connection):
    """Add one-way Simulator execution state and canonical ledger-row links."""
    rowColumns = {row["name"] for row in connection.execute("PRAGMA table_info(simulatorRows)")}
    if "executed" not in rowColumns:
        connection.execute(
            "ALTER TABLE simulatorRows ADD COLUMN executed INTEGER NOT NULL DEFAULT 0 "
            "CHECK (executed IN (0, 1))"
        )
    if "executedTransactionId" not in rowColumns:
        connection.execute(
            "ALTER TABLE simulatorRows ADD COLUMN executedTransactionId INTEGER "
            "REFERENCES transactions(id) ON DELETE SET NULL"
        )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS simulatorTransactionUnlink
           BEFORE DELETE ON transactions BEGIN
               UPDATE simulatorRows SET executed=0, executedTransactionId=NULL
               WHERE executedTransactionId=OLD.id;
           END"""
    )


def migrateToVersion15(connection):
    """Allow planning-only Buffer rows without adding ledger state."""
    connection.executescript("""
        DROP TRIGGER IF EXISTS simulatorTransactionUnlink;
        DROP INDEX IF EXISTS simulatorRowsPlanIndex;
        ALTER TABLE simulatorRows RENAME TO simulatorRowsVersion14;
        CREATE TABLE simulatorRows (
            id INTEGER PRIMARY KEY,
            planId INTEGER NOT NULL REFERENCES simulatorPlans(id) ON DELETE CASCADE,
            rowType TEXT NOT NULL CHECK (rowType IN ('live', 'income', 'spend', 'transfer', 'buffer')),
            rowOrder INTEGER NOT NULL,
            fromAccountId INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
            toAccountId INTEGER REFERENCES accounts(id) ON DELETE RESTRICT,
            categoryId INTEGER REFERENCES categories(id) ON DELETE RESTRICT,
            label TEXT,
            amountCents INTEGER CHECK (amountCents IS NULL OR (typeof(amountCents) = 'integer' AND amountCents > 0)),
            amountMode TEXT NOT NULL DEFAULT 'fixed' CHECK (amountMode IN ('fixed', 'payoff')),
            executed INTEGER NOT NULL DEFAULT 0 CHECK (executed IN (0, 1)),
            executedTransactionId INTEGER REFERENCES transactions(id) ON DELETE SET NULL
        );
        INSERT INTO simulatorRows
            (id, planId, rowType, rowOrder, fromAccountId, toAccountId, categoryId,
             label, amountCents, amountMode, executed, executedTransactionId)
        SELECT id, planId, rowType, rowOrder, fromAccountId, toAccountId, categoryId,
               label, amountCents, amountMode, executed, executedTransactionId
        FROM simulatorRowsVersion14;
        DROP TABLE simulatorRowsVersion14;
        CREATE INDEX simulatorRowsPlanIndex ON simulatorRows(planId, rowOrder);
        CREATE TRIGGER simulatorTransactionUnlink
        BEFORE DELETE ON transactions
        BEGIN
            UPDATE simulatorRows
            SET executed = 0, executedTransactionId = NULL
            WHERE executedTransactionId = OLD.id;
        END;
    """)


def parseUsdTermCents(amountText, allowSign=True):
    """Parse one strict USD term to cents; expression arithmetic happens separately."""
    signPattern = r"[+-]?" if allowSign else ""
    if re.fullmatch(rf"{signPattern}\$?(?:\d+\.\d{{3,}}|\.\d{{3,}})", amountText):
        raise ValidationError("Amount must have no more than two decimal places")
    match = re.fullmatch(
        rf"(?P<sign>{signPattern})(?:\$)?(?P<whole>(?:0|[1-9]\d*)|(?:[1-9]\d{{0,2}}(?:,\d{{3}})+))?(?:\.(?P<fraction>\d{{1,2}}))?",
        amountText,
    )
    if not match or (match.group("whole") is None and match.group("fraction") is None):
        raise ValidationError("Amount must use valid USD format")
    wholeCents = int((match.group("whole") or "0").replace(",", "")) * 100
    fraction = (match.group("fraction") or "").ljust(2, "0")
    amountCents = wholeCents + int(fraction or "0")
    return -amountCents if match.group("sign") == "-" else amountCents


def parseMoneyExpressionCents(rawAmount):
    """Evaluate the deliberately tiny USD addition/subtraction grammar in integer cents."""
    if isinstance(rawAmount, bool) or rawAmount is None:
        raise ValidationError("Amount is required")
    amountText = str(rawAmount).strip()
    if not amountText:
        raise ValidationError("Amount is required")
    if len(amountText) > 200:
        raise ValidationError("Amount expression is too long")
    firstSignLength = 1 if amountText[0] in "+-" else 0
    terms = re.split(r"\s*([+-])\s*", amountText[firstSignLength:])
    if firstSignLength:
        terms[0] = amountText[0] + terms[0]
    if len(terms) > 99:
        raise ValidationError("Amount expression has too many terms")
    amountCents = parseUsdTermCents(terms[0], allowSign=True)
    for index in range(1, len(terms), 2):
        operator, termText = terms[index], terms[index + 1]
        termCents = parseUsdTermCents(termText, allowSign=False)
        amountCents = amountCents + termCents if operator == "+" else amountCents - termCents
    if abs(amountCents) > 9223372036854775807:
        raise ValidationError("Amount is too large")
    return amountCents


def parseMoneyCents(rawAmount, positiveOnly=False):
    """Parse a strict USD value or limited addition/subtraction expression to cents."""
    amountCents = parseMoneyExpressionCents(rawAmount)
    if positiveOnly and amountCents <= 0:
        raise ValidationError("Amount must be greater than zero")
    return amountCents


def validateDate(rawDate):
    try:
        parsedDate = datetime.strptime(str(rawDate), "%Y-%m-%d").date()
    except ValueError as error:
        raise ValidationError("Date must use YYYY-MM-DD") from error
    if parsedDate.isoformat() != rawDate:
        raise ValidationError("Date must use YYYY-MM-DD")
    return rawDate


def requireText(rawText, fieldName):
    textValue = str(rawText or "").strip()
    if not textValue:
        raise ValidationError(f"{fieldName} is required")
    return textValue


def getAccount(connection, accountId, activeRequired=False):
    account = connection.execute(
        "SELECT * FROM accounts WHERE id = ?", (accountId,)
    ).fetchone()
    if not account:
        raise ValidationError("Account does not exist")
    if activeRequired and not account["active"]:
        raise ValidationError("Account is archived")
    return account


def getSelectableCategory(connection, categoryId, allowArchived=False):
    category = connection.execute(
        """
        SELECT c.*, EXISTS(
            SELECT 1 FROM categories AS child WHERE child.parentCategoryId = c.id
        ) AS hasChildren
        FROM categories AS c
        WHERE c.id = ?
        """,
        (categoryId,),
    ).fetchone()
    if not category:
        raise ValidationError("Category does not exist")
    if not allowArchived and not category["active"]:
        raise ValidationError("Category is archived")
    if category["hasChildren"]:
        raise ValidationError("Rollup categories cannot receive transactions")
    return category


def storedAmountForCategory(reportingRole, enteredAmountCents):
    if reportingRole == "income":
        return enteredAmountCents
    return -enteredAmountCents


def prepareTransaction(connection, accountId, transactionDate, description, categoryId, enteredAmount, pinned=False):
    validateDate(transactionDate)
    description = requireText(description, "Description")
    account = getAccount(connection, accountId, activeRequired=True)
    if account["accountClass"] == "system":
        raise ValidationError("System Accounts may only be used for transfers")
    category = getSelectableCategory(connection, categoryId)
    enteredAmountCents = parseMoneyCents(enteredAmount)
    storedAmountCents = storedAmountForCategory(category["reportingRole"], enteredAmountCents)
    return (
        account["id"], transactionDate, description, storedAmountCents, category["id"],
        1 if pinned else 0,
    )


def createTransaction(connection, accountId, transactionDate, description, categoryId, enteredAmount, pinned=False):
    transactionValues = prepareTransaction(
        connection, accountId, transactionDate, description, categoryId, enteredAmount, pinned
    )
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        getSelectableCategory(connection, transactionValues[4])
        cursor = connection.execute(
            """
            INSERT INTO transactions (accountId, date, description, amountCents, categoryId, pinned)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            transactionValues,
        )
    return cursor.lastrowid


def createTransactionWithReimbursement(
    connection, accountId, transactionDate, description, categoryId, enteredAmount, pinned,
    reimbursementAccountId, reimbursementDate, reimbursementDescription, reimbursementAmount,
):
    expenseValues = prepareTransaction(
        connection, accountId, transactionDate, description, categoryId, enteredAmount, pinned
    )
    expenseCategory = connection.execute(
        "SELECT name, offerReimbursement FROM categories WHERE id = ?", (expenseValues[4],)
    ).fetchone()
    if not expenseCategory["offerReimbursement"]:
        raise ValidationError("Reimbursement shortcut is not enabled for this category")
    reimbursementCategory = connection.execute(
        """SELECT c.id FROM applicationSettings AS s JOIN categories AS c ON c.id = CAST(s.value AS INTEGER)
           WHERE s.key = 'reimbursementCategoryId' AND c.active = 1 AND c.reportingRole = 'income'
             AND c.parentCategoryId IS NULL
             AND NOT EXISTS (SELECT 1 FROM categories child WHERE child.parentCategoryId = c.id)"""
    ).fetchone()
    if not reimbursementCategory:
        raise ValidationError("Select a valid Reimbursement Category in Settings")
    reimbursementValues = prepareTransaction(
        connection, reimbursementAccountId, reimbursementDate, reimbursementDescription,
        reimbursementCategory["id"], reimbursementAmount, False,
    )
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        getSelectableCategory(connection, expenseValues[4])
        getSelectableCategory(connection, reimbursementValues[4])
        cursors = []
        for transactionValues in (expenseValues, reimbursementValues):
            cursors.append(connection.execute(
                """
                INSERT INTO transactions
                    (accountId, date, description, amountCents, categoryId, pinned)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                transactionValues,
            ))
    return cursors[0].lastrowid, cursors[1].lastrowid


def createBulkTransactions(connection, transactionRows):
    if not isinstance(transactionRows, list):
        raise ValidationError("Bulk rows must be a list")
    preparedRows = []
    rowErrors = []
    for rowNumber, transactionRow in enumerate(transactionRows, 1):
        if not isinstance(transactionRow, dict):
            rowErrors.append(f"Row {rowNumber}: row must be an object")
            continue
        if any(key in transactionRow for key in ("transferId", "fromAccountId", "toAccountId")):
            rowErrors.append(f"Row {rowNumber}: transfers are not supported in Bulk Entry")
            continue
        nonDateValues = [
            transactionRow.get("accountId"), transactionRow.get("description"),
            transactionRow.get("categoryId"), transactionRow.get("amount"),
        ]
        if not any(str(value or "").strip() for value in nonDateValues):
            continue
        try:
            prepared = prepareTransaction(
                connection, transactionRow.get("accountId"), transactionRow.get("date"),
                transactionRow.get("description"), transactionRow.get("categoryId"),
                transactionRow.get("amount"), False,
            )
            if prepared[3] == 0:
                raise ValidationError("Amount must be nonzero")
            preparedRows.append(prepared)
        except (ValidationError, TypeError, ValueError) as error:
            rowErrors.append(f"Row {rowNumber}: {error}")
    if rowErrors:
        raise ValidationError("; ".join(rowErrors))
    if not preparedRows:
        raise ValidationError("Enter at least one transaction")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        for transactionValues in preparedRows:
            getSelectableCategory(connection, transactionValues[4])
        connection.executemany(
            """
            INSERT INTO transactions
                (accountId, date, description, amountCents, categoryId, pinned)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            preparedRows,
        )
    return len(preparedRows)


def updateTransaction(connection, transactionId, accountId, transactionDate, description, categoryId, enteredAmount, pinned=False):
    existing = connection.execute(
        "SELECT * FROM transactions WHERE id = ?", (transactionId,)
    ).fetchone()
    if not existing:
        raise ValidationError("Transaction does not exist")
    if existing["transferId"]:
        raise ValidationError("Edit linked transfers through the transfer operation")
    validateDate(transactionDate)
    description = requireText(description, "Description")
    try:
        selectedAccountId = int(accountId)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValidationError("Account ID must be an integer") from error
    account = getAccount(
        connection, accountId,
        activeRequired=selectedAccountId != existing["accountId"],
    )
    if account["accountClass"] == "system":
        raise ValidationError("System Accounts may only be used for transfers")
    enteredAmountCents = parseMoneyCents(enteredAmount)
    if categoryId in (None, ""):
        if existing["categoryId"] is not None:
            raise ValidationError("Category is required")
        category = None
        storedAmountCents = enteredAmountCents
    else:
        category = getSelectableCategory(
            connection, categoryId,
            allowArchived=int(categoryId) == existing["categoryId"],
        )
        storedAmountCents = storedAmountForCategory(category["reportingRole"], enteredAmountCents)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if category is not None:
            getSelectableCategory(connection, category["id"], allowArchived=category["id"] == existing["categoryId"])
        connection.execute(
            """
            UPDATE transactions
            SET accountId = ?, date = ?, description = ?, amountCents = ?, categoryId = ?, pinned = ?
            WHERE id = ?
            """,
            (account["id"], transactionDate, description, storedAmountCents,
             category["id"] if category else None, 1 if pinned else 0, transactionId),
        )


def deleteTransaction(connection, transactionId):
    existing = connection.execute(
        "SELECT transferId FROM transactions WHERE id = ?", (transactionId,)
    ).fetchone()
    if not existing:
        raise ValidationError("Transaction does not exist")
    with connection:
        linkedIds = [transactionId]
        if existing["transferId"]:
            linkedIds = [row[0] for row in connection.execute(
                "SELECT id FROM transactions WHERE transferId = ?", (existing["transferId"],)
            )]
        connection.executemany(
            "UPDATE simulatorRows SET executed = 0, executedTransactionId = NULL WHERE executedTransactionId = ?",
            ((linkedId,) for linkedId in linkedIds),
        )
        if existing["transferId"]:
            connection.execute(
                "DELETE FROM transactions WHERE transferId = ?", (existing["transferId"],)
            )
        else:
            connection.execute("DELETE FROM transactions WHERE id = ?", (transactionId,))


def prepareTransfer(connection, transactionDate, fromAccountId, toAccountId, enteredAmount, description):
    validateDate(transactionDate)
    description = requireText(description, "Description")
    amountCents = parseMoneyCents(enteredAmount, positiveOnly=True)
    fromAccount = getAccount(connection, fromAccountId, activeRequired=True)
    toAccount = getAccount(connection, toAccountId, activeRequired=True)
    if fromAccount["id"] == toAccount["id"]:
        raise ValidationError("Transfer accounts must be different")
    return fromAccount["id"], toAccount["id"], transactionDate, description, amountCents


def insertTransfer(connection, values):
    fromId, toId, transactionDate, description, amountCents = values
    transferId = str(uuid4())
    for accountId, signedAmount in ((fromId, -amountCents), (toId, amountCents)):
        connection.execute(
            "INSERT INTO transactions (accountId, date, description, amountCents, categoryId, transferId) VALUES (?, ?, ?, ?, NULL, ?)",
            (accountId, transactionDate, description, signedAmount, transferId),
        )
    return transferId


def createTransfer(connection, transactionDate, fromAccountId, toAccountId, enteredAmount, description="Transfer"):
    values = prepareTransfer(connection, transactionDate, fromAccountId, toAccountId, enteredAmount, description)
    with connection:
        return insertTransfer(connection, values)


def updateTransfer(connection, transferId, transactionDate, fromAccountId, toAccountId, enteredAmount, description="Transfer"):
    transferRows = connection.execute(
        "SELECT * FROM transactions WHERE transferId = ? ORDER BY amountCents", (transferId,)
    ).fetchall()
    if len(transferRows) != 2 or sum(row["amountCents"] for row in transferRows) != 0:
        raise ValidationError("Transfer is incomplete or invalid")
    validateDate(transactionDate)
    description = requireText(description, "Description")
    amountCents = parseMoneyCents(enteredAmount, positiveOnly=True)
    try:
        selectedFromAccountId = int(fromAccountId)
        selectedToAccountId = int(toAccountId)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValidationError("Transfer Account IDs must be integers") from error
    if selectedFromAccountId == selectedToAccountId:
        raise ValidationError("Transfer accounts must be different")
    currentAccountIds = {row["accountId"] for row in transferRows}
    fromAccount = getAccount(
        connection, fromAccountId, activeRequired=selectedFromAccountId not in currentAccountIds
    )
    toAccount = getAccount(
        connection, toAccountId, activeRequired=selectedToAccountId not in currentAccountIds
    )
    linkedSimulatorRowIds = [row["id"] for row in connection.execute(
        "SELECT id FROM simulatorRows WHERE executedTransactionId IN (?, ?)",
        (transferRows[0]["id"], transferRows[1]["id"]),
    )]
    with connection:
        connection.execute("DELETE FROM transactions WHERE transferId = ?", (transferId,))
        fromCursor = connection.execute(
            """
            INSERT INTO transactions
                (accountId, date, description, amountCents, categoryId, transferId)
            VALUES (?, ?, ?, ?, NULL, ?)
            """,
            (fromAccount["id"], transactionDate, description, -amountCents, transferId),
        )
        connection.execute(
            """INSERT INTO transactions
                   (accountId, date, description, amountCents, categoryId, transferId)
               VALUES (?, ?, ?, ?, NULL, ?)""",
            (toAccount["id"], transactionDate, description, amountCents, transferId),
        )
        if linkedSimulatorRowIds:
            connection.executemany(
                "UPDATE simulatorRows SET executed = 1, executedTransactionId = ? WHERE id = ?",
                ((fromCursor.lastrowid, rowId) for rowId in linkedSimulatorRowIds),
            )


def creditLimitValue(accountClass, accountSubtype, creditLimit):
    if accountClass != "credit" or accountSubtype != "Credit Card":
        return None
    if creditLimit in (None, ""):
        return None
    return parseMoneyCents(creditLimit, positiveOnly=True)


def utilizationWarningValue(accountClass, accountSubtype, creditLimitCents, warningPercent):
    if accountClass != "credit" or accountSubtype != "Credit Card" or creditLimitCents is None:
        return None
    if warningPercent in (None, ""):
        return None
    text = str(warningPercent).strip()
    if not re.fullmatch(r"\d{1,3}(?:\.\d{1,2})?", text):
        raise ValidationError("Utilization Warning must be from 0.01 through 100.00 with at most two decimal places")
    whole, _, fraction = text.partition(".")
    basisPoints = int(whole) * 100 + int((fraction + "00")[:2])
    if not 1 <= basisPoints <= 10000:
        raise ValidationError("Utilization Warning must be from 0.01 through 100.00")
    return basisPoints


def utilizationWarningHelper(creditLimit, warningPercent):
    creditLimitCents = creditLimitValue("credit", "Credit Card", creditLimit)
    basisPoints = utilizationWarningValue("credit", "Credit Card", creditLimitCents, warningPercent)
    if creditLimitCents is None or basisPoints is None:
        return None
    return (creditLimitCents * basisPoints + 9999) // 10000


def cashMinimumValues(accountClass, minimumBalance, minimumCautionPercent):
    if accountClass != "cash" or minimumBalance in (None, ""):
        return None, None
    minimumBalanceCents = parseMoneyCents(minimumBalance)
    if minimumBalanceCents == 0:
        return None, None
    if minimumBalanceCents < 0:
        raise ValidationError("Minimum Balance cannot be negative")
    rawPercent = 25 if minimumCautionPercent in (None, "") else minimumCautionPercent
    try:
        cautionPercent = int(rawPercent)
    except (TypeError, ValueError) as error:
        raise ValidationError("Caution Range must be a nonnegative whole-number percentage") from error
    if isinstance(rawPercent, bool) or str(rawPercent).strip() != str(cautionPercent) or cautionPercent < 0:
        raise ValidationError("Caution Range must be a nonnegative whole-number percentage")
    return minimumBalanceCents, cautionPercent


def minimumStatus(minimumBalanceCents, cautionPercent, balanceCents):
    if minimumBalanceCents is None:
        return None
    greenThresholdCents = (minimumBalanceCents * (100 + cautionPercent) + 99) // 100
    differenceCents = balanceCents - minimumBalanceCents
    state = "green" if balanceCents >= greenThresholdCents else "yellow" if balanceCents >= minimumBalanceCents else "red"
    if differenceCents == 0:
        label = "At minimum"
    elif differenceCents > 0:
        label = f"${differenceCents // 100:,}.{differenceCents % 100:02d} above minimum"
    else:
        belowCents = -differenceCents
        label = f"${belowCents // 100:,}.{belowCents % 100:02d} below minimum"
    return {"state": state, "label": label, "greenThresholdCents": greenThresholdCents}


def createAccount(connection, name, accountClass, accountSubtype=None, displayOrder=0, openingBalance=None, creditLimit=None, minimumBalance=None, minimumCautionPercent=None, utilizationWarning=None, pinnedForSelection=False):
    if type(pinnedForSelection) is not bool:
        raise ValidationError("Pin in account selectors must be Boolean")
    name = requireText(name, "Account name")
    if name.casefold() == "external":
        raise ValidationError("External is reserved for the built-in System Account")
    if accountClass not in {"cash", "credit"}:
        raise ValidationError("Account class must be cash or credit")
    accountSubtype = accountSubtype or None
    if accountClass == "credit" and accountSubtype not in {None, "Credit Card", "Personal Loan", "Other"}:
        raise ValidationError("Credit account subtype is invalid")
    if accountClass == "cash" and accountSubtype not in {None, "checking", "savings", "other"}:
        raise ValidationError("Cash account subtype is invalid")
    try:
        displayOrder = int(displayOrder)
    except (TypeError, ValueError) as error:
        raise ValidationError("Display order must be an integer") from error
    openingAmountCents = 0
    creditLimitCents = creditLimitValue(accountClass, accountSubtype, creditLimit)
    utilizationWarningBasisPoints = utilizationWarningValue(accountClass, accountSubtype, creditLimitCents, utilizationWarning)
    minimumBalanceCents, minimumCautionPercent = cashMinimumValues(accountClass, minimumBalance, minimumCautionPercent)
    if openingBalance not in (None, ""):
        openingAmountCents = parseMoneyCents(openingBalance)
        if accountClass == "credit":
            if openingAmountCents < 0:
                raise ValidationError("Credit opening amount owed cannot be negative")
            openingAmountCents = -openingAmountCents
    try:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO accounts (
                    name, accountClass, accountSubtype, displayOrder, balanceOffsetCents, creditLimitCents,
                    minimumBalanceCents, minimumCautionPercent, utilizationWarningBasisPoints, pinnedForSelection
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (name, accountClass, accountSubtype, displayOrder, openingAmountCents, creditLimitCents,
                 minimumBalanceCents, minimumCautionPercent, utilizationWarningBasisPoints, int(pinnedForSelection)),
            )
            accountId = cursor.lastrowid
        return accountId
    except sqlite3.IntegrityError as error:
        raise ValidationError("Account name must be unique") from error


def updateAccount(connection, accountId, name, accountClass, accountSubtype=None, displayOrder=0, active=True, creditLimit=None, minimumBalance=None, minimumCautionPercent=None, utilizationWarning=None, pinnedForSelection=None):
    existingAccount = getAccount(connection, accountId)
    if pinnedForSelection is None:
        pinnedForSelection = bool(existingAccount["pinnedForSelection"])
    if not existingAccount["active"] and not active and pinnedForSelection:
        raise ValidationError("Archived Accounts cannot be pinned")
    if existingAccount["accountClass"] == "system":
        raise ValidationError("System Accounts are managed by No Part Finance and cannot be modified")
    if type(pinnedForSelection) is not bool:
        raise ValidationError("Pin in account selectors must be Boolean")
    name = requireText(name, "Account name")
    if name.casefold() == "external":
        raise ValidationError("External is reserved for the built-in System Account")
    if accountClass not in {"cash", "credit"}:
        raise ValidationError("Account class must be cash or credit")
    accountSubtype = accountSubtype or None
    if accountClass == "credit" and accountSubtype not in {None, "Credit Card", "Personal Loan", "Other"}:
        raise ValidationError("Credit account subtype is invalid")
    if accountClass == "cash" and accountSubtype not in {None, "checking", "savings", "other"}:
        raise ValidationError("Cash account subtype is invalid")
    creditLimitCents = creditLimitValue(accountClass, accountSubtype, creditLimit)
    utilizationWarningBasisPoints = utilizationWarningValue(accountClass, accountSubtype, creditLimitCents, utilizationWarning)
    minimumBalanceCents, minimumCautionPercent = cashMinimumValues(accountClass, minimumBalance, minimumCautionPercent)
    if not active and connection.execute(
        """SELECT 1 FROM sinkingFundPlans p JOIN transactionTemplates tt ON tt.id = p.templateId
           WHERE p.active = 1 AND tt.accountId = ?""", (accountId,)
    ).fetchone():
        raise ValidationError("Archive or remove the active Sinking Fund plan before archiving this account")
    if not active and connection.execute(
        """SELECT 1 FROM expectedIncomePlans p JOIN transactionTemplates tt ON tt.id = p.templateId
           WHERE p.active = 1 AND tt.accountId = ?""", (accountId,)
    ).fetchone():
        raise ValidationError("Archive or remove the active expected income plan before archiving this account")
    if connection.execute(
        "SELECT 1 FROM plannedCreditPayments WHERE active = 1 AND accountId = ?", (accountId,)
    ).fetchone() and (not active or accountClass != "credit" or accountSubtype not in {"Credit Card", "Personal Loan"}):
        raise ValidationError("Archive or remove the active planned debt payment before changing this account")
    try:
        displayOrder = int(displayOrder)
        activeValue = 1 if active else 0
        with connection:
            connection.execute(
                """
                UPDATE accounts
                SET name = ?, accountClass = ?, accountSubtype = ?, displayOrder = ?, active = ?, creditLimitCents = ?,
                    minimumBalanceCents = ?, minimumCautionPercent = ?, utilizationWarningBasisPoints = ?, pinnedForSelection = ?
                WHERE id = ?
                """,
                (name, accountClass, accountSubtype, displayOrder, activeValue, creditLimitCents,
                 minimumBalanceCents, minimumCautionPercent, utilizationWarningBasisPoints, int(pinnedForSelection and active), accountId),
            )
    except sqlite3.IntegrityError as error:
        raise ValidationError("Account name must be unique") from error


def reconcileAccountBalance(connection, accountId, actualBalance):
    account = getAccount(connection, accountId)
    if account["accountClass"] == "system":
        raise ValidationError("System Accounts cannot be reconciled")
    actualBalanceCents = parseMoneyCents(actualBalance)
    if account["accountClass"] == "credit":
        if actualBalanceCents < 0:
            raise ValidationError("Credit amount owed cannot be negative")
        actualBalanceCents = -actualBalanceCents
    transactionBalanceCents = connection.execute(
        "SELECT COALESCE(SUM(amountCents), 0) FROM transactions WHERE accountId = ?",
        (accountId,),
    ).fetchone()[0]
    currentBalanceCents = account["balanceOffsetCents"] + transactionBalanceCents
    differenceCents = actualBalanceCents - currentBalanceCents
    newOffsetCents = account["balanceOffsetCents"] + differenceCents
    with connection:
        connection.execute(
            "UPDATE accounts SET balanceOffsetCents = ? WHERE id = ?",
            (newOffsetCents, accountId),
        )
    return newOffsetCents, differenceCents


def validateRollupParent(connection, parentCategoryId):
    if not parentCategoryId:
        return
    parent = connection.execute("SELECT * FROM categories WHERE id = ?", (parentCategoryId,)).fetchone()
    if not parent or parent["parentCategoryId"] is not None or parent["reportingRole"] != "expense":
        raise ValidationError("Parent must be an existing top-level category")
    if connection.execute("SELECT 1 FROM transactions WHERE categoryId = ? LIMIT 1", (parentCategoryId,)).fetchone():
        raise ValidationError("A category with transaction history cannot become a rollup. Create an empty parent and move this category beneath it instead.")
    if connection.execute("SELECT 1 FROM transactionTemplates WHERE categoryId = ? LIMIT 1", (parentCategoryId,)).fetchone():
        raise ValidationError("Change or remove Template references before making this category a rollup")
    if connection.execute("SELECT 1 FROM flexibleCategoryBudgets WHERE categoryId = ? LIMIT 1", (parentCategoryId,)).fetchone():
        raise ValidationError("Change or remove Flexible Category Budget references before making this category a rollup")


def createCategory(connection, name, parentCategoryId=None, displayOrder=0, offerReimbursement=False, reportingRole="expense"):
    name = requireText(name, "Category name")
    parentCategoryId = int(parentCategoryId) if parentCategoryId not in (None, "") else None
    if reportingRole not in {"expense", "income"}:
        raise ValidationError("Reporting role must be Expense or Income")
    if reportingRole == "income" and parentCategoryId:
        raise ValidationError("Income categories must be standalone")
    if parentCategoryId:
        if connection.execute(
            """SELECT 1 FROM sinkingFundPlans p JOIN transactionTemplates tt ON tt.id = p.templateId
               WHERE p.active = 1 AND tt.categoryId = ?""", (parentCategoryId,)
        ).fetchone():
            raise ValidationError("Archive or remove the active Sinking Fund plan before making its category a rollup")
        if connection.execute(
            "SELECT 1 FROM flexibleCategoryBudgets WHERE active = 1 AND categoryId = ?", (parentCategoryId,)
        ).fetchone():
            raise ValidationError("Remove the active flexible category budget before making its category a rollup")
        parent = connection.execute(
            "SELECT * FROM categories WHERE id = ?", (parentCategoryId,)
        ).fetchone()
        if not parent or parent["parentCategoryId"] is not None or parent["reportingRole"] != "expense":
            raise ValidationError("Parent must be an existing top-level category")
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            validateRollupParent(connection, parentCategoryId)
            cursor = connection.execute(
                "INSERT INTO categories (name, parentCategoryId, displayOrder, offerReimbursement, reportingRole) VALUES (?, ?, ?, ?, ?)",
                (name, parentCategoryId, int(displayOrder), 1 if offerReimbursement else 0, reportingRole),
            )
        return cursor.lastrowid
    except ValidationError:
        raise
    except (sqlite3.IntegrityError, ValueError) as error:
        raise ValidationError("Category name must be unique and display order must be an integer") from error


def updateCategory(connection, categoryId, name, parentCategoryId=None, displayOrder=0, active=True, offerReimbursement=False, reportingRole="expense"):
    category = connection.execute(
        "SELECT * FROM categories WHERE id = ?", (categoryId,)
    ).fetchone()
    if not category:
        raise ValidationError("Category does not exist")
    name = requireText(name, "Category name")
    if reportingRole not in {"expense", "income"}:
        raise ValidationError("Reporting role must be Expense or Income")
    parentCategoryId = int(parentCategoryId) if parentCategoryId not in (None, "") else None
    if parentCategoryId == int(categoryId):
        raise ValidationError("A category cannot be its own parent")
    hasChildren = connection.execute(
        "SELECT 1 FROM categories WHERE parentCategoryId = ? LIMIT 1", (categoryId,)
    ).fetchone()
    configured = connection.execute("SELECT 1 FROM applicationSettings WHERE key = 'reimbursementCategoryId' AND value = ?", (str(categoryId),)).fetchone()
    if configured and (not active or reportingRole != "income" or parentCategoryId):
        raise ValidationError("Choose another Reimbursement Category before changing this category")
    if reportingRole != category["reportingRole"] and connection.execute("SELECT 1 FROM transactions WHERE categoryId = ? LIMIT 1", (categoryId,)).fetchone():
        raise ValidationError("Reporting Role cannot change after a category has transactions")
    if reportingRole == "income" and (parentCategoryId or hasChildren):
        raise ValidationError("Income categories must be standalone")
    referencedByActivePlan = connection.execute(
        """SELECT 1 FROM sinkingFundPlans p JOIN transactionTemplates tt ON tt.id = p.templateId
           WHERE p.active = 1 AND tt.categoryId = ?""", (categoryId,)
    ).fetchone()
    if referencedByActivePlan and (not active or reportingRole != "expense" or parentCategoryId):
        raise ValidationError("Archive or remove the active Sinking Fund plan before changing this category")
    if connection.execute(
        "SELECT 1 FROM flexibleCategoryBudgets WHERE active = 1 AND categoryId = ?", (categoryId,)
    ).fetchone() and (not active or reportingRole != "expense" or parentCategoryId or hasChildren):
        raise ValidationError("Remove the active flexible category budget before changing this category")
    if connection.execute(
        """SELECT 1 FROM expectedIncomePlans p JOIN transactionTemplates tt ON tt.id = p.templateId
           WHERE p.active = 1 AND tt.categoryId = ?""", (categoryId,)
    ).fetchone() and (not active or reportingRole != "income" or parentCategoryId or hasChildren):
        raise ValidationError("Archive or remove the active expected income plan before changing this category")
    if parentCategoryId and connection.execute(
        """SELECT 1 FROM sinkingFundPlans p JOIN transactionTemplates tt ON tt.id = p.templateId
           WHERE p.active = 1 AND tt.categoryId = ?""", (parentCategoryId,)
    ).fetchone():
        raise ValidationError("Archive or remove the active Sinking Fund plan before making its category a rollup")
    if parentCategoryId and hasChildren:
        raise ValidationError("A rollup category cannot become a child")
    if parentCategoryId:
        parent = connection.execute(
            "SELECT * FROM categories WHERE id = ?", (parentCategoryId,)
        ).fetchone()
        if not parent or parent["parentCategoryId"] is not None or parent["reportingRole"] != "expense":
            raise ValidationError("Parent must be an existing top-level category")
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            validateRollupParent(connection, parentCategoryId)
            connection.execute(
                """
                UPDATE categories
                SET name = ?, parentCategoryId = ?, displayOrder = ?, active = ?, offerReimbursement = ?, reportingRole = ?
                WHERE id = ?
                """,
                (name, parentCategoryId, int(displayOrder), 1 if active else 0, 1 if offerReimbursement else 0, reportingRole, categoryId),
            )
    except ValidationError:
        raise
    except (sqlite3.IntegrityError, ValueError) as error:
        raise ValidationError("Category name must be unique and display order must be an integer") from error


def deleteCategory(connection, categoryId):
    category = connection.execute("SELECT * FROM categories WHERE id = ?", (categoryId,)).fetchone()
    if not category:
        raise ValidationError("Category does not exist")
    if connection.execute("SELECT 1 FROM applicationSettings WHERE key = 'reimbursementCategoryId' AND value = ?", (str(categoryId),)).fetchone():
        raise ValidationError("Choose another Reimbursement Category before deleting this category")
    if connection.execute("SELECT 1 FROM transactions WHERE categoryId = ? LIMIT 1", (categoryId,)).fetchone():
        raise ValidationError("Category cannot be deleted because transactions reference it")
    if connection.execute("SELECT 1 FROM categories WHERE parentCategoryId = ? LIMIT 1", (categoryId,)).fetchone():
        raise ValidationError("Category cannot be deleted because it has child categories")
    if connection.execute("SELECT 1 FROM flexibleCategoryBudgets WHERE categoryId = ? LIMIT 1", (categoryId,)).fetchone():
        raise ValidationError("Delete the flexible category budget before deleting this category")
    with connection:
        connection.execute("UPDATE transactionTemplates SET categoryId = NULL WHERE categoryId = ?", (categoryId,))
        connection.execute("DELETE FROM categories WHERE id = ?", (categoryId,))


def reportingPeriod(period, startDate=None, endDate=None, today=None):
    today = today or date.today()
    if period == "thisMonth":
        periodStart = today.replace(day=1)
        periodEnd = today.replace(day=monthrange(today.year, today.month)[1])
    elif period == "lastMonth":
        periodEnd = today.replace(day=1) - timedelta(days=1)
        periodStart = periodEnd.replace(day=1)
    elif period == "yearToDate":
        periodStart = today.replace(month=1, day=1)
        periodEnd = today
    elif period == "last30Days":
        periodStart = today - timedelta(days=29)
        periodEnd = today
    elif period.startswith("month:"):
        try:
            monthDate = datetime.strptime(period.removeprefix("month:"), "%Y-%m").date()
        except ValueError as error:
            raise ValidationError("Calendar month is invalid") from error
        periodStart = monthDate.replace(day=1)
        periodEnd = monthDate.replace(day=monthrange(monthDate.year, monthDate.month)[1])
    elif period == "custom":
        validateDate(startDate)
        validateDate(endDate)
        periodStart = datetime.strptime(startDate, "%Y-%m-%d").date()
        periodEnd = datetime.strptime(endDate, "%Y-%m-%d").date()
        if periodStart > periodEnd:
            raise ValidationError("Start date must not be after end date")
    else:
        raise ValidationError("Reporting period is invalid")
    dayCount = (periodEnd - periodStart).days + 1
    if period == "thisMonth":
        dayCount = today.day
    return periodStart.isoformat(), periodEnd.isoformat(), dayCount


def roundedPerDayCents(amountCents, dayCount):
    return int(
        (Decimal(amountCents) / Decimal(dayCount)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def percentageHundredths(numerator, denominator):
    if denominator <= 0:
        return None
    return int(
        (Decimal(numerator) * Decimal(10000) / Decimal(denominator)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def dashboardReport(connection, period="thisMonth", startDate=None, endDate=None, today=None):
    periodStart, periodEnd, dayCount = reportingPeriod(period, startDate, endDate, today)
    invalidRollups = connection.execute(
        """SELECT c.name FROM categories c
           WHERE EXISTS (SELECT 1 FROM categories child WHERE child.parentCategoryId = c.id)
             AND EXISTS (SELECT 1 FROM transactions t WHERE t.categoryId = c.id)
           ORDER BY c.displayOrder, c.name, c.id"""
    ).fetchall()
    categoryReportingError = None
    if invalidRollups:
        categoryNames = ", ".join(f'"{row["name"]}"' for row in invalidRollups)
        categoryReportingError = (
            f"Category {categoryNames} has transactions assigned directly to a rollup. "
            "Edit those transactions to select valid categories before viewing this category breakdown."
        )
    categoryRows = connection.execute(
        """
        SELECT c.id, c.name, c.parentCategoryId, c.displayOrder, c.reportingRole,
               COALESCE(SUM(t.amountCents), 0) AS storedAmountCents
        FROM categories AS c
        LEFT JOIN transactions AS t
          ON t.categoryId = c.id
         AND t.transferId IS NULL
         AND t.date BETWEEN ? AND ?
        GROUP BY c.id
        ORDER BY c.displayOrder, c.name
        """,
        (periodStart, periodEnd),
    ).fetchall()
    incomeCents = sum(
        row["storedAmountCents"] for row in categoryRows if row["reportingRole"] == "income"
    )
    expenseRows = [row for row in categoryRows if row["reportingRole"] == "expense"]
    expensesCents = -sum(row["storedAmountCents"] for row in expenseRows)
    categoryResults = []
    for row in expenseRows:
        amountCents = -row["storedAmountCents"]
        categoryResults.append({
            "id": row["id"],
            "name": row["name"],
            "parentCategoryId": row["parentCategoryId"],
            "displayOrder": row["displayOrder"],
            "reportingRole": row["reportingRole"],
            "amountCents": amountCents,
            "percentageHundredths": percentageHundredths(amountCents, expensesCents),
            "perDayCents": roundedPerDayCents(amountCents, dayCount),
        })
    for rollup in [row for row in categoryResults if row["parentCategoryId"] is None]:
        childAmountCents = sum(
            row["amountCents"] for row in categoryResults
            if row["parentCategoryId"] == rollup["id"]
        )
        if any(row["parentCategoryId"] == rollup["id"] for row in categoryResults):
            rollup["amountCents"] = childAmountCents
            rollup["percentageHundredths"] = percentageHundredths(childAmountCents, expensesCents)
            rollup["perDayCents"] = roundedPerDayCents(childAmountCents, dayCount)
    accountRows = connection.execute(
        """
        SELECT a.id, a.name, a.accountClass, a.active, a.displayOrder,
               COALESCE(SUM(CASE WHEN c.reportingRole = 'expense' THEN t.amountCents ELSE 0 END), 0)
                   AS expenseStoredAmountCents,
               COALESCE(SUM(CASE WHEN c.reportingRole = 'income' THEN t.amountCents ELSE 0 END), 0)
                   AS incomeStoredAmountCents,
               COUNT(CASE WHEN c.reportingRole = 'expense' THEN 1 END) AS expenseCount,
               COUNT(CASE WHEN c.reportingRole = 'income' THEN 1 END) AS incomeCount
        FROM accounts AS a
        LEFT JOIN transactions AS t
          ON t.accountId = a.id
         AND t.transferId IS NULL
         AND t.categoryId IS NOT NULL
         AND t.date BETWEEN ? AND ?
        LEFT JOIN categories AS c ON c.id = t.categoryId
        WHERE a.accountClass IN ('cash', 'credit')
        GROUP BY a.id
        HAVING a.active = 1 OR expenseCount > 0 OR incomeCount > 0
        ORDER BY CASE a.accountClass WHEN 'cash' THEN 0 ELSE 1 END,
                 LOWER(a.name), a.id
        """,
        (periodStart, periodEnd),
    ).fetchall()
    accountResults = []
    incomeAccountResults = []
    netChangeAccountResults = []
    for row in accountRows:
        expenseAmountCents = -row["expenseStoredAmountCents"]
        incomeAmountCents = row["incomeStoredAmountCents"]
        netChangeCents = row["expenseStoredAmountCents"] + row["incomeStoredAmountCents"]
        accountDetails = {
            "id": row["id"], "name": row["name"], "active": row["active"],
            "displayOrder": row["displayOrder"], "accountClass": row["accountClass"],
        }
        if row["active"] or row["expenseCount"]:
            accountResults.append({
                **accountDetails, "amountCents": expenseAmountCents,
                "percentageHundredths": percentageHundredths(expenseAmountCents, expensesCents),
            })
        if row["active"] or row["incomeCount"]:
            incomeAccountResults.append({
                **accountDetails, "amountCents": incomeAmountCents,
                "percentageHundredths": percentageHundredths(incomeAmountCents, incomeCents),
            })
        netChangeAccountResults.append({
            **accountDetails, "amountCents": netChangeCents, "isZero": netChangeCents == 0,
        })
    netChangeGroups = []
    for accountClass, label in (("cash", "Cash Accounts"), ("credit", "Credit Cards and Loans")):
        groupAccounts = [
            account for account in netChangeAccountResults
            if account["accountClass"] == accountClass
        ]
        if groupAccounts:
            netChangeGroups.append({
                "accountClass": accountClass,
                "label": label,
                "amountCents": sum(account["amountCents"] for account in groupAccounts),
                "accounts": groupAccounts,
            })
    balanceChangeRows = connection.execute(
        """
        SELECT a.id, a.name, a.accountClass, a.active, a.displayOrder,
               SUM(t.amountCents) AS ledgerChangeCents
        FROM accounts AS a
        JOIN transactions AS t ON t.accountId = a.id
        WHERE t.date BETWEEN ? AND ? AND a.accountClass IN ('cash', 'credit')
        GROUP BY a.id
        ORDER BY CASE a.accountClass WHEN 'cash' THEN 0 ELSE 1 END,
                 LOWER(a.name), a.id
        """,
        (periodStart, periodEnd),
    ).fetchall()
    balanceChangeAccountResults = []
    for row in balanceChangeRows:
        displayChangeCents = (
            -row["ledgerChangeCents"] if row["accountClass"] == "credit"
            else row["ledgerChangeCents"]
        )
        balanceChangeAccountResults.append({
            "id": row["id"], "name": row["name"], "active": row["active"],
            "displayOrder": row["displayOrder"], "accountClass": row["accountClass"],
            "amountCents": displayChangeCents,
            "isZero": displayChangeCents == 0,
        })
    balanceChangeGroups = []
    for accountClass, label in (("cash", "Cash Accounts"), ("credit", "Credit Cards and Loans")):
        groupAccounts = [
            account for account in balanceChangeAccountResults
            if account["accountClass"] == accountClass
        ]
        if groupAccounts:
            balanceChangeGroups.append({
                "accountClass": accountClass,
                "label": label,
                "amountCents": sum(account["amountCents"] for account in groupAccounts),
                "accounts": groupAccounts,
            })
    expenseAccountCategoryRows = connection.execute(
        """
        SELECT t.accountId, t.categoryId, -SUM(t.amountCents) AS amountCents
        FROM transactions AS t
        JOIN categories AS c ON c.id = t.categoryId
        WHERE t.transferId IS NULL
          AND c.reportingRole = 'expense'
          AND t.date BETWEEN ? AND ?
        GROUP BY t.accountId, t.categoryId
        """,
        (periodStart, periodEnd),
    ).fetchall()
    categoryById = {row["id"]: row for row in categoryResults}
    categoryOrder = []
    for category in [row for row in categoryResults if row["parentCategoryId"] is None]:
        categoryOrder.append(category["id"])
        categoryOrder.extend(
            row["id"] for row in categoryResults if row["parentCategoryId"] == category["id"]
        )
    categoryOrderIndex = {categoryId: index for index, categoryId in enumerate(categoryOrder)}
    accountById = {row["id"]: row for row in accountResults}
    accountOrderIndex = {row["id"]: index for index, row in enumerate(accountResults)}
    pairsByAccount = {}
    pairsByCategory = {}
    for row in expenseAccountCategoryRows:
        category = categoryById[row["categoryId"]]
        parent = categoryById.get(category["parentCategoryId"])
        account = accountById[row["accountId"]]
        pairsByAccount.setdefault(row["accountId"], []).append({
            "id": category["id"],
            "name": category["name"],
            "displayName": f"{parent['name']} › {category['name']}" if parent else category["name"],
            "amountCents": row["amountCents"],
            "isZero": row["amountCents"] == 0,
        })
        pairsByCategory.setdefault(row["categoryId"], []).append({
            "id": account["id"],
            "name": account["name"],
            "active": account["active"],
            "amountCents": row["amountCents"],
            "isZero": row["amountCents"] == 0,
        })
    expensesByAccountAndCategory = []
    for account in accountResults:
        representedCategories = pairsByAccount.get(account["id"], [])
        if not representedCategories:
            continue
        representedCategories.sort(key=lambda row: categoryOrderIndex[row["id"]])
        expensesByAccountAndCategory.append({
            "id": account["id"],
            "name": account["name"],
            "active": account["active"],
            "amountCents": sum(row["amountCents"] for row in representedCategories),
            "categories": representedCategories,
        })
    expensesByCategoryAndAccount = []
    for categoryId in categoryOrder:
        representedAccounts = pairsByCategory.get(categoryId, [])
        if not representedAccounts:
            continue
        representedAccounts.sort(key=lambda row: accountOrderIndex[row["id"]])
        category = categoryById[categoryId]
        parent = categoryById.get(category["parentCategoryId"])
        expensesByCategoryAndAccount.append({
            "id": category["id"],
            "name": category["name"],
            "displayName": f"{parent['name']} › {category['name']}" if parent else category["name"],
            "amountCents": sum(row["amountCents"] for row in representedAccounts),
            "accounts": representedAccounts,
        })
    return {
        "periodStart": periodStart,
        "periodEnd": periodEnd,
        "dayCount": dayCount,
        "expensesCents": expensesCents,
        "incomeCents": incomeCents,
        "netCents": incomeCents - expensesCents,
        "spendingPercentageHundredths": percentageHundredths(expensesCents, incomeCents),
        "perDayCents": roundedPerDayCents(expensesCents, dayCount),
        "categories": [] if categoryReportingError else categoryResults,
        "categoryReportingError": categoryReportingError,
        "accountsByExpense": accountResults,
        "expensesByAccountAndCategory": expensesByAccountAndCategory,
        "expensesByCategoryAndAccount": expensesByCategoryAndAccount,
        "accountsByIncome": incomeAccountResults,
        "accountsByNetChange": netChangeAccountResults,
        "netChangeGroups": netChangeGroups,
        "accountsByBalanceChange": balanceChangeAccountResults,
        "balanceChangeGroups": balanceChangeGroups,
    }


def optionalTemplateValues(connection, accountId, description, categoryId, enteredAmount):
    account = getAccount(connection, accountId, activeRequired=True) if accountId not in (None, "") else None
    if account and account["accountClass"] == "system":
        raise ValidationError("System Accounts cannot be used by transaction templates")
    description = requireText(description, "Description") if str(description or "").strip() else None
    category = getSelectableCategory(connection, categoryId) if categoryId not in (None, "") else None
    amountCents = parseMoneyCents(enteredAmount) if enteredAmount not in (None, "") else None
    if amountCents == 0:
        raise ValidationError("Template amount must be nonzero")
    return account, description, category, amountCents


def createTemplate(connection, name, accountId, description, categoryId, enteredAmount, displayOrder=0, prefillAmount=True):
    name = requireText(name, "Template name")
    account, description, category, amountCents = optionalTemplateValues(
        connection, accountId, description, categoryId, enteredAmount
    )
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if category is not None:
                getSelectableCategory(connection, category["id"])
            cursor = connection.execute(
                """
                INSERT INTO transactionTemplates
                    (name, accountId, description, categoryId, enteredAmountCents, displayOrder, prefillAmount)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (name, account["id"] if account else None, description,
                 category["id"] if category else None, amountCents, int(displayOrder), 1 if prefillAmount else 0),
            )
        return cursor.lastrowid
    except ValidationError:
        raise
    except (sqlite3.IntegrityError, ValueError) as error:
        raise ValidationError("Template name must be unique and display order must be an integer") from error


def updateTemplate(connection, templateId, name, accountId, description, categoryId, enteredAmount, displayOrder=0, active=True, prefillAmount=True):
    existing = connection.execute(
        "SELECT * FROM transactionTemplates WHERE id = ?", (templateId,)
    ).fetchone()
    if not existing:
        raise ValidationError("Template does not exist")
    name = requireText(name, "Template name")
    account, description, category, amountCents = optionalTemplateValues(
        connection, accountId, description, categoryId, enteredAmount
    )
    if connection.execute(
        "SELECT 1 FROM sinkingFundPlans WHERE templateId = ? AND active = 1", (templateId,)
    ).fetchone():
        remainsComplete = (
            active and description is not None and category is not None
            and category["reportingRole"] == "expense" and amountCents is not None and amountCents > 0
        )
        if not remainsComplete:
            raise ValidationError("Archive or remove the active Sinking Fund plan before making this template incomplete")
    if connection.execute(
        "SELECT 1 FROM expectedIncomePlans WHERE templateId = ? AND active = 1", (templateId,)
    ).fetchone():
        remainsComplete = (
            active and description is not None and category is not None
            and category["reportingRole"] == "income" and amountCents is not None and amountCents > 0
        )
        if not remainsComplete:
            raise ValidationError("Archive or remove the active expected income plan before making this template incomplete")
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            if category is not None:
                getSelectableCategory(connection, category["id"])
            connection.execute(
                """
                UPDATE transactionTemplates
                SET name = ?, accountId = ?, description = ?, categoryId = ?,
                    enteredAmountCents = ?, displayOrder = ?, active = ?, prefillAmount = ?
                WHERE id = ?
                """,
                (name, account["id"] if account else None, description,
                 category["id"] if category else None, amountCents,
                 int(displayOrder), 1 if active else 0, 1 if prefillAmount else 0, templateId),
            )
    except ValidationError:
        raise
    except (sqlite3.IntegrityError, ValueError) as error:
        raise ValidationError("Template name must be unique and display order must be an integer") from error


def deleteTemplate(connection, templateId):
    existing = connection.execute(
        "SELECT name FROM transactionTemplates WHERE id = ?", (templateId,)
    ).fetchone()
    if not existing:
        raise ValidationError("Template does not exist")
    if connection.execute(
        "SELECT 1 FROM sinkingFundPlans WHERE templateId = ? AND active = 1", (templateId,)
    ).fetchone():
        raise ValidationError("Archive or remove the active Sinking Fund plan before deleting this template")
    if connection.execute(
        "SELECT 1 FROM expectedIncomePlans WHERE templateId = ? AND active = 1", (templateId,)
    ).fetchone():
        raise ValidationError("Archive or remove the active expected income plan before deleting this template")
    with connection:
        connection.execute("DELETE FROM transactionTemplates WHERE id = ?", (templateId,))


def validateCompleteExpenseTemplate(connection, templateId):
    template = connection.execute(
        """SELECT tt.*, a.active AS accountActive, c.active AS categoryActive,
                  c.reportingRole,
                  EXISTS(SELECT 1 FROM categories child WHERE child.parentCategoryId = c.id) AS hasChildren
           FROM transactionTemplates tt
           LEFT JOIN accounts a ON a.id = tt.accountId
           LEFT JOIN categories c ON c.id = tt.categoryId
           WHERE tt.id = ?""",
        (templateId,),
    ).fetchone()
    if not template:
        raise ValidationError("Template does not exist")
    complete = (
        template["active"] and (template["accountId"] is None or template["accountActive"])
        and template["description"] and template["categoryId"] is not None
        and template["categoryActive"] and template["reportingRole"] == "expense"
        and not template["hasChildren"] and template["enteredAmountCents"] is not None
        and template["enteredAmountCents"] > 0
    )
    if not complete:
        raise ValidationError("Sinking Fund plans require a complete active Expense template with a positive amount")
    return template


def setPaychecksPerYear(connection, rawValue):
    try:
        paychecksPerYear = int(rawValue)
    except (TypeError, ValueError) as error:
        raise ValidationError("Paychecks Per Year must be a positive whole number") from error
    if isinstance(rawValue, bool) or str(rawValue).strip() != str(paychecksPerYear) or paychecksPerYear <= 0:
        raise ValidationError("Paychecks Per Year must be a positive whole number")
    with connection:
        connection.execute(
            "INSERT INTO applicationSettings (key, value) VALUES ('paychecksPerYear', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(paychecksPerYear),),
        )
    return paychecksPerYear


def setSuggestionRounding(connection, rawValue):
    if rawValue in (None, ""):
        with connection:
            connection.execute("DELETE FROM applicationSettings WHERE key = 'suggestionRoundingCents'")
        return None
    roundingCents = parseMoneyCents(rawValue, positiveOnly=True)
    with connection:
        connection.execute(
            "INSERT INTO applicationSettings (key, value) VALUES ('suggestionRoundingCents', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(roundingCents),),
        )
    return roundingCents


def setBudgetSuggestionWindows(connection, monthlyDays, quarterlyMonths, annualMonths):
    rawValues = {
        "monthlySuggestionWindowDays": monthlyDays,
        "quarterlySuggestionWindowMonths": quarterlyMonths,
        "annualSuggestionWindowMonths": annualMonths,
    }
    parsedValues = {}
    for key, rawValue in rawValues.items():
        try:
            parsedValue = int(rawValue)
        except (TypeError, ValueError) as error:
            raise ValidationError("Suggestion windows must be nonnegative whole numbers") from error
        if isinstance(rawValue, bool) or str(rawValue).strip() != str(parsedValue) or parsedValue < 0:
            raise ValidationError("Suggestion windows must be nonnegative whole numbers")
        parsedValues[key] = parsedValue
    with connection:
        connection.executemany(
            "INSERT INTO applicationSettings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(key, str(value)) for key, value in parsedValues.items()],
        )
    return parsedValues


def applicationSettingInteger(connection, key, defaultValue):
    row = connection.execute("SELECT value FROM applicationSettings WHERE key = ?", (key,)).fetchone()
    return int(row["value"]) if row else defaultValue


def validateFlexibleCategory(connection, categoryId):
    category = connection.execute(
        """SELECT c.*, EXISTS(SELECT 1 FROM categories child WHERE child.parentCategoryId = c.id) AS hasChildren
           FROM categories c WHERE c.id = ?""", (categoryId,)
    ).fetchone()
    if not category or not category["active"] or category["reportingRole"] != "expense" or category["hasChildren"]:
        raise ValidationError("Flexible budgets require an active selectable Expense category")
    return category


def createFlexibleCategoryBudget(connection, categoryId, monthlyTarget):
    category = validateFlexibleCategory(connection, categoryId)
    targetCents = parseMoneyCents(monthlyTarget, positiveOnly=True)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            validateFlexibleCategory(connection, categoryId)
            cursor = connection.execute(
                "INSERT INTO flexibleCategoryBudgets (categoryId, monthlyTargetCents) VALUES (?, ?)",
                (category["id"], targetCents),
            )
        return cursor.lastrowid
    except sqlite3.IntegrityError as error:
        raise ValidationError("A category may have only one active flexible budget") from error


def updateFlexibleCategoryBudget(connection, budgetId, categoryId, monthlyTarget, active=True):
    if not connection.execute("SELECT 1 FROM flexibleCategoryBudgets WHERE id = ?", (budgetId,)).fetchone():
        raise ValidationError("Flexible category budget does not exist")
    if active:
        validateFlexibleCategory(connection, categoryId)
    targetCents = parseMoneyCents(monthlyTarget, positiveOnly=True)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            getSelectableCategory(connection, categoryId, allowArchived=not active)
            if active:
                validateFlexibleCategory(connection, categoryId)
            connection.execute(
                "UPDATE flexibleCategoryBudgets SET categoryId = ?, monthlyTargetCents = ?, active = ? WHERE id = ?",
                (categoryId, targetCents, 1 if active else 0, budgetId),
            )
    except sqlite3.IntegrityError as error:
        raise ValidationError("A category may have only one active flexible budget") from error


def deleteFlexibleCategoryBudget(connection, budgetId):
    with connection:
        cursor = connection.execute("DELETE FROM flexibleCategoryBudgets WHERE id = ?", (budgetId,))
    if cursor.rowcount != 1:
        raise ValidationError("Flexible category budget does not exist")


def validateCompleteIncomeTemplate(connection, templateId):
    template = connection.execute(
        """SELECT tt.*, a.active AS accountActive, c.active AS categoryActive, c.reportingRole,
                  EXISTS(SELECT 1 FROM categories child WHERE child.parentCategoryId = c.id) AS hasChildren
           FROM transactionTemplates tt
           LEFT JOIN accounts a ON a.id = tt.accountId
           LEFT JOIN categories c ON c.id = tt.categoryId
           WHERE tt.id = ?""", (templateId,)
    ).fetchone()
    complete = template and template["active"] and (template["accountId"] is None or template["accountActive"])
    complete = complete and template["description"] and template["categoryId"] is not None
    complete = complete and template["categoryActive"] and template["reportingRole"] == "income" and not template["hasChildren"]
    complete = complete and template["enteredAmountCents"] is not None and template["enteredAmountCents"] > 0
    if not complete:
        raise ValidationError("Expected income plans require a complete active Income template with a positive amount")
    return template


def createExpectedIncomePlan(connection, templateId, expectedDate):
    validateCompleteIncomeTemplate(connection, templateId)
    validateDate(expectedDate)
    with connection:
        cursor = connection.execute(
            "INSERT INTO expectedIncomePlans (templateId, expectedDate) VALUES (?, ?)",
            (templateId, expectedDate),
        )
    return cursor.lastrowid


def updateExpectedIncomePlan(connection, planId, templateId, expectedDate, active=True):
    if not connection.execute("SELECT 1 FROM expectedIncomePlans WHERE id = ?", (planId,)).fetchone():
        raise ValidationError("Expected income plan does not exist")
    if active:
        validateCompleteIncomeTemplate(connection, templateId)
    validateDate(expectedDate)
    with connection:
        connection.execute(
            "UPDATE expectedIncomePlans SET templateId = ?, expectedDate = ?, active = ? WHERE id = ?",
            (templateId, expectedDate, 1 if active else 0, planId),
        )


def deleteExpectedIncomePlan(connection, planId):
    with connection:
        cursor = connection.execute("DELETE FROM expectedIncomePlans WHERE id = ?", (planId,))
    if cursor.rowcount != 1:
        raise ValidationError("Expected income plan does not exist")


def validateCreditPaymentAccount(connection, accountId):
    account = getAccount(connection, accountId, activeRequired=True)
    if account["accountClass"] != "credit" or account["accountSubtype"] not in {"Credit Card", "Personal Loan"}:
        raise ValidationError("Planned payments require an active Credit Card or Personal Loan")
    return account


def createPlannedCreditPayment(connection, expectedDate, accountId, description, amount):
    validateDate(expectedDate)
    account = validateCreditPaymentAccount(connection, accountId)
    description = requireText(description, "Description")
    amountCents = parseMoneyCents(amount, positiveOnly=True)
    with connection:
        cursor = connection.execute(
            "INSERT INTO plannedCreditPayments (expectedDate, accountId, description, amountCents) VALUES (?, ?, ?, ?)",
            (expectedDate, account["id"], description, amountCents),
        )
    return cursor.lastrowid


def updatePlannedCreditPayment(connection, planId, expectedDate, accountId, description, amount, active=True):
    if not connection.execute("SELECT 1 FROM plannedCreditPayments WHERE id = ?", (planId,)).fetchone():
        raise ValidationError("Planned credit payment does not exist")
    validateDate(expectedDate)
    if active:
        validateCreditPaymentAccount(connection, accountId)
    description = requireText(description, "Description")
    amountCents = parseMoneyCents(amount, positiveOnly=True)
    with connection:
        connection.execute(
            "UPDATE plannedCreditPayments SET expectedDate = ?, accountId = ?, description = ?, amountCents = ?, active = ? WHERE id = ?",
            (expectedDate, accountId, description, amountCents, 1 if active else 0, planId),
        )


def deletePlannedCreditPayment(connection, planId):
    with connection:
        cursor = connection.execute("DELETE FROM plannedCreditPayments WHERE id = ?", (planId,))
    if cursor.rowcount != 1:
        raise ValidationError("Planned credit payment does not exist")


def createSimulatorPlan(connection, name, planDate, duplicatePlanId=None):
    name = requireText(name, "Plan Name")
    validateDate(planDate)
    with connection:
        cursor = connection.execute(
            "INSERT INTO simulatorPlans (name, planDate) VALUES (?, ?)", (name, planDate)
        )
        planId = cursor.lastrowid
        if duplicatePlanId is not None:
            if not connection.execute("SELECT 1 FROM simulatorPlans WHERE id = ?", (duplicatePlanId,)).fetchone():
                raise ValidationError("Source plan does not exist")
            connection.execute(
                """INSERT INTO simulatorRows
                   (planId, rowType, rowOrder, fromAccountId, toAccountId, categoryId, label, amountCents, amountMode, executed, executedTransactionId)
                   SELECT ?, rowType, rowOrder, fromAccountId, toAccountId, categoryId, label, amountCents, amountMode, 0, NULL
                   FROM simulatorRows WHERE planId = ? ORDER BY rowOrder, id""",
                (planId, duplicatePlanId),
            )
    return planId


def updateSimulatorPlan(connection, planId, name, planDate):
    name = requireText(name, "Plan Name")
    validateDate(planDate)
    with connection:
        cursor = connection.execute(
            "UPDATE simulatorPlans SET name = ?, planDate = ? WHERE id = ?",
            (name, planDate, planId),
        )
    if cursor.rowcount != 1:
        raise ValidationError("Plan does not exist")


def deleteSimulatorPlan(connection, planId):
    with connection:
        cursor = connection.execute("DELETE FROM simulatorPlans WHERE id = ?", (planId,))
    if cursor.rowcount != 1:
        raise ValidationError("Plan does not exist")


def saveSimulatorRow(connection, planId, rowId, rowType, fromAccountId=None, toAccountId=None,
                     categoryId=None, label=None, amount=None):
    try:
        fromAccountId = int(fromAccountId) if fromAccountId not in (None, "") else None
        toAccountId = int(toAccountId) if toAccountId not in (None, "") else None
        categoryId = int(categoryId) if categoryId not in (None, "") else None
    except (TypeError, ValueError) as error:
        raise ValidationError("Selected account or category does not exist") from error
    if rowType not in {"live", "income", "spend", "transfer", "payoff", "buffer"}:
        raise ValidationError("Type is required")
    payoffMode = rowType == "payoff"
    storedRowType = "transfer" if payoffMode else rowType
    if not connection.execute("SELECT 1 FROM simulatorPlans WHERE id = ?", (planId,)).fetchone():
        raise ValidationError("Plan does not exist")
    existing = None
    if rowId is not None:
        existing = connection.execute(
            "SELECT * FROM simulatorRows WHERE id = ? AND planId = ?", (rowId, planId)
        ).fetchone()
        if not existing:
            raise ValidationError("Worksheet row does not exist")
        if existing["executed"]:
            raise ValidationError("Executed rows are read-only")
    fromId = toId = category = rowLabel = amountCents = None
    if storedRowType == "live":
        account = getAccount(connection, fromAccountId, activeRequired=existing is None or fromAccountId != existing["fromAccountId"])
        if account["accountClass"] != "cash":
            raise ValidationError("Live balance requires a Cash account")
        duplicate = connection.execute(
            "SELECT 1 FROM simulatorRows WHERE planId = ? AND rowType = 'live' AND fromAccountId = ? AND id != ?",
            (planId, account["id"], rowId or -1),
        ).fetchone()
        if duplicate:
            raise ValidationError("That Cash account already has a Live balance row")
        fromId = account["id"]
    else:
        rowLabel = requireText(label, "Label")
        amountCents = None if payoffMode else parseMoneyCents(amount, positiveOnly=True)
        if storedRowType == "income":
            account = getAccount(connection, toAccountId, activeRequired=existing is None or toAccountId != existing["toAccountId"])
            if account["accountClass"] == "system":
                raise ValidationError("External is available only on Simulator Transfer rows")
            toId = account["id"]
        elif storedRowType == "buffer":
            account = getAccount(connection, fromAccountId, activeRequired=True)
            if account["accountClass"] != "cash":
                raise ValidationError("Buffer requires an active Cash account")
            fromId = account["id"]
        elif storedRowType == "spend":
            account = getAccount(connection, fromAccountId, activeRequired=existing is None or fromAccountId != existing["fromAccountId"])
            if account["accountClass"] == "system":
                raise ValidationError("External is available only on Simulator Transfer rows")
            fromId = account["id"]
        else:
            fromAccount = getAccount(connection, fromAccountId, activeRequired=existing is None or fromAccountId != existing["fromAccountId"])
            toAccount = getAccount(connection, toAccountId, activeRequired=existing is None or toAccountId != existing["toAccountId"])
            fromId, toId = fromAccount["id"], toAccount["id"]
            if fromId == toId:
                raise ValidationError("From and To accounts must differ")
            if payoffMode:
                if fromAccount["accountClass"] != "cash" or toAccount["accountClass"] != "credit":
                    raise ValidationError("Payoff requires a Cash source and Credit destination")
                duplicatePayoff = connection.execute(
                    "SELECT 1 FROM simulatorRows WHERE planId = ? AND amountMode = 'payoff' AND toAccountId = ? AND id != ?",
                    (planId, toId, rowId or -1),
                ).fetchone()
                if duplicatePayoff:
                    raise ValidationError("A Credit account may have only one Payoff row per plan")
        if storedRowType in {"income", "spend"} and categoryId not in (None, ""):
            selectedCategory = getSelectableCategory(
                connection, categoryId,
                allowArchived=existing is not None and categoryId == existing["categoryId"],
            )
            requiredRole = "income" if storedRowType == "income" else "expense"
            if selectedCategory["reportingRole"] != requiredRole:
                raise ValidationError(f"{storedRowType.title()} requires a compatible category")
            category = selectedCategory["id"]
    with connection:
        if existing:
            connection.execute(
                """UPDATE simulatorRows SET rowType=?, fromAccountId=?, toAccountId=?, categoryId=?, label=?, amountCents=?, amountMode=?
                   WHERE id=? AND planId=?""",
                (storedRowType, fromId, toId, category, rowLabel, amountCents, "payoff" if payoffMode else "fixed", rowId, planId),
            )
            return rowId
        nextOrder = connection.execute(
            "SELECT COALESCE(MAX(rowOrder), 0) + 1 FROM simulatorRows WHERE planId = ?", (planId,)
        ).fetchone()[0]
        cursor = connection.execute(
            """INSERT INTO simulatorRows
               (planId,rowType,rowOrder,fromAccountId,toAccountId,categoryId,label,amountCents,amountMode)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (planId, storedRowType, nextOrder, fromId, toId, category, rowLabel, amountCents, "payoff" if payoffMode else "fixed"),
        )
    return cursor.lastrowid


def deleteSimulatorRow(connection, planId, rowId):
    with connection:
        cursor = connection.execute(
            "DELETE FROM simulatorRows WHERE id = ? AND planId = ?", (rowId, planId)
        )
    if cursor.rowcount != 1:
        raise ValidationError("Worksheet row does not exist")


def deleteExecutedSimulatorRows(connection, planId, expectedCount):
    try:
        expectedCount = int(expectedCount)
    except (TypeError, ValueError) as error:
        raise ValidationError("Executed-row count is required") from error
    if expectedCount < 1:
        raise ValidationError("Executed-row count is invalid")
    with connection:
        plan = connection.execute(
            "SELECT name FROM simulatorPlans WHERE id = ?", (planId,)
        ).fetchone()
        if not plan:
            raise ValidationError("Plan does not exist")
        executedCount = connection.execute(
            """SELECT COUNT(*) FROM simulatorRows
               WHERE planId = ? AND executed = 1
                 AND rowType NOT IN ('live', 'buffer')""",
            (planId,),
        ).fetchone()[0]
        if executedCount == 0:
            raise ValidationError("Plan has no executed financial rows")
        if executedCount != expectedCount:
            raise ValidationError("Executed rows changed; review the plan and try again")
        cursor = connection.execute(
            """DELETE FROM simulatorRows
               WHERE planId = ? AND executed = 1
                 AND rowType NOT IN ('live', 'buffer')""",
            (planId,),
        )
        if cursor.rowcount != executedCount:
            raise sqlite3.IntegrityError("Executed Simulator row set changed during deletion")
    return plan["name"], executedCount


def setSimulatorRowExecuted(connection, planId, rowId, executed):
    row = connection.execute(
        "SELECT rowType, executedTransactionId FROM simulatorRows WHERE id = ? AND planId = ?",
        (rowId, planId),
    ).fetchone()
    if not row:
        raise ValidationError("Worksheet row does not exist")
    if row["rowType"] in {"live", "buffer"}:
        raise ValidationError("Planning-only rows do not have execution state")
    if not executed and row["executedTransactionId"] is not None:
        raise ValidationError("Delete the linked ledger event before returning this row to Planned")
    with connection:
        connection.execute(
            "UPDATE simulatorRows SET executed = ? WHERE id = ? AND planId = ?",
            (1 if executed else 0, rowId, planId),
        )


def recordSimulatorTransaction(connection, planId, rowId, accountId, transactionDate,
                               description, categoryId, enteredAmount, pinned=False):
    row = connection.execute(
        "SELECT * FROM simulatorRows WHERE id = ? AND planId = ?", (rowId, planId)
    ).fetchone()
    if not row or row["rowType"] not in {"income", "spend"} or row["executed"]:
        raise ValidationError("Planned Income or Spend row does not exist")
    transactionValues = prepareTransaction(
        connection, accountId, transactionDate, description, categoryId, enteredAmount, pinned
    )
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        getSelectableCategory(connection, transactionValues[4])
        cursor = connection.execute(
            "INSERT INTO transactions (accountId,date,description,amountCents,categoryId,pinned) VALUES (?,?,?,?,?,?)",
            transactionValues,
        )
        connection.execute(
            "UPDATE simulatorRows SET executed=1, executedTransactionId=? WHERE id=? AND planId=?",
            (cursor.lastrowid, rowId, planId),
        )
    return cursor.lastrowid


def recordSimulatorTransfer(connection, planId, rowId, transactionDate, fromAccountId,
                            toAccountId, enteredAmount, description):
    row = connection.execute(
        "SELECT * FROM simulatorRows WHERE id = ? AND planId = ?", (rowId, planId)
    ).fetchone()
    if not row or row["rowType"] != "transfer" or row["executed"]:
        raise ValidationError("Planned Transfer, Payment, or Payoff row does not exist")
    validateDate(transactionDate)
    description = requireText(description, "Description")
    amountCents = parseMoneyCents(enteredAmount, positiveOnly=True)
    try:
        selectedFromAccountId = int(fromAccountId)
        selectedToAccountId = int(toAccountId)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValidationError("Transfer Account IDs must be integers") from error
    if selectedFromAccountId == selectedToAccountId:
        raise ValidationError("Transfer accounts must be different")
    fromAccount = getAccount(connection, fromAccountId, activeRequired=True)
    toAccount = getAccount(connection, toAccountId, activeRequired=True)
    transferId = str(uuid4())
    with connection:
        sourceCursor = connection.execute(
            "INSERT INTO transactions (accountId,date,description,amountCents,categoryId,transferId) VALUES (?,?,?,?,NULL,?)",
            (fromAccount["id"], transactionDate, description, -amountCents, transferId),
        )
        connection.execute(
            "INSERT INTO transactions (accountId,date,description,amountCents,categoryId,transferId) VALUES (?,?,?,?,NULL,?)",
            (toAccount["id"], transactionDate, description, amountCents, transferId),
        )
        if row["amountMode"] == "payoff":
            connection.execute(
                """UPDATE simulatorRows SET rowType='transfer', amountMode='fixed',
                   fromAccountId=?, toAccountId=?, label=?, amountCents=?, executed=1,
                   executedTransactionId=? WHERE id=? AND planId=?""",
                (fromAccount["id"], toAccount["id"], description, amountCents,
                 sourceCursor.lastrowid, rowId, planId),
            )
        else:
            connection.execute(
                "UPDATE simulatorRows SET executed=1, executedTransactionId=? WHERE id=? AND planId=?",
                (sourceCursor.lastrowid, rowId, planId),
            )
    return transferId, sourceCursor.lastrowid


def simulatorWorkspace(connection, requestedPlanId=None, repairMissingLinks=True):
    reimbursementSetting = connection.execute(
        """SELECT c.id FROM applicationSettings s JOIN categories c ON c.id=CAST(s.value AS INTEGER)
           WHERE s.key='reimbursementCategoryId' AND c.active=1 AND c.reportingRole='income'
             AND c.parentCategoryId IS NULL
             AND NOT EXISTS (SELECT 1 FROM categories child WHERE child.parentCategoryId=c.id)"""
    ).fetchone()
    reimbursementCategoryId = reimbursementSetting["id"] if reimbursementSetting else None
    plans = [dict(row) for row in connection.execute(
        "SELECT * FROM simulatorPlans ORDER BY planDate, id"
    )]
    if not plans:
        return {"plans": [], "plan": None, "reimbursementCategoryId": reimbursementCategoryId}
    selected = next((plan for plan in plans if plan["id"] == requestedPlanId), plans[0])
    if repairMissingLinks:
        with connection:
            connection.execute(
                """UPDATE simulatorRows SET executed=0, executedTransactionId=NULL
                   WHERE planId=? AND executedTransactionId IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM transactions t WHERE t.id=simulatorRows.executedTransactionId)""",
                (selected["id"],),
            )
    rows = [dict(row) for row in connection.execute(
        """SELECT r.*, fa.name fromAccountName, fa.active fromAccountActive, fa.accountClass fromAccountClass,
                  fa.accountSubtype fromAccountSubtype, ta.name toAccountName, ta.active toAccountActive,
                  ta.accountClass toAccountClass, ta.accountSubtype toAccountSubtype,
                  c.name categoryName, c.active categoryActive, c.reportingRole categoryRole
           FROM simulatorRows r LEFT JOIN accounts fa ON fa.id=r.fromAccountId
           LEFT JOIN accounts ta ON ta.id=r.toAccountId LEFT JOIN categories c ON c.id=r.categoryId
           WHERE r.planId=?
           ORDER BY CASE WHEN amountMode = 'payoff' THEN 5
                         ELSE CASE rowType WHEN 'live' THEN 1 WHEN 'income' THEN 2 WHEN 'spend' THEN 3
                                           WHEN 'transfer' THEN 4 WHEN 'buffer' THEN 6 END
                    END, rowOrder, id""",
        (selected["id"],),
    )]
    if not repairMissingLinks:
        for row in rows:
            if row["executedTransactionId"] is not None and not connection.execute(
                "SELECT 1 FROM transactions WHERE id = ?", (row["executedTransactionId"],)
            ).fetchone():
                row["executedTransactionId"] = None
                row["executed"] = 0
    balanceRows = connection.execute(
        """SELECT a.id, a.name, a.accountClass, a.accountSubtype, a.active, a.creditLimitCents,
                  a.minimumBalanceCents, a.minimumCautionPercent,
                  a.balanceOffsetCents + COALESCE(SUM(t.amountCents),0) balanceCents
           FROM accounts a LEFT JOIN transactions t ON t.accountId=a.id GROUP BY a.id"""
    ).fetchall()
    balances = {row["id"]: dict(row) for row in balanceRows}
    accountOrder = []
    changes = {}
    bufferCentsByAccount = {}
    liveAccountIds = {
        row["fromAccountId"] for row in rows
        if row["rowType"] == "live" and row["fromAccountId"]
        and balances[row["fromAccountId"]]["accountClass"] == "cash"
    }
    liveTotal = incomeTotal = spendingTotal = paymentAndCashTransferTotal = 0
    for row in rows:
        if row["amountMode"] == "payoff":
            row["rowType"] = "payoff"
        if row["executedTransactionId"] is not None:
            linked = connection.execute(
                """SELECT t.*, c.reportingRole categoryRole
                   FROM transactions t LEFT JOIN categories c ON c.id=t.categoryId WHERE t.id=?""",
                (row["executedTransactionId"],),
            ).fetchone()
            if linked:
                row["linkedTransferId"] = linked["transferId"]
                row["linkedDate"] = linked["date"]
                row["label"] = linked["description"]
                if linked["transferId"]:
                    pair = connection.execute(
                        "SELECT * FROM transactions WHERE transferId=? ORDER BY amountCents",
                        (linked["transferId"],),
                    ).fetchall()
                    if len(pair) == 2 and sum(item["amountCents"] for item in pair) == 0:
                        row["fromAccountId"], row["toAccountId"] = pair[0]["accountId"], pair[1]["accountId"]
                        row["amountCents"] = -pair[0]["amountCents"]
                else:
                    row["amountCents"] = linked["amountCents"] if linked["categoryRole"] == "income" else -linked["amountCents"]
                    if row["rowType"] == "income":
                        row["toAccountId"] = linked["accountId"]
                    else:
                        row["fromAccountId"] = linked["accountId"]
                    row["categoryId"] = linked["categoryId"]
        amount = row["amountCents"] or 0
        row["effectiveAmountCents"] = amount
        if row["executed"]:
            continue
        ids = [row["fromAccountId"], row["toAccountId"]]
        for accountId in ids:
            if accountId and accountId not in accountOrder:
                accountOrder.append(accountId)
        if row["rowType"] == "live":
            if balances[row["fromAccountId"]]["accountClass"] == "cash":
                liveTotal += balances[row["fromAccountId"]]["balanceCents"]
        elif row["rowType"] == "income":
            incomeTotal += amount; changes[row["toAccountId"]] = changes.get(row["toAccountId"], 0) + amount
        elif row["rowType"] == "spend":
            spendingTotal += amount; changes[row["fromAccountId"]] = changes.get(row["fromAccountId"], 0) - amount
        elif row["rowType"] == "buffer":
            # A Buffer is an allocation margin, not money projected to leave its Cash account.
            spendingTotal += amount
            bufferCentsByAccount[row["fromAccountId"]] = bufferCentsByAccount.get(row["fromAccountId"], 0) + amount
        elif row["rowType"] == "transfer":
            fromIsLiveCash = row["fromAccountId"] in liveAccountIds
            toIsLiveCash = row["toAccountId"] in liveAccountIds
            if balances[row["fromAccountId"]]["accountClass"] == "cash" and balances[row["toAccountId"]]["accountClass"] == "cash":
                # A cash transfer changes available money only when it crosses
                # the selected Live Balance pool; direction determines its sign.
                if fromIsLiveCash and not toIsLiveCash:
                    paymentAndCashTransferTotal += amount
                elif toIsLiveCash and not fromIsLiveCash:
                    paymentAndCashTransferTotal -= amount
            else:
                paymentAndCashTransferTotal += amount
            changes[row["fromAccountId"]] = changes.get(row["fromAccountId"], 0) - amount
            changes[row["toAccountId"]] = changes.get(row["toAccountId"], 0) + amount
    # A payoff is derived after every fixed planned change, so it reaches zero
    # without making worksheet row order financially meaningful.
    for row in rows:
        if row["rowType"] != "payoff" or row["executed"]:
            continue
        destinationId = row["toAccountId"]
        projectedBeforePayoff = balances[destinationId]["balanceCents"] + changes.get(destinationId, 0)
        payoffAmount = max(0, -projectedBeforePayoff)
        row["effectiveAmountCents"] = payoffAmount
        paymentAndCashTransferTotal += payoffAmount
        changes[row["fromAccountId"]] = changes.get(row["fromAccountId"], 0) - payoffAmount
        changes[destinationId] = changes.get(destinationId, 0) + payoffAmount
    results = []
    groupedAccountOrder = sorted(
        accountOrder,
        key=lambda accountId: (
            0 if balances[accountId]["accountClass"] == "cash" else 1,
            balances[accountId]["name"].lower(),
            accountId,
        ),
    )
    for accountId in groupedAccountOrder:
        account = balances[accountId]; change = changes.get(accountId, 0)
        if account["accountClass"] == "system":
            continue
        projectedBalanceCents = account["balanceCents"] + change
        result = {
            **account,
            "plannedChangeCents": change,
            "displayPlannedChangeCents": -change if account["accountClass"] == "credit" else change,
            "projectedBalanceCents": projectedBalanceCents,
            "bufferCents": bufferCentsByAccount.get(accountId, 0),
        }
        result["minimumStatus"] = minimumStatus(account["minimumBalanceCents"], account["minimumCautionPercent"], projectedBalanceCents)
        if account["accountClass"] == "credit" and account["accountSubtype"] == "Credit Card" and account["creditLimitCents"]:
            owedCents = max(0, -projectedBalanceCents)
            basisPoints = (owedCents * 10000 + account["creditLimitCents"] // 2) // account["creditLimitCents"]
            result["projectedUtilization"] = {
                "basisPoints": basisPoints,
                "label": f"{basisPoints // 100}.{basisPoints % 100:02d}%",
                "accessibleLabel": f"{account['name']} projected utilization: {basisPoints // 100}.{basisPoints % 100:02d}%",
                "state": "green" if basisPoints < 3000 else "yellow" if basisPoints < 5000 else "red",
            }
        else:
            result["projectedUtilization"] = None
        results.append(result)
    selected["rows"] = rows
    selected["results"] = results
    selected["totals"] = {"startingCashCents": liveTotal, "plannedIncomeCents": incomeTotal, "plannedSpendingCents": spendingTotal,
                          "transfersPaymentsCents": paymentAndCashTransferTotal,
                          "unallocatedMoneyCents": liveTotal + incomeTotal - spendingTotal - paymentAndCashTransferTotal}
    return {"plans": plans, "plan": selected, "reimbursementCategoryId": reimbursementCategoryId}


def createSinkingFundPlan(connection, templateId, frequency, expectedDate):
    if frequency not in {"monthly", "quarterly", "yearly"}:
        raise ValidationError("Frequency must be Monthly, Quarterly, or Yearly")
    if frequency != "monthly" and not connection.execute("SELECT 1 FROM applicationSettings WHERE key = 'paychecksPerYear'").fetchone():
        raise ValidationError("Set Paychecks Per Year before creating a Sinking Fund plan")
    validateCompleteExpenseTemplate(connection, templateId)
    validateDate(expectedDate)
    try:
        with connection:
            cursor = connection.execute(
                "INSERT INTO sinkingFundPlans (templateId, frequency, expectedDate) VALUES (?, ?, ?)",
                (templateId, frequency, expectedDate),
            )
        return cursor.lastrowid
    except sqlite3.IntegrityError as error:
        raise ValidationError("A template may have only one active Sinking Fund plan") from error


def simulatorLedgerMapping(connection, transactionId=None, transferId=None):
    """Return an eligible ledger event as existing Simulator row values."""
    if bool(transactionId) == bool(transferId):
        raise ValidationError("Select one ledger event to send")
    if transferId:
        rows = connection.execute(
            """SELECT t.*, a.name AS accountName, a.active AS accountActive
               FROM transactions t JOIN accounts a ON a.id = t.accountId
               WHERE t.transferId = ? ORDER BY t.amountCents""", (transferId,)
        ).fetchall()
        if (len(rows) != 2 or sum(row["amountCents"] for row in rows) != 0
                or rows[0]["amountCents"] >= 0 or rows[1]["amountCents"] <= 0
                or not all(row["accountActive"] for row in rows)):
            raise ValidationError("This transfer cannot be sent to Simulator")
        fromRow, toRow = rows
        return {
            "rowType": "transfer", "rowTypeLabel": "Planned Transfer",
            "fromAccountId": fromRow["accountId"], "fromAccountName": fromRow["accountName"],
            "toAccountId": toRow["accountId"], "toAccountName": toRow["accountName"],
            "categoryId": None, "label": fromRow["description"],
            "amountCents": -fromRow["amountCents"],
        }
    row = connection.execute(
        """SELECT t.*, a.name AS accountName, a.active AS accountActive,
                  c.name AS categoryName, c.active AS categoryActive,
                  c.reportingRole, EXISTS(SELECT 1 FROM categories child WHERE child.parentCategoryId = c.id) AS hasChildren
           FROM transactions t JOIN accounts a ON a.id = t.accountId
           LEFT JOIN categories c ON c.id = t.categoryId WHERE t.id = ?""", (transactionId,)
    ).fetchone()
    if (not row or row["transferId"] or not row["categoryId"] or not row["accountActive"]
            or not row["categoryActive"] or row["hasChildren"]):
        raise ValidationError("This transaction cannot be sent to Simulator")
    amountCents = row["amountCents"] if row["reportingRole"] == "income" else -row["amountCents"]
    if amountCents <= 0:
        raise ValidationError("Refunds and negative corrections cannot be sent to Simulator")
    income = row["reportingRole"] == "income"
    return {
        "rowType": "income" if income else "spend",
        "rowTypeLabel": "Planned Income" if income else "Planned Spend",
        "fromAccountId": None if income else row["accountId"],
        "toAccountId": row["accountId"] if income else None,
        "accountName": row["accountName"], "categoryId": row["categoryId"],
        "categoryName": row["categoryName"], "label": row["description"],
        "amountCents": amountCents,
    }


def sendLedgerEventToSimulator(connection, planId, transactionId=None, transferId=None):
    mapping = simulatorLedgerMapping(connection, transactionId, transferId)
    rowId = saveSimulatorRow(
        connection, planId, None, mapping["rowType"], mapping["fromAccountId"],
        mapping["toAccountId"], mapping["categoryId"], mapping["label"],
        f"{mapping['amountCents'] // 100}.{mapping['amountCents'] % 100:02d}",
    )
    plan = connection.execute("SELECT name FROM simulatorPlans WHERE id = ?", (planId,)).fetchone()
    return rowId, plan["name"]


def updateSinkingFundPlan(connection, planId, templateId, frequency, expectedDate, active=True):
    if not connection.execute("SELECT 1 FROM sinkingFundPlans WHERE id = ?", (planId,)).fetchone():
        raise ValidationError("Sinking Fund plan does not exist")
    if frequency not in {"monthly", "quarterly", "yearly"}:
        raise ValidationError("Frequency must be Monthly, Quarterly, or Yearly")
    if active:
        if frequency != "monthly" and not connection.execute("SELECT 1 FROM applicationSettings WHERE key = 'paychecksPerYear'").fetchone():
            raise ValidationError("Set Paychecks Per Year before activating a Sinking Fund plan")
        validateCompleteExpenseTemplate(connection, templateId)
    validateDate(expectedDate)
    try:
        with connection:
            connection.execute(
                "UPDATE sinkingFundPlans SET templateId = ?, frequency = ?, expectedDate = ?, active = ? WHERE id = ?",
                (templateId, frequency, expectedDate, 1 if active else 0, planId),
            )
    except sqlite3.IntegrityError as error:
        raise ValidationError("A template may have only one active Sinking Fund plan") from error


def deleteSinkingFundPlan(connection, planId):
    with connection:
        cursor = connection.execute("DELETE FROM sinkingFundPlans WHERE id = ?", (planId,))
    if cursor.rowcount != 1:
        raise ValidationError("Sinking Fund plan does not exist")


def addCalendarMonths(calendarDate, monthCount):
    monthIndex = calendarDate.year * 12 + calendarDate.month - 1 + monthCount
    year, zeroBasedMonth = divmod(monthIndex, 12)
    month = zeroBasedMonth + 1
    return date(year, month, min(calendarDate.day, monthrange(year, month)[1]))


def sinkingFundOccurrenceRange(expectedDate, frequency, today):
    intervalMonths = {"monthly": 1, "quarterly": 3, "yearly": 12}[frequency]
    anchor = datetime.strptime(expectedDate, "%Y-%m-%d").date()
    monthDifference = (today.year - anchor.year) * 12 + today.month - anchor.month
    occurrenceIndex = monthDifference // intervalMonths
    occurrence = addCalendarMonths(anchor, occurrenceIndex * intervalMonths)
    while occurrence > today:
        occurrenceIndex -= 1
        occurrence = addCalendarMonths(anchor, occurrenceIndex * intervalMonths)
    nextOccurrence = addCalendarMonths(anchor, (occurrenceIndex + 1) * intervalMonths)
    if occurrence == today:
        return addCalendarMonths(anchor, (occurrenceIndex - 1) * intervalMonths), occurrence
    return occurrence, nextOccurrence


def calendarMonthDifference(earlierDate, laterDate):
    return (laterDate.year - earlierDate.year) * 12 + laterDate.month - earlierDate.month


def budgetTemplateSuggestions(connection, selectedDate=None):
    selectedDate = selectedDate or date.today()
    if isinstance(selectedDate, str):
        validateDate(selectedDate)
        selectedDate = datetime.strptime(selectedDate, "%Y-%m-%d").date()
    windows = {
        "monthlyDays": applicationSettingInteger(connection, "monthlySuggestionWindowDays", 3),
        "quarterlyMonths": applicationSettingInteger(connection, "quarterlySuggestionWindowMonths", 1),
        "annualMonths": applicationSettingInteger(connection, "annualSuggestionWindowMonths", 3),
    }
    planRows = connection.execute(
        """SELECT templateId, frequency, expectedDate,
                  CASE frequency WHEN 'monthly' THEN 'Monthly' WHEN 'quarterly' THEN 'Quarterly' ELSE 'Yearly' END AS reason
           FROM sinkingFundPlans WHERE active = 1
           UNION ALL
           SELECT templateId, 'monthly', expectedDate, 'Expected income' FROM expectedIncomePlans WHERE active = 1"""
    ).fetchall()
    linkedTemplateIds = set()
    suggestedTemplateIds = set()
    suggestionReasons = {}
    for row in planRows:
        linkedTemplateIds.add(row["templateId"])
        priorDate, upcomingDate = sinkingFundOccurrenceRange(
            row["expectedDate"], row["frequency"], selectedDate
        )
        if row["frequency"] == "monthly":
            nearestDays = min(abs((selectedDate - priorDate).days), abs((upcomingDate - selectedDate).days))
            matches = nearestDays <= windows["monthlyDays"]
        elif row["frequency"] == "quarterly":
            nearestMonths = min(
                abs(calendarMonthDifference(priorDate, selectedDate)),
                abs(calendarMonthDifference(selectedDate, upcomingDate)),
            )
            matches = nearestMonths <= windows["quarterlyMonths"]
        else:
            matches = any(
                0 <= calendarMonthDifference(selectedDate, occurrence) <= windows["annualMonths"]
                for occurrence in (priorDate, upcomingDate)
            )
        if matches:
            suggestedTemplateIds.add(row["templateId"])
            suggestionReasons[str(row["templateId"])] = row["reason"]
    return {
        **windows,
        "linkedTemplateIds": sorted(linkedTemplateIds),
        "suggestedTemplateIds": sorted(suggestedTemplateIds),
        "suggestionReasons": suggestionReasons,
    }


def sinkingFundSummary(connection, today=None):
    today = today or date.today()
    setting = connection.execute("SELECT value FROM applicationSettings WHERE key = 'paychecksPerYear'").fetchone()
    paychecksPerYear = int(setting["value"]) if setting else None
    rows = connection.execute(
        """SELECT p.*, tt.name, tt.accountId, tt.description, tt.categoryId, tt.enteredAmountCents,
                  a.name AS accountName, a.accountClass,
                  a.active AS accountActive, c.name AS categoryName,
                  c.displayOrder AS categoryDisplayOrder, c.parentCategoryId,
                  parent.name AS parentCategoryName
           FROM sinkingFundPlans p
           JOIN transactionTemplates tt ON tt.id = p.templateId
           LEFT JOIN accounts a ON a.id = tt.accountId
           LEFT JOIN categories c ON c.id = tt.categoryId
           LEFT JOIN categories parent ON parent.id = c.parentCategoryId
           ORDER BY p.id"""
    ).fetchall()
    plans = []
    for row in rows:
        amountCents = row["enteredAmountCents"]
        sinkingFund = row["frequency"] != "monthly"
        contributionMonths = 3 if row["frequency"] == "quarterly" else 12
        monthlyContributionCents = None if not sinkingFund else (amountCents + contributionMonths - 1) // contributionMonths
        perPaycheckContributionCents = None
        if sinkingFund and paychecksPerYear:
            annualAmount = amountCents * (4 if row["frequency"] == "quarterly" else 1)
            perPaycheckContributionCents = (annualAmount + paychecksPerYear - 1) // paychecksPerYear
        priorDate, upcomingDate = sinkingFundOccurrenceRange(row["expectedDate"], row["frequency"], today)
        reserveTargetCents = None
        if sinkingFund:
            elapsedDays = (today - priorDate).days
            cycleDays = (upcomingDate - priorDate).days
            reserveTargetCents = (amountCents * elapsedDays + cycleDays - 1) // cycleDays
        plan = dict(row)
        plan.update({
            "monthlyContributionCents": monthlyContributionCents,
            "perPaycheckContributionCents": perPaycheckContributionCents,
            "reserveTargetCents": reserveTargetCents,
            "upcomingExpectedDate": upcomingDate.isoformat(),
        })
        plans.append(plan)
    activeSinkingFunds = [plan for plan in plans if plan["active"] and plan["frequency"] != "monthly"]
    activeMonthlyPlans = [plan for plan in plans if plan["active"] and plan["frequency"] == "monthly"]
    suggestionSetting = connection.execute(
        "SELECT value FROM applicationSettings WHERE key = 'suggestionRoundingCents'"
    ).fetchone()
    suggestionRoundingCents = int(suggestionSetting["value"]) if suggestionSetting else None
    currentMonthStart = today.replace(day=1)
    suggestionStart = addCalendarMonths(currentMonthStart, -3)
    suggestionRows = connection.execute(
        """SELECT c.id, c.name, c.displayOrder,
                  COALESCE(SUM(CASE WHEN t.transferId IS NULL THEN -t.amountCents ELSE 0 END), 0) AS threeMonthCents
           FROM categories c
           LEFT JOIN transactions t ON t.categoryId = c.id AND t.date >= ? AND t.date < ?
           WHERE c.active = 1 AND c.reportingRole = 'expense'
             AND NOT EXISTS(SELECT 1 FROM categories child WHERE child.parentCategoryId = c.id)
           GROUP BY c.id ORDER BY c.displayOrder, c.name, c.id""",
        (suggestionStart.isoformat(), currentMonthStart.isoformat()),
    ).fetchall()
    flexibleCategories = []
    for row in suggestionRows:
        suggestionCents = max(0, int((Decimal(row["threeMonthCents"]) / Decimal(3)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
        if suggestionRoundingCents and suggestionCents:
            suggestionCents = ((suggestionCents + suggestionRoundingCents - 1) // suggestionRoundingCents) * suggestionRoundingCents
        flexibleCategories.append({
            "id": row["id"], "name": row["name"], "displayOrder": row["displayOrder"],
            "suggestionCents": suggestionCents, "active": 1,
        })
    categorySuggestions = {row["id"]: row["suggestionCents"] for row in flexibleCategories}
    flexibleRows = connection.execute(
        """SELECT b.*, c.name AS categoryName, c.displayOrder
           FROM flexibleCategoryBudgets b JOIN categories c ON c.id = b.categoryId
           ORDER BY c.displayOrder, c.name, b.id"""
    ).fetchall()
    flexibleBudgets = []
    for row in flexibleRows:
        item = dict(row)
        item["suggestionCents"] = categorySuggestions.get(row["categoryId"], 0)
        flexibleBudgets.append(item)

    incomeRows = connection.execute(
        """SELECT p.*, tt.name, tt.accountId, tt.description, tt.categoryId, tt.enteredAmountCents,
                  a.name AS accountName, c.name AS categoryName
           FROM expectedIncomePlans p JOIN transactionTemplates tt ON tt.id = p.templateId
           LEFT JOIN accounts a ON a.id = tt.accountId LEFT JOIN categories c ON c.id = tt.categoryId
           ORDER BY p.id"""
    ).fetchall()
    expectedIncomePlans = []
    for row in incomeRows:
        item = dict(row)
        item["upcomingExpectedDate"] = sinkingFundOccurrenceRange(row["expectedDate"], "monthly", today)[1].isoformat()
        expectedIncomePlans.append(item)

    paymentRows = connection.execute(
        """SELECT p.*, a.name AS accountName FROM plannedCreditPayments p
           JOIN accounts a ON a.id = p.accountId ORDER BY p.id"""
    ).fetchall()
    plannedCreditPayments = []
    for row in paymentRows:
        item = dict(row)
        item["upcomingExpectedDate"] = sinkingFundOccurrenceRange(row["expectedDate"], "monthly", today)[1].isoformat()
        plannedCreditPayments.append(item)

    comingUpEnd = today + timedelta(days=7)
    comingUp = []
    for plan in plans:
        if plan["active"] and today <= datetime.strptime(plan["upcomingExpectedDate"], "%Y-%m-%d").date() <= comingUpEnd:
            comingUp.append({
                "date": plan["upcomingExpectedDate"], "item": plan["name"],
                "type": "Monthly Recurring" if plan["frequency"] == "monthly" else "Sinking Fund",
                "amountCents": plan["enteredAmountCents"], "id": f"budget-{plan['id']}",
            })
    for plan in expectedIncomePlans:
        if plan["active"] and today <= datetime.strptime(plan["upcomingExpectedDate"], "%Y-%m-%d").date() <= comingUpEnd:
            comingUp.append({
                "date": plan["upcomingExpectedDate"], "item": plan["name"], "type": "Expected Income",
                "amountCents": plan["enteredAmountCents"], "id": f"income-{plan['id']}",
            })
    for plan in plannedCreditPayments:
        if plan["active"] and today <= datetime.strptime(plan["upcomingExpectedDate"], "%Y-%m-%d").date() <= comingUpEnd:
            comingUp.append({
                "date": plan["upcomingExpectedDate"], "item": plan["description"], "type": "Debt Payment",
                "amountCents": plan["amountCents"], "id": f"payment-{plan['id']}",
            })
    comingUp.sort(key=lambda item: (item["date"], item["type"].lower(), item["item"].lower(), item["id"]))

    totalFlexibleCents = sum(item["monthlyTargetCents"] for item in flexibleBudgets if item["active"])
    totalExpectedIncomeCents = sum(item["enteredAmountCents"] for item in expectedIncomePlans if item["active"])
    totalCreditPaymentsCents = sum(item["amountCents"] for item in plannedCreditPayments if item["active"])
    totalMonthlyRecurringCents = sum(plan["enteredAmountCents"] for plan in activeMonthlyPlans)
    totalMonthlyContributionCents = sum(plan["monthlyContributionCents"] for plan in activeSinkingFunds)
    paymentAccountTotals = {}
    for plan in activeMonthlyPlans:
        accountId = plan["accountId"]
        item = paymentAccountTotals.setdefault(accountId, {
            "accountId": accountId,
            "accountName": plan["accountName"] if accountId is not None else "Unassigned",
            "accountClass": plan["accountClass"],
            "amountCents": 0,
            "items": [],
        })
        item["amountCents"] += plan["enteredAmountCents"]
        item["items"].append({
            "id": plan["id"], "name": plan["name"],
            "upcomingExpectedDate": plan["upcomingExpectedDate"],
            "amountCents": plan["enteredAmountCents"],
        })
    monthlyRecurringByPaymentAccount = sorted(
        paymentAccountTotals.values(),
        key=lambda item: (
            item["accountId"] is None,
            0 if item["accountClass"] == "cash" else 1,
            item["accountName"].lower(),
            item["accountId"] or 0,
        ),
    )
    categoryTotals = {}
    for plan in activeMonthlyPlans:
        categoryId = plan["categoryId"]
        item = categoryTotals.setdefault(categoryId, {
            "categoryId": categoryId,
            "categoryName": plan["categoryName"],
            "parentCategoryName": plan["parentCategoryName"],
            "displayOrder": plan["categoryDisplayOrder"],
            "amountCents": 0,
            "items": [],
        })
        item["amountCents"] += plan["enteredAmountCents"]
        item["items"].append({
            "id": plan["id"], "name": plan["name"],
            "upcomingExpectedDate": plan["upcomingExpectedDate"],
            "amountCents": plan["enteredAmountCents"],
        })
    monthlyRecurringByCategory = sorted(
        categoryTotals.values(),
        key=lambda item: (
            item["displayOrder"] if item["displayOrder"] is not None else 0,
            item["categoryName"].lower(),
            item["categoryId"],
        ),
    )
    return {
        "paychecksPerYear": paychecksPerYear,
        "suggestionRoundingCents": suggestionRoundingCents,
        "plans": plans,
        "flexibleCategories": flexibleCategories,
        "flexibleBudgets": flexibleBudgets,
        "expectedIncomePlans": expectedIncomePlans,
        "plannedCreditPayments": plannedCreditPayments,
        "comingUp": comingUp,
        "totalReserveTargetCents": sum(plan["reserveTargetCents"] for plan in activeSinkingFunds),
        "totalMonthlyContributionCents": totalMonthlyContributionCents,
        "totalPerPaycheckContributionCents": None if not paychecksPerYear else sum(plan["perPaycheckContributionCents"] for plan in activeSinkingFunds),
        "totalMonthlyRecurringCents": totalMonthlyRecurringCents,
        "monthlyRecurringByPaymentAccount": monthlyRecurringByPaymentAccount,
        "monthlyRecurringByCategory": monthlyRecurringByCategory,
        "totalFlexibleCategoryBudgetCents": totalFlexibleCents,
        "totalPlannedCreditPaymentCents": totalCreditPaymentsCents,
        "totalExpectedIncomeCents": totalExpectedIncomeCents,
        "monthlyRequirementCents": totalMonthlyRecurringCents + totalFlexibleCents + totalCreditPaymentsCents + totalMonthlyContributionCents - totalExpectedIncomeCents,
    }


def migrateToVersion19(connection):
    """Add authentication and durable proposals without rewriting financial records."""
    connection.executescript(securitySchemaSql)


def migrateToVersion20(connection):
    """Keep saved Owner corrections separate from immutable agent retry content."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(pendingSubmissions)")}
    if "reviewContent" not in columns:
        connection.execute("ALTER TABLE pendingSubmissions ADD COLUMN reviewContent TEXT")
    if "reviewVersion" not in columns:
        connection.execute("ALTER TABLE pendingSubmissions ADD COLUMN reviewVersion INTEGER NOT NULL DEFAULT 0 CHECK (reviewVersion >= 0)")


class SubmissionConflict(ValidationError):
    pass


def integrationLabel(label):
    if not isinstance(label, str):
        raise ValidationError("Label must be text")
    label = requireText(label, "Label")
    if len(label) > 128:
        raise ValidationError("Label must contain at most 128 characters")
    return label


def updateIntegrationLabel(connection, secretId, label):
    label = integrationLabel(label)
    with connection:
        if not connection.execute("UPDATE integrationSecrets SET label = ? WHERE id = ?", (label, secretId)).rowcount:
            raise ValidationError("Integration secret does not exist")


def createIntegrationSecret(connection, label):
    label = integrationLabel(label)
    with connection:
        return connection.execute(
            "INSERT INTO integrationSecrets (label, secret) VALUES (?, ?)",
            (label, secrets.token_urlsafe(32)),
        ).lastrowid


def pendingKind(submitted):
    return "transfer" if submitted.get("type") == "transfer" else "transaction"


def pendingFields(kind):
    return ("date", "fromAccountId", "toAccountId", "description", "amount") if kind == "transfer" else ("date", "accountId", "description", "categoryId", "amount")


def submissionContent(submitted, manual=False):
    if not isinstance(submitted, dict):
        raise ValidationError("Submission must be an object")
    kind = pendingKind(submitted)
    fields = {"submissionId", *pendingFields(kind)}
    if kind == "transfer":
        fields.add("type")
    elif manual:
        fields.add("pinned")
    if set(submitted) != fields:
        raise ValidationError("Submit exactly the supported ordinary or transfer fields; transfers have type transfer and no Category")
    if not isinstance(submitted["submissionId"], str) or not 1 <= len(submitted["submissionId"]) <= 128 or not submitted["submissionId"].strip():
        raise ValidationError("Submission ID must contain 1 through 128 characters")
    idFields = ("fromAccountId", "toAccountId") if kind == "transfer" else ("accountId", "categoryId")
    if any(type(submitted[field]) is not int or submitted[field] <= 0 for field in idFields):
        raise ValidationError("Account and Category IDs must be positive integers")
    if any(not isinstance(submitted[field], str) for field in ("date", "description", "amount")):
        raise ValidationError("Date, Description, and Amount must be strings; Amount is entered USD, not ledger cents")
    if manual and kind == "transaction" and type(submitted["pinned"]) is not bool:
        raise ValidationError("Pin must be Boolean")
    return json.dumps(submitted, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def preparePending(connection, kind, row, pinned=False):
    if kind == "transfer":
        return prepareTransfer(connection, row.get("date"), row.get("fromAccountId"), row.get("toAccountId"), row.get("amount"), row.get("description")), None
    values = prepareTransaction(connection, row.get("accountId"), row.get("date"), row.get("description"), row.get("categoryId"), row.get("amount"), pinned)
    return values, getSelectableCategory(connection, row.get("categoryId"))["reportingRole"]


def insertPending(connection, integrationId, submitted, originalContent, role, sourceSnapshot=None):
    cursor = connection.execute(
        "INSERT INTO pendingSubmissions (integrationId, submissionId, originalContent, originalRole, kind, source, pinned, sourceSnapshot) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (integrationId, submitted["submissionId"], originalContent, role, pendingKind(submitted), "manual" if integrationId is None else "integration", int(submitted.get("pinned", False)), sourceSnapshot),
    )
    return dict(connection.execute("SELECT * FROM pendingSubmissions WHERE id = ?", (cursor.lastrowid,)).fetchone())


def submitPendingTransaction(connection, integrationId, submitted):
    return submitPendingEntry(connection, integrationId, submitted)


def submitPendingEntry(connection, integrationId, submitted):
    originalContent = submissionContent(submitted, manual=integrationId is None)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT * FROM pendingSubmissions WHERE integrationId IS ? AND submissionId = ?", (integrationId, submitted["submissionId"])).fetchone()
        if existing:
            if existing["originalContent"] != originalContent:
                raise SubmissionConflict("Submission ID already used with different content")
            return dict(existing), False
        values, role = preparePending(connection, pendingKind(submitted), submitted, submitted.get("pinned", False))
        return insertPending(connection, integrationId, submitted, originalContent, role), True


def pendingCounts(rows):
    transactions = sum(row["kind"] == "transaction" for row in rows)
    return {"count": len(rows), "transactionCount": transactions, "transferCount": len(rows) - transactions}


def pendingReviewSummary(rows):
    if not isinstance(rows, list):
        raise ValidationError("Summary rows must be a list")
    totals = {"transaction": 0, "transfer": 0}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"kind", "amount"} or row["kind"] not in ("transaction", "transfer"):
            raise ValidationError("Each summary row requires a proposal kind and entered Amount")
        kind = row["kind"]
        try:
            if not isinstance(row["amount"], str):
                raise ValidationError("Amount must be entered USD text")
            amountCents = parseMoneyCents(row["amount"], positiveOnly=kind == "transfer")
        except ValidationError:
            # An invalid amount makes its entire kind unavailable; never show a partial sum.
            totals[kind] = None
        else:
            if totals[kind] is not None:
                # Entered signs are already user-facing: refunds/reversals subtract.
                # Each transfer proposal contributes once, without creating ledger rows.
                totals[kind] += amountCents
    return {"transactionCents": totals["transaction"], "transferCents": totals["transfer"]}


def pendingReviewRows(connection):
    reviewRows = []
    for proposal in connection.execute(
        "SELECT p.*, s.label FROM pendingSubmissions p LEFT JOIN integrationSecrets s ON s.id = p.integrationId WHERE p.status = 'Pending' ORDER BY p.id"
    ):
        submitted = json.loads(proposal["reviewContent"] or proposal["originalContent"])
        review = {**submitted, "id": proposal["id"], "kind": proposal["kind"], "source": proposal["source"], "pinned": bool(proposal["pinned"]), "submissionId": proposal["submissionId"], "label": "Manual" if proposal["source"] == "manual" else proposal["label"], "originalRole": proposal["originalRole"], "reviewVersion": proposal["reviewVersion"]}
        category = connection.execute("SELECT reportingRole FROM categories WHERE id = ?", (submitted.get("categoryId"),)).fetchone()
        review["currentRole"] = category["reportingRole"] if category else None
        review["roleChanged"] = proposal["kind"] == "transaction" and review["currentRole"] != review["originalRole"]
        try:
            values, role = preparePending(connection, proposal["kind"], submitted)
            review["ledgerAmountCents"] = values[3] if proposal["kind"] == "transaction" else None
            review["validationError"] = None
        except ValidationError as error:
            review["ledgerAmountCents"] = None
            review["validationError"] = str(error)
        reviewRows.append(review)
    return reviewRows


def validatePendingReviewVersion(proposal, reviewVersion):
    if type(reviewVersion) is not int or reviewVersion != proposal["reviewVersion"]:
        raise SubmissionConflict("Review is stale: this proposal was saved in another view. Refresh before saving or deciding.")


def validatedReview(connection, proposal, row):
    fields = pendingFields(proposal["kind"])
    if set(row) - {*fields, "id", "reviewedRole", "reviewVersion", "kind"} or row.get("kind", proposal["kind"]) != proposal["kind"]:
        raise ValidationError("Proposal kind is fixed; submit only its review fields")
    content = {key: row.get(key) for key in fields}
    submitted = {"submissionId": proposal["submissionId"], **content}
    if proposal["kind"] == "transfer":
        submitted["type"] = "transfer"
    submissionContent(submitted)
    values, role = preparePending(connection, proposal["kind"], content, bool(proposal["pinned"]))
    if proposal["kind"] == "transaction" and row.get("reviewedRole") != role:
        raise SubmissionConflict("Category Reporting Role changed. Review the current sign and approve again.")
    return content, values


def savePendingReview(connection, proposalId, row, integrationUpdate=False):
    if not isinstance(row, dict):
        raise ValidationError("Review fields must be an object")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        proposal = connection.execute("SELECT * FROM pendingSubmissions WHERE id = ?", (proposalId,)).fetchone()
        if not proposal and integrationUpdate:
            return None
        if not proposal or proposal["status"] != "Pending":
            raise SubmissionConflict("Review is stale: this proposal is no longer Pending. Refresh before saving.")
        requiredFields = {*pendingFields(proposal["kind"]), "reviewVersion"}
        if not integrationUpdate or proposal["kind"] == "transaction":
            requiredFields.add("reviewedRole")
        if set(row) != requiredFields:
            raise ValidationError("Submit only the fields for this proposal kind and review metadata")
        validatePendingReviewVersion(proposal, row["reviewVersion"])
        content, values = validatedReview(connection, proposal, row)
        nextVersion = proposal["reviewVersion"] + 1
        connection.execute("UPDATE pendingSubmissions SET reviewContent = ?, reviewVersion = ? WHERE id = ?", (json.dumps(content, ensure_ascii=False, sort_keys=True), nextVersion, proposalId))
    return nextVersion


def decidePendingTransactions(connection, decision, rows, approveAll=False):
    if type(approveAll) is not bool or (approveAll and decision != "approve"):
        raise ValidationError("Approve All is only available for approval")
    if decision not in {"approve", "reject"} or not isinstance(rows, list) or not rows:
        raise ValidationError("Select proposals and choose Approve or Reject")
    if any(not isinstance(row, dict) or type(row.get("id")) is not int for row in rows):
        raise ValidationError("Each selected proposal requires an integer ID")
    proposalIds = [row["id"] for row in rows]
    if len(set(proposalIds)) != len(proposalIds):
        raise ValidationError("A proposal may only be selected once")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if approveAll:
            pendingIds = {proposal["id"] for proposal in connection.execute("SELECT id FROM pendingSubmissions WHERE status = 'Pending'")}
            if pendingIds != set(proposalIds):
                raise SubmissionConflict("Review is stale: the pending queue changed. Refresh before approving all.")
        for row in rows:
            proposal = connection.execute("SELECT * FROM pendingSubmissions WHERE id = ?", (row["id"],)).fetchone()
            if not proposal or proposal["status"] != "Pending":
                raise SubmissionConflict("Review is stale: a selected proposal is no longer Pending. Refresh before deciding.")
            validatePendingReviewVersion(proposal, row.get("reviewVersion", 0))
            if approveAll:
                savedContent = json.loads(proposal["reviewContent"] or proposal["originalContent"])
                if any(row.get(field) != savedContent[field] for field in pendingFields(proposal["kind"])):
                    raise SubmissionConflict("Save or cancel all row changes before approving all pending transactions.")
            transactionId = transferId = None
            if decision == "approve":
                content, values = validatedReview(connection, proposal, row)
                if proposal["kind"] == "transfer":
                    transferId = insertTransfer(connection, values)
                else:
                    transactionId = connection.execute("INSERT INTO transactions (accountId, date, description, amountCents, categoryId, pinned) VALUES (?, ?, ?, ?, ?, ?)", values).lastrowid
            connection.execute("UPDATE pendingSubmissions SET status = ?, transactionId = ?, transferId = ? WHERE id = ?", ("Approved" if decision == "approve" else "Rejected", transactionId, transferId, row["id"]))
    return len(rows)


def ledgerMoveSnapshot(connection, transactionId=None, transferId=None):
    if (transactionId is None) == (transferId is None):
        raise ValidationError("Choose one ledger event")
    if transferId is not None:
        rows = connection.execute("SELECT * FROM transactions WHERE transferId = ? ORDER BY amountCents, id", (transferId,)).fetchall()
        if len(rows) != 2 or rows[0]["amountCents"] >= 0 or rows[1]["amountCents"] <= 0 or sum(row["amountCents"] for row in rows) != 0 or rows[0]["accountId"] == rows[1]["accountId"] or rows[0]["date"] != rows[1]["date"] or rows[0]["description"] != rows[1]["description"] or any(row["categoryId"] is not None for row in rows):
            raise ValidationError("Transfer is incomplete or invalid")
    else:
        row = connection.execute("SELECT * FROM transactions WHERE id = ?", (transactionId,)).fetchone()
        if not row or row["transferId"] is not None or row["categoryId"] is None:
            raise ValidationError("Only categorized ordinary transactions or valid transfers can move to Pending")
        rows = [row]
    snapshot = [{key: row[key] for key in ("id", "eventIdentity", "accountId", "date", "description", "amountCents", "categoryId", "transferId", "pinned")} for row in rows]
    if transferId is None:
        snapshot[0]["reportingRole"] = connection.execute("SELECT reportingRole FROM categories WHERE id = ?", (rows[0]["categoryId"],)).fetchone()["reportingRole"]
    snapshot[0]["enteredAmount"] = moneyInputValue(-rows[0]["amountCents"] if transferId is not None else storedAmountForCategory(snapshot[0]["reportingRole"], rows[0]["amountCents"]))
    return snapshot


def moveLedgerToPending(connection, requestId, snapshot, transactionId=None, transferId=None):
    if not isinstance(requestId, str) or not 1 <= len(requestId) <= 128 or not requestId.strip() or not isinstance(snapshot, list):
        raise ValidationError("Move requires a request identity and reviewed snapshot")
    originalSnapshot = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT * FROM pendingSubmissions WHERE source = 'manual' AND submissionId = ?", (requestId,)).fetchone()
        if existing:
            if existing["sourceSnapshot"] != originalSnapshot:
                raise SubmissionConflict("Move identity already used with another event")
            # Retry before source lookup: a reused ledger ID is never removed.
            return dict(existing), False
        current = ledgerMoveSnapshot(connection, transactionId, transferId)
        if current != snapshot:
            raise SubmissionConflict("Ledger event changed. Refresh before moving to Pending.")
        first = current[0]
        submitted = {"submissionId": requestId, "date": first["date"], "description": first["description"]}
        if transferId is not None:
            submitted.update(type="transfer", fromAccountId=first["accountId"], toAccountId=current[1]["accountId"], amount=moneyInputValue(current[1]["amountCents"]))
            role = None
        else:
            role = first["reportingRole"]
            enteredCents = storedAmountForCategory(role, first["amountCents"])
            submitted.update(accountId=first["accountId"], categoryId=first["categoryId"], amount=moneyInputValue(enteredCents), pinned=bool(first["pinned"]))
        proposal = insertPending(connection, None, submitted, submissionContent(submitted, manual=True), role, originalSnapshot)
        for sourceRow in current:
            connection.execute("UPDATE simulatorRows SET executed = 0, executedTransactionId = NULL WHERE executedTransactionId = ?", (sourceRow["id"],))
            connection.execute("DELETE FROM transactions WHERE id = ?", (sourceRow["id"],))
        return proposal, True


def moneyInputValue(cents):
    return ("-" if cents < 0 else "") + f"{abs(cents) // 100}.{abs(cents) % 100:02d}"


def migrateToVersion21(connection):
    """Preserve proposals and history while adding kinds, Manual identity and selector pins."""
    proposalColumns = {row["name"] for row in connection.execute("PRAGMA table_info(pendingSubmissions)")}
    if "kind" not in proposalColumns:
        connection.executescript("""
        BEGIN IMMEDIATE;
        DROP INDEX IF EXISTS pendingStatusIndex;
        ALTER TABLE pendingSubmissions RENAME TO previousPendingSubmissions;
        CREATE TABLE pendingSubmissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            integrationId INTEGER REFERENCES integrationSecrets(id) ON DELETE RESTRICT,
            submissionId TEXT NOT NULL,
            originalContent TEXT NOT NULL,
            reviewContent TEXT,
            reviewVersion INTEGER NOT NULL DEFAULT 0 CHECK (reviewVersion >= 0),
            originalRole TEXT CHECK (originalRole IN ('income', 'expense')),
            kind TEXT NOT NULL DEFAULT 'transaction' CHECK (kind IN ('transaction', 'transfer')),
            source TEXT NOT NULL DEFAULT 'integration' CHECK (source IN ('integration', 'manual')),
            pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
            sourceSnapshot TEXT,
            status TEXT NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending', 'Approved', 'Rejected')),
            transactionId INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
            transferId TEXT,
            CHECK ((source = 'manual' AND integrationId IS NULL) OR (source = 'integration' AND integrationId IS NOT NULL)),
            CHECK ((kind = 'transaction' AND originalRole IS NOT NULL AND transferId IS NULL) OR (kind = 'transfer' AND originalRole IS NULL AND transactionId IS NULL)),
            UNIQUE(integrationId, submissionId)
        );
        INSERT INTO pendingSubmissions (id, integrationId, submissionId, originalContent, reviewContent, reviewVersion, originalRole, status, transactionId)
            SELECT id, integrationId, submissionId, originalContent, reviewContent, reviewVersion, originalRole, status, transactionId FROM previousPendingSubmissions;
        DROP TABLE previousPendingSubmissions;
        CREATE UNIQUE INDEX manualSubmissionIdentity ON pendingSubmissions(submissionId) WHERE source = 'manual';
        CREATE INDEX pendingStatusIndex ON pendingSubmissions(status, id);
        COMMIT;
    """)
    accountColumns = {row["name"] for row in connection.execute("PRAGMA table_info(accounts)")}
    if "pinnedForSelection" not in accountColumns:
        connection.execute("""ALTER TABLE accounts ADD COLUMN pinnedForSelection INTEGER NOT NULL DEFAULT 0 CHECK (pinnedForSelection IN (0, 1) AND (pinnedForSelection = 0 OR (active = 1 AND accountClass IN ('cash', 'credit'))));""")
    transactionColumns = {row["name"] for row in connection.execute("PRAGMA table_info(transactions)")}
    if "eventIdentity" not in transactionColumns:
        # Moving removes a ledger event. Later approval must create new row IDs,
        # while an event token also identifies stale snapshots independently of IDs.
        transactionSql = schemaSql[schemaSql.index("CREATE TABLE IF NOT EXISTS transactions ("):schemaSql.index("CREATE TABLE IF NOT EXISTS transactionTemplates (")]
        transactionSql = transactionSql.replace("IF NOT EXISTS transactions", "transactionsNext").replace("id INTEGER PRIMARY KEY,", "id INTEGER PRIMARY KEY AUTOINCREMENT,", 1).replace("    transferId TEXT,", "    eventIdentity TEXT,\n    transferId TEXT,")
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.executescript("BEGIN IMMEDIATE;\n" + transactionSql + """
                INSERT INTO transactionsNext (id, accountId, date, description, amountCents, categoryId, transferId, pinned, eventIdentity)
                    SELECT id, accountId, date, description, amountCents, categoryId, transferId, pinned, lower(hex(randomblob(16))) FROM transactions;
                DROP TABLE transactions;
                ALTER TABLE transactionsNext RENAME TO transactions;
                CREATE TRIGGER ledgerEventIdentity AFTER INSERT ON transactions WHEN NEW.eventIdentity IS NULL
                    BEGIN UPDATE transactions SET eventIdentity = lower(hex(randomblob(16))) WHERE id = NEW.id; END;
            """ + schemaSql + "COMMIT;")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")
