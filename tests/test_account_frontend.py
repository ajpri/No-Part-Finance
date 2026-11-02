"""Focused My Account browser-handler checks, without any database access."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

import tests


class AccountFrontendTests(unittest.TestCase):
    def runJavascript(self, harness):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("Node.js unavailable; set NODE_BINARY for frontend checks")
        source = (Path(__file__).resolve().parents[1] / "app.js").read_text()
        accountFunctions = "function renderMyAccount" + source.split("function renderMyAccount", 1)[1].split("function useSameTabNavigation", 1)[0]
        completed = subprocess.run([nodePath, "-e", harness + "\n" + accountFunctions + "\ncheck().catch(error => { console.error(error); process.exitCode = 1; });"], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def testSettingsDefaultFragmentsRolesAndNoLedger(self):
        self.runJavascript(r'''
const assert = require("node:assert/strict");
let browserSession = {role: "owner", ledgerId: "test"};
let location = {hash: ""}, auditRequestSequence = 0, auditLoads = 0;
function loadAuditLog() { auditLoads++; }
const elements = new Map();
const document = {querySelector(selector) { if (!elements.has(selector)) elements.set(selector, {hidden: false, attributes: {}, setAttribute(key,value) {this.attributes[key]=value;}, removeAttribute(key) {delete this.attributes[key];}}); return elements.get(selector); }};
async function check() {
  showSettingsSection();
  assert.equal(document.querySelector("#settingsMyAccount").hidden, false);
  assert.equal(document.querySelector("#settingsAccounts").hidden, true);
  location.hash = "#accounts"; showSettingsSection();
  assert.equal(document.querySelector("#settingsAccounts").hidden, false);
  location.hash = "#audit-log"; showSettingsSection(); assert.equal(auditLoads, 1);
  location.hash = ""; showSettingsSection();
  assert.equal(document.querySelector("#settingsMyAccount").hidden, false);
  browserSession.role = "auditor"; location.hash = "#integrations"; showSettingsSection();
  assert.equal(document.querySelector("#settingsMyAccount").hidden, false);
  browserSession.ledgerId = null; location.hash = "#accounts"; showSettingsSection();
  assert.equal(document.querySelector("#settingsMyAccount").hidden, false);
  assert.equal(document.querySelector("#settingsAccounts").hidden, true);
}
''')

    def testAccountFormsPreserveErrorsAndResetPasswordOnSuccess(self):
        self.runJavascript(r'''
const assert = require("node:assert/strict");
let browserSession = {csrfToken: "old"};
const elements = new Map();
const document = {querySelector(selector) { if (!elements.has(selector)) elements.set(selector, {value: "", textContent: "", hidden: false, open: true, close(){this.open=false;}, replaceChildren(){}}); return elements.get(selector); }};
const account = {username: "<script>name</script>", firstName: "Test", lastName: "Person", allowAccountSelfService: true, csrfToken: "retained"};
let nextResult = account, nextError = null, submitted;
async function api(path, options) {if(["/api/account/sessions", "/api/account/notifications"].includes(path)) return []; submitted = {path, payload: JSON.parse(options.body)}; if(nextError) throw nextError; return nextResult;}
function showFormError(form,error) {form.error.textContent = error.message;}
function makeForm() { return {error: {textContent:""}, button: {disabled:false}, resetCount:0, querySelector(selector) {return selector === ".formError" ? this.error : this.button;}, reset(){this.resetCount++;}}; }
async function check() {
  renderMyAccount(account);
  assert.equal(document.querySelector("#accountUsername").textContent, "<script>name</script>");
  assert.equal(document.querySelector("#accountEditing").hidden, false);
  assert.equal(document.querySelector("#changeAccountNameButton").hidden, false);
  renderMyAccount({...account, allowAccountSelfService:false});
  assert.equal(document.querySelector("#accountEditing").hidden, true);
  assert.equal(document.querySelector("#changeAccountNameButton").hidden, true);
  const form = makeForm(), event = {preventDefault(){},currentTarget:form};
  document.querySelector("#accountFirstName").value = "Edited";
  nextError = new Error("Name rejected"); await saveAccountName(event);
  assert.equal(document.querySelector("#accountFirstName").value, "Edited");
  assert.equal(form.error.textContent,"Name rejected"); assert.equal(form.button.disabled,false);
  assert.equal(document.querySelector("#accountNameDialog").open, true);
  nextError = null; await saveAccountName(event);
  assert.equal(submitted.path,"/api/account/name");
  assert.deepEqual(submitted.payload,{firstName:"Edited",lastName:"Person"});
  assert.equal(browserSession.csrfToken,"retained");
  assert.equal(document.querySelector("#accountNameDialog").open, false);
  document.querySelector("#accountCurrentPassword").value = "  old Unicode α  ";
  document.querySelector("#accountNewPassword").value = "  next Unicode β  ";
  document.querySelector("#accountPasswordConfirmation").value = "  next Unicode β  ";
  nextError = new Error("Current password rejected"); await changeAccountPassword(event);
  assert.equal(form.resetCount,0); assert.equal(form.error.textContent,"Current password rejected");
  assert.equal(document.querySelector("#accountPasswordDialog").open, true);
  document.querySelector("#accountFirstName").value = "Unsaved name";
  nextError = null; await changeAccountPassword(event);
  assert.equal(document.querySelector("#accountFirstName").value, "Unsaved name");
  assert.equal(form.resetCount,1); assert.equal(form.button.disabled,false);
  assert.equal(document.querySelector("#accountPasswordDialog").open, false);
  assert.deepEqual(submitted.payload,{currentPassword:"  old Unicode α  ",newPassword:"  next Unicode β  ",confirmation:"  next Unicode β  "});
}
''')

    def testAccountDialogOpeningPrefillsNamesAndClearsPasswordDrafts(self):
        self.runJavascript(r'''
const assert = require("node:assert/strict");
let browserSession = {allowAccountSelfService:true, firstName:"Current", lastName:"Name"};
const elements = new Map();
const document = {querySelector(selector) {if (!elements.has(selector)) elements.set(selector, {value:"draft", textContent:"old", open:false, showModal(){this.open=true;}, error:{textContent:"old"}, querySelector(){return this.error;}, reset(){this.resetCount=(this.resetCount||0)+1;}}); return elements.get(selector);}};
function openDialog(dialog,form){form.querySelector(".formError").textContent="";dialog.showModal();}
async function check(){
  openAccountNameDialog();
  assert.equal(document.querySelector("#accountNameDialog").open,true);
  assert.equal(document.querySelector("#accountFirstName").value,"Current");
  assert.equal(document.querySelector("#accountLastName").value,"Name");
  assert.equal(document.querySelector("#accountNameForm").error.textContent,"");
  document.querySelector("#accountFirstName").value="Cancelled draft";
  openAccountNameDialog();
  assert.equal(document.querySelector("#accountFirstName").value,"Current");
  openAccountPasswordDialog();
  assert.equal(document.querySelector("#accountPasswordForm").resetCount,1);
  assert.equal(document.querySelector("#accountPasswordDialog").open,true);
  browserSession.allowAccountSelfService=false;
  openAccountPasswordDialog();openAccountNameDialog();
  assert.equal(document.querySelector("#accountPasswordForm").resetCount,1);
  assert.equal(document.querySelector("#accountNameForm").resetCount,2);
}
''')
