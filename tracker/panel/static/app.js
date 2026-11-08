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
  ["#/watchlist", "Watchlist"],
  ["#/discovery", "Discovery"],
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

// ── watchlist ────────────────────────────────────────────────────────────

function dollarsToCents(text) {
  const value = String(text || "").replace(/[$,\s]/g, "");
  if (!value) return null;
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) throw new Error("Enter a price like 249.99");
  return Math.round(number * 100);
}

// Checkboxes for alert types. `selected` null = server defaults (when allowed).
function kindPicker(keys, selected, { allowDefaults = true, defaults = [] } = {}) {
  const useDefaults = h("input", { type: "checkbox" });
  useDefaults.checked = allowDefaults && selected === null;
  const boxes = Object.entries(keys).map(([key, label]) => {
    const box = h("input", { type: "checkbox", value: key });
    box.checked = selected ? selected.includes(key) : defaults.includes(key);
    return [box, h("label", { class: "check" }, box, label)];
  });
  const grid = h("div", { class: "checks" }, boxes.map(([, label]) => label));
  const sync = () => { grid.hidden = useDefaults.checked; };
  useDefaults.addEventListener("change", sync);
  sync();
  const el = h("div", {},
    allowDefaults ? h("label", { class: "check" }, useDefaults, "Use this server's defaults") : null,
    grid);
  return {
    el,
    value: () => (allowDefaults && useDefaults.checked ? null : boxes.filter(([b]) => b.checked).map(([b]) => b.value)),
  };
}

function kindsText(kinds, keys) {
  if (kinds === null || kinds === undefined) return h("span", { class: "muted" }, "defaults");
  if (!kinds.length) return h("span", { class: "muted" }, "none");
  return h("div", { class: "row" }, kinds.map((k) => h("span", { class: "pill neutral" }, keys[k] || k)));
}

function mutedText(until) {
  if (!until || new Date(until) <= new Date()) return null;
  return h("span", { class: "pill warn", title: new Date(until).toLocaleString() }, `muted ${when(until).textContent.replace(/^in /, "")}`);
}

async function mutate(path, method, body, done) {
  try {
    const result = await api(path, { method, body });
    if (done) toast(typeof done === "function" ? done(result) : done);
    route();
    return result;
  } catch (error) { handleError(error); return null; }
}

