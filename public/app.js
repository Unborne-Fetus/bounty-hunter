const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("en-US");
const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });

function relativeDate(value) {
  if (!value) return "Unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  const delta = date.getTime() - Date.now();
  const absMinutes = Math.round(Math.abs(delta) / 60000);
  if (absMinutes < 1) return "just now";
  if (absMinutes < 60) return `${absMinutes}m ${delta < 0 ? "ago" : "from now"}`;
  const hours = Math.round(absMinutes / 60);
  if (hours < 48) return `${hours}h ${delta < 0 ? "ago" : "from now"}`;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function text(id, value) { $(id).textContent = value; }
function escapeText(value) { const node = document.createElement("span"); node.textContent = value ?? ""; return node.innerHTML; }

function renderReasons(reasons) {
  const root = $("rejection-reasons");
  root.replaceChildren();
  const max = Math.max(...reasons.map((item) => item.count), 1);
  reasons.slice(0, 8).forEach((item) => {
    const row = document.createElement("div");
    row.className = "reason-row";
    row.innerHTML = `<div><div class="reason-copy" title="${escapeText(item.reason)}">${escapeText(item.reason)}</div><div class="reason-bar"><i style="width:${Math.max(3, item.count / max * 100)}%"></i></div></div><div class="reason-count">${number.format(item.count)}</div>`;
    root.append(row);
  });
}

function sourceDetail(item) {
  if (item.available_reported != null) return `${item.opportunities} of ${item.available_reported} reported`;
  if (item.organizations_added != null) return `${item.organizations_added} organizations added`;
  if (item.attempted != null) return `${item.attempted} attempted · ${item.requests ?? 0} requests`;
  return item.fallback_reason ? item.fallback_reason.replaceAll("_", " ") : "—";
}

function renderSources(data) {
  const counts = data.current_scan.source_status_counts || {};
  $("source-summary").innerHTML = Object.entries(counts).map(([status, count]) => `<span class="source-chip"><strong>${number.format(count)}</strong>${escapeText(status)}</span>`).join("");
  $("source-table").innerHTML = data.source_health.map((item) => `<tr><td>${escapeText(item.name)}</td><td><span class="health ${escapeText(item.status)}">${escapeText(item.status)}</span></td><td>${number.format(item.opportunities)} opportunities</td><td>${escapeText(sourceDetail(item))}</td></tr>`).join("");
}

function renderOpportunities(items) {
  $("opportunity-grid").innerHTML = items.slice(0, 6).map((item) => {
    const link = item.issue_url || item.source_url || "#";
    const firstBlocker = item.rejection_reasons[0] || item.warnings[0] || "Insufficient public evidence";
    return `<article class="opportunity-card"><div class="op-meta"><span>${escapeText(item.source)}</span><span class="bounty">${usd.format(item.bounty_usd)}</span></div><h3>${escapeText(item.title)}</h3><span class="repo">${escapeText(item.repository || "Repository unknown")}</span><div class="economics"><div><span>Expected value</span><strong class="${item.expected_value_usd < 0 ? "negative" : ""}">${usd.format(item.expected_value_usd)}</strong></div><div><span>Hourly return</span><strong class="${item.expected_hourly_return_usd < 0 ? "negative" : ""}">${usd.format(item.expected_hourly_return_usd)}</strong></div></div><p class="blocker"><strong>${item.gate_rejection_count} gate blocker${item.gate_rejection_count === 1 ? "" : "s"}.</strong> ${escapeText(firstBlocker)}.</p><a class="op-link" href="${escapeText(link)}" target="_blank" rel="noreferrer">View public evidence ↗</a></article>`;
  }).join("");
}

function render(data) {
  const healthy = data.monitor.healthy;
  const attention = data.monitor.attention_required;
  const pill = $("monitor-pill");
  pill.className = `status-pill ${healthy && !attention ? "" : attention ? "warning" : "error"}`;
  pill.textContent = healthy && !attention ? "Healthy" : attention ? "Needs attention" : "Degraded";
  text("safety-message", data.safety.message);
  text("last-scan", relativeDate(data.monitor.finished_at));
  text("next-scan", relativeDate(data.monitor.next_scan_at));
  text("failure-count", number.format(data.monitor.consecutive_failures));
  text("metric-scans", number.format(data.lifetime.scans));
  text("metric-observed", number.format(data.lifetime.opportunity_observations));
  text("metric-distinct", number.format(data.lifetime.distinct_opportunities));
  text("metric-eligible", number.format(data.current_scan.eligible));
  text("metric-evaluated", `of ${number.format(data.current_scan.evaluated)} evaluated`);
  renderReasons(data.rejection_reasons);
  renderSources(data);
  renderOpportunities(data.opportunities);
  const terminal = data.pilot.terminal_attempts;
  const minimum = data.pilot.minimum_terminal_attempts;
  text("pilot-progress", `${terminal} / ${minimum}`);
  $("pilot-bar").style.width = `${Math.min(100, terminal / Math.max(minimum, 1) * 100)}%`;
  text("pilot-attempts", number.format(data.lifetime.attempts));
  text("pilot-wins", number.format(data.lifetime.wins));
  text("pilot-net", usd.format(data.lifetime.net_income_usd));
  text("snapshot-time", `Snapshot ${relativeDate(data.snapshot_at)}`);
}

fetch("/telemetry.json", { cache: "no-store" })
  .then((response) => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); })
  .then(render)
  .catch(() => { $("error-banner").hidden = false; const pill = $("monitor-pill"); pill.className = "status-pill error"; pill.textContent = "Unavailable"; });
