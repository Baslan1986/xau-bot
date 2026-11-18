// Solnerv Attend: employee app + admin dashboard (vanilla JS, RTL)
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const app = $("#app");

const LABEL = { arrive: "وصول", enter: "دخول", exit: "خروج", depart: "انطلاق" };
const ICON = { arrive: "📍", enter: "🔵", exit: "🟠", depart: "🚗" };
const NEXT = { null: ["arrive"], depart: ["arrive"], arrive: ["enter", "depart"],
               exit: ["enter", "depart"], enter: ["exit", "depart"] };
const STATE_TEXT = { null: "خارج الدوام", depart: "خارج الدوام", arrive: "بالمحطة",
                     enter: "داخل الموقع", exit: "بالمحطة (برا الموقع)" };
const STATUS = {
  present: ["حاضر", "b-ok"], in_progress: ["بالدوام", "b-info"], missing_depart: ["نسي انطلاق", "b-warn"],
  absent: ["غياب", "b-bad"], leave: ["إجازة", "b-info"], upcoming: ["قادم", ""],
  unscheduled: ["خارج الجدول", "b-warn"],
};
const LSTATUS = {
  pending_substitute: ["بانتظار البديل", "b-warn"], pending_admin: ["بانتظار الإدارة", "b-info"],
  approved: ["مقبول", "b-ok"], rejected: ["مرفوض", "b-bad"], cancelled: ["ملغي", ""],
};
const badge = ([t, c]) => `<span class="badge ${c}">${esc(t)}</span>`;

// ---------------------------------------------------------------- helpers
async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || (opts.body ? "POST" : "GET"),
    headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401 && !opts.allow401) { renderLogin(); throw new Error("401"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    throw new Error(Array.isArray(d) ? "تأكد من المعلومات المدخلة" : d || "صار خطأ، جرب مرة ثانية");
  }
  return data;
}
let toastT;
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "show" + (bad ? " bad" : "");
  clearTimeout(toastT); toastT = setTimeout(() => (t.className = ""), 3200);
  if (navigator.vibrate) navigator.vibrate(bad ? [60, 40, 60] : 30);
}
const pad = (n) => String(n).padStart(2, "0");
const localISO = (d = new Date()) =>
  `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
const today = () => localISO().slice(0, 10);
const DAYS = ["الأحد", "الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت"];
const fmtTime = (s) => (s ? s.slice(11, 16) : "—");
const fmtDay = (s) => { if (!s) return "—"; const d = new Date(s); return `${DAYS[d.getDay()]} ${d.getDate()}/${d.getMonth() + 1}`; };
const fmtDT = (s) => (s ? `${fmtDay(s)} ${fmtTime(s)}` : "—");
const hm = (min) => { min = Math.round(min || 0); const h = Math.floor(min / 60), m = min % 60;
  return h ? `${h}س${m ? ` ${m}د` : ""}` : `${m}د`; };
const kindName = (k) => (k === "D" ? "☀️ نهاري" : "🌙 ليلي");
const mapLink = (lat, lng) => `https://maps.google.com/?q=${encodeURIComponent(lat)},${encodeURIComponent(lng)}`;

function openDialog(html, onSubmit) {
  const dlg = $("#dlg"), body = $("#dlg-body");
  body.innerHTML = html;
  body.onsubmit = async (e) => {
    const btn = e.submitter;
    if (!btn || btn.value === "cancel") return;
    e.preventDefault();
    btn.disabled = true;
    try { if ((await onSubmit?.(new FormData(body), btn.value, body)) !== false) dlg.close(); }
    catch (err) { if (err.message !== "401") toast(err.message, true); }
    finally { btn.disabled = false; }
  };
  dlg.showModal();
  return body;
}
const dlgButtons = (ok = "حفظ", cls = "primary") =>
  `<div class="row" style="margin-top:14px"><button class="btn ${cls}" value="ok">${esc(ok)}</button>
   <button class="btn ghost" value="cancel" formnovalidate>إلغاء</button></div>`;

function getPos() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(new Error("جهازك ما بدعم تحديد الموقع"));
    navigator.geolocation.getCurrentPosition(
      (p) => resolve({ lat: p.coords.latitude, lng: p.coords.longitude, accuracy: p.coords.accuracy }),
      (e) => reject(new Error(e.code === 1 ? "اسمح للتطبيق يوصل لموقعك من إعدادات المتصفح"
                                          : "ما قدرنا نحدد موقعك، تأكد إن الـ GPS شغال")),
      { enableHighAccuracy: true, timeout: 20000, maximumAge: 5000 });
  });
}

function drawMap(el, polys = [], markers = []) {
  if (!window.L) { el.innerHTML = '<p class="empty">الخريطة مش متاحة بدون إنترنت</p>'; return; }
  if (el._map) el._map.remove();
  const map = L.map(el, { zoomControl: true });
  el._map = map;
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    { maxZoom: 19, attribution: "© OpenStreetMap" }).addTo(map);
  const bounds = [];
  for (const p of polys) {
    if (!p.points?.length) continue;
    L.polygon(p.points, { color: p.color || "#0f766e", weight: 2, fillOpacity: .12 })
      .addTo(map).bindTooltip(p.label || "");
    bounds.push(...p.points);
  }
  for (const m of markers) {
    L.circleMarker([m.lat, m.lng], { radius: 9, color: "#fff", weight: 2, fillColor: m.color || "#0f766e",
      fillOpacity: 1 }).addTo(map).bindTooltip(m.label || "", { permanent: !!m.permanent, direction: "top" });
    bounds.push([m.lat, m.lng]);
  }
  bounds.length ? map.fitBounds(bounds, { padding: [24, 24], maxZoom: 17 }) : map.setView([31.95, 35.9], 7);
  setTimeout(() => map.invalidateSize(), 50);
}

// ---------------------------------------------------------------- offline queue
const QKEY = "solnerv-queue";
const loadQ = () => { try { return JSON.parse(localStorage.getItem(QKEY) || "[]"); } catch { return []; } };
const saveQ = (q) => { try { localStorage.setItem(QKEY, JSON.stringify(q)); } catch {} };
async function flushQueue() {
  const q = loadQ();
  if (!q.length || !navigator.onLine) return;
  try {
    const r = await api("/api/events/sync", { body: { events: q } });
    saveQ([]);
    const bad = r.results.filter((x) => !x.ok);
    toast(bad.length ? `انرفعت الحركات، ${bad.length} منها انرفض: ${bad[0].error}`
                     : `✅ انرفعت ${q.length} حركة كانت محفوظة`, bad.length > 0);
    if (S.me) await refreshMe();
  } catch (e) { /* still offline */ }
}
addEventListener("online", () => { flushQueue(); render(); });
addEventListener("offline", () => render());

// ---------------------------------------------------------------- state
const S = { me: null, mode: "emp", tab: "home", atab: "live" };

async function refreshMe() {
  S.me = await api("/api/me");
  const q = loadQ();
  if (q.length) S.me.state = q[q.length - 1].type, S.me.allowed = NEXT[S.me.state];
}

async function boot() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  try {
    const st = await api("/api/setup", { allow401: true });
    if (st.needed) return renderSetup();
    await refreshMe();
    S.mode = S.me.user.role === "admin" ? "admin" : "emp";
    flushQueue();
    render();
  } catch (e) {
    if (e.message === "401") return;
    if (!navigator.onLine) {
      app.innerHTML = `<div class="center-screen"><div class="card narrow"><h2>ما في إنترنت</h2>
        <p>افتح التطبيق مرة وحدة وإنت متصل عشان نحفظ بياناتك، بعدين بشتغل بدون إنترنت.</p></div></div>`;
    } else renderLogin();
  }
}

