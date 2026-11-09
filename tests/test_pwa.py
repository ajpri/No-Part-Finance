"""PWA metadata, public assets, packaging, and administrator policy."""

import tests  # Enable the production-database guard before application imports.
import base64
import json
import tempfile
import unittest
from pathlib import Path
from app import createApp
import database as ledger
from tests.test_support import ownerClient
import os
import subprocess
from unittest.mock import patch
from tests.test_support import connectFixtureDatabase as connectDatabase
from tests.test_support import PwaFixture

class PwaServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.databasePath = Path(self.directory.name) / "synthetic.db"
        self.backupsPath = Path(self.directory.name) / "backups"
        self.backupsPath.mkdir()
        self.application = createApp(self.databasePath)
        self.client = self.application.test_client()
        self.connection = connectDatabase(self.databasePath)
        self.iconPaths = {
            "/assets/icon-192.png", "/assets/icon-512.png",
            "/assets/icon-maskable-512.png", "/assets/apple-touch-icon.png",
        }

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def snapshot(self):
        return tuple(self.connection.iterdump())

    def assertMetadata(self, response):
        self.addCleanup(response.close)
        self.assertEqual("text/html", response.mimetype)
        self.assertIn("noindex", response.headers["X-Robots-Tag"])
        self.assertEqual("no-store", response.headers["Cache-Control"])
        html = response.get_data(as_text=True)
        for expected in (
            '<meta name="theme-color" content="#202124">',
            '<meta name="apple-mobile-web-app-title" content="No Part Finance">',
            '<meta name="apple-mobile-web-app-capable" content="yes">',
            '<link rel="manifest" href="/pwa.webmanifest">',
            '<link rel="apple-touch-icon" href="/assets/apple-touch-icon.png">',
            '<script src="/theme.js"></script>',
        ):
            self.assertIn(expected, html)
        self.assertLess(html.index('<script src="/theme.js"></script>'), html.index('<link rel="stylesheet"'))
        self.assertNotIn("navigator.serviceWorker", html)

    def testManifestAndThemeArePublicBeforeOwnerAndSignIn(self):
        before = self.snapshot()
        response = self.client.get("/pwa.webmanifest")
        self.assertEqual(200, response.status_code)
        self.assertEqual("application/manifest+json", response.mimetype)
        self.addCleanup(response.close)
        manifest = json.loads(response.get_data())
        self.assertEqual("No Part Finance", manifest["name"])
        self.assertEqual("No Part Finance", manifest["short_name"])
        self.assertEqual("A simple, self-hosted personal finance app for tracking accounts, transactions, and spending.", manifest["description"])
        self.assertEqual(("/", "/", "/", "standalone"), (manifest["id"], manifest["start_url"], manifest["scope"], manifest["display"]))
        self.assertEqual("#202124", manifest["theme_color"])
        self.assertEqual("#202124", manifest["background_color"])
        self.assertEqual({
            ("/assets/icon-192.png", "192x192", "image/png", "any"),
            ("/assets/icon-512.png", "512x512", "image/png", "any"),
            ("/assets/icon-maskable-512.png", "512x512", "image/png", "maskable"),
        }, {(icon["src"], icon["sizes"], icon["type"], icon.get("purpose", "any")) for icon in manifest["icons"]})
        with self.client.head("/pwa.webmanifest") as head:
            self.assertEqual(200, head.status_code)
        script = self.client.get("/theme.js")
        self.addCleanup(script.close)
        self.assertEqual(200, script.status_code)
        self.assertEqual("text/javascript", script.mimetype)
        self.assertIn(b"applyThemePreference", script.get_data())
        with self.client.head("/theme.js") as head:
            self.assertEqual(200, head.status_code)
        self.assertEqual(before, self.snapshot())
        ownerClient(self.application)
        before = self.snapshot()
        signedOut = self.application.test_client()
        for path in ("/pwa.webmanifest", "/theme.js"):
            with signedOut.get(path) as publicResponse:
                self.assertEqual(200, publicResponse.status_code)
        self.assertEqual(before, self.snapshot())

    def testMetadataOnOwnerConfigurationSignInAndApplicationShell(self):
        before = self.snapshot()
        response = self.client.get("/")
        self.assertEqual(503, response.status_code)
        self.assertMetadata(response)
        self.assertEqual(before, self.snapshot())
        owner = ownerClient(self.application)
        before = self.snapshot()
        self.assertMetadata(self.client.get("/sign-in"))
        self.assertEqual(302, self.client.get("/").status_code)
        for route in ("/", "/transactions", "/settings", "/budget", "/simulator", "/pending"):
            with self.subTest(route=route):
                self.assertMetadata(owner.get(route))
        self.assertEqual(before, self.snapshot())

    def testOnlyExactIconFilesArePublicAndMissingArtworkHasNoSubstitute(self):
        before = self.snapshot()
        emptyPublicRoot = Path(self.directory.name) / "empty-public"
        emptyPublicRoot.mkdir()
        with patch("app.projectDirectory", emptyPublicRoot):
            for path in self.iconPaths:
                with self.subTest(path=path):
                    self.assertEqual(404, self.client.get(path).status_code)
                    self.assertEqual(404, self.client.head(path).status_code)
        for path in ("/assets/README.md", "/assets/finance.db", "/assets/../SPEC.md", "/assets/%2e%2e/AGENTS.md", "/assets/nested/icon-192.png", "/assets/ICON-192.png", "/finance.db", "/SPEC.md", "/AGENTS.md", "/theme.js/extra", "/pwa.webmanifest/extra"):
            with self.subTest(path=path):
                self.assertEqual(404, self.client.get(path).status_code)
        self.assertEqual(before, self.snapshot())

    def testPublicPwaRoutesAcceptOnlyReadsAndDoNotBypassFinancialAuthorization(self):
        before = self.snapshot()
        for path in ("/pwa.webmanifest", "/theme.js", *self.iconPaths):
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                with self.subTest(path=path, method=method):
                    self.assertEqual(405, self.client.open(path, method=method).status_code)
        self.assertEqual(503, self.client.get("/api/dashboard").status_code)
        self.assertEqual(before, self.snapshot())
        ownerClient(self.application)
        before = self.snapshot()
        self.assertEqual(401, self.client.get("/api/dashboard").status_code)
        self.assertEqual(401, self.client.post("/api/transactions", json={}).status_code)
        self.assertEqual(before, self.snapshot())

    def testSuppliedIconExportsAreServedAsPngWithoutExposingOtherAssets(self):
        # Test-only artwork stays outside the repository; no placeholder is shipped.
        publicRoot = Path(self.directory.name) / "public"
        assetsPath = publicRoot / "assets"
        assetsPath.mkdir(parents=True)
        pngBytes = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aGMsAAAAASUVORK5CYII=")
        for iconPath in self.iconPaths:
            (assetsPath / Path(iconPath).name).write_bytes(pngBytes)
        (assetsPath / "private.txt").write_text("PRIVATE")
        before = self.snapshot()
        with patch("app.projectDirectory", publicRoot):
            for iconPath in self.iconPaths:
                with self.subTest(path=iconPath):
                    response = self.client.get(iconPath)
                    self.addCleanup(response.close)
                    self.assertEqual(200, response.status_code)
                    self.assertEqual("image/png", response.mimetype)
                    self.assertEqual(pngBytes, response.get_data())
                    with self.client.head(iconPath) as head:
                        self.assertEqual(200, head.status_code)
            self.assertEqual(404, self.client.get("/assets/private.txt").status_code)
        self.assertEqual(before, self.snapshot())

