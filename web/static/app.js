"use strict";
// Dashboard der Agent-Box. Spricht nur mit der eigenen API (/api/...).

// Die VMs kommen aus der Statusantwort (Registry); Karten entstehen beim ersten Auftauchen
const CONTEXTS = [];
const $ = (sel, root = document) => root.querySelector(sel);
const cards = {};
let pollTimer = null;

async function api(path, body) {
  const opts = body === undefined
    ? { credentials: "same-origin" }
    : { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Agentbox": "1" },
        body: JSON.stringify(body) };
  const res = await fetch("/api/" + path, opts);
  if (res.status === 401) { showLogin(); throw new Error("nicht angemeldet"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || data.error || res.statusText);
  return data;
}

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (err ? " err" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = ""), err ? 6000 : 3000);
}

function fmtTime(epoch) {
  return new Date(epoch * 1000).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function showLogin() {
  clearInterval(pollTimer);
  $("#contexts").hidden = true;
  $("#logout").hidden = true;
  $("#login").hidden = false;
  $("#password").focus();
}

function showApp() {
  $("#login").hidden = true;
  $("#contexts").hidden = false;
  $("#logout").hidden = false;
  refresh();
  clearInterval(pollTimer);
  pollTimer = setInterval(refresh, 5000);
}

async function act(ctx, action, body = {}, confirmText) {
  if (confirmText && !confirm(confirmText)) return null;
  const card = cards[ctx];
  card.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    const r = await api(`${ctx}/${action}`, body);
    if (action !== "pair") toast(r.message || "erledigt");
    return r;
  } catch (e) {
    toast(e.message, true);
    return null;
  } finally {
    card.querySelectorAll("button").forEach((b) => (b.disabled = false));
    refresh();
  }
}

function buildCard(ctx) {
  const card = $("#ctx-card").content.firstElementChild.cloneNode(true);
  $(".ctx-name", card).textContent = ctx;
  card.querySelectorAll("button[data-act]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const a = btn.dataset.act;
      if (a === "reset") return act(ctx, a, {}, `${ctx}: Systemplatte frisch aufsetzen? Laufende Prozesse in der VM enden, Repos und Verläufe bleiben.`);
      if (a === "restart") return act(ctx, a, {}, `${ctx} neu starten?`);
      if (a === "focus") return act(ctx, a, {}, `Alle anderen VMs pausieren, damit ${ctx} allen Speicher bekommt?`);
      if (a === "open-web") {
        const min = $(".open-minutes", card).value;
        return act(ctx, a, { minutes: Number(min) }, `${ctx}: ${min} Minuten lang jede öffentliche Domain erlauben? Schadcode in der VM könnte in dieser Zeit Daten nach außen schicken.`);
      }
      if (a === "pair") {
        const r = await act(ctx, a, { confirm: true }, "Pairing-Code anzeigen? Er gibt vollen Zugriff auf diese VM.");
        if (r && r.ok) { $(".pair-out", card).hidden = false; $(".pair-out textarea", card).value = r.message; }
        return;
      }
      return act(ctx, a);
    });
  });
  $(".pair-out .copy", card).addEventListener("click", async () => {
    await navigator.clipboard.writeText($(".pair-out textarea", card).value);
    toast("kopiert");
  });
  $(".add-domain", card).addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const input = $("input", ev.target);
    if (await act(ctx, "allow", { domain: input.value })) input.value = "";
    loadLists(ctx);
  });
  $(".token-form", card).addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const input = $("input", ev.target);
    const r = await act(ctx, "claude-token", { token: input.value });
    input.value = "";
    if (r && r.ok && confirm(`Token gespeichert. ${ctx} jetzt neu starten, damit es gilt?`)) act(ctx, "restart");
  });
  $(".owners-form", card).addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const owners = $("input", ev.target).value.split(/[\s,]+/).filter(Boolean);
    if (!owners.length) return;
    if (await act(ctx, "set-owners", { owners })) $("input", ev.target).value = "";
    loadLists(ctx);
  });
  $("#contexts").append(card);
  cards[ctx] = card;
  loadLists(ctx);
}

function li(text, btnText, onClick) {
  const el = document.createElement("li");
  const span = document.createElement("span");
  span.textContent = text;
  el.append(span);
  if (btnText) {
    const b = document.createElement("button");
    b.className = "ghost";
    b.textContent = btnText;
    b.addEventListener("click", onClick);
    el.append(b);
  }
  return el;
}

// reasons of a blocked row that names the VM's own address (as web/ui/app/utils/access.ts)
const CLIENT_REASONS = ["not-tls", "no-server-name"];

