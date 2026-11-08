// FlipScout owner panel. Plain ES modules: no framework, no build step, no third-party code.
// Every value from the API is rendered with textContent (never innerHTML), so data can't inject markup.

const app = document.getElementById("app");
const toasts = document.getElementById("toasts");

// ── tiny DOM helpers ─────────────────────────────────────────────────────

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  append(el, kids);
  return el;
}

function append(el, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const SVG = "http://www.w3.org/2000/svg";
function s(tag, attrs, ...kids) {
  const el = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs || {})) el.setAttribute(key, String(value));
  for (const kid of kids.flat()) if (kid) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
}

function toast(text, kind = "") {
  const el = h("div", { class: `toast ${kind}` }, text);
  toasts.append(el);
  setTimeout(() => el.remove(), 8000);
}

// ── API ──────────────────────────────────────────────────────────────────

class AuthError extends Error {}

async function api(path, { method = "GET", body } = {}) {
  const headers = { Accept: "application/json" };
  if (method !== "GET") headers["X-FlipScout"] = "1";
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await response.json(); } catch { /* empty body */ }
  if (response.status === 401 && path !== "/api/session") throw new AuthError();
  if (!response.ok) throw new Error((data && (data.detail || data.error)) || `HTTP ${response.status}`);
  return data;
}

// ── formatting ───────────────────────────────────────────────────────────

function money(cents, currency = "USD") {
  if (cents === null || cents === undefined) return "—";
  return new Intl.NumberFormat(undefined, { style: "currency", currency: currency || "USD" }).format(cents / 100);
}
function signedMoney(cents) {
  if (cents === null || cents === undefined) return h("span", { class: "muted" }, "—");
  return h("span", { class: cents >= 0 ? "pos" : "neg" }, (cents >= 0 ? "+" : "−") + money(Math.abs(cents)));
}
function pct(value) {
  return value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;
}
function duration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const units = [["d", 86400], ["h", 3600], ["m", 60]];
  for (const [label, size] of units) if (seconds >= size) {
    const major = Math.floor(seconds / size);
    return `${major}${label}`;
  }
  return `${Math.max(0, Math.round(seconds))}s`;
}
function when(iso) {
  if (!iso) return h("span", { class: "muted" }, "—");
  const date = new Date(iso);
  const ago = (Date.now() - date.getTime()) / 1000;
  return h("span", { title: date.toLocaleString() }, ago >= 0 ? `${duration(ago)} ago` : `in ${duration(-ago)}`);
}

const STATUS = {
  in_stock: ["In stock", "good"],
  low_stock: ["Low stock", "warn"],
  out_of_stock: ["Sold out", "bad"],
  coming_soon: ["Coming soon", "info"],
  unknown: ["Unknown", "neutral"],
};
function statusPill(status, delisted) {
  if (delisted) return h("span", { class: "pill neutral" }, "Delisted");
  const [label, kind] = STATUS[status] || [status || "—", "neutral"];
  return h("span", { class: `pill ${kind}` }, label);
}

const BASIS = {
  sold: "eBay sold",
  manual: "your sales",
  scarcity: "sold-out history",
  active: "sold-out market",
  capped: "capped (in stock)",
};
const CONFIDENCE = { high: "good", medium: "info", low: "warn", none: "neutral" };
function confidencePill(value) {
  return h("span", { class: `pill ${CONFIDENCE[value] || "neutral"}` }, value || "none");
}

const KINDS = {
  deal: "Deal",
  back_in_stock: "Back in stock",
  new_listing: "New listing",
  early_access: "Early Access",
  sold_out: "Sold out",
  price_change: "Price change",
  restock_eta: "Restock date",
  status_change: "Status change",
  delisted: "Delisted",
  relisted: "Relisted",
};
const KIND_STYLE = { deal: "good", back_in_stock: "good", new_listing: "info", early_access: "info", sold_out: "bad", delisted: "neutral" };

function productLink(key, text) {
  return h("a", { href: `#/product/${encodeURIComponent(key)}` }, text);
}