// ---------------------------------------------------------------- login / setup
function renderLogin() {
  stopPing();
  app.innerHTML = `<div class="center-screen"><form class="card narrow" id="login" style="width:100%">
    <img class="logo-big" src="/static/icon.svg" alt="">
    <h1 style="text-align:center">Solnerv Attend</h1>
    <p class="muted" style="text-align:center">نظام دوام محطات الطاقة الشمسية</p>
    <div class="field"><label for="ph">رقم الهاتف</label>
      <input id="ph" name="phone" class="ltr" inputmode="tel" autocomplete="username" required></div>
    <div class="field"><label for="pw">كلمة السر</label>
      <input id="pw" name="password" type="password" autocomplete="current-password" required></div>
    <button class="btn primary" style="width:100%">دخول</button></form></div>`;
  $("#login").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/login", { body: { phone: f.get("phone"), password: f.get("password") }, allow401: true });
      boot();
    } catch (err) { toast(err.message, true); }
  };
}

function renderSetup(step = 1, data = {}) {
  const steps = `<div class="steps">${[1, 2].map((i) => `<i class="${i <= step ? "on" : ""}"></i>`).join("")}</div>`;
  if (step === 1) {
    app.innerHTML = `<div class="center-screen"><form class="card narrow" id="f" style="width:100%">
      <img class="logo-big" src="/static/icon.svg" alt="">${steps}
      <h1>أهلاً فيك 👋</h1><p class="muted">خلينا نجهز النظام بدقيقتين. أول شي، حساب المدير:</p>
      <div class="field"><label>اسمك</label><input name="admin_name" required minlength="2" value="${esc(data.admin_name)}"></div>
      <div class="field"><label>رقم هاتفك (بتدخل فيه)</label><input name="phone" class="ltr" inputmode="tel" required minlength="6" value="${esc(data.phone)}"></div>
      <div class="field"><label>كلمة السر</label><input name="password" type="password" required minlength="6" autocomplete="new-password"></div>
      <button class="btn primary" style="width:100%">التالي ←</button></form></div>`;
    $("#f").onsubmit = (e) => { e.preventDefault(); renderSetup(2, { ...data, ...Object.fromEntries(new FormData(e.target)) }); };
    return;
  }
  app.innerHTML = `<div class="center-screen"><form class="card narrow" id="f" style="width:100%">${steps}
    <h1>أول محطة ☀️</h1>
    <div class="field"><label>اسم المحطة</label><input name="station_name" required minlength="2"></div>
    <div class="field"><label>حدود المحطة</label>${POLY_HELP}
      <textarea name="polygon" required placeholder="31.9012, 35.9031&#10;31.9050, 35.9100&#10;31.8990, 35.9120"></textarea></div>
    <div id="pv" class="map sm"></div>
    <div class="row" style="margin-top:14px"><button class="btn primary">ابدأ</button>
      <button type="button" class="btn ghost" id="back">رجوع</button></div></form></div>`;
  wirePolyPreview($("#f [name=polygon]"), $("#pv"));
  $("#back").onclick = () => renderSetup(1, data);
  $("#f").onsubmit = async (e) => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target));
    try { await api("/api/setup", { body: { ...data, ...f } }); toast("🎉 جاهز! هلأ ضيف الموظفين"); S.atab = "employees"; boot(); }
    catch (err) { toast(err.message, true); }
  };
}
const POLY_HELP = `<p class="small muted">من جوجل ماب: اكبس كبسة يمين (أو كبسة طويلة على الجوال) على كل زاوية من زوايا المحطة،
  انسخ الإحداثيات اللي بتطلع، وحط كل نقطة بسطر. بتقدر كمان تلصق ملف KML من Google Earth أو My Maps.</p>`;
function parsePolyClient(text) {
  const m = text.match(/<coordinates>([\s\S]*?)<\/coordinates>/i);
  if (m) return m[1].trim().split(/\s+/).map((t) => t.split(",").map(Number)).filter((p) => p.length >= 2).map(([a, b]) => [b, a]);
  if (text.trim().startsWith("[")) { try { return JSON.parse(text); } catch { return []; } }
  return text.split("\n").map((l) => (l.match(/-?\d+(?:\.\d+)?/g) || []).map(Number)).filter((p) => p.length >= 2).map((p) => p.slice(0, 2));
}
function wirePolyPreview(ta, mapEl, extra = () => []) {
  const draw = () => drawMap(mapEl, [{ points: parsePolyClient(ta.value), label: "حدود" }, ...extra()]);
  ta.addEventListener("input", draw); draw();
}

// ---------------------------------------------------------------- shell
function render() {
  if (!S.me) return;
  if (S.mode === "admin") return renderAdmin();
  const u = S.me.user;
  const unread = S.me.unread + S.me.pending_cover;
  app.innerHTML = `
    <header class="topbar"><div class="brand"><img src="/static/icon.svg" alt=""> Solnerv</div>
      <div class="row"><span class="small">${esc(S.me.station?.name || "")}</span>
      <button class="btn sm" id="menu" aria-label="حسابي">👤</button></div></header>
    ${!navigator.onLine || loadQ().length ? `<div class="offline-bar">${!navigator.onLine ? "📴 بدون إنترنت. " : ""}${loadQ().length ? `${loadQ().length} حركة محفوظة، رح تنرفع لحالها` : "الحركات بتنحفظ وبتنرفع لما يرجع النت"}</div>` : ""}
    <main class="wrap narrow" id="main"></main>
    <button class="sos" id="sos" aria-label="طوارئ: اكبس مطوّل">SOS</button>
    <nav class="tabbar">${[["home", "🏠", "الرئيسية"], ["schedule", "📅", "جدولي"], ["requests", "📝", "طلباتي"],
      ["notes", "🔔", "إشعارات"]].map(([k, i, t]) => `<button data-tab="${k}" ${S.tab === k ? 'aria-current="page"' : ""}>
      <span class="ic">${i}</span>${t}${k === "notes" && S.me.unread ? `<span class="count">${S.me.unread}</span>` : ""}
      ${k === "requests" && S.me.pending_cover ? `<span class="count">${S.me.pending_cover}</span>` : ""}</button>`).join("")}</nav>`;
  $$(".tabbar button").forEach((b) => (b.onclick = () => { S.tab = b.dataset.tab; render(); }));
  $("#menu").onclick = profileDialog;
  wireSOS($("#sos"));
  ({ home: renderHome, schedule: renderSchedule, requests: renderRequests, notes: renderNotes })[S.tab]();
  managePing();
}

function profileDialog() {
  const u = S.me.user;
  openDialog(`<h2>${esc(u.name)}</h2><p class="muted">${esc(u.job_title || "")} ${u.team ? `· فريق ${esc(u.team)}` : ""}</p>
    <div class="grid" style="margin-block:12px">
      <div class="stat"><b>${u.annual_balance}</b><span>رصيد سنوي (شفت)</span></div>
      <div class="stat"><b>${u.sick_balance}</b><span>رصيد مرضي</span></div>
      <div class="stat"><b>${u.departure_hours_balance}</b><span>ساعات مغادرة</span></div></div>
    <h3>تغيير كلمة السر</h3>
    <div class="field"><input name="old" type="password" placeholder="الحالية" autocomplete="current-password"></div>
    <div class="field"><input name="new" type="password" placeholder="الجديدة (6 خانات أو أكثر)" minlength="6" autocomplete="new-password"></div>
    <div class="row"><button class="btn" value="pw">حفظ كلمة السر</button>
      ${u.role === "admin" ? `<button class="btn primary" value="admin">لوحة الإدارة</button>` : ""}
      <button class="btn danger" value="logout">خروج</button>
      <button class="btn ghost" value="cancel">إغلاق</button></div>`,
    async (f, v) => {
      if (v === "logout") { await api("/api/logout", { body: {} }); S.me = null; stopPing(); return renderLogin(); }
      if (v === "admin") { S.mode = "admin"; return render(); }
      if (v === "pw") { await api("/api/me/password", { body: { old: f.get("old"), new: f.get("new") } }); toast("✅ تغيرت كلمة السر"); }
    });
}