async function watchlistPage() {
  const data = await api("/api/watchlist");
  const keys = data.alert_keys;
  const mine = data.items.filter((w) => w.owner);
  const members = data.items.filter((w) => !w.owner);
  const selected = new Set();

  // Add form
  const query = h("input", { type: "text", placeholder: "SKU, product name, or store link", "aria-label": "Product to watch", class: "grow" });
  const region = h("select", { "aria-label": "Region" }, h("option", { value: "" }, "All regions"), data.regions.map((r) => h("option", { value: r }, r.toUpperCase())));
  const target = h("input", { type: "text", inputmode: "decimal", placeholder: "Target price (optional)", "aria-label": "Target price" });
  const picker = kindPicker(keys, null);
  const error = h("div", { class: "error" });
  const addButton = h("button", { class: "primary", type: "submit" }, "Add to watchlist");
  const addForm = h("form", {
    class: "grid",
    onsubmit: async (event) => {
      event.preventDefault();
      error.textContent = "";
      if (!query.value.trim()) { error.textContent = "Enter a SKU, product name, or store link."; return; }
      let cents;
      try { cents = dollarsToCents(target.value); } catch (err) { error.textContent = err.message; return; }
      addButton.disabled = true;
      try {
        const result = await api("/api/watchlist", { method: "POST", body: { query: query.value.trim(), region: region.value || null, max_price_cents: cents, kinds: picker.value() } });
        toast(`Watching ${result.listings} listing(s)${result.created < result.listings ? " (some updated)" : ""}.`);
        route();
      } catch (err) {
        if (err instanceof AuthError) return loginView("Your session ended. Log in again.");
        error.textContent = err.message;
      } finally { addButton.disabled = false; }
    },
  }, h("div", { class: "row" }, query, region, target, addButton), h("details", {}, h("summary", {}, "Alert types"), picker.el), error);

  const subscribe = h("input", { type: "checkbox" });
  subscribe.checked = data.subscribed;
  subscribe.addEventListener("change", () => mutate("/api/subscription", "POST", { subscribed: subscribe.checked }, subscribe.checked ? "You'll be pinged for every important alert." : "Stopped pinging you for every alert."));

  // Bulk bar
  const count = h("span", { class: "muted" }, "0 selected");
  const bulk = (action, hours) => async () => {
    if (!selected.size) return toast("Select some watches first.", "bad");
    if (action === "remove" && !confirm(`Remove ${selected.size} watch(es)?`)) return;
    await mutate("/api/watches/bulk", "POST", { ids: [...selected], action, hours }, (r) => `${r.count} watch(es) ${action === "remove" ? "removed" : action === "mute" ? "muted" : "unmuted"}.`);
  };
  const bulkBar = h("div", { class: "row bulkbar" }, count,
    h("button", { onclick: bulk("mute", 24) }, "Mute 24h"),
    h("button", { onclick: bulk("mute", 168) }, "Mute 7 days"),
    h("button", { onclick: bulk("unmute") }, "Unmute"),
    h("button", { class: "danger", onclick: bulk("remove") }, "Remove"));
  const tick = (id, box) => { if (box.checked) selected.add(id); else selected.delete(id); count.textContent = `${selected.size} selected`; };

  const editRow = (w, tr) => {
    const price = h("input", { type: "text", inputmode: "decimal", value: w.max_price_cents ? (w.max_price_cents / 100).toFixed(2) : "", placeholder: "No target", "aria-label": "Target price" });
    const kinds = kindPicker(keys, w.kinds);
    const err = h("div", { class: "error" });
    const editor = h("tr", { class: "editor" }, h("td", { colspan: 7 },
      h("div", { class: "grid" },
        h("label", {}, "Only ping at or below ", price),
        kinds.el,
        err,
        h("div", { class: "row" },
          h("button", { class: "primary", onclick: async () => {
            let cents;
            try { cents = dollarsToCents(price.value); } catch (e) { err.textContent = e.message; return; }
            await mutate(`/api/watches/${w.watch_id}`, "PATCH", { max_price_cents: cents, kinds: kinds.value() }, "Watch updated.");
          } }, "Save"),
          h("button", { onclick: () => editor.remove() }, "Cancel")))));
    tr.after(editor);
  };

  // Test: what this watch alerts you about, and a sample alert sent to Discord.
  const testRow = async (w, tr) => {
    const next = tr.nextElementSibling;
    if (next && next.classList.contains("editor")) next.remove();
    const cell = h("td", { colspan: 7 }, h("div", { class: "muted" }, "Checking…"));
    const editor = h("tr", { class: "editor" }, cell);
    tr.after(editor);
    const show = async (kind, send) => {
      let r;
      try { r = await api(`/api/watches/${w.watch_id}/test`, { method: "POST", body: { kind, send } }); } catch (error) { editor.remove(); return handleError(error); }
      const pick = h("select", { "aria-label": "Alert type to send" }, r.coverage.map((c) => h("option", { value: c.kind }, c.label)));
      pick.value = r.selected.kind;
      const sendButton = h("button", { class: "primary", disabled: !r.can_send, title: r.can_send ? "Posts where the real alert would go, pinging only you" : "Discord isn't connected" }, "Send test to Discord");
      sendButton.addEventListener("click", () => { sendButton.disabled = true; show(pick.value, true); });
      pick.addEventListener("change", () => show(pick.value, false));
      let result = null;
      if (r.sent) {
        result = r.sent.ok
          ? h("div", { class: "pill good" }, "Sent. ", r.sent.link ? h("a", { href: r.sent.link, target: "_blank", rel: "noopener noreferrer" }, "Open in Discord") : "Check Discord.")
          : h("div", { class: "error" }, `Not sent: ${r.sent.error}`);
      }
      const rows = r.coverage.map((c) => h("tr", {},
        h("td", {}, c.label),
        h("td", {}, c.pings ? h("span", { class: "pill good" }, "Pings you") : c.posted ? h("span", { class: "pill neutral" }, "Posts, no ping") : h("span", { class: "pill bad" }, "Not posted")),
        h("td", {}, c.channel || "—", c.sms ? h("span", { class: "pill info", title: "SMS isn't sent for tests" }, " + SMS") : null),
        h("td", { class: "muted" }, c.reason)));
      cell.replaceChildren(h("div", { class: "grid" },
        h("div", {}, h("strong", {}, `What ${r.sku} ${r.region.toUpperCase()} alerts you about`), r.server ? h("span", { class: "muted" }, ` · in ${r.server}`) : null),
        h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Alert", "Result", "Where", "Why"].map((label) => h("th", {}, label)))), h("tbody", {}, rows))),
        h("div", { class: "row" }, pick, sendButton, h("button", { onclick: () => editor.remove() }, "Close")),
        result,
        h("div", { class: "muted" }, "Rules and \"ping me for every important alert\" can also ping you; this checks the watch on its own. Tests never ping roles or other people.")));
    };
    await show(null, false);
  };

  const prioritySelect = (w) => {
    const select = h("select", { "aria-label": "Check priority" }, ["high", "normal", "low"].map((p) => h("option", { value: p }, p)));
    select.value = w.listing.priority;
    select.addEventListener("change", () => mutate(`/api/listings/${w.listing.id}/priority`, "POST", { priority: select.value }, `Priority set to ${select.value}.`));
    return select;
  };

  const myTable = mine.length ? h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ["", "Product", "Store", "Price", "Alerts", "Priority", ""].map((label, i) => h("th", { class: i === 3 ? "num" : "" }, label)))),
    h("tbody", {}, mine.map((w) => {
      const box = h("input", { type: "checkbox", "aria-label": `Select ${w.listing.sku}` });
      box.addEventListener("change", () => tick(w.watch_id, box));
      const muted = mutedText(w.muted_until);
      const tr = h("tr", {},
        h("td", {}, box),
        h("td", {}, productLink(w.listing.variant_id, w.listing.sku), h("div", { class: "muted" }, w.listing.name),
          w.flip_net_cents !== null ? h("div", {}, "Flip ", signedMoney(w.flip_net_cents)) : null),
        h("td", {}, h("div", { class: "row" }, `${w.listing.source === "unifi" ? "" : `${w.listing.source} `}${w.listing.region.toUpperCase()}`, statusPill(w.listing.status, w.listing.delisted), muted)),
        h("td", { class: "num" }, money(w.listing.price_cents, w.listing.currency),
          w.max_price_cents ? h("div", { class: "muted" }, `ping ≤ ${money(w.max_price_cents)}`) : null),
        h("td", {}, kindsText(w.kinds, keys)),
        h("td", {}, prioritySelect(w)),
        h("td", {}, h("div", { class: "row actions" },
          h("button", { onclick: (e) => editRow(w, e.target.closest("tr")) }, "Edit"),
          h("button", { title: "See what this watch alerts you about, and send a sample alert", onclick: (e) => testRow(w, e.target.closest("tr")) }, "Test"),
          muted ? h("button", { onclick: () => mutate(`/api/watches/${w.watch_id}`, "PATCH", { mute_hours: 0 }, "Unmuted.") }, "Unmute")
            : h("button", { title: "No pings for 24 hours", onclick: () => mutate(`/api/watches/${w.watch_id}`, "PATCH", { mute_hours: 24 }, "Muted for 24 hours.") }, "Mute"),
          h("button", { class: "danger", onclick: () => confirm(`Stop watching ${w.listing.sku} ${w.listing.region.toUpperCase()}?`) && mutate(`/api/watches/${w.watch_id}`, "DELETE", undefined, "Removed.") }, "Remove"))));
      return tr;
    })))) : h("div", { class: "empty" }, "You aren't watching anything yet. Add a product above, or create a rule below.");

  // Rules
  const rulesCard = await rulesSection(data, keys);

  const memberTable = table([
    { label: "Product", cell: (w) => h("div", {}, productLink(w.listing.variant_id, w.listing.sku), h("div", { class: "muted" }, w.listing.name)) },
    { label: "Store", cell: (w) => w.listing.region.toUpperCase() },
    { label: "Member", cell: (w) => h("span", { class: "mono" }, w.user_id) },
    { label: "Target", num: true, cell: (w) => (w.max_price_cents ? `≤ ${money(w.max_price_cents)}` : "—") },
    { label: "Added", cell: (w) => when(w.created_at) },
    { label: "", cell: (w) => h("button", { class: "danger", onclick: () => confirm("Remove this member's watch?") && mutate(`/api/watches/${w.watch_id}`, "DELETE", undefined, "Removed.") }, "Remove") },
  ], members, "No members are watching anything.");

  const content = h("div", { class: "grid" },
    data.owner_configured ? null : h("div", { class: "card note-warn" }, "Set DISCORD_OWNER_IDS in .env so watches made here can ping your Discord account."),
    h("div", { class: "card" }, h("h2", {}, "Add to watchlist"),
      h("p", { class: "muted" }, "Paste a UniFi, Amazon, Best Buy, B&H, or Newegg link, or type a SKU or name. You'll be pinged in your home Discord server."),
      addForm,
      h("label", { class: "check spaced" }, subscribe, `Ping me for every important alert (back in stock, Early Access, new, deals) · ${data.subscribers} subscribed in total`)),
    h("div", { class: "card" }, h("div", { class: "row spread" }, h("h2", {}, `My watches (${mine.length})`), mine.length ? bulkBar : null), myTable),
    rulesCard,
    h("div", { class: "card" }, h("h2", {}, `Members' watches (${members.length})`), memberTable));
  shell(content, "Watchlist", "What you're tracking, how you're alerted, and standing rules");
}