function table(columns, rows, emptyText = "Nothing here yet.") {
  if (!rows.length) return h("div", { class: "empty" }, emptyText);
  return h("div", { class: "table-wrap" },
    h("table", {},
      h("thead", {}, h("tr", {}, columns.map((c) => h("th", { class: c.num ? "num" : "" }, c.label)))),
      h("tbody", {}, rows.map((row) => h("tr", {}, columns.map((c) => h("td", { class: c.num ? "num" : "" }, c.cell(row))))))));
}

// ── chart (inline SVG) ───────────────────────────────────────────────────

function lineChart(series, { height = 200, format = (v) => v, labels = [] } = {}) {
  const width = 640, left = 64, right = 12, top = 12, bottom = 26;
  const all = series.flatMap((x) => x.values.filter((v) => v !== null && v !== undefined));
  if (!all.length) return h("div", { class: "empty" }, "No history yet. It builds up one snapshot per day.");
  let min = Math.min(...all), max = Math.max(...all);
  if (min === max) { min -= Math.abs(min) * 0.1 || 1; max += Math.abs(max) * 0.1 || 1; }
  const n = Math.max(...series.map((x) => x.values.length));
  const x = (i) => left + (n <= 1 ? (width - left - right) / 2 : (i * (width - left - right)) / (n - 1));
  const y = (v) => top + (1 - (v - min) / (max - min)) * (height - top - bottom);
  const svg = s("svg", { viewBox: `0 0 ${width} ${height}`, role: "img" });
  for (const v of [min, (min + max) / 2, max]) {
    svg.append(s("line", { class: "grid-line", x1: left, x2: width - right, y1: y(v), y2: y(v) }));
    svg.append(s("text", { class: "axis", x: left - 6, y: y(v) + 4, "text-anchor": "end" }, format(v)));
  }
  if (labels.length) {
    svg.append(s("text", { class: "axis", x: x(0), y: height - 6, "text-anchor": "start" }, labels[0]));
    if (labels.length > 1) svg.append(s("text", { class: "axis", x: x(labels.length - 1), y: height - 6, "text-anchor": "end" }, labels[labels.length - 1]));
  }
  series.forEach((serie, index) => {
    const cls = `s${index + 1}`;
    const points = serie.values.map((v, i) => (v === null || v === undefined ? null : [x(i), y(v)])).filter(Boolean);
    if (points.length > 1) svg.append(s("polyline", { class: cls, points: points.map((p) => p.join(",")).join(" ") }));
    for (const [px, py] of points) svg.append(s("circle", { class: cls, cx: px, cy: py, r: points.length > 1 ? 2.5 : 4 }));
  });
  return h("div", { class: "chart" }, svg,
    h("div", { class: "legend" }, series.map((serie, index) => h("span", {}, h("i", { class: `s${index + 1}` }), serie.label))));
}

// ── shell ────────────────────────────────────────────────────────────────

const NAV = [
  ["#/", "Overview"],
  ["#/opportunities", "Opportunities"],
  ["#/radar", "Radar"],
  ["#/servers", "Servers"],
  ["#/audit", "Audit log"],
];

function shell(content, title, subtitle) {
  const hash = location.hash || "#/";
  const nav = h("nav", { class: "nav" }, NAV.map(([href, label]) => {
    const active = href === "#/" ? hash === "#/" || hash === "" : hash.startsWith(href);
    return h("a", { href, class: active ? "active" : "" }, label);
  }));
  const side = h("aside", { class: "side" },
    h("div", { class: "brand" }, h("img", { src: "/static/icon.svg", alt: "" }), "FlipScout"),
    nav,
    h("div", { class: "spacer" }),
    h("button", { onclick: logout }, "Log out"),
    h("button", { class: "danger", onclick: revokeAll, title: "End every panel session, including this one" }, "Sign out everywhere"));
  const main = h("main", { class: "main" },
    h("div", { class: "topbar" },
      h("div", {}, h("h1", {}, title), subtitle ? h("div", { class: "muted" }, subtitle) : null),
      searchBox()),
    content);
  app.className = "";
  app.replaceChildren(h("div", { class: "shell" }, side, main));
}