class PwaPackageTests(unittest.TestCase):
    def setUp(self):
        from packageApplication import includedFiles
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.repositoryPath = self.root / "repository"
        self.repositoryPath.mkdir()
        self.backupsPath = self.repositoryPath / "backups"
        self.backupsPath.mkdir()
        for relativePath in includedFiles:
            includedPath = self.repositoryPath / relativePath
            includedPath.parent.mkdir(parents=True, exist_ok=True)
            includedPath.write_text("Synthetic maintained file: " + relativePath)
        self.databasePath = self.repositoryPath / "synthetic.db"
        ledger.initializeDatabase(self.databasePath)

    def tearDown(self):
        self.directory.cleanup()

    def package(self, includeDatabase):
        from packageApplication import createApplicationPackage
        return createApplicationPackage(repositoryPath=self.repositoryPath, databasePath=self.databasePath, outputPath=self.root / "packages", includeDatabase=includeDatabase)

    def testBothPackageVariantsIncludeMetadataWithoutPendingArtwork(self):
        import zipfile
        from packageApplication import archiveRootName, pwaIconFiles
        for includeDatabase in (False, True):
            with self.subTest(includeDatabase=includeDatabase):
                result = self.package(includeDatabase)
                with zipfile.ZipFile(result["archivePath"]) as archive:
                    names = set(archive.namelist())
                    for required in ("theme.js", "pwa.webmanifest", "assets/README.md", "tests/test_pwa.py"):
                        self.assertIn(archiveRootName + "/" + required, names)
                    self.assertEqual(includeDatabase, archiveRootName + "/finance.db" in names)
                    for iconPath in pwaIconFiles:
                        self.assertNotIn(archiveRootName + "/" + iconPath, names)

    def testBothPackageVariantsIncludeOnlyApprovedSuppliedIconExports(self):
        import zipfile
        from packageApplication import archiveRootName, pwaIconFiles
        pngBytes = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aGMsAAAAASUVORK5CYII=")
        for iconPath in pwaIconFiles:
            (self.repositoryPath / iconPath).write_bytes(pngBytes)
        privatePath = self.repositoryPath / "assets" / "private.txt"
        privatePath.write_text("PRIVATE")
        for includeDatabase in (False, True):
            with self.subTest(includeDatabase=includeDatabase):
                result = self.package(includeDatabase)
                with zipfile.ZipFile(result["archivePath"]) as archive:
                    for iconPath in pwaIconFiles:
                        archivedPath = archiveRootName + "/" + iconPath
                        self.assertEqual(pngBytes, archive.read(archivedPath))
                        self.assertEqual((self.repositoryPath / iconPath).stat().st_mode & 0o777, (archive.getinfo(archivedPath).external_attr >> 16) & 0o777)
                    self.assertNotIn(archiveRootName + "/assets/private.txt", archive.namelist())

