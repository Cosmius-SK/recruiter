/* TalentFlow Console — drives the workflow API with forms instead of raw JSON. */

"use strict";

const $ = (sel) => document.querySelector(sel);

const DEFAULT_BRIEF =
  "We need a senior backend engineer for the platform team in Austin, TX (hybrid). " +
  "Python and Go on Kubernetes/AWS at scale; they will own the event-pipeline rewrite " +
  "and mentor two mid-level engineers. Engineering department, one headcount, level L5. " +
  "Hiring manager: Asha Menon.";

const STAGES = [
  { key: "intake", label: "Intake & requisition" },
  { key: "jd", label: "JD approval" },
  { key: "sourcing", label: "Sourcing & screening" },
  { key: "shortlist", label: "Shortlist" },
  { key: "prescreen", label: "Pre-screen & scheduling" },
  { key: "interviews", label: "Interviews" },
  { key: "decision", label: "Hire decision" },
  { key: "offer", label: "Offer" },
  { key: "onboarding", label: "Onboarding & BGV" },
  { key: "done", label: "Complete" },
];

const GATE_STAGE = {
  jd_approval: "jd",
  finance_approval: "jd",
  shortlist_review: "shortlist",
  interview_feedback: "interviews",
  hire_decision: "decision",
  offer_escalation: "offer",
  bgv_review: "onboarding",
};

let API_BASE = "";
let wfId = localStorage.getItem("tf_workflow_id") || null;
let busyTimer = null;

/* ----------------------------- bootstrap ------------------------------ */

document.addEventListener("DOMContentLoaded", async () => {
  $("#brief").value = DEFAULT_BRIEF;
  $("#btn-start").addEventListener("click", startWorkflow);
  $("#btn-new").addEventListener("click", () => {
    wfId = null;
    localStorage.removeItem("tf_workflow_id");
    setBusy(false);   // always dismiss a stuck overlay
    showStart();
  });
  $("#btn-load").addEventListener("click", () => {
    const id = $("#load-id").value.trim();
    if (id) { setWorkflow(id); refresh(); }
  });
  await detectApiBase();
  if (wfId) { setWorkflow(wfId); refresh(); } else { showStart(); }
});

async function detectApiBase() {
  // Same-origin single service (Cloud Run) exposes /openapi.json at the root;
  // behind the compose nginx the API lives under /api/.
  try {
    const r = await fetch("/openapi.json", { method: "GET" });
    if (r.ok) { API_BASE = ""; return; }
  } catch (e) { /* fall through */ }
  API_BASE = "/api";
}

/* ------------------------------- helpers ------------------------------ */

function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function money(n) {
  return n == null ? "—" : "$" + Number(n).toLocaleString("en-US");
}

