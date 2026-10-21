import logging
import json
import os
import sqlite3
import tempfile
import secrets
import time
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from flask import Flask, Response, g, jsonify, request, send_file, send_from_directory, redirect, render_template_string
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.serving import make_server

from packageApplication import PackagingError, applicationVersion, createApplicationPackage
from users import configureOnStartup, manageUsers, check_password_hash, passwordHash

from database import (
    ValidationError,
    bindLedger, connectInitialLedgerDatabase, exportLedgerDatabase, setIntegrationState,
    reconcileAccountBalance,
    connectDatabase,
    createAccount,
    createBulkTransactions,
    createCategory,
    createTransaction,
    createTransactionWithReimbursement,
    createTemplate,
    createSinkingFundPlan,
    createFlexibleCategoryBudget,
    createExpectedIncomePlan,
    createPlannedCreditPayment,
    createTransfer,
    createSimulatorPlan,
    deleteTransaction,
    deleteTemplate,
    deleteSinkingFundPlan,
    deleteFlexibleCategoryBudget,
    deleteExpectedIncomePlan,
    deletePlannedCreditPayment,
    deleteCategory,
    deleteSimulatorPlan,
    deleteSimulatorRow,
    deleteExecutedSimulatorRows,
    initializeDatabase,
    dashboardReport,
    reportingPeriod,
    schemaVersion,
    saveSimulatorRow,
    sendLedgerEventToSimulator,
    simulatorLedgerMapping,
    setSimulatorRowExecuted,
    recordSimulatorTransaction,
    recordSimulatorTransfer,
    simulatorWorkspace,
    minimumStatus,
    parseMoneyCents,
    setPaychecksPerYear,
    setSuggestionRounding,
    setBudgetSuggestionWindows,
    budgetTemplateSuggestions,
    sinkingFundSummary,
    updateAccount,
    updateCategory,
    updateTransaction,
    updateTemplate,
    updateSinkingFundPlan,
    updateFlexibleCategoryBudget,
    updateExpectedIncomePlan,
    updatePlannedCreditPayment,
    updateTransfer,
    updateSimulatorPlan,
    utilizationWarningHelper,
    utilizationStatus,
    createIntegrationSecret,
    updateIntegrationLabel,
    submitPendingTransaction,
    pendingReviewRows,
    pendingReviewSummary,
    savePendingReview,
    decidePendingTransactions,
    SubmissionConflict,
    preparePending,
    pendingCounts,
    submitPendingEntry,
    ledgerMoveSnapshot,
    moveLedgerToPending,
    getSelectableCategory,
)


projectDirectory = Path(__file__).resolve().parent
databasePath = Path(os.environ.get("FINANCE_DATABASE_PATH", projectDirectory / "finance.db"))
def combinedRecentEvents(rows, limit=5):
    transferRows = {}
    for row in rows:
        if row["transferId"]:
            transferRows.setdefault(row["transferId"], []).append(row)
    handledTransfers = set()
    events = []
    for row in rows:
        transferId = row["transferId"]
        if transferId and transferId not in handledTransfers:
            linked = transferRows[transferId]
            valid = (len(linked) == 2 and sum(item["amountCents"] for item in linked) == 0
                     and linked[0]["accountId"] != linked[1]["accountId"]
                     and linked[0]["date"] == linked[1]["date"]
                     and linked[0]["description"] == linked[1]["description"]
                     and all(item["categoryId"] is None for item in linked))
            if valid:
                fromRow = next((item for item in linked if item["amountCents"] < 0), None)
                toRow = next((item for item in linked if item["amountCents"] > 0), None)
                valid = fromRow is not None and toRow is not None
            if valid:
                event = dict(fromRow)
                event.update({
                    "id": max(item["id"] for item in linked),
                    "accountName": f"{fromRow['accountName']} → {toRow['accountName']}",
                    "categoryName": "Transfer", "amountCents": -fromRow["amountCents"],
                    "combinedTransfer": True,
                    "displayAmountCents": -fromRow["amountCents"],
                    "fromAccountId": fromRow["accountId"], "toAccountId": toRow["accountId"],
                    "fromAccountName": fromRow["accountName"], "toAccountName": toRow["accountName"],
                })
                events.append(event)
                handledTransfers.add(transferId)
                if len(events) == limit:
                    break
                continue
        if transferId and transferId in handledTransfers:
            continue
        event = dict(row)
        event["combinedTransfer"] = False
        event["displayAmountCents"] = (
            row["amountCents"] if transferId or not row["categoryName"] or ("categoryRole" in row.keys() and row["categoryRole"] == "income")
            else -row["amountCents"]
        )
        events.append(event)
        if len(events) == limit:
            break
    return events


def accountEligibility(account):
    return {
        "ordinaryEligible": bool(account["active"] and account["accountClass"] != "system"),
        "transferEligible": bool(account["active"]),
    }


