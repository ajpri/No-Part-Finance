let accounts = [];
let categories = [];
let transactions = [];
let templates = [];
let budget = null;
let simulator = null;
let simulatorDirtyRows = new Set();
let simulatorRecordingRowId = null;
let loadedSinkingFundDate = null;
let minimumHelperRequest = 0;
let utilizationHelperRequest = 0;
let transferHelperRequest = 0;
let notificationTimer = null;
let notificationRemainingMilliseconds = 0;
let notificationStartedAt = 0;
let simulatorSavePending = false;
let browserSession = null;
let pendingDecisionBusy = false;
const pendingSummaryState = {all: {requestId: 0, signature: null}, selected: {requestId: 0, signature: null}};
const currentPath = window.location.pathname;

function localCalendarDate(currentDate = new Date()) {
  const year = currentDate.getFullYear();
  const month = String(currentDate.getMonth() + 1).padStart(2, "0");
  const day = String(currentDate.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

const today = localCalendarDate();
const periodUrlValues = {
  all: "all",
  thisMonth: "this-month",
  lastMonth: "last-month",
  yearToDate: "year-to-date",
  last30Days: "last-30-days",
  custom: "custom",
};

function dollars(amountCents, signed = false) {
  const sign = amountCents < 0 ? "-" : signed && amountCents > 0 ? "+" : "";
  return `${sign}$${(Math.abs(amountCents) / 100).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
}

function friendlyCalendarDate(isoDate) {
  const match = String(isoDate ?? "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) return isoDate;
  const [, year, month, day] = match;
  const monthName = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][Number(month) - 1];
  if (!monthName) return isoDate;
  const result = `${monthName} ${Number(day)}`;
  return Number(year) === new Date().getFullYear() ? result : `${result}, ${year}`;
}

function setMoneyInputValue(input, value) {
  input.value = value;
}

function submittedMoneyValue(rawValue) {
  return String(rawValue ?? "").trim();
}

async function evaluateMoneyInput(input) {
  if (input.dataset.pendingField && input.closest("tr")?.saving) return;
  const requestId = Number(input.dataset.moneyRequestId || "0");
  const rawValue = input.value;
  if (!rawValue.trim()) return;
  try {
    const result = await api("/api/money/evaluate", {method: "POST", body: JSON.stringify({expression: rawValue})});
    const dialog = input.closest("dialog");
    if (!input.isConnected || Number(input.dataset.moneyRequestId || "0") !== requestId || input.value !== rawValue || (dialog && !dialog.open)) return;
    input.value = result.inputValue;
    if (input.dataset.pendingField) input.dispatchEvent(new Event("change"));
  } catch (error) {
    if (input.isConnected && Number(input.dataset.moneyRequestId || "0") === requestId && input.value === rawValue) showFormError(input.closest("form"), error);
  }
}

function enhanceMoneyInputs(root = document) {
  const candidates = [];
  if (root.matches?.('input[inputmode="decimal"]')) candidates.push(root);
  if (root.querySelectorAll) candidates.push(...root.querySelectorAll('input[inputmode="decimal"]'));
  for (const input of candidates) {
    if (input.id === "utilizationWarning" || input.dataset.moneyEnhanced) continue;
    input.dataset.moneyEnhanced = "true";
    const wrapper = document.createElement("span");
    wrapper.className = "moneyInput";
    const prefix = document.createElement("span");
    prefix.className = "moneyInputPrefix";
    prefix.setAttribute("aria-hidden", "true");
    prefix.textContent = "$";
    input.before(wrapper);
    wrapper.append(prefix, input);
    input.addEventListener("input", () => { input.dataset.moneyRequestId = String(Number(input.dataset.moneyRequestId || "0") + 1); });
    input.addEventListener("focus", () => { input.dataset.moneyRequestId = String(Number(input.dataset.moneyRequestId || "0") + 1); });
    input.addEventListener("blur", () => evaluateMoneyInput(input));
  }
}

function sendNotification(message, options = {}) {
  if (typeof message !== "string" || !message) return;
  const notification = document.querySelector("#temporaryNotification");
  const closeButton = document.querySelector("#temporaryNotificationClose");
  const seconds = Number(options.time);
  const time = Number.isFinite(seconds) && seconds > 0 ? seconds : 5;
  const color = ["green", "yellow", "red"].includes(options.color) ? options.color : "green";
  closeButton.hidden = options.hasCloseButton === false;
  document.querySelector("#temporaryNotificationMessage").textContent = message;
  notification.className = `temporaryNotification status${color[0].toUpperCase()}${color.slice(1)}`;
  notification.hidden = false;
  clearTimeout(notificationTimer);
  notificationRemainingMilliseconds = time * 1000;
  resumeNotificationTimer();
}

function closeNotification() {
  clearTimeout(notificationTimer);
  const closeButton = document.querySelector("#temporaryNotificationClose");
  if (document.activeElement === closeButton) closeButton.blur();
  document.querySelector("#temporaryNotification").hidden = true;
}

function pauseNotificationTimer() {
  if (!notificationStartedAt) return;
  clearTimeout(notificationTimer);
  notificationRemainingMilliseconds -= Date.now() - notificationStartedAt;
  notificationStartedAt = 0;
}

function resumeNotificationTimer() {
  if (document.querySelector("#temporaryNotification").hidden) return;
  notificationStartedAt = Date.now();
  notificationTimer = setTimeout(closeNotification, Math.max(1, notificationRemainingMilliseconds));
}

function friendlyAccountBalance(account) {
  return account.accountClass === "credit"
    ? account.balanceCents > 0 ? `${dollars(account.balanceCents)} credit` : dollars(-account.balanceCents)
    : dollars(account.balanceCents);
}

async function ledgerNotification(action, accountIds) {
  await loadReferenceData();
  const lines = [action];
  for (const accountId of accountIds) {
    const account = accounts.find((item) => item.id === Number(accountId));
    if (account && account.accountClass !== "system") lines.push(`${account.name}: ${friendlyAccountBalance(account)}`);
  }
  sendNotification(lines.join("\n"));
}

function friendlySimulatorBalance(account, amountCents) {
  if (account.accountClass !== "credit") return dollars(amountCents);
  return amountCents > 0 ? `${dollars(amountCents)} credit` : dollars(-amountCents);
}

async function loadSimulator(requestedPlanId = new URLSearchParams(location.search).get("plan"), preservedDrafts = []) {
  await loadReferenceData(); simulator = await api(`/api/simulator${requestedPlanId ? `?plan=${encodeURIComponent(requestedPlanId)}` : ""}`);
  if (simulator.plan && String(requestedPlanId || "") !== String(simulator.plan.id)) history.replaceState({}, "", `/simulator?plan=${simulator.plan.id}`);
  simulatorDirtyRows.clear(); renderSimulator(preservedDrafts);
}

function renderSimulator(preservedDrafts = []) {
  const hasPlan = Boolean(simulator.plan); document.querySelector("#simulatorPlanControls").hidden = !hasPlan;
  document.querySelector("#simulatorEmpty").hidden = hasPlan; document.querySelector("#simulatorWorkspace").hidden = !hasPlan;
  if (!hasPlan) return;
  const plan = simulator.plan; setOptions(document.querySelector("#simulatorPlanSelect"), simulator.plans.map((item) => ({id:item.id,name:`${friendlyCalendarDate(item.planDate)} — ${item.name}`,active:true})), null, plan.id);
  document.querySelector("#deleteExecutedSimulatorRowsButton").hidden = !plan.rows.some((row) => row.executed && !["live", "buffer"].includes(row.rowType));
  if (browserSession?.role === "auditor") { renderAuditorWorksheet(); renderSimulatorResults(); return; }
  const body = document.querySelector("#simulatorRows"); body.replaceChildren(); let previousRowType=null; for (const row of plan.rows) { const preserved=preservedDrafts.find((draft)=>draft.id===row.id);const displayed=preserved?{...row,...preserved.values,categoryId:preserved.categoryId}:row;appendSimulatorRow(displayed,previousRowType!==null&&previousRowType!==row.rowType,Boolean(preserved),row);previousRowType=row.rowType; } for(const draft of preservedDrafts.filter((item)=>item.id===null))appendSimulatorRow({...draft.values,categoryId:draft.categoryId},false,true);appendSimulatorRow(); renderSimulatorResults();
}

function simulatorSelect(rows, placeholder, value) { const select=document.createElement("select"); setOptions(select, rows, placeholder, value); return select; }
function simulatorAccountSelect(rows, placeholder, value) { const select=document.createElement("select"); setAccountOptions(select, rows, placeholder, value); return select; }
function simulatorDraftFromRow(row) {
  const rowType=row.cells[0].querySelector("select").value; const accountSelects=row.cells[2].querySelectorAll("select");
  return {rowType,selectedTemplateId:row.cells[1].querySelector("select")?.value||"",fromAccountId:rowType==="income"?null:accountSelects[0]?.value,toAccountId:rowType==="income"?accountSelects[0]?.value:accountSelects[1]?.value,label:row.cells[3].querySelector("input")?.value,amountValue:row.cells[4].querySelector("input")?.value};
}
function activeSimulatorDirtyRows() {
  for (const row of simulatorDirtyRows) if (!row.isConnected || !row.classList.contains("unsavedRow")) simulatorDirtyRows.delete(row);
  return [...simulatorDirtyRows].filter((row) => !row.classList.contains("hangingRow"));
}
function preservedSimulatorDrafts(excludedRow = null) { return activeSimulatorDirtyRows().filter((row)=>row!==excludedRow).map((row)=>({id:row.dataset.id?Number(row.dataset.id):null,categoryId:row.dataset.categoryId?Number(row.dataset.categoryId):null,values:simulatorDraftFromRow(row)})); }
function appendSimulatorRow(saved = {}, groupStart = false, dirty = false, originalSavedRow = null) {
  const hanging=!saved.id&&!saved.rowType; const tr=document.createElement("tr"); if (saved.id) tr.dataset.id=saved.id; else if (hanging) tr.classList.add("hangingRow"); else tr.classList.add("unsavedRow"); if(dirty)tr.classList.add("unsavedRow");if(saved.categoryId)tr.dataset.categoryId=saved.categoryId;
  if (groupStart) tr.classList.add("simulatorGroupStart");
  const originalSaved={...(originalSavedRow||saved)};
  let previousKind=saved.rowType||""; let retainedTransferAmount=saved.rowType==="transfer"&&saved.amountCents?(saved.amountCents/100).toFixed(2):saved.rowType==="payoff"&&saved.effectiveAmountCents!==undefined?(saved.effectiveAmountCents/100).toFixed(2):saved.amountValue||"";
  const type=simulatorSelect([{id:"live",name:"Live balance",active:true},{id:"income",name:"Income",active:true},{id:"spend",name:"Spend",active:true},{id:"transfer",name:"Transfer",active:true},{id:"payoff",name:"Payoff",active:true},{id:"buffer",name:"Buffer",active:true}],"Select type",saved.rowType); tr.insertCell().append(type);
  const templateCell=tr.insertCell(), accountCell=tr.insertCell(), labelCell=tr.insertCell(), amountCell=tr.insertCell(), actions=tr.insertCell(); amountCell.className="amount";
  if (saved.executed) {
    tr.classList.add("simulatorExecutedRow");
    type.disabled=true; templateCell.textContent="—";
    const accountName=(id)=>accounts.find((account)=>account.id===Number(id))?.name||"—";
    accountCell.textContent=["transfer","payoff"].includes(saved.rowType)?`${accountName(saved.fromAccountId)} → ${accountName(saved.toAccountId)}`:accountName(saved.rowType==="income"?saved.toAccountId:saved.fromAccountId);
    labelCell.textContent=saved.label||"—"; amountCell.textContent=dollars(saved.effectiveAmountCents||0);
    const edit=document.createElement("button");edit.type="button";edit.textContent="Edit";edit.addEventListener("click",()=>openSimulatorRowDialog(saved));actions.append(edit);bodyAppend(tr);return;
  }
  function rebuild() {
    const kind=type.value; templateCell.replaceChildren(); accountCell.replaceChildren(); labelCell.replaceChildren(); amountCell.replaceChildren();
    if (!kind) { for (const cell of [templateCell,accountCell,labelCell,amountCell]) cell.textContent=""; return; }
    if (["income","spend"].includes(kind)) { const compatible=activeTemplates().filter((item)=>!item.categoryId || categories.find((c)=>c.id===item.categoryId)?.reportingRole===(kind==="income"?"income":"expense")); const use=simulatorSelect(compatible,"None",saved.selectedTemplateId||""); use.className="rowTemplate"; templateCell.append(use); use.addEventListener("change",()=>{const item=templates.find((x)=>x.id===Number(use.value));if(item){accountCell.querySelector("select").value=item.accountId||"";labelCell.querySelector("input").value=item.description||"";setMoneyInputValue(amountCell.querySelector("input"),item.enteredAmountCents?(item.enteredAmountCents/100).toFixed(2):"");if(item.categoryId)tr.dataset.categoryId=item.categoryId;else delete tr.dataset.categoryId;}else delete tr.dataset.categoryId;mark();}); } else templateCell.textContent="—";
    const currentAccount = kind==="income" ? saved.toAccountId : saved.fromAccountId; const allowed=accounts.filter((a)=>kind==="buffer"?a.active:(a.active || a.id===Number(currentAccount))).filter((a)=>kind==="transfer" || a.accountClass!=="system").filter((a)=>(!["live","payoff","buffer"].includes(kind)) || a.accountClass==="cash"); const first=simulatorAccountSelect(allowed,"Select account",currentAccount); first.className="fromAccount"; accountCell.append(first);
    if (["transfer","payoff"].includes(kind)) { const destinations=accounts.filter((a)=>a.active || a.id===Number(saved.toAccountId)).filter((a)=>kind!=="payoff"||a.accountClass==="credit");const second=simulatorAccountSelect(destinations,"Select account",saved.toAccountId); second.className="toAccount"; second.addEventListener("change",()=>{const destination=accounts.find((account)=>account.id===Number(second.value));const label=labelCell.querySelector("input");if(destination?.accountClass==="credit"&&label){label.value=paymentDescription(destination);mark();}else if(destination?.accountClass==="cash"&&label){label.value=`Transfer to ${destination.name}`;mark();}}); const arrow=document.createElement("span");arrow.textContent="→";arrow.setAttribute("aria-hidden","true");const pair=document.createElement("div");pair.className="simulatorAccountPair";pair.append(first,arrow,second);accountCell.replaceChildren(pair); }
    if (kind==="live") { labelCell.textContent="—"; const account=accounts.find((a)=>a.id===Number(currentAccount)); amountCell.textContent=account?dollars(account.balanceCents):"—"; } else { const label=document.createElement("input"); label.value=saved.label||""; labelCell.append(label); if(kind==="payoff"){amountCell.textContent=Number.isInteger(saved.effectiveAmountCents)?dollars(saved.effectiveAmountCents):"Calculated on save";}else{const amount=document.createElement("input"); amount.inputMode="decimal"; amount.placeholder="0.00"; amount.value=saved.amountValue??(saved.amountCents?(saved.amountCents/100).toFixed(2):""); amountCell.append(amount);} }
  }
  function mark(){simulatorDirtyRows.add(tr);tr.classList.add("unsavedRow");save.hidden=false;if(tr.dataset.id)cancel.hidden=false;if(typeof record!=="undefined")record.hidden=true;}
  tr.addEventListener("keydown",(event)=>{if(event.key==="Enter"&&tr.dataset.id&&tr.classList.contains("unsavedRow")&&event.target.matches('input[type="text"], input:not([type]), input[type="number"], input[type="date"]')){event.preventDefault();if(!simulatorSavePending)saveSimulatorWorksheetRow(tr);}});type.addEventListener("change",()=>{ const wasHanging=tr.classList.contains("hangingRow"); const nextKind=type.value; const accountSelects=accountCell.querySelectorAll("select"); const currentLabel=labelCell.querySelector("input")?.value||""; const currentAmount=amountCell.querySelector("input")?.value||""; const previousFrom=previousKind==="income"?null:accountSelects[0]?.value;const previousTo=previousKind==="income"?accountSelects[0]?.value:["transfer","payoff"].includes(previousKind)?accountSelects[1]?.value:null;if(previousKind==="transfer")retainedTransferAmount=currentAmount||retainedTransferAmount;const fixedTypes=["income","spend","transfer","buffer"];const preserveFixedFields=fixedTypes.includes(previousKind)&&fixedTypes.includes(nextKind);const preservePaymentFields=["transfer","payoff"].includes(previousKind)&&["transfer","payoff"].includes(nextKind);const nextSaved={id:saved.id};if(["live","spend","transfer","payoff","buffer"].includes(nextKind)&&previousFrom)nextSaved.fromAccountId=previousFrom;if(["income","transfer","payoff"].includes(nextKind)&&previousTo)nextSaved.toAccountId=previousTo;if(preserveFixedFields||preservePaymentFields)nextSaved.label=currentLabel;if(preserveFixedFields)nextSaved.amountValue=currentAmount;if(preservePaymentFields)nextSaved.amountValue=nextKind==="transfer"?retainedTransferAmount:"";tr.classList.remove("hangingRow");saved=nextSaved;previousKind=nextKind;delete tr.dataset.categoryId;rebuild();mark();if(wasHanging){actions.append(save," ",remove);appendSimulatorRow();} }); tr.addEventListener("input",mark); tr.addEventListener("change",mark); rebuild();
  const save=document.createElement("button"); save.type="button"; save.textContent="Save Row"; save.hidden=Boolean(saved.id)&&!dirty; save.addEventListener("click",()=>saveSimulatorWorksheetRow(tr)); const cancel=document.createElement("button");cancel.type="button";cancel.textContent="Cancel Changes";cancel.hidden=!dirty;cancel.addEventListener("click",()=>{simulatorDirtyRows.delete(tr);tr.classList.remove("unsavedRow");saved={...originalSaved};previousKind=saved.rowType||"";retainedTransferAmount=saved.rowType==="transfer"&&saved.amountCents?(saved.amountCents/100).toFixed(2):saved.rowType==="payoff"&&saved.effectiveAmountCents!==undefined?(saved.effectiveAmountCents/100).toFixed(2):"";type.value=saved.rowType||"";if(saved.categoryId)tr.dataset.categoryId=saved.categoryId;else delete tr.dataset.categoryId;rebuild();save.hidden=true;cancel.hidden=true;record.hidden=false;}); const remove=document.createElement("button"); remove.type="button"; remove.className="destructiveButton"; remove.textContent="Delete"; remove.addEventListener("click",()=>deleteSimulatorWorksheetRow(tr)); const edit=document.createElement("button");edit.type="button";edit.textContent="Edit";edit.addEventListener("click",()=>openSimulatorRowDialog(saved));const record=document.createElement("button");record.type="button";record.className="positiveAction";record.textContent=["income","spend"].includes(saved.rowType)?"+ Transaction":"+ Transfer";record.hidden=dirty;record.addEventListener("click",()=>recordSimulatorRow(saved)); if(!hanging){actions.append(save," ");if(saved.id)actions.append(cancel," ");if(saved.id&&!["live","buffer"].includes(saved.rowType))actions.append(record," ",edit);else if(saved.id)actions.append(edit);else actions.append(remove);} if(dirty||(!saved.id&&saved.rowType))simulatorDirtyRows.add(tr);bodyAppend(tr); function bodyAppend(row){const body=document.querySelector("#simulatorRows"),composer=body.querySelector(".hangingRow");if(composer&&row!==composer)body.insertBefore(row,composer);else body.append(row);}
}

function recordSimulatorRow(row){simulatorRecordingRowId=row.id;if(["income","spend"].includes(row.rowType)){newTransaction().then(()=>{document.querySelector("#transactionAccount").value=row.rowType==="income"?row.toAccountId:row.fromAccountId;document.querySelector("#transactionDescription").value=row.label||"";document.querySelector("#transactionCategory").value=row.rowType==="income"?(simulator.reimbursementCategoryId||""):(row.categoryId||"");setMoneyInputValue(document.querySelector("#transactionAmount"),((row.effectiveAmountCents||row.amountCents)/100).toFixed(2));});}else{newTransfer();document.querySelector("#transferFromAccount").value=row.fromAccountId;document.querySelector("#transferToAccount").value=row.toAccountId;setMoneyInputValue(document.querySelector("#transferAmount"),(row.effectiveAmountCents/100).toFixed(2));document.querySelector("#transferDescription").value=row.label||"Transfer";updateTransferPresentation(false);}}
function openSimulatorRowDialog(row){const form=document.querySelector("#simulatorRowForm");form.reset();form.dataset.rowType=row.rowType;const summary=document.querySelector("#simulatorRowSummary");summary.replaceChildren();const accountName=(id)=>accounts.find((item)=>item.id===Number(id))?.name||"—";const details=[["Type",row.rowType==="live"?"Live balance":row.rowType[0].toUpperCase()+row.rowType.slice(1)],["Status",row.executed?"Executed":"Planned"]];if(row.rowType==="income")details.push(["Account",accountName(row.toAccountId)]);else if(["transfer","payoff"].includes(row.rowType))details.push(["From Account",accountName(row.fromAccountId)],["To Account",accountName(row.toAccountId)]);else details.push(["Account",accountName(row.fromAccountId)]);if(row.label)details.push(["Label",row.label]);if(row.rowType!=="live")details.push(["Amount",dollars(row.effectiveAmountCents||row.amountCents||0)]);if(row.executedTransactionId)details.push(["Source",row.linkedTransferId?"Linked ledger transfer":"Linked ledger transaction"]);for(const [name,value] of details){const wrapper=document.createElement("div"),term=document.createElement("dt"),description=document.createElement("dd");term.textContent=name;description.textContent=value;wrapper.append(term,description);summary.append(wrapper);}form.dataset.hasLinkedLedger=row.executedTransactionId?"true":"false";const planningOnly=["live","buffer"].includes(row.rowType);document.querySelector("#simulatorEditRowId").value=row.id;document.querySelector("#simulatorExecuted").checked=Boolean(row.executed);document.querySelector("#simulatorExecutedLabel").hidden=planningOnly;document.querySelector("#simulatorExecutedHelp").hidden=planningOnly;document.querySelector("#simulatorExecuted").disabled=Boolean(row.executedTransactionId);document.querySelector("#saveSimulatorExecution").hidden=planningOnly||Boolean(row.executedTransactionId);document.querySelector("#simulatorManualExecutionMessage").hidden=!(row.executed&&!row.executedTransactionId);const duplicate=document.querySelector("#duplicateSimulatorRowButton");duplicate.hidden=row.rowType==="live";duplicate.onclick=()=>{appendSimulatorRow({rowType:row.rowType,fromAccountId:row.fromAccountId,toAccountId:row.toAccountId,label:row.label,amountValue:((row.effectiveAmountCents||row.amountCents||0)/100).toFixed(2),categoryId:row.categoryId});document.querySelector("#simulatorRowDialog").close();};const linked=document.querySelector("#editLinkedSimulatorTransaction");linked.hidden=!row.executedTransactionId;linked.onclick=()=>{document.querySelector("#simulatorRowDialog").close();if(row.linkedTransferId){editTransfer(row.linkedTransferId).then(()=>document.querySelector("#transferForm").dataset.simulatorLinked="true");}else{editOrdinaryTransaction({...row,id:row.executedTransactionId,date:row.linkedDate,accountId:row.rowType==="income"?row.toAccountId:row.fromAccountId,description:row.label,enteredAmountCents:row.effectiveAmountCents,pinned:0,transferId:null});document.querySelector("#transactionForm").dataset.simulatorLinked="true";}};openDialog(document.querySelector("#simulatorRowDialog"),form);}
async function saveSimulatorExecutionState(event){event.preventDefault();const rowId=document.querySelector("#simulatorEditRowId").value;const row=simulator.plan.rows.find((item)=>item.id===Number(rowId));const executed=document.querySelector("#simulatorExecuted").checked;if(row.executed&&!row.executedTransactionId&&!executed&&!confirm("Simulator has no linked ledger transaction. Returning this row to Planned may double-count money if you recorded the activity elsewhere. Continue?"))return;await api(`/api/simulator/plans/${simulator.plan.id}/rows/${rowId}/execution`,{method:"PATCH",body:JSON.stringify({executed})});document.querySelector("#simulatorRowDialog").close();await loadSimulator(simulator.plan.id);}

async function saveSimulatorWorksheetRow(row) { if(simulatorSavePending)return;simulatorSavePending=true;const type=row.cells[0].querySelector("select").value; const selects=row.cells[2].querySelectorAll("select"); const payload={rowType:type,fromAccountId:type==="income"?null:selects[0]?.value,toAccountId:type==="income"?selects[0]?.value:selects[1]?.value,categoryId:row.dataset.categoryId||null,label:row.cells[3].querySelector("input")?.value,amount:row.cells[4].querySelector("input")?.value}; const preservedDrafts=preservedSimulatorDrafts(row);try { await api(`/api/simulator/plans/${simulator.plan.id}/rows${row.dataset.id?`/${row.dataset.id}`:""}`,{method:row.dataset.id?"PATCH":"POST",body:JSON.stringify(payload)}); await loadSimulator(simulator.plan.id,preservedDrafts); } catch(error){window.alert(error.message);} finally{simulatorSavePending=false;} }
async function deleteSimulatorWorksheetRow(row){if(!row.dataset.id){row.remove();simulatorDirtyRows.delete(row);return;} if(!confirm("Permanently delete this worksheet row?"))return;const preservedDrafts=preservedSimulatorDrafts(row);await api(`/api/simulator/plans/${simulator.plan.id}/rows/${row.dataset.id}`,{method:"DELETE"});await loadSimulator(simulator.plan.id,preservedDrafts);}
function statusBadge(status, utilization = false){const badge=document.createElement("span");badge.className=`utilizationBadge utilization${status.state[0].toUpperCase()}${status.state.slice(1)}`;badge.textContent=status.label;badge.setAttribute("aria-label",status.accessibleLabel||status.label+(utilization?" projected utilization":""));return badge;}
function renderSimulatorResults(){const p=simulator.plan,t=p.totals; document.querySelector("#simulatorStartingCash").textContent=dollars(t.startingCashCents); document.querySelector("#simulatorIncomeTotal").textContent=dollars(t.plannedIncomeCents); document.querySelector("#simulatorSpendingTotal").textContent=dollars(t.plannedSpendingCents); document.querySelector("#simulatorTransferTotal").textContent=dollars(t.transfersPaymentsCents); document.querySelector("#simulatorUnallocated").textContent=dollars(t.unallocatedMoneyCents); const excludeBuffer=document.querySelector("#excludeBufferFromProjectedBalances").checked;const body=document.querySelector("#simulatorResultRows");body.replaceChildren();for(const a of p.results){const row=body.insertRow();if(a.accountClass==="credit")row.classList.add("creditResultRow");const name=row.insertCell();name.append(a.name+(a.active?"":" (archived)"));if(a.minimumStatus)name.append(" ",statusBadge(a.minimumStatus));if(a.projectedUtilization)name.append(" ",statusBadge(a.projectedUtilization,true));const displayedProjectedBalanceCents=excludeBuffer&&a.accountClass==="cash"?a.projectedBalanceCents-a.bufferCents:a.projectedBalanceCents;const balance=row.insertCell();balance.textContent=friendlySimulatorBalance(a,displayedProjectedBalanceCents);balance.className="amount";}}
function openSimulatorPlanDialog(mode="new"){const form=document.querySelector("#simulatorPlanDialogForm");form.reset();if(mode===true)mode="duplicate";if(mode===false)mode="new";const editing=mode==="edit",duplicating=mode==="duplicate";document.querySelector("#simulatorPlanDialogTitle").textContent=editing?"Edit Plan":duplicating?"Duplicate Plan":"New Plan";document.querySelector("#simulatorPlanSubmitButton").textContent=editing?"Save":"Create Plan";document.querySelector("#simulatorDuplicateSource").value=duplicating?simulator.plan.id:"";document.querySelector("#simulatorEditPlanId").value=editing?simulator.plan.id:"";document.querySelector("#simulatorDialogDate").value=editing?simulator.plan.planDate:today;document.querySelector("#simulatorDialogName").value=editing?simulator.plan.name:"";document.querySelector("#deleteSimulatorPlanButton").hidden=!editing;openDialog(document.querySelector("#simulatorPlanDialog"),form);}
async function createSimulatorPlanFromDialog(event){event.preventDefault();const form=event.currentTarget,editPlanId=document.querySelector("#simulatorEditPlanId").value,preservedDrafts=editPlanId?preservedSimulatorDrafts():[];const payload={name:document.querySelector("#simulatorDialogName").value,planDate:document.querySelector("#simulatorDialogDate").value};try{if(editPlanId){await api(`/api/simulator/plans/${editPlanId}`,{method:"PATCH",body:JSON.stringify(payload)});document.querySelector("#simulatorPlanDialog").close();await loadSimulator(Number(editPlanId),preservedDrafts);return;}payload.duplicatePlanId=document.querySelector("#simulatorDuplicateSource").value||null;const result=await api("/api/simulator/plans",{method:"POST",body:JSON.stringify(payload)});document.querySelector("#simulatorPlanDialog").close();const nextUrl=new URL(window.location.href);nextUrl.searchParams.set("plan",result.id);history.pushState({},"",`${nextUrl.pathname}${nextUrl.search}${nextUrl.hash}`);await loadSimulator();}catch(error){showFormError(form,error);}}
async function removeSimulatorPlan(){const form=document.querySelector("#simulatorPlanDialogForm"),plan=simulator.plan;if(!confirm(`Permanently delete plan "${plan.name}"? Type the plan name in the next prompt to confirm.`))return;if(prompt("Enter the plan name to confirm deletion:")!==plan.name)return;try{await api(`/api/simulator/plans/${plan.id}`,{method:"DELETE"});document.querySelector("#simulatorPlanDialog").close();await loadSimulator();}catch(error){showFormError(form,error);}}
async function removeExecutedSimulatorRows(){const plan=simulator.plan,executedCount=plan.rows.filter((row)=>row.executed&&!["live","buffer"].includes(row.rowType)).length;if(!executedCount)return;const message=`Delete ${executedCount} executed ${executedCount===1?"row":"rows"} from "${plan.name}"?\n\nLinked transactions and transfers will remain in the ledger.\nThis cannot be undone.`;if(!confirm(message))return;const preservedDrafts=preservedSimulatorDrafts();try{await api(`/api/simulator/plans/${plan.id}/executed-rows`,{method:"DELETE",body:JSON.stringify({expectedCount:executedCount})});await loadSimulator(plan.id,preservedDrafts);}catch(error){window.alert(error.message);}}
function warnSimulatorUnsaved(event){if(activeSimulatorDirtyRows().length){event.preventDefault();event.returnValue="";}}

async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {"Content-Type": "application/json", ...(browserSession ? {"X-CSRF-Token": browserSession.csrfToken} : {}), ...options.headers}});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Request failed");
  return result;
}

function option(select, value, label) {
  const item = document.createElement("option");
  item.value = value;
  item.textContent = label;
  select.append(item);
  return item;
}

function setOptions(select, rows, placeholder, currentValue = "") {
  select.replaceChildren();
  if (placeholder !== null) option(select, "", placeholder);
  for (const row of rows) {
    const item = option(select, row.id, row.name + (row.active ? "" : " (archived)"));
    if (row.accountClass) item.dataset.accountClass = row.accountClass;
    if (row.accountSubtype) item.dataset.accountSubtype = row.accountSubtype;
  }
  select.value = currentValue ?? "";
}

function setAccountOptions(select, rows, placeholder, currentValue = "", operational = true) {
  select.replaceChildren();
  if (placeholder !== null) option(select, "", placeholder);
  const accountClasses = new Set(rows.map((account) => account.accountClass));
  const hasPins = operational && rows.some((account) => account.pinnedForSelection && account.active && account.accountClass !== "system");
  const grouped = accountClasses.size > 1 || hasPins;
  for (const [accountClass, label] of [["pinned", "Pinned"], ["cash", "Cash"], ["credit", "Credit Cards and Loans"], ["system", "System"]]) {
    const classRows = rows.filter((account) => {
      const pinned = hasPins && account.pinnedForSelection && account.active && account.accountClass !== "system";
      return accountClass === "pinned" ? pinned : !pinned && account.accountClass === accountClass;
    });
    if (accountClass === "pinned") classRows.sort((left, right) => left.selectorOrder - right.selectorOrder);
    if (!classRows.length) continue;
    const parent = grouped ? document.createElement("optgroup") : select;
    if (grouped) parent.label = label;
    for (const account of classRows) {
      const item = option(parent, account.id, account.name + (account.active ? "" : " (archived)"));
      item.dataset.accountClass = account.accountClass;
      if (account.accountSubtype) item.dataset.accountSubtype = account.accountSubtype;
    }
    if (grouped) select.append(parent);
  }
  select.value = currentValue ?? "";
}

function setMixedCategoryOptions(select, rows, placeholder, currentValue = "") {
  select.replaceChildren();
  if (placeholder !== null) option(select, "", placeholder);
  const categoryNames = new Map(categories.map((category) => [category.id, category.name]));
  for (const [role, label] of [["expense", "Expense"], ["income", "Income"]]) {
    const roleRows = rows.filter((category) => category.reportingRole === role);
    if (!roleRows.length) continue;
    const group = document.createElement("optgroup");
    group.label = label;
    for (const category of roleRows) {
      const parentName = categoryNames.get(category.parentCategoryId);
      const categoryLabel = `${parentName ? `${parentName} › ` : ""}${category.name}${category.active ? "" : " (archived)"}`;
      option(group, category.id, categoryLabel);
    }
    select.append(group);
  }
  select.value = currentValue ?? "";
}

function showFormError(form, error) {
  form.querySelector(".formError").textContent = error.message;
}

function openDialog(dialog, form) {
  form.querySelector(".formError").textContent = "";
  enhanceMoneyInputs(form);
  dialog.showModal();
}

async function loadReferenceData() {
  [accounts, categories, templates] = await Promise.all([api("/api/accounts"), api("/api/categories"), api("/api/templates")]);
  updateEntryGuidance();
}

function updateEntryGuidance() {
  // Use the same backend metadata and existing selectors as ordinary creation.
  // External, archived Accounts, and nonselectable Categories are not prerequisites.
  const accountMissing = activeAccountsWith().length === 0;
  const categoryMissing = selectableCategoriesWith().length === 0;
  for (const message of document.querySelectorAll("[data-entry-account-needed]")) message.hidden = !accountMissing;
  for (const message of document.querySelectorAll("[data-entry-category-needed]")) message.hidden = !categoryMissing;
  for (const message of document.querySelectorAll("[data-auditor-guidance]")) message.hidden = browserSession?.role !== "auditor";
  document.querySelector("#setupEmptyState").hidden = !accountMissing && !categoryMissing;
  document.querySelector("#transactionSetupHelp").hidden = Boolean(document.querySelector("#transactionId").value) || (!accountMissing && !categoryMissing);
}

function activeAccountsWith(currentId = null) {
  return accounts.filter((account) => account.accountClass !== "system" && (account.active || account.id === Number(currentId)));
}

function transferAccountsWith(currentId = null) {
  return accounts.filter((account) => account.active || account.id === Number(currentId));
}

function selectableCategoriesWith(currentId = null) {
  return categories.filter((category) => !category.hasChildren && (category.active || category.id === Number(currentId)));
}

function activeTemplates() {
  return templates.filter((template) => template.active
    && (!template.accountId || accounts.some((account) => account.id === template.accountId && account.active))
    && (!template.categoryId || categories.some((category) => category.id === template.categoryId && category.active && !category.hasChildren)));
}

async function refreshDailyPage() {
  await loadReferenceData();
  if (currentPath === "/") await loadDashboard();
  if (currentPath === "/transactions") {
    renderReferenceSelectors();
    await loadTransactions();
  }
  if (currentPath === "/budget") await loadBudget();
  if (currentPath === "/simulator") await loadSimulator(simulator?.plan?.id);
}

function renderReferenceSelectors() {
  setAccountOptions(document.querySelector("#accountFilter"), accounts, "All accounts", "", false);
  setMixedCategoryOptions(document.querySelector("#categoryFilter"), categories, "All categories");
}

function urlKey(name) {
  return name.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function restoreReferenceFilter(select, records, reference, label) {
  select.querySelector('[data-invalid-reference]')?.remove();
  if (reference === null) { select.value = ""; return ""; }
  // Old slugs contain no colon. Resolve them only when identity is unambiguous.
  const matches = reference.startsWith("id:")
    ? records.filter((record) => /^id:[1-9]\d*$/.test(reference) && `id:${record.id}` === reference)
    : records.filter((record) => urlKey(record.name) === reference);
  if (matches.length === 1) { select.value = String(matches[0].id); return ""; }
  const option = document.createElement("option");
  option.value = "invalid";
  option.dataset.invalidReference = reference;
  option.textContent = `Invalid ${label.toLowerCase()} filter`;
  select.append(option);
  select.value = option.value;
  return `${label} filter is ${matches.length > 1 ? "ambiguous" : "invalid or unavailable"}. Choose another ${label.toLowerCase()} or clear the filter.`;
}

function periodFromUrl(rawPeriod) {
  if (rawPeriod?.startsWith("month:")) return rawPeriod;
  return Object.keys(periodUrlValues).find((period) => periodUrlValues[period] === rawPeriod) || "last30Days";
}

function restoreTransactionFiltersFromUrl() {
  const urlFilters = new URLSearchParams(window.location.search);
  const period = periodFromUrl(urlFilters.get("period"));
  document.querySelector("#periodFilter").value = period;
  const custom = period === "custom";
  document.querySelector("#transactionCustomDates").hidden = !custom;
  document.querySelector("#startDateFilter").disabled = !custom;
  document.querySelector("#endDateFilter").disabled = !custom;
  document.querySelector("#startDateFilter").value = urlFilters.get("start") || "";
  document.querySelector("#endDateFilter").value = urlFilters.get("end") || "";
  const filterErrors = [
    restoreReferenceFilter(document.querySelector("#accountFilter"), accounts, urlFilters.get("account"), "Account"),
    restoreReferenceFilter(document.querySelector("#categoryFilter"), categories, urlFilters.get("category"), "Category"),
  ];
  document.querySelector("#descriptionFilter").value = urlFilters.get("search") || "";
  document.querySelector("#typeFilter").value = ["transactions", "transfers"].includes(urlFilters.get("type")) ? urlFilters.get("type") : "all";
  const rawAmountMode = urlFilters.get("amountMode");
  const amountMode = rawAmountMode || "any";
  const amountModeSelect = document.querySelector("#amountModeFilter");
  amountModeSelect.querySelector('[data-invalid-option]')?.remove();
  if (!["any", "exact", "lessThan", "greaterThan", "between", "withinDollar"].includes(amountMode)) {
    const invalidOption = document.createElement("option");
    invalidOption.value = amountMode;
    invalidOption.textContent = `Invalid mode: ${amountMode}`;
    invalidOption.dataset.invalidOption = "true";
    amountModeSelect.append(invalidOption);
  }
  amountModeSelect.value = amountMode;
  document.querySelector("#amountFilter").value = urlFilters.get("amount") || "";
  document.querySelector("#amountMinFilter").value = urlFilters.get("amountMin") || "";
  document.querySelector("#amountMaxFilter").value = urlFilters.get("amountMax") || "";
  updateAmountFilterPresentation();
  return filterErrors.filter(Boolean).join(" ");
}

function updateAmountFilterPresentation() {
  const mode = document.querySelector("#amountModeFilter").value;
  const active = mode !== "any";
  const between = mode === "between";
  document.querySelector("#amountFilterValues").hidden = !active;
  document.querySelector("#amountFilterSingleLabel").hidden = !active || between;
  document.querySelector("#amountFilterMinimumLabel").hidden = !between;
  document.querySelector("#amountFilterMaximumLabel").hidden = !between;
}

function transactionFilterUrlFromForm() {
  const urlFilters = new URLSearchParams();
  const period = document.querySelector("#periodFilter").value;
  urlFilters.set("period", periodUrlValues[period] || period);
  if (period === "custom") {
    const start = document.querySelector("#startDateFilter").value;
    const end = document.querySelector("#endDateFilter").value;
    if (start) urlFilters.set("start", start);
    if (end) urlFilters.set("end", end);
  }
  const account = accounts.find((item) => item.id === Number(document.querySelector("#accountFilter").value));
  const category = categories.find((item) => item.id === Number(document.querySelector("#categoryFilter").value));
  const search = document.querySelector("#descriptionFilter").value.trim();
  if (account) urlFilters.set("account", `id:${account.id}`);
  if (category) urlFilters.set("category", `id:${category.id}`);
  // Applying another control must not silently discard an unresolved URL filter.
  for (const [field, parameter] of [["#accountFilter", "account"], ["#categoryFilter", "category"]]) {
    const invalidReference = document.querySelector(field).selectedOptions[0]?.dataset.invalidReference;
    if (invalidReference !== undefined) urlFilters.set(parameter, invalidReference);
  }
  if (search) urlFilters.set("search", search);
  if (document.querySelector("#typeFilter").value !== "all") urlFilters.set("type", document.querySelector("#typeFilter").value);
  const amountMode = document.querySelector("#amountModeFilter").value;
  if (amountMode !== "any") {
    urlFilters.set("amountMode", amountMode);
    if (amountMode === "between") {
      urlFilters.set("amountMin", document.querySelector("#amountMinFilter").value.trim());
      urlFilters.set("amountMax", document.querySelector("#amountMaxFilter").value.trim());
    } else {
      urlFilters.set("amount", document.querySelector("#amountFilter").value.trim());
    }
  }
  const currentFilters = new URLSearchParams(window.location.search);
  if (currentFilters.get("sort")) urlFilters.set("sort", currentFilters.get("sort"));
  if (currentFilters.get("direction")) urlFilters.set("direction", currentFilters.get("direction"));
  const query = urlFilters.toString();
  return `/transactions${query ? `?${query}` : ""}`;
}

async function loadTransactions() {
  const filterSearch = window.location.search;
  const filterError = restoreTransactionFiltersFromUrl();
  const urlFilters = new URLSearchParams(window.location.search);
  const query = new URLSearchParams();
  const period = document.querySelector("#periodFilter").value;
  query.set("period", period);
  if (period === "custom") {
    query.set("startDate", urlFilters.get("start") || "");
    query.set("endDate", urlFilters.get("end") || "");
  }
  for (const [field, parameter] of [["#accountFilter", "accountId"], ["#categoryFilter", "categoryId"], ["#descriptionFilter", "description"]]) {
    const value = document.querySelector(field).value;
    if (value) query.set(parameter, value);
  }
  query.set("sort", urlFilters.get("sort") || "date");
  query.set("direction", urlFilters.get("direction") || "desc");
  query.set("type", document.querySelector("#typeFilter").value);
  query.set("includeSummary", "true");
  const amountMode = document.querySelector("#amountModeFilter").value;
  if (amountMode !== "any") {
    query.set("amountMode", amountMode);
    if (amountMode === "between") {
      query.set("amountMin", document.querySelector("#amountMinFilter").value);
      query.set("amountMax", document.querySelector("#amountMaxFilter").value);
    } else query.set("amount", document.querySelector("#amountFilter").value);
  }
  try {
    if (filterError) throw new Error(filterError);
    const result = await api(`/api/transactions?${query}`);
    if (window.location.search !== filterSearch) return;
    transactions = result.transactions;
    document.querySelector("#transactionExpenseTotal").textContent = dollars(result.summary.expenseCents);
    document.querySelector("#transactionIncomeTotal").textContent = dollars(result.summary.incomeCents);
    document.querySelector("#transactionTransferTotal").textContent = dollars(result.summary.transferPaymentCents);
  } catch (error) {
    if (window.location.search !== filterSearch) return;
    transactions = [];
    document.querySelector("#transactionRows").replaceChildren();
    for (const id of ["#transactionExpenseTotal", "#transactionIncomeTotal", "#transactionTransferTotal"]) document.querySelector(id).textContent = "—";
    document.querySelector("#transactionStatus").textContent = error.message;
    return;
  }
  const body = document.querySelector("#transactionRows");
  body.replaceChildren();
  for (const transaction of transactions) {
    const row = body.insertRow();
    for (const value of [friendlyCalendarDate(transaction.date), transaction.accountName, transaction.description, transaction.categoryName || "—"]) {
      row.insertCell().textContent = value;
    }
    const amountCell = row.insertCell();
    amountCell.className = "amount";
    amountCell.textContent = dollars(transaction.displayAmountCents ?? transaction.amountCents, !transaction.categoryId && !transaction.combinedTransfer);
    const actions = row.insertCell();
    actions.className = "rowActions";
    const duplicateButton = document.createElement("button");
    duplicateButton.type = "button";
    duplicateButton.textContent = "Duplicate";
    const ordinaryEligible = !transaction.transferId && accounts.some((item)=>item.id===transaction.accountId&&item.active) && categories.some((item)=>item.id===transaction.categoryId&&item.active&&!item.hasChildren);
    const transferEligible = Boolean(transaction.transferId);
    duplicateButton.hidden = !ordinaryEligible && !transferEligible;
    duplicateButton.addEventListener("click", () => duplicateTransaction(transaction));
    const simulatorButton = document.createElement("button");
    simulatorButton.type = "button";
    simulatorButton.textContent = "Send to Simulator";
    simulatorButton.hidden = !transaction.simulatorEligible;
    simulatorButton.addEventListener("click", () => openSendToSimulator(transaction));
    const editButton = document.createElement("button");
    editButton.type = "button";
    editButton.textContent = "Edit";
    editButton.addEventListener("click", () => transaction.transferId ? editTransfer(transaction.transferId) : editOrdinaryTransaction(transaction));
    actions.append(simulatorButton, duplicateButton, editButton);
  }
  document.querySelector("#transactionStatus").textContent = `${transactions.length} transaction${transactions.length === 1 ? "" : "s"}`;
  for (const button of document.querySelectorAll("[data-sort]")) {
    const active = button.dataset.sort === query.get("sort");
    button.textContent = button.dataset.sort[0].toUpperCase() + button.dataset.sort.slice(1) + (active ? ` ${query.get("direction") === "asc" ? "▲" : "▼"}` : "");
  }
}

async function duplicateTransaction(transaction) {
  if (!transaction.transferId) {
    await newTransaction();
    document.querySelector("#transactionAccount").value=transaction.accountId;
    document.querySelector("#transactionDescription").value=transaction.description;
    document.querySelector("#transactionCategory").value=transaction.categoryId;
    setMoneyInputValue(document.querySelector("#transactionAmount"),(transaction.enteredAmountCents/100).toFixed(2));
    document.querySelector("#transactionPinned").checked=false;
    return;
  }
  try {
    const transfer=await api(`/api/transfers/${transaction.transferId}`);
    const eligible=accounts.some((item)=>item.id===transfer.fromAccountId&&item.active)&&accounts.some((item)=>item.id===transfer.toAccountId&&item.active);
    if(!eligible)return;
    newTransfer();
    document.querySelector("#transferFromAccount").value=transfer.fromAccountId;
    document.querySelector("#transferToAccount").value=transfer.toAccountId;
    setMoneyInputValue(document.querySelector("#transferAmount"),(transfer.amountCents/100).toFixed(2));
    document.querySelector("#transferDescription").value=transfer.description;
    updateTransferPresentation(false);
  } catch(error){document.querySelector("#transactionStatus").textContent=error.message;}
}

async function openSendToSimulator(transaction) {
  const form = document.querySelector("#sendToSimulatorForm");
  form.reset();
  showFormError(form, new Error(""));
  const query = transaction.transferId
    ? `transferId=${encodeURIComponent(transaction.transferId)}`
    : `transactionId=${encodeURIComponent(transaction.id)}`;
  try {
    const result = await api(`/api/simulator/send-preview?${query}`);
    document.querySelector("#sendSimulatorTransactionId").value = transaction.transferId ? "" : transaction.id;
    document.querySelector("#sendSimulatorTransferId").value = transaction.transferId || "";
    setOptions(document.querySelector("#sendSimulatorPlan"), result.plans.map((plan) => ({...plan, name:`${friendlyCalendarDate(plan.planDate)} — ${plan.name}`, active:true})), "Select plan");
    const preview = document.querySelector("#sendSimulatorPreview");
    preview.replaceChildren();
    const details = [["Type", result.preview.rowTypeLabel]];
    if (result.preview.rowType === "transfer") details.push(["From Account", result.preview.fromAccountName], ["To Account", result.preview.toAccountName]);
    else details.push(["Account", result.preview.accountName]);
    details.push(["Label", result.preview.label]);
    if (result.preview.categoryName) details.push(["Category", result.preview.categoryName]);
    details.push(["Amount", dollars(result.preview.amountCents)]);
    for (const [name, value] of details) {
      const wrapper=document.createElement("div"),term=document.createElement("dt"),description=document.createElement("dd");
      term.textContent=name;description.textContent=value;wrapper.append(term,description);preview.append(wrapper);
    }
    openDialog(document.querySelector("#sendToSimulatorDialog"), form);
  } catch (error) { document.querySelector("#transactionStatus").textContent = error.message; }
}

async function sendToSimulator(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    planId: document.querySelector("#sendSimulatorPlan").value,
    transactionId: document.querySelector("#sendSimulatorTransactionId").value || null,
    transferId: document.querySelector("#sendSimulatorTransferId").value || null,
  };
  try {
    const result = await api("/api/simulator/send", {method:"POST", body:JSON.stringify(payload)});
    document.querySelector("#sendToSimulatorDialog").close();
    sendNotification(`Sent — Added to ${result.planName}.`);
  } catch (error) { showFormError(form, error); }
}

async function sortTransactions(event) {
  const selectedSort = event.currentTarget.dataset.sort;
  const urlFilters = new URLSearchParams(window.location.search);
  const currentSort = urlFilters.get("sort");
  const firstDirection = ["date", "amount"].includes(selectedSort) ? "desc" : "asc";
  const direction = currentSort === selectedSort ? urlFilters.get("direction") === "asc" ? "desc" : "asc" : firstDirection;
  urlFilters.set("sort", selectedSort); urlFilters.set("direction", direction);
  window.history.pushState({}, "", `/transactions?${urlFilters}`);
  await loadTransactions();
}

async function applyTransactionFilterForm(event) {
  event.preventDefault();
  window.history.pushState({}, "", transactionFilterUrlFromForm());
  await loadTransactions();
}

async function refreshTemplateSuggestions(preferredTemplateId = null) {
  const select = document.querySelector("#transactionTemplate");
  const selectedDate = document.querySelector("#transactionDate").value;
  if (!selectedDate) return;
  const suggestion = await api(`/api/budget/template-suggestions?date=${encodeURIComponent(selectedDate)}`);
  const availableTemplates = activeTemplates();
  const linkedIds = new Set(suggestion.linkedTemplateIds);
  const suggestedIds = new Set(suggestion.suggestedTemplateIds);
  const suggested = availableTemplates.filter((template) => suggestedIds.has(template.id));
  const normal = availableTemplates.filter((template) => !linkedIds.has(template.id));
  const outsideWindow = availableTemplates.filter((template) => linkedIds.has(template.id) && !suggestedIds.has(template.id));
  select.replaceChildren(); option(select, "", "None");
  const appendGroup = (label, rows, includeReason = false) => {
    if (!rows.length) return;
    const group = document.createElement("optgroup"); group.label = label;
    for (const template of rows) {
      const reason = includeReason ? suggestion.suggestionReasons[String(template.id)] : null;
      option(group, template.id, reason ? `${template.name} (${reason})` : template.name);
    }
    select.append(group);
  };
  appendGroup("Suggested", suggested, true);
  appendGroup("Templates", normal);
  if (document.querySelector("#showAllTemplates").checked) appendGroup("Other planned templates", outsideWindow);
  document.querySelector("#showAllTemplatesLabel").hidden = outsideWindow.length === 0;
  if (preferredTemplateId !== null) select.value = String(preferredTemplateId);
}

async function newTransaction(preferredTemplateId = null) {
  const form = document.querySelector("#transactionForm");
  form.reset();
  document.querySelector("#transactionId").value = "";
  configurePendingEntry("Transaction", false);
  updateEntryGuidance();
  form.dataset.previousAccountId = "";
  form.dataset.simulatorLinked = "false";
  document.querySelector("#deleteTransactionButton").hidden = true;
  document.querySelector("#transactionDate").value = today;
  document.querySelector("#transactionDialogTitle").textContent = "New Transaction";
  setAccountOptions(document.querySelector("#transactionAccount"), activeAccountsWith(), "Select account");
  setMixedCategoryOptions(document.querySelector("#transactionCategory"), selectableCategoriesWith(), "Select category");
  setOptions(document.querySelector("#transactionTemplate"), [], "None");
  document.querySelector("#showAllTemplatesLabel").hidden = false;
  setAccountOptions(document.querySelector("#reimbursementAccount"), activeAccountsWith(), "Select account");
  updateReimbursementAvailability();
  document.querySelector("#transactionAmountHint").textContent = "Enter ordinary expenses and income as positive amounts. Use a negative amount for an Expense refund or an Income reversal.";
  openDialog(document.querySelector("#transactionDialog"), form);
  if (preferredTemplateId !== null) document.querySelector("#showAllTemplates").checked = true;
  try {
    await refreshTemplateSuggestions(preferredTemplateId);
    if (preferredTemplateId !== null) document.querySelector("#transactionTemplate").dispatchEvent(new Event("change"));
  } catch (error) {
    setOptions(document.querySelector("#transactionTemplate"), activeTemplates(), "None", preferredTemplateId);
  }
}

function addBulkEntryRow(hanging = true) {
  const row = document.querySelector("#bulkEntryRows").insertRow();
  if (hanging) row.classList.add("bulkHangingRow");
  const dateInput = document.createElement("input"); dateInput.type = "date"; dateInput.value = today;
  const accountSelect = document.createElement("select"); setAccountOptions(accountSelect, activeAccountsWith(), "Select account");
  const descriptionInput = document.createElement("input");
  const categorySelect = document.createElement("select"); setMixedCategoryOptions(categorySelect, selectableCategoriesWith(), "Select category");
  const amountInput = document.createElement("input"); amountInput.inputMode = "decimal"; amountInput.placeholder = "0.00";
  for (const control of [dateInput, accountSelect, descriptionInput, categorySelect, amountInput]) row.insertCell().append(control);
  const actionCell = row.insertCell();
  const finishRow = () => {
    if (!row.classList.contains("bulkHangingRow")) return;
    const complete = dateInput.value && accountSelect.value && descriptionInput.value.trim()
      && categorySelect.value && amountInput.value.trim();
    if (!complete) return;
    row.classList.remove("bulkHangingRow");
    const removeButton = document.createElement("button"); removeButton.type = "button"; removeButton.textContent = "Remove";
    removeButton.addEventListener("click", () => row.remove()); actionCell.append(removeButton);
    addBulkEntryRow(true);
  };
  row.addEventListener("input", finishRow); row.addEventListener("change", finishRow);
}

function newBulkEntry() {
  const form = document.querySelector("#bulkEntryForm"); form.reset();
  document.querySelector("#bulkEntryRows").replaceChildren();
  addBulkEntryRow(true);
  openDialog(document.querySelector("#bulkEntryDialog"), form);
}

async function saveBulkEntry(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const rows = [...document.querySelectorAll("#bulkEntryRows tr")].map((row) => {
    const controls = row.querySelectorAll("input, select");
    return {date: controls[0].value, accountId: controls[1].value, description: controls[2].value, categoryId: controls[3].value, amount: controls[4].value};
  });
  try {
    await api("/api/transactions/bulk", {method: "POST", body: JSON.stringify({rows})});
    document.querySelector("#bulkEntryDialog").close();
    await refreshDailyPage();
  } catch (error) { showFormError(form, error); }
}

function editOrdinaryTransaction(transaction) {
  const form = document.querySelector("#transactionForm");
  form.reset();
  document.querySelector("#transactionId").value = transaction.id;
  configurePendingEntry("Transaction", true, transaction.categoryId ? `/api/transactions/${transaction.id}` : null);
  document.querySelector("#transactionSetupHelp").hidden = true;
  form.dataset.previousAccountId = transaction.accountId;
  form.dataset.originalValues = JSON.stringify({date:transaction.date,accountId:String(transaction.accountId),description:transaction.description,categoryId:String(transaction.categoryId||""),amount:(transaction.enteredAmountCents/100).toFixed(2),pinned:Boolean(transaction.pinned),alsoLogReimbursement:false});
  document.querySelector("#deleteTransactionButton").hidden = false;
  document.querySelector("#transactionDate").value = transaction.date;
  document.querySelector("#transactionDescription").value = transaction.description;
  document.querySelector("#transactionAmount").value = (transaction.enteredAmountCents / 100).toFixed(2);
  document.querySelector("#transactionPinned").checked = Boolean(transaction.pinned);
  document.querySelector("#transactionDialogTitle").textContent = "Edit Transaction";
  setAccountOptions(document.querySelector("#transactionAccount"), activeAccountsWith(transaction.accountId), "Select account", transaction.accountId);
  setMixedCategoryOptions(document.querySelector("#transactionCategory"), selectableCategoriesWith(transaction.categoryId), transaction.categoryId ? "Select category" : "Uncategorized", transaction.categoryId);
  setOptions(document.querySelector("#transactionTemplate"), activeTemplates(), "None");
  document.querySelector("#showAllTemplatesLabel").hidden = true;
  document.querySelector("#alsoLogReimbursementLabel").hidden = true;
  document.querySelector("#reimbursementFields").hidden = true;
  document.querySelector("#transactionAmountHint").textContent = transaction.categoryId ? "Enter ordinary expenses and income as positive amounts. Use a negative amount for an Expense refund or an Income reversal." : "Uncategorized history uses the actual signed ledger amount.";
  openDialog(document.querySelector("#transactionDialog"), form);
}

async function saveTransaction(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const transactionId = document.querySelector("#transactionId").value;
  const alsoLogReimbursement = !transactionId && document.querySelector("#alsoLogReimbursement").checked;
  const payload = {date: document.querySelector("#transactionDate").value, accountId: document.querySelector("#transactionAccount").value, description: document.querySelector("#transactionDescription").value, categoryId: document.querySelector("#transactionCategory").value, amount: document.querySelector("#transactionAmount").value, pinned: document.querySelector("#transactionPinned").checked, alsoLogReimbursement};
  if (alsoLogReimbursement) payload.reimbursement = {accountId: document.querySelector("#reimbursementAccount").value, description: document.querySelector("#reimbursementDescription").value, amount: document.querySelector("#reimbursementAmount").value, date: document.querySelector("#reimbursementDate").value};
  const body = JSON.stringify(payload);
  try {
    const simulatorRecordPath = simulatorRecordingRowId && !transactionId ? `/api/simulator/plans/${simulator.plan.id}/rows/${simulatorRecordingRowId}/transaction` : null;
    await api(transactionId ? `/api/transactions/${transactionId}` : simulatorRecordPath || "/api/transactions", {method: transactionId ? "PATCH" : "POST", body});
    const unchanged = transactionId && form.dataset.originalValues === JSON.stringify({...payload,accountId:String(payload.accountId),categoryId:String(payload.categoryId||""),amount:submittedMoneyValue(payload.amount)});
    if (!unchanged) {
      const affectedAccountIds = [payload.accountId];
      if (transactionId && form.dataset.previousAccountId && Number(form.dataset.previousAccountId) !== Number(payload.accountId)) affectedAccountIds.push(form.dataset.previousAccountId);
      await ledgerNotification(transactionId ? "Saved - Transaction updated" : "Saved - Transaction added", affectedAccountIds);
    }
    simulatorRecordingRowId = null;
    document.querySelector("#transactionDialog").close();
    if (currentPath === "/new") window.location.assign("/");
    else await refreshDailyPage();
  } catch (error) { showFormError(form, error); }
}

async function removeEditedTransaction() { const form=document.querySelector("#transactionForm"),transactionId=document.querySelector("#transactionId").value;if(!transactionId)return;const linked=form.dataset.simulatorLinked==="true";if(!confirm(`Delete this transaction?${linked?" The linked Simulator row will return to Planned.":""}`))return;const accountId=document.querySelector("#transactionAccount").value;try{await api(`/api/transactions/${transactionId}`,{method:"DELETE"});document.querySelector("#transactionDialog").close();await ledgerNotification("Deleted - Transaction deleted",[accountId]);await refreshDailyPage();}catch(error){showFormError(form,error);}}

function newTransfer() {
  const form = document.querySelector("#transferForm");
  form.reset();
  document.querySelector("#transferId").value = "";
  configurePendingEntry("Transfer", false);
  form.dataset.simulatorLinked = "false";
  document.querySelector("#deleteTransferButton").hidden = true;
  document.querySelector("#transferDate").value = today;
  document.querySelector("#transferDescription").value = "Transfer";
  document.querySelector("#transferDialogTitle").textContent = "New Transfer";
  setAccountOptions(document.querySelector("#transferFromAccount"), transferAccountsWith(), "Select account");
  setAccountOptions(document.querySelector("#transferToAccount"), transferAccountsWith(), "Select account");
  updateTransferPresentation(false);
  openDialog(document.querySelector("#transferDialog"), form);
}

function paymentDescription(account) {
  const name = account.name.trim();
  const needsCardSuffix = account.accountSubtype !== "Personal Loan" && !/\bcard$/i.test(name);
  return `${name}${needsCardSuffix ? " Card" : ""} Payment`;
}

function updateTransferPresentation(updateDescription = true) {
  const destinationSelect = document.querySelector("#transferToAccount");
  const destination = accounts.find((account) => account.id === Number(destinationSelect.value));
  const isPayment = destinationSelect.selectedOptions[0]?.dataset.accountClass === "credit";
  const paymentHint = document.querySelector("#transferPaymentHint");
  for(const [selectId,lineId] of [["#transferFromAccount","#transferFromBalance"],["#transferToAccount","#transferToBalance"]]){const selected=accounts.find((account)=>account.id===Number(document.querySelector(selectId).value));const line=document.querySelector(lineId);line.hidden=!selected||selected.accountClass==="system";if(selected&&selected.accountClass!=="system"){const friendly=selected.accountClass==="credit"?(selected.balanceCents>0?`${dollars(selected.balanceCents)} credit`:dollars(-selected.balanceCents)):dollars(selected.balanceCents);line.textContent=`Current Balance: ${friendly}`;}}
  paymentHint.hidden = !isPayment;
  paymentHint.replaceChildren();
  if (isPayment) {
    paymentHint.append(`This will be recorded as a payment to ${destination.name}. `);
  }
  if (updateDescription) {
    document.querySelector("#transferDescription").value = isPayment
      ? paymentDescription(destination)
      : destination ? `Transfer to ${destination.name}` : "Transfer";
  }
  refreshTransferAmountHelper();
}

async function refreshTransferAmountHelper() {
  // A later account selection or dialog reset invalidates earlier helper responses.
  const requestId = ++transferHelperRequest;
  const fromAccountId = document.querySelector("#transferFromAccount").value;
  const toAccountId = document.querySelector("#transferToAccount").value;
  const transferId = document.querySelector("#transferId").value;
  if (!fromAccountId || !toAccountId) return;
  const isCurrentRequest = () => requestId === transferHelperRequest && document.querySelector("#transferDialog").open
    && document.querySelector("#transferFromAccount").value === fromAccountId
    && document.querySelector("#transferToAccount").value === toAccountId
    && document.querySelector("#transferId").value === transferId;
  try {
    const result = await api(`/api/transfer-amount-helper?fromAccountId=${encodeURIComponent(fromAccountId)}&toAccountId=${encodeURIComponent(toAccountId)}&editing=${Boolean(transferId)}`);
    if (!isCurrentRequest() || !result.eligible) return;
    const hint = document.querySelector("#transferPaymentHint");
    hint.hidden = false;
    const helper = document.createElement("button");
    helper.type = "button";
    helper.className = "transferAmountHelper";
    helper.textContent = result.label;
    helper.setAttribute("aria-label", result.label);
    helper.addEventListener("click", () => setMoneyInputValue(document.querySelector("#transferAmount"), (result.amountCents / 100).toFixed(2)));
    hint.append(helper);
  } catch (error) {
    if (isCurrentRequest()) showFormError(document.querySelector("#transferForm"), error);
  }
}

async function editTransfer(transferId) {
  try {
    const transfer = await api(`/api/transfers/${transferId}`);
    const form = document.querySelector("#transferForm");
    form.reset();
    document.querySelector("#transferId").value = transferId;
    configurePendingEntry("Transfer", true, `/api/transfers/${transferId}`);
    form.dataset.originalValues=JSON.stringify({date:transfer.date,fromAccountId:String(transfer.fromAccountId),toAccountId:String(transfer.toAccountId),amount:(transfer.amountCents/100).toFixed(2),description:transfer.description});
    document.querySelector("#deleteTransferButton").hidden = false;
    document.querySelector("#transferDate").value = transfer.date;
    document.querySelector("#transferAmount").value = (transfer.amountCents / 100).toFixed(2);
    document.querySelector("#transferDescription").value = transfer.description;
    document.querySelector("#transferDialogTitle").textContent = "Edit Transfer";
    setAccountOptions(document.querySelector("#transferFromAccount"), transferAccountsWith(transfer.fromAccountId), "Select account", transfer.fromAccountId);
    setAccountOptions(document.querySelector("#transferToAccount"), transferAccountsWith(transfer.toAccountId), "Select account", transfer.toAccountId);
    updateTransferPresentation(false);
    openDialog(document.querySelector("#transferDialog"), form);
  } catch (error) { document.querySelector("#transactionStatus").textContent = error.message; }
}

async function saveTransfer(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const transferId = document.querySelector("#transferId").value;
  const payload = {date: document.querySelector("#transferDate").value, fromAccountId: document.querySelector("#transferFromAccount").value, toAccountId: document.querySelector("#transferToAccount").value, amount: document.querySelector("#transferAmount").value, description: document.querySelector("#transferDescription").value};
  const body = JSON.stringify(payload);
  const comparisonBody = JSON.stringify({...payload, amount: submittedMoneyValue(payload.amount)});
  try { const simulatorRecordPath=simulatorRecordingRowId&&!transferId?`/api/simulator/plans/${simulator.plan.id}/rows/${simulatorRecordingRowId}/transfer`:null;await api(transferId ? `/api/transfers/${transferId}` : simulatorRecordPath||"/api/transfers", {method: transferId ? "PATCH" : "POST", body});if(!transferId||form.dataset.originalValues!==comparisonBody)await ledgerNotification(transferId?"Saved - Transfer updated":"Saved - Transfer added",[document.querySelector("#transferFromAccount").value,document.querySelector("#transferToAccount").value]);simulatorRecordingRowId=null; document.querySelector("#transferDialog").close(); await refreshDailyPage(); }
  catch (error) { showFormError(form, error); }
}

async function removeEditedTransfer(){const form=document.querySelector("#transferForm"),transferId=document.querySelector("#transferId").value;if(!transferId)return;const linked=form.dataset.simulatorLinked==="true";if(!confirm(`Delete this transfer? Both ledger rows will be deleted.${linked?" The linked Simulator row will return to Planned.":""}`))return;const accountIds=[document.querySelector("#transferFromAccount").value,document.querySelector("#transferToAccount").value];try{await api(`/api/transfers/${transferId}`,{method:"DELETE"});document.querySelector("#transferDialog").close();await ledgerNotification("Deleted - Transfer deleted",accountIds);await refreshDailyPage();}catch(error){showFormError(form,error);}}

function applyTransactionTemplate(event) {
  const template = templates.find((item) => item.id === Number(event.target.value));
  if (!template) {
    document.querySelector("#transactionAccount").value = "";
    document.querySelector("#transactionDescription").value = "";
    document.querySelector("#transactionCategory").value = "";
    document.querySelector("#transactionAmount").value = "";
    updateReimbursementAvailability();
    return;
  }
  document.querySelector("#transactionAccount").value = template.accountId || "";
  document.querySelector("#transactionDescription").value = template.description || "";
  document.querySelector("#transactionCategory").value = template.categoryId || "";
  setMoneyInputValue(document.querySelector("#transactionAmount"), template.prefillAmount && template.enteredAmountCents !== null ? (template.enteredAmountCents / 100).toFixed(2) : "");
  updateReimbursementAvailability();
}

function updateReimbursementAvailability() {
  const category = categories.find((item) => item.id === Number(document.querySelector("#transactionCategory").value));
  const available = !document.querySelector("#transactionId").value && category?.offerReimbursement && !category.hasChildren;
  document.querySelector("#alsoLogReimbursementLabel").hidden = !available;
  if (!available) document.querySelector("#alsoLogReimbursement").checked = false;
  updateReimbursementFields();
}

function updateReimbursementFields() {
  updatePendingEntryAvailability();
  const enabled = !document.querySelector("#alsoLogReimbursementLabel").hidden && document.querySelector("#alsoLogReimbursement").checked;
  const fields = document.querySelector("#reimbursementFields");
  fields.hidden = !enabled;
  for (const control of fields.querySelectorAll("input, select")) control.required = enabled;
  if (enabled) {
    if (!document.querySelector("#reimbursementAmount").value) document.querySelector("#reimbursementAmount").value = document.querySelector("#transactionAmount").value;
    if (!document.querySelector("#reimbursementDate").value) document.querySelector("#reimbursementDate").value = document.querySelector("#transactionDate").value;
  }
}

function percentage(value) {
  return value === null ? "Unavailable" : `${(value / 100).toFixed(2)}%`;
}

function addDashboardAmountRow(body, name, amountCents, percentageHundredths, perDayCents, className = "") {
  const row = body.insertRow();
  row.className = className;
  row.insertCell().textContent = name;
  for (const value of [dollars(amountCents), percentage(percentageHundredths), dollars(perDayCents)]) {
    const cell = row.insertCell(); cell.className = "amount"; cell.textContent = value;
  }
}

function renderDashboardAccounts(dashboard) {
  const hideZero = document.querySelector("#hideZeroBalances").checked;
  const cashRows = document.querySelector("#cashAccountRows");
  const creditRows = document.querySelector("#creditAccountRows");
  cashRows.replaceChildren(); creditRows.replaceChildren();
  let cashTotal = 0;
  const orderedAccounts = [...dashboard.accounts].sort((left, right) => Number(left.balanceCents === 0) - Number(right.balanceCents === 0));
  for (const account of orderedAccounts) {
    if (account.accountClass === "cash") {
      cashTotal += account.balanceCents;
      if (hideZero && account.balanceCents === 0 && account.minimumStatus?.state !== "red") continue;
      const row = cashRows.insertRow(); const accountCell=row.insertCell();accountCell.textContent = account.name;if(account.minimumStatus)accountCell.append(" ",statusBadge(account.minimumStatus));
      const balance = row.insertCell(); balance.className = "amount"; balance.textContent = dollars(account.balanceCents);
    } else {
      if (hideZero && account.balanceCents === 0) continue;
      const row = creditRows.insertRow(); const accountCell = row.insertCell(); accountCell.textContent = account.name;
      const balance = row.insertCell(); balance.className = "amount";
      balance.textContent = account.balanceCents > 0 ? `${dollars(account.balanceCents)} credit` : dollars(-account.balanceCents);
      if (account.accountSubtype === "Credit Card" && account.creditLimitCents) accountCell.append(" ", utilizationBadge(Math.max(0, -account.balanceCents), account.creditLimitCents, `${account.name} utilization`));
    }
  }
  const banner=document.querySelector("#minimumBalanceBanner"),lines=document.querySelector("#minimumBalanceAlertLines");lines.replaceChildren();for(const alert of dashboard.minimumBalanceAlerts){const line=document.createElement("div");line.textContent=`${alert.accountName}: ${alert.label}`;lines.append(line);}banner.hidden=dashboard.minimumBalanceAlerts.length===0;
  const utilizationBanner=document.querySelector("#creditUtilizationBanner"),utilizationLines=document.querySelector("#creditUtilizationAlertLines");utilizationLines.replaceChildren();for(const alert of dashboard.creditUtilizationAlerts){const line=document.createElement("div");line.textContent=`${alert.accountName}: ${dollars(alert.balanceCents)} (${alert.utilizationLabel}) — ${alert.warningLabel} warning reached`;utilizationLines.append(line);}utilizationBanner.hidden=dashboard.creditUtilizationAlerts.length===0;
  document.querySelector("#cashTotal").textContent = dollars(cashTotal);
  document.querySelector("#totalCreditBalance").textContent = dashboard.totalCreditBalanceCents >= 0
    ? dollars(dashboard.totalCreditBalanceCents)
    : `${dollars(-dashboard.totalCreditBalanceCents)} credit`;
  const totalBadge = document.querySelector("#totalCreditUtilization");
  document.querySelector("#totalCreditUtilizationRow").hidden = !dashboard.totalCreditLimitCents;
  if (dashboard.totalCreditLimitCents) setUtilizationBadge(totalBadge, dashboard.totalRevolvingOwedCents, dashboard.totalCreditLimitCents, "Total revolving utilization");
}

function setUtilizationBadge(element, owedCents, limitCents, label) {
  const value = owedCents * 100 / limitCents;
  element.textContent = `${value.toFixed(2)}%`;
  element.className = `utilizationBadge ${value < 30 ? "utilizationGreen" : value < 50 ? "utilizationYellow" : "utilizationRed"}`;
  element.setAttribute("aria-label", `${label}: ${value.toFixed(2)}%`);
}

function utilizationBadge(owedCents, limitCents, label) {
  const badge = document.createElement("span");
  setUtilizationBadge(badge, owedCents, limitCents, label);
  return badge;
}

function renderDashboardCategories(dashboard) {
  const body = document.querySelector("#expenseCategoryRows"); body.replaceChildren();
  const hideZero = document.querySelector("#hideZeroBalances").checked;
  for (const category of dashboard.categories.filter((item) => item.parentCategoryId === null)) {
    const children = dashboard.categories.filter((item) => item.parentCategoryId === category.id);
    if (children.length) {
      if (!hideZero || children.some((item) => item.amountCents !== 0)) addDashboardAmountRow(body, category.name, category.amountCents, category.percentageHundredths, category.perDayCents, "needsRollup");
      for (const child of children) if (!hideZero || child.amountCents !== 0) addDashboardAmountRow(body, child.name, child.amountCents, child.percentageHundredths, child.perDayCents, "needsChild");
    } else if (!hideZero || category.amountCents !== 0) addDashboardAmountRow(body, category.name, category.amountCents, category.percentageHundredths, category.perDayCents);
  }
}

function renderDashboardExpenseAccounts(dashboard) {
  const body = document.querySelector("#expenseAccountRows"); body.replaceChildren();
  const hideZero = document.querySelector("#hideZeroBalances").checked;
  for (const account of dashboard.accountsByExpense) {
    if (hideZero && account.amountCents === 0) continue;
    const row = body.insertRow(); row.insertCell().textContent = account.name;
    for (const value of [dollars(account.amountCents), percentage(account.percentageHundredths)]) {
      const cell = row.insertCell(); cell.className = "amount"; cell.textContent = value;
    }
  }
}

function renderDashboardExpenseAccountsAndCategories(dashboard) {
  const body = document.querySelector("#expenseAccountCategoryRows"); body.replaceChildren();
  const hideZero = document.querySelector("#hideZeroBalances").checked;
  for (const account of dashboard.expensesByAccountAndCategory) {
    const visibleCategories = account.categories.filter((category) => !hideZero || !category.isZero);
    if (!visibleCategories.length) continue;
    const accountRow = body.insertRow(); accountRow.className = "expenseDetailGroup";
    const accountHeading = document.createElement("th"); accountHeading.scope = "row"; accountHeading.textContent = account.name; accountRow.append(accountHeading);
    const accountAmount = accountRow.insertCell(); accountAmount.className = "amount"; accountAmount.textContent = dollars(account.amountCents);
    for (const category of visibleCategories) {
      const row = body.insertRow(); row.className = "expenseDetailChild";
      row.insertCell().textContent = category.displayName;
      const amount = row.insertCell(); amount.className = "amount"; amount.textContent = dollars(category.amountCents);
    }
  }
}

function renderDashboardExpenseCategoriesAndAccounts(dashboard) {
  const body = document.querySelector("#expenseCategoryAccountRows"); body.replaceChildren();
  const hideZero = document.querySelector("#hideZeroBalances").checked;
  for (const category of dashboard.expensesByCategoryAndAccount) {
    const visibleAccounts = category.accounts.filter((account) => !hideZero || !account.isZero);
    if (!visibleAccounts.length) continue;
    const categoryRow = body.insertRow(); categoryRow.className = "expenseDetailGroup";
    const categoryHeading = document.createElement("th"); categoryHeading.scope = "row"; categoryHeading.textContent = category.displayName; categoryRow.append(categoryHeading);
    const categoryAmount = categoryRow.insertCell(); categoryAmount.className = "amount"; categoryAmount.textContent = dollars(category.amountCents);
    for (const account of visibleAccounts) {
      const row = body.insertRow(); row.className = "expenseDetailChild";
      row.insertCell().textContent = account.name;
      const amount = row.insertCell(); amount.className = "amount"; amount.textContent = dollars(account.amountCents);
    }
  }
}

function renderDashboardIncomeAccounts(dashboard) {
  const body = document.querySelector("#incomeAccountRows"); body.replaceChildren();
  const hideZero = document.querySelector("#hideZeroBalances").checked;
  for (const account of dashboard.accountsByIncome) {
    if (hideZero && account.amountCents === 0) continue;
    const row = body.insertRow(); row.insertCell().textContent = account.name;
    for (const value of [dollars(account.amountCents), percentage(account.percentageHundredths)]) {
      const cell = row.insertCell(); cell.className = "amount"; cell.textContent = value;
    }
  }
}

function renderDashboardNetChangeAccounts(dashboard) {
  const body = document.querySelector("#netChangeAccountRows"); body.replaceChildren();
  const hideZero = document.querySelector("#hideZeroBalances").checked;
  for (const group of dashboard.netChangeGroups) {
    const visibleAccounts = group.accounts.filter((account) => !hideZero || !account.isZero);
    if (!visibleAccounts.length) continue;
    const groupRow = body.insertRow(); groupRow.className = "expenseDetailGroup";
    const groupHeading = document.createElement("th"); groupHeading.scope = "row"; groupHeading.textContent = group.label; groupRow.append(groupHeading);
    const groupAmount = groupRow.insertCell(); groupAmount.className = "amount"; groupAmount.textContent = dollars(group.amountCents);
    for (const account of visibleAccounts) {
      const row = body.insertRow(); row.className = "expenseDetailChild"; row.insertCell().textContent = account.name;
      const amount = row.insertCell(); amount.className = "amount"; amount.textContent = dollars(account.amountCents);
    }
  }
}

function renderDashboardBalanceChangeAccounts(dashboard) {
  const body = document.querySelector("#balanceChangeAccountRows"); body.replaceChildren();
  const hideZero = document.querySelector("#hideZeroBalances").checked;
  for (const group of dashboard.balanceChangeGroups) {
    const visibleAccounts = group.accounts.filter((account) => !hideZero || !account.isZero);
    if (!visibleAccounts.length) continue;
    const groupRow = body.insertRow(); groupRow.className = "expenseDetailGroup";
    const groupHeading = document.createElement("th"); groupHeading.scope = "row"; groupHeading.textContent = group.label; groupRow.append(groupHeading);
    const groupAmount = groupRow.insertCell(); groupAmount.className = "amount"; groupAmount.textContent = dollars(group.amountCents, true);
    for (const account of visibleAccounts) {
      const row = body.insertRow(); row.className = "expenseDetailChild"; row.insertCell().textContent = account.name;
      const amount = row.insertCell(); amount.className = "amount"; amount.textContent = dollars(account.amountCents, true);
    }
  }
}

function showDashboardBreakdown(view) {
  for (const panel of document.querySelectorAll("[data-breakdown-panel]")) {
    panel.hidden = panel.dataset.breakdownPanel !== view;
  }
}

function renderRecentTransactions(rows) {
  document.querySelector("#recentEmptyState").hidden = rows.length > 0;
  document.querySelector("#recentEntryGuidance").hidden = rows.length > 0 || activeAccountsWith().length === 0 || selectableCategoriesWith().length === 0 || browserSession?.role === "auditor";
  const body = document.querySelector("#recentTransactionRows"); body.replaceChildren();
  for (const transaction of rows) {
    const row = body.insertRow();
    for (const value of [friendlyCalendarDate(transaction.date), transaction.accountName, transaction.description, transaction.categoryName || "—"]) row.insertCell().textContent = value;
    const amount = row.insertCell(); amount.className = "amount"; amount.textContent = dollars(transaction.displayAmountCents ?? transaction.amountCents, !transaction.categoryId && !transaction.combinedTransfer);
    const action = row.insertCell(); const edit = document.createElement("button"); edit.type = "button"; edit.textContent = "Edit";
    edit.addEventListener("click", () => transaction.transferId ? editTransfer(transaction.transferId) : editOrdinaryTransaction({...transaction, enteredAmountCents: transaction.categoryRole === "income" ? transaction.amountCents : transaction.categoryName ? -transaction.amountCents : transaction.amountCents}));
    action.append(edit);
  }
}

function renderQuickUpdates(rows) {
  const section = document.querySelector("#quickUpdatesSection");
  section.hidden = rows.length === 0;
  const body = document.querySelector("#quickUpdateRows"); body.replaceChildren();
  for (const transaction of rows) {
    const row = body.insertRow();
    for (const value of [friendlyCalendarDate(transaction.date), transaction.accountName, transaction.description, transaction.categoryName || "—"]) row.insertCell().textContent = value;
    const amount = row.insertCell(); amount.className = "amount"; amount.textContent = dollars(transaction.displayAmountCents ?? transaction.amountCents, !transaction.categoryId);
    const action = row.insertCell(); const edit = document.createElement("button"); edit.type = "button"; edit.textContent = "Edit";
    edit.addEventListener("click", () => editOrdinaryTransaction({...transaction, enteredAmountCents: transaction.categoryRole === "income" ? transaction.amountCents : transaction.categoryName ? -transaction.amountCents : transaction.amountCents}));
    action.append(edit);
  }
}

async function loadDashboard() {
  const query = new URLSearchParams({period: document.querySelector("#dashboardPeriod").value});
  if (query.get("period") === "custom") {
    query.set("startDate", document.querySelector("#dashboardStartDate").value);
    query.set("endDate", document.querySelector("#dashboardEndDate").value);
  }
  const dashboard = await api(`/api/dashboard?${query}`);
  renderDashboardAccounts(dashboard);
  renderDashboardCategories(dashboard);
  renderDashboardExpenseAccounts(dashboard);
  renderDashboardExpenseAccountsAndCategories(dashboard);
  renderDashboardExpenseCategoriesAndAccounts(dashboard);
  renderDashboardIncomeAccounts(dashboard);
  renderDashboardNetChangeAccounts(dashboard);
  renderDashboardBalanceChangeAccounts(dashboard);
  renderRecentTransactions(dashboard.recentTransactions);
  renderQuickUpdates(dashboard.quickUpdates);
  document.querySelector("#dashboardExpenses").textContent = dollars(dashboard.expensesCents);
  document.querySelector("#dashboardIncome").textContent = dollars(dashboard.incomeCents);
  document.querySelector("#dashboardNet").textContent = dollars(dashboard.netCents);
  document.querySelector("#dashboardSpendingPercentage").textContent = percentage(dashboard.spendingPercentageHundredths);
  document.querySelector("#dashboardPerDay").textContent = dollars(dashboard.perDayCents);
}

async function initializeDashboardPeriods() {
  const months = await api("/api/report-periods");
  for (const select of [document.querySelector("#dashboardPeriod"), document.querySelector("#periodFilter")]) {
    const customOption = select.querySelector('option[value="custom"]');
    for (const month of months) {
      const monthDate = new Date(`${month}-01T00:00:00`);
      const monthOption = document.createElement("option");
      monthOption.value = `month:${month}`;
      monthOption.textContent = monthDate.toLocaleDateString("en-US", {month: "long", year: "numeric"});
      select.insertBefore(monthOption, customOption);
    }
  }
}

function renderAccounts() {
  const cashBody = document.querySelector("#settingsCashAccountRows");
  const creditBody = document.querySelector("#settingsCreditAccountRows");
  const systemBody = document.querySelector("#settingsSystemAccountRows");
  cashBody.replaceChildren(); creditBody.replaceChildren(); systemBody.replaceChildren();
  for (const account of accounts.filter((item) => item.accountClass === "cash")) {
    const row = cashBody.insertRow();
    row.insertCell().textContent = account.name + (account.active ? "" : " (archived)");
    row.insertCell().textContent = account.accountSubtype || "—";
    const minimum = row.insertCell(); minimum.className = "amount minimumColumn"; minimum.textContent = account.minimumBalanceCents !== null ? dollars(account.minimumBalanceCents) : "—";
    row.insertCell().textContent = account.pinnedForSelection ? "Yes" : "No";
    const actions = row.insertCell(); actions.className = "rowActions";
    const edit = document.createElement("button"); edit.textContent = "Edit"; edit.addEventListener("click", () => editAccount(account)); actions.append(edit);
  }
  for (const account of accounts.filter((item) => item.accountClass === "credit")) {
    const row = creditBody.insertRow();
    row.insertCell().textContent = account.name + (account.active ? "" : " (archived)");
    row.insertCell().textContent = account.accountSubtype || "—";
    const creditLimit = row.insertCell(); creditLimit.className = "amount"; creditLimit.textContent = account.creditLimitCents === null ? "—" : dollars(account.creditLimitCents);
    const warning = row.insertCell(); warning.className = "amount"; warning.textContent = account.utilizationWarningBasisPoints === null ? "—" : `${Math.floor(account.utilizationWarningBasisPoints / 100)}.${String(account.utilizationWarningBasisPoints % 100).padStart(2, "0")}%`;
    row.insertCell().textContent = account.pinnedForSelection ? "Yes" : "No";
    const actions = row.insertCell(); actions.className = "rowActions";
    const edit = document.createElement("button"); edit.textContent = "Edit"; edit.addEventListener("click", () => editAccount(account)); actions.append(edit);
  }
  for (const account of accounts.filter((item) => item.accountClass === "system")) {
    const row = systemBody.insertRow();
    row.insertCell().textContent = account.name;
    row.insertCell().textContent = "Transfer boundary for money outside NPF";
  }
}

function updateCreditLimitVisibility() { const visible = document.querySelector("#accountClass").value === "credit" && document.querySelector("#accountSubtype").value === "Credit Card"; document.querySelector("#creditCardFields").hidden = !visible; if (!visible){document.querySelector("#creditLimit").value="";document.querySelector("#utilizationWarning").value="";} }
function updateAccountSubtypeOptions(currentValue = "") { const select = document.querySelector("#accountSubtype"); select.replaceChildren(); option(select, "", "None"); const cash=document.querySelector("#accountClass").value === "cash"; const values = !cash ? [["Credit Card", "Credit Card"], ["Personal Loan", "Personal Loan"], ["Other", "Other"]] : [["checking", "Checking"], ["savings", "Savings"], ["other", "Other"]]; for (const [value, label] of values) option(select, value, label); select.value = currentValue; document.querySelector("#cashMinimumFields").hidden=!cash; updateCreditLimitVisibility(); }
function newAccount() { document.querySelector("#accountPinnedForSelection").disabled = false; const form = document.querySelector("#accountForm"); form.reset(); document.querySelector("#accountId").value = ""; updateAccountSubtypeOptions(); document.querySelector("#accountDialogTitle").textContent = "New Account"; document.querySelector("#accountActiveLabel").hidden = true; document.querySelector("#openingBalanceFields").hidden = false; document.querySelector("#reconciliationFields").hidden=true; openDialog(document.querySelector("#accountDialog"), form); }
function editAccount(account) { const form = document.querySelector("#accountForm"); form.reset(); document.querySelector("#accountId").value = account.id; document.querySelector("#accountName").value = account.name; document.querySelector("#accountClass").value = account.accountClass; updateAccountSubtypeOptions(account.accountSubtype || ""); document.querySelector("#creditLimit").value = account.creditLimitCents ? (account.creditLimitCents / 100).toFixed(2) : "";document.querySelector("#utilizationWarning").value=account.utilizationWarningBasisPoints?`${Math.floor(account.utilizationWarningBasisPoints/100)}.${String(account.utilizationWarningBasisPoints%100).padStart(2,"0")}`:""; document.querySelector("#minimumBalance").value=account.minimumBalanceCents?(account.minimumBalanceCents/100).toFixed(2):"";document.querySelector("#minimumCautionPercent").value=account.minimumCautionPercent??25; document.querySelector("#accountActive").checked = Boolean(account.active); document.querySelector("#accountPinnedForSelection").checked = Boolean(account.pinnedForSelection); document.querySelector("#accountPinnedForSelection").disabled = !account.active; document.querySelector("#accountDialogTitle").textContent = "Edit Account"; document.querySelector("#accountActiveLabel").hidden = false; document.querySelector("#openingBalanceFields").hidden = true;document.querySelector("#reconciliationFields").hidden=false;document.querySelector("#actualBalance").value="";document.querySelector("#reconciliationStatus").textContent="";setReconciliationBalance(account); openDialog(document.querySelector("#accountDialog"), form); updateMinimumHelper(); }
async function saveAccount(event) { event.preventDefault(); const form = event.currentTarget; const id = document.querySelector("#accountId").value; const payload = {name: document.querySelector("#accountName").value, accountClass: document.querySelector("#accountClass").value, accountSubtype: document.querySelector("#accountSubtype").value, creditLimit: document.querySelector("#creditLimit").value,utilizationWarning:document.querySelector("#utilizationWarning").value, minimumBalance:document.querySelector("#minimumBalance").value,minimumCautionPercent:document.querySelector("#minimumCautionPercent").value, active: document.querySelector("#accountActive").checked, pinnedForSelection: document.querySelector("#accountPinnedForSelection").checked}; if (!id) payload.openingBalance = document.querySelector("#openingBalance").value; try { await api(id ? `/api/accounts/${id}` : "/api/accounts", {method: id ? "PATCH" : "POST", body: JSON.stringify(payload)}); document.querySelector("#accountDialog").close(); await loadSettings(); } catch (error) { showFormError(form, error); } }
async function updateMinimumHelper(){const helper=document.querySelector("#minimumBalanceHelper");const requestId=++minimumHelperRequest;if(document.querySelector("#accountClass").value!=="cash"){helper.replaceChildren();return;}const minimumBalance=document.querySelector("#minimumBalance").value,minimumCautionPercent=document.querySelector("#minimumCautionPercent").value;if(!minimumBalance||minimumCautionPercent===""){helper.replaceChildren();return;}try{const result=await api("/api/accounts/minimum-helper",{method:"POST",body:JSON.stringify({minimumBalance,minimumCautionPercent})});if(requestId!==minimumHelperRequest)return;const lines=result.lines.map((line)=>{const item=document.createElement("div");item.textContent=line;return item;});helper.replaceChildren(...lines);}catch(error){if(requestId===minimumHelperRequest)helper.replaceChildren();}}
function setReconciliationBalance(account){document.querySelector("#reconciliationCurrentBalance").textContent=account.accountClass==="credit"?(account.balanceCents>0?`${dollars(account.balanceCents)} credit`:`${dollars(Math.abs(account.balanceCents))} owed`):dollars(account.balanceCents);document.querySelector("#reconciliationHint").textContent=account.accountClass==="credit"?"Enter the actual amount currently owed as a positive number.":"Enter the actual current balance.";}
async function saveReconciliation(){const form=document.querySelector("#accountForm"),id=document.querySelector("#accountId").value;try{const result=await api(`/api/accounts/${id}/reconciliation`,{method:"POST",body:JSON.stringify({actualBalance:document.querySelector("#actualBalance").value})});document.querySelector("#actualBalance").value="";const account=accounts.find((item)=>item.id===Number(id));account.balanceCents=result.balanceCents;setReconciliationBalance(account);document.querySelector("#reconciliationStatus").textContent="Balance reconciled.";await loadReferenceData();}catch(error){showFormError(form,error);}}
async function updateUtilizationWarningHelper(){const helper=document.querySelector("#utilizationWarningHelper"),requestId=++utilizationHelperRequest,creditLimit=document.querySelector("#creditLimit").value,utilizationWarning=document.querySelector("#utilizationWarning").value;if(document.querySelector("#creditCardFields").hidden||!creditLimit||!utilizationWarning){helper.textContent="";return;}try{const result=await api("/api/accounts/utilization-warning-helper",{method:"POST",body:JSON.stringify({creditLimit,utilizationWarning})});if(requestId===utilizationHelperRequest)helper.textContent=result.text;}catch(error){if(requestId===utilizationHelperRequest)helper.textContent="";}}

function renderCategories() { const names = new Map(categories.map((category) => [category.id, category.name])); const body = document.querySelector("#categoryRows"); body.replaceChildren(); for (const category of categories) { const row = body.insertRow(); for (const value of [category.displayOrder, category.name, names.get(category.parentCategoryId) || "—", category.reportingRole === "income" ? "Income" : "Expense", category.active ? "Active" : "Archived"]) row.insertCell().textContent = value; const actions = row.insertCell(); const edit = document.createElement("button"); edit.textContent = "Edit"; edit.addEventListener("click", () => editCategory(category)); actions.append(edit); } }
function categoryParents(currentId = null) { return categories.filter((category) => category.parentCategoryId === null && category.id !== Number(currentId)); }
function newCategory() { const form = document.querySelector("#categoryForm"); form.reset(); document.querySelector("#categoryId").value = ""; document.querySelector("#categoryReportingRole").value = "expense"; document.querySelector("#categoryDialogTitle").textContent = "New Category"; document.querySelector("#categoryActiveLabel").hidden = true; document.querySelector("#deleteCategoryButton").hidden = true; setOptions(document.querySelector("#categoryParent"), categoryParents(), "None"); openDialog(document.querySelector("#categoryDialog"), form); }
function editCategory(category) { const form = document.querySelector("#categoryForm"); form.reset(); document.querySelector("#categoryId").value = category.id; document.querySelector("#categoryReportingRole").value = category.reportingRole; document.querySelector("#categoryName").value = category.name; document.querySelector("#categoryDisplayOrder").value = category.displayOrder; document.querySelector("#categoryOfferReimbursement").checked = Boolean(category.offerReimbursement); document.querySelector("#categoryActive").checked = Boolean(category.active); document.querySelector("#categoryDialogTitle").textContent = "Edit Category"; document.querySelector("#categoryActiveLabel").hidden = false; document.querySelector("#deleteCategoryButton").hidden = false; setOptions(document.querySelector("#categoryParent"), categoryParents(category.id), "None", category.parentCategoryId); openDialog(document.querySelector("#categoryDialog"), form); }
async function saveCategory(event) { event.preventDefault(); const form = event.currentTarget; const id = document.querySelector("#categoryId").value; const payload = {name: document.querySelector("#categoryName").value, reportingRole: document.querySelector("#categoryReportingRole").value, parentCategoryId: document.querySelector("#categoryParent").value || null, displayOrder: document.querySelector("#categoryDisplayOrder").value, active: document.querySelector("#categoryActive").checked, offerReimbursement: document.querySelector("#categoryOfferReimbursement").checked}; try { await api(id ? `/api/categories/${id}` : "/api/categories", {method: id ? "PATCH" : "POST", body: JSON.stringify(payload)}); document.querySelector("#categoryDialog").close(); await loadSettings(); } catch (error) { showFormError(form, error); } }
async function removeEditedCategory() { const category = categories.find((item) => item.id === Number(document.querySelector("#categoryId").value)); if (!category || !window.confirm(`Permanently delete category "${category.name}"? This cannot be undone.`)) return; try { await api(`/api/categories/${category.id}`, {method: "DELETE"}); document.querySelector("#categoryDialog").close(); await loadSettings(); } catch (error) { window.alert(error.message); } }
function renderTemplates() { const body = document.querySelector("#templateRows"); body.replaceChildren(); for (const template of templates) { const row = body.insertRow(); row.dataset.templateId = template.id; for (const value of [template.displayOrder, template.name + (template.active ? "" : " (archived)"), template.accountName || "—", template.categoryName || "—"]) row.insertCell().textContent = value; const amount = row.insertCell(); amount.className = "amount"; amount.textContent = template.enteredAmountCents === null ? "—" : dollars(template.enteredAmountCents); const actions = row.insertCell(); actions.className = "rowActions"; const edit = document.createElement("button"); edit.textContent = "Edit"; edit.addEventListener("click", () => editTemplate(template)); actions.append(edit); } }
function newTemplate() { const form = document.querySelector("#templateForm"); form.reset(); document.querySelector("#templateId").value = ""; document.querySelector("#templateDialogTitle").textContent = "New Template"; document.querySelector("#templateActiveLabel").hidden = true; document.querySelector("#deleteTemplateButton").hidden = true; setAccountOptions(document.querySelector("#templateAccount"), activeAccountsWith(), "None"); setMixedCategoryOptions(document.querySelector("#templateCategory"), selectableCategoriesWith(), "None"); openDialog(document.querySelector("#templateDialog"), form); }
function editTemplate(template) { const form = document.querySelector("#templateForm"); form.reset(); document.querySelector("#templateId").value = template.id; document.querySelector("#templateName").value = template.name; document.querySelector("#templateDescription").value = template.description || ""; document.querySelector("#templateAmount").value = template.enteredAmountCents === null ? "" : (template.enteredAmountCents / 100).toFixed(2); document.querySelector("#templatePrefillAmount").checked = Boolean(template.prefillAmount); document.querySelector("#templateDisplayOrder").value = template.displayOrder; document.querySelector("#templateActive").checked = Boolean(template.active); document.querySelector("#templateDialogTitle").textContent = "Edit Template"; document.querySelector("#templateActiveLabel").hidden = false; document.querySelector("#deleteTemplateButton").hidden = false; setAccountOptions(document.querySelector("#templateAccount"), activeAccountsWith(template.accountId), "None", template.accountId); setMixedCategoryOptions(document.querySelector("#templateCategory"), selectableCategoriesWith(template.categoryId), "None", template.categoryId); openDialog(document.querySelector("#templateDialog"), form); }
async function saveTemplate(event) { event.preventDefault(); const form = event.currentTarget; const id = document.querySelector("#templateId").value; const payload = {name: document.querySelector("#templateName").value, accountId: document.querySelector("#templateAccount").value, description: document.querySelector("#templateDescription").value, categoryId: document.querySelector("#templateCategory").value, amount: document.querySelector("#templateAmount").value, prefillAmount: document.querySelector("#templatePrefillAmount").checked, displayOrder: document.querySelector("#templateDisplayOrder").value, active: document.querySelector("#templateActive").checked}; try { await api(id ? `/api/templates/${id}` : "/api/templates", {method: id ? "PATCH" : "POST", body: JSON.stringify(payload)}); document.querySelector("#templateDialog").close(); await loadSettings(); } catch (error) { showFormError(form, error); } }
async function removeEditedTemplate() { const template = templates.find((item) => item.id === Number(document.querySelector("#templateId").value)); if (!template || !window.confirm(`Permanently delete template "${template.name}"? Existing transactions are unaffected.`)) return; try { await api(`/api/templates/${template.id}`, {method: "DELETE"}); document.querySelector("#templateDialog").close(); await loadSettings(); } catch (error) { window.alert(error.message); } }
async function loadSettings() { await loadReferenceData(); renderAccounts(); renderCategories(); renderTemplates(); const [settings, databaseStatus] = await Promise.all([api("/api/settings"), api("/api/system/database-status")]); const selected = categories.find((category) => category.id === Number(settings.reimbursementCategoryId)); document.querySelector("#reimbursementCategoryName").textContent = selected?.name || "Not selected"; setOptions(document.querySelector("#reimbursementCategorySetting"), categories.filter((category) => category.active && category.reportingRole === "income" && !category.parentCategoryId && !category.hasChildren), "Select category", settings.reimbursementCategoryId); document.querySelector("#paychecksPerYear").value = settings.paychecksPerYear || ""; setMoneyInputValue(document.querySelector("#suggestionRounding"), settings.suggestionRoundingCents ? (Number(settings.suggestionRoundingCents) / 100).toFixed(2) : ""); document.querySelector("#monthlySuggestionWindow").value = settings.monthlySuggestionWindowDays; document.querySelector("#quarterlySuggestionWindow").value = settings.quarterlySuggestionWindowMonths; document.querySelector("#annualSuggestionWindow").value = settings.annualSuggestionWindowMonths; document.querySelector("#themeSetting").value = localStorage.getItem("theme") || "system"; document.querySelector("#databaseTransactionCount").textContent = databaseStatus.transactionCount.toLocaleString("en-US"); document.querySelector("#databaseAccountCount").textContent = databaseStatus.accountCount.toLocaleString("en-US"); document.querySelector("#databaseSchemaVersion").textContent = databaseStatus.schemaVersion; document.querySelector("#databaseModifiedLocal").textContent = databaseStatus.modifiedLocal; }

function completeExpenseTemplates() {
  return templates.filter((template) => template.active && template.description
    && template.categoryId && template.enteredAmountCents > 0
    && (!template.accountId || accounts.some((account) => account.id === template.accountId && account.active))
    && categories.some((category) => category.id === template.categoryId && category.active && category.reportingRole === "expense" && !category.hasChildren));
}

async function loadBudget() {
  await loadReferenceData();
  const asOf=document.querySelector("#sinkingFundAsOfDate");if(!asOf.value)asOf.value=today;
  budget = await api(`/api/budget?date=${encodeURIComponent(asOf.value)}`);
  loadedSinkingFundDate=asOf.value;
  document.querySelector("#budgetSetupMessage").hidden = Boolean(budget.paychecksPerYear);
  document.querySelector("#newSinkingFundButton").disabled = false;
  document.querySelector("#totalReserveTarget").textContent = dollars(budget.totalReserveTargetCents);
  document.querySelector("#totalMonthlySinking").textContent = dollars(budget.totalMonthlyContributionCents);
  document.querySelector("#totalPerPaycheckSinking").textContent = budget.totalPerPaycheckContributionCents === null ? "Set Paychecks Per Year" : dollars(budget.totalPerPaycheckContributionCents);
  document.querySelector("#totalMonthlyRecurring").textContent = dollars(budget.totalMonthlyRecurringCents);
  document.querySelector("#totalFlexibleBudget").textContent = dollars(budget.totalFlexibleCategoryBudgetCents);
  document.querySelector("#totalPlannedPayments").textContent = dollars(budget.totalPlannedCreditPaymentCents);
  document.querySelector("#totalExpectedIncome").textContent = dollars(budget.totalExpectedIncomeCents);
  document.querySelector("#monthlyRequirementTotal").textContent = dollars(budget.monthlyRequirementCents);
  document.querySelector("#requirementMonthlyRecurring").textContent = dollars(budget.totalMonthlyRecurringCents);
  document.querySelector("#requirementFlexible").textContent = dollars(budget.totalFlexibleCategoryBudgetCents);
  document.querySelector("#requirementCreditPayments").textContent = dollars(budget.totalPlannedCreditPaymentCents);
  document.querySelector("#requirementSinking").textContent = dollars(budget.totalMonthlyContributionCents);
  document.querySelector("#requirementIncome").textContent = dollars(budget.totalExpectedIncomeCents);
  renderBudget();
  renderMonthlyRecurring();
  renderFlexibleBudgets();
  renderExpectedIncome();
  renderPlannedPayments();
  renderComingUp();
  showBudgetSection();
}

function renderComingUp() {
  const body = document.querySelector("#comingUpRows"); body.replaceChildren();
  for (const item of budget.comingUp) {
    const row = body.insertRow(); row.insertCell().textContent = friendlyCalendarDate(item.date);
    row.insertCell().textContent = item.item; row.insertCell().textContent = item.type;
    const amount = row.insertCell(); amount.className = "amount"; amount.textContent = dollars(item.amountCents);
    const actions = row.insertCell(); actions.className = "rowActions";
    const action = document.createElement("button"); action.type = "button";
    if (item.id.startsWith("payment-")) {
      action.textContent = "+ Transfer";
      const plan = budget.plannedCreditPayments.find((candidate) => candidate.id === Number(item.id.slice(8)));
      action.addEventListener("click", () => makePlannedPayment(plan));
    } else {
      action.textContent = "+ Transaction";
      const planId = Number(item.id.slice(item.id.indexOf("-") + 1));
      const plan = item.id.startsWith("income-")
        ? budget.expectedIncomePlans.find((candidate) => candidate.id === planId)
        : budget.plans.find((candidate) => candidate.id === planId);
      action.addEventListener("click", () => createTransactionFromPlan(plan));
    }
    action.className = "positiveAction";
    actions.append(action);
  }
  document.querySelector("#comingUpEmpty").hidden = budget.comingUp.length !== 0;
}

function showBudgetSection() {
  const sections = {
    "monthly-recurring": "budgetMonthlyRecurring",
    flexible: "budgetFlexible",
    "payments-income": "budgetPaymentsIncome",
    "sinking-funds": "budgetSinkingFunds",
  };
  const selected = sections[location.hash.slice(1)] ? location.hash.slice(1) : "monthly-recurring";
  for (const [fragment, sectionId] of Object.entries(sections)) {
    document.querySelector(`#${sectionId}`).hidden = fragment !== selected;
    const link = document.querySelector(`.budgetNavigation a[href="#${fragment}"]`);
    if (fragment === selected) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  }
}

function useBudgetFragmentNavigation(event) {
  const link = event.target.closest("a");
  if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault(); window.history.pushState({}, "", `${window.location.pathname}${window.location.search}${link.hash}`); showBudgetSection();
}

function budgetSortState() {
  const urlFilters = new URLSearchParams(window.location.search);
  const validSorts = new Set(["nextDate", "fund", "amount", "setAside", "targetToday"]);
  return {
    sort: validSorts.has(urlFilters.get("sort")) ? urlFilters.get("sort") : "nextDate",
    direction: urlFilters.get("direction") === "desc" ? "desc" : "asc",
  };
}

function renderBudget() {
  const {sort, direction} = budgetSortState();
  const sortValues = {
    nextDate: (plan) => plan.upcomingExpectedDate,
    fund: (plan) => plan.name,
    amount: (plan) => plan.enteredAmountCents,
    setAside: (plan) => plan.monthlyContributionCents,
    targetToday: (plan) => plan.reserveTargetCents,
  };
  const sortedPlans = budget.plans.filter((plan) => plan.frequency !== "monthly").sort((left, right) => {
    const leftValue = sortValues[sort](left);
    const rightValue = sortValues[sort](right);
    if (leftValue === null && rightValue !== null) return 1;
    if (leftValue !== null && rightValue === null) return -1;
    let comparison = typeof leftValue === "number"
      ? leftValue - rightValue
      : String(leftValue ?? "").localeCompare(String(rightValue ?? ""), undefined, {sensitivity: "base"});
    if (comparison === 0) comparison = left.id - right.id;
    return direction === "asc" ? comparison : -comparison;
  });
  const body = document.querySelector("#sinkingFundRows"); body.replaceChildren();
  for (const plan of sortedPlans) {
    const row = body.insertRow();
    if (!plan.active) row.className = "archivedRow";
    const nextDate = row.insertCell(); nextDate.textContent = friendlyCalendarDate(plan.upcomingExpectedDate);
    const frequency = document.createElement("div"); frequency.className = "secondaryText"; frequency.textContent = plan.frequency === "quarterly" ? "Quarterly" : "Yearly"; nextDate.append(frequency);
    row.insertCell().textContent = plan.name + (plan.active ? "" : " (archived)");
    const amount = row.insertCell(); amount.className = "amount"; amount.textContent = dollars(plan.enteredAmountCents);
    const setAside = row.insertCell(); setAside.className = "amount setAsideValues";
    const monthly = document.createElement("div"); monthly.textContent = `Monthly ${dollars(plan.monthlyContributionCents)}`;
    const perPaycheck = document.createElement("div"); perPaycheck.textContent = `Per paycheck ${plan.perPaycheckContributionCents === null ? "—" : dollars(plan.perPaycheckContributionCents)}`;
    setAside.append(monthly, perPaycheck);
    const target = row.insertCell(); target.className = "amount"; target.textContent = dollars(plan.reserveTargetCents);
    appendBudgetPlanActions(row, plan);
  }
  for (const button of document.querySelectorAll("[data-budget-sort]")) {
    button.textContent = button.dataset.label + (button.dataset.budgetSort === sort ? ` ${direction === "asc" ? "▲" : "▼"}` : "");
  }
}

function monthlySortState() {
  const urlFilters = new URLSearchParams(window.location.search);
  const sort = ["nextDate", "expense", "amount"].includes(urlFilters.get("monthlySort")) ? urlFilters.get("monthlySort") : "nextDate";
  return {sort, direction: urlFilters.get("monthlyDirection") === "desc" ? "desc" : "asc"};
}

function renderMonthlyRecurring() {
  const groupBy = document.querySelector("#monthlyRecurringGroupBy").value;
  const {sort, direction} = monthlySortState();
  const sortValues = {
    nextDate: (plan) => plan.upcomingExpectedDate,
    expense: (plan) => plan.name,
    amount: (plan) => plan.enteredAmountCents ?? plan.amountCents,
  };
  const sortMonthlyItems = (items) => [...items].sort((left, right) => {
    const leftValue = sortValues[sort](left);
    const rightValue = sortValues[sort](right);
    let comparison = typeof leftValue === "number"
      ? leftValue - rightValue
      : String(leftValue).localeCompare(String(rightValue), undefined, {sensitivity: "base"});
    if (comparison === 0) comparison = left.id - right.id;
    return direction === "asc" ? comparison : -comparison;
  });
  const appendAggregateRows = (body, groups, labelFor) => {
    body.replaceChildren();
    for (const group of groups) {
      const row = body.insertRow(); row.className = "monthlyRecurringAggregateRow";
      const label = row.insertCell(); label.colSpan = 2; label.textContent = labelFor(group);
      const amount = row.insertCell(); amount.className = "amount"; amount.textContent = dollars(group.amountCents);
      row.insertCell();
      for (const item of sortMonthlyItems(group.items)) {
        const detail = body.insertRow(); detail.className = "monthlyRecurringDetailRow";
        detail.insertCell().textContent = friendlyCalendarDate(item.upcomingExpectedDate);
        detail.insertCell().textContent = item.name;
        const detailAmount = detail.insertCell(); detailAmount.className = "amount"; detailAmount.textContent = dollars(item.amountCents);
        const plan = budget.plans.find((candidate) => candidate.id === item.id);
        appendBudgetPlanActions(detail, plan);
      }
    }
  };
  for (const panel of document.querySelectorAll("[data-monthly-recurring-group]")) {
    panel.hidden = panel.dataset.monthlyRecurringGroup !== groupBy;
  }
  const paymentAccountBody = document.querySelector("#monthlyRecurringPaymentAccountRows");
  appendAggregateRows(paymentAccountBody, budget.monthlyRecurringByPaymentAccount, (item) => item.accountName);
  const categoryBody = document.querySelector("#monthlyRecurringCategoryRows");
  appendAggregateRows(categoryBody, budget.monthlyRecurringByCategory, (item) => `Category: ${item.parentCategoryName ? `${item.parentCategoryName} › ` : ""}${item.categoryName}`);
  const plans = sortMonthlyItems(budget.plans.filter((plan) => plan.frequency === "monthly"));
  const body = document.querySelector("#monthlyRecurringRows"); body.replaceChildren();
  for (const plan of plans) {
    const row = body.insertRow();
    if (!plan.active) row.className = "archivedRow";
    row.insertCell().textContent = friendlyCalendarDate(plan.upcomingExpectedDate);
    const expense = row.insertCell(); expense.textContent = plan.name + (plan.active ? "" : " (archived)");
    const details = [plan.accountName, plan.categoryName].filter(Boolean).join(" · ");
    if (details) { const secondary = document.createElement("div"); secondary.className = "secondaryText"; secondary.textContent = details; expense.append(secondary); }
    const amount = row.insertCell(); amount.className = "amount"; amount.textContent = dollars(plan.enteredAmountCents);
    appendBudgetPlanActions(row, plan);
  }
  for (const button of document.querySelectorAll("[data-monthly-sort]")) {
    button.textContent = button.dataset.label + (button.dataset.monthlySort === sort ? ` ${direction === "asc" ? "▲" : "▼"}` : "");
  }
}

function appendBudgetPlanActions(row, plan) {
  const actions = row.insertCell(); actions.className = "rowActions";
  const create = document.createElement("button"); create.type = "button"; create.className = "positiveAction"; create.textContent = "+ Transaction"; create.disabled = !plan.active; create.addEventListener("click", () => createTransactionFromPlan(plan));
  const edit = document.createElement("button"); edit.type = "button"; edit.textContent = "Edit"; edit.addEventListener("click", () => editSinkingFund(plan));
  actions.append(create, edit);
}

function sortBudget(event) {
  const selectedSort = event.currentTarget.dataset.budgetSort;
  const current = budgetSortState();
  const firstDirection = ["amount", "setAside", "targetToday"].includes(selectedSort) ? "desc" : "asc";
  const direction = current.sort === selectedSort ? current.direction === "asc" ? "desc" : "asc" : firstDirection;
  const urlFilters = new URLSearchParams(window.location.search);
  urlFilters.set("sort", selectedSort); urlFilters.set("direction", direction);
  window.history.pushState({}, "", `/budget?${urlFilters}${window.location.hash}`);
  renderBudget();
}

function sortMonthlyRecurring(event) {
  const selectedSort = event.currentTarget.dataset.monthlySort;
  const current = monthlySortState();
  const firstDirection = selectedSort === "amount" ? "desc" : "asc";
  const direction = current.sort === selectedSort ? current.direction === "asc" ? "desc" : "asc" : firstDirection;
  const urlFilters = new URLSearchParams(window.location.search);
  urlFilters.set("monthlySort", selectedSort); urlFilters.set("monthlyDirection", direction);
  window.history.pushState({}, "", `/budget?${urlFilters}${window.location.hash}`);
  renderMonthlyRecurring();
}

async function createTransactionFromPlan(plan) { await newTransaction(plan.templateId); }

function newSinkingFund() {
  const form = document.querySelector("#sinkingFundForm"); form.reset();
  document.querySelector("#sinkingFundId").value = ""; document.querySelector("#sinkingFundExpectedDate").value = today;
  document.querySelector("#sinkingFundDialogTitle").textContent = "New Budget Plan"; document.querySelector("#sinkingFundActiveLabel").hidden = true;
  document.querySelector("#sinkingFundSubmitButton").textContent = "Create Plan";
  document.querySelector("#deleteSinkingFundButton").hidden = true;
  const usedTemplateIds = new Set(budget.plans.filter((plan) => plan.active).map((plan) => plan.templateId));
  setOptions(document.querySelector("#sinkingFundTemplate"), completeExpenseTemplates().filter((template) => !usedTemplateIds.has(template.id)), "Select template");
  updateRequiredTemplateLink("sinkingFund", false); openDialog(document.querySelector("#sinkingFundDialog"), form);
}

function editSinkingFund(plan) {
  const form = document.querySelector("#sinkingFundForm"); form.reset();
  document.querySelector("#sinkingFundId").value = plan.id; document.querySelector("#sinkingFundFrequency").value = plan.frequency;
  document.querySelector("#sinkingFundExpectedDate").value = plan.expectedDate;
  document.querySelector("#sinkingFundActive").checked = Boolean(plan.active); document.querySelector("#sinkingFundActiveLabel").hidden = false;
  document.querySelector("#deleteSinkingFundButton").hidden = false;
  document.querySelector("#sinkingFundDialogTitle").textContent = "Edit Budget Plan";
  document.querySelector("#sinkingFundSubmitButton").textContent = "Save";
  setOptions(document.querySelector("#sinkingFundTemplate"), completeExpenseTemplates(), "Select template", plan.templateId);
  updateRequiredTemplateLink("sinkingFund", true); openDialog(document.querySelector("#sinkingFundDialog"), form);
}

async function saveSinkingFund(event) {
  event.preventDefault(); const form = event.currentTarget; const id = document.querySelector("#sinkingFundId").value;
  const payload = {templateId: document.querySelector("#sinkingFundTemplate").value, frequency: document.querySelector("#sinkingFundFrequency").value, expectedDate: document.querySelector("#sinkingFundExpectedDate").value, active: document.querySelector("#sinkingFundActive").checked};
  try { await api(id ? `/api/sinking-funds/${id}` : "/api/sinking-funds", {method: id ? "PATCH" : "POST", body: JSON.stringify(payload)}); document.querySelector("#sinkingFundDialog").close(); await loadBudget(); }
  catch (error) { showFormError(form, error); }
}

async function removeEditedSinkingFund() { const plan = budget.plans.find((item) => item.id === Number(document.querySelector("#sinkingFundId").value)); if (!plan || !window.confirm(`Permanently delete Budget plan "${plan.name}"?`)) return; try { await api(`/api/sinking-funds/${plan.id}`, {method: "DELETE"}); document.querySelector("#sinkingFundDialog").close(); await loadBudget(); } catch (error) { window.alert(error.message); } }

function renderFlexibleBudgets() {
  const body = document.querySelector("#flexibleBudgetRows"); body.replaceChildren();
  for (const item of budget.flexibleBudgets) {
    const row = body.insertRow(); if (!item.active) row.className = "archivedRow";
    row.insertCell().textContent = item.categoryName + (item.active ? "" : " (archived)");
    const target = row.insertCell(); target.className = "amount"; target.textContent = dollars(item.monthlyTargetCents);
    const suggestion = row.insertCell(); suggestion.className = "amount"; suggestion.textContent = dollars(item.suggestionCents);
    const actions = row.insertCell(); const edit = document.createElement("button"); edit.type = "button"; edit.textContent = "Edit"; edit.addEventListener("click", () => editFlexibleBudget(item)); actions.append(edit);
  }
}

function flexibleCategoryOptions(currentId = null) {
  const used = new Set(budget.flexibleBudgets.filter((item) => item.active && item.categoryId !== currentId).map((item) => item.categoryId));
  return budget.flexibleCategories.filter((category) => !used.has(category.id));
}

function updateFlexibleSuggestion() {
  const category = budget.flexibleCategories.find((item) => item.id === Number(document.querySelector("#flexibleBudgetCategory").value));
  document.querySelector("#flexibleBudgetSuggestion").textContent = category ? `Three-month suggestion: ${dollars(category.suggestionCents)}` : "";
}

function newFlexibleBudget() {
  const form = document.querySelector("#flexibleBudgetForm"); form.reset(); document.querySelector("#flexibleBudgetId").value = "";
  document.querySelector("#flexibleBudgetDialogTitle").textContent = "New Category Budget"; document.querySelector("#deleteFlexibleBudgetButton").hidden = true;
  setOptions(document.querySelector("#flexibleBudgetCategory"), flexibleCategoryOptions(), "Select category"); updateFlexibleSuggestion();
  openDialog(document.querySelector("#flexibleBudgetDialog"), form);
}

function editFlexibleBudget(item) {
  const form = document.querySelector("#flexibleBudgetForm"); form.reset(); document.querySelector("#flexibleBudgetId").value = item.id;
  document.querySelector("#flexibleBudgetDialogTitle").textContent = "Edit Category Budget"; document.querySelector("#deleteFlexibleBudgetButton").hidden = false;
  setOptions(document.querySelector("#flexibleBudgetCategory"), flexibleCategoryOptions(item.categoryId), "Select category", item.categoryId);
  document.querySelector("#flexibleBudgetTarget").value = (item.monthlyTargetCents / 100).toFixed(2); updateFlexibleSuggestion(); openDialog(document.querySelector("#flexibleBudgetDialog"), form);
}

async function saveFlexibleBudget(event) { event.preventDefault(); const form = event.currentTarget; const id = document.querySelector("#flexibleBudgetId").value; const payload = {categoryId: document.querySelector("#flexibleBudgetCategory").value, monthlyTarget: document.querySelector("#flexibleBudgetTarget").value}; try { await api(id ? `/api/flexible-budgets/${id}` : "/api/flexible-budgets", {method: id ? "PATCH" : "POST", body: JSON.stringify(payload)}); document.querySelector("#flexibleBudgetDialog").close(); await loadBudget(); } catch (error) { showFormError(form, error); } }
async function removeFlexibleBudget() { const item = budget.flexibleBudgets.find((row) => row.id === Number(document.querySelector("#flexibleBudgetId").value)); if (!item || !window.confirm(`Permanently delete category budget "${item.categoryName}"?`)) return; try { await api(`/api/flexible-budgets/${item.id}`, {method: "DELETE"}); document.querySelector("#flexibleBudgetDialog").close(); await loadBudget(); } catch (error) { window.alert(error.message); } }

function incomeTemplates() { return templates.filter((template) => template.active && template.description && template.categoryId && template.enteredAmountCents > 0 && (!template.accountId || accounts.some((account) => account.id === template.accountId && account.active)) && categories.some((category) => category.id === template.categoryId && category.active && category.reportingRole === "income" && !category.hasChildren)); }

function nextDateSort(items, state, values) { return [...items].sort((left, right) => { const leftValue = values[state.sort](left); const rightValue = values[state.sort](right); let comparison = typeof leftValue === "number" ? leftValue - rightValue : String(leftValue).localeCompare(String(rightValue), undefined, {sensitivity: "base"}); if (comparison === 0) comparison = left.id - right.id; return state.direction === "asc" ? comparison : -comparison; }); }
function planSortState(prefix, validSorts) { const values = new URLSearchParams(window.location.search); const sort = validSorts.includes(values.get(`${prefix}Sort`)) ? values.get(`${prefix}Sort`) : "nextDate"; return {sort, direction: values.get(`${prefix}Direction`) === "desc" ? "desc" : "asc"}; }
function changePlanSort(prefix, selectedSort, numeric, render) { const current = planSortState(prefix, prefix === "income" ? ["nextDate", "income", "amount"] : ["nextDate", "account", "description", "amount"]); const first = numeric.includes(selectedSort) ? "desc" : "asc"; const direction = current.sort === selectedSort ? current.direction === "asc" ? "desc" : "asc" : first; const values = new URLSearchParams(window.location.search); values.set(`${prefix}Sort`, selectedSort); values.set(`${prefix}Direction`, direction); window.history.pushState({}, "", `/budget?${values}${window.location.hash}`); render(); }

function renderExpectedIncome() {
  const state = planSortState("income", ["nextDate", "income", "amount"]); const values = {nextDate: (item) => item.upcomingExpectedDate, income: (item) => item.name, amount: (item) => item.enteredAmountCents};
  const body = document.querySelector("#expectedIncomeRows"); body.replaceChildren();
  for (const item of nextDateSort(budget.expectedIncomePlans, state, values)) { const row = body.insertRow(); if (!item.active) row.className = "archivedRow"; row.insertCell().textContent = friendlyCalendarDate(item.upcomingExpectedDate); const income = row.insertCell(); income.textContent = item.name + (item.active ? "" : " (archived)"); const details = [item.accountName, item.categoryName].filter(Boolean).join(" · "); if (details) { const secondary = document.createElement("div"); secondary.className = "secondaryText"; secondary.textContent = details; income.append(secondary); } const amount = row.insertCell(); amount.className = "amount"; amount.textContent = dollars(item.enteredAmountCents); const actions = row.insertCell(); const create = document.createElement("button"); create.type = "button"; create.className = "positiveAction"; create.textContent = "+ Transaction"; create.disabled = !item.active; create.addEventListener("click", () => createTransactionFromPlan(item)); const edit = document.createElement("button"); edit.type = "button"; edit.textContent = "Edit"; edit.addEventListener("click", () => editExpectedIncome(item)); actions.append(create, edit); }
  for (const button of document.querySelectorAll("[data-income-sort]")) button.textContent = button.dataset.label + (button.dataset.incomeSort === state.sort ? ` ${state.direction === "asc" ? "▲" : "▼"}` : "");
}

function newExpectedIncome() { const form = document.querySelector("#expectedIncomeForm"); form.reset(); document.querySelector("#expectedIncomeId").value = ""; document.querySelector("#expectedIncomeDate").value = today; document.querySelector("#expectedIncomeDialogTitle").textContent = "New Expected Monthly Income"; document.querySelector("#expectedIncomeActiveLabel").hidden = true; document.querySelector("#deleteExpectedIncomeButton").hidden = true; setOptions(document.querySelector("#expectedIncomeTemplate"), incomeTemplates(), "Select template"); updateRequiredTemplateLink("expectedIncome", false); openDialog(document.querySelector("#expectedIncomeDialog"), form); }
function editExpectedIncome(item) { const form = document.querySelector("#expectedIncomeForm"); form.reset(); document.querySelector("#expectedIncomeId").value = item.id; document.querySelector("#expectedIncomeDate").value = item.expectedDate; document.querySelector("#expectedIncomeActive").checked = Boolean(item.active); document.querySelector("#expectedIncomeActiveLabel").hidden = false; document.querySelector("#deleteExpectedIncomeButton").hidden = false; document.querySelector("#expectedIncomeDialogTitle").textContent = "Edit Expected Income"; setOptions(document.querySelector("#expectedIncomeTemplate"), incomeTemplates(), "Select template", item.templateId); updateRequiredTemplateLink("expectedIncome", true); openDialog(document.querySelector("#expectedIncomeDialog"), form); }
async function saveExpectedIncome(event) { event.preventDefault(); const form = event.currentTarget; const id = document.querySelector("#expectedIncomeId").value; const payload = {templateId: document.querySelector("#expectedIncomeTemplate").value, expectedDate: document.querySelector("#expectedIncomeDate").value, active: document.querySelector("#expectedIncomeActive").checked}; try { await api(id ? `/api/expected-income-plans/${id}` : "/api/expected-income-plans", {method: id ? "PATCH" : "POST", body: JSON.stringify(payload)}); document.querySelector("#expectedIncomeDialog").close(); await loadBudget(); } catch (error) { showFormError(form, error); } }
async function removeExpectedIncome() { const item = budget.expectedIncomePlans.find((row) => row.id === Number(document.querySelector("#expectedIncomeId").value)); if (!item || !window.confirm(`Permanently delete expected income "${item.name}"?`)) return; try { await api(`/api/expected-income-plans/${item.id}`, {method: "DELETE"}); document.querySelector("#expectedIncomeDialog").close(); await loadBudget(); } catch (error) { window.alert(error.message); } }

function renderPlannedPayments() {
  const state = planSortState("payment", ["nextDate", "account", "description", "amount"]); const values = {nextDate: (item) => item.upcomingExpectedDate, account: (item) => item.accountName, description: (item) => item.description, amount: (item) => item.amountCents}; const body = document.querySelector("#plannedPaymentRows"); body.replaceChildren();
  for (const item of nextDateSort(budget.plannedCreditPayments, state, values)) { const row = body.insertRow(); if (!item.active) row.className = "archivedRow"; row.insertCell().textContent = friendlyCalendarDate(item.upcomingExpectedDate); const payment = row.insertCell(); payment.textContent = item.description; const account = document.createElement("div"); account.className = "secondaryText"; account.textContent = item.accountName + (item.active ? "" : " (archived)"); payment.append(account); const amount = row.insertCell(); amount.className = "amount"; amount.textContent = dollars(item.amountCents); const actions = row.insertCell(); const make = document.createElement("button"); make.type = "button"; make.className = "positiveAction"; make.textContent = "+ Transfer"; make.disabled = !item.active; make.addEventListener("click", () => makePlannedPayment(item)); const edit = document.createElement("button"); edit.type = "button"; edit.textContent = "Edit"; edit.addEventListener("click", () => editPlannedPayment(item)); actions.append(make, edit); }
  for (const button of document.querySelectorAll("[data-payment-sort]")) button.textContent = button.dataset.label + (button.dataset.paymentSort === state.sort ? ` ${state.direction === "asc" ? "▲" : "▼"}` : "");
}

function creditPaymentAccounts() { return accounts.filter((account) => account.active && account.accountClass === "credit" && ["Credit Card", "Personal Loan"].includes(account.accountSubtype)); }
function newPlannedPayment() { const form = document.querySelector("#plannedPaymentForm"); form.reset(); document.querySelector("#plannedPaymentId").value = ""; document.querySelector("#plannedPaymentDate").value = today; document.querySelector("#plannedPaymentDialogTitle").textContent = "New Planned Monthly Debt Payment"; document.querySelector("#plannedPaymentActiveLabel").hidden = true; document.querySelector("#deletePlannedPaymentButton").hidden = true; setAccountOptions(document.querySelector("#plannedPaymentAccount"), creditPaymentAccounts(), "Select account"); openDialog(document.querySelector("#plannedPaymentDialog"), form); }
function editPlannedPayment(item) { const form = document.querySelector("#plannedPaymentForm"); form.reset(); document.querySelector("#plannedPaymentId").value = item.id; document.querySelector("#plannedPaymentDate").value = item.expectedDate; setAccountOptions(document.querySelector("#plannedPaymentAccount"), creditPaymentAccounts(), "Select account", item.accountId); document.querySelector("#plannedPaymentDescription").value = item.description; document.querySelector("#plannedPaymentAmount").value = (item.amountCents / 100).toFixed(2); document.querySelector("#plannedPaymentActive").checked = Boolean(item.active); document.querySelector("#plannedPaymentActiveLabel").hidden = false; document.querySelector("#deletePlannedPaymentButton").hidden = false; document.querySelector("#plannedPaymentDialogTitle").textContent = "Edit Planned Monthly Debt Payment"; openDialog(document.querySelector("#plannedPaymentDialog"), form); }
async function savePlannedPayment(event) { event.preventDefault(); const form = event.currentTarget; const id = document.querySelector("#plannedPaymentId").value; const payload = {expectedDate: document.querySelector("#plannedPaymentDate").value, accountId: document.querySelector("#plannedPaymentAccount").value, description: document.querySelector("#plannedPaymentDescription").value, amount: document.querySelector("#plannedPaymentAmount").value, active: document.querySelector("#plannedPaymentActive").checked}; try { await api(id ? `/api/planned-credit-payments/${id}` : "/api/planned-credit-payments", {method: id ? "PATCH" : "POST", body: JSON.stringify(payload)}); document.querySelector("#plannedPaymentDialog").close(); await loadBudget(); } catch (error) { showFormError(form, error); } }
async function removePlannedPayment() { const item = budget.plannedCreditPayments.find((row) => row.id === Number(document.querySelector("#plannedPaymentId").value)); if (!item || !window.confirm(`Permanently delete planned payment "${item.description}"?`)) return; try { await api(`/api/planned-credit-payments/${item.id}`, {method: "DELETE"}); document.querySelector("#plannedPaymentDialog").close(); await loadBudget(); } catch (error) { window.alert(error.message); } }
function makePlannedPayment(item) { newTransfer(); document.querySelector("#transferToAccount").value = String(item.accountId); updateTransferPresentation(false); document.querySelector("#transferDescription").value = item.description; setMoneyInputValue(document.querySelector("#transferAmount"), (item.amountCents / 100).toFixed(2)); document.querySelector("#transferDate").value = today; }

async function saveBudgetSettings(event) { event.preventDefault(); const form = event.currentTarget; try { await api("/api/settings/paychecks-per-year", {method: "PUT", body: JSON.stringify({paychecksPerYear: document.querySelector("#paychecksPerYear").value})}); await api("/api/settings/suggestion-rounding", {method: "PUT", body: JSON.stringify({amount: document.querySelector("#suggestionRounding").value})}); await api("/api/settings/budget-suggestion-windows", {method: "PUT", body: JSON.stringify({monthlyDays: document.querySelector("#monthlySuggestionWindow").value, quarterlyMonths: document.querySelector("#quarterlySuggestionWindow").value, annualMonths: document.querySelector("#annualSuggestionWindow").value})}); await loadSettings(); } catch (error) { showFormError(form, error); } }

function applyTheme(theme) {
  if (theme === "system") document.documentElement.removeAttribute("data-theme"); else document.documentElement.dataset.theme = theme;
  localStorage.setItem("theme", theme);
}

function changeReimbursementCategory() {
  openDialog(document.querySelector("#reimbursementCategoryDialog"), document.querySelector("#reimbursementCategoryForm"));
}

async function saveReimbursementCategory(event) {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api("/api/settings/reimbursement-category", {method: "PUT", body: JSON.stringify({categoryId: document.querySelector("#reimbursementCategorySetting").value})});
    document.querySelector("#reimbursementCategoryDialog").close();
    await loadSettings();
  } catch (error) { showFormError(form, error); }
}