class PwaPolicyTests(PwaFixture, unittest.TestCase):
    def testPwaPolicyDefaultsFalseAndIndependentOverrides(self):
        from platformConfiguration import ConfigurationError, configurationSample, loadConfiguration
        root = Path(self.directory.name) / "default-installation"
        root.mkdir()
        defaults = loadConfiguration(root)
        self.assertFalse(defaults["pwaOfflineEnabled"])
        self.assertFalse(defaults["pushNotificationsEnabled"])
        sample = configurationSample()
        self.assertIn("pwaOfflineEnabled = false", sample)
        self.assertIn("pushNotificationsEnabled = false", sample)
        path = root / "platform.conf"
        for offline, push in [(False, False), (True, False), (False, True), (True, True)]:
            path.write_text(f"pwaOfflineEnabled = {str(offline).lower()}\npushNotificationsEnabled = {str(push).lower()}\n")
            saved = loadConfiguration(root)
            self.assertEqual(offline, saved["pwaOfflineEnabled"])
            self.assertEqual(push, saved["pushNotificationsEnabled"])
        for key in ("pwaOfflineEnabled", "pushNotificationsEnabled"):
            path.write_text(f"{key} = not-a-boolean\n")
            with self.assertRaises(ConfigurationError):
                loadConfiguration(root)
        path.write_text("pwaOfflineEnabled = true\npushNotificationsEnabled = true\n")
        running = createApp(root / "restart-test.db")
        path.write_text("pwaOfflineEnabled = false\npushNotificationsEnabled = false\n")
        self.assertTrue(running.test_client().get("/pwa-policy").get_json()["pwaOfflineEnabled"])
        restarted = createApp(root / "restart-test.db")
        self.assertEqual({"pwaOfflineEnabled": False, "pushNotificationsEnabled": False}, restarted.test_client().get("/pwa-policy").get_json())

    def testPwaPolicyPublicRoutesManifestAndWorkerCombinations(self):
        signedOut = self.application.test_client()
        configuration = self.application.config["PLATFORM_CONFIGURATION"]
        for offline, push in [(False, False), (True, False), (False, True), (True, True)]:
            with self.subTest(offline=offline, push=push):
                configuration.update(pwaOfflineEnabled=offline, pushNotificationsEnabled=push)
                response = signedOut.get("/pwa-policy")
                self.assertEqual(200, response.status_code)
                self.assertEqual({"pwaOfflineEnabled": offline, "pushNotificationsEnabled": push}, response.get_json())
                self.assertEqual("no-store", response.headers["Cache-Control"])
                with signedOut.get("/pwa.webmanifest") as manifest:
                    self.assertEqual(200, manifest.status_code)
                self.assertEqual(200 if offline else 404, signedOut.get("/offline-shell").status_code)
                worker = signedOut.get("/service-worker.js").get_data(as_text=True)
                self.assertIn(f'const offlineEnabled = "{str(offline).lower()}" === "true";', worker)
                self.assertIn(f'const pushEnabled = "{str(push).lower()}" === "true";', worker)
                self.assertNotIn("__PWA_OFFLINE_ENABLED__", worker)
                self.assertIn('/pwa.webmanifest', signedOut.get("/sign-in").get_data(as_text=True))

    def testDisabledWorkerClearsOnlyOwnedCachesAndDoesNotIntercept(self):
        node = os.environ.get("NODE_BINARY")
        if not node:
            self.skipTest("Set NODE_BINARY for disabled worker checks")
        source = Path("service-worker.js").read_text().replace("__PUSH_NOTIFICATIONS_ENABLED__", "false").replace("__PWA_OFFLINE_ENABLED__", "false")
        harness = r'''
const assert=require("node:assert/strict");
const names=new Set(["npf-shell-old","other-app-cache"]),handlers={};let pending;
const caches={async keys(){return [...names];},async delete(name){names.delete(name);},open(){throw new Error("Disabled worker must not populate cache");}};
const self={location:{origin:"https://finance.example.org"},addEventListener:(name,handler)=>handlers[name]=handler,async skipWaiting(){},clients:{async claim(){}},registration:{showNotification(){throw new Error("Push disabled");}}};
'''
        check = r'''
async function check(){
 handlers.install({waitUntil(promise){pending=promise;}});await pending;
 handlers.activate({waitUntil(promise){pending=promise;}});await pending;
 assert.deepEqual([...names],["other-app-cache"]);
 handlers.fetch({request:{url:"https://finance.example.org/",method:"GET",mode:"navigate"},respondWith(){throw new Error("Disabled cache intercepted navigation");}});
 handlers.push({waitUntil(){throw new Error("Disabled push processed notification");}});
}
check().catch(error=>{console.error(error);process.exit(1);});
'''
        result = subprocess.run([node, "-e", harness + source + check], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
