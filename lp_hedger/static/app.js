/* LP Hedger UI: plain JS, no build step. Talks to the local FastAPI server. */
const $ = (s) => document.querySelector(s);
const api = async (path, body, method) => {
  const r = await fetch('/api/' + path, {
    method: method || (body ? 'POST' : 'GET'),
    headers: { 'content-type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
};
const fmtUsd = (v) => v == null ? '–' : (v < 0 ? '-' : '') + '$' + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: v > 1000 ? 0 : 2 });
const fmtK = (v) => (v < 0 ? '-' : '') + '$' + (Math.abs(v) >= 1000 ? (Math.abs(v) / 1000).toFixed(1) + 'k' : Math.abs(v).toFixed(0));
const fmt = (v, d = 4) => v == null ? '–' : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });
const short = (a) => a ? a.slice(0, 6) + '…' + a.slice(-4) : '';

let presets = [];
let cfg = null;

/* ---------------- wallet gate ---------------- */
async function refreshGate(st) {
  const gate = $('#gate'), app = $('#app');
  $('.header-right').classList.toggle('hidden', !st.wallet_unlocked);
  if (st.wallet_unlocked) { gate.classList.add('hidden'); app.classList.remove('hidden'); return; }
  gate.classList.remove('hidden'); app.classList.add('hidden');
  const exists = st.wallet_exists;
  $('#gate-title').textContent = exists ? 'Unlock your hot wallet' : 'Create your hot wallet';
  $('#gate-text').textContent = exists ? `Wallet ${st.wallet_address || ''} is locked. Enter the password to continue.` :
    'This creates a new local hot wallet. Fund it with a little ETH for gas plus the ETH and USDC you want to deploy.';
  $('#gate-submit').textContent = exists ? 'Unlock' : 'Create wallet';
  $('#gate-import-wrap').classList.toggle('hidden', exists);
  $('#gate-password').autocomplete = exists ? 'current-password' : 'new-password';
}
$('#gate-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const password = $('#gate-password').value;
  const pk = $('#gate-import').value.trim();
  try {
    const st = await api('status');
    if (st.wallet_exists) await api('wallet/unlock', { password });
    else await api('wallet/create', { password, private_key: pk || null });
    $('#gate-password').value = ''; $('#gate-import').value = '';
    await poll();
  } catch (err) { alert(err.message); }
});

/* ---------------- settings ---------------- */
function fillChainSelects() {
  const cs = $('#chain-select');
  cs.innerHTML = presets.map(p => `<option value="${p.key}">${p.name}</option>`).join('');
  cs.value = cfg.chain.chain;
  fillPools();
}
function fillPools() {
  const chain = presets.find(p => p.key === $('#chain-select').value) || presets[0];
  const ps = $('#pool-select');
  ps.innerHTML = chain.pools.map(p => `<option value="${p.name}">${p.name}</option>`).join('');
  if (chain.pools.some(p => p.name === cfg.chain.pool)) ps.value = cfg.chain.pool;
  $('[name="chain.rpc_url"]').placeholder = 'leave empty for ' + chain.default_rpc;
}
$('#chain-select').addEventListener('change', fillPools);

function loadSettingsForm() {
  const f = $('#settings');
  for (const el of f.elements) {
    if (!el.name) continue;
    const [sec, key] = el.name.split('.');
    const v = cfg[sec]?.[key];
    if (el.type === 'checkbox') el.checked = !!v; else if (el.tagName === 'SELECT') { /* set after options */ el.value = v; } else el.value = v ?? '';
  }
  fillChainSelects();
  $('[name="derive.environment"]').value = cfg.derive.environment;
  $('[name="hedge.mode"]').value = cfg.hedge.mode;
}
$('#settings').addEventListener('submit', async (e) => {
  e.preventDefault();
  const patch = {};
  for (const el of e.target.elements) {
    if (!el.name) continue;
    const [sec, key] = el.name.split('.');
    patch[sec] = patch[sec] || {};
    patch[sec][key] = el.type === 'checkbox' ? el.checked : el.value;
  }
  try {
    cfg = (await api('config', patch, 'PUT')).config;
    $('#settings-msg').textContent = 'Saved ✓'; setTimeout(() => $('#settings-msg').textContent = '', 2500);
    poll();
  } catch (err) { $('#settings-msg').textContent = err.message; }
});
$('#btn-verify-derive').addEventListener('click', async () => {
  const out = $('#derive-verify'); out.textContent = 'Checking…';
  try {
    const f = $('#settings');
    await api('config', { derive: { environment: f['derive.environment'].value, derive_wallet: f['derive.derive_wallet'].value, subaccount_id: f['derive.subaccount_id'].value } }, 'PUT');
    const r = await api('derive/verify', {});
    out.innerHTML = `<span class="good">Connected.</span> Collateral ${fmtUsd(r.collateral_usd)} · margin ${r.margin_type || ''} · subaccounts ${r.subaccount_ids.join(', ')}`;
  } catch (err) { out.innerHTML = `<span class="bad">${err.message}</span>`; }
});