function showSettingsSection() {
  const section = ["accounts", "categories", "templates", "budget", "system"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "accounts";
  for (const name of ["accounts", "categories", "templates", "budget", "system"]) {
    document.querySelector(`#settings${name[0].toUpperCase()}${name.slice(1)}`).hidden = name !== section;
    const link = document.querySelector(`.settingsNavigation a[href="#${name}"]`);
    if (name === section) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  }
}

function useSameTabNavigation(event) {
  const link = event.target.closest("a");
  if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  window.location.assign(link.href);
}

function useSettingsFragmentNavigation(event) {
  const link = event.target.closest("a");
  if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  window.history.pushState({}, "", link.hash);
  showSettingsSection();
}

async function start() {
  browserSession = await api("/api/session");
  document.body.dataset.role = browserSession.role;
  document.querySelector("#auditorBanner").hidden = browserSession.role !== "auditor";
  if (browserSession.role === "auditor") applyAuditorControls();
  enhanceMoneyInputs(document);
  new MutationObserver((records) => {
    for (const record of records) for (const node of record.addedNodes) if (node.nodeType === Node.ELEMENT_NODE) enhanceMoneyInputs(node);
  }).observe(document.body, {childList: true, subtree: true});
  const roleLabel = document.createElement("label"); roleLabel.textContent = "Reporting Role "; const roleSelect = document.createElement("select"); roleSelect.id = "categoryReportingRole"; option(roleSelect, "expense", "Expense"); option(roleSelect, "income", "Income"); roleLabel.append(roleSelect); document.querySelector("#categoryName").closest("label").after(roleLabel);
  if (browserSession.role === "auditor") applyAuditorControls();
  const path = currentPath;
  document.querySelector(path === "/pending" ? "#pendingPage" : path === "/transactions" ? "#transactionsPage" : path === "/settings" ? "#settingsPage" : path === "/budget" ? "#budgetPage" : path === "/simulator" ? "#simulatorPage" : "#dashboardPage").hidden = false;
  for (const link of document.querySelectorAll("header a")) if (link.getAttribute("href") === (path === "/new" ? "/" : path)) link.setAttribute("aria-current", "page");
  for (const button of document.querySelectorAll("[data-close]")) button.addEventListener("click", () => button.closest("dialog").close());
  for (const dialog of document.querySelectorAll("dialog")) {
    let pointerStartedOnBackdrop = false;
    dialog.addEventListener("pointerdown", (event) => { pointerStartedOnBackdrop = event.target === dialog; });
    dialog.addEventListener("pointerup", (event) => { if (pointerStartedOnBackdrop && event.target === dialog) dialog.close(); pointerStartedOnBackdrop = false; });
  }
  const application = await api("/api/application");
  document.querySelector("#applicationVersion").textContent = application.version;
  if (path === "/transactions") { await loadReferenceData(); await initializeDashboardPeriods(); renderReferenceSelectors(); await loadTransactions(); }
  if (path === "/settings") await loadSettings();
  if (path === "/settings") showSettingsSection();
  if (path === "/budget") await loadBudget();
  if (path === "/simulator") await loadSimulator();
  if (path === "/settings") highlightSettingsTemplate();
  if (path === "/pending") { await loadReferenceData(); await loadPendingReview(); }
  if (browserSession.role === "owner") {
    if (path === "/settings") await loadIntegrationSecrets();
    if (["/", "/new", "/transactions"].includes(path)) await refreshPendingCount();
  }
  if (path === "/" || path === "/new") { await loadReferenceData(); await initializeDashboardPeriods(); await loadDashboard(); }
  if (path === "/new" && browserSession.role === "owner") {
    const transactionDialog = document.querySelector("#transactionDialog");
    transactionDialog.addEventListener("close", () => { if (window.location.pathname === "/new") window.history.replaceState({}, "", "/"); }, {once: true});
    await newTransaction();
  }
}

document.querySelector("#newTransactionButton").addEventListener("click", () => newTransaction()); document.querySelector("#newTransferButton").addEventListener("click", newTransfer); document.querySelector("#transactionForm").addEventListener("submit", saveTransaction); document.querySelector("#transferForm").addEventListener("submit", saveTransfer);
document.querySelector("#sendToSimulatorForm").addEventListener("submit", sendToSimulator);
document.querySelector("#transactionDate").addEventListener("change", () => { if (!document.querySelector("#transactionId").value) refreshTemplateSuggestions(); });
document.querySelector("#showAllTemplates").addEventListener("change", () => refreshTemplateSuggestions());
document.querySelector("#newBulkEntryButton").addEventListener("click", newBulkEntry); document.querySelector("#dashboardBulkEntryButton").addEventListener("click", newBulkEntry); document.querySelector("#bulkEntryForm").addEventListener("submit", saveBulkEntry);
document.querySelector("#transferToAccount").addEventListener("change", () => updateTransferPresentation(true));
document.querySelector("#transferFromAccount").addEventListener("change", () => updateTransferPresentation(false));
document.querySelector("#filterForm").addEventListener("submit", applyTransactionFilterForm); document.querySelector("#periodFilter").addEventListener("change", (event) => { const custom = event.target.value === "custom"; document.querySelector("#transactionCustomDates").hidden = !custom; document.querySelector("#startDateFilter").disabled = !custom; document.querySelector("#endDateFilter").disabled = !custom; });
document.querySelector("#amountModeFilter").addEventListener("change", updateAmountFilterPresentation);
for (const button of document.querySelectorAll("[data-sort]")) button.addEventListener("click", sortTransactions);
for (const button of document.querySelectorAll("[data-budget-sort]")) button.addEventListener("click", sortBudget);
for (const button of document.querySelectorAll("[data-monthly-sort]")) button.addEventListener("click", sortMonthlyRecurring);
for (const button of document.querySelectorAll("[data-income-sort]")) button.addEventListener("click", (event) => changePlanSort("income", event.currentTarget.dataset.incomeSort, ["amount"], renderExpectedIncome));
for (const button of document.querySelectorAll("[data-payment-sort]")) button.addEventListener("click", (event) => changePlanSort("payment", event.currentTarget.dataset.paymentSort, ["amount"], renderPlannedPayments));
document.querySelector("#newAccountButton").addEventListener("click", newAccount); document.querySelector("#accountForm").addEventListener("submit", saveAccount);document.querySelector("#reconcileAccountButton").addEventListener("click",saveReconciliation); document.querySelector("#newCategoryButton").addEventListener("click", newCategory); document.querySelector("#categoryForm").addEventListener("submit", saveCategory); document.querySelector("#accountClass").addEventListener("change", () => updateAccountSubtypeOptions()); document.querySelector("#accountSubtype").addEventListener("change", updateCreditLimitVisibility);
document.querySelector("#newTemplateButton").addEventListener("click", newTemplate); document.querySelector("#templateForm").addEventListener("submit", saveTemplate); document.querySelector("#transactionTemplate").addEventListener("change", applyTransactionTemplate);
document.querySelector("#deleteCategoryButton").addEventListener("click", removeEditedCategory);
document.querySelector("#deleteTemplateButton").addEventListener("click", removeEditedTemplate);
document.querySelector("#releaseHistoryButton").addEventListener("click", () => document.querySelector("#releaseHistoryDialog").showModal());
document.querySelector("#changeReimbursementCategoryButton").addEventListener("click", changeReimbursementCategory);
document.querySelector("#reimbursementCategoryForm").addEventListener("submit", saveReimbursementCategory);
document.querySelector("#newSinkingFundButton").addEventListener("click", newSinkingFund);
document.querySelector("#monthlyRecurringGroupBy").addEventListener("change", renderMonthlyRecurring);
document.querySelector("#sinkingFundForm").addEventListener("submit", saveSinkingFund);
document.querySelector("#deleteSinkingFundButton").addEventListener("click", removeEditedSinkingFund);
document.querySelector("#newFlexibleBudgetButton").addEventListener("click", newFlexibleBudget);
document.querySelector("#flexibleBudgetCategory").addEventListener("change", updateFlexibleSuggestion);
document.querySelector("#flexibleBudgetForm").addEventListener("submit", saveFlexibleBudget);
document.querySelector("#deleteFlexibleBudgetButton").addEventListener("click", removeFlexibleBudget);
document.querySelector("#newExpectedIncomeButton").addEventListener("click", newExpectedIncome);
document.querySelector("#expectedIncomeForm").addEventListener("submit", saveExpectedIncome);
document.querySelector("#deleteExpectedIncomeButton").addEventListener("click", removeExpectedIncome);
document.querySelector("#newPlannedPaymentButton").addEventListener("click", newPlannedPayment);
document.querySelector("#plannedPaymentForm").addEventListener("submit", savePlannedPayment);
document.querySelector("#deletePlannedPaymentButton").addEventListener("click", removePlannedPayment);
document.querySelector("#budgetSettingsForm").addEventListener("submit", saveBudgetSettings);
function refreshSinkingFundDate(){const value=document.querySelector("#sinkingFundAsOfDate").value;if(value&&value!==loadedSinkingFundDate)loadBudget();}
document.querySelector("#sinkingFundAsOfDate").addEventListener("change",refreshSinkingFundDate);
document.querySelector("#sinkingFundAsOfDate").addEventListener("blur",refreshSinkingFundDate);
document.querySelector("#sinkingFundAsOfDate").addEventListener("keydown",(event)=>{if(event.key==="Enter"){event.preventDefault();refreshSinkingFundDate();}});
document.querySelector("#minimumBalance").addEventListener("input",updateMinimumHelper);
document.querySelector("#minimumCautionPercent").addEventListener("input",updateMinimumHelper);document.querySelector("#creditLimit").addEventListener("input",updateUtilizationWarningHelper);document.querySelector("#utilizationWarning").addEventListener("input",updateUtilizationWarningHelper);document.querySelector("#deleteTransactionButton").addEventListener("click",removeEditedTransaction);document.querySelector("#deleteTransferButton").addEventListener("click",removeEditedTransfer);const temporaryNotification=document.querySelector("#temporaryNotification");temporaryNotification.addEventListener("mouseenter",pauseNotificationTimer);temporaryNotification.addEventListener("mouseleave",resumeNotificationTimer);temporaryNotification.addEventListener("focusin",pauseNotificationTimer);temporaryNotification.addEventListener("focusout",resumeNotificationTimer);document.querySelector("#temporaryNotificationClose").addEventListener("click",closeNotification);
document.querySelector("#themeSetting").addEventListener("change", (event) => applyTheme(event.target.value));
document.querySelector("#mainNavigation").addEventListener("click", useSameTabNavigation);
document.querySelector(".settingsNavigation").addEventListener("click", useSettingsFragmentNavigation);
document.querySelector(".budgetNavigation").addEventListener("click", useBudgetFragmentNavigation);
document.querySelector("#transactionCategory").addEventListener("change", updateReimbursementAvailability); document.querySelector("#alsoLogReimbursement").addEventListener("change", updateReimbursementFields);
document.querySelector("#dashboardTransactionButton").addEventListener("click", () => newTransaction()); document.querySelector("#dashboardTransferButton").addEventListener("click", newTransfer); document.querySelector("#hideZeroBalances").addEventListener("change", loadDashboard); document.querySelector("#dashboardPeriod").addEventListener("change", (event) => { const custom = event.target.value === "custom"; document.querySelector("#dashboardCustomDates").hidden = !custom; document.querySelector("#dashboardStartDate").disabled = !custom; document.querySelector("#dashboardEndDate").disabled = !custom; if (!custom) loadDashboard(); }); document.querySelector("#dashboardStartDate").addEventListener("change", () => { if (document.querySelector("#dashboardEndDate").value) loadDashboard(); }); document.querySelector("#dashboardEndDate").addEventListener("change", () => { if (document.querySelector("#dashboardStartDate").value) loadDashboard(); });
document.querySelector("#dashboardBreakdown").addEventListener("change", (event) => showDashboardBreakdown(event.target.value));
document.querySelector("#newSimulatorPlanButton").addEventListener("click",()=>openSimulatorPlanDialog(false));
document.querySelector("#emptyNewSimulatorPlanButton").addEventListener("click",()=>openSimulatorPlanDialog(false));
document.querySelector("#duplicateSimulatorPlanButton").addEventListener("click",()=>openSimulatorPlanDialog(true));
document.querySelector("#editSimulatorPlanButton").addEventListener("click",()=>openSimulatorPlanDialog("edit"));
document.querySelector("#deleteSimulatorPlanButton").addEventListener("click",removeSimulatorPlan);
document.querySelector("#deleteExecutedSimulatorRowsButton").addEventListener("click",removeExecutedSimulatorRows);
document.querySelector("#simulatorPlanDialogForm").addEventListener("submit",createSimulatorPlanFromDialog);
document.querySelector("#simulatorRowForm").addEventListener("submit",saveSimulatorExecutionState);
document.querySelector("#deleteSimulatorRowButton").addEventListener("click",async()=>{const form=document.querySelector("#simulatorRowForm");const rowId=document.querySelector("#simulatorEditRowId").value;let message=form.dataset.rowType==="buffer"?"Permanently delete this Buffer row?":"Permanently delete this Simulator row?";if(form.dataset.hasLinkedLedger==="true")message+=" The linked ledger event will not be deleted.";if(!confirm(message))return;await api(`/api/simulator/plans/${simulator.plan.id}/rows/${rowId}`,{method:"DELETE"});document.querySelector("#simulatorRowDialog").close();await loadSimulator(simulator.plan.id);});
document.querySelector("#transactionDialog").addEventListener("close",()=>{simulatorRecordingRowId=null;});
document.querySelector("#transferDialog").addEventListener("close",()=>{simulatorRecordingRowId=null;});
document.querySelector("#excludeBufferFromProjectedBalances").addEventListener("change",renderSimulatorResults);
document.querySelector("#simulatorPlanSelect").addEventListener("change",async(event)=>{if(activeSimulatorDirtyRows().length&&!confirm("Discard unsaved worksheet row edits?")){event.target.value=simulator.plan.id;return;}const nextUrl=new URL(window.location.href);nextUrl.searchParams.set("plan",event.target.value);history.pushState({},"",`${nextUrl.pathname}${nextUrl.search}${nextUrl.hash}`);await loadSimulator();});
window.addEventListener("beforeunload",warnSimulatorUnsaved);
window.addEventListener("popstate", () => { if (currentPath === "/transactions") loadTransactions(); if (currentPath === "/budget" && budget) { renderBudget(); renderMonthlyRecurring(); renderExpectedIncome(); renderPlannedPayments(); showBudgetSection(); } if (currentPath === "/settings") showSettingsSection(); if (currentPath === "/simulator") loadSimulator(); });
window.addEventListener("hashchange", () => { if (currentPath === "/settings") showSettingsSection(); if (currentPath === "/budget") showBudgetSection(); });
function applyAuditorControls() {
  document.querySelector("#releaseHistoryButton").dataset.viewControl = "true";
  document.querySelector("#filterForm button[type='submit']").dataset.viewControl = "true";
  for (const control of document.querySelectorAll("#settingsPage input, #settingsPage select, dialog input, dialog select")) {
    if (control.id !== "themeSetting") control.disabled = true;
  }
  for (const button of document.querySelectorAll("#releaseHistoryDialog [data-close]")) button.dataset.viewControl = "true";
}

function renderAuditorWorksheet() {
  const body = document.querySelector("#simulatorRows");
  body.replaceChildren();
  for (const savedRow of simulator.plan.rows) {
    const row = body.insertRow();
    const accountName = (id) => accounts.find((account) => account.id === id)?.name || "—";
    const accountNames = savedRow.fromAccountId && savedRow.toAccountId ? `${accountName(savedRow.fromAccountId)} → ${accountName(savedRow.toAccountId)}` : accountName(savedRow.fromAccountId || savedRow.toAccountId);
    for (const value of [savedRow.rowType + (savedRow.executed ? " (Executed)" : ""), "—", accountNames, savedRow.label || "—", dollars(savedRow.effectiveAmountCents ?? savedRow.amountCents ?? 0), "—"]) row.insertCell().textContent = value;
  }
}

async function loadIntegrationSecrets() {
  const secrets = await api("/api/integration-secrets");
  const body = document.querySelector("#integrationSecretRows");
  body.replaceChildren();
  for (const secret of secrets) {
    const row = body.insertRow();
    row.insertCell().textContent = secret.label + (secret.active ? "" : " (revoked)");
    const value = row.insertCell(); value.textContent = "••••••••"; value.className = "integrationSecretValue";
    const actions = row.insertCell();
    const reveal = document.createElement("button"); reveal.type = "button"; reveal.textContent = "Reveal";
    reveal.addEventListener("click", async () => {
      try {
        if (reveal.textContent === "Hide") { value.textContent = "••••••••"; reveal.textContent = "Reveal"; }
        else { value.textContent = (await api(`/api/integration-secrets/${secret.id}/reveal`, {method: "POST"})).secret; reveal.textContent = "Hide"; }
      } catch (error) { document.querySelector("#integrationSecretStatus").textContent = error.message; }
    });
    const copy = document.createElement("button"); copy.type = "button"; copy.textContent = "Copy";
    copy.addEventListener("click", async () => {
      try {
        const result = await api(`/api/integration-secrets/${secret.id}/reveal`, {method: "POST"});
        if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(result.secret);
        else {
          const copyInput = document.createElement("textarea"); copyInput.value = result.secret;
          document.body.append(copyInput); copyInput.select();
          const copied = document.execCommand("copy"); copyInput.remove(); copy.focus();
          if (!copied) throw new Error("Copy unavailable. Reveal and copy the secret manually.");
        }
        document.querySelector("#integrationSecretStatus").textContent = "Secret copied.";
      } catch (error) { document.querySelector("#integrationSecretStatus").textContent = error.message; }
    });
    const editLabel = document.createElement("button"); editLabel.type = "button"; editLabel.textContent = "Edit Label";
    editLabel.addEventListener("click", async () => {
      const label = window.prompt("Integration label:", secret.label);
      if (label === null) return;
      try {
        await api(`/api/integration-secrets/${secret.id}`, {method: "PATCH", body: JSON.stringify({label})});
        await loadIntegrationSecrets();
        document.querySelector("#integrationSecretStatus").textContent = "Integration label updated.";
      } catch (error) { document.querySelector("#integrationSecretStatus").textContent = error.message; }
    });
    actions.append(editLabel, " ", reveal, " ", copy);
    if (secret.active) {
      const revoke = document.createElement("button"); revoke.type = "button"; revoke.textContent = "Revoke";
      revoke.addEventListener("click", async () => {
        if (!confirm(`Revoke "${secret.label}"? Existing proposals remain reviewable.`)) return;
        try { await api(`/api/integration-secrets/${secret.id}`, {method: "DELETE"}); await loadIntegrationSecrets(); }
        catch (error) { document.querySelector("#integrationSecretStatus").textContent = error.message; }
      });
      actions.append(" ", revoke);
    }
  }
}

async function refreshPendingCount() {
  if (browserSession?.role !== "owner") return;
  const result = await api("/api/pending");
  const reviewButton = document.querySelector("#reviewPendingButton");
  reviewButton.textContent = `Review Pending (${result.count})`;
  reviewButton.classList.toggle("statusYellow", result.count > 0);
  reviewButton.disabled = result.count === 0;
  document.querySelector("#pendingBannerDescription").textContent = `${result.count} pending items — ${result.transactionCount} transactions and ${result.transferCount} transfers. Review before adding them to the ledger.`;
  document.querySelector("#pendingBanner").hidden = !result.count;
}

function pendingDraft(row) {
  const fields = row.dataset.kind === "transfer" ? ["date", "fromAccountId", "toAccountId", "description", "amount"] : ["date", "accountId", "description", "categoryId", "amount"];
  const draft = {id: Number(row.dataset.id)};
  for (const name of fields) {
    const value = row.querySelector(`[data-pending-field="${name}"]`).value;
    draft[name] = name.endsWith("Id") ? Number(value) || null : value;
  }
  return draft;
}

function pendingReviewElements() {
  return [...document.querySelectorAll("#pendingRows tr, #pendingTransferRows tr")];
}

async function refreshPendingSummary() {
  await Promise.all([refreshPendingSummaryGroup(false), refreshPendingSummaryGroup(true)]);
}

async function refreshPendingSummaryGroup(selectedOnly) {
  const state = pendingSummaryState[selectedOnly ? "selected" : "all"];
  const rows = pendingReviewElements()
    .filter((row) => !selectedOnly || row.querySelector("[data-pending-selected]").checked)
    .map((row) => ({kind: row.dataset.kind, amount: row.querySelector("[data-pending-field=amount]").value}));
  const signature = JSON.stringify(rows);
  if (signature === state.signature) return;
  state.signature = signature;
  const requestId = ++state.requestId;
  const transactionTotal = document.querySelector(selectedOnly ? "#pendingSelectedTransactionTotal" : "#pendingTransactionTotal");
  const transferTotal = document.querySelector(selectedOnly ? "#pendingSelectedTransferTotal" : "#pendingTransferTotal");
  const status = document.querySelector(selectedOnly ? "#pendingSelectedSummaryStatus" : "#pendingSummaryStatus");
  if (selectedOnly && !rows.length) {
    transactionTotal.textContent = "$0.00"; transferTotal.textContent = "$0.00";
    status.textContent = "";
    return;
  }
  try {
    const summary = await api("/api/pending/summary", {method: "POST", body: JSON.stringify({rows})});
    if (requestId !== state.requestId) return;
    transactionTotal.textContent = summary.transactionDisplay ?? "Unavailable";
    transferTotal.textContent = summary.transferDisplay ?? "Unavailable";
    status.textContent = summary.transactionCents === null || summary.transferCents === null ? (selectedOnly ? "Correct invalid selected amounts to see selected totals." : "Correct invalid amounts to see all totals.") : "";
  } catch (error) {
    if (requestId !== state.requestId) return;
    state.signature = null;
    transactionTotal.textContent = "Unavailable"; transferTotal.textContent = "Unavailable";
    status.textContent = `Totals unavailable: ${error.message}`;
  }
}

function updatePendingRowActions(row) {
  const dirty = JSON.stringify(pendingDraft(row)) !== JSON.stringify(row.savedDraft);
  row.classList.toggle("unsavedRow", dirty);
  row.querySelector("[data-pending-save]").hidden = !dirty;
  row.querySelector("[data-pending-cancel]").hidden = !dirty;
  const defaultButton = row.querySelector("[data-pending-default-description]");
  if (defaultButton) {
    const description = pendingTransferDefaultDescription(row);
    const usingDefault = description !== null && pendingDraft(row).description === description;
    defaultButton.disabled = Boolean(row.saving || pendingDecisionBusy) || description === null || usingDefault;
    defaultButton.textContent = "Use default description";
    defaultButton.setAttribute("aria-label", `${defaultButton.textContent} for proposal ${row.dataset.id}`);
  }
}

async function savePendingRow(row) {
  if (row.saving || pendingDecisionBusy) return;
  const status = row.querySelector("[data-pending-status]");
  const controls = [...row.querySelectorAll("input, select, button")];
  try {
    row.saving = true;
    controls.forEach((control) => { control.disabled = true; });
    // Ignore an earlier blur-format response while saving this exact validated draft.
    for (const control of controls) if (control.dataset.moneyEnhanced) control.dataset.moneyRequestId = String(Number(control.dataset.moneyRequestId || "0") + 1);
    const amount = row.querySelector("[data-pending-field=amount]");
    const formatted = await api("/api/money/evaluate", {method: "POST", body: JSON.stringify({expression: amount.value})});
    amount.value = formatted.inputValue;
    const draft = pendingDraft(row);
    await previewPendingRow(row);
    if (!row.previewValid || row.previewSignature !== JSON.stringify(draft)) return;
    const {id, ...fields} = draft;
    const result = await api(`/api/pending/${id}`, {method: "PATCH", body: JSON.stringify({...fields, reviewedRole: row.reviewedRole, reviewVersion: row.reviewVersion})});
    row.reviewVersion = result.reviewVersion;
    row.savedDraft = {...draft};
    updatePendingRowActions(row);
    sendNotification("Row saved. Item remains Pending.");
  } catch (error) {
    status.textContent = error.message; status.hidden = false;
  } finally {
    row.saving = false;
    controls.forEach((control) => { control.disabled = false; });
    updatePendingRowActions(row);
  }
}

function pendingTransferDefaultDescription(row) {
  const destination = accounts.find((account) => account.id === Number(row.querySelector("[data-pending-field=toAccountId]").value));
  return destination ? (destination.accountClass === "credit" ? paymentDescription(destination) : `Transfer to ${destination.name}`) : null;
}

function usePendingTransferDescription(row) {
  if (row.dataset.kind !== "transfer" || row.saving || pendingDecisionBusy) return false;
  const description = pendingTransferDefaultDescription(row);
  if (description === null) return false;
  row.querySelector("[data-pending-field=description]").value = description;
  updatePendingRowActions(row); previewPendingRow(row);
  return true;
}

function cancelPendingRowChanges(row) {
  if (row.saving) return;
  for (const control of row.querySelectorAll("[data-pending-field]")) control.value = row.savedDraft[control.dataset.pendingField];
  updatePendingRowActions(row);
  previewPendingRow(row);
}

async function previewPendingRow(row, refreshSummary = true) {
  if (refreshSummary) refreshPendingSummary();
  const requestId = (row.previewRequestId || 0) + 1;
  row.previewRequestId = requestId;
  row.reviewedRole = null; row.previewValid = false; row.previewSignature = null;
  const status = row.querySelector("[data-pending-status]");
  status.textContent = ""; status.hidden = true;
  const draft = pendingDraft(row);
  try {
    const preview = await api("/api/pending/preview", {method: "POST", body: JSON.stringify({...draft, kind: row.dataset.kind})});
    if (row.previewRequestId !== requestId || JSON.stringify(pendingDraft(row)) !== JSON.stringify(draft)) return;
    row.previewValid = true; row.reviewedRole = preview.reviewedRole; row.previewSignature = JSON.stringify(draft);
    if (row.dataset.kind !== "transfer" && row.dataset.originalRole !== preview.reviewedRole) {
      status.textContent = `Reporting Role changed from ${row.dataset.originalRole} to ${preview.reviewedRole}. Current ledger effect: ${dollars(preview.ledgerAmountCents, true)}. Check this sign before approval.`;
      status.hidden = false;
    }
  } catch (error) {
    if (row.previewRequestId === requestId) { status.textContent = `Needs correction: ${error.message}`; status.hidden = false; }
  }
}

async function loadPendingReview() {
  for (const state of Object.values(pendingSummaryState)) { ++state.requestId; state.signature = null; }
  document.querySelector("#pendingSelectedTransactionTotal").textContent = "$0.00";
  document.querySelector("#pendingSelectedTransferTotal").textContent = "$0.00";
  document.querySelector("#pendingSelectedSummaryStatus").textContent = "";
  document.querySelector("#pendingSummaryStatus").textContent = "";
  const review = await api("/api/pending");
  const transactionBody = document.querySelector("#pendingRows"), transferBody = document.querySelector("#pendingTransferRows");
  transactionBody.replaceChildren(); transferBody.replaceChildren();
  document.querySelector("#pendingTransactionsHeading").textContent = `Transactions (${review.transactionCount})`;
  document.querySelector("#pendingTransfersHeading").textContent = `Transfers (${review.transferCount})`;
  document.querySelector("#pendingEmpty").hidden = Boolean(review.count);
  document.querySelector("#approvePendingButton").hidden = !review.count;
  document.querySelector("#approveAllPendingButton").hidden = !review.count;
  document.querySelector("#rejectPendingButton").hidden = !review.count;
  const previews = [];
  for (const proposal of review.rows) {
    const body = proposal.kind === "transfer" ? transferBody : transactionBody;
    const row = body.insertRow(); row.dataset.kind = proposal.kind; row.dataset.id = proposal.id; row.dataset.originalRole = proposal.originalRole; row.reviewVersion = proposal.reviewVersion;
    const selected = document.createElement("input"); selected.type = "checkbox"; selected.setAttribute("aria-label", `Select proposal ${proposal.id} from ${proposal.label}`); selected.dataset.pendingSelected = "true"; row.insertCell().append(selected);
    selected.addEventListener("change", () => refreshPendingSummary());
    row.insertCell().textContent = proposal.label;
    for (const [name, label] of (proposal.kind === "transfer" ? [["date", "Date"], ["fromAccountId", "From Account"], ["toAccountId", "To Account"], ["description", "Description"], ["amount", "Positive USD Amount"]] : [["date", "Date"], ["accountId", "Account"], ["description", "Description"], ["categoryId", "Category"], ["amount", "Entered USD Amount"]])) {
      const control = document.createElement(["accountId", "categoryId", "fromAccountId", "toAccountId"].includes(name) ? "select" : "input");
      control.dataset.pendingField = name; control.setAttribute("aria-label", `${label} for proposal ${proposal.id}`);
      if (["fromAccountId", "toAccountId"].includes(name)) setAccountOptions(control, transferAccountsWith(proposal[name]), "Select account", proposal[name]);
      else if (name === "accountId") setAccountOptions(control, activeAccountsWith(proposal.accountId), "Select account", proposal.accountId);
      else if (name === "categoryId") setMixedCategoryOptions(control, categories.filter((category) => category.active && !category.hasChildren || category.id === proposal.categoryId), "Select category", proposal.categoryId);
      else { control.type = name === "date" ? "date" : "text"; control.value = proposal[name]; if (name === "amount") { control.inputMode = "decimal"; control.dataset.money = "true"; } }
      if (control.tagName === "SELECT" && proposal[name] && control.value !== String(proposal[name])) { option(control, proposal[name], `Unavailable ${label} (ID ${proposal[name]})`); control.value = proposal[name]; }
      row.insertCell().append(control);
      const changed = (event) => {
        if (row.dataset.kind === "transfer" && name === "toAccountId" && event.type === "change" && usePendingTransferDescription(row)) return;
        updatePendingRowActions(row); previewPendingRow(row);
      };
      control.addEventListener("input", changed);
      control.addEventListener("change", changed);
      control.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && control.tagName === "INPUT" && row.classList.contains("unsavedRow")) {
          event.preventDefault(); if (!event.repeat) savePendingRow(row);
        }
      });
    }
    const status = document.createElement("div"); status.dataset.pendingStatus = "true"; status.className = "formHint"; status.setAttribute("role", "status"); status.hidden = true; row.lastElementChild.append(status);
    row.savedDraft = pendingDraft(row);
    const actions = row.insertCell();
    const save = document.createElement("button"); save.type = "button"; save.textContent = "Save Row"; save.dataset.pendingSave = "true"; save.hidden = true;
    save.addEventListener("click", () => savePendingRow(row));
    const cancel = document.createElement("button"); cancel.type = "button"; cancel.textContent = "Cancel Changes"; cancel.dataset.pendingCancel = "true"; cancel.hidden = true;
    cancel.addEventListener("click", () => cancelPendingRowChanges(row));
    const saveActions = document.createElement("span"); saveActions.className = "pendingSaveActions";
    saveActions.append(save, cancel); actions.append(saveActions, " ");
    if (proposal.kind === "transfer") {
      const defaultDescription = document.createElement("button"); defaultDescription.type = "button"; defaultDescription.textContent = "Use default description";
      defaultDescription.dataset.pendingDefaultDescription = "true";
      defaultDescription.setAttribute("aria-label", `Use default description for proposal ${proposal.id}`);
      defaultDescription.addEventListener("click", () => usePendingTransferDescription(row));
      actions.append(defaultDescription, " ");
    }
    updatePendingRowActions(row);
    previews.push(previewPendingRow(row, false));
  }
  enhanceMoneyInputs(transactionBody); enhanceMoneyInputs(transferBody);
  await Promise.all(previews);
  await refreshPendingSummary();
}