async function rulesSection(data, keys) {
  const regionsBoxes = data.regions.map((r) => { const b = h("input", { type: "checkbox", value: r }); return [b, h("label", { class: "check" }, b, r.toUpperCase())]; });
  const name = h("input", { type: "text", placeholder: "Rule name, e.g. Cheap cameras", "aria-label": "Rule name", maxlength: 80 });
  const text = h("input", { type: "text", placeholder: "Text in SKU or name (e.g. G6, Pro Max)", "aria-label": "Text to match", maxlength: 80 });
  const category = h("select", { "aria-label": "Category" }, h("option", { value: "" }, "Any category"));
  api("/api/categories").then((c) => {
    category.append(
      h("optgroup", { label: "Families" }, c.groups.map((g) => h("option", { value: g.value }, g.label))),
      h("optgroup", { label: "Categories" }, c.categories.map((x) => h("option", { value: x.value }, `${x.label} (${x.count})`))));
  }).catch(handleError);
  const maxPrice = h("input", { type: "text", inputmode: "decimal", placeholder: "Max price (optional)", "aria-label": "Max price" });
  const minProfit = h("input", { type: "text", inputmode: "decimal", placeholder: "Min credible profit (optional)", "aria-label": "Minimum credible profit" });
  const kinds = kindPicker(keys, null, { allowDefaults: false, defaults: data.rule_default_kinds });
  const preview = h("div", { class: "muted" }, "Fill in what to match to see what it covers.");
  const error = h("div", { class: "error" });
  let timer = null;
  const refreshPreview = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const regions = regionsBoxes.filter(([b]) => b.checked).map(([b]) => b.value).join(",");
      if (!text.value.trim() && !category.value.trim() && !regions) { preview.textContent = "Fill in what to match to see what it covers."; return; }
      try {
        const r = await api(`/api/rules/preview?query=${encodeURIComponent(text.value.trim())}&category=${encodeURIComponent(category.value.trim())}&regions=${encodeURIComponent(regions)}`);
        preview.replaceChildren(`Covers ${r.count} product(s) today, plus any new ones that match.`, r.sample.length ? h("div", { class: "mono" }, r.sample.join(" · ")) : null);
      } catch (e) { handleError(e); }
    }, 300);
  };
  text.addEventListener("input", refreshPreview);
  category.addEventListener("change", refreshPreview);
  regionsBoxes.forEach(([b]) => b.addEventListener("change", refreshPreview));

  const form = h("form", {
    class: "grid",
    onsubmit: async (event) => {
      event.preventDefault();
      error.textContent = "";
      let max, min;
      try { max = dollarsToCents(maxPrice.value); } catch (e) { error.textContent = e.message; return; }
      try { min = minProfit.value.trim() ? Math.round(Number(minProfit.value.replace(/[$,\s]/g, "")) * 100) : null; } catch { min = null; }
      if (min !== null && !(min >= 0)) { error.textContent = "Minimum profit must be a number."; return; }
      const chosen = kinds.value();
      if (!chosen.length) { error.textContent = "Pick at least one alert type."; return; }
      const body = { name: name.value.trim() || text.value.trim() || category.value.trim() || "Rule", query: text.value.trim() || null, category: category.value.trim() || null, regions: regionsBoxes.filter(([b]) => b.checked).map(([b]) => b.value), kinds: chosen, max_price_cents: max, min_profit_cents: min };
      try {
        await api("/api/rules", { method: "POST", body });
        toast(`Rule "${body.name}" created.`);
        route();
      } catch (e) {
        if (e instanceof AuthError) return loginView("Your session ended. Log in again.");
        error.textContent = e.message;
      }
    },
  },
  h("div", { class: "row" }, name, text, category),
  h("div", { class: "row" }, h("span", { class: "muted" }, "Regions:"), regionsBoxes.map(([, l]) => l), h("span", { class: "muted" }, "(none = any)")),
  h("div", { class: "row" }, maxPrice, minProfit),
  kinds.el, preview, error,
  h("div", {}, h("button", { class: "primary", type: "submit" }, "Create rule")));

  const describe = (r) => [
    r.query ? `text "${r.query}"` : null,
    r.category ? `category "${r.category}"` : null,
    r.regions.length ? r.regions.join("/").toUpperCase() : "any region",
    r.max_price_cents ? `≤ ${money(r.max_price_cents)}` : null,
    r.min_profit_cents !== null ? `credible profit ≥ ${money(r.min_profit_cents)}` : null,
  ].filter(Boolean).join(" · ");

  const list = data.rules.length ? h("div", { class: "grid" }, data.rules.map((r) => {
    const enabled = h("input", { type: "checkbox" });
    enabled.checked = r.enabled;
    enabled.addEventListener("change", () => mutate(`/api/rules/${r.id}`, "PATCH", { enabled: enabled.checked }, enabled.checked ? "Rule on." : "Rule off."));
    const muted = mutedText(r.muted_until);
    return h("div", { class: "rule" },
      h("div", { class: "row spread" },
        h("div", {}, h("strong", {}, r.name), r.owner ? null : h("span", { class: "muted mono" }, ` · ${r.user_id}`)),
        h("div", { class: "row" },
          muted,
          h("label", { class: "check" }, enabled, "On"),
          muted ? h("button", { onclick: () => mutate(`/api/rules/${r.id}`, "PATCH", { mute_hours: 0 }, "Unmuted.") }, "Unmute")
            : h("button", { onclick: () => mutate(`/api/rules/${r.id}`, "PATCH", { mute_hours: 24 }, "Muted for 24 hours.") }, "Mute 24h"),
          h("button", { class: "danger", onclick: () => confirm(`Delete rule "${r.name}"?`) && mutate(`/api/rules/${r.id}`, "DELETE", undefined, "Rule deleted.") }, "Delete"))),
      h("div", { class: "muted" }, describe(r)),
      kindsText(r.kinds, keys));
  })) : h("div", { class: "empty" }, "No rules yet. A rule pings you for anything that matches, including products that don't exist yet.");

  return h("div", { class: "card" }, h("h2", {}, `Rules (${data.rules.length})`), list,
    h("details", { class: "spaced" }, h("summary", {}, "New rule"), form));
}