function searchBox() {
  const box = h("div", { class: "search" });
  const input = h("input", { type: "search", placeholder: "Find a product (SKU or name)…", "aria-label": "Find a product" });
  const list = h("div", { class: "suggest", hidden: true });
  let timer = null;
  let items = [];
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { list.hidden = true; return; }
    timer = setTimeout(async () => {
      try {
        items = (await api(`/api/search?q=${encodeURIComponent(q)}`)).items;
      } catch (error) { return handleError(error); }
      list.replaceChildren(...items.map((m) => productLink(m.variant_id, `${m.sku} · ${m.name}${m.pack_size > 1 ? ` (${m.pack_size}-pack)` : ""}`)));
      list.hidden = !items.length;
    }, 200);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && items.length) location.hash = `#/product/${items[0].variant_id}`;
    if (event.key === "Escape") list.hidden = true;
  });
  list.addEventListener("click", () => { list.hidden = true; input.value = ""; });
  return append(box, [input, list]);
}

// ── auth ─────────────────────────────────────────────────────────────────

function loginView(message = "") {
  stopTimers();
  const error = h("div", { class: "error", role: "alert" }, message);
  const input = h("input", { inputmode: "numeric", autocomplete: "one-time-code", maxlength: 6, pattern: "[0-9]{6}", required: true, "aria-label": "Authenticator code", placeholder: "000000" });
  const button = h("button", { class: "primary", type: "submit" }, "Log in");
  const form = h("form", {
    onsubmit: async (event) => {
      event.preventDefault();
      const code = input.value.replace(/\s/g, "");
      if (!/^\d{6}$/.test(code)) { error.textContent = "Enter the 6-digit code from your authenticator app."; return; }
      button.disabled = true;
      try {
        await api("/api/session", { method: "POST", body: { code } });
        route();
      } catch (err) {
        error.textContent = err.message;
        input.value = "";
        input.focus();
      } finally { button.disabled = false; }
    },
  }, input, button, error);
  input.addEventListener("input", () => { error.textContent = ""; });
  app.className = "";
  app.replaceChildren(h("div", { class: "login" },
    h("div", { class: "card" },
      h("div", { class: "brand" }, h("img", { src: "/static/icon.svg", alt: "" }), "FlipScout"),
      h("div", { class: "muted" }, "Owner panel. Enter the code from your authenticator app."),
      form)));
  input.focus();
}

async function logout() {
  try { await api("/api/session", { method: "DELETE" }); } catch { /* already gone */ }
  loginView("Logged out.");
}

async function revokeAll() {
  if (!confirm("End every panel session on every device, including this one?")) return;
  try { await api("/api/session/revoke-all", { method: "POST" }); } catch { /* ignore */ }
  loginView("All sessions ended.");
}

function handleError(error) {
  if (error instanceof AuthError) return loginView("Your session ended. Log in again.");
  toast(error.message, "bad");
}

// ── timers (polling) ─────────────────────────────────────────────────────

let timers = [];
function every(ms, fn) { timers.push(setInterval(fn, ms)); }
function stopTimers() { timers.forEach(clearInterval); timers = []; }

let lastDealId = null;
async function checkDeals() {
  try {
    const { items } = await api("/api/events?kind=deal&limit=5");
    const newest = items.length ? items[0].id : 0;
    if (lastDealId !== null) {
      for (const e of items.filter((x) => x.id > lastDealId).reverse()) toast(`Deal: ${e.sku} ${e.region ? e.region.toUpperCase() : ""} · ${e.new_value || ""}`);
    }
    lastDealId = Math.max(lastDealId || 0, newest);
  } catch (error) { if (error instanceof AuthError) loginView("Your session ended. Log in again."); }
}

// ── pages ────────────────────────────────────────────────────────────────