/* ---------------- actions ---------------- */
document.querySelectorAll('[data-job]').forEach(b => b.addEventListener('click', async () => {
  const job = b.dataset.job;
  if (b.classList.contains('danger') && !confirm(`Really ${b.textContent.toLowerCase()}?`)) return;
  try { await api('jobs/' + job, {}); $('#action-note').textContent = `Queued "${b.textContent}". It runs on the next engine tick.`; poll(); }
  catch (err) { alert(err.message); }
}));
$('#btn-toggle-engine').addEventListener('click', async () => {
  try {
    const st = await api('status');
    await api(st.running ? 'engine/stop' : 'engine/start', {});
    poll();
  } catch (err) { alert(err.message); }
});

/* ---------------- render ---------------- */
function render(st) {
  $('#mode-badge').textContent = st.dry_run ? 'DRY RUN' : 'LIVE';
  $('#mode-badge').className = 'badge ' + (st.dry_run ? 'dry' : 'live');
  $('#engine-badge').textContent = st.running ? 'engine running' : 'engine stopped';
  $('#engine-badge').className = 'badge ' + (st.running ? 'on' : '');
  $('#btn-toggle-engine').textContent = st.running ? 'Stop' : 'Start';
  document.querySelectorAll('[data-job]').forEach(b => b.disabled = !st.running);

  const m = st.market || {}, w = st.wallet || {}, lp = st.lp || {}, h = st.hedge || {}, ch = st.chain || {};
  $('#s-price').textContent = m.eth_price ? fmtUsd(m.eth_price) : '–';
  $('#s-chain').textContent = ch.name ? `${ch.name} · ${ch.pool}${ch.rpc_ok ? '' : ' · RPC DOWN'}` : (st.last_error || 'waiting for first tick…');
  $('#s-wallet-usd').textContent = w.value_usd != null ? fmtUsd(w.value_usd) : '–';
  $('#s-wallet').textContent = st.wallet_address || '';
  $('#s-wallet').title = st.wallet_address || '';
  $('#s-wallet-bal').textContent = w.eth != null ? `${fmt(w.eth, 4)} ETH · ${fmt(w.weth, 4)} WETH · ${fmt(w.usdc, 2)} USDC` : '';

  if (lp.token_id) {
    $('#s-lp-usd').textContent = fmtUsd(lp.value_usd);
    $('#s-lp-range').innerHTML = `#${lp.token_id} · ${fmtUsd(lp.price_low)} – ${fmtUsd(lp.price_high)} · <span class="${lp.in_range ? 'good' : 'warn'}">${lp.in_range ? 'in range' : 'OUT OF RANGE'}</span>`;
    $('#s-lp-fees').textContent = `${fmt(lp.eth, 4)} ETH + ${fmt(lp.usdc, 2)} USDC · fees ${fmtUsd(lp.fees_usd_total)} · ${fmt(lp.eth_at_lower, 3)} ETH at range low`;
  } else { $('#s-lp-usd').textContent = 'none'; $('#s-lp-range').textContent = 'No LP position. Use "Open LP position".'; $('#s-lp-fees').textContent = ''; }

  if (!st.derive_configured) { $('#s-hedge').textContent = 'not set up'; $('#s-hedge-inst').textContent = 'Fill in the Derive account settings below.'; $('#s-hedge-cost').textContent = ''; }
  else if (!h.connected) { $('#s-hedge').textContent = 'offline'; $('#s-hedge-inst').textContent = h.note || ''; $('#s-hedge-cost').textContent = ''; }
  else {
    $('#s-hedge').textContent = `${fmt(h.held_contracts, 3)} / ${fmt(h.target_contracts, 3)} puts`;
    const t = h.ticker;
    $('#s-hedge-inst').textContent = h.instrument ? `${h.instrument}${t ? ` · Δ ${fmt(t.delta, 2)} · ${fmt(t.days_to_expiry, 1)}d · ask ${fmtUsd(t.ask)}` : ''}` : (h.note || '');
    const paid = (st.state?.premium_paid_usd || 0) - (st.state?.premium_received_usd || 0);
    $('#s-hedge-cost').textContent = `collateral ${fmtUsd(h.collateral_usd)} · net premium ${fmtUsd(paid)} · fees collected ${fmtUsd(st.state?.fees_collected_usd || 0)}`;
  }

  // plan box
  const a = h.action;
  if (a) {
    const kind = { none: 'No trade needed', buy: 'Buy puts', sell: 'Sell puts', roll: 'Roll hedge', need_ticker: 'Fetching quote' }[a.kind] || a.kind;
    $('#hedge-plan').innerHTML = `<div><span class="k">Hedge decision:</span> <b>${kind}</b>${a.amount ? ` ${fmt(a.amount, 3)} × ${h.instrument}` : ''}</div>
      <div class="small muted">${a.note || ''}${a.close?.length ? ' · closing ' + a.close.join(', ') : ''}</div>
      ${h.reason ? `<div class="small muted">${h.reason}</div>` : ''}
      ${(a.warnings || []).map(x => `<div class="small warn">⚠ ${x}</div>`).join('')}
      ${st.last_error ? `<div class="small bad">Last error: ${st.last_error}</div>` : ''}`;
  } else {
    $('#hedge-plan').innerHTML = st.last_error ? `<div class="small bad">Last error: ${st.last_error}</div>` : `<span class="muted small">${h.note || 'Engine has not evaluated the hedge yet.'}</span>`;
  }

  // events
  $('#events').innerHTML = (st.events || []).map(e => `<div class="ev ${e.level}"><span class="t">${new Date(e.ts * 1000).toLocaleString()}</span><span class="m">${esc(e.msg)}</span></div>`).join('');
  drawChart(st.scenarios || []);
}
const esc = (s) => String(s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

function drawChart(rows) {
  const svg = $('#chart');
  if (!rows.length) { svg.innerHTML = '<text x="320" y="150" fill="#8b93a7" text-anchor="middle">No position yet</text>'; $('#scenario-table').innerHTML = ''; return; }
  const W = 640, H = 300, P = { l: 64, r: 12, t: 12, b: 28 };
  const xs = rows.map(r => r.move_pct);
  const series = [['total_pnl', '#2ecc71', 'LP + hedge'], ['lp_pnl', '#5b8cff', 'LP only'], ['hedge_pnl', '#ffb347', 'Hedge only'], ['hodl_pnl', '#8b93a7', 'Hold instead', true]];
  const ys = rows.flatMap(r => series.map(s => r[s[0]]));
  const ymin = Math.min(0, ...ys), ymax = Math.max(0, ...ys), pad = (ymax - ymin) * 0.08 || 1;
  const X = v => P.l + (v - xs[0]) / (xs[xs.length - 1] - xs[0]) * (W - P.l - P.r);
  const Y = v => P.t + (1 - (v - (ymin - pad)) / ((ymax + pad) - (ymin - pad))) * (H - P.t - P.b);
  let out = '';
  // grid
  for (let i = 0; i <= 4; i++) { const v = ymin - pad + (ymax - ymin + 2 * pad) * i / 4; out += `<line x1="${P.l}" x2="${W - P.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="#262b36"/><text x="${P.l - 6}" y="${Y(v) + 4}" fill="#8b93a7" font-size="11" text-anchor="end">${fmtK(v)}</text>`; }
  out += `<line x1="${P.l}" x2="${W - P.r}" y1="${Y(0)}" y2="${Y(0)}" stroke="#8b93a7" stroke-dasharray="2 3"/>`;
  out += `<line x1="${X(0)}" x2="${X(0)}" y1="${P.t}" y2="${H - P.b}" stroke="#8b93a7" stroke-dasharray="2 3"/>`;
  xs.forEach(x => out += `<text x="${X(x)}" y="${H - 8}" fill="#8b93a7" font-size="11" text-anchor="middle">${x > 0 ? '+' : ''}${x}%</text>`);
  for (const [k, col, , dash] of series) {
    out += `<polyline fill="none" stroke="${col}" stroke-width="${k === 'total_pnl' ? 3 : 2}" ${dash ? 'stroke-dasharray="6 4"' : ''} points="${rows.map(r => `${X(r.move_pct)},${Y(r[k])}`).join(' ')}"/>`;
  }
  svg.innerHTML = out;
  $('#chart-legend').innerHTML = series.map(s => `<span><i style="background:${s[1]}"></i>${s[2]}</span>`).join('');
  $('#scenario-table').innerHTML = `<tr><th>ETH move</th><th>Price</th><th>LP</th><th>Hedge</th><th>Total</th><th>Hold</th></tr>` +
    rows.filter(r => [-30, -20, -10, 0, 10, 20, 30].includes(r.move_pct)).map(r => `<tr><td>${r.move_pct > 0 ? '+' : ''}${r.move_pct}%</td><td>${fmtUsd(r.price)}</td><td>${fmtUsd(r.lp_pnl)}</td><td>${fmtUsd(r.hedge_pnl)}</td><td class="${r.total_pnl >= 0 ? 'good' : 'bad'}">${fmtUsd(r.total_pnl)}</td><td>${fmtUsd(r.hodl_pnl)}</td></tr>`).join('');
}

/* ---------------- boot ---------------- */
async function poll() {
  try {
    const st = await api('status');
    await refreshGate(st);
    if (st.wallet_unlocked) render(st);
  } catch (err) { $('#s-chain').textContent = 'server unreachable: ' + err.message; }
}
(async () => {
  presets = await api('presets');
  cfg = (await api('config')).config;
  loadSettingsForm();
  await poll();
  setInterval(poll, 5000);
})();