// ── discovery ────────────────────────────────────────────────────────────

const DISCOVERY_TABS = [["promising", "Promising"], ["new", "New"], ["checked", "Checked"], ["tracked", "Tracked"], ["ignored", "Ignored"], ["dropped", "Dropped"], ["all", "All"]];
const SIGNAL_KINDS = { manual: "You checked it", trending: "Trending", most_viewed: "Most viewed", sold_out: "Sold out", big_discount: "Big discount" };
const STATUS_STYLE = { promising: "good", tracked: "info", new: "neutral", checked: "neutral", ignored: "neutral", dropped: "warn" };

function availabilityPill(available) {
  if (available === true) return h("span", { class: "pill good" }, "buyable");
  if (available === false) return h("span", { class: "pill bad" }, "sold out");
  return null;
}

function buyCell(c) {
  if (!c.retail_cents) return h("span", { class: "muted" }, "no store price");
  const sale = c.regular_cents && c.regular_cents > c.retail_cents;
  return h("div", {},
    h("div", { class: "row" }, money(c.retail_cents), availabilityPill(c.available)),
    sale ? h("div", { class: "muted" }, `reg. ${money(c.regular_cents)} (−${pct(1 - c.retail_cents / c.regular_cents)})`) : null,
    c.retail_url ? h("a", { href: c.retail_url, target: "_blank", rel: "noopener noreferrer" }, `${c.retail_source || "store"} ↗`) : null);
}

