/* Trafficomm Reporting Workspace - vanilla JS, no build step. */
(function () {
  "use strict";
  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
  const body = document.body;
  const ROLE_EXT = { raw: [".csv", ".xlsx"], template: [".xlsx"], client_deck: [".pptx"], creatives: [".zip"] };
  const ROLE_LABEL = { raw: "Raw data", template: "Excel template", client_deck: "Client deck", creatives: "Creative images (ZIP)" };

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

  // ------------------------------------------------------------------ new task
  function initNewTask() {
    const form = $("#taskForm"); if (!form) return;
    let taskId = body.dataset.taskId || null;
    const files = new Map(); // id -> summary
    const saveState = $("#saveState");
    let creating = null, saveTimer = null, saving = Promise.resolve();

    function formData() {
      const fd = new FormData(form); const o = {};
      ["client_id", "profile_id", "title", "brand", "period_start", "period_end", "comparison_start", "comparison_end", "currency", "instructions"].forEach((k) => { o[k] = fd.get(k) || ""; });
      o.deliverables = fd.getAll("deliverables");
      return o;
    }
    async function ensureTask() {
      if (taskId) return taskId;
      if (!creating) creating = api("POST", "/api/tasks", { client_id: form.client_id.value }).then((r) => {
        taskId = String(r.id); body.dataset.taskId = taskId; history.replaceState(null, "", "/tasks/" + taskId + "/edit"); return taskId;
      });
      return creating;
    }
    function save() {
      saving = saving.then(async () => {
        try { await ensureTask(); await api("PATCH", "/api/tasks/" + taskId, formData()); saveState.textContent = "Draft saved " + new Date().toLocaleTimeString(); }
        catch (e) { saveState.textContent = "Not saved: " + e.message; }
      });
      return saving;
    }
    function scheduleSave() { saveState.textContent = "Saving…"; clearTimeout(saveTimer); saveTimer = setTimeout(save, 600); checkDeck(); }
    form.addEventListener("input", scheduleSave);
    form.addEventListener("change", scheduleSave);

    function filterProfiles() {
      const c = form.client_id.value; let first = null, selOk = false;
      $$("option", form.profile_id).forEach((o) => { const ok = !o.dataset.client || o.dataset.client === c || o.dataset.client === "None"; o.hidden = !ok; if (ok && !first) first = o; if (ok && o.selected) selOk = true; });
      if (!selOk && first) first.selected = true;
    }
    form.client_id.addEventListener("change", filterProfiles); filterProfiles();

    function zoneFor(role) { return $('.dropzone[data-role="' + (role === "creatives" ? "client_deck" : role) + '"]'); }
    function renderFile(li, f) {
      files.set(f.id, f); li.dataset.fid = f.id; li.className = "fitem";
      const opts = Object.keys(ROLE_EXT).filter((r) => ROLE_EXT[r].includes(ext(f.name)));
      let meta = "";
      if (f.sheets && f.sheets.length) meta = f.sheets.map((s) => esc(s.name) + ": " + (s.rows == null ? "?" : s.rows) + " rows" + (s.platform ? " · " + esc(s.platform) : "") + (s.currency ? " · " + esc(s.currency) : "") + (s.dates && s.dates.min ? " · " + esc(s.dates.min) + " → " + esc(s.dates.max) : "") + (s.total_rows ? " · " + s.total_rows + " total row(s) excluded" : "") + (s.summary_blocks ? " · " + s.summary_blocks + " summary block(s) excluded" : "")).join("<br>");
      if (f.deck) meta = f.deck.slides + " slides · " + esc(f.deck.aspect) + (f.deck.unsupported ? " · " + f.deck.unsupported + " unsupported element(s)" : "") + (f.deck.summary ? "<br>" + esc(typeof f.deck.summary === "string" ? f.deck.summary : JSON.stringify(f.deck.summary)) : "");
      if (f.images != null) meta = f.images + " image(s)";
      li.innerHTML = '<div class="frow"><span class="fname">' + esc(f.name) + '</span><span class="muted small">' + size(f.size) + "</span>" +
        '<select title="File role" aria-label="File role">' + opts.map((r) => '<option value="' + r + '"' + (r === f.role ? " selected" : "") + ">" + ROLE_LABEL[r] + "</option>").join("") + "</select>" +
        '<button type="button" class="btn btn-ghost btn-sm" data-remove>Remove</button></div>' +
        (meta ? '<div class="fmeta">' + meta + "</div>" : "") +
        (f.warnings && f.warnings.length ? '<ul class="fwarn">' + f.warnings.slice(0, 6).map((w) => "<li>" + esc(w) + "</li>").join("") + "</ul>" : "");
      $("select", li).addEventListener("change", async (e) => {
        try { const nf = await api("PATCH", "/api/files/" + f.id, { role: e.target.value }); li.remove(); const nli = document.createElement("li"); $(".filelist", zoneFor(nf.role)).appendChild(nli); renderFile(nli, nf); checkDeck(); toast("Role changed to " + ROLE_LABEL[nf.role]); }
        catch (err) { toast(err.message, true); e.target.value = f.role; }
      });
      $("[data-remove]", li).addEventListener("click", async () => {
        try { await api("PATCH", "/api/files/" + f.id, { remove: true }); files.delete(f.id); li.remove(); checkDeck(); } catch (err) { toast(err.message, true); }
      });
    }
    async function handleFiles(zone, list) {
      const role = zone.dataset.role;
      try { await ensureTask(); } catch (e) { toast(e.message, true); return; }
      for (const file of list) {
        const li = document.createElement("li"); li.className = "fitem";
        li.innerHTML = '<div class="frow"><span class="fname">' + esc(file.name) + '</span><span class="muted small">' + size(file.size) + '</span><span class="muted small pct">0%</span></div><div class="bar"><i></i></div><div class="fmeta">Uploading…</div>';
        $(".filelist", zone).appendChild(li);
        const allowed = zone.dataset.accept.split(",");
        if (!allowed.includes(ext(file.name))) { li.classList.add("err"); $(".fmeta", li).textContent = "Not accepted here (" + zone.dataset.accept + ")"; continue; }
        let r = role; if (role === "client_deck" && ext(file.name) === ".zip") r = "creatives";
        try {
          const f = await upload("/api/tasks/" + taskId + "/files", file, { role: r }, (p) => { $(".bar i", li).style.width = Math.round(p * 100) + "%"; $(".pct", li).textContent = Math.round(p * 100) + "%"; if (p >= 1) $(".fmeta", li).textContent = "Inspecting…"; });
          renderFile(li, f);
        } catch (e) { li.classList.add("err"); $(".fmeta", li).textContent = e.message; $(".bar", li).remove(); }
      }
      checkDeck();
    }
    $$(".dropzone").forEach((zone) => {
      const input = $("input[type=file]", zone);
      $(".dz-pick", zone).addEventListener("click", () => input.click());
      input.addEventListener("change", () => { handleFiles(zone, Array.from(input.files)); input.value = ""; });
      ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("over"); }));
      ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("over"); }));
      zone.addEventListener("drop", (e) => handleFiles(zone, Array.from(e.dataTransfer.files)));
    });
    try { JSON.parse($("#draftFiles").textContent || "[]").forEach((f) => { const li = document.createElement("li"); $(".filelist", zoneFor(f.role)).appendChild(li); renderFile(li, f); }); } catch (e) { /* none */ }

    function checkDeck() {
      const want = $$('input[name=deliverables]:checked').map((i) => i.value);
      const hasDeck = Array.from(files.values()).some((f) => f.role === "client_deck");
      $("#deckMissing").hidden = !(want.includes("deck_update") && !hasDeck);
    }
    checkDeck();

    const del = $("#deleteDraft");
    if (del) del.addEventListener("click", async () => { if (!confirm("Delete this draft and its uploaded files?")) return; try { await api("POST", "/api/tasks/" + taskId + "/delete"); location.href = "/"; } catch (e) { toast(e.message, true); } });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = $("#formError"); err.hidden = true;
      const fail = (m) => { err.textContent = m; err.hidden = false; err.scrollIntoView({ block: "center" }); };
      const d = formData();
      if (!d.title.trim()) return fail("Give the task a title.");
      if (!d.deliverables.length) return fail("Choose at least one deliverable.");
      if (!Array.from(files.values()).some((f) => f.role === "raw")) return fail("Upload at least one raw data file in area A.");
      const btn = $("#submitBtn"); btn.disabled = true; btn.textContent = "Submitting…";
      try { clearTimeout(saveTimer); await save(); const r = await api("POST", "/api/tasks/" + taskId + "/submit"); location.href = r.url; }
      catch (ex) { fail(ex.message); btn.disabled = false; btn.textContent = "Submit for processing"; }
    });
  }

  // ------------------------------------------------------------------ workspace
  function initWorkspace() {
    const tid = body.dataset.taskId; if (!tid) return;
    let token = body.dataset.stateToken;
    let dirty = false;
    document.addEventListener("input", (e) => { if (e.target.closest("form")) dirty = true; });
    document.addEventListener("change", (e) => { if (e.target.closest("form")) dirty = true; });

    async function poll() {
      try {
        const s = await api("GET", "/api/tasks/" + tid + "/state");
        if (s.token !== token) {
          if (!dirty) { sessionStorage.setItem("tc-scroll", String(window.scrollY)); location.reload(); return; }
          toast("This task has been updated - refresh to see the latest status");
          token = s.token;
        }
        setTimeout(poll, s.processing ? 2000 : 5000);
      } catch (e) { setTimeout(poll, 8000); }
    }
    setTimeout(poll, body.dataset.processing === "1" ? 1500 : 4000);
    try { const y = sessionStorage.getItem("tc-scroll"); if (y) { sessionStorage.removeItem("tc-scroll"); window.scrollTo(0, +y); } } catch (e) { /* ignore */ }
    const m = $("#msgs"); if (m) m.scrollTop = m.scrollHeight;

    $$("[data-action]").forEach((b) => b.addEventListener("click", async () => {
      const a = b.dataset.action;
      const confirmText = { cancel: "Cancel processing of this version? Earlier versions are not affected.", delete: "Delete this task, its uploads and all outputs? This cannot be undone." }[a];
      if (confirmText && !confirm(confirmText)) return;
      b.disabled = true;
      try {
        await api("POST", "/api/tasks/" + tid + "/" + a, a === "approve" ? { version_id: +b.dataset.version } : undefined);
        if (a === "delete") { location.href = "/"; return; }
        dirty = false; location.reload();
      } catch (e) { toast(e.message, true); b.disabled = false; }
    }));

    const qf = $("#questionsForm");
    if (qf) qf.addEventListener("submit", async (e) => {
      e.preventDefault();
      const answers = {}, save = {};
      $$("input[data-qid]", qf).forEach((i) => { if ((i.type === "radio" && i.checked) || (i.type !== "radio" && i.value.trim())) answers[i.dataset.qid] = i.value.trim(); });
      $$("input[data-save]", qf).forEach((i) => { if (i.checked) save[i.dataset.save] = true; });
      const qids = new Set($$("input[data-qid]", qf).map((i) => i.dataset.qid));
      if (Object.keys(answers).length < qids.size) { toast("Please answer every question", true); return; }
      const btn = $("button", qf); btn.disabled = true; btn.textContent = "Resuming…";
      try { await api("POST", "/api/tasks/" + tid + "/answers", { answers, save }); dirty = false; location.reload(); }
      catch (ex) { toast(ex.message, true); btn.disabled = false; btn.textContent = "Submit answers and continue"; }
    });

    const ff = $("#feedbackForm");
    if (ff) ff.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = ff.text.value.trim(); if (!text) return;
      const btn = $("button", ff); btn.disabled = true; btn.textContent = "Working…";
      const ids = []; const list = $("#fbFiles"); list.innerHTML = "";
      try {
        for (const file of Array.from(ff.extra.files)) {
          const li = document.createElement("li"); li.className = "fitem"; li.innerHTML = '<div class="frow"><span class="fname">' + esc(file.name) + '</span></div><div class="bar"><i></i></div>'; list.appendChild(li);
          const f = await upload("/api/tasks/" + tid + "/files", file, { for_revision: "1" }, (p) => { $(".bar i", li).style.width = Math.round(p * 100) + "%"; });
          ids.push(f.id); $(".bar", li).outerHTML = '<div class="fmeta">' + esc(ROLE_LABEL[f.role] || f.role) + (f.sheets && f.sheets.length ? " · " + f.sheets.map((s) => s.rows + " rows").join(", ") : "") + "</div>";
        }
        const r = await api("POST", "/api/tasks/" + tid + "/feedback", { text, base_version_id: ff.base_version_id ? ff.base_version_id.value : null, file_ids: ids });
        if (!r.ok) toast(r.summary || "No change could be made", true);
        dirty = false; setTimeout(() => location.reload(), r.ok ? 300 : 2500);
      } catch (ex) { toast(ex.message, true); btn.disabled = false; btn.textContent = "Create revision"; }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    const p = body.dataset.page;
    if (p === "new_task") initNewTask();
    if (p === "workspace") initWorkspace();
  });
})();