def createApp(databasePathOverride=None):
    app = Flask(__name__)
    activeDatabasePath = Path(databasePathOverride or databasePath)
    initializeDatabase(activeDatabasePath)
    app.config["DATABASE_PATH"] = str(activeDatabasePath)
    app.config["PACKAGE_OUTPUT_DIRECTORY"] = str(projectDirectory / "packages")
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    sessions = {}
    loginAttempts = {}
    usernameAttempts = {}
    authenticationLock = threading.RLock()
    dummyPasswordHash = passwordHash(secrets.token_urlsafe(32))

    def authenticationPage(message=None, csrfToken=None, status=200):
        return render_template_string("""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex, nofollow, noarchive"><title>No Part Finance — Sign In</title><meta name="theme-color" content="#202124"><meta name="apple-mobile-web-app-title" content="No Part Finance"><meta name="apple-mobile-web-app-capable" content="yes"><link rel="manifest" href="/pwa.webmanifest"><link rel="apple-touch-icon" href="/assets/apple-touch-icon.png"><script src="/theme.js"></script><link rel="stylesheet" href="/styles.css"></head><body><main class="signIn"><h1>No Part Finance</h1>{% if csrfToken %}<h2>Sign In</h2><form method="post" action="/sign-in"><input type="hidden" name="csrfToken" value="{{ csrfToken }}"><label>Username <input name="username" autocomplete="username" required maxlength="64"></label><label>Password <input name="password" type="password" autocomplete="current-password" required maxlength="128"></label><p role="alert">{{ message or "" }}</p><button type="submit">Sign In</button></form>{% else %}<p>{{ message }}</p>{% endif %}</main><footer class="signInVersion">No Part Finance {{ version }}</footer></body></html>""", message=message, csrfToken=csrfToken, version=applicationVersion), status

    def setSessionCookie(response, token, maxAge):
        response.set_cookie("npfSession", token, max_age=maxAge, httponly=True, secure=request.is_secure, samesite="Lax")
        return response

    def csrfValid():
        received = request.form.get("csrfToken") or request.headers.get("X-CSRF-Token", "")
        return bool(g.browserSession and received and secrets.compare_digest(received, g.browserSession["csrfToken"]))

    @app.before_request
    def authorizeRequest():
        g.user = None
        g.integration = None
        g.browserSession = None
        if request.endpoint in {"robots", "styles", "javascript", "apiDocumentation", "openApiDocument", "webAppManifest", "themeJavascript", "pwaIcon"} or request.endpoint is None:
            return None
        connection = getDatabase()
        if not connection.execute("SELECT 1 FROM ledgerAccess WHERE role = 'owner'").fetchone():
            message = "No Part Finance is not configured. An Owner is required before you can sign in. From the application folder, run startWindows.bat manage-users (Windows) or ./startLinux.sh manage-users (Linux) in a terminal and choose Create Owner. After setup, reload this page."
            if request.path.startswith("/api/") or request.path == "/health":
                return jsonify({"error": message}), 503
            return authenticationPage(message, status=503)
        authorizationParts = request.headers.get("Authorization", "").split(None, 1)
        isBearer = bool(authorizationParts and authorizationParts[0].lower() == "bearer")
        if request.path.startswith("/api/integration/"):
            secret = authorizationParts[1] if isBearer and len(authorizationParts) == 2 else ""
            if secret:
                g.integration = connection.execute("SELECT id, label, ledgerId FROM integrationSecrets WHERE secret = ? AND active = 1 AND revoked = 0 AND EXISTS (SELECT 1 FROM ledgerAccess WHERE ledgerAccess.ledgerId = integrationSecrets.ledgerId AND role = 'owner')", (secret,)).fetchone()
            if not g.integration:
                return jsonify({"error": "Valid integration Bearer authentication required"}), 401
            bindLedger(connection, g.integration["ledgerId"], "integration", g.integration["id"], g.integration["label"])
            return None
        # A proxy's Basic/Digest header is not an integration credential and grants
        # no app access. Browser requests still require the normal session and CSRF.
        if isBearer:
            return jsonify({"error": "Integration credentials cannot access browser operations"}), 403
        token = request.cookies.get("npfSession")
        now = time.time()
        with authenticationLock:
            for expiredToken in [key for key, value in sessions.items() if value["expiresAt"] <= now]:
                del sessions[expiredToken]
            g.browserSession = sessions.get(token)
        if g.browserSession and g.browserSession.get("userId"):
            memberships = connection.execute("SELECT u.id, u.username, u.authVersion, a.ledgerId, a.role, a.enabled AS accessEnabled FROM users u JOIN ledgerAccess a ON a.userId = u.id WHERE u.id = ?", (g.browserSession["userId"],)).fetchall()
            if len(memberships) == 1:
                user = memberships[0]
                owner = connection.execute("SELECT 1 FROM ledgerAccess WHERE ledgerId = ? AND role = 'owner'", (user["ledgerId"],)).fetchone()
                if owner and user["accessEnabled"] and user["authVersion"] == g.browserSession["authVersion"]:
                    g.user = user
                    bindLedger(connection, user["ledgerId"], "user", user["id"], user["username"])
        if request.endpoint == "signIn":
            return None
        if not g.user:
            if request.path.startswith("/api/") or request.path == "/health":
                return jsonify({"error": "Sign In required"}), 401
            return redirect("/sign-in")
        ownerOnly = request.endpoint in {"integrationSecrets", "editIntegrationLabel", "revealIntegrationSecret", "revokeIntegrationSecret", "changeIntegrationState", "reviewPending", "savePendingRow", "previewPending", "summarizePending", "decidePending", "pendingPage", "downloadBackup", "downloadApplicationPackage", "transactionPendingSnapshot", "transferPendingSnapshot"}
        isMutation = request.method not in {"GET", "HEAD", "OPTIONS"}
        nonpersistentPosts = {"evaluateMoneyExpression", "accountMinimumHelper", "accountUtilizationWarningHelper", "logOut"}
        if g.user["role"] == "auditor" and (ownerOnly or (isMutation and request.endpoint not in nonpersistentPosts)):
            return jsonify({"error": "Read-only Auditor access"}), 403
        if isMutation and not csrfValid():
            return jsonify({"error": "CSRF validation failed. Reload and try again."}), 403

    @app.route("/sign-in", methods=["GET", "POST"])
    def signIn():
        if request.method == "GET":
            token = secrets.token_urlsafe(32)
            csrfToken = secrets.token_urlsafe(32)
            with authenticationLock:
                sessions.pop(request.cookies.get("npfSession"), None)
                sessions[token] = {"expiresAt": time.time() + 600, "csrfToken": csrfToken}
            response = app.make_response(authenticationPage(csrfToken=csrfToken))
            return setSessionCookie(response, token, 600)
        if not csrfValid():
            return authenticationPage("Sign In form expired. Reload and try again.", status=403)
        username = request.form.get("username", "").strip().casefold()
        password = request.form.get("password", "")
        address = request.remote_addr or "unknown"
        # Hold the lock through verification: concurrent attempts cannot pass a
        # shared threshold using stale counts or erase another request's failure.
        with authenticationLock:
            now = time.time()
            for key in [key for key, attempts in loginAttempts.items() if now - attempts[0] >= 60]:
                del loginAttempts[key]
            for key in [key for key, attempts in usernameAttempts.items() if now >= attempts[1]]:
                del usernameAttempts[key]
            attempts = loginAttempts.setdefault(address, [now, 0])
            failures, until = usernameAttempts.get(username, (0, now + 300))
            if attempts[1] >= 5 or failures >= 5:
                return authenticationPage("Unable to sign in. Try again later.", csrfToken=g.browserSession["csrfToken"], status=429)
            attempts[1] += 1
            connection = getDatabase()
            user = connection.execute("SELECT * FROM users WHERE usernameKey = ?", (username,)).fetchone()
            valid = len(password) <= 128 and check_password_hash(user["passwordHash"] if user else dummyPasswordHash, password)
            memberships = connection.execute("SELECT ledgerId, enabled FROM ledgerAccess WHERE userId = ?", (user["id"],)).fetchall() if user else []
            if not user or not valid or len(memberships) != 1 or not memberships[0]["enabled"]:
                failures += 1
                usernameAttempts[username] = (failures, now + 300 if failures == 5 else until)
                return authenticationPage("Invalid username or password.", csrfToken=g.browserSession["csrfToken"], status=401)
            ledgerId = memberships[0]["ledgerId"]
            if not connection.execute("SELECT 1 FROM ledgerAccess WHERE ledgerId = ? AND role = 'owner'", (ledgerId,)).fetchone():
                return authenticationPage("Invalid username or password.", csrfToken=g.browserSession["csrfToken"], status=401)
            token = secrets.token_urlsafe(32)
            with connection:
                connection.execute("UPDATE users SET lastLoginAt = ? WHERE id = ?", (datetime.fromtimestamp(now, timezone.utc).isoformat(), user["id"]))
            sessions.pop(request.cookies.get("npfSession"), None)
            sessions[token] = {"userId": user["id"], "authVersion": user["authVersion"], "expiresAt": now + 168 * 3600, "csrfToken": secrets.token_urlsafe(32)}
            loginAttempts.pop(address, None)
            usernameAttempts.pop(username, None)
        return setSessionCookie(redirect("/"), token, 168 * 3600)

    @app.post("/api/logout")
    def logOut():
        with authenticationLock:
            sessions.pop(request.cookies.get("npfSession"), None)
        response = jsonify({"status": "ok"})
        response.delete_cookie("npfSession", secure=request.is_secure, httponly=True, samesite="Lax")
        return response

    @app.get("/api/session")
    def sessionDetails():
        return {"role": g.user["role"], "username": g.user["username"], "csrfToken": g.browserSession["csrfToken"], "expiresAt": g.browserSession["expiresAt"]}

    @app.after_request
    def disallowIndexing(response):
        if response.mimetype == "text/html":
            response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        if response.mimetype == "text/html" or request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @app.route("/robots.txt", methods=["GET", "HEAD"])
    def robots():
        return Response("User-agent: *\nDisallow: /\n", content_type="text/plain; charset=utf-8")

    def getDatabase():
        if "database" not in g:
            g.database = connectDatabase(app.config["DATABASE_PATH"])
        return g.database

    @app.teardown_appcontext
    def closeDatabase(exception=None):
        connection = g.pop("database", None)
        if connection is not None:
            connection.close()

    @app.errorhandler(ValidationError)
    def validationFailure(error):
        return jsonify({"error": str(error)}), 409 if isinstance(error, SubmissionConflict) else 400

    @app.errorhandler(RequestEntityTooLarge)
    def oversizedSubmission(error):
        if request.endpoint in {"integrationSubmissions", "updateIntegrationPending"} and request.method in {"POST", "PUT"}:
            return jsonify({"error": "Request exceeds the 1 MiB limit"}), 413
        return error

    @app.errorhandler(Exception)
    def unexpectedFailure(error):
        if isinstance(error, HTTPException):
            return error
        app.logger.exception("Unexpected request failure")
        return jsonify({"error": "Unexpected application error"}), 500

    @app.route("/")
    @app.route("/transactions")
    @app.route("/new")
    @app.route("/settings")
    @app.route("/budget")
    @app.route("/simulator")
    @app.route("/pending")
    def pageShell():
        if request.path == "/pending" and g.user["role"] != "owner":
            return jsonify({"error": "Owner access required"}), 403
        return Response((projectDirectory / "index.html").read_text().replace("__APPLICATION_VERSION__", applicationVersion), mimetype="text/html")

    @app.route("/styles.css")
    def styles():
        return send_from_directory(projectDirectory, "styles.css")

    @app.route("/pwa.webmanifest", methods=["GET", "HEAD"])
    def webAppManifest():
        return send_from_directory(projectDirectory, "pwa.webmanifest", mimetype="application/manifest+json")

    @app.route("/theme.js", methods=["GET", "HEAD"])
    def themeJavascript():
        return send_from_directory(projectDirectory, "theme.js", mimetype="text/javascript")

    @app.route("/assets/<path:filename>", methods=["GET", "HEAD"])
    def pwaIcon(filename):
        if filename not in {"icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"}:
            return Response(status=404)
        return send_from_directory(projectDirectory / "assets", filename, mimetype="image/png")

    @app.route("/app.js")
    def javascript():
        return send_from_directory(projectDirectory, "app.js")

    @app.get("/api/backup")
    def downloadBackup():
        with tempfile.TemporaryDirectory(prefix="npf-web-backup-") as temporaryDirectory:
            backupPath = Path(temporaryDirectory) / "finance-backup.db"
            exportLedgerDatabase(getDatabase(), backupPath, disableIntegrationKeysOption())
            backupBytes = backupPath.read_bytes()
        return send_file(
            BytesIO(backupBytes),
            mimetype="application/vnd.sqlite3",
            as_attachment=True,
            download_name=f"finance-backup-{date.today().isoformat()}.db",
        )

    @app.get("/api/package")
    def downloadApplicationPackage():
        return packageDownloadResponse(True)

    @app.get("/api/package-no-db")
    def downloadApplicationPackageWithoutDatabase():
        return packageDownloadResponse(False)

    def disableIntegrationKeysOption():
        if set(request.args) - {"disableIntegrationKeys"}:
            raise ValidationError("Unsupported download option")
        value = request.args.get("disableIntegrationKeys", "false")
        if value not in {"true", "false"}:
            raise ValidationError("disableIntegrationKeys must be true or false")
        return value == "true"

    def packageDownloadResponse(includeDatabase):
        try:
            # Web exports never persist whole-installation snapshots in packages/.
            with tempfile.TemporaryDirectory(prefix="npf-web-package-") as directory:
                exportedPath = Path(directory) / "finance.db"
                if includeDatabase:
                    exportLedgerDatabase(getDatabase(), exportedPath, disableIntegrationKeysOption())
                result = createApplicationPackage(
                    repositoryPath=projectDirectory,
                    databasePath=exportedPath,
                    outputPath=Path(directory) / "packages",
                    includeDatabase=includeDatabase,
                )
                archiveBytes = result["archivePath"].read_bytes()
                archiveName = result["archivePath"].name
        except PackagingError as error:
            app.logger.error("Application package creation failed: %s", error)
            return jsonify({"error": str(error)}), 500
        response = send_file(BytesIO(archiveBytes), mimetype="application/zip", as_attachment=True, download_name=archiveName)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.route("/health")
    def health():
        connection = connectDatabase(app.config["DATABASE_PATH"])
        try:
            connection.execute("SELECT 1").fetchone()
        finally:
            connection.close()
        return {"status": "ok", "schemaVersion": schemaVersion, "applicationVersion": applicationVersion}

    @app.get("/api/application")
    def applicationDetails():
        return {"name": "No Part Finance", "version": applicationVersion}

    @app.post("/api/money/evaluate")
    def evaluateMoneyExpression():
        expression = (request.get_json(silent=True) or {}).get("expression")
        amountCents = parseMoneyCents(expression)
        absoluteCents = abs(amountCents)
        inputValue = f"{absoluteCents // 100:,}.{absoluteCents % 100:02d}"
        if amountCents < 0:
            inputValue = "-" + inputValue
        displayValue = f"-${inputValue[1:]}" if amountCents < 0 else f"${inputValue}"
        return {"amountCents": amountCents, "inputValue": inputValue, "displayValue": displayValue}

    @app.get("/api/accounts")
    def listAccounts():
        connection = getDatabase()
        rows = connection.execute(
            """
            SELECT a.*, a.balanceOffsetCents + COALESCE(SUM(t.amountCents), 0) AS balanceCents
            FROM scopedaccounts AS a
            LEFT JOIN scopedtransactions AS t ON t.accountId = a.id
            GROUP BY a.id
            ORDER BY CASE a.accountClass WHEN 'cash' THEN 0 WHEN 'credit' THEN 1 ELSE 2 END,
                     LOWER(a.name), a.id
            """
        ).fetchall()
        selectorOrder = {row["id"]: index for index, row in enumerate(sorted(rows, key=lambda account: (account["name"].lower(), account["id"])))}
        return jsonify([{**dict(row), "pinnedForSelection": bool(row["pinnedForSelection"]), "selectorOrder": selectorOrder[row["id"]]} for row in rows])

    @app.get("/api/dashboard")
    def getDashboard():
        connection = getDatabase()
        period = request.args.get("period", "thisMonth")
        report = dashboardReport(
            connection, period, request.args.get("startDate"), request.args.get("endDate")
        )
        accountRows = connection.execute(
            """
            SELECT a.*, a.balanceOffsetCents + COALESCE(SUM(t.amountCents), 0) AS balanceCents
            FROM scopedaccounts AS a
            LEFT JOIN scopedtransactions AS t ON t.accountId = a.id
            WHERE a.active = 1 AND a.accountClass IN ('cash', 'credit')
            GROUP BY a.id
            ORDER BY CASE a.accountClass WHEN 'cash' THEN 0 WHEN 'credit' THEN 1 ELSE 2 END,
                     LOWER(a.name), a.id
            """
        ).fetchall()
        report["totalCreditBalanceCents"] = -sum(
            row["balanceCents"] for row in accountRows if row["accountClass"] == "credit"
        )
        eligibleCards = [row for row in accountRows if row["accountClass"] == "credit" and row["accountSubtype"] == "Credit Card" and row["creditLimitCents"]]
        report["totalCreditLimitCents"] = sum(row["creditLimitCents"] for row in eligibleCards)
        report["totalRevolvingOwedCents"] = sum(max(0, -row["balanceCents"]) for row in eligibleCards)
        report["totalUtilization"] = utilizationStatus(
            report["totalRevolvingOwedCents"], report["totalCreditLimitCents"], "Total revolving utilization"
        )
        recentRows = connection.execute(
            """
            SELECT t.*, a.name AS accountName, c.name AS categoryName, c.reportingRole AS categoryRole
            FROM scopedtransactions AS t
            JOIN scopedaccounts AS a ON a.id = t.accountId
            LEFT JOIN scopedcategories AS c ON c.id = t.categoryId
            WHERE t.pinned = 0 OR t.transferId IS NOT NULL
            ORDER BY t.date DESC, t.id DESC
            """
        ).fetchall()
        quickUpdateRows = connection.execute(
            """
            SELECT t.*, a.name AS accountName, c.name AS categoryName, c.reportingRole AS categoryRole
            FROM scopedtransactions AS t
            JOIN scopedaccounts AS a ON a.id = t.accountId
            LEFT JOIN scopedcategories AS c ON c.id = t.categoryId
            WHERE t.pinned = 1 AND t.transferId IS NULL
            ORDER BY t.date DESC, t.id DESC
            """
        ).fetchall()
        report["accounts"] = [dict(row) for row in accountRows]
        for account in report["accounts"]:
            account.update(accountEligibility(account))
            account["utilization"] = (
                utilizationStatus(max(0, -account["balanceCents"]), account["creditLimitCents"], f"{account['name']} utilization")
                if account["accountClass"] == "credit" and account["accountSubtype"] == "Credit Card"
                else None
            )
            account["minimumStatus"] = minimumStatus(
                account["minimumBalanceCents"], account["minimumCautionPercent"], account["balanceCents"]
            )
        report["minimumBalanceAlerts"] = [
            {"accountName": account["name"], **account["minimumStatus"]}
            for account in report["accounts"] if account["minimumStatus"] and account["minimumStatus"]["state"] == "red"
        ]
        report["creditUtilizationAlerts"] = []
        for account in report["accounts"]:
            limit = account["creditLimitCents"]
            warning = account["utilizationWarningBasisPoints"]
            owed = max(0, -account["balanceCents"])
            if (account["accountClass"] == "credit" and account["accountSubtype"] == "Credit Card"
                    and limit and warning and owed > 0 and owed * 10000 >= limit * warning):
                displayedBasisPoints = (owed * 10000 + limit // 2) // limit
                warningLabel = f"{warning // 100}.{warning % 100:02d}".rstrip("0").rstrip(".")
                report["creditUtilizationAlerts"].append({
                    "accountName": account["name"], "balanceCents": owed,
                    "utilizationLabel": f"{displayedBasisPoints // 100}.{displayedBasisPoints % 100:02d}%",
                    "warningLabel": f"{warningLabel}%",
                })
        report["recentTransactions"] = combinedRecentEvents(recentRows)
        report["quickUpdates"] = combinedRecentEvents(quickUpdateRows, limit=len(quickUpdateRows))
        return jsonify(report)

    @app.get("/api/report-periods")
    def reportPeriods():
        previousMonth = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        monthRows = getDatabase().execute(
            """
            SELECT DISTINCT substr(date, 1, 7) AS month
            FROM scopedtransactions
            WHERE substr(date, 1, 7) < ?
            ORDER BY month DESC
            """,
            (previousMonth,),
        ).fetchall()
        return jsonify([row["month"] for row in monthRows])

    @app.post("/api/accounts")
    def addAccount():
        accountRequest = request.get_json(silent=True) or {}
        accountId = createAccount(
            getDatabase(), accountRequest.get("name"), accountRequest.get("accountClass"),
            accountRequest.get("accountSubtype"), accountRequest.get("displayOrder", 0),
            accountRequest.get("openingBalance"),
            accountRequest.get("creditLimit"),
            accountRequest.get("minimumBalance"), accountRequest.get("minimumCautionPercent"),
            accountRequest.get("utilizationWarning"), accountRequest.get("pinnedForSelection", False),
        )
        return jsonify({"id": accountId}), 201

    @app.patch("/api/accounts/<int:accountId>")
    def editAccount(accountId):
        accountRequest = request.get_json(silent=True) or {}
        existingAccount = getDatabase().execute(
            "SELECT displayOrder, pinnedForSelection FROM scopedaccounts WHERE id = ?", (accountId,)
        ).fetchone()
        if not existingAccount:
            raise ValidationError("Account was not found")
        updateAccount(
            getDatabase(), accountId, accountRequest.get("name"),
            accountRequest.get("accountClass"), accountRequest.get("accountSubtype"),
            existingAccount["displayOrder"], accountRequest.get("active", True),
            accountRequest.get("creditLimit"),
            accountRequest.get("minimumBalance"), accountRequest.get("minimumCautionPercent"),
            accountRequest.get("utilizationWarning"), accountRequest.get("pinnedForSelection", bool(existingAccount["pinnedForSelection"])),
        )
        return jsonify({"status": "ok"})

    @app.post("/api/accounts/minimum-helper")
    def accountMinimumHelper():
        accountRequest = request.get_json(silent=True) or {}
        from database import cashMinimumValues
        minimumBalanceCents, cautionPercent = cashMinimumValues(
            "cash", accountRequest.get("minimumBalance"), accountRequest.get("minimumCautionPercent")
        )
        if minimumBalanceCents is None:
            return jsonify({"minimumBalanceCents": None, "minimumCautionPercent": None, "lines": []})
        status = minimumStatus(minimumBalanceCents, cautionPercent, minimumBalanceCents)
        green = status["greenThresholdCents"]
        if cautionPercent == 0:
            lines = [f"Green: ${green // 100:,}.{green % 100:02d} and above", "Exact minimum: At minimum", f"Red: ${(minimumBalanceCents - 1) // 100:,}.{(minimumBalanceCents - 1) % 100:02d} and below"]
        else:
            lines = [f"Green: ${green // 100:,}.{green % 100:02d} and above", f"Yellow: ${minimumBalanceCents // 100:,}.{minimumBalanceCents % 100:02d} through ${(green - 1) // 100:,}.{(green - 1) % 100:02d}", f"Red: ${(minimumBalanceCents - 1) // 100:,}.{(minimumBalanceCents - 1) % 100:02d} and below"]
        return jsonify({"minimumBalanceCents": minimumBalanceCents, "minimumCautionPercent": cautionPercent, "lines": lines})

    @app.post("/api/accounts/utilization-warning-helper")
    def accountUtilizationWarningHelper():
        accountRequest = request.get_json(silent=True) or {}
        warningBalanceCents = utilizationWarningHelper(
            accountRequest.get("creditLimit"), accountRequest.get("utilizationWarning")
        )
        return jsonify({
            "warningBalanceCents": warningBalanceCents,
            "text": "" if warningBalanceCents is None else
                f"Warning begins at a ${warningBalanceCents // 100:,}.{warningBalanceCents % 100:02d} balance.",
        })

    @app.post("/api/accounts/<int:accountId>/reconciliation")
    def reconcileBalance(accountId):
        reconciliationRequest = request.get_json(silent=True) or {}
        balanceOffsetCents, differenceCents = reconcileAccountBalance(
            getDatabase(), accountId, reconciliationRequest.get("actualBalance"),
        )
        return jsonify({
            "balanceOffsetCents": balanceOffsetCents,
            "differenceCents": differenceCents,
            "balanceCents": getDatabase().execute(
                "SELECT balanceOffsetCents + COALESCE((SELECT SUM(amountCents) FROM scopedtransactions WHERE accountId = scopedaccounts.id), 0) FROM scopedaccounts WHERE id = ?",
                (accountId,),
            ).fetchone()[0],
        })

    @app.get("/api/categories")
    def listCategories():
        rows = getDatabase().execute(
            """
            SELECT c.*, EXISTS(
                SELECT 1 FROM scopedcategories AS child WHERE child.parentCategoryId = c.id
            ) AS hasChildren
            FROM scopedcategories AS c
            ORDER BY c.displayOrder, c.name
            """
        ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.post("/api/categories")
    def addCategory():
        categoryRequest = request.get_json(silent=True) or {}
        categoryId = createCategory(
            getDatabase(), categoryRequest.get("name"), categoryRequest.get("parentCategoryId"),
            categoryRequest.get("displayOrder", 0),
            categoryRequest.get("offerReimbursement", False),
            categoryRequest.get("reportingRole", "expense"),
        )
        return jsonify({"id": categoryId}), 201

    @app.patch("/api/categories/<int:categoryId>")
    def editCategory(categoryId):
        categoryRequest = request.get_json(silent=True) or {}
        updateCategory(
            getDatabase(), categoryId, categoryRequest.get("name"),
            categoryRequest.get("parentCategoryId"), categoryRequest.get("displayOrder", 0),
            categoryRequest.get("active", True),
            categoryRequest.get("offerReimbursement", False),
            categoryRequest.get("reportingRole", "expense"),
        )
        return jsonify({"status": "ok"})

    @app.delete("/api/categories/<int:categoryId>")
    def removeCategory(categoryId):
        deleteCategory(getDatabase(), categoryId)
        return jsonify({"status": "ok"})

    @app.get("/api/settings")
    def listSettings():
        rows = getDatabase().execute("SELECT key, value FROM scopedapplicationSettings").fetchall()
        settings = {row["key"]: row["value"] for row in rows}
        settings.setdefault("integrationTransferDefaultDescription", "0")
        settings.setdefault("monthlySuggestionWindowDays", "3")
        settings.setdefault("quarterlySuggestionWindowMonths", "1")
        settings.setdefault("annualSuggestionWindowMonths", "3")
        return jsonify(settings)

    @app.put("/api/settings/integration-transfers")
    def saveIntegrationTransferSettings():
        settingsRequest = request.get_json(silent=True)
        if not isinstance(settingsRequest, dict) or set(settingsRequest) != {"useDefaultDescription"} or type(settingsRequest["useDefaultDescription"]) is not bool:
            raise ValidationError("Submit useDefaultDescription as a Boolean")
        connection = getDatabase()
        with connection:
            connection.execute("INSERT INTO applicationSettings (key, value) VALUES ('integrationTransferDefaultDescription', ?) ON CONFLICT(ledgerId, key) DO UPDATE SET value = excluded.value", ("1" if settingsRequest["useDefaultDescription"] else "0",))
        return jsonify({"useDefaultDescription": settingsRequest["useDefaultDescription"]})

    @app.get("/api/simulator")
    def getSimulator():
        rawPlanId = request.args.get("plan")
        try:
            planId = int(rawPlanId) if rawPlanId else None
        except ValueError:
            planId = None
        return jsonify(simulatorWorkspace(getDatabase(), planId, repairMissingLinks=bool(g.user and g.user["role"] == "owner")))

    @app.get("/api/simulator/send-preview")
    def simulatorSendPreview():
        transactionId = request.args.get("transactionId")
        transferId = request.args.get("transferId")
        try:
            transactionId = int(transactionId) if transactionId else None
        except ValueError as error:
            raise ValidationError("Transaction does not exist") from error
        mapping = simulatorLedgerMapping(getDatabase(), transactionId, transferId)
        plans = getDatabase().execute(
            "SELECT id, name, planDate FROM scopedsimulatorPlans ORDER BY planDate, id"
        ).fetchall()
        return jsonify({"preview": mapping, "plans": [dict(plan) for plan in plans]})

    @app.post("/api/simulator/send")
    def sendTransactionToSimulator():
        sendRequest = request.get_json(silent=True) or {}
        try:
            planId = int(sendRequest.get("planId"))
            transactionId = int(sendRequest["transactionId"]) if sendRequest.get("transactionId") else None
        except (TypeError, ValueError) as error:
            raise ValidationError("Simulator plan is required") from error
        rowId, planName = sendLedgerEventToSimulator(
            getDatabase(), planId, transactionId, sendRequest.get("transferId") or None
        )
        return jsonify({"id": rowId, "planName": planName}), 201

    @app.post("/api/simulator/plans")
    def addSimulatorPlan():
        planRequest = request.get_json(silent=True) or {}
        duplicatePlanId = planRequest.get("duplicatePlanId")
        if duplicatePlanId not in (None, ""):
            try:
                duplicatePlanId = int(duplicatePlanId)
            except (TypeError, ValueError) as error:
                raise ValidationError("Source plan does not exist") from error
        planId = createSimulatorPlan(getDatabase(), planRequest.get("name"), planRequest.get("planDate"), duplicatePlanId)
        return jsonify({"id": planId}), 201

    @app.patch("/api/simulator/plans/<int:planId>")
    def editSimulatorPlan(planId):
        planRequest = request.get_json(silent=True) or {}
        updateSimulatorPlan(getDatabase(), planId, planRequest.get("name"), planRequest.get("planDate"))
        return jsonify({"status": "ok"})

    @app.delete("/api/simulator/plans/<int:planId>")
    def removeSimulatorPlan(planId):
        deleteSimulatorPlan(getDatabase(), planId)
        return jsonify({"status": "ok"})

    @app.post("/api/simulator/plans/<int:planId>/rows")
    def addSimulatorRow(planId):
        rowRequest = request.get_json(silent=True) or {}
        rowId = saveSimulatorRow(getDatabase(), planId, None, rowRequest.get("rowType"),
            rowRequest.get("fromAccountId"), rowRequest.get("toAccountId"), rowRequest.get("categoryId"),
            rowRequest.get("label"), rowRequest.get("amount"))
        return jsonify({"id": rowId}), 201

    @app.patch("/api/simulator/plans/<int:planId>/rows/<int:rowId>")
    def editSimulatorRow(planId, rowId):
        rowRequest = request.get_json(silent=True) or {}
        saveSimulatorRow(getDatabase(), planId, rowId, rowRequest.get("rowType"),
            rowRequest.get("fromAccountId"), rowRequest.get("toAccountId"), rowRequest.get("categoryId"),
            rowRequest.get("label"), rowRequest.get("amount"))
        return jsonify({"status": "ok"})

    @app.delete("/api/simulator/plans/<int:planId>/rows/<int:rowId>")
    def removeSimulatorRow(planId, rowId):
        deleteSimulatorRow(getDatabase(), planId, rowId)
        return jsonify({"status": "ok"})

    @app.delete("/api/simulator/plans/<int:planId>/executed-rows")
    def removeExecutedSimulatorRows(planId):
        cleanupRequest = request.get_json(silent=True) or {}
        planName, deletedCount = deleteExecutedSimulatorRows(
            getDatabase(), planId, cleanupRequest.get("expectedCount")
        )
        return jsonify({"status": "ok", "planName": planName, "deletedCount": deletedCount})

    @app.patch("/api/simulator/plans/<int:planId>/rows/<int:rowId>/execution")
    def editSimulatorExecution(planId, rowId):
        executionRequest = request.get_json(silent=True) or {}
        setSimulatorRowExecuted(getDatabase(), planId, rowId, bool(executionRequest.get("executed")))
        return jsonify({"status": "ok"})

    @app.post("/api/simulator/plans/<int:planId>/rows/<int:rowId>/transaction")
    def recordSimulatorOrdinaryTransaction(planId, rowId):
        transactionRequest = request.get_json(silent=True) or {}
        transactionId = recordSimulatorTransaction(
            getDatabase(), planId, rowId, transactionRequest.get("accountId"),
            transactionRequest.get("date"), transactionRequest.get("description"),
            transactionRequest.get("categoryId"), transactionRequest.get("amount"),
            transactionRequest.get("pinned", False),
        )
        return jsonify({"id": transactionId}), 201

    @app.post("/api/simulator/plans/<int:planId>/rows/<int:rowId>/transfer")
    def recordSimulatorPaymentOrTransfer(planId, rowId):
        transferRequest = request.get_json(silent=True) or {}
        transferId, transactionId = recordSimulatorTransfer(
            getDatabase(), planId, rowId, transferRequest.get("date"),
            transferRequest.get("fromAccountId"), transferRequest.get("toAccountId"),
            transferRequest.get("amount"), transferRequest.get("description", "Transfer"),
        )
        return jsonify({"transferId": transferId, "transactionId": transactionId}), 201

    @app.get("/api/system/database-status")
    def databaseStatus():
        connection = getDatabase()
        transactionCount = connection.execute("SELECT COUNT(*) FROM scopedtransactions").fetchone()[0]
        accountCount = connection.execute("SELECT COUNT(*) FROM scopedaccounts").fetchone()[0]
        databaseSchemaVersion = connection.execute("PRAGMA user_version").fetchone()[0]
        latestChange = connection.execute("SELECT MAX(changedAt) FROM auditEvents WHERE ledgerId = currentLedgerId()").fetchone()[0]
        modifiedTime = datetime.fromisoformat(latestChange.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if latestChange else "Not recorded"
        return jsonify({
            "transactionCount": transactionCount,
            "accountCount": accountCount,
            "schemaVersion": databaseSchemaVersion,
            "modifiedLocal": modifiedTime,
        })

    @app.put("/api/settings/reimbursement-category")
    def setReimbursementCategory():
        categoryId = (request.get_json(silent=True) or {}).get("categoryId")
        category = getDatabase().execute(
            """SELECT c.* FROM scopedcategories c WHERE c.id = ? AND c.active = 1
               AND c.reportingRole = 'income' AND c.parentCategoryId IS NULL
               AND NOT EXISTS (SELECT 1 FROM scopedcategories child WHERE child.parentCategoryId = c.id)""",
            (categoryId,),
        ).fetchone()
        if not category:
            raise ValidationError("Reimbursement Category must be an active standalone Income category")
        with getDatabase():
            getDatabase().execute(
                "INSERT INTO applicationSettings (key, value) VALUES ('reimbursementCategoryId', ?) ON CONFLICT(ledgerId, key) DO UPDATE SET value = excluded.value", (str(categoryId),),
            )
        return jsonify({"status": "ok"})

    @app.get("/api/transactions")
    def listTransactions():
        period = request.args.get("period", "last30Days")
        today = date.today()
        if period != "all":
            periodStart, periodEnd, _ = reportingPeriod(
                period, request.args.get("startDate"), request.args.get("endDate"), today
            )
        accountId = request.args.get("accountId")
        categoryId = request.args.get("categoryId")
        description = request.args.get("description", "").strip()
        transactionType = request.args.get("type", "all")
        if transactionType not in {"all", "transactions", "transfers"}:
            raise ValidationError("Transaction type is invalid")
        sort = request.args.get("sort", "date")
        direction = request.args.get("direction", "desc")
        if sort not in {"date", "account", "description", "category", "amount"} or direction not in {"asc", "desc"}:
            raise ValidationError("Transaction sort is invalid")
        rows = getDatabase().execute(
            """
            SELECT t.*, a.name AS accountName, a.accountClass, c.name AS categoryName, c.reportingRole AS categoryRole
            FROM scopedtransactions AS t
            JOIN scopedaccounts AS a ON a.id = t.accountId
            LEFT JOIN scopedcategories AS c ON c.id = t.categoryId
            ORDER BY t.date DESC, t.id DESC
            """
        ).fetchall()
        transactions = []
        for row in rows:
            transaction = dict(row)
            transaction["enteredAmountCents"] = (
                -row["amountCents"] if row["categoryRole"] == "expense" else row["amountCents"]
            )
            transactions.append(transaction)
        # Group the full ledger before any filter so either Account side retains
        # the complete transfer and malformed linked history cannot be disguised.
        events = combinedRecentEvents(transactions, limit=len(transactions))
        if period != "all":
            events = [event for event in events if periodStart <= event["date"] <= periodEnd]
        if transactionType != "all":
            events = [event for event in events if bool(event["transferId"]) == (transactionType == "transfers")]
        if accountId:
            try:
                selectedAccountId = int(accountId)
            except ValueError as error:
                raise ValidationError("Account ID must be an integer") from error
            events = [event for event in events if selectedAccountId in {event["accountId"], event.get("toAccountId")}]
        if categoryId:
            try:
                selectedCategoryId = int(categoryId)
            except ValueError as error:
                raise ValidationError("Category ID must be an integer") from error
            events = [event for event in events if not event["transferId"] and event["categoryId"] == selectedCategoryId]
        if description:
            events = [event for event in events if description.casefold() in event["description"].casefold()]

        def eventSortValue(event):
            if sort == "amount":
                return event["displayAmountCents"]
            if sort == "account":
                return (event.get("fromAccountName", event["accountName"]).casefold(), event.get("toAccountName", "").casefold())
            if sort == "category":
                return (event["categoryName"] or "").casefold()
            return event[sort].casefold()

        events.sort(key=lambda event: (eventSortValue(event), event["id"]), reverse=direction == "desc")
        if sort == "category":
            events.sort(key=lambda event: event["categoryName"] is None)
        amountMode = request.args.get("amountMode", "any")
        if amountMode not in {"any", "exact", "lessThan", "greaterThan", "between", "withinDollar"}:
            raise ValidationError("Amount filter mode is invalid")
        if amountMode != "any":
            def filterAmount(parameterName):
                amountCents = parseMoneyCents(request.args.get(parameterName))
                if amountCents < 0:
                    raise ValidationError("Amount filter must not be negative")
                return amountCents

            if amountMode == "between":
                minimumCents = filterAmount("amountMin")
                maximumCents = filterAmount("amountMax")
                if minimumCents > maximumCents:
                    raise ValidationError("Minimum amount must not exceed maximum amount")
                events = [event for event in events if minimumCents <= abs(event["amountCents"]) <= maximumCents]
            else:
                amountCents = filterAmount("amount")
                if amountMode == "withinDollar" and amountCents % 100:
                    raise ValidationError("Dollar range amount must be a whole dollar")
                comparisons = {
                    "exact": lambda value: value == amountCents,
                    "lessThan": lambda value: value < amountCents,
                    "greaterThan": lambda value: value > amountCents,
                    "withinDollar": lambda value: amountCents <= value <= amountCents + 99,
                }
                events = [event for event in events if comparisons[amountMode](abs(event["amountCents"]))]
        for event in events:
            try:
                simulatorLedgerMapping(
                    getDatabase(), None if event["transferId"] else event["id"], event["transferId"]
                )
                event["simulatorEligible"] = True
            except ValidationError:
                event["simulatorEligible"] = False
        if request.args.get("includeSummary") == "true":
            return jsonify({
                "transactions": events,
                "summary": {
                    "expenseCents": sum(
                        event["displayAmountCents"] for event in events
                        if not event["transferId"] and event["categoryRole"] == "expense"
                    ),
                    "incomeCents": sum(
                        event["displayAmountCents"] for event in events
                        if not event["transferId"] and event["categoryRole"] == "income"
                    ),
                    "transferPaymentCents": sum(
                        event["displayAmountCents"] for event in events if event["combinedTransfer"]
                    ),
                },
            })
        return jsonify(events)

    @app.post("/api/transactions")
    def addTransaction():
        transactionRequest = request.get_json(silent=True) or {}
        if transactionRequest.get("alsoLogReimbursement"):
            reimbursement = transactionRequest.get("reimbursement") or {}
            transactionId, reimbursementId = createTransactionWithReimbursement(
                getDatabase(), transactionRequest.get("accountId"), transactionRequest.get("date"),
                transactionRequest.get("description"), transactionRequest.get("categoryId"),
                transactionRequest.get("amount"), transactionRequest.get("pinned", False),
                reimbursement.get("accountId"), reimbursement.get("date"),
                reimbursement.get("description"), reimbursement.get("amount"),
            )
            return jsonify({"id": transactionId, "reimbursementId": reimbursementId}), 201
        transactionId = createTransaction(
            getDatabase(), transactionRequest.get("accountId"), transactionRequest.get("date"),
            transactionRequest.get("description"), transactionRequest.get("categoryId"),
            transactionRequest.get("amount"), transactionRequest.get("pinned", False),
        )
        return jsonify({"id": transactionId}), 201

    @app.post("/api/transactions/bulk")
    def addBulkTransactions():
        bulkRequest = request.get_json(silent=True) or {}
        createdCount = createBulkTransactions(getDatabase(), bulkRequest.get("rows"))
        return jsonify({"createdCount": createdCount}), 201

    @app.patch("/api/transactions/<int:transactionId>")
    def editTransaction(transactionId):
        transactionRequest = request.get_json(silent=True) or {}
        updateTransaction(
            getDatabase(), transactionId, transactionRequest.get("accountId"),
            transactionRequest.get("date"), transactionRequest.get("description"),
            transactionRequest.get("categoryId"), transactionRequest.get("amount"),
            transactionRequest.get("pinned", False),
        )
        return jsonify({"status": "ok"})

    @app.delete("/api/transactions/<int:transactionId>")
    def removeTransaction(transactionId):
        deleteTransaction(getDatabase(), transactionId)
        return jsonify({"status": "ok"})

    @app.post("/api/transfers")
    def addTransfer():
        transferRequest = request.get_json(silent=True) or {}
        transferId = createTransfer(
            getDatabase(), transferRequest.get("date"), transferRequest.get("fromAccountId"),
            transferRequest.get("toAccountId"), transferRequest.get("amount"),
            transferRequest.get("description", "Transfer"),
        )
        return jsonify({"transferId": transferId}), 201

    @app.get("/api/transfer-amount-helper")
    def transferAmountHelper():
        if request.args.get("editing") == "true":
            return jsonify({"eligible": False})
        connection = getDatabase()
        try:
            fromId = int(request.args.get("fromAccountId", ""))
            toId = int(request.args.get("toAccountId", ""))
        except ValueError:
            return jsonify({"eligible": False})
        accountRows = connection.execute(
            """SELECT a.*, a.balanceOffsetCents + COALESCE(SUM(t.amountCents), 0) balanceCents
               FROM scopedaccounts a LEFT JOIN scopedtransactions t ON t.accountId = a.id
               WHERE a.id IN (?, ?) AND a.active = 1 GROUP BY a.id""", (fromId, toId)
        ).fetchall()
        accountMap = {row["id"]: row for row in accountRows}
        source, destination = accountMap.get(fromId), accountMap.get(toId)
        if not source or not destination or source["accountClass"] != "cash":
            return jsonify({"eligible": False})
        if destination["accountClass"] == "credit" and destination["accountSubtype"] in {"Credit Card", "Personal Loan"}:
            amountCents = max(0, -destination["balanceCents"])
            if amountCents and source["balanceCents"] >= amountCents:
                return jsonify({"eligible": True, "amountCents": amountCents,
                                "label": f"Use ${amountCents // 100:,}.{amountCents % 100:02d} (Payoff)", "kind": "payoff"})
        if destination["accountClass"] == "cash":
            amountCents = sinkingFundSummary(connection)["totalPerPaycheckContributionCents"]
            if amountCents and amountCents > 0:
                return jsonify({"eligible": True, "amountCents": amountCents,
                                "label": f"Use ${amountCents // 100:,}.{amountCents % 100:02d} (Sinking Fund per paycheck)", "kind": "sinkingFund"})
        return jsonify({"eligible": False})

    @app.get("/api/transfers/<transferId>")
    def getTransfer(transferId):
        rows = getDatabase().execute(
            "SELECT * FROM scopedtransactions WHERE transferId = ? ORDER BY amountCents", (transferId,)
        ).fetchall()
        if len(rows) != 2 or sum(row["amountCents"] for row in rows) != 0:
            raise ValidationError("Transfer is incomplete or invalid")
        fromRow, toRow = rows
        return jsonify({
            "transferId": transferId,
            "date": fromRow["date"],
            "description": fromRow["description"],
            "amountCents": -fromRow["amountCents"],
            "fromAccountId": fromRow["accountId"],
            "toAccountId": toRow["accountId"],
        })

    @app.patch("/api/transfers/<transferId>")
    def editTransfer(transferId):
        transferRequest = request.get_json(silent=True) or {}
        updateTransfer(
            getDatabase(), transferId, transferRequest.get("date"),
            transferRequest.get("fromAccountId"), transferRequest.get("toAccountId"),
            transferRequest.get("amount"), transferRequest.get("description", "Transfer"),
        )
        return jsonify({"status": "ok"})

    @app.delete("/api/transfers/<transferId>")
    def removeTransfer(transferId):
        row = getDatabase().execute(
            "SELECT id FROM scopedtransactions WHERE transferId = ? ORDER BY amountCents LIMIT 1", (transferId,)
        ).fetchone()
        if not row:
            raise ValidationError("Transfer does not exist")
        deleteTransaction(getDatabase(), row["id"])
        return jsonify({"status": "ok"})

    @app.get("/api/templates")
    def listTemplates():
        rows = getDatabase().execute(
            """
            SELECT tt.*, a.name AS accountName, c.name AS categoryName
            FROM scopedtransactionTemplates AS tt
            LEFT JOIN scopedaccounts AS a ON a.id = tt.accountId
            LEFT JOIN scopedcategories AS c ON c.id = tt.categoryId
            ORDER BY tt.displayOrder, tt.name
            """
        ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.post("/api/templates")
    def addTemplate():
        templateRequest = request.get_json(silent=True) or {}
        templateId = createTemplate(
            getDatabase(), templateRequest.get("name"), templateRequest.get("accountId"),
            templateRequest.get("description"), templateRequest.get("categoryId"),
            templateRequest.get("amount"), templateRequest.get("displayOrder", 0),
            templateRequest.get("prefillAmount", True),
        )
        return jsonify({"id": templateId}), 201

    @app.patch("/api/templates/<int:templateId>")
    def editTemplate(templateId):
        templateRequest = request.get_json(silent=True) or {}
        updateTemplate(
            getDatabase(), templateId, templateRequest.get("name"),
            templateRequest.get("accountId"), templateRequest.get("description"),
            templateRequest.get("categoryId"), templateRequest.get("amount"),
            templateRequest.get("displayOrder", 0), templateRequest.get("active", True),
            templateRequest.get("prefillAmount", True),
        )
        return jsonify({"status": "ok"})

    @app.delete("/api/templates/<int:templateId>")
    def removeTemplate(templateId):
        deleteTemplate(getDatabase(), templateId)
        return jsonify({"status": "ok"})

    @app.get("/api/budget")
    def getBudget():
        connection = getDatabase()
        budgetSummary = sinkingFundSummary(connection)
        selectedDate = request.args.get("date")
        if selectedDate:
            from database import validateDate
            validateDate(selectedDate)
            selectedDate = datetime.strptime(selectedDate, "%Y-%m-%d").date()
            selectedDateSummary = sinkingFundSummary(connection, selectedDate)
            selectedPlans = {plan["id"]: plan for plan in selectedDateSummary["plans"]}
            for plan in budgetSummary["plans"]:
                if plan["frequency"] != "monthly":
                    selectedPlan = selectedPlans[plan["id"]]
                    plan["reserveTargetCents"] = selectedPlan["reserveTargetCents"]
                    plan["upcomingExpectedDate"] = selectedPlan["upcomingExpectedDate"]
            budgetSummary["totalReserveTargetCents"] = selectedDateSummary["totalReserveTargetCents"]
        return jsonify(budgetSummary)

    @app.get("/api/budget/template-suggestions")
    def getBudgetTemplateSuggestions():
        return jsonify(budgetTemplateSuggestions(getDatabase(), request.args.get("date")))

    @app.put("/api/settings/paychecks-per-year")
    def savePaychecksPerYear():
        paychecksPerYear = setPaychecksPerYear(
            getDatabase(), (request.get_json(silent=True) or {}).get("paychecksPerYear")
        )
        return jsonify({"paychecksPerYear": paychecksPerYear})

    @app.put("/api/settings/suggestion-rounding")
    def saveSuggestionRounding():
        roundingCents = setSuggestionRounding(
            getDatabase(), (request.get_json(silent=True) or {}).get("amount")
        )
        return jsonify({"suggestionRoundingCents": roundingCents})

    @app.put("/api/settings/budget-suggestion-windows")
    def saveBudgetSuggestionWindows():
        settingsRequest = request.get_json(silent=True) or {}
        return jsonify(setBudgetSuggestionWindows(
            getDatabase(), settingsRequest.get("monthlyDays"),
            settingsRequest.get("quarterlyMonths"), settingsRequest.get("annualMonths"),
        ))

    @app.post("/api/flexible-budgets")
    def addFlexibleBudget():
        budgetRequest = request.get_json(silent=True) or {}
        budgetId = createFlexibleCategoryBudget(
            getDatabase(), budgetRequest.get("categoryId"), budgetRequest.get("monthlyTarget")
        )
        return jsonify({"id": budgetId}), 201

    @app.patch("/api/flexible-budgets/<int:budgetId>")
    def editFlexibleBudget(budgetId):
        budgetRequest = request.get_json(silent=True) or {}
        updateFlexibleCategoryBudget(
            getDatabase(), budgetId, budgetRequest.get("categoryId"),
            budgetRequest.get("monthlyTarget"), budgetRequest.get("active", True),
        )
        return jsonify({"status": "ok"})

    @app.delete("/api/flexible-budgets/<int:budgetId>")
    def removeFlexibleBudget(budgetId):
        deleteFlexibleCategoryBudget(getDatabase(), budgetId)
        return jsonify({"status": "ok"})

    @app.post("/api/expected-income-plans")
    def addExpectedIncome():
        planRequest = request.get_json(silent=True) or {}
        planId = createExpectedIncomePlan(
            getDatabase(), planRequest.get("templateId"), planRequest.get("expectedDate")
        )
        return jsonify({"id": planId}), 201

    @app.patch("/api/expected-income-plans/<int:planId>")
    def editExpectedIncome(planId):
        planRequest = request.get_json(silent=True) or {}
        updateExpectedIncomePlan(
            getDatabase(), planId, planRequest.get("templateId"), planRequest.get("expectedDate"),
            planRequest.get("active", True),
        )
        return jsonify({"status": "ok"})

    @app.delete("/api/expected-income-plans/<int:planId>")
    def removeExpectedIncome(planId):
        deleteExpectedIncomePlan(getDatabase(), planId)
        return jsonify({"status": "ok"})

    @app.post("/api/planned-credit-payments")
    def addPlannedPayment():
        planRequest = request.get_json(silent=True) or {}
        planId = createPlannedCreditPayment(
            getDatabase(), planRequest.get("expectedDate"), planRequest.get("accountId"),
            planRequest.get("description"), planRequest.get("amount"),
        )
        return jsonify({"id": planId}), 201

    @app.patch("/api/planned-credit-payments/<int:planId>")
    def editPlannedPayment(planId):
        planRequest = request.get_json(silent=True) or {}
        updatePlannedCreditPayment(
            getDatabase(), planId, planRequest.get("expectedDate"), planRequest.get("accountId"),
            planRequest.get("description"), planRequest.get("amount"), planRequest.get("active", True),
        )
        return jsonify({"status": "ok"})

    @app.delete("/api/planned-credit-payments/<int:planId>")
    def removePlannedPayment(planId):
        deletePlannedCreditPayment(getDatabase(), planId)
        return jsonify({"status": "ok"})

    @app.post("/api/sinking-funds")
    def addSinkingFund():
        planRequest = request.get_json(silent=True) or {}
        planId = createSinkingFundPlan(
            getDatabase(), planRequest.get("templateId"), planRequest.get("frequency"),
            planRequest.get("expectedDate"),
        )
        return jsonify({"id": planId}), 201

    @app.patch("/api/sinking-funds/<int:planId>")
    def editSinkingFund(planId):
        planRequest = request.get_json(silent=True) or {}
        updateSinkingFundPlan(
            getDatabase(), planId, planRequest.get("templateId"), planRequest.get("frequency"),
            planRequest.get("expectedDate"), planRequest.get("active", True),
        )
        return jsonify({"status": "ok"})

    @app.delete("/api/sinking-funds/<int:planId>")
    def removeSinkingFund(planId):
        deleteSinkingFundPlan(getDatabase(), planId)
        return jsonify({"status": "ok"})

    @app.get("/docs")
    def apiDocumentation():
        return send_from_directory(projectDirectory, "docs.html")

    @app.get("/openapi.json")
    def openApiDocument():
        document = json.loads((projectDirectory / "openapi.json").read_text())
        document["info"]["version"] = applicationVersion
        return jsonify(document)

    @app.route("/api/integration-secrets", methods=["GET", "POST"])
    def integrationSecrets():
        connection = getDatabase()
        if request.method == "POST":
            secretRequest = request.get_json(silent=True)
            if not isinstance(secretRequest, dict) or not isinstance(secretRequest.get("label"), str):
                raise ValidationError("Label must be text")
            secretId = createIntegrationSecret(connection, secretRequest["label"])
            return jsonify({"id": secretId}), 201
        return jsonify([dict(row) for row in connection.execute("SELECT id, label, active, revoked, CASE WHEN revoked = 1 THEN 'Revoked' WHEN active = 1 THEN 'Enabled' ELSE 'Disabled' END AS status FROM scopedintegrationSecrets ORDER BY id")])

    @app.patch("/api/integration-secrets/<int:secretId>")
    def editIntegrationLabel(secretId):
        secretRequest = request.get_json(silent=True)
        if not isinstance(secretRequest, dict) or set(secretRequest) != {"label"}:
            raise ValidationError("Submit only the integration label")
        updateIntegrationLabel(getDatabase(), secretId, secretRequest["label"])
        return jsonify({"status": "ok"})

    @app.post("/api/integration-secrets/<int:secretId>/reveal")
    def revealIntegrationSecret(secretId):
        secret = getDatabase().execute("SELECT secret FROM scopedintegrationSecrets WHERE id = ?", (secretId,)).fetchone()
        if not secret:
            raise ValidationError("Integration secret does not exist")
        return jsonify({"secret": secret["secret"]})

    @app.delete("/api/integration-secrets/<int:secretId>")
    def revokeIntegrationSecret(secretId):
        connection = getDatabase()
        with connection:
            if not connection.execute("UPDATE integrationSecrets SET active = 0, revoked = 1 WHERE ledgerId = currentLedgerId() AND (id = ?)", (secretId,)).rowcount:
                raise ValidationError("Integration secret does not exist")
        return jsonify({"status": "ok"})

    @app.post("/api/integration-secrets/<int:secretId>/state")
    def changeIntegrationState(secretId):
        submitted = request.get_json(silent=True)
        if not isinstance(submitted, dict) or set(submitted) != {"enabled"} or type(submitted["enabled"]) is not bool:
            raise ValidationError("Submit enabled as a Boolean")
        setIntegrationState(getDatabase(), secretId, submitted["enabled"])
        return jsonify({"status": "ok"})

    @app.get("/api/pending")
    def reviewPending():
        rows = pendingReviewRows(getDatabase())
        return jsonify({**pendingCounts(rows), "rows": rows})

    @app.patch("/api/pending/<int:proposalId>")
    def savePendingRow(proposalId):
        nextVersion = savePendingReview(getDatabase(), proposalId, request.get_json(silent=True))
        return jsonify({"reviewVersion": nextVersion})

    @app.post("/api/pending/summary")
    def summarizePending():
        submitted = request.get_json(silent=True)
        if not isinstance(submitted, dict) or set(submitted) != {"rows"}:
            raise ValidationError("Submit summary rows only")
        summary = pendingReviewSummary(submitted["rows"])
        for kind in ("transaction", "transfer"):
            amountCents = summary[f"{kind}Cents"]
            # Format integer cents here so even large sums display without float rounding.
            summary[f"{kind}Display"] = None if amountCents is None else (
                ("-" if amountCents < 0 else "")
                + f"${abs(amountCents) // 100:,}.{abs(amountCents) % 100:02d}"
            )
        return jsonify(summary)

    @app.post("/api/pending/preview")
    def previewPending():
        row = request.get_json(silent=True)
        if not isinstance(row, dict):
            raise ValidationError("Review fields must be an object")
        kind = row.get("kind", "transaction")
        if kind not in {"transaction", "transfer"}:
            raise ValidationError("Invalid proposal kind")
        values, role = preparePending(getDatabase(), kind, row)
        return jsonify({"ledgerAmountCents": values[3] if kind == "transaction" else None, "reviewedRole": role, "valid": True})

    @app.post("/api/pending/manual")
    def createManualPending():
        proposal, created = submitPendingEntry(getDatabase(), None, request.get_json(silent=True))
        return jsonify(statusResult(proposal)), 201 if created else 200

    @app.get("/api/transactions/<int:transactionId>/pending-snapshot")
    def transactionPendingSnapshot(transactionId):
        if g.user["role"] != "owner":
            return jsonify({"error": "Owner access required"}), 403
        return jsonify({"snapshot": ledgerMoveSnapshot(getDatabase(), transactionId=transactionId)})

    @app.get("/api/transfers/<transferId>/pending-snapshot")
    def transferPendingSnapshot(transferId):
        if g.user["role"] != "owner":
            return jsonify({"error": "Owner access required"}), 403
        return jsonify({"snapshot": ledgerMoveSnapshot(getDatabase(), transferId=transferId)})

    def movePendingRequest(transactionId=None, transferId=None):
        moveRequest = request.get_json(silent=True) or {}
        if set(moveRequest) != {"requestId", "snapshot"}:
            raise ValidationError("Move requires requestId and snapshot")
        proposal, created = moveLedgerToPending(getDatabase(), moveRequest["requestId"], moveRequest["snapshot"], transactionId, transferId)
        return jsonify(statusResult(proposal)), 201 if created else 200

    @app.post("/api/transactions/<int:transactionId>/move-to-pending")
    def moveTransactionPending(transactionId):
        return movePendingRequest(transactionId=transactionId)

    @app.post("/api/transfers/<transferId>/move-to-pending")
    def moveTransferPending(transferId):
        return movePendingRequest(transferId=transferId)

    @app.post("/api/pending/decision")
    def decidePending():
        decision = request.get_json(silent=True)
        if not isinstance(decision, dict):
            raise ValidationError("Decision must be an object")
        count = decidePendingTransactions(getDatabase(), decision.get("decision"), decision.get("rows"), decision.get("approveAll", False))
        return jsonify({"count": count})

    def pageBounds():
        try:
            limit = int(request.args.get("limit", "100"))
            offset = int(request.args.get("offset", "0"))
        except ValueError as error:
            raise ValidationError("limit and offset must be integers") from error
        if not 1 <= limit <= 500 or offset < 0:
            raise ValidationError("limit must be 1 through 500 and offset must be nonnegative")
        return limit, offset

    def statusResult(proposal):
        return {key: proposal[key] for key in ("id", "submissionId", "status", "transactionId", "kind", "transferId", "reviewVersion")}

    @app.get("/api/integration/pending")
    def integrationPending():
        if request.args:
            raise ValidationError("Pending read does not accept query parameters")
        rows = pendingReviewRows(getDatabase())
        return jsonify({**pendingCounts(rows), "rows": rows})

    @app.put("/api/integration/submissions/<int:proposalId>")
    def updateIntegrationPending(proposalId):
        if request.args:
            raise ValidationError("Pending update does not accept query parameters")
        nextVersion = savePendingReview(getDatabase(), proposalId, request.get_json(silent=True), integrationUpdate=True)
        if nextVersion is None:
            return jsonify({"error": "Pending proposal does not exist"}), 404
        return jsonify({"reviewVersion": nextVersion})

    @app.route("/api/integration/submissions", methods=["GET", "POST"])
    def integrationSubmissions():
        connection = getDatabase()
        if request.method == "POST":
            if request.args:
                raise ValidationError("Submission does not accept query parameters")
            proposal, created = submitPendingTransaction(connection, g.integration["id"], request.get_json(silent=True))
            return jsonify(statusResult(proposal)), 201 if created else 200
        if set(request.args) - {"limit", "offset", "submissionId", "status"}:
            raise ValidationError("Unsupported submission status filter")
        limit, offset = pageBounds()
        where = "integrationId = ?"
        queryValues = [g.integration["id"]]
        if "submissionId" in request.args:
            where += " AND submissionId = ?"
            queryValues.append(request.args["submissionId"])
        if "status" in request.args:
            if request.args["status"] not in {"Pending", "Approved", "Rejected"}:
                raise ValidationError("Submission status is invalid")
            where += " AND status = ?"
            queryValues.append(request.args["status"])
        total = connection.execute("SELECT COUNT(*) FROM scopedpendingSubmissions WHERE " + where, queryValues).fetchone()[0]
        rows = connection.execute("SELECT * FROM scopedpendingSubmissions WHERE " + where + " ORDER BY id LIMIT ? OFFSET ?", [*queryValues, limit, offset]).fetchall()
        return jsonify({"submissions": [statusResult(row) for row in rows], "total": total, "offset": offset, "limit": limit, "nextOffset": offset + limit if offset + limit < total else None})

    integrationReads = {
        "accounts": (listAccounts, set()), "categories": (listCategories, set()),
        "templates": (listTemplates, set()), "settings": (listSettings, set()),
        "report-periods": (reportPeriods, set()),
        "dashboard": (getDashboard, {"period", "startDate", "endDate"}),
        "budget": (getBudget, {"date"}), "simulator": (getSimulator, {"plan"}),
        "transactions": (listTransactions, {"period", "startDate", "endDate", "type", "accountId", "categoryId", "description", "sort", "direction", "amountMode", "amount", "amountMin", "amountMax", "includeSummary", "limit", "offset"}),
    }

    @app.get("/api/integration/read/<resource>")
    def integrationRead(resource):
        if resource not in integrationReads:
            return jsonify({"error": "Read resource does not exist"}), 404
        readView, supportedFilters = integrationReads[resource]
        if set(request.args) - supportedFilters:
            raise ValidationError("Unsupported read filter")
        if "includeSummary" in request.args and request.args["includeSummary"] not in {"true", "false"}:
            raise ValidationError("includeSummary must be true or false")
        if resource == "transactions":
            for filterName, label in (("accountId", "Account"), ("categoryId", "Category")):
                if filterName in request.args:
                    try:
                        selectedId = int(request.args[filterName])
                    except ValueError as error:
                        raise ValidationError(f"{label} ID must be a positive integer") from error
                    if selectedId < 1:
                        raise ValidationError(f"{label} ID must be a positive integer")
        result = app.make_response(readView()).get_json()
        if resource == "accounts":
            for account in result:
                account.update(accountEligibility(account))
                if account["accountClass"] == "system":
                    account.pop("balanceCents", None)
                    account.pop("balanceOffsetCents", None)
        if resource == "categories":
            for category in result:
                category["ordinaryEligible"] = bool(category["active"] and not category["hasChildren"])
        if resource == "templates":
            for template in result:
                template["ordinaryEligible"] = bool(template["active"])
                if template["accountId"]:
                    account = getDatabase().execute("SELECT active, accountClass FROM scopedaccounts WHERE id = ?", (template["accountId"],)).fetchone()
                    template["ordinaryEligible"] = bool(template["ordinaryEligible"] and account and account["active"] and account["accountClass"] != "system")
                if template["categoryId"]:
                    try:
                        category = getSelectableCategory(getDatabase(), template["categoryId"])
                        template["reportingRole"] = category["reportingRole"]
                    except ValidationError:
                        template["ordinaryEligible"] = False
        if resource == "transactions":
            limit, offset = pageBounds()
            events = result["transactions"] if isinstance(result, dict) else result
            total = len(events)
            envelope = {"transactions": events[offset:offset + limit], "total": total, "offset": offset, "limit": limit, "nextOffset": offset + limit if offset + limit < total else None}
            if isinstance(result, dict):
                envelope["summary"] = result["summary"]
            result = envelope
        return jsonify(result)

    return app


def runApplication(app):
    # Bind before prompting so an unavailable port cannot leave setup running
    # without a server. Keep protected terminal input on the main thread.
    server = make_server("0.0.0.0", 5000, app, threaded=True)
    serverThread = threading.Thread(target=server.serve_forever, name="development-server", daemon=True)
    serverThread.start()
    logging.info("Starting No Part Finance %s", applicationVersion)
    logging.info("Database path: %s", app.config["DATABASE_PATH"])
    logging.info("Schema version: %s", schemaVersion)
    logging.info("Development server host: 0.0.0.0")
    try:
        connection = connectDatabase(app.config["DATABASE_PATH"])
        try:
            ownerConfigured = connection.execute("SELECT 1 FROM ledgerAccess WHERE role = 'owner'").fetchone() is not None
            signInGuidance = "Sign in to use the application." if ownerConfigured else "Complete Owner setup below, then reload to sign in."
            logging.info("Server is running; open http://127.0.0.1:5000 in your browser. %s Keep this terminal open.", signInGuidance)
            configureOnStartup(connection)
        finally:
            connection.close()
        # A bounded join keeps Ctrl+C responsive on Windows as well as Linux.
        while serverThread.is_alive():
            serverThread.join(timeout=0.5)
    except KeyboardInterrupt:
        logging.info("Stopping No Part Finance")
    finally:
        server.shutdown()
        server.server_close()
        serverThread.join()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if sys.argv[1:] == ["--version"]:
        print(applicationVersion)
        sys.exit(0)
    app = createApp()
    if sys.argv[1:] == ["manage-users"]:
        manageUsers(app.config["DATABASE_PATH"])
        sys.exit(0)
    if sys.argv[1:]:
        sys.exit("Usage: app.py [manage-users | --version]")
    runApplication(app)