async function decidePendingSelection(decision, approveAll = false) {
  if (pendingDecisionBusy) return;
  const form = document.querySelector("#pendingForm");
  try {
    const allRows = pendingReviewElements();
    const selected = approveAll ? allRows : allRows.filter((row) => row.querySelector("[data-pending-selected]").checked);
    if (!selected.length) throw new Error("Select at least one proposal.");
    if (pendingReviewElements().some((row) => row.saving)) throw new Error("Wait for selected rows to finish saving.");
    const rows = selected.map((row) => {
      const draft = pendingDraft(row);
      if (approveAll && JSON.stringify(draft) !== JSON.stringify(row.savedDraft)) throw new Error("Save or cancel all row changes before approving all pending transactions.");
      if (decision === "approve" && (!row.previewValid || row.previewSignature !== JSON.stringify(draft))) throw new Error("Correct selected rows and wait for validation before approval.");
      return decision === "approve" ? {...draft, reviewedRole: row.reviewedRole, reviewVersion: row.reviewVersion} : {id: draft.id, reviewVersion: row.reviewVersion};
    });
    const transactionCount = selected.filter((row) => row.dataset.kind === "transaction").length;
    if (approveAll && !window.confirm(`Approve all ${rows.length} pending items — ${transactionCount} transactions and ${rows.length - transactionCount} transfers? This will add them to your ledger.`)) return;
    if (decision === "reject" && selected.some((row) => row.dataset.kind === "transfer") && !window.confirm(`Reject ${rows.length} selected pending item${rows.length === 1 ? "" : "s"} - ${transactionCount} transactions and ${rows.length - transactionCount} transfers? This cannot be undone.`)) return;
    pendingDecisionBusy = true;
    const result = await api("/api/pending/decision", {method: "POST", body: JSON.stringify({decision, rows, approveAll})});
    form.querySelector(".formError").textContent = "";
    const drafts = allRows.filter((row) => !selected.includes(row) && row.classList.contains("unsavedRow")).map((row) => ({id: row.dataset.id, draft: pendingDraft(row)}));
    await loadPendingReview();
    for (const saved of drafts) { const row = pendingReviewElements().find((item) => item.dataset.id === saved.id); if (!row) continue; for (const control of row.querySelectorAll("[data-pending-field]")) control.value = saved.draft[control.dataset.pendingField]; updatePendingRowActions(row); await previewPendingRow(row); }
    if (decision === "approve" && result.count === 1 && rows.length === 1) {
      const approved = rows[0];
      const transfer = selected[0].dataset.kind === "transfer";
      await ledgerNotification(transfer ? "Approved - Transfer added" : "Approved - Transaction added", transfer ? [approved.fromAccountId, approved.toAccountId] : [approved.accountId]);
    } else {
      sendNotification(`${decision === "approve" ? "Approved" : "Rejected"} ${result.count} proposal${result.count === 1 ? "" : "s"}.`);
    }
  } catch (error) {
    showFormError(form, error);
    if (error.message.startsWith("Category Reporting Role changed.")) {
      await Promise.all(pendingReviewElements().map((row) => previewPendingRow(row)));
    }
  } finally {
    pendingDecisionBusy = false;
    pendingReviewElements().forEach(updatePendingRowActions);
  }
}

