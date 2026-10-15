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
        owner = connection.execute("SELECT username FROM users WHERE role = 'owner'").fetchone()
        if not owner:
            createUser(connection, "owner", "Test Owner", "test-owner-password")
        elif copiedDatabase:
            # Only disposable integration copies receive a known test password.
            # Preserve the copied identity and never require the source Owner's credentials.
            username = owner["username"]
            resetPassword(connection, "owner", "test-owner-password")
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
    from database import connectDatabase
    return connectDatabase(application.config["DATABASE_PATH"])


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