// ---------------------------------------------------------------- employee: home
function greeting() { const h = new Date().getHours(); return h < 12 ? "صباح الخير" : h < 18 ? "مساء الخير" : "مساء النور"; }
function renderHome() {
  const m = S.me, st = m.state, allowed = NEXT[st] || ["arrive"];
  const primary = st === "exit" ? "depart" : allowed[0];
  const others = allowed.filter((a) => a !== primary);
  const now = new Date();
  const cur = m.shifts.find((s) => new Date(s.start) <= now && now < new Date(s.end));
  const next = m.shifts.find((s) => new Date(s.start) > now);
  const heat = [5, 6, 7, 8].includes(now.getMonth()) && now.getHours() >= 11 && now.getHours() < 16 && st && st !== "depart";
  $("#main").innerHTML = `
    <section class="hero card">
      <div class="muted">${greeting()}، ${esc(m.user.name.split(" ")[0])} 👋</div>
      <div style="margin-top:8px"><span class="state-pill"><span class="dot"></span>${esc(STATE_TEXT[st])}
        ${m.state_since && st && st !== "depart" ? ` · من ${fmtTime(m.state_since)}` : ""}</span></div>
      <button class="big-btn a-${primary}" id="primary">${ICON[primary]}<span>${LABEL[primary]}<small>${hintFor(primary)}</small></span></button>
      <div class="secondary-actions">${others.map((a) => `<button class="btn" data-act="${a}">${ICON[a]} ${LABEL[a]}</button>`).join("")}</div>
    </section>
    ${m.pending_cover ? `<div class="card" style="border-inline-start:5px solid var(--sun)"><b>🤝 في زميل طالب منك تغطية</b>
      <p class="small muted">شوف الطلب ورد عليه</p><button class="btn primary sm" id="goreq">افتح الطلبات</button></div>` : ""}
    ${heat ? `<div class="card" style="background:var(--warn-soft)"><b>🌡️ وقت الذروة</b><p class="small" style="margin:4px 0 0">اشرب مي كل 20 دقيقة، وخليك بالظل قدر الإمكان. سلامتك أهم إشي.</p></div>` : ""}
    <div class="card shift-card">${cur ? `<div class="muted small">الشفت الحالي</div><h2>${kindName(cur.kind)}</h2>
        <div>${fmtTime(cur.start)} ← ${fmtTime(cur.end)} <span class="muted small">(${remain(cur.end)} باقي)</span></div>`
      : next ? `<div class="muted small">شفتك الجاي</div><h2>${kindName(next.kind)} · ${fmtDay(next.start)}</h2>
        <div>${fmtTime(next.start)} ← ${fmtTime(next.end)} <span class="muted small">(بعد ${remain(next.start)})</span></div>`
      : `<div class="muted">ما في شفتات قريبة بجدولك</div>`}</div>
    <h3>هالشهر</h3>
    <div class="grid">
      <div class="stat"><b>${m.month.worked_hours}</b><span>ساعة دوام</span></div>
      <div class="stat"><b>${m.month.overtime_hours}</b><span>ساعة إضافي</span></div>
      <div class="stat"><b>${hm(m.month.late_minutes)}</b><span>تأخير</span></div>
      <div class="stat"><b>${m.streak} 🔥</b><span>شفت على الوقت ورا بعض</span></div></div>
    ${m.streak >= 5 ? `<p class="streak" style="text-align:center;margin-top:12px">${m.streak >= 20 ? "🏆 أسطورة الالتزام!" : m.streak >= 10 ? "⭐ التزام نجوم!" : "👏 كمل هيك!"}</p>` : ""}
    <div class="card" style="margin-top:14px"><div class="row between"><h3 style="margin:0">آخر حركاتي</h3></div><ul class="list" id="evs"><li class="muted">...</li></ul></div>`;
  $("#primary").onclick = () => clock(primary);
  $$("[data-act]").forEach((b) => (b.onclick = () => clock(b.dataset.act)));
  $("#goreq") && ($("#goreq").onclick = () => { S.tab = "requests"; render(); });
  api("/api/me/events?days=3").then((evs) => {
    const q = loadQ().map((e) => ({ type: e.type, label: LABEL[e.type], ts: localISO(new Date(e.client_ts)), pending: true })).reverse();
    const all = [...q, ...evs].slice(0, 8);
    $("#evs").innerHTML = all.length ? all.map((e) => `<li class="row between"><span>${ICON[e.type]} ${esc(e.label)}
      ${e.pending ? '<span class="badge b-warn">محفوظة</span>' : ""}${e.flagged ? `<span class="badge b-warn" title="${esc(e.flag_reason)}">⚠️</span>` : ""}</span>
      <span class="muted small">${fmtDT(e.ts)}</span></li>`).join("") : `<li class="empty">ما في حركات</li>`;
  }).catch(() => {});
}
const hintFor = (a) => ({ arrive: "لما توصل المحطة", enter: "لما تدخل الموقع", exit: "لما تطلع من الموقع", depart: "لما تترك المحطة" }[a]);
function remain(iso) {
  const min = Math.max(0, Math.round((new Date(iso) - new Date()) / 60000));
  return min > 1440 ? `${Math.floor(min / 1440)} يوم` : hm(min);
}

async function clock(type) {
  if (type === "depart" && !confirm("متأكد بدك تسجل انطلاق وتترك المحطة؟")) return;
  const btns = $$("#primary, [data-act]"); btns.forEach((b) => (b.disabled = true));
  toast("📡 عم نحدد موقعك...");
  let pos = {};
  try { pos = await getPos(); }
  catch (e) {
    if (type === "arrive" || type === "enter") { btns.forEach((b) => (b.disabled = false)); return toast(e.message, true); }
  }
  const body = { type, ...pos };
  try {
    if (!navigator.onLine) throw new TypeError("offline");
    await api("/api/events", { body });
    toast(`✅ ${LABEL[type]} ${fmtTime(localISO())}`);
    await refreshMe();
  } catch (e) {
    if (e instanceof TypeError) { // network failure -> queue
      const q = loadQ(); q.push({ ...body, client_ts: new Date().toISOString(), offline: true }); saveQ(q);
      S.me.state = type; S.me.allowed = NEXT[type]; S.me.state_since = localISO();
      toast(`💾 ${LABEL[type]} انحفظت، رح تنرفع لما يرجع النت`);
    } else if (e.message !== "401") toast(e.message, true);
  }
  render();
}

// SOS: hold 1.5s so it can't fire by accident in a pocket
function wireSOS(btn) {
  let t;
  const start = (e) => { e.preventDefault(); btn.classList.add("holding"); t = setTimeout(fireSOS, 1500); };
  const end = () => { btn.classList.remove("holding"); clearTimeout(t); };
  btn.addEventListener("pointerdown", start);
  ["pointerup", "pointerleave", "pointercancel"].forEach((ev) => btn.addEventListener(ev, end));
  btn.addEventListener("click", () => toast("اكبس مطوّل ثانية ونص لطلب المساعدة"));
}
async function fireSOS() {
  $("#sos").classList.remove("holding");
  if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
  let pos = {};
  try { pos = await getPos(); } catch {}
  try { await api("/api/me/sos", { body: pos }); toast("🆘 انبعت طلب المساعدة للإدارة وزملائك. خليك مكانك إذا بتقدر"); }
  catch { toast("ما قدرنا نبعت! اتصل بالطوارئ 911 مباشرة", true); }
}

// live location ping while checked in only (privacy)
let pingT;
function stopPing() { clearInterval(pingT); pingT = null; }
function managePing() {
  const on = S.me && S.me.state && S.me.state !== "depart";
  if (!on) return stopPing();
  if (pingT) return;
  const send = async () => {
    if (document.hidden || !navigator.onLine) return;
    try { const p = await getPos(); const r = await api("/api/me/ping", { body: p }); if (r.warning) toast(r.warning, true); } catch {}
  };
  pingT = setInterval(send, 120000);
}

