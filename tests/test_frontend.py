"""Focused behavior checks for asynchronous browser controls using optional Node.js."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

import tests  # Activates the database guard before any application imports.


class FrontendAsyncTests(unittest.TestCase):
    def testTransferHelperIgnoresOutdatedResponses(self):
        nodePath = os.environ.get("NODE_BINARY") or shutil.which("node") or shutil.which("nodejs")
        if not nodePath:
            self.skipTest("Node.js is unavailable; asynchronous frontend behavior check requires a JavaScript runtime")
        javascript = (Path(__file__).resolve().parents[1] / "app.js").read_text(encoding="utf-8")
        helperFunction = "async function refreshTransferAmountHelper()" + javascript.split(
            "async function refreshTransferAmountHelper()", 1
        )[1].split("async function editTransfer", 1)[0]
        counterDeclaration = next((line for line in javascript.splitlines() if line.startswith("let transferHelperRequest =")), "")
        harness = r"""
const assert = require("node:assert/strict");
const controls = {
  "#transferFromAccount": {value: "1"},
  "#transferToAccount": {value: "2"},
  "#transferId": {value: ""},
  "#transferDialog": {open: true},
  "#transferAmount": {value: "unchanged"},
  "#transferForm": {error: ""},
  "#transferPaymentHint": {hidden: true, children: [], append(child) { this.children.push(child); }},
};
const requests = [];
const document = {
  querySelector(selector) { return controls[selector]; },
  createElement() { return {setAttribute() {}, addEventListener(event, callback) { this.click = callback; }}; },
};
function api(path) { return new Promise((resolve, reject) => requests.push({path, resolve, reject})); }
function showFormError(form, error) { form.error = error.message; }
function setMoneyInputValue(input, value) { input.value = value; }
function clearHint() { controls["#transferPaymentHint"].children = []; controls["#transferPaymentHint"].hidden = true; }
const eligible = (amountCents) => ({eligible: true, label: `Use ${amountCents}`, amountCents});
async function check() {
  const oldRequest = refreshTransferAmountHelper();
  controls["#transferToAccount"].value = "3";
  clearHint();
  const latestRequest = refreshTransferAmountHelper();
  requests[1].resolve(eligible(2500));
  await latestRequest;
  requests[0].resolve(eligible(9900));
  await oldRequest;
  assert.equal(controls["#transferPaymentHint"].children.length, 1, "late response must not append an old helper");
  const latestButton = controls["#transferPaymentHint"].children[0];
  assert.equal(latestButton.textContent, "Use 2500");
  assert.equal(controls["#transferAmount"].value, "unchanged", "response must not set the amount automatically");
  latestButton.click();
  assert.equal(controls["#transferAmount"].value, "25.00");

  clearHint();
  const beforeBlank = refreshTransferAmountHelper();
  controls["#transferFromAccount"].value = "";
  await refreshTransferAmountHelper();
  requests[2].resolve(eligible(9900));
  await beforeBlank;
  assert.equal(controls["#transferPaymentHint"].children.length, 0, "blank selection invalidates pending request");

  controls["#transferFromAccount"].value = "1";
  const beforeClose = refreshTransferAmountHelper();
  controls["#transferDialog"].open = false;
  requests[3].resolve(eligible(9900));
  await beforeClose;
  assert.equal(controls["#transferPaymentHint"].children.length, 0, "closed dialog must not receive helper");

  controls["#transferDialog"].open = true;
  const beforeEditing = refreshTransferAmountHelper();
  controls["#transferId"].value = "10";
  requests[4].resolve(eligible(9900));
  await beforeEditing;
  assert.equal(controls["#transferPaymentHint"].children.length, 0, "create response cannot enter transfer editing");

  const ineligibleRequest = refreshTransferAmountHelper();
  requests[5].resolve({eligible: false});
  await ineligibleRequest;
  assert.equal(controls["#transferPaymentHint"].children.length, 0);

  controls["#transferId"].value = "";
  const beforeReopen = refreshTransferAmountHelper();
  controls["#transferDialog"].open = false;
  controls["#transferDialog"].open = true;
  clearHint();
  const afterReopen = refreshTransferAmountHelper();
  requests[7].resolve(eligible(4000));
  await afterReopen;
  requests[6].resolve(eligible(9900));
  await beforeReopen;
  assert.equal(controls["#transferPaymentHint"].children.length, 1, "same-pair request from prior dialog must be ignored");
  assert.equal(controls["#transferPaymentHint"].children[0].textContent, "Use 4000");

  clearHint();
  const outdatedFailure = refreshTransferAmountHelper();
  const currentFailure = refreshTransferAmountHelper();
  requests[8].reject(new Error("Outdated request failed"));
  await outdatedFailure;
  assert.equal(controls["#transferForm"].error, "", "stale failures must not overwrite the current form");
  requests[9].reject(new Error("Helper request failed"));
  await currentFailure;
  assert.equal(controls["#transferForm"].error, "Helper request failed", "current failures remain visible for correction");
  assert.equal(controls["#transferPaymentHint"].children.length, 0);
}
check().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        completed = subprocess.run([nodePath, "-e", counterDeclaration + "\n" + helperFunction + "\n" + harness], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