async function overviewPage() {
  const data = await api("/api/overview");
  const v = data.valuations || {};
  const tile = (label, value, sub) => h("div", { class: "card tile" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), sub ? h("div", { class: "sub" }, sub) : null);
  const regionRows = Object.entries(data.regions).sort(([a], [b]) => a.localeCompare(b));
  const content = h("div", {},
    h("div", { class: "grid tiles" },
      tile("Credible flips", data.credible_flips, "sold, your sales, or sold-out history"),
      tile("Active deals", data.active_deals, "buyable now, credible profit"),
      tile("Events (24h)", data.events_24h, `${data.pending_events} waiting to send`),
      tile("Watches", data.watches, `${data.subscribers} subscribers`),
      tile("Uptime", duration(data.uptime_seconds), `v${data.version}`)),
    h("div", { class: "grid cols-2" },
      h("div", { class: "card" }, h("h2", {}, "Stores"),
        table([
          { label: "Region", cell: ([r]) => r.toUpperCase() },
          { label: "In stock", num: true, cell: ([, x]) => (x.counts.in_stock || 0) + (x.counts.low_stock || 0) },
          { label: "Sold out", num: true, cell: ([, x]) => x.counts.out_of_stock || 0 },
          { label: "Coming soon", num: true, cell: ([, x]) => x.counts.coming_soon || 0 },
          { label: "Last check", cell: ([, x]) => when(x.last_checked) },
        ], regionRows)),
      h("div", { class: "card" }, h("h2", {}, "Valuations"),
        h("dl", { class: "kv" },
          h("dt", {}, "Sold-out market"), h("dd", {}, data.sold_out_market),
          h("dt", {}, "Capped (in stock)"), h("dd", {}, v.capped || 0),
          h("dt", {}, "In stock, asks under the cap"), h("dd", {}, data.in_stock_under_cap),
          h("dt", {}, "eBay sold / your sales"), h("dd", {}, (v.sold || 0) + (v.manual || 0)),
          h("dt", {}, "Not enough data"), h("dd", {}, v.none || 0)),
        h("p", { class: "muted" }, "Capped items are in stock at the store, so eBay asking prices don't count as profit. Credible flips come from real sales or prices recorded while the store was sold out.")),
      h("div", { class: "card" }, h("h2", {}, "Jobs"),
        table([
          { label: "Job", cell: (j) => h("span", { class: "mono" }, j.name) },
          { label: "Runs", num: true, cell: (j) => j.runs },
          { label: "Failures", num: true, cell: (j) => (j.failures ? h("span", { class: "neg" }, j.failures) : 0) },
          { label: "Next", num: true, cell: (j) => (j.running ? "running" : `in ${duration(j.next_in_seconds)}`) },
        ], data.jobs, "The panel isn't attached to a running scheduler.")),
      h("div", { class: "card" }, h("h2", {}, "Sources"),
        h("dl", { class: "kv" },
          h("dt", {}, "Discord bot"), h("dd", {}, onOff(data.sources.discord)),
          h("dt", {}, "eBay API"), h("dd", {}, onOff(data.sources.ebay)),
          h("dt", {}, "SMS"), h("dd", {}, onOff(data.sources.sms)),
          h("dt", {}, "UniFi regions"), h("dd", {}, data.sources.unifi_regions.join(", ").toUpperCase()),
          h("dt", {}, "Tailscale login allowlist"), h("dd", {}, onOff(data.sources.tailscale_logins))),
        h("p", { class: "muted" }, "Keys stay on the server. The panel only shows whether each one is set."))));
  shell(content, "Overview", `Up ${duration(data.uptime_seconds)} · refreshes every 30s`);
}

function onOff(value) {
  return h("span", { class: `pill ${value ? "good" : "neutral"}` }, value ? "configured" : "off");
}

