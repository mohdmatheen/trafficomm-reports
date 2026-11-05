/* Trafficomm Reporting Workspace - vanilla JS, no build step. */
(function () {
  "use strict";
  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
  const body = document.body;
  const ROLE_EXT = { raw: [".csv", ".xlsx"], template: [".xlsx"], client_deck: [".pptx"], screenshots: [".png", ".jpg", ".jpeg", ".zip"] };
  const ROLE_LABEL = { raw: "Raw data", template: "Excel template", client_deck: "Client deck", screenshots: "Screenshots", creatives: "Screenshots (ZIP)" };

  function toast(msg, err) {
    const t = $("#toast"); if (!t) return;
    t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, err ? 7000 : 3500);
  }
  async function api(method, url, data) {
    const opt = { method, headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" };
    if (data !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(data); }
    const r = await fetch(url, opt);
    let j = {}; try { j = await r.json(); } catch (e) { /* non-JSON */ }
    if (!r.ok) throw new Error(j.detail || ("Request failed (" + r.status + ")"));
    return j;
  }
  function upload(url, file, fields, onProgress) {
    return new Promise((resolve, reject) => {
      const fd = new FormData(); fd.append("file", file);
      Object.entries(fields || {}).forEach(([k, v]) => fd.append(k, v));
      const x = new XMLHttpRequest();
      x.open("POST", url); x.setRequestHeader("X-Requested-With", "fetch");
      x.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
      x.onload = () => { let j = {}; try { j = JSON.parse(x.responseText); } catch (e) { /* ignore */ }
        if (x.status >= 200 && x.status < 300) resolve(j); else reject(new Error(j.detail || ("Upload failed (" + x.status + ")"))); };
      x.onerror = () => reject(new Error("Network error during upload"));
      x.send(fd);
    });
  }
  const ext = (n) => { const i = n.lastIndexOf("."); return i >= 0 ? n.slice(i).toLowerCase() : ""; };
  const size = (n) => n < 1048576 ? Math.round(n / 1024) + " KB" : (n / 1048576).toFixed(1) + " MB";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ------------------------------------------------------------------ shell
  function initShell() {
    const openBtn = $("[data-drawer-open]"), scrim = $(".drawer-scrim"), sb = $("#sidebar");
    if (!openBtn || !sb) return;
    const set = (open) => {
      body.classList.toggle("drawer-open", open); scrim.hidden = !open; openBtn.setAttribute("aria-expanded", String(open));
      if (open) { const f = $(".sb-close", sb); if (f) f.focus(); } else openBtn.focus();
    };
    openBtn.addEventListener("click", () => set(true));
    $$("[data-drawer-close]").forEach((el) => el.addEventListener("click", () => set(false)));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && body.classList.contains("drawer-open")) set(false); });
  }
  function initLogin() {
    const b = $("#zohoBtn"); if (!b) return;
    b.addEventListener("click", (e) => {
      if (b.classList.contains("is-busy")) { e.preventDefault(); return; }
      b.classList.add("is-busy"); b.setAttribute("aria-disabled", "true");
      $(".btn-label", b).textContent = "Redirecting to Zoho…";
      $("#zohoStatus").textContent = "Taking you to Zoho to sign in…";
    });
    // coming back with the browser's back button restores the page from cache: reset the button
    window.addEventListener("pageshow", () => { b.classList.remove("is-busy"); b.removeAttribute("aria-disabled"); $(".btn-label", b).textContent = "Sign in with Zoho"; $("#zohoStatus").textContent = ""; });
  }
  function initDashboard() {
    const f = $("form.filters"); if (!f) return;
    $$("[data-autosubmit]", f).forEach((s) => s.addEventListener("change", () => f.requestSubmit ? f.requestSubmit() : f.submit()));
    f.addEventListener("submit", () => { const p = $(".task-panel"); if (p) { p.classList.add("loading"); p.setAttribute("aria-busy", "true"); } });
  }

  // ------------------------------------------------------------------ administration
  function initAdmin() {
    const live = $("#adminLive");
    const say = (m, err) => { if (live) live.textContent = m; toast(m, err); };
    const showErr = (el, m) => { if (!el) return; el.textContent = m || ""; el.hidden = !m; };
    function openDlg(d, opener) { d._opener = opener || document.activeElement; showErr($("[data-err]", d), ""); d.showModal(); }
    $$("dialog.dlg").forEach((d) => {
      d.addEventListener("close", () => { if (d._opener && d._opener.isConnected) d._opener.focus(); });
      d.addEventListener("click", (e) => { if (e.target === d) d.close("cancel"); }); // backdrop click
    });
    $$("[data-open]").forEach((b) => b.addEventListener("click", () => { const d = $("#" + b.dataset.open); d.querySelector("form").reset(); openDlg(d, b); $("input", d).focus(); }));

    // users: client-side filters over the server-rendered table
    function bindUsers() {
      const q = $("#auQ"), role = $("#auRole"), acc = $("#auAccess");
      const apply = () => {
        const t = (q.value || "").trim().toLowerCase(); let n = 0;
        $$("[data-au-row]").forEach((r) => {
          const ok = (!t || r.dataset.name.includes(t) || r.dataset.email.includes(t)) && (!role.value || r.dataset.role === role.value)
            && (!acc.value || (acc.value === "legacy" ? r.dataset.legacy === "1" : r.dataset.access === acc.value));
          r.hidden = !ok; if (ok) n++;
        });
        $("#auCount").textContent = n + (n === 1 ? " person" : " people");
        $("#auEmpty").hidden = n !== 0;
      };
      $$("[data-au-filter]").forEach((el) => el.addEventListener("input", apply));
      $("#auClear").addEventListener("click", () => { q.value = ""; role.value = ""; acc.value = ""; apply(); q.focus(); });
      $$("[data-manage]").forEach((b) => b.addEventListener("click", () => openManage(JSON.parse(b.dataset.manage), b)));
      return apply;
    }
    let applyFilters = bindUsers();
    async function refreshUsers(focusId) {
      const keep = { q: $("#auQ").value, role: $("#auRole").value, acc: $("#auAccess").value };
      const r = await fetch("/admin?partial=users", { credentials: "same-origin" });
      if (!r.ok) { say("Saved, but the list couldn't refresh. Reload the page to see it.", true); return; }
      $("#usersBody").innerHTML = await r.text();
      $("#auQ").value = keep.q; $("#auRole").value = keep.role; $("#auAccess").value = keep.acc;
      applyFilters = bindUsers(); applyFilters();
      const b = focusId && $$("[data-manage]").find((x) => JSON.parse(x.dataset.manage).id === focusId);
      if (b) b.focus();
    }

    // grant access
    const gf = $("#grantForm"), gBtn = $("#grantSubmit");
    gf.addEventListener("submit", async (e) => {
      if (e.submitter && e.submitter.value === "cancel") return;
      e.preventDefault();
      const email = $("#g-email").value.trim();
      showErr($("#g-email-err"), ""); showErr($("[data-err]", gf), "");
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { showErr($("#g-email-err"), "Enter a company email address."); $("#g-email").setAttribute("aria-invalid", "true"); $("#g-email").focus(); return; }
      $("#g-email").removeAttribute("aria-invalid");
      if (gBtn.disabled) return; gBtn.disabled = true; gBtn.textContent = "Granting…";
      try {
        const j = await api("POST", "/api/admin/users", { email, name: $("#g-name").value.trim(), role: $("#g-role").value });
        $("#grantDlg").close("done"); say(j.message); await refreshUsers(j.user.id);
      } catch (err) { showErr($("[data-err]", gf), err.message); }
      finally { gBtn.disabled = false; gBtn.textContent = "Grant access"; }
    });

    // manage role / access
    const md = $("#manageDlg"), mf = $("#manageForm"); let cur = null;
    function openManage(u, opener) {
      cur = u;
      $("[data-m-name]", md).textContent = u.name; $("[data-m-email]", md).textContent = u.email;
      $("[data-m-legacy]", md).hidden = !u.legacy;
      $$("input[name=role]", md).forEach((r) => { r.checked = r.value === u.role; r.disabled = u.lastAdmin; });
      $("[data-m-lastadmin]", md).hidden = !u.lastAdmin;
      const tg = $("[data-m-toggle]", md);
      tg.textContent = u.active ? "Revoke access" : "Restore access"; tg.disabled = u.lastAdmin && u.active;
      tg.className = "btn " + (u.active ? "btn-danger-quiet" : "");
      openDlg(md, opener);
    }
    async function patch(body, btn) {
      if (btn.disabled) return; btn.disabled = true; const old = btn.textContent; btn.textContent = "Saving…";
      try { const j = await api("PATCH", "/api/admin/users/" + cur.id, body); md.close("done"); say(j.message); await refreshUsers(cur.id); }
      catch (err) { showErr($("[data-err]", md), err.message); }
      finally { btn.disabled = false; btn.textContent = old; }
    }
    mf.addEventListener("submit", (e) => {
      if (e.submitter && e.submitter.value === "cancel") return;
      e.preventDefault();
      const r = $("input[name=role]:checked", md);
      if (!r || r.value === cur.role) { md.close("cancel"); return; }
      patch({ role: r.value }, $("[data-m-save]", md));
    });
    $("[data-m-toggle]", md).addEventListener("click", (e) => {
      const b = e.currentTarget;
      if (cur.active && !b.dataset.confirm) { b.dataset.confirm = "1"; b.textContent = "Confirm revoke"; return; }
      delete b.dataset.confirm; patch({ active: !cur.active }, b);
    });
    md.addEventListener("close", () => { delete $("[data-m-toggle]", md).dataset.confirm; });

    // retention: save policy (never deletes) and a separate, confirmed cleanup
    const rf = $("#retForm"), rd = $("#ret-days"), rErr = $("#ret-err");
    const min = +rd.min, max = +rd.max;
    let saved = rd.value;
    const valid = () => { const v = rd.value.trim(); return /^\d+$/.test(v) && +v >= min && +v <= max; };
    rf.addEventListener("submit", async (e) => {
      e.preventDefault(); const btn = $("#retSave");
      if (!valid()) { showErr(rErr, `Enter a whole number between ${min} and ${max}.`); rd.setAttribute("aria-invalid", "true"); rd.focus(); return; }
      showErr(rErr, ""); rd.removeAttribute("aria-invalid");
      if (btn.disabled) return; btn.disabled = true; btn.textContent = "Saving…";
      try { const j = await api("PUT", "/api/admin/retention", { days: +rd.value }); saved = String(j.days);
        $("#ret-hint").textContent = `Between ${min} and ${max} days. Currently ${j.days} days.`; say(j.message); }
      catch (err) { showErr(rErr, err.message); }
      finally { btn.disabled = false; btn.textContent = "Save policy"; }
    });
    const cd = $("#cleanupDlg"), cBody = $("[data-c-body]", cd), cRun = $("[data-c-run]", cd), cBox = $("[data-c-confirm]", cd);
    const fmtDate = (s) => new Date(s).toLocaleString("en-GB", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" }) + " IST";
    $("#retReview").addEventListener("click", async (e) => {
      if (rd.value !== saved) { showErr(rErr, "Save the policy first. Cleanup uses the saved policy."); rd.focus(); return; }
      cBody.innerHTML = '<p class="muted">Checking which tasks are past the retention period…</p>';
      cBox.checked = false; cRun.disabled = true; $("[data-c-confirm-wrap]", cd).hidden = true; openDlg(cd, e.currentTarget);
      try {
        const j = await api("GET", "/api/admin/retention/preview");
        if (!j.count) { cBody.innerHTML = `<p>No tasks are older than the ${j.days}-day policy. There’s nothing to clean up.</p>`; return; }
        cBody.innerHTML = `<p><b>${j.count} task${j.count === 1 ? "" : "s"}</b> haven’t been updated since ${esc(fmtDate(j.cutoff))} (the ${j.days}-day policy). The oldest was last updated ${esc(fmtDate(j.oldest))}.</p><p class="muted small">Their uploaded files, outputs and previews are deleted too. The daily check would delete the same tasks.</p>`;
        $("[data-c-confirm-wrap]", cd).hidden = false;
      } catch (err) { cBody.innerHTML = ""; showErr($("[data-err]", cd), err.message); }
    });
    cBox.addEventListener("change", () => { cRun.disabled = !cBox.checked; });
    cRun.addEventListener("click", async () => {
      if (cRun.disabled) return; cRun.disabled = true; cRun.textContent = "Running…";
      try { const j = await api("POST", "/api/admin/retention/run", { confirm: true }); cd.close("done"); say(j.message); }
      catch (err) { showErr($("[data-err]", cd), err.message); cRun.disabled = false; }
      finally { cRun.textContent = "Run cleanup"; }
    });

    // client rules: structured editor, advanced JSON behind an expander
    const setPath = (o, path, v) => { const ks = path.split("/"); let t = o; ks.slice(0, -1).forEach((k) => { if (typeof t[k] !== "object" || t[k] === null) t[k] = {}; t = t[k]; }); t[ks[ks.length - 1]] = v; };
    function collect(form) {
      const out = JSON.parse($("[data-orig]", form).textContent); const errs = [];
      $$("[data-path]", form).forEach((el) => {
        const k = el.dataset.kind, p = el.dataset.path;
        if (k === "bool") setPath(out, p, el.checked);
        else if (k === "number") { if (el.value.trim() === "" || isNaN(+el.value)) errs.push(el); else setPath(out, p, +el.value); }
        else if (k === "nullable") setPath(out, p, el.value.trim() === "" ? null : el.value);
        else if (k === "text") setPath(out, p, el.value);
        else if (k === "lines") setPath(out, p, el.value.split("\n").map((s) => s.trim()).filter(Boolean));
        else if (k === "map") {
          const m = {};
          $$(".rf-map-row", el).forEach((r) => { const kk = $("[data-k]", r).value.trim(), vi = $("[data-v]", r);
            if (!kk && !vi.value.trim()) return; if (!kk) { errs.push($("[data-k]", r)); return; }
            if (kk in m) { errs.push($("[data-k]", r)); return; }
            m[kk] = vi.dataset.type === "number" && vi.value.trim() !== "" && !isNaN(+vi.value) ? +vi.value : vi.value; });
          setPath(out, p, m);
        }
      });
      return { out, errs };
    }
    $$(".rule").forEach((li) => {
      const form = $(".rule-form", li), tog = $("[data-rule-toggle]", li), state = $("[data-state]", form), err = $("[data-err]", form);
      let dirty = false, busy = false;
      const open = (on) => { form.hidden = !on; tog.setAttribute("aria-expanded", String(on)); tog.firstChild.textContent = on ? "Hide rules" : "View rules"; };
      tog.addEventListener("click", () => {
        if (!form.hidden && dirty && !confirm("Close without saving your changes?")) return;
        open(form.hidden);
        if (!form.hidden) $("[data-json]", form).value = JSON.stringify(JSON.parse($("[data-orig]", form).textContent), null, 2);
      });
      $("[data-cancel]", form).addEventListener("click", () => { if (dirty && !confirm("Close without saving your changes?")) return; open(false); tog.focus(); });
      form.addEventListener("input", () => { dirty = true; state.textContent = "Unsaved changes"; });
      form.addEventListener("click", (e) => {
        const add = e.target.closest("[data-add]"), rm = e.target.closest("[data-rm]");
        if (add) { const rows = $(".rf-map-rows", add.parentElement); const r = document.createElement("div"); r.className = "rf-map-row";
          r.innerHTML = '<input aria-label="Name" data-k><span aria-hidden="true">→</span><input aria-label="Value" data-v data-type="text"><button type="button" class="icon-btn" data-rm aria-label="Remove entry"><svg class="ic" aria-hidden="true"><use href="#i-x"/></svg></button>';
          rows.appendChild(r); $("input", r).focus(); dirty = true; state.textContent = "Unsaved changes"; }
        if (rm) { rm.closest(".rf-map-row").remove(); dirty = true; state.textContent = "Unsaved changes"; }
      });
      async function save(settingsObj, btn) {
        if (busy) return; busy = true; btn.disabled = true; state.textContent = "Saving…"; showErr(err, "");
        try {
          const j = await api("PUT", "/api/admin/profiles/" + form.dataset.pid, { settings: settingsObj, updated_at: form.dataset.updated });
          form.dataset.updated = j.updated_at; $("[data-orig]", form).textContent = JSON.stringify(settingsObj);
          $("[data-upd]", li).textContent = "just now"; dirty = false; state.textContent = "Saved"; say(j.message);
        } catch (e2) { state.textContent = "Save failed"; showErr(err, e2.message); }
        finally { busy = false; btn.disabled = false; }
      }
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        const { out, errs } = collect(form);
        $$("[aria-invalid]", form).forEach((x) => x.removeAttribute("aria-invalid"));
        if (errs.length) { errs.forEach((x) => x.setAttribute("aria-invalid", "true")); showErr(err, "Fix the highlighted fields: numbers must be numbers, and every entry needs a unique name."); errs[0].focus(); return; }
        save(out, e.submitter || $("button[type=submit]", form));
      });
      $("[data-save-json]", form).addEventListener("click", (e) => {
        let obj; try { obj = JSON.parse($("[data-json]", form).value); } catch (x) { showErr(err, "The JSON isn’t valid: " + x.message); $("[data-json]", form).focus(); return; }
        if (!obj || typeof obj !== "object" || Array.isArray(obj)) { showErr(err, "Rules must be a JSON object."); return; }
        save(obj, e.currentTarget).then(() => { if (state.textContent === "Saved") location.reload(); });
      });
    });
    window.addEventListener("beforeunload", (e) => { if ($$(".rule-form").some((f) => !f.hidden && $("[data-state]", f).textContent === "Unsaved changes")) { e.preventDefault(); e.returnValue = ""; } });
  }

  // ------------------------------------------------------------------ usage & costs
  function initUsage() {
    const f = $("#ucFilters"); if (!f) return;
    const range = $("#uc-range");
    $$("[data-autosubmit]", f).forEach((el) => el.addEventListener("change", () => {
      const custom = range.value === "custom";
      $$(".uc-custom", f).forEach((x) => { x.hidden = !custom; });
      if (el === range && custom) { $("#uc-start").focus(); return; }  // wait for dates
      f.requestSubmit ? f.requestSubmit() : f.submit();
    }));
    $$(".uc-custom input", f).forEach((el) => el.addEventListener("change", () => { if ($("#uc-start").value && $("#uc-end").value) f.submit(); }));

    // Sync now: debounced on the server; the button only reports what happened
    const sb = $("#ucSync");
    sb.addEventListener("click", async () => {
      if (sb.disabled) return; sb.disabled = true; const old = sb.innerHTML; sb.lastChild.textContent = "Syncing…";
      try { const j = await api("POST", "/api/admin/usage/sync"); toast(j.message, !j.started); if (j.started) setTimeout(() => location.reload(), 8000); }
      catch (e) { toast(e.message, true); }
      finally { setTimeout(() => { sb.disabled = false; sb.innerHTML = old; }, 3000); }
    });

    // employee table: filter and sort over server-rendered rows
    function bindTable() {
      const q = $("#ucQ"), tb = $("#ucEmp tbody");
      if (q) q.addEventListener("input", () => { const t = q.value.trim().toLowerCase(); $$("[data-uc-row]").forEach((r) => { r.hidden = !!t && !r.dataset.name.includes(t); }); });
      $$("#ucEmp .th-sort").forEach((b) => b.addEventListener("click", () => {
        const k = b.dataset.sort, th = b.closest("th");
        const cur = th.getAttribute("aria-sort");
        const asc = cur === "descending" ? true : (cur === "ascending" ? false : k === "name");
        $$("#ucEmp th[aria-sort]").forEach((x) => x.setAttribute("aria-sort", "none")); th.setAttribute("aria-sort", asc ? "ascending" : "descending");
        const rows = $$("[data-uc-row]", tb);
        rows.sort((a, c) => { const x = k === "name" ? a.dataset.name : +a.dataset[k], y = k === "name" ? c.dataset.name : +c.dataset[k]; return (x > y ? 1 : x < y ? -1 : 0) * (asc ? 1 : -1); });
        rows.forEach((r) => tb.appendChild(r));
      }));
    }
    bindTable();

    // live application activity over SSE. Only activity refreshes; provider-confirmed costs never change here.
    const url = body.dataset.usageStream; if (!url || !window.EventSource) return;
    let pending = null, es;
    const badge = (tone, text) => { const b = $("#streamBadge"); if (b) { b.className = "badge-st tone-" + tone; b.lastChild.textContent = text; } };
    const refresh = async () => {
      pending = null;
      if (document.activeElement && document.activeElement.closest && document.activeElement.closest("#activityBody")) return; // don't yank focus
      const qs = new URLSearchParams(new FormData(f)); qs.set("partial", "activity");
      try { const r = await fetch("/admin/usage?" + qs, { credentials: "same-origin" }); if (r.ok) { $("#activityBody").innerHTML = await r.text(); bindTable(); } } catch (e) { /* keep current */ }
    };
    es = new EventSource(url);
    es.addEventListener("activity", () => {
      const la = $("#lastActivity");
      if (la) la.textContent = "Latest request " + new Date().toLocaleString("en-GB", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" }) + " IST";
      if (!pending) pending = setTimeout(refresh, 1500);
    });
    es.addEventListener("unavailable", () => { es.close(); badge("warn", "Live updates paused"); });
    es.onerror = () => badge("warn", "Reconnecting…");
    es.onopen = () => badge("ok", "Streaming");
    window.addEventListener("pagehide", () => es.close());
  }

  // ------------------------------------------------------------------ report builder
  function initNewTask() {
    const form = $("#taskForm"); if (!form) return;
    let taskId = body.dataset.taskId || null;
    const maxBytes = (+body.dataset.maxMb || 50) * 1048576;
    const files = new Map(); // id -> summary
    const saveState = $("#saveState");
    let creating = null, saveTimer = null, saving = Promise.resolve(), submitting = false, triedSubmit = false;

    function setSave(state, text) { saveState.dataset.state = state; saveState.textContent = text; }
    function formData() {
      const fd = new FormData(form); const o = {};
      ["client_id", "profile_id", "title", "brand", "period_start", "period_end", "comparison_start", "comparison_end", "currency", "fx_usd_aed", "instructions"].forEach((k) => { o[k] = fd.get(k) || ""; });
      o.deliverables = fd.getAll("deliverables");
      return o;
    }
    async function ensureTask() {
      if (taskId) return taskId;
      if (!creating) creating = api("POST", "/api/tasks", { client_id: form.client_id.value }).then((r) => {
        taskId = String(r.id); body.dataset.taskId = taskId; history.replaceState(null, "", "/tasks/" + taskId + "/edit");
        $("#deleteDraft").hidden = false; return taskId;
      }).catch((e) => { creating = null; throw e; });
      return creating;
    }
    function save() {
      saving = saving.then(async () => {
        setSave("saving", "Saving…");
        try { await ensureTask(); await api("PATCH", "/api/tasks/" + taskId, formData()); setSave("saved", "Saved"); saveState.title = "Saved at " + new Date().toLocaleTimeString(); return true; }
        catch (e) { setSave("error", "Save failed"); saveState.title = e.message; toast("Draft not saved: " + e.message, true); return false; }
      });
      return saving;
    }
    function scheduleSave() { setSave("saving", "Saving…"); clearTimeout(saveTimer); saveTimer = setTimeout(save, 700); refresh(); }
    form.addEventListener("input", scheduleSave);
    form.addEventListener("change", scheduleSave);

    function filterProfiles() {
      const c = form.client_id.value; let first = null, selOk = false;
      $$("option", form.profile_id).forEach((o) => { const ok = !o.dataset.client || o.dataset.client === c || o.dataset.client === "None"; o.hidden = !ok; if (ok && !first) first = o; if (ok && o.selected) selOk = true; });
      if (!selOk && first) first.selected = true;
    }
    form.client_id.addEventListener("change", filterProfiles); filterProfiles();

    // unambiguous dates: show the picked date written out (e.g. "1 September 2026")
    function dateHints() {
      $$("[data-date-for]").forEach((p) => {
        const v = $("#" + p.dataset.dateFor).value; if (!v) { p.textContent = ""; return; }
        const d = new Date(v + "T00:00:00"); p.textContent = isNaN(d) ? "" : d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "long", year: "numeric" });
      });
    }
    form.addEventListener("input", (e) => { if (e.target.type === "date") dateHints(); }); dateHints();

    // comparison period toggle
    const cmpBtn = $("#cmpToggle"), cmp = $("#cmpFields");
    cmpBtn.addEventListener("click", () => {
      const open = cmp.hidden; cmp.hidden = !open; cmpBtn.setAttribute("aria-expanded", String(open));
      $("span", cmpBtn).textContent = open ? "Remove comparison period" : "Add comparison period";
      if (open) form.comparison_start.focus(); else { form.comparison_start.value = ""; form.comparison_end.value = ""; dateHints(); scheduleSave(); }
    });

    // section strip: plain in-page navigation, highlights the section in view
    const stepLinks = $$(".steps a");
    stepLinks.forEach((a) => a.addEventListener("click", (e) => {
      const t = $(a.getAttribute("href")); if (!t) return; e.preventDefault();
      t.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
      const f = $("input:not([type=hidden]):not(.sr),select:not([aria-hidden]),textarea", t); if (f) f.focus({ preventScroll: true });
      markStep(a);
    }));
    function markStep(a) { stepLinks.forEach((x) => { x.classList.toggle("on", x === a); if (x === a) x.setAttribute("aria-current", "true"); else x.removeAttribute("aria-current"); }); }
    if ("IntersectionObserver" in window) {
      // two columns can show several sections at once: highlight the earliest one in view
      const visible = new Set();
      const io = new IntersectionObserver((ents) => {
        ents.forEach((en) => { if (en.isIntersecting) visible.add(en.target.id); else visible.delete(en.target.id); });
        const a = stepLinks.find((x) => visible.has(x.getAttribute("href").slice(1))); if (a) markStep(a);
      }, { rootMargin: "-35% 0px -45% 0px" });
      stepLinks.forEach((a) => { const t = $(a.getAttribute("href")); if (t) io.observe(t); });
    }

    // ---- uploads
    const ICON = { raw: "table", template: "table", client_deck: "present", screenshots: "image", creatives: "image" };
    const svg = (n) => '<svg class="ic" aria-hidden="true"><use href="#i-' + n + '"/></svg>';
    function zoneFor(role) { return $('.dropzone[data-role="' + (role === "creatives" ? "screenshots" : role) + '"]'); }
    function renderFile(li, f) {
      files.set(f.id, f); li.dataset.fid = f.id; li.className = "fitem ok";
      const opts = Object.keys(ROLE_EXT).filter((r) => ROLE_EXT[r].includes(ext(f.name)));
      let meta = "";
      if (f.sheets && f.sheets.length) meta = f.sheets.map((s) => esc(s.name) + ": " + (s.rows == null ? "?" : s.rows) + " rows" + (s.platform ? " · " + esc(s.platform) : "") + (s.currency ? " · " + esc(s.currency) : "") + (s.dates && s.dates.min ? " · " + esc(s.dates.min) + " → " + esc(s.dates.max) : "") + (s.total_rows ? " · " + s.total_rows + " total row(s) excluded" : "") + (s.summary_blocks ? " · " + s.summary_blocks + " summary block(s) excluded" : "")).join("<br>");
      if (f.deck) meta = f.deck.slides + " slides · " + esc(f.deck.aspect) + (f.deck.unsupported ? " · " + f.deck.unsupported + " unsupported element(s)" : "") + (f.deck.summary ? "<br>" + esc(typeof f.deck.summary === "string" ? f.deck.summary : JSON.stringify(f.deck.summary)) : "");
      if (f.images != null) meta = f.images + " image(s)";
      li.innerHTML = '<div class="frow"><span class="f-ic">' + svg(ICON[f.role] || "file") + '</span><span class="fname">' + esc(f.name) + '</span><span class="fsize">' + size(f.size) + '</span><span class="fstate">Uploaded</span>' +
        (opts.length > 1 ? '<label class="sr" for="role-' + f.id + '">File type for ' + esc(f.name) + '</label><select id="role-' + f.id + '">' + opts.map((r) => '<option value="' + r + '"' + (r === f.role ? " selected" : "") + ">" + ROLE_LABEL[r] + "</option>").join("") + "</select>" : "") +
        '<button type="button" class="btn btn-quiet btn-sm" data-remove>' + svg("trash") + 'Remove<span class="sr"> ' + esc(f.name) + "</span></button></div>" +
        (meta ? '<div class="fmeta">' + meta + "</div>" : "") +
        (f.warnings && f.warnings.length ? '<ul class="fwarn">' + f.warnings.slice(0, 6).map((w) => "<li>" + esc(w) + "</li>").join("") + "</ul>" : "");
      const sel = $("select", li);
      if (sel) sel.addEventListener("change", async (e) => {
        try { const nf = await api("PATCH", "/api/files/" + f.id, { role: e.target.value }); li.remove(); const nli = document.createElement("li"); $(".filelist", zoneFor(nf.role)).appendChild(nli); renderFile(nli, nf); refresh(); toast("Moved to " + ROLE_LABEL[nf.role]); }
        catch (err) { toast(err.message, true); e.target.value = f.role; }
      });
      $("[data-remove]", li).addEventListener("click", async (ev) => {
        const btn = ev.currentTarget; btn.disabled = true;
        try { await api("PATCH", "/api/files/" + f.id, { remove: true }); files.delete(f.id); li.remove(); refresh(); toast(f.name + " removed"); }
        catch (err) { toast(err.message, true); btn.disabled = false; }
      });
    }
    function failItem(li, file, zone, msg, canRetry) {
      li.className = "fitem err";
      li.innerHTML = '<div class="frow"><span class="f-ic">' + svg("alert") + '</span><span class="fname">' + esc(file.name) + '</span><span class="fsize">' + size(file.size) + "</span>" +
        (canRetry ? '<button type="button" class="btn btn-sm" data-retry>' + svg("refresh") + 'Retry<span class="sr"> ' + esc(file.name) + "</span></button>" : "") +
        '<button type="button" class="btn btn-quiet btn-sm" data-dismiss>Dismiss</button></div><div class="fmeta" role="alert">' + esc(msg) + "</div>";
      $("[data-dismiss]", li).addEventListener("click", () => li.remove());
      const r = $("[data-retry]", li); if (r) r.addEventListener("click", () => uploadOne(zone, file, li));
    }
    async function uploadOne(zone, file, li) {
      li.className = "fitem";
      li.innerHTML = '<div class="frow"><span class="f-ic">' + svg("upload") + '</span><span class="fname">' + esc(file.name) + '</span><span class="fsize">' + size(file.size) + '</span><span class="fstate pct">0%</span></div><div class="bar" role="progressbar" aria-label="Uploading ' + esc(file.name) + '" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><i></i></div><div class="fmeta">Uploading…</div>';
      try {
        await ensureTask();
        const f = await upload("/api/tasks/" + taskId + "/files", file, { role: zone.dataset.role }, (p) => {
          const n = Math.round(p * 100); $(".bar i", li).style.width = n + "%"; $(".bar", li).setAttribute("aria-valuenow", n); $(".pct", li).textContent = n + "%";
          if (p >= 1) $(".fmeta", li).textContent = "Checking the file…";
        });
        renderFile(li, f);
      } catch (e) { failItem(li, file, zone, e.message, true); }
      refresh();
    }
    async function handleFiles(zone, list) {
      const allowed = zone.dataset.accept.split(",");
      const arr = zone.dataset.multiple ? list : list.slice(0, 1);
      for (const file of arr) {
        const li = document.createElement("li"); $(".filelist", zone).appendChild(li);
        if (!allowed.includes(ext(file.name))) { failItem(li, file, zone, "This file type isn't supported here. Use " + allowed.join(", ").toUpperCase().replace(/\./g, "") + ".", false); continue; }
        if (file.size > maxBytes) { failItem(li, file, zone, "This file is larger than the " + (maxBytes / 1048576) + " MB limit.", false); continue; }
        await uploadOne(zone, file, li);
      }
    }
    $$(".dropzone").forEach((zone) => {
      const input = $("input[type=file]", zone);
      $$(".dz-pick", zone).forEach((b) => b.addEventListener("click", () => input.click()));
      input.addEventListener("change", () => { handleFiles(zone, Array.from(input.files)); input.value = ""; });
      let depth = 0;
      zone.addEventListener("dragenter", (e) => { e.preventDefault(); depth++; zone.classList.add("over"); });
      zone.addEventListener("dragover", (e) => { e.preventDefault(); });
      zone.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; zone.classList.remove("over"); } });
      zone.addEventListener("drop", (e) => { e.preventDefault(); depth = 0; zone.classList.remove("over"); handleFiles(zone, Array.from(e.dataTransfer.files)); });
    });
    // dropping a file outside a zone must not navigate away from the draft
    ["dragover", "drop"].forEach((ev) => window.addEventListener(ev, (e) => { if (!e.target.closest || !e.target.closest(".dropzone")) e.preventDefault(); }));
    try { JSON.parse($("#draftFiles").textContent || "[]").forEach((f) => { const li = document.createElement("li"); $(".filelist", zoneFor(f.role)).appendChild(li); renderFile(li, f); }); } catch (e) { /* none */ }

    // ---- requirements, inline errors
    function problems() {
      const d = formData(), out = [];
      const hasRaw = Array.from(files.values()).some((f) => f.role === "raw");
      const hasDeck = Array.from(files.values()).some((f) => f.role === "client_deck");
      if (!d.title.trim()) out.push({ id: "err-title", field: "#f-title", msg: "Give the task a title.", short: "add a task title" });
      if (d.period_start && d.period_end && d.period_start > d.period_end) out.push({ id: "err-period", field: "#f-ps", msg: "The reporting start is after the end.", short: "fix the reporting dates" });
      const fx = (d.fx_usd_aed || "").trim();
      if (fx && (isNaN(+fx) || +fx < 1 || +fx > 10)) out.push({ id: "err-fx", field: "#f-fx", msg: "Enter a rate such as 3.6725.", short: "check the USD → AED rate" });
      if (!d.deliverables.length) out.push({ id: "err-outputs", field: 'input[name=deliverables]', msg: "Choose at least one output.", short: "choose an output" });
      if (!hasRaw) out.push({ id: "err-raw", field: "#zone-raw .dz-pick", zone: "#zone-raw", msg: "Upload at least one raw report (CSV or XLSX).", short: "upload raw reports" });
      if (d.deliverables.includes("deck_update") && !hasDeck) out.push({ id: "err-deck", field: "#zone-deck .dz-pick", zone: "#zone-deck", msg: "Upload the client deck to update it, or deselect “Updated client PowerPoint”.", short: "upload the client PowerPoint" });
      return out;
    }
    function detectedCurrencies() {
      const seen = {};
      files.forEach((f) => (f.sheets || []).forEach((s) => { const k = s.currency || "not stated"; (seen[k] = seen[k] || []).push(f.name); }));
      const el = $("#detectedCur"); const keys = Object.keys(seen);
      if (!keys.length) { el.hidden = true; return; }
      el.hidden = false;
      el.textContent = "Currency in your uploads: " + keys.map((k) => k + " (" + Array.from(new Set(seen[k])).length + " file" + (new Set(seen[k]).size > 1 ? "s" : "") + ")").join(", ") + ".";
    }
    function refresh() {
      const ps = problems();
      const want = formData().deliverables.includes("deck_update");
      const tag = $("#deckTag"); tag.textContent = want ? "Required" : "Optional"; tag.classList.toggle("need", want);
      const hint = $("#submitHint");
      hint.textContent = ps.length ? "To continue: " + ps.map((p) => p.short).join(", ") + "." : "";
      $("#submitBtn").classList.toggle("incomplete", ps.length > 0);
      if (triedSubmit) showErrors(ps); else showErrors(ps.filter((p) => p.id === "err-deck" && Array.from(files.values()).length));
      detectedCurrencies();
    }
    function showErrors(ps) {
      $$(".f-err", form).forEach((e) => { e.hidden = true; e.textContent = ""; });
      $$("[aria-invalid]", form).forEach((e) => e.removeAttribute("aria-invalid"));
      $$(".dropzone.invalid").forEach((z) => z.classList.remove("invalid"));
      ps.forEach((p) => {
        const e = $("#" + p.id); if (e) { e.textContent = p.msg; e.hidden = false; }
        const f = $(p.field); if (f && f.matches("input:not([type=checkbox]),select,textarea")) f.setAttribute("aria-invalid", "true");
        if (p.zone) $(p.zone).classList.add("invalid");
      });
    }
    refresh();

    const del = $("#deleteDraft");
    del.addEventListener("click", async () => {
      if (!taskId || !confirm("Delete this draft and its uploaded files? This can't be undone.")) return;
      del.disabled = true;
      try { await api("POST", "/api/tasks/" + taskId + "/delete"); location.href = "/tasks"; } catch (e) { toast(e.message, true); del.disabled = false; }
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (submitting) return; // never queue a second job
      triedSubmit = true;
      const err = $("#formError"); err.hidden = true;
      const ps = problems(); showErrors(ps);
      if (ps.length) {
        const f = $(ps[0].field); if (f) { f.scrollIntoView({ block: "center" }); f.focus({ preventScroll: true }); }
        return;
      }
      submitting = true;
      const btn = $("#submitBtn"); btn.disabled = true; btn.classList.add("is-busy"); btn.firstChild.textContent = "Submitting…";
      try {
        clearTimeout(saveTimer);
        if (!(await save())) throw new Error("The draft couldn't be saved. Check your connection and try again.");
        const r = await api("POST", "/api/tasks/" + taskId + "/submit"); location.href = r.url;
      } catch (ex) {
        err.innerHTML = '<svg class="ic" aria-hidden="true"><use href="#i-alert"/></svg><div><b>Not submitted</b><p></p></div>'; $("p", err).textContent = ex.message; err.hidden = false;
        err.scrollIntoView({ block: "center" });
        submitting = false; btn.disabled = false; btn.classList.remove("is-busy"); btn.firstChild.textContent = "Submit for processing";
      }
    });
    window.addEventListener("beforeunload", (e) => { if (saveState.dataset.state === "saving" || saveState.dataset.state === "error") { e.preventDefault(); e.returnValue = ""; } });
  }

  // ------------------------------------------------------------------ task workspace
  function initWorkspace() {
    const tid = body.dataset.taskId; if (!tid) return;
    let token = body.dataset.stateToken;
    let dirty = false, busy = false;
    const url = new URL(location.href);

    // ---- tabs (ARIA tabs; the active tab is kept in the URL so background refreshes keep it)
    const tabs = $$('[role=tab]');
    function showTab(key, focus) {
      const btn = tabs.find((t) => t.dataset.tab === key) || tabs[0];
      tabs.forEach((t) => { const on = t === btn; t.setAttribute("aria-selected", String(on)); t.tabIndex = on ? 0 : -1; $("#" + t.getAttribute("aria-controls")).hidden = !on; });
      if (focus) btn.focus();
      const u = new URL(location.href); if (btn.dataset.tab === "overview") u.searchParams.delete("tab"); else u.searchParams.set("tab", btn.dataset.tab);
      history.replaceState(null, "", u.pathname + u.search + u.hash);
      const vf = $("#versionForm input[name=tab]"); if (vf) vf.value = btn.dataset.tab === "overview" ? "" : btn.dataset.tab;
    }
    tabs.forEach((t, i) => {
      t.addEventListener("click", () => showTab(t.dataset.tab));
      t.addEventListener("keydown", (e) => {
        let j = null;
        if (e.key === "ArrowRight") j = (i + 1) % tabs.length; else if (e.key === "ArrowLeft") j = (i - 1 + tabs.length) % tabs.length;
        else if (e.key === "Home") j = 0; else if (e.key === "End") j = tabs.length - 1;
        if (j !== null) { e.preventDefault(); showTab(tabs[j].dataset.tab, true); }
      });
    });
    const HASH_TAB = { "#validation": "validation", "#files": "files", "#outputs": "overview", "#questions": "overview", "#progress": "overview", "#feedback": "overview" };
    showTab(url.searchParams.get("tab") || HASH_TAB[location.hash] || "overview");
    if (location.hash === "#questions" || location.hash === "#outputs" || location.hash === "#validation") { const el = $(location.hash); if (el) setTimeout(() => el.scrollIntoView({ block: "start" }), 50); }
    if (location.hash === "#feedback") { const f = $("#fb-text"); if (f) setTimeout(() => f.focus(), 50); }
    $$("[data-tab-link]").forEach((a) => a.addEventListener("click", (e) => {
      e.preventDefault(); showTab(a.dataset.tabLink);
      const href = a.getAttribute("href"); const target = href && href.startsWith("#vg-") ? $(href) : $("#tab-" + a.dataset.tabLink);
      if (target) { target.scrollIntoView({ block: "start" }); target.focus({ preventScroll: true }); }
    }));
    const jf = $(".jump-fb"); if (jf) jf.addEventListener("click", (e) => { e.preventDefault(); $("#feedback").scrollIntoView({ block: "start" }); const f = $("#fb-text"); if (f) f.focus({ preventScroll: true }); });

    // ---- version selector: reload the page for that version (outputs, preview, checks and tracker all change together)
    const vs = $("#versionSelect");
    if (vs) vs.addEventListener("change", () => {
      if (dirty && !confirm("Switch version? Your unsent feedback is kept as a draft on this device.")) { vs.value = body.dataset.view; return; }
      $("#versionForm").submit();
    });

    // ---- preview: deck/workbook switch and slide thumbnails
    $$("[data-pv-switch]").forEach((b) => b.addEventListener("click", () => switchPreview(b.dataset.pvSwitch)));
    function switchPreview(k) {
      $$("[data-pv]").forEach((p) => { p.hidden = p.dataset.pv !== k; });
      $$("[data-pv-switch]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.pvSwitch === k)));
    }
    $$("[data-preview]").forEach((b) => b.addEventListener("click", () => { showTab("overview"); switchPreview(b.dataset.preview); $("#preview").scrollIntoView({ block: "start" }); }));
    $$(".pv").forEach((pv) => {
      const main = $("[data-pv-main]", pv), cap = $("[data-pv-cap]", pv), full = $("[data-pv-full]", pv);
      const thumbs = $$(".pv-thumb", pv);
      function pick(b) {
        thumbs.forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
        main.src = b.dataset.src; full.href = b.dataset.src; cap.textContent = b.dataset.unit + " " + b.dataset.n;
        main.alt = main.alt.replace(/(slide|page) \d+/i, b.dataset.unit.toLowerCase() + " " + b.dataset.n);
      }
      thumbs.forEach((b, i) => {
        b.addEventListener("click", () => pick(b));
        b.addEventListener("keydown", (e) => { const j = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1 : null; if (j !== null && thumbs[j]) { e.preventDefault(); thumbs[j].focus(); pick(thumbs[j]); } });
      });
    });

    // ---- background updates: refresh only when nothing is being typed; keep tab + version via the URL
    document.addEventListener("input", (e) => { if (e.target.closest("form") && e.target.id !== "versionSelect") dirty = true; });
    async function poll() {
      try {
        const s = await api("GET", "/api/tasks/" + tid + "/state");
        if (s.token !== token) {
          $("#liveStatus").textContent = "Task status: " + s.label;
          if (!dirty && !busy) { sessionStorage.setItem("tc-scroll", String(window.scrollY)); location.reload(); return; }
          toast("This task has been updated (" + s.label + "). Your feedback draft is kept; refresh when you're ready.");
          token = s.token;
        }
        setTimeout(poll, s.processing ? 2500 : 6000);
      } catch (e) { setTimeout(poll, 10000); }
    }
    setTimeout(poll, body.dataset.processing === "1" ? 1500 : 4000);
    try { const y = sessionStorage.getItem("tc-scroll"); if (y) { sessionStorage.removeItem("tc-scroll"); window.scrollTo(0, +y); } } catch (e) { /* ignore */ }
    const m = $("#msgs"); if (m) m.scrollTop = m.scrollHeight;

    // ---- actions (approve targets the explicit version on the button)
    $$("[data-action]").forEach((b) => b.addEventListener("click", async () => {
      const a = b.dataset.action;
      const confirmText = { cancel: "Cancel processing of this version? Earlier versions are not affected.", delete: "Delete this task, its uploads and all outputs? This cannot be undone.",
        approve: b.textContent.trim().replace(/\s+/g, " ") + "? Only this version is approved; later revisions need their own approval." }[a];
      if (confirmText && !confirm(confirmText)) return;
      b.disabled = true; busy = true;
      try {
        await api("POST", "/api/tasks/" + tid + "/" + a, a === "approve" ? { version_id: +b.dataset.version } : undefined);
        if (a === "delete") { location.href = "/tasks"; return; }
        location.reload();
      } catch (e) { toast(e.message, true); b.disabled = false; busy = false; }
    }));

    // ---- clarification answers
    const qf = $("#questionsForm");
    if (qf) qf.addEventListener("submit", async (e) => {
      e.preventDefault();
      const answers = {}, save = {};
      $$("input[data-qid]", qf).forEach((i) => { if ((i.type === "radio" && i.checked) || (i.type !== "radio" && i.value.trim())) answers[i.dataset.qid] = i.value.trim(); });
      $$("input[data-save]", qf).forEach((i) => { if (i.checked) save[i.dataset.save] = true; });
      const qids = new Set($$("input[data-qid]", qf).map((i) => i.dataset.qid));
      if (Object.keys(answers).length < qids.size) { toast("Please answer every question", true); return; }
      const btn = $("button", qf); btn.disabled = true; btn.textContent = "Resuming…"; busy = true;
      try { await api("POST", "/api/tasks/" + tid + "/answers", { answers, save }); dirty = false; location.reload(); }
      catch (ex) { toast(ex.message, true); btn.disabled = false; btn.textContent = "Submit answers and continue"; busy = false; }
    });

    // ---- feedback & revisions
    const ff = $("#feedbackForm"); if (!ff) return;
    const key = "tc-fb-" + tid, text = $("#fb-text"), base = $("#fb-base"), err = $("#fbErr"), send = $("#fbSend");
    const uploaded = []; // {id, name} - uploaded once, reused if sending fails
    const pending = []; // File objects not uploaded yet
    function baseLabel() { const o = base.selectedOptions[0]; $("#fbBaseLabel").textContent = "version " + o.dataset.n; }
    base.addEventListener("change", baseLabel); baseLabel();
    try { const d = sessionStorage.getItem(key); if (d && !text.value) { text.value = d; dirty = !!d; } } catch (e) { /* ignore */ }
    text.addEventListener("input", () => { try { sessionStorage.setItem(key, text.value); } catch (e) { /* ignore */ } err.hidden = true; text.removeAttribute("aria-invalid"); });
    const list = $("#fbFiles"), input = $("#fbFilesInput");
    $("#fbAttach").addEventListener("click", () => input.click());
    function renderFiles() {
      list.innerHTML = "";
      uploaded.forEach((f, i) => { const li = document.createElement("li"); li.className = "fitem ok"; li.innerHTML = '<div class="frow"><span class="fname">' + esc(f.name) + '</span><span class="fstate">Uploaded</span><button type="button" class="btn btn-quiet btn-sm">Remove<span class="sr"> ' + esc(f.name) + '</span></button></div>'; $("button", li).onclick = () => { uploaded.splice(i, 1); api("PATCH", "/api/files/" + f.id, { remove: true }).catch(() => {}); renderFiles(); }; list.appendChild(li); });
      pending.forEach((f, i) => { const li = document.createElement("li"); li.className = "fitem"; li.innerHTML = '<div class="frow"><span class="fname">' + esc(f.name) + '</span><span class="fsize">' + size(f.size) + '</span><button type="button" class="btn btn-quiet btn-sm">Remove<span class="sr"> ' + esc(f.name) + '</span></button></div>'; $("button", li).onclick = () => { pending.splice(i, 1); renderFiles(); }; list.appendChild(li); });
    }
    input.addEventListener("change", () => { Array.from(input.files).forEach((f) => pending.push(f)); input.value = ""; dirty = true; renderFiles(); });
    ff.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (busy) return; // no duplicate revision requests
      const t = text.value.trim();
      if (!t) { err.textContent = "Describe what you would like to change."; err.hidden = false; text.setAttribute("aria-invalid", "true"); text.focus(); return; }
      busy = true; send.disabled = true; send.firstChild.textContent = "Sending…";
      try {
        while (pending.length) {
          const f = pending[0];
          const r = await upload("/api/tasks/" + tid + "/files", f, { for_revision: "1" });
          uploaded.push({ id: r.id, name: f.name }); pending.shift(); renderFiles();
        }
        const r = await api("POST", "/api/tasks/" + tid + "/feedback", { text: t, base_version_id: +base.value, file_ids: uploaded.map((f) => f.id) });
        if (!r.ok) { err.textContent = r.summary || "No change could be made. Please rephrase."; err.hidden = false; throw null; }
        try { sessionStorage.removeItem(key); } catch (x) { /* ignore */ }
        dirty = false; toast("Revision v" + r.version + " started");
        location.href = "/tasks/" + tid;
      } catch (ex) {
        if (ex) { err.textContent = "Not sent: " + ex.message + " Your text and attachments are kept."; err.hidden = false; }
        busy = false; send.disabled = false; send.firstChild.textContent = "Send feedback";
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    const p = body.dataset.page;
    initShell();
    if (p === "login") initLogin();
    if (p === "dashboard") initDashboard();
    if (p === "new_task") initNewTask();
    if (p === "workspace") initWorkspace();
    if (p === "admin") initAdmin();
    if (p === "usage") initUsage();
  });
})();