// ---------------------------------------------------------------- employee: schedule
async function renderSchedule() {
  const month = S.month || today().slice(0, 7);
  $("#main").innerHTML = `<div class="card"><h2>شفتاتي الجاية</h2><ul class="list" id="up"><li class="muted">...</li></ul></div>
    <div class="card"><div class="row between"><h2 style="margin:0">دوامي</h2><input type="month" id="mon" value="${month}" style="width:auto"></div>
    <div id="att" style="margin-top:10px"></div></div>`;
  $("#mon").onchange = (e) => { S.month = e.target.value; renderSchedule(); };
  const [up, att] = await Promise.all([api("/api/me/schedule?days=30"), api(`/api/me/attendance?month=${month}`)]);
  $("#up").innerHTML = up.length ? up.map((s) => `<li class="row between"><span><b>${fmtDay(s.start)}</b> · ${kindName(s.kind)}
    ${s.source === "substitute" ? '<span class="badge b-info">تغطية</span>' : ""}</span>
    <span class="muted">${fmtTime(s.start)} ← ${fmtTime(s.end)}</span></li>`).join("") : `<li class="empty">ما في شفتات</li>`;
  const sm = att.summary;
  $("#att").innerHTML = `<div class="grid">
      <div class="stat"><b>${sm.worked_hours}</b><span>ساعة فعلية من ${sm.scheduled_hours}</span></div>
      <div class="stat"><b>${sm.site_hours}</b><span>ساعة بالموقع</span></div>
      <div class="stat"><b>${sm.overtime_hours}</b><span>إضافي</span></div>
      <div class="stat"><b>${sm.absences}</b><span>غياب</span></div></div>
    <ul class="list" style="margin-top:10px">${att.days.map((d) => `<li><div class="row between">
      <b>${fmtDay(d.shift?.start || d.arrive)} ${d.shift ? `· ${kindName(d.shift.kind)}` : ""}</b>${badge(STATUS[d.status])}</div>
      <div class="small muted">${d.arrive ? `وصول ${fmtTime(d.arrive)} · انطلاق ${fmtTime(d.depart)} · ${hm(d.worked_min)}` : ""}
      ${d.late_min ? ` · <span style="color:var(--bad)">تأخير ${hm(d.late_min)}</span>` : ""}
      ${d.overtime_min ? ` · <span style="color:var(--ok)">إضافي ${hm(d.overtime_min)}</span>` : ""}
      ${d.notes.map(esc).join(" · ")}</div></li>`).join("") || `<li class="empty">ما في شي لهالشهر</li>`}</ul>`;
}

// ---------------------------------------------------------------- employee: requests
async function renderRequests() {
  $("#main").innerHTML = `<button class="btn primary" id="new" style="width:100%;margin-bottom:14px">➕ طلب إجازة أو مغادرة</button>
    <div id="lists"><p class="muted">...</p></div>`;
  $("#new").onclick = leaveDialog;
  const { mine, cover } = await api("/api/leaves/mine");
  const pendingCover = cover.filter((x) => x.status === "pending_substitute");
  $("#lists").innerHTML = `
    ${pendingCover.length ? `<div class="card" style="border-inline-start:5px solid var(--sun)"><h2>🤝 طلبات تغطية إلك</h2><ul class="list">
      ${pendingCover.map((r) => `<li><b>${esc(r.user.name)}</b> بدّه ${esc(r.kind_label)}
        <div class="small muted">${fmtDT(r.start)} ← ${fmtDT(r.end)}${r.reason ? ` · ${esc(r.reason)}` : ""}</div>
        <div class="row" style="margin-top:8px"><button class="btn primary sm" data-acc="${r.id}">✅ بغطيه</button>
        <button class="btn sm" data-dec="${r.id}">ما بقدر</button></div></li>`).join("")}</ul></div>` : ""}
    <div class="card"><h2>طلباتي</h2><ul class="list">${mine.map((r) => `<li><div class="row between">
      <b>${esc(r.kind_label)} · ${r.kind === "departure" ? `${r.amount} ساعة` : `${r.amount} شفت`}</b>${badge(LSTATUS[r.status])}</div>
      <div class="small muted">${fmtDT(r.start)} ← ${fmtDT(r.end)}${r.substitute ? ` · البديل: ${esc(r.substitute.name)}` : ""}
      ${r.admin_note ? ` · ${esc(r.admin_note)}` : ""}</div>
      ${["pending_substitute", "pending_admin"].includes(r.status) ? `<button class="btn ghost sm" data-cancel="${r.id}" style="margin-top:6px">إلغاء الطلب</button>` : ""}
      </li>`).join("") || `<li class="empty">ما قدمت طلبات لسا</li>`}</ul></div>
    ${cover.filter((x) => x.status !== "pending_substitute").length ? `<div class="card"><h3>تغطيات سابقة</h3><ul class="list">
      ${cover.filter((x) => x.status !== "pending_substitute").map((r) => `<li class="row between"><span>${esc(r.user.name)} · ${fmtDay(r.start)}</span>${badge(LSTATUS[r.status])}</li>`).join("")}</ul></div>` : ""}`;
  const act = async (url, body, msg) => { try { await api(url, { body }); toast(msg); await refreshMe(); render(); } catch (e) { toast(e.message, true); } };
  $$("[data-acc]").forEach((b) => (b.onclick = () => act(`/api/leaves/${b.dataset.acc}/respond`, { accept: true }, "✅ شكراً! الطلب راح للإدارة")));
  $$("[data-dec]").forEach((b) => (b.onclick = () => act(`/api/leaves/${b.dataset.dec}/respond`, { accept: false }, "تم إبلاغ زميلك")));
  $$("[data-cancel]").forEach((b) => (b.onclick = () => confirm("تلغي الطلب؟") && act(`/api/leaves/${b.dataset.cancel}/cancel`, {}, "انلغى الطلب")));
}

function leaveDialog() {
  let kind = "annual";
  const body = openDialog(`<h2>طلب جديد</h2>
    <div class="row" style="margin-bottom:12px">${[["annual", "🏖️ سنوية"], ["sick", "🤒 مرضية"], ["departure", "⏱️ مغادرة"]]
      .map(([k, t]) => `<button type="button" class="chip" data-k="${k}" aria-pressed="${k === kind}">${t}</button>`).join("")}</div>
    <div id="dates"></div>
    <div class="field"><label>مين بغطيك؟ <span class="muted small">(إنترلوك: لازم زميل يوافق)</span></label>
      <div id="cands" class="small muted">حدد الوقت أول</div></div>
    <div class="field"><label>السبب (اختياري)</label><input name="reason" maxlength="300"></div>
    <p class="small muted" id="bal"></p>${dlgButtons("أرسل الطلب")}`,
    async (f) => {
      const { start, end } = range(f);
      const sub = f.get("sub");
      await api("/api/leaves", { body: { kind, start, end, substitute_id: sub ? Number(sub) : null, reason: f.get("reason") || null } });
      toast(sub ? "📨 انبعت لزميلك يوافق، بعدين للإدارة" : "📨 انبعت الطلب للإدارة");
      S.tab = "requests"; await refreshMe(); render();
    });
  const range = (f) => kind === "departure"
    ? { start: `${f.get("d")}T${f.get("t1")}`, end: `${f.get("d")}T${f.get("t2")}` }
    : { start: `${f.get("d1")}T00:00`, end: nextDay(f.get("d2")) + "T00:00" };
  const drawDates = () => {
    const u = S.me.user;
    $("#dates", body).innerHTML = kind === "departure"
      ? `<div class="grid"><div class="field"><label>اليوم</label><input type="date" name="d" value="${today()}" required></div>
         <div class="field"><label>من</label><input type="time" name="t1" required></div>
         <div class="field"><label>لـ</label><input type="time" name="t2" required></div></div>`
      : `<div class="grid"><div class="field"><label>من يوم</label><input type="date" name="d1" value="${today()}" required></div>
         <div class="field"><label>لـ يوم (ضمناً)</label><input type="date" name="d2" value="${today()}" required></div></div>`;
    $("#bal", body).textContent = kind === "annual" ? `رصيدك: ${u.annual_balance} شفت` : kind === "sick" ? `رصيدك: ${u.sick_balance} شفت. البديل اختياري، الإدارة بتعيّن إذا ما اخترت`
      : `رصيد المغادرات: ${u.departure_hours_balance} ساعة. البديل لازم يكون موجود بالمحطة هلأ`;
    $$("input", $("#dates", body)).forEach((i) => (i.onchange = loadCands));
    loadCands();
  };
  const loadCands = async () => {
    const f = new FormData(body); const { start, end } = range(f);
    if (start.includes("null") || start.endsWith("T") || end.endsWith("T")) return;
    $("#cands", body).textContent = "...";
    try {
      const c = await api(`/api/leaves/candidates?${new URLSearchParams({ kind, start, end })}`);
      $("#cands", body).innerHTML = (kind === "sick" ? `<label class="row" style="font-weight:400"><input type="radio" name="sub" value="" checked style="width:auto;min-height:0"> بدون (الإدارة بتعيّن)</label>` : "")
        + (c.map((p) => `<label class="row" style="font-weight:400;opacity:${p.ok ? 1 : .55}"><input type="radio" name="sub" value="${p.id}" ${p.ok ? "" : "disabled"} style="width:auto;min-height:0">
        ${esc(p.name)} ${p.ok ? "✅" : `<span class="small muted">· ${esc(p.reason)}</span>`}</label>`).join("") || "ما في زملاء بالمحطة");
    } catch (e) { $("#cands", body).textContent = e.message; }
  };
  $$(".chip", body).forEach((c) => (c.onclick = () => { kind = c.dataset.k; $$(".chip", body).forEach((x) => x.setAttribute("aria-pressed", x === c)); drawDates(); }));
  drawDates();
}
const nextDay = (d) => { const x = new Date(d + "T12:00"); x.setDate(x.getDate() + 1); return localISO(x).slice(0, 10); };