const VIEWS = {
  flips: ["Credible flips", "Profit backed by eBay sold prices, your recorded sales, or prices seen while the store was sold out."],
  sold_out: ["Sold-out market", "The store is sold out, so eBay asking prices are the real market. Watch these for restocks."],
  capped: ["Hyped in stock", "In stock at the store but listed far above retail on eBay. Asking prices only; not profit."],
  all: ["Everything valued", "Every item with enough eBay data, best score first."],
};

async function opportunitiesPage(params) {
  const view = VIEWS[params.get("view")] ? params.get("view") : "flips";
  const q = params.get("q") || "";
  const data = await api(`/api/market?view=${view}&q=${encodeURIComponent(q)}`);
  const go = (next, query = q) => { location.hash = `#/opportunities?view=${next}${query ? `&q=${encodeURIComponent(query)}` : ""}`; };
  const filter = h("input", { type: "search", value: q, placeholder: "Filter…", "aria-label": "Filter items" });
  filter.addEventListener("change", () => go(view, filter.value.trim()));
  const tabs = h("div", { class: "tabs" },
    Object.entries(VIEWS).map(([key, [label]]) => h("button", { class: key === view ? "on" : "", onclick: () => go(key) }, label)),
    filter);
  const name = { label: "Product", cell: (r) => h("div", {}, productLink(r.variant_id, r.sku), r.pack_size > 1 ? h("span", { class: "muted" }, ` ×${r.pack_size}`) : null, h("div", { class: "muted" }, r.name)) };
  const where = { label: "Store", cell: (r) => h("div", { class: "row" }, (r.region || "").toUpperCase(), statusPill(r.status)) };
  const buy = { label: "Buy", num: true, cell: (r) => money(r.valuation.retail_cents) };
  const columns = {
    flips: [name, where, buy,
      { label: "Resale", num: true, cell: (r) => money(r.valuation.flip_basis === "scarcity" ? r.valuation.scarcity_resale_cents : r.valuation.resale_cents) },
      { label: "Net", num: true, cell: (r) => signedMoney(r.valuation.flip_net_cents) },
      { label: "ROI", num: true, cell: (r) => pct(r.valuation.flip_roi) },
      { label: "Evidence", cell: (r) => (r.valuation.flip_basis === "scarcity" ? `sold-out ×${r.valuation.scarcity_days}d` : BASIS[r.valuation.flip_basis] || "—") },
      { label: "Confidence", cell: (r) => confidencePill(r.valuation.flip_confidence) }],
    sold_out: [name, where, buy,
      { label: "eBay floor", num: true, cell: (r) => money(r.valuation.resale_cents) },
      { label: "Net if bought at retail", num: true, cell: (r) => signedMoney(r.valuation.net_cents) },
      { label: "ROI", num: true, cell: (r) => pct(r.valuation.roi) },
      { label: "Sellers", num: true, cell: (r) => r.valuation.active_count },
      { label: "Sold-out days seen", num: true, cell: (r) => r.valuation.scarcity_days || 0 }],
    capped: [name, where, buy,
      { label: "eBay median ask", num: true, cell: (r) => money(r.valuation.active_median_cents) },
      { label: "Over retail", num: true, cell: (r) => (r.valuation.active_median_cents && r.valuation.retail_cents ? pct(r.valuation.active_median_cents / r.valuation.retail_cents - 1) : "—") },
      { label: "Sellers", num: true, cell: (r) => r.valuation.active_count },
      { label: "Credible flip", num: true, cell: (r) => signedMoney(r.valuation.flip_net_cents) }],
    all: [name, where, buy,
      { label: "Resale", num: true, cell: (r) => money(r.valuation.resale_cents) },
      { label: "Net", num: true, cell: (r) => signedMoney(r.valuation.net_cents) },
      { label: "Basis", cell: (r) => BASIS[r.valuation.basis] || "—" },
      { label: "Credible flip", num: true, cell: (r) => signedMoney(r.valuation.flip_net_cents) }],
  }[view];
  const empty = view === "flips"
    ? "No credible flips yet. They appear once an item has sold-out history (2+ days for low confidence, 4+ days with 3+ sellers for a deal), eBay sold data, or your own /comp sales."
    : "Nothing matches.";
  shell(h("div", {}, tabs, h("p", { class: "muted" }, VIEWS[view][1]), h("div", { class: "card" }, table(columns, data.items, empty))),
    "Opportunities", `${data.items.length} item(s)`);
}