async function loadLists(ctx) {
  const card = cards[ctx];
  try {
    const [blocked, allow, owners, alerts] = await Promise.all([
      api(`${ctx}/blocked`), api(`${ctx}/allowlist`), api(`${ctx}/owners`), api(`${ctx}/alerts`),
    ]);
    const bl = $(".blocked", card);
    bl.replaceChildren(...blocked.map((b) => {
      if (b.suspicious && b.suspicious.length) {
        // verdaechtige Domains nicht per Klick freigeben (nur per agentbox allow --force)
        const el = li(`${b.domain} · ${b.count}× · ${fmtTime(b.last)}`);
        el.classList.add("sus");
        const tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent = "verdächtig: " + b.suspicious.join(", ");
        el.append(tag);
        return el;
      }
      // the VM's own address (a connection without a destination): nothing to allow
      if (CLIENT_REASONS.includes(b.code)) {
        return li(`${b.domain} · ${b.count}× · ${fmtTime(b.last)} · ${b.reason}`);
      }
      return li(`${b.domain} · ${b.count}× · ${fmtTime(b.last)}`, "freigeben", async () => {
        await act(ctx, "allow", { domain: b.domain }); loadLists(ctx);
      });
    }));
    const box = $(".alerts-box", card);
    box.hidden = !alerts.length;
    $(".alerts", card).replaceChildren(...alerts.map((a) => {
      const el = li(`${a.domain} · ${a.count}× · zuletzt ${fmtTime(a.last)}`);
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = a.tags.join(", ");
      el.append(tag);
      return el;
    }));
    if (!blocked.length) bl.replaceChildren(li("nichts blockiert"));
    $(".count", card).textContent = blocked.length ? `(${blocked.length})` : "";
    const al = $(".allowlist", card);
    al.replaceChildren(...allow.map((d) =>
      li(d, "entfernen", async () => {
        if (!confirm(`${d} für ${ctx} wieder sperren?`)) return;
        await act(ctx, "disallow", { domain: d }); loadLists(ctx);
      })));
    if (!allow.length) al.replaceChildren(li("keine (nur die gemeinsame Grundliste)"));
    $(".count-allow", card).textContent = allow.length ? `(${allow.length})` : "";
    $(".owners", card).textContent = owners.length ? owners.join(", ") : "niemanden (keine Tokens)";
  } catch (e) { /* Status-Poll zeigt Fehler */ }
}

function render(status) {
  // Karten folgen der Registry: neue VMs bekommen eine, entfernte verlieren sie
  for (const ctx of Object.keys(status)) {
    if (!cards[ctx]) {
      CONTEXTS.push(ctx);
      buildCard(ctx);
    }
  }
  for (const ctx of CONTEXTS.filter((c) => !(c in status))) {
    cards[ctx].remove();
    delete cards[ctx];
    CONTEXTS.splice(CONTEXTS.indexOf(ctx), 1);
  }
  for (const ctx of CONTEXTS) {
    const s = status[ctx];
    const card = cards[ctx];
    if (!s || !card) continue;
    const running = s.state === "running";
    const badge = $(".state", card);
    const label = { running: "läuft", paused: "pausiert", "shut off": "aus" }[s.state] || s.state;
    badge.textContent = label;
    badge.className = "badge state " + (running ? "running" : s.state === "paused" ? "paused" : "off");
    $(".ram", card).textContent = running ? (s.rss_kb / 1048576).toFixed(1) + " GB" : "–";
    const agents = Object.entries(s.agents).map(([k, v]) => `${v} ${k}`).join(", ");
    $(".agents", card).textContent = !running ? "–" : s.agents_unknown ? "unbekannt" : agents || "keine";
    $(".disk", card).textContent = s.system_disk_since
      ? `seit ${fmtTime(s.system_disk_since)}` : "–";
    $(".gui", card).textContent = running ? (s.gui_connections == null ? "unbekannt" : s.gui_connections ? "verbunden" : "nicht verbunden") : "–";
    const mode = $(".web-mode", card);
    if (s.open_until) {
      const left = Math.max(0, Math.round((s.open_until * 1000 - Date.now()) / 60000));
      mode.textContent = `OFFEN, noch ${left} Min.`;
      mode.className = "web-mode open";
    } else {
      mode.textContent = "nur Allowlist";
      mode.className = "web-mode";
    }
    $(".token-state", card).textContent = s.claude_token_since
      ? `Claude-Token hinterlegt seit ${fmtTime(s.claude_token_since)}` : "Kein Claude-Token hinterlegt";
    card.querySelector('[data-act="start"]').hidden = running;
    card.querySelector('[data-act="pause"]').hidden = !running;
    card.querySelector('[data-act="close-web"]').hidden = !s.open_until;
  }
}

let ticks = 0;
async function refresh() {
  try {
    render(await api("status"));
    $("#updated").textContent = "aktualisiert " + new Date().toLocaleTimeString("de-DE");
    if (++ticks % 6 === 0) CONTEXTS.forEach(loadLists); // Listen alle 30 s
  } catch (e) {
    if (e.message !== "nicht angemeldet") $("#updated").textContent = "Fehler: " + e.message;
  }
}

$("#login").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("login", { password: $("#password").value });
    $("#password").value = "";
    showApp();
  } catch (e) {
    $("#login-error").textContent = e.message;
  }
});

$("#logout").addEventListener("click", async () => {
  await api("logout", {}).catch(() => {});
  showLogin();
});

api("status").then(() => showApp()).catch(() => showLogin());