// Minimal markdown -> HTML for agent-written JDs (input is escaped first).
function md(text) {
  const lines = esc(text).split(/\r?\n/);
  let html = "", inList = false;
  for (const line of lines) {
    const h = line.match(/^(#{1,4})\s+(.*)/);
    const li = line.match(/^\s*[-*]\s+(.*)/);
    if (li) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${li[1]}</li>`; continue; }
    if (inList) { html += "</ul>"; inList = false; }
    if (h) { html += `<h3>${h[2]}</h3>`; continue; }
    if (line.trim() === "") continue;
    html += `<p>${line}</p>`;
  }
  if (inList) html += "</ul>";
  return html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function toast(msg, isError) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("error", !!isError);
  el.hidden = false;
  setTimeout(() => { el.hidden = true; }, isError ? 8000 : 3500);
}

function setBusy(on, msg) {
  $("#busy").hidden = !on;
  if (on) {
    if (msg) $("#busy-msg").textContent = msg;
    setBusyDetail("");
    if (!busyTimer) {
      const started = Date.now();
      $("#busy-timer").textContent = "0:00";
      busyTimer = setInterval(() => {
        const s = Math.floor((Date.now() - started) / 1000);
        $("#busy-timer").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
      }, 1000);
    }
  } else if (busyTimer) { clearInterval(busyTimer); busyTimer = null; }
}

function setBusyDetail(text) {
  const el = $("#busy-detail");
  if (el) { el.textContent = text || ""; el.hidden = !text; }
}

function setWorkflow(id) {
  wfId = id;
  localStorage.setItem("tf_workflow_id", id);
  $("#wf-badge").textContent = "workflow " + id;
  $("#wf-badge").hidden = false;
}

async function api(path, options = {}) {
  // Time out swallowed requests instead of hanging forever (corporate proxies
  // sometimes hold a connection open without ever forwarding it).
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 45000);
  let r;
  try {
    r = await fetch(API_BASE + path, { ...options, signal: controller.signal });
  } catch (e) {
    if (e.name === "AbortError") {
      throw new Error("request timed out — the network may be blocking it (try a different network / mobile hotspot)");
    }
    throw new Error("network error — the request could not be sent (a proxy or firewall may be blocking it)");
  } finally {
    clearTimeout(timer);
  }
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (e) { /* keep statusText */ }
    throw new Error(`${r.status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
  }
  return r.json();
}

/* ----------------------------- API actions ---------------------------- */

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function startWorkflow() {
  const brief = $("#brief").value.trim();
  if (!brief) { toast("Write a brief first.", true); return; }
  setBusy(true, "Parsing the brief, checking budget, drafting the JD…");
  try {
    const ack = await api("/workflows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ brief }),
    });
    setWorkflow(ack.workflow_id);
    await pollUntilSettled();
  } catch (e) { setBusy(false); toast("Start failed — " + e.message, true); }
}

async function resume(decision, busyMsg) {
  setBusy(true, busyMsg || "Decision delivered — agents are continuing…");
  try {
    await api(`/workflows/${wfId}/resume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(decision),
    });
    await pollUntilSettled();
  } catch (e) { setBusy(false); toast("Resume failed — " + e.message, true); }
}

// Poll the workflow until it stops running (awaiting a human, completed, or errored).
// Each poll is a tiny, fast request — safe behind corporate proxies.
async function pollUntilSettled() {
  let failures = 0;
  for (;;) {
    let snap;
    try {
      snap = await api(`/workflows/${wfId}`);
      failures = 0;
    } catch (e) {
      // Tolerate a few transient hiccups, then give up so the UI never hangs.
      if (++failures >= 4) {
        setBusy(false);
        toast("Lost contact with the server — " + e.message, true);
        return;
      }
      await sleep(3000);
      continue;
    }
    if (snap.status === "running") {
      await showRunningProgress();
      await sleep(2500);
      continue;
    }
    setBusy(false);
    if (snap.status === "error") {
      toast("The agents hit an error — " + (snap.error || "unknown"), true);
      await loadTimeline();
      $("#panel-timeline").hidden = false;
      return;
    }
    render(snap);
    await loadTimeline();
    return;
  }
}

// While running, surface the latest agent action under the spinner.
async function showRunningProgress() {
  try {
    const events = await api(`/workflows/${wfId}/timeline`);
    if (events && events.length) {
      const last = events[events.length - 1];
      setBusyDetail(`${last.stage}: ${last.action}${last.detail ? " — " + last.detail : ""}`);
    }
  } catch (e) { /* best-effort */ }
}

async function refresh() {
  setBusy(true, "Loading workflow…");
  try {
    const snap = await api(`/workflows/${wfId}`);
    if (snap.status === "running") { await pollUntilSettled(); return; }
    setBusy(false);
    render(snap);
    await loadTimeline();
  } catch (e) {
    setBusy(false);
    toast("Could not load workflow — " + e.message, true);
    showStart();
  }
}

async function loadTimeline() {
  try {
    const events = await api(`/workflows/${wfId}/timeline`);
    renderTimeline(events);
  } catch (e) { /* timeline is best-effort */ }
}

/* ------------------------------ rendering ----------------------------- */

function showStart() {
  $("#panel-start").hidden = false;
  $("#panel-action").hidden = true;
  $("#panel-outcome").hidden = true;
  $("#panel-timeline").hidden = true;
  $("#panel-pipeline").hidden = true;
  $("#panel-artifacts").hidden = true;
  $("#wf-badge").hidden = true;
}

function render(snap) {
  $("#panel-start").hidden = true;
  $("#panel-timeline").hidden = false;
  $("#panel-pipeline").hidden = false;
  renderStepper(snap);
  renderArtifacts(snap);

  const pending = snap.pending_action;
  if (pending) {
    $("#panel-outcome").hidden = true;
    $("#panel-action").hidden = false;
    $("#action-title").textContent = pending.title || pending.type;
    $("#action-assignee").textContent = "decision owner: " + (pending.assignee || "you");
    $("#action-summary").textContent = pending.summary || "";
    renderGate(pending, snap);
  } else {
    $("#panel-action").hidden = true;
    $("#panel-outcome").hidden = false;
    const outcome = snap.outcome || "in progress";
    $("#outcome-title").textContent = outcome.replace(/_/g, " ").toUpperCase();
    $("#outcome-title").className = outcome === "hired" ? "outcome-hired" : "outcome-other";
    $("#outcome-summary").textContent = snap.outcome_summary || "";
  }
}

function renderStepper(snap) {
  const pendingType = snap.pending_action ? snap.pending_action.type : null;
  let currentKey;
  if (snap.outcome) currentKey = "done";
  else if (pendingType) currentKey = GATE_STAGE[pendingType] || "intake";
  else currentKey = "intake";
  const currentIdx = STAGES.findIndex((s) => s.key === currentKey);
  $("#stepper").innerHTML = STAGES.map((s, i) => {
    const cls = i < currentIdx ? "done" : i === currentIdx ? "current" : "";
    return `<li class="${cls}">${esc(s.label)}</li>`;
  }).join("");
}

function renderArtifacts(snap) {
  const a = snap.stage_artifacts || {};
  const bits = [];
  if (snap.requisition_id) bits.push(art("Requisition", `<span class="mono">${esc(snap.requisition_id)}</span>`));
  if (a.role) bits.push(art("Role", `${esc(a.role.title)} · ${esc(a.role.level)} · ${esc(a.role.location)}`));
  if (a.shortlist && a.shortlist.length) bits.push(art("Shortlist", a.shortlist.map((c) => `<span class="mono">${esc(c)}</span>`).join(", ")));
  if (a.chosen_candidate_id) bits.push(art("Selected", `<span class="mono">${esc(a.chosen_candidate_id)}</span>`));
  if (a.offer) bits.push(art("Offer", `${money(a.offer.base)} base · ${esc(a.offer.status)}${a.offer.start_date ? " · starts " + esc(a.offer.start_date) : ""}`));
  if (a.onboarding) bits.push(art("Onboarding", `BGV ${esc(a.onboarding.bgv_status)} · ${(a.onboarding.it_tickets || []).length} IT ticket(s) · ${(a.onboarding.welcome_emails || []).length} email(s)`));
  $("#panel-artifacts").hidden = bits.length === 0;
  $("#artifacts").innerHTML = bits.join("");
}

function art(label, html) {
  return `<div class="artifact"><h4>${esc(label)}</h4><div>${html}</div></div>`;
}

function renderTimeline(events) {
  const rows = events.map((e) => {
    const t = new Date(e.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    return `<div class="tl-row"><span class="t">${esc(t)}</span><span class="stage">${esc(e.stage)}</span>` +
      `<span class="tl-actor ${esc(e.actor)}">${esc(e.actor)}</span>` +
      `<span class="what"><strong>${esc(e.action)}</strong>${e.detail ? " — " + esc(e.detail) : ""}</span></div>`;
  });
  $("#timeline").innerHTML = rows.reverse().join("");
  const agents = events.filter((e) => e.actor === "agent").length;
  const humans = events.filter((e) => e.actor === "human").length;
  $("#timeline-stats").textContent = `${events.length} events · ${agents} agent actions · ${humans} human decisions`;
}

/* ------------------------------ the gates ----------------------------- */

function renderGate(p, snap) {
  const body = $("#action-body");
  switch (p.type) {
    case "jd_approval": return gateJd(body, p);
    case "finance_approval": return gateFinance(body, p);
    case "shortlist_review": return gateShortlist(body, p);
    case "interview_feedback": return gateInterviews(body, p, snap);
    case "hire_decision": return gateHire(body, p);
    case "offer_escalation": return gateOffer(body, p);
    case "bgv_review": return gateBgv(body, p);
    default:
      body.innerHTML = `<pre>${esc(JSON.stringify(p, null, 2))}</pre>`;
  }
}

function gateJd(body, p) {
  body.innerHTML = `
    <div class="jd-box">${md(p.jd_markdown || "")}</div>
    <div class="field-row"><label>Proposed ceiling:</label> <strong>${money(p.proposed_base_max)}</strong>
      <span class="muted small">(band max ${money(p.comp_band_max)})</span></div>
    <textarea id="jd-feedback" rows="2" placeholder="Optional: what should change? (required if you request changes)"></textarea>
    <div class="btn-row" style="margin-top:0.9rem">
      <button class="btn btn-primary" id="g-approve">Approve JD</button>
      <button class="btn btn-ghost" id="g-revise">Request changes</button>
    </div>`;
  $("#g-approve").onclick = () => resume({ approved: true }, "JD approved — publishing, sourcing and screening candidates…");
  $("#g-revise").onclick = () => {
    const fb = $("#jd-feedback").value.trim();
    if (!fb) { toast("Tell the agent what to change.", true); return; }
    resume({ approved: false, feedback: fb }, "Revising the JD against your feedback…");
  };
}

function gateFinance(body, p) {
  body.innerHTML = `
    <div class="field-row"><label>Requested ceiling:</label> <strong>${money(p.proposed_base_max)}</strong>
      <span class="muted small">(band max ${money(p.comp_band_max)})</span></div>
    <div class="field-row"><label for="fin-max">Approve up to:</label>
      <input id="fin-max" type="number" value="${p.proposed_base_max || ""}"></div>
    <div class="btn-row" style="margin-top:0.9rem">
      <button class="btn btn-primary" id="g-approve">Approve exception</button>
      <button class="btn btn-ghost" id="g-reject">Reject — stay in band</button>
    </div>`;
  $("#g-approve").onclick = () => resume({ approved: true, approved_max: Number($("#fin-max").value) }, "Exception recorded — sourcing begins…");
  $("#g-reject").onclick = () => resume({ approved: false }, "Proceeding within the approved band…");
}

function gateShortlist(body, p) {
  const auto = (p.auto_shortlist || []).map((c) =>
    `<div class="cand-card"><div class="cand-top"><h4>${esc(c.name)}</h4><span class="score">score ${esc(c.score)}</span></div>
     <p>${esc(c.rationale)}</p></div>`).join("");
  const flagged = (p.flagged || []).map((c, i) =>
    `<div class="cand-card"><div class="cand-top"><h4>${esc(c.name)}</h4><span class="score">score ${esc(c.score)} · flagged</span></div>
     <p>${esc(c.resume_summary)}</p><p>${esc(c.rationale)}</p>
     <div class="check"><input type="checkbox" id="flag-${i}" data-id="${esc(c.candidate_id)}" checked>
     <label for="flag-${i}">Add to shortlist</label></div></div>`).join("");
  body.innerHTML = `
    <h4 style="margin-bottom:0.5rem">Auto-shortlisted</h4>${auto || "<p class='muted small'>none</p>"}
    <h4 style="margin:1rem 0 0.5rem">Flagged for your review</h4>${flagged}
    <div class="btn-row" style="margin-top:0.9rem">
      <button class="btn btn-primary" id="g-confirm">Confirm shortlist</button>
    </div>`;
  $("#g-confirm").onclick = () => {
    const ids = Array.from(body.querySelectorAll("input[type=checkbox]:checked")).map((el) => el.dataset.id);
    resume({ include_ids: ids }, "Shortlist confirmed — pre-screening and scheduling interviews…");
  };
}

function gateInterviews(body, p, snap) {
  // One row per booked round; fall back to shortlist x standard rounds.
  let slots = (p.bookings || []).map((b) => ({ cid: b.candidate_id, round: b.round_name, who: b.interviewer }));
  if (!slots.length) {
    const rounds = ["Technical Deep-Dive", "System Design", "Values & Collaboration"];
    const panel = ["Asha Menon (EM)", "Diego Alvarez (Staff Eng)", "Hannah Roth (HRBP)"];
    for (const cid of p.shortlist || []) rounds.forEach((r, i) => slots.push({ cid, round: r, who: panel[i] }));
  }
  const rows = slots.map((s, i) => `
    <tr><td class="mono">${esc(s.cid)}</td><td>${esc(s.round)}</td><td>${esc(s.who)}</td>
      <td><select id="rate-${i}">${[5, 4, 3, 2, 1].map((n) => `<option ${n === 4 ? "selected" : ""}>${n}</option>`).join("")}</select></td>
      <td><input id="note-${i}" placeholder="notes (optional)"></td></tr>`).join("");
  body.innerHTML = `
    <p class="score-fill-note">You're playing the interview panel. In production this gate is filled by real
    interviewers from the ATS — the workflow stays parked (for days or weeks) until their scorecards arrive.</p>
    <table class="score-table"><thead><tr><th>candidate</th><th>round</th><th>interviewer</th><th>rating</th><th>notes</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <div class="btn-row"><button class="btn btn-primary" id="g-submit">Submit scorecards</button></div>`;
  $("#g-submit").onclick = () => {
    const verdictFor = (r) => (r >= 5 ? "strong_hire" : r >= 4 ? "hire" : r >= 2 ? "no_hire" : "strong_no_hire");
    const scorecards = slots.map((s, i) => {
      const rating = Number($(`#rate-${i}`).value);
      return { candidate_id: s.cid, round_name: s.round, interviewer: s.who, rating,
        verdict: verdictFor(rating), notes: $(`#note-${i}`).value || "" };
    });
    resume({ scorecards }, "Scorecards in — consolidating feedback into recommendations…");
  };
}

function gateHire(body, p) {
  const recs = (p.recommendations || []).map((r) => `
    <div class="cand-card"><div class="cand-top"><h4>${esc(r.name || r.candidate_id)}</h4>
      <span class="score">${esc(r.recommendation)} · ${esc(r.confidence)} confidence</span></div>
      <p>${esc(r.summary)}</p>
      <div class="btn-row" style="margin-top:0.6rem">
        <button class="btn ${r.recommendation === "hire" ? "btn-primary" : "btn-ghost"} btn-sm g-pick"
          data-id="${esc(r.candidate_id)}">Hire ${esc((r.name || r.candidate_id).split(" ")[0])}</button>
      </div></div>`).join("");
  body.innerHTML = `${recs}
    <div class="btn-row" style="margin-top:0.6rem">
      <button class="btn btn-ghost" id="g-none">Close without hiring</button>
    </div>`;
  body.querySelectorAll(".g-pick").forEach((btn) => {
    btn.onclick = () => resume({ candidate_id: btn.dataset.id }, "Decision recorded — negotiating the offer…");
  });
  $("#g-none").onclick = () => resume({ candidate_id: "none" }, "Closing the requisition…");
}

function gateOffer(body, p) {
  const log = (p.negotiation_log || []).map((l) => `<p>${esc(l)}</p>`).join("");
  body.innerHTML = `
    <div class="field-row"><label>Candidate is holding out for:</label> <strong>${money(p.requested_base)}</strong></div>
    <div class="field-row"><label>Current approved ceiling:</label> ${money(p.current_ceiling)}
      <span class="muted small">(band max ${money(p.comp_band_max)})</span></div>
    ${log ? `<div class="nego-log">${log}</div>` : ""}
    <div class="field-row"><label for="esc-max">Approve up to:</label>
      <input id="esc-max" type="number" value="${p.requested_base || p.current_ceiling || ""}"></div>
    <div class="btn-row" style="margin-top:0.9rem">
      <button class="btn btn-primary" id="g-approve">Approve exception</button>
      <button class="btn btn-ghost" id="g-hold">Hold the ceiling</button>
    </div>`;
  $("#g-approve").onclick = () => resume({ approved: true, new_ceiling: Number($("#esc-max").value) }, "Exception granted — closing the offer…");
  $("#g-hold").onclick = () => resume({ approved: false }, "Holding firm — final offer within ceiling…");
}

function gateBgv(body, p) {
  body.innerHTML = `
    <div class="nego-log"><strong>Vendor finding (${esc(p.bgv_case_id || "case")}):</strong><br>${esc(p.detail)}</div>
    <textarea id="bgv-note" rows="2" placeholder="Adjudication note (recommended)"></textarea>
    <div class="btn-row" style="margin-top:0.9rem">
      <button class="btn btn-primary" id="g-clear">Clear — proceed to hire</button>
      <button class="btn btn-ghost" id="g-fail">Fail BGV — release candidate</button>
    </div>`;
  $("#g-clear").onclick = () => resume({ cleared: true, note: $("#bgv-note").value || "adjudicated via console" }, "Cleared — completing the hire…");
  $("#g-fail").onclick = () => resume({ cleared: false, note: $("#bgv-note").value || "failed adjudication" }, "Candidate released — returning to backup selection…");
}