function ebayCell(c) {
  if (!c.checked_at) return h("span", { class: "muted" }, "not checked yet");
  if (!c.ebay_sellers) return h("span", { class: "muted" }, "no matching listings");
  return h("div", {}, `${c.ebay_sellers} seller(s)`, h("div", { class: "muted" }, `floor ${money(c.ebay_p25_cents)} · median ${money(c.ebay_median_cents)}`));
}

function estimateCell(c) {
  if (c.net_cents === null) return h("span", { class: "muted" }, c.checked_at && !c.retail_cents ? "needs a store price" : "—");
  return h("div", {}, signedMoney(c.net_cents), ` (${pct(c.roi)})`,
    h("div", { class: "row" }, confidencePill(c.confidence), c.capped ? h("span", { class: "pill warn", title: "Buyable at regular price, so eBay asking prices are capped" }, "capped") : null));
}

async function decideCandidate(c, action) {
  if (action === "ignore" && !confirm(`Ignore "${c.title}"? It won't come back even if it trends again.`)) return;
  try {
    const r = await api(`/api/discovery/${c.id}/decision`, { method: "POST", body: { action } });
    toast(action === "track" ? `Tracking.${r.watched ? ` Added ${r.watched} listing(s) to your watchlist.` : ""}${r.note ? ` (${r.note})` : ""}` : action === "ignore" ? "Ignored." : "Restored.");
    route();
  } catch (error) { handleError(error); }
}