function verdict(v) {
  if (!v) return h("div", { class: "empty" }, "Not valued yet: no eBay data for this item.");
  const notes = [];
  if (v.basis === "capped") notes.push(h("div", { class: "note warn" }, `Capped: the store has it in stock, so eBay asking prices are capped at about ${money(v.resale_cents)}. Listings above that are resellers hoping, not real resale value.`));
  if (v.scarcity_days && v.scarcity_resale_cents) notes.push(h("div", { class: "note" }, `Sold-out market: about ${money(v.scarcity_resale_cents)} across ${v.scarcity_days} day(s) the store was sold out`, v.scarcity_net_cents !== null ? ` → net ${money(v.scarcity_net_cents)} (${pct(v.scarcity_roi)})` : "", "."));
  if (!v.available && v.basis === "active" && v.net_cents !== null) notes.push(h("div", { class: "note" }, `Sold out at the store: eBay's price floor (${money(v.resale_cents)}) is the live market. Bought at retail on a restock, that's `, signedMoney(v.net_cents), ` (${pct(v.roi)}), based on asking prices from ${v.active_count} seller(s). It becomes a credible flip once sold-out days are recorded.`));
  if (v.flip_basis) notes.push(h("div", { class: "note good" }, `Credible flip (${BASIS[v.flip_basis]}, ${v.flip_confidence} confidence): `, signedMoney(v.flip_net_cents), ` per unit, ${pct(v.flip_roi)} ROI.`));
  else notes.push(h("div", { class: "note" }, "No credible flip yet: needs eBay sold data, your own sales (/comp), or prices recorded while the store is sold out."));
  return h("div", { class: "verdict" }, notes);
}