document.querySelector("#reviewPendingButton").addEventListener("click", () => window.location.assign("/pending"));
document.querySelector("#logOutButton").addEventListener("click", async () => { await api("/api/logout", {method: "POST"}); location.assign("/sign-in"); });
document.querySelector("#integrationSecretForm").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget;
  try { await api("/api/integration-secrets", {method: "POST", body: JSON.stringify({label: document.querySelector("#integrationSecretLabel").value})}); form.reset(); form.querySelector(".formError").textContent = ""; await loadIntegrationSecrets(); }
  catch (error) { showFormError(form, error); }
});
document.querySelector("#pendingForm").addEventListener("submit", (event) => { event.preventDefault(); decidePendingSelection("approve"); });
document.querySelector("#approveAllPendingButton").addEventListener("click", () => decidePendingSelection("approve", true));
document.querySelector("#rejectPendingButton").addEventListener("click", () => decidePendingSelection("reject"));
start().catch((error) => { document.querySelector("main").textContent = error.message; });

function entryRequestId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function configurePendingEntry(kind, editing, ledgerPath = null) {
  const form = document.querySelector(`#${kind.toLowerCase()}Form`);
  form.pendingRequestId = entryRequestId(); form.pendingBusy = false;
  form.movePath = ledgerPath;
  form.moveSnapshot = ledgerPath ? api(`${ledgerPath}/pending-snapshot`).then((result) => {
    const original = JSON.parse(form.dataset.originalValues), first = result.snapshot[0];
    const matches = original.date === first.date && original.description === first.description && (kind === "Transfer" ? Number(original.fromAccountId) === first.accountId && Number(original.toAccountId) === result.snapshot[1].accountId && original.amount === first.enteredAmount : Number(original.accountId) === first.accountId && Number(original.categoryId) === first.categoryId && original.pinned === Boolean(first.pinned) && original.amount === first.enteredAmount);
    return matches ? result.snapshot : null;
  }).catch(() => null) : null;
  document.querySelector(`#save${kind}PendingButton`).hidden = editing;
  document.querySelector(`#move${kind}PendingButton`).hidden = !editing || !ledgerPath;
  updatePendingEntryAvailability();
}