async function recheckCandidate(c) {
  try {
    await api(`/api/discovery/${c.id}/recheck`, { method: "POST" });
    toast("Rechecked on eBay.");
    route();
  } catch (error) { handleError(error); }
}

function candidateActions(c) {
  return h("div", { class: "row actions" },
    ["new", "checked", "promising", "dropped"].includes(c.status) ? h("button", { class: "primary", onclick: () => decideCandidate(c, "track") }, "Track") : null,
    c.status !== "ignored" ? h("button", { onclick: () => decideCandidate(c, "ignore") }, "Ignore") : h("button", { onclick: () => decideCandidate(c, "restore") }, "Restore"),
    c.status !== "ignored" ? h("button", { title: "Read eBay again now (uses 1 call)", onclick: () => recheckCandidate(c) }, "Recheck") : null);
}

async function discoveryPage(params) {
  const status = DISCOVERY_TABS.some(([k]) => k === params.get("status")) ? params.get("status") : "promising";
  const data = await api(`/api/discovery?status=${status}`);
  if (!data.enabled) {
    shell(h("div", { class: "card empty" }, "Discovery is off. Set DISCOVERY_ENABLED=true in .env and restart."), "Discovery");
    return;
  }
  const input = h("input", { type: "text", class: "grow", placeholder: "Product name, model, or an Amazon / Best Buy / B&H / Newegg link", "aria-label": "Item to check" });
  const error = h("div", { class: "error" });
  const button = h("button", { class: "primary", type: "submit" }, "Check on eBay");
  const form = h("form", {
    class: "grid",
    onsubmit: async (event) => {
      event.preventDefault();
      error.textContent = "";
      if (input.value.trim().length < 2) { error.textContent = "Type a product name or paste a link."; return; }
      button.disabled = true;
      button.textContent = "Checking…";
      try {
        const r = await api("/api/discovery/check", { method: "POST", body: { text: input.value.trim() } });
        if (r.message) toast(r.message);
        location.hash = `#/discovery/${r.id}`;
      } catch (err) {
        if (err instanceof AuthError) return loginView("Your session ended. Log in again.");
        error.textContent = err.message;
      } finally { button.disabled = false; button.textContent = "Check on eBay"; }
    },
  }, h("div", { class: "row" }, input, button), error);

  const t = data.thresholds;
  const sourceLine = h("div", { class: "row muted" },
    "Sources:", h("span", { class: `pill ${data.sources.ebay ? "good" : "bad"}` }, data.sources.ebay ? "eBay ready" : "eBay not configured"),
    h("span", { class: `pill ${data.sources.bestbuy ? "good" : "neutral"}` }, data.sources.bestbuy ? `Best Buy on${data.sources.bestbuy_categories ? ` · ${data.sources.bestbuy_categories} sold-out categories` : ""}` : "Best Buy off (add BESTBUY_API_KEY)"),
    h("span", {}, `eBay budget today: ${data.budget.used}/${data.budget.limit}`));

  const tabs = h("div", { class: "tabs" }, DISCOVERY_TABS.map(([key, label]) =>
    h("button", { class: key === status ? "on" : "", onclick: () => { location.hash = `#/discovery?status=${key}`; } }, key === "all" ? label : `${label} (${data.counts[key] || 0})`)));

  const empty = {
    promising: `Nothing promising yet. An item becomes promising at ${money(t.min_profit_cents)}+ estimated net, ${pct(t.min_roi)}+ ROI, and ${t.min_sellers}+ eBay sellers, then it's rechecked daily for ${t.trial_days} days.`,
    new: "No new candidates. They arrive from Best Buy (once a key is added) or when you check an item above.",
  }[status] || "Nothing here.";

  const rows = table([
    { label: "Item", cell: (c) => h("div", {}, h("a", { href: `#/discovery/${c.id}` }, c.title), h("div", { class: "muted" }, [c.model, c.category].filter(Boolean).join(" · ")), h("div", { class: "row" }, h("span", { class: `pill ${STATUS_STYLE[c.status] || "neutral"}` }, c.status), h("span", { class: "muted" }, `heat ${c.heat}`))) },
    { label: "Buy", cell: buyCell },
    { label: "eBay (asking)", cell: ebayCell },
    { label: "Est. net", cell: estimateCell },
    { label: "", cell: candidateActions },
  ], data.items, empty);

  shell(h("div", { class: "grid" },
    h("div", { class: "card" }, h("h2", {}, "Check an item"),
      h("p", { class: "muted" }, "FlipScout reads its eBay market now. If you paste a store link, it also uses the store's price and stock to estimate profit honestly."),
      form, sourceLine),
    h("div", {}, tabs, h("div", { class: "card" }, rows))),
  "Discovery", "Hot products beyond your watchlist, with an honest eBay read");
}

