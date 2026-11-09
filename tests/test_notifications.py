"""Notification devices, push delivery, permission cleanup, and isolation."""

import tests  # Enable the production-database guard before application imports.
import base64
import json
import re
import tempfile
import unittest
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
import database as ledger
from app import createApp
import os
import subprocess
from unittest.mock import Mock, patch
from users import createUser, createOwner
from tests.test_support import PwaFixture

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

    def testNotificationDeviceMigrationPreservesHistoryAndLegacyEnrollments(self):
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

class PushNotificationTests(PwaFixture, unittest.TestCase):
    def testPushMasterSwitchStopsEnrollmentAndBothDeliveryKinds(self):
        self.client.get("/api/push")
        subscription = self.pushSubscription()
        self.client.post("/api/push", json=subscription)
        self.enrollInvitationRecipient()
        configuration = self.application.config["PLATFORM_CONFIGURATION"]
        configuration["pushNotificationsEnabled"] = False
        with patch("push.applicationKey") as key, patch("push.deliverPending") as pending, patch("push.deliverInvitation") as invitation:
            self.assertEqual({"enabled": False, "publicKey": None, "endpoints": []}, self.client.get("/api/push").get_json())
            self.assertEqual(403, self.client.post("/api/push", json=subscription).status_code)
            key.assert_not_called()
            self.assertEqual(201, self.submit("push-disabled").status_code)
            response = self.client.post("/api/invitations", json={"username": "Recipient", "role": "member"})
            self.assertEqual(201, response.status_code)
            pending.assert_not_called()
            invitation.assert_not_called()
        self.assertEqual(200, self.client.delete("/api/push", json={"endpoint": subscription["endpoint"]}).status_code)
        self.assertEqual(1, self.client.get("/api/pending/status").get_json()["count"])

    def testPushPermissionEnrollmentRetryAndFailureIsolation(self):
        self.assertEqual(87, len(self.client.get("/api/push").get_json()["publicKey"]))
        subscription = self.pushSubscription()
        self.assertEqual(200, self.client.post("/api/push", json=subscription).status_code)
        with patch("push.webpush", return_value=type("Response", (), {"status_code": 201})()) as sender:
            self.assertEqual(201, self.submit("push-once").status_code)
            self.assertEqual(200, self.submit("push-once").status_code)
            self.assertEqual(1, sender.call_count)
            payload = json.loads(sender.call_args.kwargs["data"])
            self.assertEqual(self.ledgerId, payload["ledgerId"])
            self.assertEqual({"ledgerId", "proposalId"}, set(payload))
        with patch("push.webpush", side_effect=RuntimeError("private provider error")):
            self.assertEqual(201, self.submit("push-failure").status_code)
        self.assertEqual(2, self.client.get("/api/pending/status").get_json()["count"])
        self.assertEqual(200, self.client.delete("/api/push", json={"endpoint": subscription["endpoint"]}).status_code)
        with patch("push.webpush") as sender:
            self.submit("disabled")
            sender.assert_not_called()

    def testPushUsesEnrollmentOriginForInternalHttpSubmission(self):
        import base64
        self.client.get("/api/push")
        subscription = self.pushSubscription()
        self.assertEqual(200, self.client.post("/api/push", json=subscription).status_code)
        # The proposal can arrive over plain HTTP from an internal integration.
        self.client.environ_base.pop("HTTP_ORIGIN")
        response = Mock(status_code=201, text="", headers={})
        with patch("push.NoRedirectSession.post", return_value=response) as send:
            self.assertEqual(201, self.submit("internal-http").status_code)
        self.assertEqual(1, send.call_count)
        headers = send.call_args.kwargs["headers"]
        token = headers["Authorization"].split("t=", 1)[1].split(",", 1)[0]
        encoded = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual("https://finance.example.org", claims["sub"])
        self.assertEqual("https://web.push.apple.com", claims["aud"])
        self.assertEqual("300", headers["ttl"])
        self.assertEqual("aes128gcm", headers["content-encoding"])

    def testExistingEnrollmentRepairsSenderWithoutReplayingHistory(self):
        self.client.get("/api/push")
        subscription = self.pushSubscription()
        self.client.environ_base.pop("HTTP_ORIGIN")
        self.client.post("/api/push", json=subscription)
        with patch("push.webpush") as sender:
            self.assertEqual(201, self.submit("before-repair").status_code)
            sender.assert_not_called()
        before = self.connection.execute("SELECT lastProposalId FROM pushSubscriptions").fetchone()[0]
        self.client.environ_base["HTTP_ORIGIN"] = "https://finance.example.org"
        self.client.post("/api/push", json=subscription)
        saved = self.connection.execute("SELECT lastProposalId, subscription FROM pushSubscriptions").fetchone()
        self.assertEqual(before, saved[0])
        self.assertEqual("https://finance.example.org", json.loads(saved[1])["vapidSubject"])
        with patch("push.webpush", return_value=Mock(status_code=201)) as sender:
            self.assertEqual(200, self.submit("before-repair").status_code)
            sender.assert_not_called()
            self.assertEqual(201, self.submit("after-repair").status_code)
            self.assertEqual(1, sender.call_count)

    def testPushLogsProviderRejectionWithoutPrivateResponse(self):
        from pywebpush import WebPushException
        self.client.get("/api/push")
        self.client.post("/api/push", json=self.pushSubscription())
        response = Mock(status_code=403)
        response.json.return_value = {"reason": "BadJwtToken", "private": "secret endpoint"}
        with patch("push.webpush", side_effect=WebPushException("secret endpoint", response=response)):
            self.assertEqual(201, self.submit("rejected-token").status_code)
        text = (Path(self.directory.name) / "logs" / "startup.log").read_text()
        self.assertIn("HTTP 403", text)
        self.assertIn("BadJwtToken", text)
        self.assertNotIn("secret endpoint", text)

    def testPushWorkerShowsNotificationBeforeUpdatingClients(self):
        node = os.environ.get("NODE_BINARY")
        if not node:
            self.skipTest("Set NODE_BINARY for push worker checks")
        source = Path("service-worker.js").read_text().replace("__PUSH_NOTIFICATIONS_ENABLED__", "true").replace("__PWA_OFFLINE_ENABLED__", "true")
        harness = r'''
const assert=require("node:assert/strict");
const handlers={},calls=[];let pending;
const self={addEventListener:(name,handler)=>handlers[name]=handler,
 registration:{async showNotification(title,options){calls.push("notification");assert.equal(title,"Pending Review");assert.equal(options.body,"A new pending item is ready to review. Tap to open.");}},
 clients:{async matchAll(){calls.push("clients");throw new Error("A client disappeared");}}};
'''
        check = r'''
handlers.push({data:{json:()=>({ledgerId:"selected",proposalId:1})},waitUntil(promise){pending=promise;}});
pending.then(()=>assert.deepEqual(calls,["notification","clients"])).catch(error=>{console.error(error);process.exit(1);});
'''
        result = subprocess.run([node, "-e", harness + source + check], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def testInvitationPushRecipientDeduplicationAndDurableClaim(self):
        recipientId, subscription = self.enrollInvitationRecipient()
        # The sender's device must not receive someone else's invitation.
        self.client.post("/api/push", json=self.pushSubscription())
        thirdOwner = createOwner(self.connection, "Third", "third password", firstName="Third")
        thirdLedger = self.connection.execute("SELECT ledgerId FROM ledgerAccess WHERE userId = ?", (thirdOwner,)).fetchone()[0]
        with self.connection:
            self.connection.execute("INSERT INTO ledgerAccess (userId, ledgerId, role) VALUES (?, ?, 'member')", (recipientId, thirdLedger))
            self.connection.execute("""INSERT INTO pushSubscriptions (userId, ledgerId, endpoint, subscription, lastProposalId)
                SELECT userId, ?, endpoint, subscription, lastProposalId FROM pushSubscriptions WHERE userId = ? LIMIT 1""", (thirdLedger, recipientId))
        with patch("push.webpush", return_value=Mock(status_code=201)) as sender:
            response = self.client.post("/api/invitations", json={"username": "Recipient", "role": "member"})
            self.assertEqual(201, response.status_code)
            invitationId = response.get_json()["id"]
            self.assertEqual(1, sender.call_count)
            self.assertEqual(subscription["endpoint"], sender.call_args.args[0]["endpoint"])
            self.assertEqual({"endpoint", "keys"}, set(sender.call_args.args[0]))
            self.assertEqual({"type": "ledger-invitation", "invitationId": invitationId}, json.loads(sender.call_args.kwargs["data"]))
            from push import deliverInvitation
            deliverInvitation(self.connection, invitationId, None)
            self.assertEqual(1, sender.call_count)
            self.assertEqual(400, self.client.post("/api/invitations", json={"username": "Recipient", "role": "member"}).status_code)
            self.assertEqual(1, sender.call_count)
        self.login("Recipient", "recipient password")
        self.client.post("/api/push", json=subscription)
        with patch("push.webpush") as sender:
            deliverInvitation(self.connection, invitationId, None)
            sender.assert_not_called()
        self.assertEqual(1, len(self.client.get("/api/invitations").get_json()))

    def testInvitationPushFailureKeepsInviteAndGuardsStaleRecipients(self):
        import time
        from push import deliverInvitation
        recipientId, _ = self.enrollInvitationRecipient()
        with patch("push.webpush", side_effect=RuntimeError("private provider details")):
            response = self.client.post("/api/invitations", json={"username": "Recipient", "role": "auditor"})
        self.assertEqual(201, response.status_code)
        invitationId = response.get_json()["id"]
        self.assertEqual("Pending", self.connection.execute("SELECT status FROM sharingInvitations WHERE id = ?", (invitationId,)).fetchone()[0])
        # Clear the test-only delivery claim to independently exercise eligibility guards.
        for condition in ["cancelled", "expired", "disabled", "revoked"]:
            with self.subTest(condition=condition):
                with self.connection:
                    self.connection.execute("UPDATE pushSubscriptions SET subscription = json_remove(subscription, '$.lastInvitationId') WHERE userId = ?", (recipientId,))
                    now = int(time.time())
                    self.connection.execute("UPDATE sharingInvitations SET status = 'Pending', createdAt = ?, expiresAt = ? WHERE id = ?", (now, now + 604800, invitationId))
                    if condition == "cancelled":
                        self.connection.execute("UPDATE sharingInvitations SET status = 'Cancelled' WHERE id = ?", (invitationId,))
                    elif condition == "expired":
                        self.connection.execute("UPDATE sharingInvitations SET createdAt = 0, expiresAt = 604800 WHERE id = ?", (invitationId,))
                    elif condition == "disabled":
                        self.connection.execute("UPDATE users SET enabled = 0 WHERE id = ?", (recipientId,))
                    else:
                        self.connection.execute("UPDATE users SET enabled = 1 WHERE id = ?", (recipientId,))
                        self.connection.execute("DELETE FROM pushSubscriptions WHERE userId = ?", (recipientId,))
                with patch("push.webpush") as sender:
                    deliverInvitation(self.connection, invitationId, None)
                    sender.assert_not_called()

    def testInvitationAuditRollbackSendsNoPush(self):
        self.enrollInvitationRecipient()
        with self.connection:
            self.connection.execute("CREATE TRIGGER failInvitationAudit BEFORE INSERT ON auditEvents WHEN NEW.recordType = 'sharingInvitations' BEGIN SELECT RAISE(ABORT, 'test audit failure'); END")
        with patch("push.webpush") as sender:
            response = self.client.post("/api/invitations", json={"username": "Recipient", "role": "member"})
            self.assertEqual(500, response.status_code)
            sender.assert_not_called()
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM sharingInvitations").fetchone()[0])

    def testInvitationWorkerCopyAndReviewDestination(self):
        node = os.environ.get("NODE_BINARY")
        if not node:
            self.skipTest("Set NODE_BINARY for invitation notification checks")
        source = Path("service-worker.js").read_text().replace("__PUSH_NOTIFICATIONS_ENABLED__", "true").replace("__PWA_OFFLINE_ENABLED__", "true")
        harness = r'''
const assert=require("node:assert/strict");
const handlers={},notifications=[],messages=[],opened=[];let pending;
const self={location:{origin:"https://finance.example.org"},addEventListener:(name,handler)=>handlers[name]=handler,
 registration:{async showNotification(title,options){notifications.push({title,...options});}},
 clients:{async matchAll(){return [{visibilityState:"visible",url:"https://finance.example.org/?ledger=existing",postMessage(message){messages.push(message);}}];},async openWindow(url){opened.push(url);}}};
'''
        check = r'''
async function check(){
 handlers.push({data:{json:()=>({type:"ledger-invitation",invitationId:7})},waitUntil(promise){pending=promise;}});await pending;
 assert.equal(notifications[0].title,"Ledger Invitation");assert.equal(notifications[0].body,"You’ve been invited to join a ledger. Tap to review.");assert.equal(notifications[0].data.url,"/pending");assert.deepEqual(messages,[{type:"invitation-arrival"}]);
 handlers.notificationclick({notification:{close(){},data:notifications[0].data},waitUntil(promise){pending=promise;}});await pending;assert.deepEqual(opened,["https://finance.example.org/pending"]);
}
check().catch(error=>{console.error(error);process.exit(1);});
'''
        result = subprocess.run([node, "-e", harness + source + check], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def testPushGuardsAndExpiredSubscriptionCleanup(self):
        subscription = self.pushSubscription()
        self.client.get("/api/push")
        for endpoint in ("http://web.push.apple.com/x", "https://127.0.0.1/x", "https://web.push.apple.com.attacker.example/x", "https://web.push.apple.com:5000/x"):
            self.assertEqual(400, self.client.post("/api/push", json={**subscription, "endpoint": endpoint}).status_code)
        self.client.post("/api/push", json=subscription)
        with patch("push.webpush", return_value=type("Response", (), {"status_code": 410})()):
            self.submit("expired-push")
        self.assertEqual([], self.client.get("/api/push").get_json()["endpoints"])
        self.client.post("/api/push", json=subscription)
        with self.connection:
            self.connection.execute("UPDATE users SET enabled = 0 WHERE id = ?", (self.ownerId,))
        from push import deliverPending
        with patch("push.webpush") as sender:
            deliverPending(self.connection, self.ledgerId, 100, "https://example.test")
            sender.assert_not_called()
        self.login("Auditor", "auditor password")
        self.assertEqual(403, self.client.get("/api/push").status_code)
        self.assertEqual(403, self.client.post("/api/push", json=subscription).status_code)


if __name__ == "__main__":
    unittest.main()
