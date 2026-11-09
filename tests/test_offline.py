"""Read-only Dashboard snapshots, offline navigation, and cache isolation."""

import tests  # Enable the production-database guard before application imports.
import os
import subprocess
import unittest
from pathlib import Path
from tests.test_support import PwaFixture

class OfflineDashboardTests(PwaFixture, unittest.TestCase):
    def testOfflineSnapshotIsolationSelectionsAndNoCredentials(self):
        node = os.environ.get("NODE_BINARY")
        if not node:
            self.skipTest("Set NODE_BINARY for offline storage behavior checks")
        source = Path("offline.js").read_text()
        harness = r'''
const assert=require("node:assert/strict");
const memory=new Map();
const window={isSecureContext:true};const navigator={serviceWorker:{}};
const localStorage={getItem(){return null;},setItem(){}};
let serverPolicy={pwaOfflineEnabled:true,pushNotificationsEnabled:true},connected=true;
const fetch=async()=>{if(!connected)throw new TypeError("offline");return {ok:true,json:async()=>serverPolicy};};
const cacheNames=new Set(["npf-shell-old","other-cache"]);
const caches={async keys(){return [...cacheNames];},async delete(name){cacheNames.delete(name);}};window.caches=caches;
const connection={createObjectStore(){},transaction(){const tx={};tx.objectStore=()=>({
 put(value){memory.set(value.key,structuredClone(value));return{};},
 get(key){return{result:structuredClone(memory.get(key))};},
 getAll(){return{result:structuredClone([...memory.values()])};},
 delete(key){memory.delete(key);return{};},clear(){memory.clear();return{};}
});queueMicrotask(()=>tx.oncomplete());return tx;}};
const indexedDB={open(){const request={result:connection};queueMicrotask(()=>request.onsuccess());return request;}};
async function check(){
 const session={userId:1,ledgerId:"A",role:"owner",ledgers:[{id:"A",label:"One"},{id:"B",label:"Two"}],csrfToken:"SECRET",username:"PRIVATE"};
 const controls={dashboardPeriod:{value:"custom"},dashboardStartDate:{value:"2026-01-01"}};
 await OfflineFinance.save(session,{expensesCents:100},controls);
 await OfflineFinance.save({...session,ledgerId:"B"},{expensesCents:200},controls);
 await OfflineFinance.save({...session,userId:2,ledgerId:"C"},{expensesCents:300},controls);
 assert.equal((await OfflineFinance.available(1)).snapshots.length,2);
 assert.equal((await OfflineFinance.available()).snapshots[0].ledgerId,"C");
 const serialized=JSON.stringify([...memory.values()]);assert(!serialized.includes("SECRET"));assert(!serialized.includes("PRIVATE"));
 assert.equal((await OfflineFinance.available(1)).snapshots[0].controls.dashboardPeriod.value,"custom");
 await OfflineFinance.clear(1,"A");assert.deepEqual((await OfflineFinance.available(1)).snapshots.map(s=>s.ledgerId),["B"]);
 await OfflineFinance.clear(1);assert.equal((await OfflineFinance.available(1)).snapshots.length,0);
 assert.equal((await OfflineFinance.available(2)).snapshots.length,1);
 connected=false;await OfflineFinance.refreshPolicy();assert.equal((await OfflineFinance.available(2)).snapshots.length,1);
 connected=true;serverPolicy={pwaOfflineEnabled:false,pushNotificationsEnabled:true};await OfflineFinance.refreshPolicy();
 assert.equal(memory.size,0);assert.deepEqual([...cacheNames],["other-cache"]);assert.equal(OfflineFinance.policy.pushNotificationsEnabled,true);
 await OfflineFinance.save(session,{expensesCents:400},controls);assert.equal(memory.size,0);
 connected=false;await OfflineFinance.refreshPolicy();assert.equal((await OfflineFinance.available(2)).snapshots.length,0);
 connected=true;serverPolicy={pwaOfflineEnabled:true,pushNotificationsEnabled:false};await OfflineFinance.refreshPolicy();
 await OfflineFinance.save(session,{expensesCents:500},controls);assert.equal((await OfflineFinance.available(1)).snapshots.length,1);assert.equal(OfflineFinance.policy.pushNotificationsEnabled,false);
}
'''
        result = subprocess.run([node, "-e", harness + source + "\ncheck().catch(e=>{console.error(e);process.exit(1)});"], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def testPublicOfflineShellAndWorkerContainNoFinancialState(self):
        signedOut = self.application.test_client()
        response = signedOut.get("/offline-shell")
        self.assertEqual(200, response.status_code)
        self.assertNotIn("owner password", response.get_data(as_text=True))
        worker = signedOut.get("/service-worker.js")
        self.assertEqual(200, worker.status_code)
        self.assertIn("26.11.10", worker.get_data(as_text=True))
        self.assertNotIn("__APPLICATION_VERSION__", worker.get_data(as_text=True))
        self.assertEqual("no-cache", worker.headers["Cache-Control"])

    def testCompactOfflineTimestampKeepsOlderDates(self):
        node = os.environ.get("NODE_BINARY")
        if not node:
            self.skipTest("Set NODE_BINARY for timestamp presentation checks")
        source = Path("app.js").read_text()
        localDate = source[source.index("function localCalendarDate("):source.index("const today =")]
        formatter = source[source.index("function offlineUpdatedLabel("):source.index("function offlineStatus(")]
        harness = r'''
const assert=require("node:assert/strict");
const now=new Date(2026,9,27,16,0);
assert.equal(offlineUpdatedLabel(new Date(2026,9,27,14,35).toISOString(),now),"Updated 2:35 PM");
assert.match(offlineUpdatedLabel(new Date(2026,9,26,14,35).toISOString(),now),/^Updated Oct 26, 2:35 PM$/);
assert.match(offlineUpdatedLabel(new Date(2025,9,26,14,35).toISOString(),now),/2025/);
assert.equal(offlineUpdatedLabel("malformed",now),"Updated time unavailable");
'''
        result = subprocess.run([node, "-e", localDate + formatter + harness], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def testOfflineDashboardLinkProtectsDraftsWithoutNetwork(self):
        node = os.environ.get("NODE_BINARY")
        if not node:
            self.skipTest("Set NODE_BINARY for offline navigation checks")
        source = Path("app.js").read_text()
        navigation = source[source.index("async function openCachedDashboard()"):source.index("async function showOfflineDashboard(")]
        harness = r'''
const assert=require("node:assert/strict");
let offlineSnapshot={ledgerId:"selected"},offlineColdStart=false,allowDiscard=false,shown=null,closed=0,url=null;
const simulatorDirtyRows=new Set(["draft"]);
const document={querySelectorAll:()=>[{close(){closed++;}}]};
const history={replaceState(_state,_title,path){url=path;}};
function hasLedgerDrafts(){return true;}
function confirm(){return allowDiscard;}
function ledgerUrl(path,id){return `${path}?ledger=${id}`;}
async function showOfflineDashboard(snapshot){shown=snapshot;}
async function api(){throw new Error("Offline navigation must not request the server");}
async function check(){
 await openCachedDashboard();assert.equal(shown,null);assert.equal(closed,0);assert.equal(simulatorDirtyRows.size,1);
 allowDiscard=true;await openCachedDashboard();assert.equal(shown,offlineSnapshot);assert.equal(url,"/?ledger=selected");assert.equal(closed,1);assert.equal(simulatorDirtyRows.size,0);assert.equal(offlineColdStart,true);
 shown=null;offlineSnapshot=null;await openCachedDashboard();assert.equal(shown,null);
}
'''
        result = subprocess.run([node, "-e", harness + navigation + "check().catch(e=>{console.error(e);process.exit(1)});"], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