async function candidatePage(id) {
  const c = await api(`/api/discovery/${encodeURIComponent(id)}`);
  const notes = [];
  if (c.capped) notes.push(h("div", { class: "note warn" }, `Capped: it can still be bought at ${money(c.regular_cents || c.retail_cents)}, so eBay asking prices above that plus tax aren't real resale value.`));
  if (c.available === false) notes.push(h("div", { class: "note" }, "Sold out at the store, so eBay asking prices are the live market. A restock is the buying chance."));
  if (c.regular_cents && c.retail_cents && c.regular_cents > c.retail_cents) notes.push(h("div", { class: "note" }, `On sale: ${money(c.retail_cents)} vs ${money(c.regular_cents)} regular. Resale is judged against the regular price.`));
  if (c.net_cents === null && c.checked_at) notes.push(h("div", { class: "note" }, c.retail_cents ? "Not enough matching eBay listings to estimate." : "No store price yet: paste a store link in Check an item to estimate profit."));
  if (c.net_cents !== null) notes.push(h("div", { class: `note ${c.net_cents > 0 ? "good" : ""}` }, "Estimate: ", signedMoney(c.net_cents), ` (${pct(c.roi)} ROI), ${c.confidence} confidence. It's based on eBay asking prices from ${c.ebay_sellers} seller(s), not sold prices.`));
  if (c.status === "promising" && c.trial_until) notes.push(h("div", { class: "note" }, "On trial until ", when(c.trial_until), ": rechecked daily, kept only if the profit holds."));

  const hist = c.history;
  const labels = hist.map((x) => x.day);
  shell(h("div", { class: "grid" },
    h("div", { class: "row" }, h("span", { class: `pill ${STATUS_STYLE[c.status] || "neutral"}` }, c.status), candidateActions(c)),
    h("div", { class: "grid cols-2" },
      h("div", { class: "card" }, h("h2", {}, "Verdict"), notes.length ? h("div", { class: "verdict" }, notes) : h("div", { class: "empty" }, "Not checked yet.")),
      h("div", { class: "card" }, h("h2", {}, "Details"),
        h("dl", { class: "kv" },
          h("dt", {}, "Model"), h("dd", {}, c.model || "—"),
          h("dt", {}, "UPC"), h("dd", {}, c.upc || "—"),
          h("dt", {}, "Buy"), h("dd", {}, buyCell(c)),
          h("dt", {}, "eBay search"), h("dd", {}, c.ebay_query || "—"),
          h("dt", {}, "eBay"), h("dd", {}, ebayCell(c)),
          h("dt", {}, "Heat"), h("dd", {}, `${c.heat} from ${c.signal_count} signal(s)`),
          h("dt", {}, "First seen"), h("dd", {}, when(c.first_seen_at)),
          h("dt", {}, "Checked"), h("dd", {}, when(c.checked_at)),
          h("dt", {}, "Next check"), h("dd", {}, c.next_check_at ? when(c.next_check_at) : "—")))),
    h("div", { class: "grid cols-2" },
      h("div", { class: "card" }, h("h2", {}, "eBay prices (daily)"),
        lineChart([
          { label: "Floor (25th pct)", values: hist.map((x) => x.p25_cents) },
          { label: "Median ask", values: hist.map((x) => x.median_cents) },
          { label: "Store price", values: hist.map((x) => x.retail_cents) },
        ], { format: (v) => money(v).replace(/\.\d\d$/, ""), labels })),
      h("div", { class: "card" }, h("h2", {}, "eBay sellers (daily)"),
        lineChart([{ label: "Distinct sellers", values: hist.map((x) => x.sellers) }], { format: (n) => String(Math.round(n)), labels }))),
    h("div", { class: "card" }, h("h2", {}, "Signals"),
      table([
        { label: "Source", cell: (s) => s.source },
        { label: "Signal", cell: (s) => SIGNAL_KINDS[s.kind] || s.kind },
        { label: "Rank", num: true, cell: (s) => s.rank ?? "—" },
        { label: "Price", num: true, cell: (s) => money(s.price_cents) },
        { label: "Seen", num: true, cell: (s) => `${s.times_seen}×` },
        { label: "Last", cell: (s) => when(s.seen_at) },
        { label: "", cell: (s) => (s.url ? h("a", { href: s.url, target: "_blank", rel: "noopener noreferrer" }, "Open ↗") : "") },
      ], c.signals))),
  c.title, [c.brand, c.category].filter(Boolean).join(" · "));
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
    else if (parts[0] === "watchlist") await watchlistPage();
    else if (parts[0] === "discovery" && parts[1]) await candidatePage(decodeURIComponent(parts[1]));
    else if (parts[0] === "discovery") await discoveryPage(params);
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