function updatePendingEntryAvailability() {
  const button = document.querySelector("#saveTransactionPendingButton");
  const paired = document.querySelector("#alsoLogReimbursement").checked;
  button.disabled = paired;
  let hint = document.querySelector("#pendingReimbursementHint");
  if (!hint) { hint = document.createElement("p"); hint.id = "pendingReimbursementHint"; hint.className = "formHint"; button.closest(".dialogActions").before(hint); }
  hint.hidden = !paired || button.hidden;
  hint.textContent = "Uncheck Also log reimbursement to save one Pending item.";
}

function entryPayload(kind) {
  if (kind === "Transfer") return {type: "transfer", date: document.querySelector("#transferDate").value, fromAccountId: Number(document.querySelector("#transferFromAccount").value), toAccountId: Number(document.querySelector("#transferToAccount").value), description: document.querySelector("#transferDescription").value, amount: document.querySelector("#transferAmount").value};
  return {date: document.querySelector("#transactionDate").value, accountId: Number(document.querySelector("#transactionAccount").value), description: document.querySelector("#transactionDescription").value, categoryId: Number(document.querySelector("#transactionCategory").value), amount: document.querySelector("#transactionAmount").value, pinned: document.querySelector("#transactionPinned").checked};
}

async function saveEntryPending(kind) {
  const form = document.querySelector(`#${kind.toLowerCase()}Form`);
  if (form.pendingBusy) return;
  try {
    if (kind === "Transaction" && document.querySelector("#alsoLogReimbursement").checked) throw new Error("Uncheck Also log reimbursement to save one Pending item.");
    if (!form.reportValidity()) return;
    form.pendingBusy = true;
    await api("/api/pending/manual", {method: "POST", body: JSON.stringify({submissionId: form.pendingRequestId, ...entryPayload(kind)})});
    document.querySelector(`#${kind.toLowerCase()}Dialog`).close();
    simulatorRecordingRowId = null;
    sendNotification("Saved — Item remains Pending until Owner approval.");
    if (currentPath === "/new") window.location.assign("/");
    else { await refreshDailyPage(); await refreshPendingCount(); }
  } catch (error) { showFormError(form, error); }
  finally { form.pendingBusy = false; }
}

