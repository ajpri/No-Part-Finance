"""Notification installation lifecycle uses guarded synthetic databases only."""
import tests

import base64
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

import database as ledger
from app import createApp
from users import createUser


class NotificationDeviceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "synthetic.db"
        ledger.initializeDatabase(self.path)
        self.connection = ledger.connectInitialLedgerDatabase(self.path)
        self.addCleanup(self.connection.close)
        self.ledgerId = self.connection.ledgerId
        self.ownerId = createUser(self.connection, "owner", "Owner", "owner password", firstName="Owner")
        self.auditorId = createUser(self.connection, "auditor", "Auditor", "auditor password", firstName="Auditor")
        (Path(self.directory.name) / "platform.conf").write_text("pushNotificationsEnabled = true\n")
        self.application = createApp(self.path)

    def login(self, username="Owner", password="owner password", application=None, installationId=None):
        client = (application or self.application).test_client()
        if installationId:
            client.set_cookie("npfInstallation", installationId)
        csrf = re.search('name="csrfToken" value="([^"]+)"', client.get("/sign-in").get_data(as_text=True)).group(1)
        response = client.post("/sign-in", data={"username": username, "password": password, "csrfToken": csrf})
        self.assertEqual(302, response.status_code)
        installationCookie = next(value for value in response.headers.getlist("Set-Cookie") if value.startswith("npfInstallation="))
        self.assertIn("HttpOnly", installationCookie)
        self.assertIn("SameSite=Lax", installationCookie)
        client.environ_base["HTTP_X_CSRF_TOKEN"] = client.get("/api/session").get_json()["csrfToken"]
        return client

    def subscription(self, marker="fixture"):
        public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
        return {"endpoint": "https://web.push.apple.com/" + marker, "keys": {"p256dh": encode(public), "auth": encode(b"a" * 16)}}

    def enroll(self, client, marker="fixture"):
        subscription = self.subscription(marker)
        self.assertEqual(200, client.post("/api/push", json=subscription).status_code)
        return subscription

    def devices(self, client):
        response = client.get("/api/account/notifications")
        self.assertEqual(200, response.status_code)
        return response.get_json()

    def testAssociationSafeListOwnRemovalAndExplicitReenrollment(self):
        client, auditor = self.login(), self.login("Auditor", "auditor password")
        subscription = self.enroll(client)
        saved = self.connection.execute("SELECT * FROM pushSubscriptions").fetchone()
        self.assertEqual(client.get_cookie("npfInstallation").value, saved["installationId"])
        rows = self.devices(client)
        self.assertEqual({"id", "client", "ledgerName", "label", "current", "sessionStatus"}, set(rows[0]))
        self.assertNotIn(subscription["endpoint"], json.dumps(rows))
        self.assertNotIn(saved["installationId"], json.dumps(rows))
        self.assertEqual([], self.devices(auditor))
        self.assertIn(auditor.post("/api/account/notifications/disable", json={"id": saved["id"]}).status_code, (400, 404))
        self.assertEqual(1, len(self.devices(client)))
        self.assertEqual({"status": "ok"}, client.post("/api/account/notifications/disable", json={"id": saved["id"]}).get_json())
        self.assertEqual([], self.devices(client))
        self.assertEqual(200, client.post("/api/push", json=subscription, headers={"X-Background-Request": "true"}).status_code)
        self.assertEqual([], self.devices(client))
        self.assertEqual(200, client.post("/api/push", json=subscription).status_code)
        self.assertEqual(1, len(self.devices(client)))

    def testExplicitLogoutCurrentRemoteAndOthersCleanupAcrossLedgers(self):
        for operation in ("current", "remote", "others"):
            with self.subTest(operation=operation):
                first, remote = self.login(), self.login()
                self.enroll(first, operation + "first")
                self.enroll(remote, operation + "remote")
                remoteInstallation = remote.get_cookie("npfInstallation").value
                # Independent ledger enrollment shares this installation association.
                with self.connection:
                    self.connection.execute("INSERT INTO ledgers(id,name) VALUES (?, 'Other synthetic ledger')", (operation,))
                    self.connection.execute("""INSERT INTO pushSubscriptions(userId,ledgerId,endpoint,subscription,lastProposalId,installationId,client)
                        SELECT userId, ?, endpoint || '-other', subscription, lastProposalId, installationId, client
                        FROM pushSubscriptions WHERE installationId = ?""", (operation, remoteInstallation))
                if operation == "current":
                    self.assertEqual(200, remote.post("/api/logout").status_code)
                elif operation == "others":
                    self.assertEqual(200, first.post("/api/account/sessions/logout", json={"others": True}).status_code)
                else:
                    remoteId = next(row["id"] for row in first.get("/api/account/sessions").get_json() if not row["current"])
                    self.assertEqual(200, first.post("/api/account/sessions/logout", json={"id": remoteId}).status_code)
                self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM pushSubscriptions WHERE installationId = ?", (remoteInstallation,)).fetchone()[0])
                self.assertTrue(any(row["id"] for row in self.devices(first)))
                self.assertEqual(401, remote.get("/api/session").status_code)
                first.post("/api/logout")

    def testPasswordChangeRetainsCompletingInstallationRemovesOthersAtomically(self):
        first, remote = self.login(), self.login()
        self.enroll(first, "first")
        self.enroll(remote, "remote")
        ownInstallation = first.get_cookie("npfInstallation").value
        with self.connection:
            self.connection.execute("CREATE TRIGGER rejectPasswordAudit BEFORE INSERT ON auditEvents WHEN NEW.action = 'password-change' BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END")
        payload = {"currentPassword": "owner password", "newPassword": "new private passphrase", "confirmation": "new private passphrase"}
        self.assertEqual(500, first.post("/api/account/password", json=payload).status_code)
        self.assertEqual(2, len(self.devices(first)))
        self.assertEqual(200, remote.get("/api/session").status_code)
        with self.connection:
            self.connection.execute("DROP TRIGGER rejectPasswordAudit")
        self.assertEqual(200, first.post("/api/account/password", json=payload).status_code)
        self.assertEqual([ownInstallation], [row[0] for row in self.connection.execute("SELECT installationId FROM pushSubscriptions")])
        self.assertEqual(401, remote.get("/api/session").status_code)

    def testCleanupFailurePreservesSessionsAndEnrollments(self):
        client, remote = self.login(), self.login()
        self.enroll(remote)
        with self.connection:
            self.connection.execute("CREATE TRIGGER rejectNotificationDelete BEFORE DELETE ON pushSubscriptions BEGIN SELECT RAISE(ABORT, 'synthetic cleanup failure'); END")
        for requester, route, payload in ((remote, "/api/logout", {}), (client, "/api/account/sessions/logout", {"others": True})):
            with self.subTest(route=route):
                self.assertEqual(500, requester.post(route, json=payload).status_code)
                self.assertEqual(1, len(self.devices(client)))
                self.assertEqual(200, remote.get("/api/session").status_code)
                self.assertEqual(200, client.get("/api/session").status_code)

    def testExpiryRestartPreservesEnrollmentAndAllRemovalWhenDisabled(self):
        client = self.login()
        self.enroll(client)
        expiry = client.get("/api/session").get_json()["expiresAt"]
        with patch("app.time.time", return_value=expiry):
            self.assertEqual(401, client.get("/api/session").status_code)
        restarted = createApp(self.path)
        newClient = self.login(application=restarted)
        self.assertEqual(1, len(self.devices(newClient)))
        restarted.config["PLATFORM_CONFIGURATION"].update(pushNotificationsEnabled=False, allowAccountSelfService=False)
        self.assertEqual(200, newClient.post("/api/account/notifications/disable", json={"all": True}).status_code)
        self.assertEqual([], self.devices(newClient))

    def testOwnRemovalForAuditorAndNoLedgerAndLegacyRows(self):
        with self.connection:
            self.connection.execute("INSERT INTO pushSubscriptions(userId,ledgerId,endpoint,subscription,lastProposalId) VALUES (?,?,'legacy-auditor','{}',0)", (self.auditorId, self.ledgerId))
        auditor = self.login("Auditor", "auditor password")
        self.assertEqual("Unknown client", self.devices(auditor)[0]["client"])
        legacy = self.devices(auditor)[0]
        self.assertFalse(legacy["current"])
        self.assertEqual(200, auditor.post("/api/account/notifications/label", json={"id": legacy["id"], "label": "Legacy tablet"}).status_code)
        with self.connection:
            self.connection.execute("DELETE FROM ledgerAccess WHERE userId = ?", (self.auditorId,))
        self.assertIsNone(auditor.get("/api/session").get_json()["ledgerId"])
        self.application.config["PLATFORM_CONFIGURATION"]["allowAccountSelfService"] = False
        self.assertEqual(200, auditor.post("/api/account/notifications/label", json={"id": legacy["id"], "label": "No-ledger tablet"}).status_code)
        self.assertEqual(200, auditor.post("/api/account/notifications/disable", json={"all": True}).status_code)
        self.assertEqual([], self.devices(auditor))

    def testSecurityValidationAndCredentialFreeExport(self):
        client = self.login()
        self.enroll(client)
        self.assertEqual(401, self.application.test_client().get("/api/account/notifications").status_code)
        self.assertEqual(403, client.get("/api/account/notifications", headers={"Authorization": "Bearer no-browser-auth"}).status_code)
        for submitted in ({}, {"all": 1}, {"all": False}, {"id": True}, {"id": "1"}, {"id": -1}, {"all": True, "id": 1}):
            self.assertEqual(400, client.post("/api/account/notifications/disable", json=submitted).status_code)
        token = client.environ_base.pop("HTTP_X_CSRF_TOKEN")
        self.assertEqual(403, client.post("/api/account/notifications/disable", json={"all": True}).status_code)
        client.environ_base["HTTP_X_CSRF_TOKEN"] = token
        exportedPath = Path(self.directory.name) / "exported.db"
        exportedPath.write_bytes(client.get("/api/backup").data)
        exported = ledger.connectDatabase(exportedPath)
        self.addCleanup(exported.close)
        self.assertEqual(0, exported.execute("SELECT COUNT(*) FROM pushSubscriptions").fetchone()[0])
        self.assertEqual(0, exported.execute("SELECT COUNT(*) FROM pushConfiguration").fetchone()[0])

    def testSchema30MigrationPreservesHistoryAndLegacyEnrollments(self):
        client = self.login()
        self.enroll(client)
        account = ledger.createAccount(self.connection, "Cash", "cash", openingBalance="123")
        category = ledger.createCategory(self.connection, "Expense")
        ledger.createTransaction(self.connection, account, "2026-10-01", "Synthetic history", category, "12")
        beforeTransactions = [tuple(row) for row in self.connection.execute("SELECT * FROM transactions")]
        beforeEnrollments = [tuple(row) for row in self.connection.execute("SELECT id,userId,ledgerId,endpoint,subscription,lastProposalId FROM pushSubscriptions")]
        with self.connection:
            self.connection.execute("DROP INDEX pushSubscriptionsInstallation")
            self.connection.execute("ALTER TABLE pushSubscriptions DROP COLUMN installationId")
            self.connection.execute("ALTER TABLE pushSubscriptions DROP COLUMN client")
            self.connection.execute("PRAGMA user_version = 30")
        ledger.initializeDatabase(self.path)
        self.assertEqual(beforeTransactions, [tuple(row) for row in self.connection.execute("SELECT * FROM transactions")])
        self.assertEqual(beforeEnrollments, [tuple(row) for row in self.connection.execute("SELECT id,userId,ledgerId,endpoint,subscription,lastProposalId FROM pushSubscriptions")])
        self.assertEqual((None, "Unknown client"), tuple(self.connection.execute("SELECT installationId,client FROM pushSubscriptions").fetchone()))
        self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())


    def testCurrentDeviceFirstAndLabelsPreservedDuringRefresh(self):
        current, remote = self.login(), self.login()
        subscription = self.enroll(current, "current-label")
        self.enroll(remote, "remote-label")
        with self.connection:
            self.connection.execute("UPDATE pushSubscriptions SET client = CASE WHEN endpoint = ? THEN 'Zulu browser' ELSE 'Alpha browser' END", (subscription["endpoint"],))
        rows = self.devices(current)
        self.assertTrue(rows[0]["current"])
        self.assertFalse(rows[1]["current"])
        self.assertTrue(self.devices(remote)[0]["current"])
        currentId = rows[0]["id"]
        before = self.connection.execute("SELECT subscription,lastProposalId FROM pushSubscriptions WHERE id = ?", (currentId,)).fetchone()
        response = current.post("/api/account/notifications/label", json={"id": currentId, "label": "  My iPhone  "})
        self.assertEqual(200, response.status_code)
        self.assertEqual("My iPhone", self.devices(current)[0]["label"])
        saved = self.connection.execute("SELECT subscription,lastProposalId FROM pushSubscriptions WHERE id = ?", (currentId,)).fetchone()
        self.assertEqual({**json.loads(before["subscription"]), "deviceLabel": "My iPhone"}, json.loads(saved["subscription"]))
        self.assertEqual(before["lastProposalId"], saved["lastProposalId"])
        self.assertEqual(200, current.post("/api/push", json=subscription, headers={"X-Background-Request": "true"}).status_code)
        self.assertEqual("My iPhone", self.devices(current)[0]["label"])
        otherLedger = current.post("/api/ledgers", json={"name": "Another ledger"}).get_json()["id"]
        current.environ_base["HTTP_X_LEDGER_ID"] = self.ledgerId
        self.assertEqual(200, current.post("/api/push", json=self.subscription("new-ledger-label"), headers={"X-Ledger-ID": otherLedger}).status_code)
        self.assertEqual(2, sum(row["current"] and row["label"] == "My iPhone" for row in self.devices(current)))
        self.assertEqual(200, current.post("/api/account/notifications/label", json={"id": currentId, "label": ""}).status_code)
        self.assertEqual("", self.devices(current)[0]["label"])
        self.assertEqual(31, self.connection.execute("PRAGMA user_version").fetchone()[0])

    def testLabelsAreOwnUserOnlyValidatedAndAtomicAcrossLedgerEnrollments(self):
        current, auditor = self.login(), self.login("Auditor", "auditor password")
        self.enroll(current, "label-atomic")
        selected = self.devices(current)[0]
        with self.connection:
            self.connection.execute("INSERT INTO ledgers(id,name) VALUES ('label-ledger', 'Other ledger')")
            self.connection.execute("""INSERT INTO pushSubscriptions(userId,ledgerId,endpoint,subscription,lastProposalId,installationId,client)
                SELECT userId,'label-ledger',endpoint || '-other',subscription,lastProposalId,installationId,client
                FROM pushSubscriptions WHERE id = ?""", (selected["id"],))
        before = [tuple(row) for row in self.connection.execute("SELECT * FROM pushSubscriptions ORDER BY id")]
        for label in (None, 123, "x" * 61, "Bad\nLabel"):
            self.assertEqual(400, current.post("/api/account/notifications/label", json={"id": selected["id"], "label": label}).status_code)
        self.assertEqual(400, auditor.post("/api/account/notifications/label", json={"id": selected["id"], "label": "Foreign"}).status_code)
        self.assertEqual(403, current.post("/api/account/notifications/label", json={"id": selected["id"], "label": "CSRF"}, headers={"X-CSRF-Token": "wrong"}).status_code)
        self.assertEqual(before, [tuple(row) for row in self.connection.execute("SELECT * FROM pushSubscriptions ORDER BY id")])
        with self.connection:
            self.connection.execute("CREATE TRIGGER rejectDeviceLabel BEFORE UPDATE ON pushSubscriptions WHEN NEW.ledgerId = 'label-ledger' BEGIN SELECT RAISE(ABORT, 'synthetic label failure'); END")
        self.assertEqual(500, current.post("/api/account/notifications/label", json={"id": selected["id"], "label": "Tablet"}).status_code)
        self.assertEqual(before, [tuple(row) for row in self.connection.execute("SELECT * FROM pushSubscriptions ORDER BY id")])
        with self.connection:
            self.connection.execute("DROP TRIGGER rejectDeviceLabel")
        self.assertEqual(200, current.post("/api/account/notifications/label", json={"id": selected["id"], "label": "Tablet"}).status_code)
        self.assertTrue(all(row["label"] == "Tablet" and row["current"] for row in self.devices(current)))

    def testPermissionCleanupIsCurrentInstallationOnlyAcrossLedgersAndIdempotent(self):
        current, remote = self.login(), self.login()
        subscription = self.enroll(current, "permission-current")
        self.enroll(remote, "permission-remote")
        installation = current.get_cookie("npfInstallation").value
        with self.connection:
            self.connection.execute("INSERT INTO ledgers(id,name) VALUES ('permission-ledger','Other ledger')")
            self.connection.execute("""INSERT INTO pushSubscriptions(userId,ledgerId,endpoint,subscription,lastProposalId,installationId,client)
                SELECT userId,'permission-ledger',endpoint || '-other',subscription,lastProposalId,installationId,client
                FROM pushSubscriptions WHERE installationId = ?""", (installation,))
            self.connection.execute("INSERT INTO pushSubscriptions(userId,ledgerId,endpoint,subscription,lastProposalId,installationId) VALUES (?,?,'other-user','{}',0,?)", (self.auditorId, self.ledgerId, installation))
            self.connection.execute("INSERT INTO pushSubscriptions(userId,ledgerId,endpoint,subscription,lastProposalId) VALUES (?,?,'legacy-permission','{}',0)", (self.ownerId, self.ledgerId))
        preserved = [tuple(row) for row in self.connection.execute("SELECT * FROM pushSubscriptions WHERE userId != ? OR installationId IS NULL OR installationId != ? ORDER BY id", (self.ownerId, installation))]
        beforeSession = current.get("/api/account/sessions").get_json()[0]
        with patch("app.time.time", return_value=beforeSession["lastActiveAt"] + 10):
            for attempt in range(2):
                self.assertEqual(200, current.post("/api/account/notifications/disable", json={"currentDevice": True}, headers={"X-Background-Request": "true"}).status_code)
            afterSession = current.get("/api/account/sessions", headers={"X-Background-Request": "true"}).get_json()[0]
        self.assertEqual(beforeSession["lastActiveAt"], afterSession["lastActiveAt"])
        self.assertEqual(preserved, [tuple(row) for row in self.connection.execute("SELECT * FROM pushSubscriptions ORDER BY id")])
        self.assertEqual(200, current.post("/api/push", json=subscription, headers={"X-Background-Request": "true"}).status_code)
        self.assertEqual(preserved, [tuple(row) for row in self.connection.execute("SELECT * FROM pushSubscriptions ORDER BY id")])
        self.assertEqual(200, current.post("/api/push", json=subscription).status_code)
        self.assertEqual(1, sum(row["current"] for row in self.devices(current)))

    def testPermissionCleanupForNoLedgerAuditorWithFeaturesDisabled(self):
        auditor = self.login("Auditor", "auditor password")
        installation = auditor.get_cookie("npfInstallation").value
        with self.connection:
            self.connection.execute("INSERT INTO pushSubscriptions(userId,ledgerId,endpoint,subscription,lastProposalId,installationId) VALUES (?,?,'auditor-permission','{}',0,?)", (self.auditorId, self.ledgerId, installation))
            self.connection.execute("DELETE FROM ledgerAccess WHERE userId = ?", (self.auditorId,))
        self.application.config["PLATFORM_CONFIGURATION"].update(pushNotificationsEnabled=False, allowAccountSelfService=False)
        self.assertEqual(200, auditor.post("/api/account/notifications/disable", json={"currentDevice": True}).status_code)
        self.assertEqual([], self.devices(auditor))

    def testPermissionCleanupValidationCsrfAndAtomicFailure(self):
        client = self.login()
        self.enroll(client, "permission-validation")
        before = [tuple(row) for row in self.connection.execute("SELECT * FROM pushSubscriptions")]
        for submitted in ({"currentDevice": False}, {"currentDevice": 1}, {"currentDevice": "true"}, {"currentDevice": True, "all": True}, {"currentDevice": True, "installationId": "foreign"}):
            self.assertEqual(400, client.post("/api/account/notifications/disable", json=submitted).status_code)
        self.assertEqual(403, client.post("/api/account/notifications/disable", json={"currentDevice": True}, headers={"X-CSRF-Token": "wrong"}).status_code)
        self.assertEqual(401, self.application.test_client().post("/api/account/notifications/disable", json={"currentDevice": True}).status_code)
        self.assertEqual(403, client.post("/api/account/notifications/disable", json={"currentDevice": True}, headers={"Authorization": "Bearer fixture"}).status_code)
        with self.connection:
            self.connection.execute("CREATE TRIGGER rejectPermissionCleanup BEFORE DELETE ON pushSubscriptions BEGIN SELECT RAISE(ABORT, 'synthetic cleanup failure'); END")
        self.assertEqual(500, client.post("/api/account/notifications/disable", json={"currentDevice": True}).status_code)
        self.assertEqual(before, [tuple(row) for row in self.connection.execute("SELECT * FROM pushSubscriptions")])
        with self.connection:
            self.connection.execute("DROP TRIGGER rejectPermissionCleanup")
        self.assertEqual(200, client.post("/api/account/notifications/disable", json={"currentDevice": True}).status_code)
        self.assertEqual([], self.devices(client))

    def testNotificationSessionPresenceAcrossRestartAndSameBrowserSignin(self):
        current, remote = self.login(), self.login()
        self.enroll(current, "presence-current")
        self.enroll(remote, "presence-remote")
        currentInstallation = current.get_cookie("npfInstallation").value
        remoteInstallation = remote.get_cookie("npfInstallation").value
        with self.connection:
            self.connection.execute("INSERT INTO ledgers(id,name) VALUES ('presence-ledger','Other ledger')")
            self.connection.execute("""INSERT INTO pushSubscriptions(userId,ledgerId,endpoint,subscription,lastProposalId,installationId,client)
                SELECT userId,'presence-ledger',endpoint || '-other',subscription,lastProposalId,installationId,client
                FROM pushSubscriptions WHERE installationId = ?""", (remoteInstallation,))
            self.connection.execute("INSERT INTO pushSubscriptions(userId,ledgerId,endpoint,subscription,lastProposalId) VALUES (?,?,'presence-legacy','{}',0)", (self.ownerId, self.ledgerId))
        before = [tuple(row) for row in self.connection.execute("SELECT * FROM pushSubscriptions ORDER BY id")]
        self.assertEqual(["signed-in"] * 3, [row["sessionStatus"] for row in self.devices(current) if row["sessionStatus"] != "unknown"])
        self.assertEqual(1, sum(row["sessionStatus"] == "unknown" for row in self.devices(current)))
        restarted = createApp(self.path)
        observer = self.login(application=restarted)
        afterRestart = self.devices(observer)
        self.assertEqual(3, sum(row["sessionStatus"] == "no-active-session" for row in afterRestart))
        # Another User's session on the same browser does not count for this User.
        self.login("Auditor", "auditor password", application=restarted, installationId=remoteInstallation)
        self.assertEqual(afterRestart, self.devices(observer))
        resumed = self.login(application=restarted, installationId=remoteInstallation)
        resumedIds = {row["id"] for row in self.devices(resumed) if row["current"]}
        self.assertEqual(2, len(resumedIds))
        for row in self.devices(observer):
            self.assertEqual("signed-in" if row["id"] in resumedIds else "unknown" if row["sessionStatus"] == "unknown" else "no-active-session", row["sessionStatus"])
        self.assertEqual(before, [tuple(row) for row in self.connection.execute("SELECT * FROM pushSubscriptions ORDER BY id")])
        self.assertNotIn(currentInstallation, json.dumps(self.devices(observer)))
        self.assertNotIn(remoteInstallation, json.dumps(self.devices(observer)))
        self.assertEqual(31, self.connection.execute("PRAGMA user_version").fetchone()[0])

    def testNotificationSessionPresenceUsesAnyUnexpiredAssociatedSession(self):
        first = self.login()
        self.enroll(first, "presence-expiry")
        installation = first.get_cookie("npfInstallation").value
        firstExpiry = first.get("/api/session").get_json()["expiresAt"]
        with patch("app.time.time", return_value=firstExpiry - 168 * 3600 + 60):
            second = self.login(installationId=installation)
            secondExpiry = second.get("/api/session").get_json()["expiresAt"]
        with patch("app.time.time", return_value=firstExpiry - 168 * 3600 + 120):
            observer = self.login()
        with patch("app.time.time", return_value=firstExpiry):
            self.assertEqual("signed-in", self.devices(observer)[0]["sessionStatus"])
        with patch("app.time.time", return_value=secondExpiry):
            self.assertEqual("no-active-session", self.devices(observer)[0]["sessionStatus"])
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM pushSubscriptions").fetchone()[0])

    def testProfileSessionInvalidationUpdatesNotificationPresence(self):
        current, remote = self.login(), self.login()
        self.enroll(current, "presence-profile-current")
        self.enroll(remote, "presence-profile-remote")
        self.assertTrue(all(row["sessionStatus"] == "signed-in" for row in self.devices(current)))
        self.assertEqual(200, current.put("/api/account/name", json={"firstName": "Updated", "lastName": "Owner"}).status_code)
        for row in self.devices(current):
            self.assertEqual("signed-in" if row["current"] else "no-active-session", row["sessionStatus"])