async function productPage(key) {
  const p = await api(`/api/products/${encodeURIComponent(key)}`);
  const v = p.valuation;
  const hist = p.ebay_history;
  const labels = hist.map((x) => x.day);
  const trend = p.trend;
  const content = h("div", { class: "grid" },
    h("div", { class: "grid cols-2" },
      h("div", { class: "card" }, h("h2", {}, "Verdict"), verdict(v)),
      h("div", { class: "card" }, h("h2", {}, "Valuation"),
        v ? h("dl", { class: "kv" },
          h("dt", {}, "Buy at retail"), h("dd", {}, money(v.retail_cents)),
          h("dt", {}, "Resale used"), h("dd", {}, money(v.resale_cents), " ", h("span", { class: "muted" }, `(${BASIS[v.basis] || "—"})`)),
          h("dt", {}, "Fees + shipping"), h("dd", {}, money((v.fees_cents || 0) + (v.shipping_cents || 0))),
          h("dt", {}, "Net"), h("dd", {}, signedMoney(v.net_cents), ` (${pct(v.roi)})`),
          h("dt", {}, "eBay sellers"), h("dd", {}, `${v.active_count} · median ask ${money(v.active_median_cents)} · low ${money(v.active_low_cents)}`),
          h("dt", {}, "Confidence"), h("dd", {}, confidencePill(v.confidence)),
          h("dt", {}, "Valued"), h("dd", {}, when(v.computed_at))) : h("div", { class: "empty" }, "—"),
        trend ? h("p", { class: "muted" }, `eBay over ${trend.days} day(s): sellers ${trend.sellers_then} → ${trend.sellers_now}${trend.sellers_change !== null ? ` (${pct(trend.sellers_change)})` : ""}, price floor ${money(trend.p25_then)} → ${money(trend.p25_now)}.`) : null)),
    h("div", { class: "card" }, h("h2", {}, "Stores"),
      table([
        { label: "Region", cell: (l) => l.region.toUpperCase() },
        { label: "Status", cell: (l) => h("div", { class: "row" }, statusPill(l.status, l.delisted_at), l.early_access ? h("span", { class: "pill info" }, "EA") : null) },
        { label: "Price", num: true, cell: (l) => money(l.price_cents, l.currency) },
        { label: "Restocks", num: true, cell: (l) => l.restocks },
        { label: "Sells out in", num: true, cell: (l) => duration(l.median_sellout_seconds) },
        { label: "Out of stock for", num: true, cell: (l) => (l.out_of_stock_seconds ? `${duration(l.out_of_stock_seconds)}${l.out_of_stock_at_least ? "+" : ""}` : "—") },
        { label: "Restock ETA", cell: (l) => l.restock_eta || "—" },
        { label: "Checked", cell: (l) => when(l.last_checked_at) },
        { label: "", cell: (l) => h("a", { href: l.url, target: "_blank", rel: "noopener noreferrer" }, "Store ↗") },
      ], p.listings)),
    h("div", { class: "grid cols-2" },
      h("div", { class: "card" }, h("h2", {}, "eBay prices (daily)"),
        lineChart([
          { label: "Price floor (25th pct)", values: hist.map((x) => x.p25_cents) },
          { label: "Median ask", values: hist.map((x) => x.median_cents) },
          { label: "Retail", values: hist.map((x) => x.retail_cents) },
        ], { format: (c) => money(c).replace(/\.\d\d$/, ""), labels })),
      h("div", { class: "card" }, h("h2", {}, "eBay sellers (daily)"),
        lineChart([{ label: "Distinct sellers", values: hist.map((x) => x.sellers) }], { format: (n) => String(Math.round(n)), labels }))),
    h("div", { class: "card" }, h("h2", {}, "Recent events"),
      table([
        { label: "When", cell: (e) => when(e.occurred_at) },
        { label: "Event", cell: (e) => h("span", { class: `pill ${KIND_STYLE[e.kind] || "neutral"}` }, KINDS[e.kind] || e.kind) },
        { label: "Region", cell: (e) => (e.region || "").toUpperCase() },
        { label: "Detail", cell: (e) => [e.old_value, e.new_value].filter(Boolean).join(" → ") },
      ], p.events, "No events recorded for this product.")));
  shell(content, `${p.sku}${p.pack_size > 1 ? ` ×${p.pack_size}` : ""}`, `${p.name}${p.category ? ` · ${p.category}` : ""}`);
}

const RADAR_FILTERS = [["", "All"], ["deal", "Deals"], ["back_in_stock", "Back in stock"], ["new_listing", "New"], ["early_access", "Early Access"], ["sold_out", "Sold out"], ["price_change", "Price"]];

async function radarPage(params) {
  const kind = params.get("kind") || "";
  const data = await api(`/api/events?limit=150${kind ? `&kind=${encodeURIComponent(kind)}` : ""}`);
  const tabs = h("div", { class: "tabs" }, RADAR_FILTERS.map(([key, label]) =>
    h("button", { class: key === kind ? "on" : "", onclick: () => { location.hash = key ? `#/radar?kind=${key}` : "#/radar"; } }, label)));
  const feed = data.items.length
    ? h("div", { class: "feed" }, data.items.map((e) => h("div", { class: "card event" },
      h("div", { class: "when" }, when(e.occurred_at)),
      h("div", {},
        h("div", { class: "row" },
          h("span", { class: `pill ${KIND_STYLE[e.kind] || "neutral"}` }, KINDS[e.kind] || e.kind),
          e.sku ? productLink(e.sku, e.sku) : null,
          e.region ? h("span", { class: "muted" }, e.region.toUpperCase()) : null,
          e.price_cents !== null ? h("span", {}, money(e.price_cents, e.currency)) : null,
          e.early_access ? h("span", { class: "pill info" }, "EA") : null),
        h("div", { class: "muted" }, e.name || ""),
        e.kind === "deal" && e.new_value ? h("div", { class: "pos" }, e.new_value) : null,
        ["price_change", "restock_eta", "status_change"].includes(e.kind) ? h("div", {}, `${e.old_value || "—"} → ${e.new_value || "—"}`) : null,
        e.kind !== "deal" && e.profit_text ? h("div", { class: "pos" }, e.profit_text) : null))))
    : h("div", { class: "card empty" }, "No events yet.");
  shell(h("div", {}, tabs, feed), "Radar", "Newest first · refreshes every 30s");
}