async function renderNotes() {
  const ns = await api("/api/me/notifications");
  S.me.unread = 0;
  $$(".tabbar [data-tab=notes] .count").forEach((x) => x.remove());
  $("#main").innerHTML = `<div class="card"><h2>الإشعارات</h2><ul class="list">${ns.map((n) => `<li>
    <div ${n.read ? "" : 'style="font-weight:700"'}>${linkify(n.text)}</div><div class="small muted">${fmtDT(n.ts)}</div></li>`).join("")
    || `<li class="empty">ما في إشعارات 🎉</li>`}</ul></div>`;
}
// only Google Maps links we generated become clickable
const linkify = (t) => esc(t).replace(/https:\/\/maps\.google\.com\/\?q=[-\d.]+,[-\d.]+/g, (u) => `<a href="${u}" target="_blank" rel="noopener">📍 افتح الموقع</a>`);

// ---------------------------------------------------------------- admin
const ATABS = [["live", "📡 مباشر"], ["requests", "📝 الطلبات"], ["employees", "👷 الموظفين"],
               ["schedule", "📅 الجدول"], ["stations", "☀️ المحطات"], ["reports", "📊 التقارير"]];
let liveT;
function renderAdmin() {
  stopPing(); clearInterval(liveT);
  app.innerHTML = `<header class="topbar"><div class="brand"><img src="/static/icon.svg" alt=""> Solnerv · الإدارة</div>
    <div class="row"><button class="btn sm" id="toemp">📱 تطبيقي</button><button class="btn sm" id="out">خروج</button></div></header>
    <nav class="admin-nav">${ATABS.map(([k, t]) => `<button data-a="${k}" ${S.atab === k ? 'aria-current="page"' : ""}>${t}</button>`).join("")}</nav>
    <main class="wrap" id="main"><p class="muted">...</p></main>`;
  $("#toemp").onclick = () => { S.mode = "emp"; S.tab = "home"; render(); };
  $("#out").onclick = async () => { await api("/api/logout", { body: {} }); S.me = null; renderLogin(); };
  $$(".admin-nav button").forEach((b) => (b.onclick = () => { S.atab = b.dataset.a; renderAdmin(); }));
  const fn = { live: aLive, requests: aRequests, employees: aEmployees, schedule: aSchedule, stations: aStations, reports: aReports }[S.atab];
  fn().catch((e) => e.message !== "401" && toast(e.message, true));
}

async function aLive() {
  const d = await api("/api/admin/live");
  const present = d.people.filter((p) => p.state && p.state !== "depart");
  const onShift = d.people.filter((p) => p.on_shift);
  const missing = d.people.filter((p) => p.missing);
  const colors = { arrive: "#16a34a", enter: "#2563eb", exit: "#ea580c" };
  $("#main").innerHTML = `
    ${d.alerts.map((a) => `<div class="card alert-card"><div class="row between"><b>${a.kind === "sos" ? "🆘 طوارئ" : "⚠️ طلع من الحدود"} · ${esc(a.name)}</b>
      <span class="small">${fmtDT(a.ts)}</span></div>${a.note ? `<div class="small">${esc(a.note)}</div>` : ""}
      <div class="row" style="margin-top:8px">${a.lat != null ? `<a class="btn sm" href="${mapLink(a.lat, a.lng)}" target="_blank" rel="noopener">📍 الموقع</a>` : ""}
      <button class="btn sm primary" data-res="${a.id}">تم التعامل</button></div></div>`).join("")}
    <div class="grid" style="margin-bottom:14px">
      <div class="stat"><b>${present.length}</b><span>موجودين هلأ</span></div>
      <div class="stat"><b>${onShift.length}</b><span>مناوبين حسب الجدول</span></div>
      <div class="stat"><b style="color:${missing.length ? "var(--bad)" : "inherit"}">${missing.length}</b><span>مناوب وما وصل</span></div>
      <div class="stat"><b>${d.pending_leaves}</b><span>طلبات بانتظارك</span></div></div>
    <div class="grid2"><div class="card"><h2>الخريطة</h2><div class="map" id="map"></div></div>
      <div class="card"><h2>الموظفين</h2><div class="table-wrap"><table><thead><tr><th>الاسم</th><th>الحالة</th><th>من</th><th>الشفت</th></tr></thead><tbody>
      ${d.people.filter((p) => p.station_id).map((p) => `<tr><td>${esc(p.name)}${p.team ? ` <span class="muted small">${esc(p.team)}</span>` : ""}</td>
        <td>${p.missing ? badge(["ما وصل", "b-bad"]) : badge([STATE_TEXT[p.state], p.state && p.state !== "depart" ? "b-ok" : ""])}</td>
        <td class="small">${p.state && p.state !== "depart" ? fmtTime(p.since) : ""}</td>
        <td class="small">${p.shift ? `${kindName(p.shift.kind)} ${fmtTime(p.shift.start)}-${fmtTime(p.shift.end)}` : "—"}</td></tr>`).join("")}
      </tbody></table></div></div></div>
    ${d.flagged.length ? `<div class="card"><h2>⚠️ حركات بحاجة مراجعة (آخر يومين)</h2><ul class="list">${d.flagged.map((e) =>
      `<li class="row between"><span><b>${esc(e.name)}</b> · ${esc(e.label)} · <span class="small">${esc(e.flag_reason)}</span></span><span class="small muted">${fmtDT(e.ts)}</span></li>`).join("")}</ul></div>` : ""}
    <p class="small muted">بتتحدث تلقائياً كل 30 ثانية</p>`;
  drawMap($("#map"), d.stations.flatMap((s) => [{ points: s.polygon, label: s.name }, ...(s.site_polygon ? [{ points: s.site_polygon, color: "#2563eb", label: `موقع ${s.name}` }] : [])]),
    present.filter((p) => p.lat != null).map((p) => ({ lat: p.lat, lng: p.lng, label: p.name, color: colors[p.state], permanent: true })));
  $$("[data-res]").forEach((b) => (b.onclick = async () => { await api(`/api/admin/alerts/${b.dataset.res}/resolve`, { body: {} }); aLive(); }));
  clearInterval(liveT);
  liveT = setInterval(() => S.atab === "live" && S.mode === "admin" && !$("#dlg").open && aLive().catch(() => {}), 30000);
}

