import hashlib
import os
import sqlite3
from pathlib import Path

import tests  # Activates production-database isolation before importing database code.
from database import createCategory


testCategoryDefinitions = (
    ("Needs", None, 10, False),
    ("Recurring Needs", "Needs", 11, False),
    ("Groceries/Gas/Household", "Needs", 12, False),
    ("Random Needs", "Needs", 13, False),
    ("Interest", "Needs", 14, False),
    ("Wants", None, 20, False),
    ("Experiences", None, 30, False),
    ("Giving", None, 40, False),
    ("Savings/Future", None, 50, False),
    ("Pass-Through", None, 60, True),
    ("Income/Reimbursement", None, 70, False),
    ("Utilities", None, 80, False),
)


def createScenarioCategories(connection):
    """Create explicit category fixtures for tests that exercise the August behavior."""
    categoryIds = {}
    for name, parentName, displayOrder, offerReimbursement in testCategoryDefinitions:
        categoryIds[name] = createCategory(
            connection,
            name,
            categoryIds.get(parentName),
            displayOrder,
            offerReimbursement,
            "income" if name == "Income/Reimbursement" else "expense",
        )
    connection.execute(
        "INSERT OR REPLACE INTO applicationSettings (key, value) VALUES ('reimbursementCategoryId', ?)",
        (str(categoryIds["Income/Reimbursement"]),),
    )
    connection.commit()
    return categoryIds


def ownerClient(application, copiedDatabase=False):
    """Use the real sign-in and CSRF flow for legacy financial regression tests."""
    from users import createUser, resetPassword
    username = "Test Owner"
    connection = connectDatabaseForOwner(application)
    try:
        owner = connection.execute("SELECT username FROM scopedusers WHERE role = 'owner'").fetchone()
        if not owner:
            createUser(connection, "owner", "Test Owner", "test-owner-password")
        elif copiedDatabase:
            # Only disposable integration copies receive a known test password.
            # Preserve the copied identity and never require the source Owner's credentials.
            username = owner["username"]
            resetPassword(connection, connection.execute("SELECT id FROM scopedusers WHERE role = 'owner'").fetchone()[0], "test-owner-password")
    finally:
        connection.close()
    client = application.test_client()
    signInHtml = client.get("/sign-in").get_data(as_text=True)
    import re
    csrfToken = re.search(r'name="csrfToken" value="([^"]+)"', signInHtml).group(1)
    response = client.post("/sign-in", data={"username": username, "password": "test-owner-password", "csrfToken": csrfToken})
    if response.status_code != 302:
        raise AssertionError("Test Owner login failed")
    client.environ_base["HTTP_X_CSRF_TOKEN"] = client.get("/api/session").get_json()["csrfToken"]
    return client


def connectDatabaseForOwner(application):
    return connectFixtureDatabase(application.config["DATABASE_PATH"])


def openReadOnlyDatabase(databasePath):
    databaseUri = Path(databasePath).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(databaseUri, uri=True)


def copyProductionDatabase(testCase, destinationPath):
    configuredSourcePath = os.environ.get("FINANCE_TEST_SOURCE_DATABASE")
    if not configuredSourcePath:
        testCase.skipTest(
            "Real-data integration requires an explicit FINANCE_TEST_SOURCE_DATABASE path"
        )
    sourcePath = Path(configuredSourcePath).resolve()
    if not sourcePath.exists():
        testCase.skipTest("Configured real-data integration source is absent")
    destinationPath = Path(destinationPath).resolve()
    if destinationPath == sourcePath.resolve():
        raise AssertionError("Production integration destination must be a separate temporary path")
    destinationPath.parent.mkdir(parents=True, exist_ok=True)
    sourceHashBefore = hashlib.sha256(sourcePath.read_bytes()).digest()
    sourceConnection = openReadOnlyDatabase(sourcePath)
    targetConnection = sqlite3.connect(destinationPath)
    try:
        sourceConnection.backup(targetConnection)
    finally:
        sourceConnection.close()
        targetConnection.close()
    sourceHashAfter = hashlib.sha256(sourcePath.read_bytes()).digest()
    testCase.assertEqual(sourceHashBefore, sourceHashAfter, "Production finance.db changed during read-only backup")


def connectFixtureDatabase(databasePath):
    """Explicit single-ledger synthetic fixtures; legacy migration inputs stay raw."""
    from database import connectDatabase, bindInitialLedger
    connection = connectDatabase(databasePath)
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'ledgers'").fetchone():
        bindInitialLedger(connection)
    return connection


def useHistoricalSchema21(connection):
    """Rebuild a synthetic fixture in its genuine pre-ledger schema before downgrades."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'ledgers'").fetchone():
        return
    connection.commit()
    historical = sqlite3.connect(":memory:")
    historical.row_factory = sqlite3.Row
    historical.executescript((Path(__file__).parent / "schema21.sql").read_text())
    historicalTriggers = [row[0] for row in historical.execute("SELECT sql FROM sqlite_master WHERE type='trigger'")]
    for row in historical.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        historical.execute(f'DROP TRIGGER "{row[0]}"')
    for row in historical.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'").fetchall():
        table = row[0]
        columns = [column["name"] for column in historical.execute(f"PRAGMA table_info({table})")]
        if table == "users":
            source = "SELECT u.id, a.role, u.username, u.usernameKey, u.passwordHash, u.authVersion FROM users u JOIN ledgerAccess a ON a.userId=u.id"
        else:
            source = "SELECT " + ", ".join(columns) + f" FROM {table}"
        records = connection.execute(source).fetchall()
        if records:
            historical.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in columns)})", [tuple(record) for record in records])
    for statement in historicalTriggers:
        historical.execute(statement)
    historical.commit()
    for row in connection.execute("SELECT name FROM sqlite_temp_master WHERE type='view'").fetchall():
        connection.execute(f'DROP VIEW "{row[0]}"')
    historical.backup(connection)
    historical.close()


def snapshotWithoutLoginTime(connection):
    """Financial/state assertions exclude the separately tested sign-in timestamp."""
    import re
    hasLoginTime = any(row[1] == "lastLoginAt" for row in connection.execute("PRAGMA table_info(users)"))
    statements = list(connection.iterdump())
    if hasLoginTime:
        statements = [re.sub(r",(?:NULL|'[^']*')\);$", ",NULL);", statement) if statement.startswith('INSERT INTO "users"') else statement for statement in statements]
    return statements