async function moveEntryPending(kind) {
  const form = document.querySelector(`#${kind.toLowerCase()}Form`);
  if (form.pendingBusy || !form.movePath) return;
  try {
    const payload = entryPayload(kind), {type, ...ordinaryFields} = payload;
    const comparison = kind === "Transfer" ? {...ordinaryFields, fromAccountId: String(payload.fromAccountId), toAccountId: String(payload.toAccountId), amount: submittedMoneyValue(payload.amount)} : {...payload, accountId: String(payload.accountId), categoryId: String(payload.categoryId || ""), amount: submittedMoneyValue(payload.amount), alsoLogReimbursement: false};
    // Compare fields by value; object property order is not draft state.
    const original = JSON.parse(form.dataset.originalValues);
    if (Object.keys(original).some((key) => original[key] !== comparison[key])) throw new Error("Save or Cancel Changes first. Move to Pending uses the persisted event.");
    const snapshot = await form.moveSnapshot;
    if (!snapshot) throw new Error("This event cannot move to Pending. Reopen the editor to refresh it.");
    if (!window.confirm("Move this event to Pending? It will be removed from balances and reports immediately until approval. Linked Simulator rows will return to Planned and affect projections again, retaining their saved values. Later approval will not relink them. Rejection leaves the event outside the ledger.")) return;
    form.pendingBusy = true;
    await api(`${form.movePath}/move-to-pending`, {method: "POST", body: JSON.stringify({requestId: form.pendingRequestId, snapshot})});
    document.querySelector(`#${kind.toLowerCase()}Dialog`).close();
    sendNotification("Moved — Event removed from ledger and awaiting Pending approval.");
    await refreshDailyPage(); await refreshPendingCount();
  } catch (error) { showFormError(form, error); }
  finally { form.pendingBusy = false; }
}