async function aRequests() {
  const status = S.lfilter ?? "pending_admin";
  const [list, emps] = await Promise.all([api(`/api/admin/leaves${status ? `?status=${status}` : ""}`), api("/api/admin/employees")]);
  $("#main").innerHTML = `<div class="row" style="margin-bottom:12px">${[["pending_admin", "بانتظاري"], ["pending_substitute", "بانتظار البديل"], ["approved", "مقبولة"], ["rejected", "مرفوضة"], ["", "الكل"]]
      .map(([k, t]) => `<button class="chip" data-f="${k}" aria-pressed="${status === k}">${t}</button>`).join("")}</div>
    <div class="card"><ul class="list">${list.map((r) => `<li><div class="row between"><b>${esc(r.user.name)} · ${esc(r.kind_label)} (${r.amount} ${r.kind === "departure" ? "ساعة" : "شفت"})</b>${badge(LSTATUS[r.status])}</div>
      <div class="small muted">${fmtDT(r.start)} ← ${fmtDT(r.end)} · البديل: ${r.substitute ? `${esc(r.substitute.name)} ${r.substitute_status === "accepted" ? "✅" : "⏳"}` : "—"}
      ${r.reason ? ` · "${esc(r.reason)}"` : ""}${r.admin_note ? ` · ملاحظة: ${esc(r.admin_note)}` : ""}</div>
      ${r.status === "pending_admin" ? `<div class="row" style="margin-top:8px">
        ${!r.substitute ? `<select data-sub="${r.id}" style="width:auto"><option value="">بدون بديل (حذف الشفتات)</option>${emps.filter((e) => e.active && e.id !== r.user.id).map((e) => `<option value="${e.id}">${esc(e.name)}</option>`).join("")}</select>` : ""}
        <button class="btn primary sm" data-ok="${r.id}">✅ موافقة</button><button class="btn sm" data-no="${r.id}">رفض</button></div>` : ""}</li>`).join("")
      || `<li class="empty">ما في طلبات هون 👌</li>`}</ul></div>`;
  $$("[data-f]").forEach((b) => (b.onclick = () => { S.lfilter = b.dataset.f; aRequests(); }));
  $$("[data-ok]").forEach((b) => (b.onclick = async () => {
    const sel = $(`[data-sub="${b.dataset.ok}"]`);
    try { await api(`/api/admin/leaves/${b.dataset.ok}/decide`, { body: { approve: true, substitute_id: sel?.value ? Number(sel.value) : null } }); toast("✅ تمت الموافقة واتحدث الجدول"); aRequests(); }
    catch (e) { toast(e.message, true); }
  }));
  $$("[data-no]").forEach((b) => (b.onclick = () => openDialog(`<h2>سبب الرفض</h2><div class="field"><input name="note" maxlength="300" placeholder="اختياري"></div>${dlgButtons("رفض", "danger")}`,
    async (f) => { await api(`/api/admin/leaves/${b.dataset.no}/decide`, { body: { approve: false, note: f.get("note") || null } }); toast("انرفض الطلب"); aRequests(); })));
}