async function serversPage() {
  const data = await api("/api/servers");
  const decide = (g, status) => async () => {
    const verb = status === "approved" ? "Approve" : "Block";
    if (!confirm(`${verb} ${g.name || g.guild_id}?${status === "blocked" ? " The bot will leave it." : ""}`)) return;
    try {
      await api(`/api/servers/${g.guild_id}/decision`, { method: "POST", body: { status } });
      toast(`${verb}d ${g.name || g.guild_id}.`);
      route();
    } catch (error) { handleError(error); }
  };
  const statusKind = { approved: "good", pending: "warn", blocked: "bad" };
  shell(h("div", { class: "card" }, table([
    { label: "Server", cell: (g) => h("div", {}, g.name || "Unknown", h("div", { class: "muted mono" }, g.guild_id)) },
    { label: "Status", cell: (g) => h("span", { class: `pill ${statusKind[g.status] || "neutral"}` }, g.status) },
    { label: "Alerts", cell: (g) => (g.alert_channel ? "channel set" : h("span", { class: "muted" }, "no channel")) },
    { label: "Valuations", cell: (g) => (g.show_valuations ? "shown" : "hidden") },
    { label: "Decided", cell: (g) => when(g.decided_at) },
    { label: "", cell: (g) => h("div", { class: "row" },
      g.status !== "approved" ? h("button", { onclick: decide(g, "approved") }, "Approve") : null,
      g.status !== "blocked" ? h("button", { class: "danger", onclick: decide(g, "blocked") }, "Block") : null) },
  ], data.items, "The bot hasn't joined any servers.")), "Servers", "Approve or block Discord servers. Changes are audited.");
}

async function auditPage() {
  const data = await api("/api/audit?limit=200");
  shell(h("div", { class: "card" }, table([
    { label: "When", cell: (a) => when(a.at) },
    { label: "Who", cell: (a) => a.actor },
    { label: "Action", cell: (a) => h("span", { class: "mono" }, a.action) },
    { label: "Target", cell: (a) => a.target || "—" },
    { label: "Details", cell: (a) => (a.details ? h("span", { class: "mono muted" }, typeof a.details === "string" ? a.details : JSON.stringify(a.details)) : "—") },
  ], data.items, "No admin actions recorded yet.")), "Audit log", "Append-only: entries can't be edited or deleted.");
}

// ── router ───────────────────────────────────────────────────────────────

async function route() {
  stopTimers();
  const hash = location.hash.replace(/^#/, "") || "/";
  const [path, query = ""] = hash.split("?");
  const params = new URLSearchParams(query);
  const parts = path.split("/").filter(Boolean);
  try {
    if (parts[0] === "opportunities") await opportunitiesPage(params);
    else if (parts[0] === "product" && parts[1]) await productPage(decodeURIComponent(parts[1]));
    else if (parts[0] === "radar") { await radarPage(params); every(30000, () => radarPage(params).catch(handleError)); }
    else if (parts[0] === "servers") await serversPage();
    else if (parts[0] === "audit") await auditPage();
    else { await overviewPage(); every(30000, () => overviewPage().catch(handleError)); }
    every(60000, checkDeals);
    window.scrollTo(0, 0);
  } catch (error) {
    handleError(error);
  }
}

async function start() {
  try {
    const session = await api("/api/session");
    if (!session.authenticated) return loginView();
  } catch (error) {
    app.textContent = `Can't reach FlipScout: ${error.message}`;
    return;
  }
  await checkDeals();
  route();
}

window.addEventListener("hashchange", route);
start();
