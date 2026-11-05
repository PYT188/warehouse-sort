const ZONES = window.__ZONES__ || {};
const ORDER = Object.keys(ZONES);
const $ = (id) => document.getElementById(id);

/* ---------- 底栏格口 ---------- */
const tiles = {};
ORDER.forEach((code) => {
  const el = document.createElement("div");
  el.className = "chute-tile";
  el.style.setProperty("--c", ZONES[code].color);
  el.innerHTML = `<div class="letter">${code}</div>
                  <div class="meta"><b>${ZONES[code].zone}</b><span>0</span></div>`;
  $("chutes").appendChild(el);
  tiles[code] = el.querySelector("span");
});

/* ---------- 图例 ---------- */
ORDER.forEach((code) => {
  const s = document.createElement("span");
  s.innerHTML = `<i style="background:${ZONES[code].color}"></i>${code} · ${ZONES[code].zone}`;
  $("legend").appendChild(s);
});

/* ---------- 实时卡片 ---------- */
const cards = new Map();

function makeCard(it) {
  const el = document.createElement("div");
  el.className = "card";
  el.innerHTML = `
    <div class="c-head">
      <div class="c-name"><b></b><span></span></div>
      <div class="chute"></div>
    </div>
    <div class="c-zone"></div>
    <div class="c-conf"><i></i></div>
    <div class="c-handle"></div>
    <div class="c-reason"></div>`;
  el._refs = {
    name: el.querySelector(".c-name b"),
    en: el.querySelector(".c-name span"),
    chute: el.querySelector(".chute"),
    zone: el.querySelector(".c-zone"),
    bar: el.querySelector(".c-conf i"),
    handle: el.querySelector(".c-handle"),
    reason: el.querySelector(".c-reason"),
  };
  return el;
}

function renderCard(el, it) {
  const color = (ZONES[it.chute] || {}).color || "#38bdf8";
  el.style.setProperty("--c", color);
  const r = el._refs;
  r.name.textContent = it.item_zh || it.item;
  r.en.textContent = it.item;
  r.chute.textContent = it.chute;
  r.zone.innerHTML = `${it.zone} · <em>置信度 ${(it.conf * 100).toFixed(0)}%</em>`;
  r.bar.style.width = `${Math.round(it.conf * 100)}%`;
  r.handle.textContent = it.handling || "";
  r.reason.textContent = it.reason || "";
}

let emptyEl = $("empty");

function renderItems(items) {
  const now = performance.now();
  const alive = new Set();

  items.forEach((it) => {
    alive.add(it.item);
    let el = cards.get(it.item);
    if (!el) {
      el = makeCard(it);
      cards.set(it.item, el);
      $("items").appendChild(el);
    }
    el._seen = now;
    renderCard(el, it);
  });

  cards.forEach((el, key) => {
    if (!alive.has(key) && now - (el._seen || 0) > 2200) {
      el.remove();
      cards.delete(key);
    }
  });

  emptyEl.style.display = cards.size ? "none" : "block";
}

/* ---------- 状态轮询 ---------- */
let online = false;

async function tick() {
  try {
    const s = await (await fetch("/api/state", { cache: "no-store" })).json();
    online = true;
    $("fps").textContent = s.fps ?? "--";
    $("infer").textContent = s.infer_ms ?? "--";
    $("backend").textContent = s.backend ?? "--";
    $("seen").textContent = `${s.seen ?? 0} 件`;

    Object.keys(s.totals || {}).forEach((code) => {
      if (tiles[code]) tiles[code].textContent = s.totals[code];
    });

    renderItems(s.items || []);

    const dev = s.device || {};
    const badge = $("deviceBadge");
    badge.textContent = dev.enabled
      ? `设备 · ${dev.kind} · 成功 ${dev.ok}/${dev.sent}`
      : "设备 · 未启用";

    const box = $("dispatch");
    if (dev.last) {
      box.className = "dispatch " + (dev.last.ok ? "ok" : "bad");
      box.innerHTML =
        `最近下发 <b>${dev.last.chute} 号格口</b> · ${dev.last.item} · ${dev.last.ms} ms · ` +
        (dev.last.ok ? "已接受" : `失败（${dev.last.detail}）`);
    } else {
      box.className = "dispatch";
      box.textContent = dev.enabled
        ? `已连接 ${dev.kind} 设备，等待首个目标…`
        : "设备下发未启用（DEVICE=off）";
    }

    $("status").classList.toggle("on", !!s.camera);
    $("status").classList.toggle("off", !s.camera);
    $("statusText").textContent = s.camera ? "在线" : "摄像头离线";
  } catch (e) {
    online = false;
    $("status").classList.remove("on");
    $("status").classList.add("off");
    $("statusText").textContent = "服务未连接";
  } finally {
    setTimeout(tick, 650);
  }
}

/* ---------- 手动查询 ---------- */
async function ask() {
  const name = $("q").value.trim();
  if (!name) return;
  const box = $("manual");
  box.innerHTML = `<div class="empty" style="padding:14px 0">推理中…</div>`;
  try {
    const res = await fetch(`/api/route?name=${encodeURIComponent(name)}`);
    if (!res.ok) throw new Error("bad");
    const it = await res.json();
    const el = makeCard(it);
    renderCard(el, it);
    box.innerHTML = "";
    box.appendChild(el);
  } catch (e) {
    box.innerHTML = `<div class="empty" style="padding:14px 0">查询失败</div>`;
  }
}

$("qbtn").addEventListener("click", ask);
$("q").addEventListener("keydown", (e) => { if (e.key === "Enter") ask(); });

tick();