async function aEmployees() {
  const [emps, teams, stations] = await Promise.all([api("/api/admin/employees"), api("/api/admin/teams"), api("/api/admin/stations")]);
  S.cache = { teams, stations };
  $("#main").innerHTML = `<div class="row" style="margin-bottom:12px"><button class="btn primary" id="add">➕ موظف</button>
    <button class="btn" id="bulk">📋 إضافة مجموعة</button>${!teams.length ? `<span class="small muted">نصيحة: اعمل الفرق من صفحة الجدول أول، عشان تربط الموظفين فيها</span>` : ""}</div>
    <div class="card table-wrap"><table><thead><tr><th>الاسم</th><th>الهاتف</th><th>الفريق</th><th>سنوي</th><th>مرضي</th><th>مغادرات</th><th></th></tr></thead><tbody>
    ${emps.map((u) => `<tr style="opacity:${u.active ? 1 : .5}"><td>${esc(u.name)} ${u.role === "admin" ? badge(["أدمن", "b-info"]) : ""}<div class="small muted">${esc(u.job_title || "")}</div></td>
      <td class="ltr">${esc(u.phone)}</td><td>${esc(u.team || "—")}</td><td>${u.annual_balance}</td><td>${u.sick_balance}</td><td>${u.departure_hours_balance}</td>
      <td><button class="btn sm" data-ed="${u.id}">تعديل</button></td></tr>`).join("") || `<tr><td colspan="7" class="empty">ما في موظفين</td></tr>`}</tbody></table></div>`;
  $("#add").onclick = () => empDialog();
  $$("[data-ed]").forEach((b) => (b.onclick = () => empDialog(emps.find((u) => u.id == b.dataset.ed))));
  $("#bulk").onclick = () => openDialog(`<h2>إضافة مجموعة موظفين</h2>
    <p class="small muted">كل موظف بسطر: <b>الاسم، رقم الهاتف، الفريق، الوظيفة</b> (الفريق والوظيفة اختياريين). بتقدر تنسخهم من Excel.</p>
    <div class="field"><textarea name="text" required placeholder="أحمد محمد، 0791234567، A، فني كهرباء&#10;سامي علي، 0797654321، B"></textarea></div>
    <div class="field"><label>المحطة (للي ما إلهم فريق)</label><select name="station_id">${stations.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("")}</select></div>
    <div class="field"><label>كلمة سر أولية للكل</label><input name="default_password" minlength="6" required value="${Math.random().toString().slice(2, 8)}"></div>
    <p class="small muted">بلّغ الموظفين يغيروها أول ما يدخلوا.</p>${dlgButtons("أضف")}`,
    async (f) => {
      const r = await api("/api/admin/employees/bulk", { body: { text: f.get("text"), default_password: f.get("default_password"), station_id: Number(f.get("station_id")) } });
      toast(`✅ انضاف ${r.created.length} موظف${r.errors.length ? ` · ${r.errors.length} ملاحظة` : ""}`, r.errors.length > 0);
      if (r.errors.length) alert(r.errors.join("\n"));
      aEmployees();
    });
}
function empDialog(u = {}) {
  const { teams, stations } = S.cache;
  const isNew = !u.id;
  openDialog(`<h2>${isNew ? "موظف جديد" : esc(u.name)}</h2>
    <div class="grid"><div class="field"><label>الاسم</label><input name="name" required minlength="2" value="${esc(u.name)}"></div>
    <div class="field"><label>رقم الهاتف</label><input name="phone" class="ltr" required minlength="6" value="${esc(u.phone)}"></div>
    <div class="field"><label>الوظيفة</label><input name="job_title" value="${esc(u.job_title)}" placeholder="فني، مشرف، حارس..."></div>
    <div class="field"><label>${isNew ? "كلمة سر أولية" : "كلمة سر جديدة (اختياري)"}</label><input name="password" ${isNew ? "required" : ""} minlength="6" autocomplete="new-password"></div>
    <div class="field"><label>الفريق</label><select name="team_id"><option value="">—</option>${teams.map((t) => `<option value="${t.id}" ${u.team_id === t.id ? "selected" : ""}>${esc(t.name)}</option>`).join("")}</select></div>
    <div class="field"><label>المحطة</label><select name="station_id">${stations.map((s) => `<option value="${s.id}" ${u.station_id === s.id ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></div>
    <div class="field"><label>رصيد سنوي (شفت)</label><input name="annual_balance" type="number" step="0.5" min="0" value="${u.annual_balance ?? 14}"></div>
    <div class="field"><label>رصيد مرضي (شفت)</label><input name="sick_balance" type="number" step="0.5" min="0" value="${u.sick_balance ?? 14}"></div>
    <div class="field"><label>ساعات مغادرة</label><input name="departure_hours_balance" type="number" step="0.5" min="0" value="${u.departure_hours_balance ?? 8}"></div>
    <div class="field"><label>الصلاحية</label><select name="role"><option value="employee">موظف</option><option value="admin" ${u.role === "admin" ? "selected" : ""}>أدمن (بوافق على الطلبات)</option></select></div></div>
    <label class="row" style="font-weight:400"><input type="checkbox" name="active" ${u.active !== false ? "checked" : ""} style="width:auto;min-height:0"> فعّال</label>
    ${dlgButtons()}`,
    async (f) => {
      const body = { name: f.get("name"), phone: f.get("phone"), job_title: f.get("job_title") || null, password: f.get("password") || null,
        team_id: f.get("team_id") ? Number(f.get("team_id")) : null, station_id: Number(f.get("station_id")) || null, role: f.get("role"),
        active: f.get("active") === "on", annual_balance: Number(f.get("annual_balance")), sick_balance: Number(f.get("sick_balance")),
        departure_hours_balance: Number(f.get("departure_hours_balance")) };
      await api(isNew ? "/api/admin/employees" : `/api/admin/employees/${u.id}`, { method: isNew ? "POST" : "PUT", body });
      toast("✅ انحفظ"); aEmployees();
    });
}

const PRESETS = [["D,D,D,D,D,D,D,N,N,N,N,N,N,N", "أسبوع نهار · أسبوع ليل (فريقين 24/7)"],
                 ["D,D,N,N,O,O,O,O", "2 نهار · 2 ليل · 4 راحة"], ["D,D,D,D,O,O,O,O", "4 نهار · 4 راحة"],
                 ["N,N,N,N,O,O,O,O", "4 ليل · 4 راحة"], ["D,O", "يوم ويوم"], ["D,D,D,D,D,D,O", "6 أيام · جمعة راحة"]];
async function aSchedule() {
  const [teams, stations, emps] = await Promise.all([api("/api/admin/teams"), api("/api/admin/stations"), api("/api/admin/employees")]);
  const sid = S.sid || stations[0]?.id, start = S.sstart || today();
  const sch = sid ? await api(`/api/admin/schedule?station_id=${sid}&start=${start}&days=14`) : { shifts: [], gaps: [] };
  const days = [...Array(14)].map((_, i) => { const d = new Date(start + "T12:00"); d.setDate(d.getDate() + i); return localISO(d).slice(0, 10); });
  const people = emps.filter((e) => e.station_id == sid && e.active);
  const cell = (uid, day) => sch.shifts.find((s) => s.user_id === uid && s.start.slice(0, 10) === day);
  $("#main").innerHTML = `<div class="card"><div class="row between"><h2 style="margin:0">الفرق</h2><button class="btn primary sm" id="addt">➕ فريق</button></div>
    <ul class="list">${teams.map((t) => `<li class="row between"><span><b>فريق ${esc(t.name)}</b> · <span class="ltr small">${esc(t.pattern)}</span>
      <span class="small muted">· يبدأ ${esc(t.anchor_date)} · ${people.filter((p) => p.team_id === t.id).length} موظف</span></span>
      <span class="row"><button class="btn sm" data-gen="${t.id}">⚙️ ولّد الجدول</button><button class="btn sm ghost" data-et="${t.id}">تعديل</button></span></li>`).join("")
      || `<li class="empty">ما في فرق. اعمل فريق A وفريق B، وحدد نمط كل واحد</li>`}</ul></div>
    <div class="card"><div class="row between"><h2 style="margin:0">جدول 14 يوم</h2>
      <div class="row"><select id="sid" style="width:auto">${stations.map((s) => `<option value="${s.id}" ${s.id == sid ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select>
      <input type="date" id="sst" value="${start}" style="width:auto"></div></div>
      <p class="small muted">☀️ نهار · 🌙 ليل · الإطار المتقطع = تغطية بدل زميل · اكبس على أي خانة لتعدّلها</p>
      <div class="table-wrap"><table class="sched"><thead><tr><th class="name"></th>${days.map((d) => `<th class="small">${fmtDay(d).replace(" ", "<br>")}</th>`).join("")}</tr></thead><tbody>
      ${people.map((p) => `<tr><th class="name">${esc(p.name)}</th>${days.map((d) => { const s = cell(p.id, d);
        return `<td class="${s ? s.kind : ""} ${s?.source === "substitute" ? "sub" : ""}" data-u="${p.id}" data-d="${d}" ${s ? `data-s="${s.id}"` : ""}
          title="${s?.covering_for_name ? `بدل ${esc(s.covering_for_name)}` : ""}" style="cursor:pointer">${s ? (s.kind === "D" ? "☀️" : "🌙") : "·"}</td>`; }).join("")}</tr>`).join("")}
      <tr><th class="name small">نقص بالتغطية</th>${days.map((d) => { const g = sch.gaps.filter((x) => x.start.slice(0, 10) === d);
        return `<td class="${g.length ? "gap" : ""} small">${g.map((x) => (x.kind === "D" ? "☀️" : "🌙")).join("")}</td>`; }).join("")}</tr>
      </tbody></table></div></div>`;
  $("#sid") && ($("#sid").onchange = (e) => { S.sid = Number(e.target.value); aSchedule(); });
  $("#sst") && ($("#sst").onchange = (e) => { S.sstart = e.target.value; aSchedule(); });
  $("#addt").onclick = () => teamDialog(stations);
  $$("[data-et]").forEach((b) => (b.onclick = () => teamDialog(stations, teams.find((t) => t.id == b.dataset.et))));
  $$("[data-gen]").forEach((b) => (b.onclick = () => openDialog(`<h2>توليد الجدول</h2>
    <p class="small muted">بنعمل شفتات لكل موظفين الفريق حسب النمط. الشفتات اليدوية والتغطيات ما بتتغير.</p>
    <div class="grid"><div class="field"><label>من</label><input type="date" name="start" value="${today()}" required></div>
    <div class="field"><label>كم يوم</label><input type="number" name="days" value="30" min="1" max="120" required></div></div>${dlgButtons("ولّد")}`,
    async (f) => { const r = await api("/api/admin/schedule/generate", { body: { team_id: Number(b.dataset.gen), start: f.get("start"), days: Number(f.get("days")) } }); toast(`✅ انعمل ${r.created} شفت`); aSchedule(); })));
  $$("td[data-u]").forEach((td) => (td.onclick = () => {
    const name = people.find((p) => p.id == td.dataset.u)?.name;
    if (td.dataset.s) {
      openDialog(`<h2>${esc(name)} · ${fmtDay(td.dataset.d)}</h2><p>${td.title ? esc(td.title) : "شفت مجدول"}</p>${dlgButtons("احذف الشفت", "danger")}`,
        async () => { await api(`/api/admin/shifts/${td.dataset.s}`, { method: "DELETE" }); toast("انحذف"); aSchedule(); });
    } else {
      openDialog(`<h2>إضافة شفت لـ ${esc(name)}</h2><p class="muted">${fmtDay(td.dataset.d)}</p>
        <div class="row"><button class="btn primary" value="D">☀️ نهاري</button><button class="btn primary" value="N">🌙 ليلي</button><button class="btn ghost" value="cancel">إلغاء</button></div>`,
        async (_, v) => { await api("/api/admin/shifts", { body: { user_id: Number(td.dataset.u), kind: v, day: td.dataset.d } }); toast("✅ انضاف"); aSchedule(); });
    }
  }));
}
function teamDialog(stations, t = {}) {
  const body = openDialog(`<h2>${t.id ? `فريق ${esc(t.name)}` : "فريق جديد"}</h2>
    <div class="grid"><div class="field"><label>الاسم</label><input name="name" required value="${esc(t.name || "")}" placeholder="A"></div>
    <div class="field"><label>المحطة</label><select name="station_id">${stations.map((s) => `<option value="${s.id}" ${t.station_id === s.id ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></div></div>
    <div class="field"><label>نمط الدوام</label><div class="row" style="margin-bottom:8px">${PRESETS.map(([p, n]) => `<button type="button" class="chip" data-p="${p}">${n}</button>`).join("")}</div>
      <input name="pattern" class="ltr" required value="${esc(t.pattern || "D,D,N,N,O,O,O,O")}">
      <p class="small muted">D = نهار، N = ليل، O = راحة. النمط بيتكرر. للفريق الثاني نفس النمط بس مزاح، مثلاً O,O,O,O,D,D,N,N</p></div>
    <div class="field"><label>أول يوم بالنمط</label><input type="date" name="anchor_date" required value="${esc(t.anchor_date || today())}"></div>${dlgButtons()}`,
    async (f) => {
      const b = { name: f.get("name"), station_id: Number(f.get("station_id")), pattern: f.get("pattern"), anchor_date: f.get("anchor_date") };
      await api(t.id ? `/api/admin/teams/${t.id}` : "/api/admin/teams", { method: t.id ? "PUT" : "POST", body: b });
      toast("✅ انحفظ الفريق، هلأ ولّد الجدول"); aSchedule();
    });
  $$("[data-p]", body).forEach((c) => (c.onclick = () => ($("[name=pattern]", body).value = c.dataset.p)));
}

async function aStations() {
  const stations = await api("/api/admin/stations");
  $("#main").innerHTML = `<div class="row" style="margin-bottom:12px"><button class="btn primary" id="adds">➕ محطة</button></div>
    <div class="grid2">${stations.map((s) => `<div class="card"><div class="row between"><h2 style="margin:0">${esc(s.name)}</h2><button class="btn sm" data-es="${s.id}">تعديل</button></div>
      <p class="small muted">الشفت ${s.shift_hours} ساعة · نهار ${esc(s.day_start)} · ليل ${esc(s.night_start)} · سماح ${s.grace_minutes} د · أقل عدد ${s.min_staff}</p>
      <div class="map sm" data-map="${s.id}"></div></div>`).join("")}</div>`;
  stations.forEach((s) => drawMap($(`[data-map="${s.id}"]`), [{ points: s.polygon, label: "المحطة" }, ...(s.site_polygon ? [{ points: s.site_polygon, color: "#2563eb", label: "موقع العمل" }] : [])]));
  $("#adds").onclick = () => stationDialog();
  $$("[data-es]").forEach((b) => (b.onclick = () => stationDialog(stations.find((s) => s.id == b.dataset.es))));
}
function stationDialog(s = {}) {
  const lines = (p) => (p || []).map((x) => x.join(", ")).join("\n");
  const body = openDialog(`<h2>${s.id ? esc(s.name) : "محطة جديدة"}</h2>
    <div class="field"><label>الاسم</label><input name="name" required minlength="2" value="${esc(s.name || "")}"></div>
    <div class="field"><label>حدود المحطة (وصول / انطلاق)</label>${POLY_HELP}<textarea name="polygon" required>${esc(lines(s.polygon))}</textarea></div>
    <div class="field"><label>حدود موقع العمل (دخول / خروج) <span class="muted small">اختياري، إذا فاضي بنستعمل حدود المحطة</span></label>
      <textarea name="site_polygon">${esc(lines(s.site_polygon))}</textarea></div>
    <div id="pv" class="map sm" style="margin-bottom:12px"></div>
    <div class="grid"><div class="field"><label>بداية النهار</label><input type="time" name="day_start" value="${esc(s.day_start || "07:00")}"></div>
    <div class="field"><label>بداية الليل</label><input type="time" name="night_start" value="${esc(s.night_start || "19:00")}"></div>
    <div class="field"><label>طول الشفت (ساعة)</label><input type="number" name="shift_hours" min="1" max="24" value="${s.shift_hours || 12}"></div>
    <div class="field"><label>سماح التأخير (د)</label><input type="number" name="grace_minutes" min="0" max="120" value="${s.grace_minutes ?? 10}"></div>
    <div class="field"><label>أقل عدد بالشفت</label><input type="number" name="min_staff" min="0" max="50" value="${s.min_staff ?? 1}"></div></div>${dlgButtons()}`,
    async (f) => {
      const b = Object.fromEntries(f);
      ["shift_hours", "grace_minutes", "min_staff"].forEach((k) => (b[k] = Number(b[k])));
      await api(s.id ? `/api/admin/stations/${s.id}` : "/api/admin/stations", { method: s.id ? "PUT" : "POST", body: b });
      toast("✅ انحفظت المحطة"); aStations();
    });
  const site = $("[name=site_polygon]", body);
  wirePolyPreview($("[name=polygon]", body), $("#pv", body), () => [{ points: parsePolyClient(site.value), color: "#2563eb", label: "موقع العمل" }]);
  site.addEventListener("input", () => $("[name=polygon]", body).dispatchEvent(new Event("input")));
}

async function aReports() {
  const month = S.rmonth || today().slice(0, 7);
  const stations = await api("/api/admin/stations");
  const sid = S.rsid ?? "";
  const rows = await api(`/api/admin/report?month=${month}${sid ? `&station_id=${sid}` : ""}`);
  const tot = (k) => Math.round(rows.reduce((a, r) => a + r[k], 0) * 10) / 10;
  $("#main").innerHTML = `<div class="card"><div class="row between"><div class="row"><input type="month" id="rm" value="${month}" style="width:auto">
      <select id="rs" style="width:auto"><option value="">كل المحطات</option>${stations.map((s) => `<option value="${s.id}" ${s.id == sid ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></div>
      <a class="btn primary" href="/api/admin/report.csv?month=${month}${sid ? `&station_id=${sid}` : ""}">⬇️ Excel للرواتب</a></div></div>
    <div class="grid" style="margin-bottom:14px"><div class="stat"><b>${tot("worked_hours")}</b><span>ساعة دوام</span></div>
      <div class="stat"><b>${tot("overtime_hours")}</b><span>ساعة إضافي</span></div><div class="stat"><b>${tot("absences")}</b><span>غياب</span></div>
      <div class="stat"><b>${hm(tot("late_minutes"))}</b><span>مجموع التأخير</span></div></div>
    <div class="card table-wrap"><table><thead><tr><th>الموظف</th><th>مجدول</th><th>فعلي</th><th>بالموقع</th><th>إضافي</th><th>تأخير</th><th>غياب</th><th>نسي انطلاق</th><th>إجازة</th></tr></thead><tbody>
    ${rows.map((r) => `<tr data-r="${r.user.id}" style="cursor:pointer"><td><b>${esc(r.user.name)}</b></td><td>${r.scheduled_hours}</td><td>${r.worked_hours}</td><td>${r.site_hours}</td>
      <td>${r.overtime_hours}</td><td>${hm(r.late_minutes)}</td><td>${r.absences ? badge([r.absences, "b-bad"]) : 0}</td><td>${r.missing_departs ? badge([r.missing_departs, "b-warn"]) : 0}</td><td>${r.leave_shifts}</td></tr>`).join("")}
    </tbody></table></div><p class="small muted">اكبس على أي موظف لتشوف التفاصيل يوم بيوم</p>`;
  $("#rm").onchange = (e) => { S.rmonth = e.target.value; aReports(); };
  $("#rs").onchange = (e) => { S.rsid = e.target.value; aReports(); };
  $$("[data-r]").forEach((tr) => (tr.onclick = async () => {
    const d = await api(`/api/admin/report/${tr.dataset.r}?month=${month}`);
    openDialog(`<h2>${esc(d.user.name)} · ${esc(month)}</h2><ul class="list">${d.days.map((x) => `<li><div class="row between"><b>${fmtDay(x.shift?.start || x.arrive)} ${x.shift ? kindName(x.shift.kind) : ""}</b>${badge(STATUS[x.status])}</div>
      <div class="small muted">${x.arrive ? `${fmtTime(x.arrive)} ← ${fmtTime(x.depart)} · ${hm(x.worked_min)} · بالموقع ${hm(x.site_min)}` : ""}${x.late_min ? ` · تأخير ${hm(x.late_min)}` : ""}${x.overtime_min ? ` · إضافي ${hm(x.overtime_min)}` : ""} ${x.notes.map(esc).join(" · ")}</div></li>`).join("") || `<li class="empty">ما في بيانات</li>`}</ul>
      <h3>كل الحركات</h3><ul class="list small">${d.events.map((e) => `<li class="row between"><span>${ICON[e.type]} ${esc(e.label)} ${e.flagged ? `⚠️ ${esc(e.flag_reason)}` : ""} ${e.lat != null ? `<a href="${mapLink(e.lat, e.lng)}" target="_blank" rel="noopener">📍</a>` : ""}</span><span class="muted">${fmtDT(e.ts)}</span></li>`).join("")}</ul>
      <div class="row"><button class="btn ghost" value="cancel">إغلاق</button></div>`);
  }));
}

boot();
