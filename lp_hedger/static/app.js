/* LP Hedger paper-trading UI: plain JS, no build step. Talks to the local FastAPI server. */
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
const fmtUsd = (v, d) => v == null || isNaN(v) ? '–' : (v < 0 ? '-' : '') + '$' + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: d != null ? d : (Math.abs(v) >= 1000 ? 0 : 2), minimumFractionDigits: d != null ? d : (Math.abs(v) >= 1000 ? 0 : 2) });
const fmtSigned = (v, d) => v == null || isNaN(v) ? '–' : (v >= 0 ? '+' : '-') + '$' + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: d != null ? d : 2, minimumFractionDigits: d != null ? d : 2 });
const fmtK = (v) => (v < 0 ? '-' : '') + '$' + (Math.abs(v) >= 1e6 ? (Math.abs(v) / 1e6).toFixed(1) + 'M' : Math.abs(v) >= 1000 ? (Math.abs(v) / 1000).toFixed(1) + 'k' : Math.abs(v).toFixed(Math.abs(v) < 10 ? 1 : 0));
const fmt = (v, d = 4) => v == null || isNaN(v) ? '–' : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });
const pct = (v, d = 1) => v == null || isNaN(v) ? '–' : Number(v).toFixed(d) + '%';
const cls = (v) => v == null ? '' : v >= 0 ? 'good' : 'bad';
const esc = (s) => String(s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
const ago = (ts) => { const s = Date.now() / 1000 - ts; return s < 90 ? `${Math.round(s)}s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : `${(s / 3600).toFixed(1)} h ago`; };
const dateShort = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });

let presets = [];
let cfg = null;

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
  $('[name="chain.rpc_url"]').placeholder = 'leave empty for ' + chain.default_rpc + ' (+ fallbacks)';
}
$('#chain-select').addEventListener('change', fillPools);

function loadSettingsForm() {
  const f = $('#settings');
  for (const el of f.elements) {
    if (!el.name) continue;
    const [sec, key] = el.name.split('.');
    const v = cfg[sec]?.[key];
    if (el.type === 'checkbox') el.checked = !!v; else el.value = v ?? '';
  }
  fillChainSelects();
  $('[name="derive.environment"]').value = cfg.derive.environment;
  $('[name="derive.api_version"]').value = cfg.derive.api_version || 'v2';
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

/* ---------------- actions ---------------- */
document.querySelectorAll('[data-job]').forEach(b => b.addEventListener('click', async () => {
  const job = b.dataset.job;
  if (b.classList.contains('danger') && !confirm(`Really ${b.textContent.toLowerCase()}? (paper only)`)) return;
  try { await api('paper/' + job, {}); $('#action-note').textContent = `Queued "${b.textContent}". It runs on the next engine tick.`; poll(); }
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
  const running = st.running;
  $('#engine-badge').textContent = running ? (st.last_tick ? `polling · ${ago(st.last_tick)}` : 'polling · first tick…') : 'engine stopped';
  $('#engine-badge').className = 'badge ' + (running ? 'on' : '');
  $('#btn-toggle-engine').textContent = running ? 'Stop' : 'Start';
  document.querySelectorAll('[data-job]').forEach(b => {
    const open = st.lp?.open;
    b.disabled = !running || (b.dataset.job === 'open_lp' && open) || ((b.dataset.job === 'close_lp' || b.dataset.job === 'rebalance') && !open);
  });

  const tr = st.tracking || {}, m = st.market || {}, lp = st.lp || {}, h = st.hedge || {}, pool = st.pool || {}, tot = st.totals || {}, pr = st.projection;
  $('#track-badge').textContent = tr.chain_name ? `${tr.chain_name} · ${tr.pool}` : '…';
  $('#track-badge').className = 'badge track';

  // banner
  const banner = $('#banner');
  if (st.last_error) { banner.className = 'banner error'; banner.textContent = 'Last error: ' + st.last_error; }
  else if (tr.locked) { banner.className = 'banner'; banner.textContent = `Tracking ${tr.chain_name} ${tr.pool} because the open paper position lives there. The chain/pool in Settings applies after you close it.`; }
  else banner.className = 'banner hidden';

  // price card
  $('#s-price').textContent = m.eth_price ? fmtUsd(m.eth_price, 2) : '–';
  $('#s-chain').innerHTML = tr.chain_name ? `${tr.chain_name} · ${tr.pool} · <a class="muted" href="${tr.explorer}/address/${tr.pool_address}" target="_blank" rel="noopener">pool ↗</a>` : (st.last_error ? '' : 'waiting for first tick…');
  $('#s-block').textContent = m.block ? `block ${m.block} · gas ${fmt(m.gas_price_gwei, 3)} gwei · ${tr.rpc?.replace('https://', '')}` : '';

  // pool card
  $('#s-pool-days').textContent = pool.days_requested ?? 5;
  if (pool.days_covered > 0) {
    $('#s-vol').textContent = fmtK(pool.volume_usd) + ' vol';
    $('#s-vol-detail').textContent = `${fmtK(pool.volume_per_day_usd)}/day · 24h ${fmtK(pool.volume_24h_usd)} · pool fees ${fmtK(pool.fees_usd)}`;
    const bf = pool.backfill || {};
    const src = bf.method ? `${bf.method === 'archive' ? 'fee growth at historical blocks' : 'sampled swap logs'} via ${(bf.rpc || '').replace('https://', '')}` : 'live samples only';
    $('#s-pool-src').textContent = `${pool.days_covered.toFixed(1)} of ${pool.days_requested} days covered · ${src}`;
  } else { $('#s-vol').textContent = '–'; $('#s-vol-detail').textContent = 'collecting history…'; $('#s-pool-src').textContent = pool.backfill?.note ? 'backfill failed: ' + pool.backfill.note.slice(0, 80) : ''; }

  // LP card
  if (lp.open) {
    $('#s-lp-usd').innerHTML = `${fmtUsd(lp.value_usd)} <span class="small ${cls(lp.pnl_usd)}">${fmtSigned(lp.pnl_usd)}</span>`;
    $('#s-lp-range').innerHTML = `${fmtUsd(lp.price_low, 0)} – ${fmtUsd(lp.price_high, 0)} · <span class="${lp.in_range ? 'good' : 'warn'}">${lp.in_range ? 'in range' : 'OUT OF RANGE'}</span> · ${lp.age_days < 1 ? (lp.age_days * 24).toFixed(1) + ' h' : lp.age_days.toFixed(1) + ' d'} old${lp.time_in_range_pct != null ? ` · ${pct(lp.time_in_range_pct, 0)} in range` : ''}`;
    $('#s-lp-fees').textContent = `fees ${fmtUsd(lp.fees_usd_total)} · IL ${fmtSigned(lp.il_usd)} · costs ${fmtUsd(lp.entry_cost_usd)} · ${fmt(lp.eth, 4)} ETH + ${fmt(lp.usdc, 0)} USDC`;
  } else if (lp.plan) {
    const p = lp.plan;
    $('#s-lp-usd').innerHTML = `<span class="muted">none</span>`;
    $('#s-lp-range').textContent = `Plan: ${fmtUsd(p.value_usd, 0)} in ${fmtUsd(p.price_low, 0)} – ${fmtUsd(p.price_high, 0)}`;
    $('#s-lp-fees').textContent = `${fmt(p.eth, 4)} ETH + ${fmt(p.usd, 0)} USDC · ${fmt(p.eth_at_lower, 3)} ETH at range low · est. entry cost ${fmtUsd(p.entry_cost_usd)}`;
  } else { $('#s-lp-usd').textContent = '–'; $('#s-lp-range').textContent = ''; $('#s-lp-fees').textContent = ''; }

  // hedge card
  if (!h.connected) { $('#s-hedge').textContent = h.api ? 'offline' : '–'; $('#s-hedge-inst').textContent = h.note || ''; $('#s-hedge-cost').textContent = ''; }
  else {
    const held = h.held_contracts || 0;
    $('#s-hedge').innerHTML = held > 0 ? `${fmt(held, 3)} puts <span class="small ${cls(h.pnl_usd)}">${fmtSigned(h.pnl_usd)}</span>` : `<span class="muted">none</span>`;
    const legs = h.legs || [];
    $('#s-hedge-inst').textContent = legs.length ? legs.map(l => `${l.instrument_name} (${fmt(l.days_to_expiry, 1)}d, Δ ${fmt(l.delta, 2)})`).join(' · ') : (h.instrument ? `candidate ${h.instrument}` : (h.note || ''));
    $('#s-hedge-cost').textContent = `value ${fmtUsd(h.value_usd)} · paid ${fmtUsd(h.premium_paid_usd)} + fees ${fmtUsd(h.fees_paid_usd)} · received ${fmtUsd(h.premium_received_usd)} · payouts ${fmtUsd(h.settled_payout_usd)}`;
  }

  // totals
  if (tot.total_pnl_usd != null) {
    $('#s-total').innerHTML = `<span class="${cls(tot.total_pnl_usd)}">${fmtSigned(tot.total_pnl_usd)}</span>`;
    $('#s-total-detail').textContent = `LP ${fmtSigned(tot.lp_pnl_usd)} · hedge ${fmtSigned(tot.hedge_pnl_usd)} · holding instead ${fmtSigned(tot.hodl_pnl_usd)}`;
    const r = st.realized || {};
    $('#s-total-realized').textContent = `realized: LP ${fmtSigned(r.lp_usd)} · hedge ${fmtSigned(r.hedge_usd)} · costs paid ${fmtUsd(r.costs_usd)}`;
  }

  renderProjection(pr, lp, h);

  // decision box
  const a = h.action;
  if (a) {
    const kind = { none: 'No trade needed', buy: 'Buy puts', sell: 'Sell puts', roll: 'Roll hedge', need_ticker: 'Fetching quote' }[a.kind] || a.kind;
    const t = h.ticker;
    $('#hedge-plan').innerHTML = `<div><span class="k">Hedge decision this tick:</span> <b>${kind}</b>${a.amount ? ` ${fmt(a.amount, 3)} × ${h.instrument}` : ''}</div>
      <div class="small muted">${esc(a.note || '')}${a.close?.length ? ' · closing ' + a.close.join(', ') : ''}</div>
      ${h.reason && h.reason !== a.note ? `<div class="small muted">${esc(h.reason)}</div>` : ''}
      ${t ? `<div class="small muted">${h.instrument}: bid ${fmtUsd(t.bid)} (${fmt(t.bid_size, 1)}) / ask ${fmtUsd(t.ask)} (${fmt(t.ask_size, 1)}) · mark ${fmtUsd(t.mark)} · Δ ${fmt(t.delta, 2)} · IV ${pct(t.iv * 100, 0)} · ${fmt(t.days_to_expiry, 1)} d</div>` : ''}
      ${(a.warnings || []).map(x => `<div class="small warn">⚠ ${esc(x)}</div>`).join('')}`;
  } else {
    $('#hedge-plan').innerHTML = `<span class="muted small">${esc(h.note || 'The hedge policy has not run yet.')}</span>`;
  }

  // legs
  const legs = h.legs || [];
  $('#legs-table').innerHTML = legs.length ? `<tr><th>Instrument</th><th>Qty</th><th>Paid</th><th>Mark</th><th>Bid</th><th>Δ</th><th>Expires</th><th>P&amp;L</th></tr>` +
    legs.map(l => `<tr><td>${l.instrument_name}</td><td>${fmt(l.amount, 3)}</td><td>${fmtUsd(l.entry_price)}</td><td>${fmtUsd(l.mark)}</td><td>${fmtUsd(l.bid)}</td><td>${fmt(l.delta, 2)}</td><td>${fmt(l.days_to_expiry, 1)} d</td><td class="${cls(l.pnl_usd)}">${fmtSigned(l.pnl_usd)}</td></tr>`).join('')
    : '<tr><td class="muted">No hedge legs held.</td></tr>';
  $('#trades').innerHTML = (h.trades || []).length ? h.trades.map(t => `<div class="ev"><span class="t">${new Date(t.ts * 1000).toLocaleString()}</span><span class="m">${t.side.toUpperCase()} ${fmt(t.amount, 3)} ${t.instrument} @ ${fmtUsd(t.price)}${t.side === 'settle' ? ` → payout ${fmtUsd(t.payout_usd)}` : ` · fee ${fmtUsd(t.fee_usd)} · vs mark ${fmtUsd(t.spread_cost_usd)}`}</span></div>`).join('') : '<div class="ev"><span class="m muted">No paper trades yet.</span></div>';
  const closed = st.closed || [];
  $('#closed-table').innerHTML = closed.length ? `<tr><th>Closed</th><th>Reason</th><th>Size</th><th>Days</th><th>Fees</th><th>IL</th><th>Costs</th><th>P&amp;L</th></tr>` +
    closed.map(c => `<tr><td>${dateShort(c.ts)}</td><td>${esc(c.reason)}</td><td>${fmtUsd(c.deploy_usd, 0)}</td><td>${fmt(c.days, 1)}</td><td>${fmtUsd(c.fees_usd)}</td><td>${fmtSigned(c.il_usd)}</td><td>${fmtUsd(c.entry_cost_usd + c.exit_cost_usd)}</td><td class="${cls(c.pnl_usd)}">${fmtSigned(c.pnl_usd)}</td></tr>`).join('')
    : '<tr><td class="muted">Nothing closed yet.</td></tr>';

  // events
  $('#events').innerHTML = (st.events || []).map(e => `<div class="ev ${e.level}"><span class="t">${new Date(e.ts * 1000).toLocaleString()}</span><span class="m ${e.msg.startsWith('PAPER') ? 'paper' : ''}">${esc(e.msg)}</span></div>`).join('');

  $('#scenario-sub').textContent = st.scenario_is_plan ? '(planned position)' : '';
  drawScenario(st.scenarios || []);
  drawEquity(st.equity || []);
  drawDaily(st.daily || [], pool);
}

function renderProjection(pr, lp, h) {
  const el = $('#projection');
  if (!pr) { el.innerHTML = '<span class="muted small">Waiting for pool history…</span>'; return; }
  $('#p-days').textContent = pr.based_on_days ? pr.based_on_days.toFixed(1) : '–';
  const hz = pr.horizon || {}, hg = pr.hedge;
  const subject = lp.open ? 'your open position' : 'the planned position';
  const verdict = (() => {
    if (!pr.based_on_days) return ['warn', 'No pool history yet. Numbers appear after the backfill or a few polls.'];
    const net = hz.net_usd, days = pr.horizon_days;
    if (hg && hg.fees_cover_hedge_pct != null) {
      if (hg.fees_cover_hedge_pct >= 150) return ['good', `Fees are earning about ${pct(hg.fees_cover_hedge_pct, 0)} of the hedge cost: over ${days} days ${subject} nets ≈ ${fmtSigned(net)} if volume and range behaviour stay as they were, with the downside below ${fmtUsd(hg.strike || 0, 0)} capped by the puts.`];
      if (hg.fees_cover_hedge_pct >= 100) return ['warn', `Fees barely cover the hedge (${pct(hg.fees_cover_hedge_pct, 0)}): ≈ ${fmtSigned(net)} over ${days} days before any price move. Thin margin; a quieter week turns this negative.`];
      return ['bad', `Fees cover only ${pct(hg.fees_cover_hedge_pct, 0)} of the hedge premium: ≈ ${fmtSigned(net)} over ${days} days. Fully insured LPing here loses money at current volume; widen coverage down, pick a cheaper strike/expiry, or a busier pool.`];
    }
    return [net >= 0 ? 'good' : 'bad', `Unhedged: ≈ ${fmtSigned(net)} in fees over ${days} days after entry costs, but the downside below the range is unprotected (you would hold ${fmt(lp.open ? lp.eth_at_lower : lp.plan?.eth_at_lower, 3)} ETH).`];
  })();
  el.innerHTML = `
    <div class="big">
      <div><div class="l">Fees / day</div><div class="n">${fmtUsd(pr.fees_per_day_usd)}</div><div class="hint">${pct(pr.fee_apr_pct, 1)} APR on size · ${pct(pr.time_in_range_pct, 0)} time in range</div></div>
      <div><div class="l">Hedge / day</div><div class="n">${hg ? fmtUsd(hg.cost_per_day_usd) : '–'}</div><div class="hint">${hg ? `${hg.instrument} · ${fmt(hg.contracts, 3)} × ${fmtUsd(hg.price)} + fee ${fmtUsd(hg.fee_usd)}` : (h.connected ? 'no quote (hedging off or no LP plan)' : 'Derive offline')}</div></div>
      <div><div class="l">Net carry / day</div><div class="n ${cls(pr.net_carry_per_day_usd)}">${fmtSigned(pr.net_carry_per_day_usd)}</div><div class="hint">${hg && hg.fees_cover_hedge_pct != null ? `fees cover ${pct(hg.fees_cover_hedge_pct, 0)} of the hedge` : 'fees minus hedge cost'}</div></div>
    </div>
    <table class="small">
      <tr><td>Fees the range would have earned over the last ${pr.based_on_days.toFixed(1)} days</td><td>${fmtUsd(pr.fees_lookback_usd)}</td></tr>
      <tr><td>Your share of in-range liquidity</td><td>${pr.pool_share_pct != null ? pct(pr.pool_share_pct, 3) : '–'}</td></tr>
      ${hg ? `<tr><td>Hedge premium per purchase (${fmt(hg.held_days, 0)} days held before roll)</td><td>${fmtUsd(hg.premium_usd + hg.fee_usd)} (${pct(hg.premium_pct_of_lp, 2)} of size)</td></tr>
      <tr><td>Days of fees to pay for one hedge purchase</td><td>${hg.breakeven_days != null ? fmt(hg.breakeven_days, 1) : '–'}</td></tr>` : ''}
      <tr><td>Entry costs (swap fee, price impact, gas)</td><td>${fmtUsd(pr.entry_cost_usd)}</td></tr>
      <tr><th>Projected ${pr.horizon_days} days, price staying in range</th><th></th></tr>
      <tr><td>Fees</td><td class="good">${fmtSigned(hz.fees_usd)}</td></tr>
      <tr><td>Hedge cost (premium + fees, rolled)</td><td class="bad">${fmtSigned(-hz.hedge_cost_usd)}</td></tr>
      <tr><td>Entry costs</td><td class="bad">${fmtSigned(-hz.entry_cost_usd)}</td></tr>
      <tr><td><b>Net</b></td><td class="${cls(hz.net_usd)}"><b>${fmtSigned(hz.net_usd)}</b> (${pct(hz.net_pct, 2)})</td></tr>
    </table>
    <div class="verdict ${verdict[0]}">${verdict[1]}</div>`;
}

/* ---------------- charts ---------------- */
function frame(svg, xs, ys, P, W, H, fmtY, fmtX) {
  const ymin0 = Math.min(0, ...ys), ymax0 = Math.max(0, ...ys), pad = (ymax0 - ymin0) * 0.08 || 1;
  const ymin = ymin0 - pad, ymax = ymax0 + pad;
  const x0 = xs[0], x1 = xs[xs.length - 1];
  const X = v => P.l + (x1 === x0 ? 0.5 : (v - x0) / (x1 - x0)) * (W - P.l - P.r);
  const Y = v => P.t + (1 - (v - ymin) / (ymax - ymin)) * (H - P.t - P.b);
  let out = '';
  for (let i = 0; i <= 4; i++) { const v = ymin + (ymax - ymin) * i / 4; out += `<line x1="${P.l}" x2="${W - P.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="#262b36"/><text x="${P.l - 6}" y="${Y(v) + 4}" fill="#8b93a7" font-size="11" text-anchor="end">${fmtY(v)}</text>`; }
  out += `<line x1="${P.l}" x2="${W - P.r}" y1="${Y(0)}" y2="${Y(0)}" stroke="#8b93a7" stroke-dasharray="2 3"/>`;
  const n = Math.min(xs.length, 7);
  for (let i = 0; i < n; i++) { const x = xs[Math.round(i * (xs.length - 1) / Math.max(n - 1, 1))]; out += `<text x="${X(x)}" y="${H - 8}" fill="#8b93a7" font-size="11" text-anchor="middle">${fmtX(x)}</text>`; }
  return { X, Y, out };
}

function drawScenario(rows) {
  const svg = $('#chart');
  if (!rows.length) { svg.innerHTML = '<text x="320" y="130" fill="#8b93a7" text-anchor="middle">No position yet</text>'; $('#scenario-table').innerHTML = ''; return; }
  const W = 640, H = 260, P = { l: 64, r: 12, t: 12, b: 28 };
  const xs = rows.map(r => r.move_pct);
  const series = [['total_pnl', '#2ecc71', 'LP + hedge'], ['lp_pnl', '#5b8cff', 'LP only'], ['hedge_pnl', '#ffb347', 'Hedge only'], ['hodl_pnl', '#8b93a7', 'Hold instead', true]];
  const ys = rows.flatMap(r => series.map(s => r[s[0]]));
  const f = frame(svg, xs, ys, P, W, H, fmtK, x => (x > 0 ? '+' : '') + x + '%');
  let out = f.out + `<line x1="${f.X(0)}" x2="${f.X(0)}" y1="${P.t}" y2="${H - P.b}" stroke="#8b93a7" stroke-dasharray="2 3"/>`;
  for (const [k, col, , dash] of series) out += `<polyline fill="none" stroke="${col}" stroke-width="${k === 'total_pnl' ? 3 : 2}" ${dash ? 'stroke-dasharray="6 4"' : ''} points="${rows.map(r => `${f.X(r.move_pct)},${f.Y(r[k])}`).join(' ')}"/>`;
  svg.innerHTML = out;
  $('#chart-legend').innerHTML = series.map(s => `<span><i style="background:${s[1]}"></i>${s[2]}</span>`).join('');
  $('#scenario-table').innerHTML = `<tr><th>ETH move</th><th>Price</th><th>LP</th><th>Hedge</th><th>Total</th><th>Hold</th></tr>` +
    rows.filter(r => [-30, -20, -10, 0, 10, 20, 30].includes(r.move_pct)).map(r => `<tr><td>${r.move_pct > 0 ? '+' : ''}${r.move_pct}%</td><td>${fmtUsd(r.price, 0)}</td><td>${fmtSigned(r.lp_pnl, 0)}</td><td>${fmtSigned(r.hedge_pnl, 0)}</td><td class="${cls(r.total_pnl)}">${fmtSigned(r.total_pnl, 0)}</td><td>${fmtSigned(r.hodl_pnl, 0)}</td></tr>`).join('');
}

function drawEquity(pts) {
  const svg = $('#equity-chart');
  if (pts.length < 2) { svg.innerHTML = '<text x="320" y="130" fill="#8b93a7" text-anchor="middle">Open a paper position to start the curve</text>'; $('#equity-legend').innerHTML = ''; return; }
  const W = 640, H = 260, P = { l: 64, r: 12, t: 12, b: 28 };
  const series = [['total_pnl_usd', '#2ecc71', 'Total P&L'], ['lp_pnl_usd', '#5b8cff', 'LP (value + fees − costs)'], ['hedge_pnl_usd', '#ffb347', 'Hedge'], ['hodl_pnl_usd', '#8b93a7', 'Hold instead', true]];
  const xs = pts.map(p => p.ts);
  const ys = pts.flatMap(p => series.map(s => p[s[0]] || 0));
  const span = xs[xs.length - 1] - xs[0];
  const fx = span > 2 * 86400 ? dateShort : t => new Date(t * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  const f = frame(svg, xs, ys, P, W, H, fmtK, fx);
  let out = f.out;
  for (const [k, col, , dash] of series) out += `<polyline fill="none" stroke="${col}" stroke-width="${k === 'total_pnl_usd' ? 3 : 1.5}" ${dash ? 'stroke-dasharray="6 4"' : ''} points="${pts.map(p => `${f.X(p.ts)},${f.Y(p[k] || 0)}`).join(' ')}"/>`;
  svg.innerHTML = out;
  $('#equity-legend').innerHTML = series.map(s => `<span><i style="background:${s[1]}"></i>${s[2]}</span>`).join('');
}

function drawDaily(days, pool) {
  const svg = $('#daily-chart');
  if (!days.length || !days.some(d => d.volume_usd > 0)) { svg.innerHTML = '<text x="320" y="130" fill="#8b93a7" text-anchor="middle">No volume history yet</text>'; $('#daily-table').innerHTML = ''; $('#daily-legend').innerHTML = ''; return; }
  const W = 640, H = 260, P = { l: 64, r: 64, t: 12, b: 28 };
  const vmax = Math.max(...days.map(d => d.volume_usd)) * 1.1 || 1;
  const fmax = Math.max(...days.map(d => d.position_fees_usd)) * 1.1 || 1;
  const bw = (W - P.l - P.r) / days.length;
  const Yv = v => P.t + (1 - v / vmax) * (H - P.t - P.b);
  const Yf = v => P.t + (1 - v / fmax) * (H - P.t - P.b);
  let out = '';
  for (let i = 0; i <= 4; i++) { const v = vmax * i / 4; out += `<line x1="${P.l}" x2="${W - P.r}" y1="${Yv(v)}" y2="${Yv(v)}" stroke="#262b36"/><text x="${P.l - 6}" y="${Yv(v) + 4}" fill="#8b93a7" font-size="11" text-anchor="end">${fmtK(v)}</text><text x="${W - P.r + 6}" y="${Yf(fmax * i / 4) + 4}" fill="#ffb347" font-size="11">${fmtK(fmax * i / 4)}</text>`; }
  days.forEach((d, i) => {
    const x = P.l + i * bw;
    const partial = d.coverage_pct < 95;
    out += `<rect x="${x + bw * 0.15}" y="${Yv(d.volume_usd)}" width="${bw * 0.7}" height="${Yv(0) - Yv(d.volume_usd)}" fill="#5b8cff" opacity="${partial ? 0.45 : 0.85}"><title>${dateShort(d.day_start)}: volume ${fmtUsd(d.volume_usd, 0)} (${d.coverage_pct.toFixed(0)}% of day covered)</title></rect>`;
    out += `<text x="${x + bw / 2}" y="${H - 8}" fill="#8b93a7" font-size="11" text-anchor="middle">${dateShort(d.day_start)}</text>`;
  });
  out += `<polyline fill="none" stroke="#ffb347" stroke-width="2" points="${days.map((d, i) => `${P.l + i * bw + bw / 2},${Yf(d.position_fees_usd)}`).join(' ')}"/>`;
  days.forEach((d, i) => out += `<circle cx="${P.l + i * bw + bw / 2}" cy="${Yf(d.position_fees_usd)}" r="3" fill="#ffb347"><title>${dateShort(d.day_start)}: your range would have earned ${fmtUsd(d.position_fees_usd)}</title></circle>`);
  svg.innerHTML = out;
  $('#daily-legend').innerHTML = `<span><i style="background:#5b8cff"></i>Pool volume (left axis; faded = partial day)</span><span><i style="background:#ffb347"></i>Fees your range would earn (right axis)</span>`;
  $('#daily-table').innerHTML = `<tr><th>Day</th><th>Volume</th><th>Pool fees</th><th>Your fees</th><th>In range</th><th>Covered</th></tr>` +
    days.map(d => `<tr><td>${dateShort(d.day_start)}</td><td>${fmtK(d.volume_usd)}</td><td>${fmtK(d.fees_usd)}</td><td>${fmtUsd(d.position_fees_usd)}</td><td>${d.time_in_range_pct == null ? '–' : pct(d.time_in_range_pct, 0)}</td><td>${d.coverage_pct.toFixed(0)}%</td></tr>`).join('');
}

/* ---------------- boot ---------------- */
async function poll() {
  try { render(await api('status')); }
  catch (err) { $('#s-chain').textContent = 'server unreachable: ' + err.message; }
}
(async () => {
  presets = await api('presets');
  cfg = (await api('config')).config;
  loadSettingsForm();
  await poll();
  setInterval(poll, 5000);
})();
