/* AI Support, Client Intelligence and the control centre. Vanilla JS, no build step. */
(function () {
  "use strict";
  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
  const body = document.body;
  function toast(msg, err) {
    const t = $("#toast"); if (!t) return;
    t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, err ? 7000 : 3500);
    const live = $("#spLive"); if (live) live.textContent = msg;
  }
  async function api(method, url, data) {
    const opt = { method, headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" };
    if (data !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(data); }
    const r = await fetch(url, opt);
    let j = {}; try { j = await r.json(); } catch (e) { /* non-JSON */ }
    if (!r.ok) throw new Error(j.detail || ("Request failed (" + r.status + ")"));
    return j;
  }
  function upload(url, file, fields) {
    return new Promise((resolve, reject) => {
      const fd = new FormData(); fd.append("file", file);
      Object.entries(fields || {}).forEach(([k, v]) => fd.append(k, v));
      const x = new XMLHttpRequest(); x.open("POST", url); x.setRequestHeader("X-Requested-With", "fetch");
      x.onload = () => { let j = {}; try { j = JSON.parse(x.responseText); } catch (e) { /* ignore */ }
        if (x.status >= 200 && x.status < 300) resolve(j); else reject(new Error(j.detail || ("Upload failed (" + x.status + ")"))); };
      x.onerror = () => reject(new Error("Network error during upload")); x.send(fd);
    });
  }
  function busy(btn, on) { if (!btn) return; btn.disabled = on; btn.classList.toggle("is-busy", on); }
  const reload = () => window.location.reload();
  const fail = (e, btn) => { busy(btn, false); toast(e.message, true); };

  // new issue
  const nf = $("#newIssueForm");
  if (nf) nf.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("button[type=submit]", nf); busy(btn, true);
    const d = Object.fromEntries(new FormData(nf).entries());
    const err = $("#ni-err"); err.hidden = true;
    try { const r = await api("POST", "/api/support/issues", d); if (!r.created) sessionStorage.setItem("spNote", "You already reported this; your message was added to the open issue."); window.location = r.url; }
    catch (ex) { busy(btn, false); err.textContent = ex.message; err.hidden = false; }
  });

  // issue page
  const iid = body.dataset.issueId;
  if (iid) {
    try { const n = sessionStorage.getItem("spNote"); if (n) { toast(n); sessionStorage.removeItem("spNote"); } } catch (e) { /* storage blocked */ }
    const mf = $("#msgForm");
    if (mf) mf.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = $("button[type=submit]", mf); busy(btn, true); toast("Claude is reading your message…");
      try { await api("POST", `/api/support/issues/${iid}/messages`, { text: $("#msgText").value }); reload(); } catch (ex) { fail(ex, btn); }
    });
    $$("input[type=file][data-slot]").forEach((inp) => inp.addEventListener("change", async () => {
      const f = inp.files[0]; if (!f) return;
      toast("Uploading " + f.name + "…");
      try { await upload(`/api/support/issues/${iid}/attachments`, f, { slot: inp.dataset.slot }); reload(); } catch (ex) { toast(ex.message, true); }
    }));
    const pf = $("#pilotForm");
    if (pf) pf.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = $("button[type=submit]", pf); busy(btn, true);
      try { const r = await api("POST", `/api/support/issues/${iid}/pilot-answers`, { answers: Object.fromEntries(new FormData(pf).entries()) });
        sessionStorage.setItem("spNote", r.missing.length ? "Saved. Still needed: " + r.missing.join("; ") : "Saved. Everything needed is here."); reload(); }
      catch (ex) { fail(ex, btn); }
    });
    const rec = $("[data-sp=reconcile]");
    if (rec) rec.addEventListener("click", async () => { busy(rec, true); toast("Reconciling day by day…");
      try { await api("POST", `/api/support/issues/${iid}/reconcile`); reload(); } catch (ex) { fail(ex, rec); } });
    // live refresh when something changes (an answer from Claude, a PR, a deploy)
    let token = body.dataset.stateToken;
    setInterval(async () => {
      if (document.hidden || ($("#msgText") && $("#msgText").value.trim())) return;
      try { const r = await api("GET", `/api/support/issues/${iid}/state`); if (r.token !== token) { token = r.token; reload(); } } catch (e) { /* offline */ }
    }, 10000);
  }
  const issueActions = {
    resolve: () => api("POST", `/api/support/issues/${iid}/resolve`, {}),
    reopen: () => api("POST", `/api/support/issues/${iid}/reopen`, { reason: ($("#msgText") || {}).value || "" }),
    engineering: () => api("POST", `/api/support/issues/${iid}/engineering`, {}),
    "confirm-ok": () => api("POST", `/api/support/issues/${iid}/confirm`, { ok: true }),
    "confirm-bad": () => api("POST", `/api/support/issues/${iid}/confirm`, { ok: false, note: ($("#msgText") || {}).value || "" }),
    approve: (b) => api("POST", `/api/support/runs/${b.dataset.run}/approve`),
    retry: (b) => api("POST", `/api/support/runs/${b.dataset.run}/retry`),
    cancel: (b) => api("POST", `/api/support/runs/${b.dataset.run}/cancel`),
    rollback: (b) => window.confirm("Open a pull request that reverts this fix?") ? api("POST", `/api/support/runs/${b.dataset.run}/rollback`) : Promise.reject(new Error("Cancelled")),
    "read-all": () => api("POST", "/api/notifications/read", {}),
    estop: (b) => api("PUT", "/api/admin/support/settings", { emergency_stop: b.dataset.on }),
  };
  $$("[data-sp]").forEach((b) => {
    const fn = issueActions[b.dataset.sp]; if (!fn) return;
    b.addEventListener("click", async () => { busy(b, true); try { const r = await fn(b); if (r && r.message) sessionStorage.setItem("spNote", r.message); reload(); } catch (ex) { fail(ex, b); } });
  });

  // Client Intelligence
  const cid = body.dataset.clientId;
  const rf = $("#ruleForm");
  if (rf && cid) rf.addEventListener("submit", async (e) => {
    e.preventDefault(); const btn = $("button[type=submit]", rf); busy(btn, true);
    try { await api("POST", `/api/intelligence/${cid}/rules`, Object.fromEntries(new FormData(rf).entries())); reload(); } catch (ex) { fail(ex, btn); }
  });
  $$("[data-rule]").forEach((b) => b.addEventListener("click", async () => {
    busy(b, true); const v = $(`[data-validated="${b.dataset.id}"]`);
    try { await api("POST", `/api/intelligence/rules/${b.dataset.id}/decide`, { approve: b.dataset.rule === "approve", validated: v ? v.checked : null }); reload(); }
    catch (ex) { fail(ex, b); }
  }));

  // control centre
  const cf = $("#ctlForm");
  if (cf) cf.addEventListener("submit", async (e) => {
    e.preventDefault(); const btn = $("button[type=submit]", cf); busy(btn, true);
    const d = {}; $$("input", cf).forEach((i) => { d[i.name] = i.type === "checkbox" ? (i.checked ? "1" : "0") : i.value; });
    try { await api("PUT", "/api/admin/support/settings", d); toast("Controls saved"); busy(btn, false); } catch (ex) { fail(ex, btn); }
  });
  $$("[data-client-ai]").forEach((i) => i.addEventListener("change", async () => {
    try { await api("PATCH", `/api/admin/support/clients/${i.dataset.clientAi}`, { ai_processing_allowed: i.checked }); toast("Saved"); } catch (ex) { i.checked = !i.checked; toast(ex.message, true); }
  }));
  $$("[data-user-lead]").forEach((i) => i.addEventListener("change", async () => {
    try { await api("PATCH", `/api/admin/support/users/${i.dataset.userLead}`, { is_lead: i.checked }); toast("Saved"); } catch (ex) { i.checked = !i.checked; toast(ex.message, true); }
  }));
  $$("[data-user-limit]").forEach((i) => i.addEventListener("change", async () => {
    try { await api("PATCH", `/api/admin/support/users/${i.dataset.userLimit}`, { monthly_limit: i.value }); toast("Limit saved"); } catch (ex) { toast(ex.message, true); }
  }));
})();