for (const kind of ["Transaction", "Transfer"]) {
  document.querySelector(`#save${kind}PendingButton`).addEventListener("click", () => saveEntryPending(kind));
  document.querySelector(`#move${kind}PendingButton`).addEventListener("click", () => moveEntryPending(kind));
}
document.querySelector("#alsoLogReimbursement").addEventListener("change", updatePendingEntryAvailability);
document.querySelector("#accountActive").addEventListener("change", () => {
  const active = document.querySelector("#accountActive").checked, pin = document.querySelector("#accountPinnedForSelection");
  pin.disabled = !active; if (!active) pin.checked = false;
});

function updateRequiredTemplateLink(prefix, editing) {
  const select = document.querySelector(`#${prefix}Template`), target = document.querySelector(`#${prefix}TemplateEditLink`);
  target.replaceChildren();
  const selected = templates.find((template) => template.active && template.id === Number(select.value));
  target.hidden = !editing || !selected;
  if (target.hidden) return;
  const link = document.createElement("a"); link.href = `/settings?highlightTemplate=${selected.id}#templates`;
  link.textContent = `Edit "${selected.name}" in Settings — Templates`; target.append(link);
}

function highlightSettingsTemplate() {
  const value = new URLSearchParams(location.search).get("highlightTemplate");
  if (!/^[1-9]\d*$/.test(value || "") || location.hash !== "#templates") return;
  const target = [...completeExpenseTemplates(), ...incomeTemplates()].find((template) => String(template.id) === value);
  const row = target && document.querySelector(`#templateRows tr[data-template-id="${target.id}"]`);
  if (!row) return;
  row.scrollIntoView({block: "center"}); row.classList.add("templateHighlight");
  window.setTimeout(() => row.classList.remove("templateHighlight"), 4000);
}

for (const [prefix, id] of [["sinkingFund", "sinkingFundId"], ["expectedIncome", "expectedIncomeId"]]) {
  document.querySelector(`#${prefix}Template`).addEventListener("change", () => updateRequiredTemplateLink(prefix, Boolean(document.querySelector(`#${id}`).value)));
}
