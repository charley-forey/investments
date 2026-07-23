/* ============================================================================
   app.js — the operations console.

   Structure:
     state / api        — fetch with in-flight de-dupe, error surfacing, staleness
     router             — hash routes so every view and drill-down is linkable
     ui                 — table, card, toast, drawer, command palette
     views              — cockpit, portfolio, markets, mind, performance, control
   ========================================================================= */
'use strict';
const F = V.fmt;
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ==========================================================================
   State
   ======================================================================== */
const state = {
  view: 'cockpit',
  range: localStorage.range || '1M',      // 1W | 1M | 3M | ALL
  brush: null,                            // [tMin, tMax] from a chart drag
  symbol: localStorage.symbol || null,
  heatMetric: 'momentum_20',
  cache: {},
  seen: JSON.parse(localStorage.seen || '{}'),
  timer: null,
};
const RANGE_DAYS = { '1W': 7, '1M': 30, '3M': 90, ALL: 3650 };

function rangeStart() {
  if (state.brush) return state.brush[0];
  return Date.now() - RANGE_DAYS[state.range] * 864e5;
}
function rangeEnd() { return state.brush ? state.brush[1] : Date.now(); }
const inRange = ts => { const t = +new Date(ts); return t >= rangeStart() && t <= rangeEnd(); };

/* ==========================================================================
   API — one fetch per URL in flight; failures become toasts, not blank pages
   ======================================================================== */
const inflight = {};
async function api(path, opts) {
  if (!opts && inflight[path]) return inflight[path];
  const p = fetch(path, opts).then(async r => {
    if (!r.ok) throw new Error(`${r.status} ${await r.text().catch(() => '')}`.slice(0, 180));
    return r.json();
  }).finally(() => { delete inflight[path]; });
  if (!opts) inflight[path] = p;
  return p.catch(e => { toast(`${path} — ${e.message}`, 'critical'); throw e; });
}
const post = (path, body) => api(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}),
});
/** Fetch a set of endpoints; a failure yields null for that slot, never a dead view. */
async function pull(map) {
  const keys = Object.keys(map);
  const vals = await Promise.all(keys.map(k => api(map[k]).catch(() => null)));
  return Object.fromEntries(keys.map((k, i) => [k, vals[i]]));
}

/* ==========================================================================
   UI primitives
   ======================================================================== */
function toast(msg, kind = '') {
  const t = document.createElement('div');
  t.className = 'toast ' + kind;
  t.textContent = msg;
  $('#toasts').appendChild(t);
  setTimeout(() => t.remove(), kind === 'critical' ? 9000 : 4200);
}

/** A card with an optional table-view toggle wired to its chart host. */
function card(o) {
  const id = 'c' + Math.random().toString(36).slice(2, 8);
  return `<section class="card ${o.cls || ''}">
    <header>
      <h3>${esc(o.title)}</h3>
      ${o.hint ? `<span class="hint">${esc(o.hint)}</span>` : ''}
      <span class="head-actions">${o.actions || ''}${o.chart !== false ? `
        <button class="icon" data-table="${id}" title="Switch to table view"
          aria-label="Switch to table view">▤</button>` : ''}</span>
    </header>
    ${o.chart !== false ? `<div class="chart-host" id="${id}"></div>` : (o.body || '')}
  </section>`;
}

/** Sortable, mobile-stacking, page-capped table. cols: {k,label,r,fmt,cls,sortVal}
 *  Long lists are capped rather than dumped: an 800-row table is a 40,000px page
 *  on a phone, and nobody scrolls it. Filter + "show more" instead. */
const PAGE = () => innerWidth < 720 ? 10 : 25;
function table(host, cols, rows, o = {}) {
  if (!host) return;
  const st = host._sort || o.sort || {};
  let shown = host._shown || o.limit || PAGE();
  let q = host._q || '';
  const render = () => {
    let rs = [...rows];
    if (q) {
      const ql = q.toLowerCase();
      rs = rs.filter(r => cols.some(c => {
        const v = c.sortVal ? c.sortVal(r) : r[c.k];
        return String(v ?? '').toLowerCase().includes(ql);
      }));
    }
    if (st.k) {
      const c = cols.find(c => c.k === st.k);
      rs.sort((a, b) => {
        const va = c.sortVal ? c.sortVal(a) : a[st.k], vb = c.sortVal ? c.sortVal(b) : b[st.k];
        if (va == null) return 1; if (vb == null) return -1;
        return (typeof va === 'number' ? va - vb : String(va).localeCompare(String(vb)))
          * (st.dir === 'asc' ? 1 : -1);
      });
    }
    const total = rs.length;
    const page = rs.slice(0, shown);
    host.innerHTML = `
      ${rows.length > PAGE() || q ? `<div class="table-tools">
        <input type="search" class="tfilter" placeholder="Filter ${rows.length} rows…" value="${esc(q)}">
        <span class="hint">${total === rows.length ? `${total} rows`
          : `${total} of ${rows.length} rows`}</span></div>` : ''}
      <div class="table-wrap"><table><thead><tr>${
        cols.map(c => `<th data-k="${c.k}" class="${c.r ? 'r' : ''} ${c.sec ? 'sec' : ''}" ${
          st.k === c.k ? `aria-sort="${st.dir === 'asc' ? 'ascending' : 'descending'}"` : ''
        } title="Sort by ${esc(c.label)}">${esc(c.label)}</th>`).join('')
      }</tr></thead><tbody>${
        page.length ? page.map((r, i) => `<tr ${o.rowAttr ? o.rowAttr(r) : ''}>${
          cols.map(c => `<td data-l="${esc(c.label)}" class="${c.r ? 'r' : ''} ${c.sec ? 'sec' : ''} ${
            c.cls ? c.cls(r) : ''}">${c.fmt ? c.fmt(r, i) : esc(r[c.k] ?? '—')}</td>`).join('')
        }</tr>`).join('') : `<tr><td colspan="${cols.length}"><div class="empty">${
          esc(q ? `No row matches “${esc(q)}”.` : (o.empty || 'Nothing here yet.'))}</div></td></tr>`
      }</tbody></table></div>
      ${total > shown ? `<div class="table-more"><button class="more">Show ${
        Math.min(PAGE() * 2, total - shown)} more <span class="muted">(${
        total - shown} hidden)</span></button></div>` : ''}`;

    $$('th[data-k]', host).forEach(th => th.onclick = () => {
      const k = th.dataset.k;
      host._sort = st.k === k && st.dir === 'desc' ? { k, dir: 'asc' } : { k, dir: 'desc' };
      table(host, cols, rows, { ...o, sort: host._sort });
    });
    const more = $('.more', host);
    if (more) more.onclick = () => { host._shown = shown + PAGE() * 2; table(host, cols, rows, o); };
    const filt = $('.tfilter', host);
    if (filt) filt.oninput = e => {
      host._q = e.target.value; host._shown = o.limit || PAGE();
      table(host, cols, rows, o);
      const el = $('.tfilter', host);
      el.focus(); el.setSelectionRange(el.value.length, el.value.length);
    };
  };
  render();
}
/** Long free text: one line in the cell, the whole thing on hover and in the sheet. */
const clip = (s, n = 90) => s
  ? `<span class="clip" title="${esc(s)}">${esc(String(s).slice(0, n))}${String(s).length > n ? '…' : ''}</span>`
  : '<span class="muted">—</span>';

/* -- drawer ---------------------------------------------------------------- */
function openDrawer(title, html, sub) {
  $('#drawerTitle').textContent = title;
  $('#drawerSub').innerHTML = sub || '';
  $('#drawerBody').innerHTML = html;
  $('#drawer').classList.add('on');
  $('#scrim').classList.add('on');
  $('#drawerClose').focus();
}
function closeDrawer() {
  $('#drawer').classList.remove('on');
  $('#scrim').classList.remove('on');
}

/* ==========================================================================
   Router
   ======================================================================== */
const VIEWS = {
  cockpit:     { label: 'Cockpit',     ico: '◧', render: viewCockpit,     title: 'Cockpit' },
  portfolio:   { label: 'Portfolio',   ico: '▤', render: viewPortfolio,   title: 'Portfolio & risk' },
  markets:     { label: 'Markets',     ico: '◪', render: viewMarkets,     title: 'Markets & signals' },
  mind:        { label: 'Agent',       ico: '◆', render: viewMind,        title: 'Agent reasoning' },
  performance: { label: 'Performance', ico: '◨', render: viewPerformance, title: 'Performance & edge' },
  control:     { label: 'Control',     ico: '⚙', render: viewControl,     title: 'Control & configuration' },
};

function route() {
  const [, view, a, b] = (location.hash || '#/cockpit').split('/');
  state.view = VIEWS[view] ? view : 'cockpit';
  if (state.view === 'markets' && a) { state.symbol = decodeURIComponent(a).toUpperCase(); localStorage.symbol = state.symbol; }
  $$('#rail a').forEach(el => el.setAttribute('aria-current',
    el.dataset.v === state.view ? 'page' : 'false'));
  $('#viewTitle').textContent = VIEWS[state.view].title;
  render();
  if (view === 'symbol' && a) symbolSheet(decodeURIComponent(a));
  if (view === 'decision' && a) decisionSheet(a);
}
const go = h => { location.hash = h; };

let rendering = false;
async function render() {
  if (rendering) return;
  rendering = true;
  const main = $('#main');
  $$('.chart-host', main).forEach(h => h.classList.add('stale'));   // hold, never flash
  try { await VIEWS[state.view].render(main); }
  catch (e) { console.error(e); }
  finally { rendering = false; wireCards(); }
}

/** Wire the per-card table-view toggles after each render. */
function wireCards() {
  $$('[data-table]').forEach(b => b.onclick = () => {
    const host = document.getElementById(b.dataset.table);
    if (!host || !host._table) return toast('No table view for this panel yet.');
    const on = !host._showTable;
    b.setAttribute('aria-pressed', on);
    b.title = on ? 'Switch to chart view' : 'Switch to table view';
    V.showTable(host, on);
  });
}

/* ==========================================================================
   Shared fragments
   ======================================================================== */
function tile(k, v, o = {}) {
  return `<div class="tile ${o.click ? 'clickable' : ''}" ${o.click ? `onclick="${o.click}"` : ''}>
    <div class="k">${esc(k)}${o.badge || ''}</div>
    <div class="v ${o.cls || ''} ${o.sm ? 'sm' : ''}">${v}</div>
    ${o.sub ? `<div class="d">${o.sub}</div>` : ''}
    ${o.spark ? `<div class="spark" id="${o.spark}"></div>` : ''}
  </div>`;
}
const pnlCls = n => n > 0 ? 'up' : n < 0 ? 'down' : '';
/** P&L always carries a sign glyph — colour never conveys it alone. */
const signed = (n, f = F.usd) => n == null ? '—'
  : `<span class="${pnlCls(n)}">${F.sig(n)} ${f(Math.abs(n))}</span>`;
const symLink = s => `<span class="sym" onclick="go('#/symbol/${encodeURIComponent(s)}')">${esc(s)}</span>`;

/** Just enough markdown for the digests the agent writes — escaped first, so the
    model's output can never inject markup. */
function md(src) {
  return esc(src)
    .replace(/^### (.*)$/gm, '<h4>$1</h4>')
    .replace(/^## (.*)$/gm, '<h3>$1</h3>')
    .replace(/^# (.*)$/gm, '<h3>$1</h3>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|\s)\*(?!\s)(.+?)(?<!\s)\*/g, '$1<em>$2</em>')
    .replace(/`(.+?)`/g, '<code>$1</code>')
    .replace(/^\s*[-•]\s+(.*)$/gm, '<li>$1</li>')
    .replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, '<ul>$1</ul>');
}

function statusBadge(m) {
  if (!m) return '<span class="badge">connecting…</span>';
  if (m.kill_switch) return '<span class="badge critical"><span class="dot"></span>Kill switch — trading halted</span>';
  if (m.reconcile_halt) return '<span class="badge warning"><span class="dot"></span>Reconcile halt</span>';
  return m.healthy
    ? '<span class="badge good"><span class="dot pulse"></span>Healthy</span>'
    : '<span class="badge warning"><span class="dot"></span>Unhealthy</span>';
}

/** Drawdown series from an equity curve — the risk half of the equity story. */
function drawdown(pts) {
  let peak = -Infinity;
  return pts.map(p => { peak = Math.max(peak, p.equity); return { x: p.x, y: peak > 0 ? (p.equity - peak) / peak : 0 }; });
}
const eqPoints = hist => (hist || []).map(p => ({ x: +new Date(p.ts), y: p.equity, equity: p.equity, ts: p.ts }))
  .filter(p => isFinite(p.x)).sort((a, b) => a.x - b.x);

/* ==========================================================================
   VIEW · Cockpit — state of the world, what needs me, what changed
   ======================================================================== */
async function viewCockpit(main) {
  const d = await pull({
    m: '/api/metrics', a: '/api/account', p: '/api/pnl', eq: '/api/equity',
    intel: '/api/intel', hb: '/api/heartbeats?limit=200', risk: '/api/portfolio_risk',
    cfg: '/api/config', opp: '/api/opportunities?limit=30', usage: '/api/usage?days=14',
  });
  const { m, a, p, cfg } = d;
  const eq = eqPoints(d.eq).filter(x => inRange(x.ts));
  const pending = (d.opp || []).filter(o => o.status === 'pending_approval');
  const lim = cfg && cfg.limits;
  // Broker down should degrade to the last journalled equity, not to a dash —
  // "I can't reach Alpaca" is not the same as "I don't know what you're worth".
  const live = a && a.available;
  const lastEq = eq.length ? eq[eq.length - 1] : null;
  const equityText = live ? F.usd(a.equity) : lastEq ? F.usd(lastEq.y) : '—';
  const equitySub = live ? '' : lastEq
    ? `<span class="badge warning"><span class="dot"></span>last known ${F.dt(lastEq.ts)}</span>`
    : 'no snapshot yet';

  main.innerHTML = `
    <div class="toolbar">
      ${rangeControl()}
      ${statusBadge(m)}
      ${m && m.mode === 'live' ? '<span class="badge live"><span class="dot"></span>LIVE MONEY</span>'
        : '<span class="badge accent">Paper</span>'}
      <span class="hint">${m ? esc(m.health) : ''}</span>
      <span class="spacer"></span>
      <span class="hint">${m && m.last_successful_cycle
        ? 'Last good cycle ' + F.dt(m.last_successful_cycle) : 'No successful cycle yet'}</span>
    </div>

    ${pending.length ? `<section class="card" style="border-color:var(--warning);margin-bottom:14px">
      <header><h3>Needs your decision</h3>
        <span class="hint">${pending.length} proposal${pending.length > 1 ? 's' : ''} waiting for approval</span></header>
      <div id="pendingBox"></div></section>` : ''}

    <div class="tiles" style="margin-bottom:14px">
      ${tile('Equity', equityText, { spark: 'eqSpark', sub: equitySub })}
      ${tile('Day P&L', a && a.available ? signed(a.daily_pl) : '—',
        { sub: a && a.available ? `${F.sig(a.daily_pl_pct)} ${F.num(Math.abs(a.daily_pl_pct || 0), 2)}% of equity` : '' })}
      ${tile('Unrealized', p ? signed(p.unrealized) : '—', { sub: `${a && a.available ? a.open_positions : 0} open positions` })}
      ${tile('Realized', p ? signed(p.realized) : '—', { sub: `${p ? p.closed_trades : 0} closed trades` })}
      ${tile('Buying power', a && a.available ? F.usdc(a.buying_power) : '—',
        { sub: a && a.available ? F.usdc(a.cash) + ' cash' : '' })}
      ${tile('Agent cost 24h', m ? F.usd(m.cost_24h_usd, 2) : '—',
        { sub: m ? `${m.cycles_24h} cycles run` : '' })}
      ${tile('Day trades', a && a.available ? `${a.daytrade_count}` : '—',
        { sub: lim && lim.pdt && lim.pdt.enforce ? `PDT cap ${lim.pdt.max_day_trades_per_5_days} / 5d` : 'PDT off' })}
      ${tile('Scale level', m ? `L${m.live_scale_level ?? 0}` : '—', { sub: 'capital ramp stage', sm: true })}
    </div>

    <div class="grid">
      ${card({ title: 'Equity curve', cls: 'col-8',
        hint: 'Drag to zoom every panel · double-click to reset' })}
      ${card({ title: 'Risk against your configured limits', cls: 'col-4',
        hint: 'bars fill toward the cap in limits.yaml' })}
      ${card({ title: 'Drawdown from peak', cls: 'col-8',
        hint: lim ? `circuit breaker halts at ${lim.portfolio.drawdown_circuit_pct}%` : '' })}
      ${card({ title: 'Daemon liveness', cls: 'col-4', chart: false,
        body: '<div id="hbStrip" class="strip"></div><div id="hbNote" class="hint" style="margin-top:9px"></div>' })}
      ${card({ title: 'Agent spend', cls: 'col-4', hint: 'by cycle type' })}
      ${card({ title: 'Latest market digest', cls: 'col-8', chart: false,
        actions: '<button onclick="go(\'#/markets\')">Open markets</button>',
        body: `<div class="prose md" id="digest">${d.intel && d.intel.digest
          ? md(d.intel.digest.slice(0, 3000)) : 'No digest yet — run a premarket cycle.'}</div>` })}
    </div>`;

  const hosts = $$('.chart-host', main);
  V.spark($('#eqSpark'), eq.map(p => p.y));

  V.line(hosts[0], {
    height: 300, area: true, series: [{ name: 'Equity', points: eq }],
    empty: 'Equity snapshots build as cycles run and you view this page.',
    yKind: 'usd', xTipFmt: F.dt,
    onBrush: (a2, b2) => { state.brush = a2 == null ? null : [a2, b2]; render(); },
  });

  if (d.risk && d.risk.available && lim) {
    const eqv = (a && a.equity) || 1;
    V.bullets(hosts[1], [
      { label: 'Gross exposure', value: d.risk.gross_exposure, cap: eqv * lim.portfolio.max_gross_exposure_pct / 100,
        fmt: F.usdc, note: `${F.num(d.risk.gross_leverage, 2)}× equity` },
      { label: 'Net delta', value: Math.abs(d.risk.net_delta_dollars),
        cap: lim.portfolio.max_net_delta_pct ? eqv * lim.portfolio.max_net_delta_pct / 100 : 0,
        fmt: F.usdc, note: lim.portfolio.max_net_delta_pct ? '' : 'net-delta cap disabled (0)' },
      { label: 'Largest position', value: d.risk.largest_position_pct * eqv,
        cap: eqv * lim.position.max_position_pct / 100, fmt: F.usdc,
        note: `${F.pct(d.risk.largest_position_pct, 1)} of equity` },
      { label: 'Open positions', value: d.risk.n_positions, cap: lim.position.max_open_positions,
        fmt: F.int },
      { label: 'Day loss vs kill switch', value: Math.max(0, -(a && a.daily_pl || 0)),
        cap: eqv * lim.loss_kill_switch.max_daily_loss_pct / 100, fmt: F.usdc,
        note: 'breach cancels orders and blocks new proposals' },
    ]);
  } else {
    hosts[1].innerHTML = '<p class="empty">Broker unavailable — check ALPACA keys in .env.</p>';
  }

  V.line(hosts[2], {
    height: 190, area: true, areaBase: 0, zeroBase: true,
    series: [{ name: 'Drawdown', points: drawdown(eq), color: V.tok('--down') }],
    yKind: 'pct', empty: 'Not enough equity history yet.',
    bands: lim ? [{ from: -lim.portfolio.drawdown_circuit_pct / 100, to: -1, color: V.tok('--critical') }] : [],
  });

  // Liveness: one cell per heartbeat, newest right. Colour + a written note.
  const hb = (d.hb || []).slice().reverse();
  $('#hbStrip').innerHTML = hb.slice(-90).map(h => {
    const k = h.status === 'error' ? 'err' : h.status === 'warn' ? 'warn' : 'ok';
    return `<i class="${k}" style="height:${k === 'ok' ? 60 : 100}%" title="${esc(h.ts)} ${esc(h.job)} — ${esc(h.status)}"></i>`;
  }).join('') || '<span class="muted">no heartbeats yet</span>';
  const errs = hb.filter(h => h.status === 'error').length;
  $('#hbNote').innerHTML = hb.length
    ? `${hb.length} heartbeats · <span class="${errs ? 'down' : 'up'}">${errs} error${errs === 1 ? '' : 's'}</span> · newest ${F.dt(hb[hb.length - 1].ts)}`
    : 'The daemon has not checked in yet.';

  const byCycle = {};
  (d.usage && d.usage.rows || []).forEach(r => {
    byCycle[r.cycle || 'other'] = (byCycle[r.cycle || 'other'] || 0) + (r.cost_usd || 0);
  });
  V.bar(hosts[3], {
    data: Object.entries(byCycle).map(([k, v]) => ({ label: k, value: v })).sort((x, y) => y.value - x.value),
    fmt: v => F.usd(v, 2), measure: 'Spend (14d)', dimension: 'Cycle',
    empty: 'No agent spend recorded yet.', height: 190,
  });

  if (pending.length) {
    table($('#pendingBox'), [
      { k: 'proposal_id', label: '#', fmt: r => `<a onclick="go('#/decision/${r.proposal_id}')">#${r.proposal_id}</a>` },
      { k: 'symbol', label: 'Symbol', fmt: r => symLink(r.symbol) },
      { k: 'side', label: 'Order', fmt: r => `${esc(r.side)} ${F.num(r.qty)}` },
      { k: 'expected_edge_usd', label: 'Expected edge', r: true, fmt: r => F.usd(r.expected_edge_usd) },
      { k: 'thesis', label: 'Thesis', fmt: r => clip(r.thesis) },
      { k: '_a', label: 'Decide', fmt: r => `<div class="row">
          <button class="primary" onclick="approve(${r.proposal_id})">Approve</button>
          <button class="danger" onclick="deny(${r.proposal_id})">Deny</button></div>` },
    ], pending);
  }
}

function rangeControl() {
  return `<div class="seg" role="group" aria-label="Time range">${
    Object.keys(RANGE_DAYS).map(k => `<button data-range="${k}"
      aria-pressed="${state.range === k && !state.brush}"
      onclick="setRange('${k}')">${k}</button>`).join('')
  }</div>${state.brush ? `<button onclick="setRange('${state.range}')" title="Clear zoom">
    zoomed · ${F.date(state.brush[0])}–${F.date(state.brush[1])} ✕</button>` : ''}`;
}
function setRange(k) { state.range = k; localStorage.range = k; state.brush = null; render(); }

/* ==========================================================================
   VIEW · Portfolio
   ======================================================================== */
async function viewPortfolio(main) {
  const d = await pull({
    pos: '/api/positions', risk: '/api/portfolio_risk', a: '/api/account',
    cfg: '/api/config', trades: '/api/trades?limit=120',
  });
  const positions = (d.pos && d.pos.positions) || [];
  const lots = (d.pos && d.pos.lots) || [];
  const eqv = (d.a && d.a.equity) || 1;
  const lim = d.cfg && d.cfg.limits;
  const totalUnreal = positions.reduce((s, p) => s + (p.unrealized_pl || 0), 0);
  const gross = positions.reduce((s, p) => s + Math.abs(p.market_value || 0), 0);

  main.innerHTML = `
    <div class="tiles" style="margin-bottom:14px">
      ${tile('Positions', positions.length, { sub: lim ? `cap ${lim.position.max_open_positions}` : '' })}
      ${tile('Gross exposure', F.usdc(gross), { sub: `${F.num(gross / eqv, 2)}× equity` })}
      ${tile('Unrealized P&L', signed(totalUnreal))}
      ${tile('Net delta', d.risk && d.risk.available ? F.usdc(d.risk.net_delta_dollars) : '—',
        { sub: 'per 1-unit move' })}
      ${tile('Portfolio beta', d.risk && d.risk.available ? F.num(d.risk.portfolio_beta, 2) : '—')}
      ${tile('Net vega', d.risk && d.risk.available ? F.usdc(d.risk.net_vega) : '—',
        { sub: 'per 1 vol point' })}
    </div>
    <div class="grid">
      ${card({ title: 'Exposure by position', cls: 'col-7',
        hint: 'signed market value — click a bar to drill in' })}
      ${card({ title: 'Risk vs limits', cls: 'col-5' })}
      ${card({ title: 'Open positions', cls: 'col-12', chart: false,
        hint: 'click a symbol for its full history', body: '<div id="posT"></div>' })}
      ${card({ title: 'Tax lots', cls: 'col-7', chart: false,
        hint: 'holding period drives the after-tax number', body: '<div id="lotT"></div>' })}
      ${card({ title: 'Days to long-term treatment', cls: 'col-5',
        hint: 'closing before 0 is taxed at the short-term rate' })}
      ${card({ title: 'Orders & fills', cls: 'col-12', chart: false, body: '<div id="ordT"></div>' })}
    </div>`;

  const hosts = $$('.chart-host', main);
  V.bar(hosts[0], {
    data: positions.map(p => ({ label: p.symbol, value: p.market_value,
      note: `unrealized ${F.usd(p.unrealized_pl)}` })).sort((a, b) => Math.abs(b.value) - Math.abs(a.value)),
    diverging: true, fmt: F.usdc, measure: 'Market value', dimension: 'Position',
    empty: 'No open positions.', onPick: p => go('#/symbol/' + encodeURIComponent(p.label)),
  });

  if (d.risk && d.risk.available && lim) {
    V.bullets(hosts[1], [
      { label: 'Gross leverage', value: d.risk.gross_leverage, cap: lim.portfolio.max_gross_exposure_pct / 100,
        fmt: v => F.num(v, 2) + '×' },
      { label: 'Largest position', value: d.risk.largest_position_pct * 100,
        cap: lim.position.max_position_pct, fmt: v => F.num(v, 1) + '%' },
      { label: 'Positions', value: d.risk.n_positions, cap: lim.position.max_open_positions, fmt: F.int },
      { label: 'Net exposure', value: Math.abs(d.risk.net_exposure), cap: eqv, fmt: F.usdc,
        note: `${F.num(d.risk.net_leverage, 2)}× equity, ${d.risk.net_exposure >= 0 ? 'net long' : 'net short'}` },
    ]);
  } else hosts[1].innerHTML = '<p class="empty">Broker unavailable.</p>';

  table($('#posT'), [
    { k: 'symbol', label: 'Symbol', fmt: r => symLink(r.symbol) },
    { sec: true, k: 'asset_class', label: 'Class' },
    { k: 'qty', label: 'Qty', r: true, fmt: r => F.num(r.qty) },
    { k: 'avg_entry_price', label: 'Entry', r: true, fmt: r => F.usd(r.avg_entry_price) },
    { k: 'market_value', label: 'Market value', r: true, fmt: r => F.usd(r.market_value) },
    { k: '_w', label: '% equity', r: true, sortVal: r => Math.abs(r.market_value) / eqv,
      fmt: r => F.pct(Math.abs(r.market_value) / eqv, 1) },
    { k: 'unrealized_pl', label: 'Unrealized', r: true, fmt: r => signed(r.unrealized_pl) },
    { k: '_r', label: 'Return', r: true, sortVal: r => r.unrealized_pl / (Math.abs(r.market_value) || 1),
      fmt: r => signed(r.unrealized_pl / (Math.abs(r.market_value - r.unrealized_pl) || 1), v => F.pct(v, 2)) },
  ], positions, { empty: d.pos && d.pos.available ? 'Flat — no open positions.' : 'Broker unavailable.' });

  table($('#lotT'), [
    { k: 'lot_id', label: 'Lot', fmt: r => '#' + r.lot_id },
    { k: 'symbol', label: 'Symbol', fmt: r => symLink(r.symbol) },
    { k: 'qty', label: 'Qty', r: true, fmt: r => F.num(r.qty) },
    { k: 'open_price', label: 'Open', r: true, fmt: r => F.usd(r.open_price) },
    { k: 'holding_days', label: 'Held', r: true, fmt: r => r.holding_days + 'd' },
    { k: 'days_to_long_term', label: 'To long-term', r: true,
      fmt: r => r.days_to_long_term === 0 ? '<span class="badge good">long-term</span>' : r.days_to_long_term + 'd' },
  ], lots, { empty: 'No open tax lots.' });

  V.bar(hosts[2], {
    data: lots.map(l => ({ label: l.symbol + ' #' + l.lot_id, value: l.days_to_long_term })),
    fmt: v => v === 0 ? 'long-term' : v + 'd', measure: 'Days remaining', dimension: 'Lot',
    empty: 'No open lots.',
  });

  const orders = d.trades || [];
  table($('#ordT'), [
    { k: 'ts', label: 'Time', fmt: r => F.dt(r.ts), sortVal: r => +new Date(r.ts) },
    { k: 'symbol', label: 'Symbol', fmt: r => symLink(r.symbol) },
    { k: 'side', label: 'Side', fmt: r => `<span class="badge ${r.side === 'buy' ? 'accent' : ''}">${esc(r.side)}</span>` },
    { k: 'qty', label: 'Qty', r: true, fmt: r => F.num(r.qty) },
    { sec: true, k: 'order_type', label: 'Type' },
    { k: 'limit_price', label: 'Limit', r: true, fmt: r => F.usd(r.limit_price) },
    { k: 'status', label: 'Status', fmt: r => `<span class="badge ${
      r.status === 'filled' ? 'good' : r.status === 'canceled' ? '' : 'accent'}">${esc(r.status)}</span>` },
    { k: '_f', label: 'Fills', fmt: r => (r.fills || []).map(f =>
      `${F.num(f.qty)} @ ${F.usd(f.price)}`).join(', ') || '<span class="muted">—</span>' },
    { sec: true, k: '_s', label: 'Slippage', r: true, sortVal: r => (r.fills || [])[0]?.slippage_bps,
      fmt: r => { const s = (r.fills || [])[0]; return s && s.slippage_bps != null
        ? F.num(s.slippage_bps, 1) + ' bps' : '—'; } },
  ], orders, { sort: { k: 'ts', dir: 'desc' }, empty: 'No orders yet.' });
}

/* ==========================================================================
   VIEW · Markets — price charts, signals, news
   ======================================================================== */
async function viewMarkets(main) {
  const d = await pull({
    sig: '/api/signals?limit=400', pos: '/api/positions', watch: '/api/watchlist',
    news: '/api/news?limit=60', sent: '/api/sentiment', intel: '/api/intel',
  });
  const snaps = d.sig || [];
  const universe = [...new Set([
    ...((d.pos && d.pos.positions) || []).map(p => p.symbol),
    ...snaps.map(s => s.symbol),
  ])].filter(Boolean).sort();
  if (!state.symbol && universe.length) state.symbol = universe[0];

  // latest snapshot per symbol
  const latest = {};
  snaps.forEach(s => { if (!latest[s.symbol] || s.ts > latest[s.symbol].ts) latest[s.symbol] = s; });
  const rows = Object.values(latest);

  main.innerHTML = `
    <div class="toolbar">
      <label for="symSel">Symbol</label>
      <select id="symSel">${universe.map(s =>
        `<option ${s === state.symbol ? 'selected' : ''}>${esc(s)}</option>`).join('')
        || '<option>—</option>'}</select>
      <div class="seg" role="group" aria-label="Timeframe">
        ${['1Day', '1Hour', '15Min'].map(t => `<button data-tf="${t}"
          aria-pressed="${(state.tf || '1Day') === t}">${t}</button>`).join('')}
      </div>
      <button onclick="go('#/symbol/'+encodeURIComponent(state.symbol))">Full symbol sheet →</button>
      <span class="spacer"></span>
      <label for="heatSel">Heatmap metric</label>
      <select id="heatSel">
        <option value="momentum_20">20d momentum</option>
        <option value="realized_vol">Realized vol</option>
        <option value="atr_pct">ATR %</option>
        <option value="dist_from_high">Distance off high</option>
        <option value="_iv_rank">IV rank</option>
        <option value="_sentiment">News sentiment</option>
      </select>
    </div>
    <div class="grid">
      ${card({ title: `${esc(state.symbol || '')} price`, cls: 'col-8',
        hint: 'candles with 20 & 50-period moving averages' })}
      ${card({ title: 'Live quote & signals', cls: 'col-4', chart: false, body: '<div id="quoteBox"></div>' })}
      ${card({ title: 'Signal heatmap', cls: 'col-12',
        hint: 'every symbol the agent tracked, over time — click a row to open it' })}
      ${card({ title: 'Latest signal snapshot', cls: 'col-12', chart: false, body: '<div id="sigT"></div>' })}
      ${card({ title: 'News sentiment', cls: 'col-5' })}
      ${card({ title: 'Watchlist', cls: 'col-7', chart: false,
        body: `<div class="prose md">${d.watch && d.watch.markdown
          ? md(d.watch.markdown) : 'No watchlist yet — run a premarket cycle.'}</div>` })}
      ${card({ title: 'News feed', cls: 'col-12', chart: false, body: '<div id="newsT"></div>' })}
    </div>`;

  $('#symSel').onchange = e => { state.symbol = e.target.value; localStorage.symbol = state.symbol; render(); };
  $$('[data-tf]').forEach(b => b.onclick = () => { state.tf = b.dataset.tf; render(); });
  $('#heatSel').value = state.heatMetric;
  $('#heatSel').onchange = e => { state.heatMetric = e.target.value; render(); };

  const hosts = $$('.chart-host', main);
  if (state.symbol) {
    const bars = await api(`/api/bars/${encodeURIComponent(state.symbol)}?days=180&timeframe=${state.tf || '1Day'}`)
      .catch(() => null);
    V.candles(hosts[0], {
      bars: (bars && bars.bars) || [], overlays: [{ period: 20 }, { period: 50 }],
      empty: bars && bars.source === 'none'
        ? 'No price history — the broker connection is unavailable, so bars cannot be fetched.'
        : 'No bars cached for this symbol yet.',
    });
  }

  const q = latest[state.symbol];
  $('#quoteBox').innerHTML = q ? `<div class="tiles" style="grid-template-columns:repeat(2,1fr)">
      ${tile('Last', F.usd(q.last), { sm: true })}
      ${tile('Spread', q.spread_bps == null ? '—' : F.num(q.spread_bps, 1) + ' bps', { sm: true })}
      ${tile('20d momentum', q.features ? signed(q.features.momentum_20, v => F.pct(v, 1)) : '—', { sm: true })}
      ${tile('Realized vol', q.features ? F.pct(q.features.realized_vol, 0) : '—', { sm: true })}
      ${tile('ATM IV', F.pct(q.atm_iv, 0), { sm: true })}
      ${tile('IV rank', q.iv_rank == null ? '—' : F.num(q.iv_rank, 0) + '%', { sm: true })}
      ${tile('Sentiment', q.sentiment == null ? '—' : signed(q.sentiment, v => F.num(v, 2)), { sm: true })}
      ${tile('Mentions', q.mention_count ?? '—', { sm: true })}
    </div><div class="hint" style="margin-top:10px">As of ${F.dt(q.ts)}</div>`
    : '<p class="empty">No snapshot for this symbol yet.</p>';

  // Heatmap: symbols × recent cycles
  const metric = state.heatMetric;
  const val = s => metric === '_iv_rank' ? (s.iv_rank == null ? null : s.iv_rank / 100)
    : metric === '_sentiment' ? s.sentiment
    : (s.features ? s.features[metric] : null);
  const bySym = {};
  snaps.filter(s => inRange(s.ts)).forEach(s => (bySym[s.symbol] = bySym[s.symbol] || []).push(s));
  const stamps = [...new Set(snaps.filter(s => inRange(s.ts)).map(s => s.ts))].sort().slice(-24);
  const heatRows = Object.entries(bySym).sort().map(([sym, list]) => ({
    label: sym,
    cells: stamps.map(t => {
      const hit = list.find(s => s.ts === t) || list.reduce((a, b) =>
        Math.abs(+new Date(b.ts) - +new Date(t)) < Math.abs(+new Date(a.ts) - +new Date(t)) ? b : a, list[0]);
      return { label: F.dt(t), v: hit ? val(hit) : null };
    }),
  }));
  V.heat(hosts[1], {
    rows: heatRows, cols: stamps.map(t => F.date(t)),
    diverging: metric === 'momentum_20' || metric === '_sentiment' || metric === 'dist_from_high',
    fmt: v => metric === '_sentiment' ? F.num(v, 2) : F.pct(v, 1),
    measure: $('#heatSel').selectedOptions[0].text,
    onPick: sym => { state.symbol = sym; render(); },
    empty: 'No signal snapshots in this range — widen the time range or run a cycle.',
  });

  const pf = (r, k) => r.features ? r.features[k] : null;
  table($('#sigT'), [
    { k: 'symbol', label: 'Symbol', fmt: r => symLink(r.symbol) },
    { k: 'last', label: 'Last', r: true, fmt: r => F.usd(r.last) },
    { k: 'spread_bps', label: 'Spread', r: true, fmt: r => r.spread_bps == null ? '—' : F.num(r.spread_bps, 1) + ' bps' },
    { k: '_m', label: '20d mom', r: true, sortVal: r => pf(r, 'momentum_20'),
      fmt: r => signed(pf(r, 'momentum_20'), v => F.pct(v, 1)) },
    { k: '_v', label: 'Vol', r: true, sortVal: r => pf(r, 'realized_vol'), fmt: r => F.pct(pf(r, 'realized_vol'), 0) },
    { sec: true, k: '_a', label: 'ATR', r: true, sortVal: r => pf(r, 'atr_pct'), fmt: r => F.pct(pf(r, 'atr_pct'), 1) },
    { sec: true, k: '_h', label: 'Off high', r: true, sortVal: r => pf(r, 'dist_from_high'),
      fmt: r => signed(pf(r, 'dist_from_high'), v => F.pct(v, 1)) },
    { sec: true, k: 'atm_iv', label: 'ATM IV', r: true, fmt: r => F.pct(r.atm_iv, 0) },
    { k: 'iv_rank', label: 'IV rank', r: true, fmt: r => r.iv_rank == null ? '—' : F.num(r.iv_rank, 0) + '%' },
    { sec: true, k: 'pc_skew', label: 'P/C skew', r: true, fmt: r => signed(r.pc_skew, v => F.pct(v, 1)) },
    { k: 'sentiment', label: 'Sentiment', r: true, fmt: r => signed(r.sentiment, v => F.num(v, 2)) },
    { sec: true, k: 'ts', label: 'As of', fmt: r => F.dt(r.ts), sortVal: r => +new Date(r.ts) },
  ], rows, { sort: { k: 'symbol', dir: 'asc' }, empty: 'No signal snapshots yet.' });

  V.bar(hosts[2], {
    data: (d.sent || []).map(s => ({ label: s.symbol, value: s.polarity,
      note: `${s.mention_count} mentions` })),
    diverging: true, fmt: v => F.num(v, 2), measure: 'Polarity', dimension: 'Symbol',
    empty: 'No sentiment recorded yet.', onPick: p => { state.symbol = p.label; render(); },
  });

  table($('#newsT'), [
    { k: 'ts', label: 'Time', fmt: r => F.dt(r.ts), sortVal: r => +new Date(r.ts) },
    { k: 'symbol', label: 'Symbol', fmt: r => r.symbol ? symLink(r.symbol) : '—' },
    { k: 'headline', label: 'Headline', fmt: r => r.url
      ? `<a href="${esc(r.url)}" target="_blank" rel="noopener" title="${esc(r.headline)}"
           class="clip">${esc(r.headline)}</a>` : clip(r.headline, 110) },
    { sec: true, k: 'source', label: 'Source', fmt: r => `<span class="muted">${esc(r.source || '')}</span>` },
  ], d.news || [], { sort: { k: 'ts', dir: 'desc' }, empty: 'No news ingested yet.' });
}

/* ==========================================================================
   VIEW · Agent mind — the funnel, the constraints, the narrative
   ======================================================================== */
async function viewMind(main) {
  const days = RANGE_DAYS[state.range];
  const d = await pull({
    funnel: `/api/funnel?days=${days}`, verd: `/api/verdicts?days=${days}`,
    cyc: '/api/cycle-log?limit=80', opp: '/api/opportunities?limit=80',
    dec: '/api/decisions?limit=60', m: '/api/metrics',
  });
  const opp = d.opp || [];
  const byStatus = {};
  opp.forEach(o => byStatus[o.status] = (byStatus[o.status] || 0) + 1);

  main.innerHTML = `
    <div class="toolbar">
      ${rangeControl()}
      <input id="askBox" placeholder="Ask the record: why did you pass on NVDA?" style="min-width:min(340px,100%)">
      <button class="primary" onclick="ask()">Search reasoning</button>
      <span class="spacer"></span>
      <span class="hint">${opp.length} proposals · ${(d.cyc || []).length} logged cycles</span>
    </div>
    <div id="askOut"></div>
    <div class="grid">
      ${card({ title: 'Decision funnel', cls: 'col-5',
        hint: `last ${days}d — the biggest drop is your real bottleneck` })}
      ${card({ title: 'Which guardrails actually fire', cls: 'col-7',
        hint: 'rules that vetoed or flagged a proposal' })}
      ${card({ title: 'Proposal outcomes', cls: 'col-5' })}
      ${card({ title: 'Cycle activity — what it examined each interval', cls: 'col-7', chart: false,
        hint: 'includes no-trade cycles', body: '<div id="cycT"></div>' })}
      ${card({ title: 'Opportunities considered', cls: 'col-12', chart: false,
        hint: 'every proposal, its edge, and the verdict that decided it',
        body: '<div id="oppT"></div>' })}
      ${card({ title: 'Recent guardrail verdicts', cls: 'col-12', chart: false, body: '<div id="verdT"></div>' })}
    </div>`;

  const hosts = $$('.chart-host', main);
  V.funnel(hosts[0], { stages: (d.funnel && d.funnel.stages) || [] });

  const rules = ((d.verd && d.verd.by_rule) || []).reduce((acc, r) => {
    const k = r.rule || '(unnamed)';
    acc[k] = acc[k] || { label: k, value: 0, veto: 0 };
    acc[k].value += r.n;
    if (r.verdict === 'veto') acc[k].veto += r.n;
    return acc;
  }, {});
  V.bar(hosts[1], {
    data: Object.values(rules).sort((a, b) => b.value - a.value).slice(0, 12)
      .map(r => ({ ...r, note: `${r.veto} veto${r.veto === 1 ? '' : 's'}`,
        color: r.veto ? V.tok('--critical') : V.series(0) })),
    fmt: F.int, measure: 'Times fired', dimension: 'Rule',
    empty: 'No guardrail verdicts recorded in this range.',
  });

  V.bar(hosts[2], {
    data: Object.entries(byStatus).map(([k, v]) => ({ label: k.replace(/_/g, ' '), value: v,
      color: k === 'submitted' ? V.tok('--good') : k === 'vetoed' ? V.tok('--critical')
        : k === 'pending_approval' ? V.tok('--warning') : V.series(0) })),
    fmt: F.int, measure: 'Proposals', dimension: 'Status', empty: 'No proposals yet.',
  });

  const cyc = (d.cyc || []).map((r, i) => ({ ...r, _i: i }));
  window._CYC = cyc;
  table($('#cycT'), [
    { k: 'ts', label: 'Time', sortVal: r => +new Date(r.ts),
      fmt: r => `<a onclick="cycleSheet(${r._i})">${F.dt(r.ts)}</a>` },
    { k: '_sym', label: 'Examined', fmt: r => (r.symbols_examined || []).map(symLink).join(' ') || '<span class="muted">—</span>' },
    { sec: true, k: 'tool_count', label: 'Tools', r: true },
    { k: 'cost_usd', label: 'Cost', r: true, fmt: r => F.usd(r.cost_usd, 3) },
    { k: 'summary', label: 'Summary', fmt: r => clip(r.summary, 110) },
  ], cyc, { sort: { k: 'ts', dir: 'desc' }, empty: 'No cycles logged yet.' });

  table($('#oppT'), [
    { k: 'proposal_id', label: '#', fmt: r => `<a onclick="go('#/decision/${r.proposal_id}')">#${r.proposal_id}</a>` },
    { k: 'ts', label: 'Time', fmt: r => F.dt(r.ts), sortVal: r => +new Date(r.ts) },
    { k: 'symbol', label: 'Symbol', fmt: r => symLink(r.symbol) },
    { k: 'strategy_tag', label: 'Strategy' },
    { k: 'side', label: 'Order', fmt: r => `${esc(r.side)} ${F.num(r.qty)}` },
    { k: 'expected_edge_usd', label: 'Expected edge', r: true, fmt: r => F.usd(r.expected_edge_usd) },
    { sec: true, k: 'confidence', label: 'Conf', r: true, fmt: r => r.confidence == null ? '—' : F.num(r.confidence, 2) },
    { k: 'status', label: 'Status', fmt: r => `<span class="badge ${
      r.status === 'submitted' ? 'good' : r.status === 'vetoed' ? 'critical'
        : r.status === 'pending_approval' ? 'warning' : ''}">${esc((r.status || '').replace(/_/g, ' '))}</span>` },
    { k: 'reason', label: 'Deciding reason', fmt: r => clip(r.reason || r.thesis, 110) },
  ], opp, { sort: { k: 'ts', dir: 'desc' },
    empty: 'No proposals yet — the Cycle activity panel shows what each interval examined.' });

  table($('#verdT'), [
    { k: 'ts', label: 'Time', fmt: r => F.dt(r.ts), sortVal: r => +new Date(r.ts) },
    { k: 'proposal_id', label: 'Proposal', fmt: r => `<a onclick="go('#/decision/${r.proposal_id}')">#${r.proposal_id}</a>` },
    { sec: true, k: 'source', label: 'Source' },
    { k: 'verdict', label: 'Verdict', fmt: r => `<span class="badge ${
      r.verdict === 'veto' ? 'critical' : r.verdict === 'pass' ? 'good' : 'warning'}">${esc(r.verdict)}</span>` },
    { k: 'rule', label: 'Rule' },
    { k: 'reason', label: 'Reason', fmt: r => clip(r.reason, 110) },
  ], (d.verd && d.verd.recent) || [], { sort: { k: 'ts', dir: 'desc' }, empty: 'No verdicts recorded.' });

  $('#askBox').onkeydown = e => { if (e.key === 'Enter') ask(); };
}

async function ask() {
  const q = $('#askBox').value.trim();
  if (!q) return;
  const rows = await api('/api/query?q=' + encodeURIComponent(q)).catch(() => []);
  $('#askOut').innerHTML = `<section class="card" style="margin-bottom:14px">
    <header><h3>Answer from the decision record</h3>
      <span class="hint">${rows.length} match${rows.length === 1 ? '' : 'es'} for “${esc(q)}”</span>
      <span class="head-actions"><button onclick="$('#askOut').innerHTML=''">Clear</button></span></header>
    ${rows.length ? rows.map(r => `<div style="border-top:1px solid var(--hairline);padding:12px 0">
      <a onclick="go('#/decision/${r.proposal_id}')"><b>${esc(r.summary)}</b></a>
      <div class="prose" style="max-height:190px;margin-top:6px">${esc(r.reasoning || '(no reasoning captured)')}</div>
    </div>`).join('') : '<div class="empty">Nothing in the record matches that. Try a symbol or strategy name.</div>'}
  </section>`;
}

/* ==========================================================================
   VIEW · Performance
   ======================================================================== */
async function viewPerformance(main) {
  const d = await pull({
    perf: '/api/performance', pnl: '/api/pnl', eq: '/api/equity', edge: '/api/edge',
    usage: `/api/usage?days=${RANGE_DAYS[state.range]}`, pos: '/api/positions',
  });
  const eq = eqPoints(d.eq).filter(x => inRange(x.ts));
  const strat = Object.entries((d.perf && d.perf.per_strategy) || {})
    .map(([tag, s]) => ({ tag, ...s }));
  const spend = (d.usage && d.usage.total_usd) || 0;
  const net = ((d.pnl && d.pnl.realized) || 0) - spend;

  main.innerHTML = `
    <div class="toolbar">${rangeControl()}
      <span class="hint">Costs are the agent's API spend — the edge has to clear it.</span></div>
    <div class="tiles" style="margin-bottom:14px">
      ${tile('Total P&L', d.pnl ? signed(d.pnl.total) : '—')}
      ${tile('Realized', d.pnl ? signed(d.pnl.realized) : '—', { sub: `${d.pnl ? d.pnl.closed_trades : 0} closed` })}
      ${tile('Unrealized', d.pnl ? signed(d.pnl.unrealized) : '—')}
      ${tile('Agent spend', F.usd(spend, 2), { sub: `${state.range} window` })}
      ${tile('Net of agent cost', signed(net), { sub: 'realized P&L − API spend' })}
      ${tile('Strategies live', strat.length, { sub: strat.filter(s => s.stage === 'live').length + ' at live stage' })}
    </div>
    <div class="grid">
      ${card({ title: 'Equity', cls: 'col-7' })}
      ${card({ title: 'Drawdown', cls: 'col-5' })}
      ${card({ title: 'Expectancy per strategy', cls: 'col-6',
        hint: 'average $ per trade, after tax where known' })}
      ${card({ title: 'Trades vs expectancy', cls: 'col-6',
        hint: 'sample size on x — anything left of ~30 trades is noise, not edge' })}
      ${card({ title: 'Strategy detail', cls: 'col-12', chart: false, body: '<div id="stratT"></div>' })}
      ${card({ title: 'Capital allocation', cls: 'col-6', chart: false, body: '<div id="allocT"></div>' })}
      ${card({ title: 'Agent spend over time', cls: 'col-6', hint: 'stacked by cycle' })}
      ${card({ title: 'Edge vs benchmark', cls: 'col-12', chart: false, body: '<div id="edgeBox"></div>' })}
    </div>`;

  const hosts = $$('.chart-host', main);
  V.line(hosts[0], { height: 260, area: true, series: [{ name: 'Equity', points: eq }],
    yKind: 'usd', onBrush: (a, b) => { state.brush = a == null ? null : [a, b]; render(); } });
  V.line(hosts[1], { height: 260, area: true, areaBase: 0, zeroBase: true,
    series: [{ name: 'Drawdown', points: drawdown(eq), color: V.tok('--down') }], yKind: 'pct' });

  V.bar(hosts[2], {
    data: strat.map(s => ({ label: s.tag, value: s.after_tax_expectancy ?? s.expectancy,
      note: `${s.trades} trades · ${F.pct(s.win_rate || 0, 0)} win rate` })),
    diverging: true, fmt: v => F.usd(v, 2), measure: 'Expectancy / trade', dimension: 'Strategy',
    empty: 'No closed trades scored yet.',
  });
  V.scatter(hosts[3], {
    points: strat.map(s => ({ label: s.tag, x: s.trades, y: s.after_tax_expectancy ?? s.expectancy,
      value: s.after_tax_expectancy ?? s.expectancy })),
    xLabel: 'Closed trades', yLabel: 'Expectancy', xFmt: F.int, yFmt: v => F.usd(v, 2),
    empty: 'No scored strategies yet.',
  });

  table($('#stratT'), [
    { k: 'tag', label: 'Strategy' },
    { k: 'stage', label: 'Stage', fmt: r => `<span class="badge ${
      r.stage === 'live' ? 'good' : r.stage === 'paper' ? 'accent' : ''}">${esc(r.stage || '—')}</span>` },
    { k: 'trades', label: 'Trades', r: true },
    { k: 'win_rate', label: 'Win rate', r: true, fmt: r => F.pct(r.win_rate || 0, 0) },
    { k: 'expectancy', label: 'Expectancy', r: true, fmt: r => signed(r.expectancy, v => F.usd(v, 2)) },
    { k: 'after_tax_expectancy', label: 'After tax', r: true, fmt: r => signed(r.after_tax_expectancy, v => F.usd(v, 2)) },
    { k: 'gross_pnl', label: 'Gross P&L', r: true, fmt: r => signed(r.gross_pnl) },
    { sec: true, k: 'max_drawdown', label: 'Max DD', r: true, fmt: r => F.usd(r.max_drawdown) },
  ], strat, { sort: { k: 'gross_pnl', dir: 'desc' }, empty: 'No strategy has closed a trade yet.' });

  table($('#allocT'), [
    { k: 'tag', label: 'Strategy' },
    { k: 'weight', label: 'Weight', r: true, fmt: r => F.pct(r.weight, 1) },
    { k: 'after_tax_expectancy', label: 'After-tax exp', r: true, fmt: r => F.usd(r.after_tax_expectancy, 2) },
    { k: 'trades', label: 'Trades', r: true },
    { k: 'confidence', label: 'Confidence' },
  ], (d.perf && d.perf.allocation) || [], { empty: 'No allocation computed yet.' });

  // Spend over time, stacked by cycle
  const buckets = {};
  ((d.usage && d.usage.rows) || []).forEach(r => {
    const day = (r.ts || '').slice(0, 10);
    buckets[day] = buckets[day] || { label: F.date(day) };
    const k = r.cycle || 'other';
    buckets[day][k] = (buckets[day][k] || 0) + (r.cost_usd || 0);
  });
  const keys = [...new Set(((d.usage && d.usage.rows) || []).map(r => r.cycle || 'other'))];
  V.stack(hosts[4], { rows: Object.keys(buckets).sort().map(k => buckets[k]), keys,
    height: 240, fmt: v => F.usd(v, 2), empty: 'No agent spend recorded in this range.' });

  const e = d.edge;
  $('#edgeBox').innerHTML = e ? `
    <div class="tiles" style="margin-bottom:12px">
      ${tile('Portfolio edge', e.portfolio && e.portfolio.edge_bps != null
        ? F.num(e.portfolio.edge_bps, 1) + ' bps' : '—', { sm: true })}
      ${tile('Benchmark', e.benchmark && e.benchmark.benchmark || '—', { sm: true,
        sub: e.benchmark && e.benchmark.note ? esc(e.benchmark.note) : '' })}
      ${tile('Excess return', e.benchmark && e.benchmark.excess_return != null
        ? signed(e.benchmark.excess_return, v => F.pct(v, 2)) : '—', { sm: true })}
    </div>
    <pre class="raw">${esc(JSON.stringify(e, null, 2)).slice(0, 4000)}</pre>` : '<p class="empty">Edge stats unavailable.</p>';
}

/* ==========================================================================
   VIEW · Control — schema-driven config, actions, health
   ======================================================================== */
let CFG = { section: 'limits', data: null, original: null, schema: null, help: {} };

async function viewControl(main) {
  const d = await pull({ cfg: '/api/config', sch: '/api/config/schema', m: '/api/metrics',
    hb: '/api/heartbeats?limit=120', usage: '/api/usage?days=30' });
  CFG.data = JSON.parse(JSON.stringify(d.cfg[CFG.section]));
  CFG.original = JSON.parse(JSON.stringify(d.cfg[CFG.section]));
  CFG.schema = d.sch && d.sch[CFG.section] && d.sch[CFG.section].schema;
  CFG.help = (d.sch && d.sch[CFG.section] && d.sch[CFG.section].help) || {};
  const m = d.m;

  main.innerHTML = `
    <div class="grid" style="margin-bottom:14px">
      ${card({ title: 'Run a cycle now', cls: 'col-4', chart: false, body: `
        <p class="hint" style="margin:0 0 10px">Triggers real model calls and costs money. Proposals still
          pass through every guardrail.</p>
        <div class="row">
          <button onclick="runCycle('premarket')">▶ Premarket</button>
          <button onclick="runCycle('intraday')">▶ Intraday</button>
          <button onclick="runCycle('eod')">▶ End of day</button>
        </div>` })}
      ${card({ title: 'Safety state', cls: 'col-4', chart: false, body: `
        <div class="row" style="margin-bottom:10px">${statusBadge(m)}
          ${m && m.kill_switch ? '<button class="danger" onclick="resetKill()">Reset kill switch</button>' : ''}</div>
        <div class="hint">${m ? esc(m.health) : ''}</div>
        ${m && m.reconcile_halt ? `<div class="hint" style="color:var(--warning);margin-top:8px">
          Reconcile halt: ${esc(m.reconcile_halt)}</div>` : ''}` })}
      ${card({ title: 'Spend, 30 days', cls: 'col-4', chart: false, body: `
        <div class="tiles" style="grid-template-columns:1fr 1fr">
          ${tile('Total', F.usd((d.usage && d.usage.total_usd) || 0, 2), { sm: true })}
          ${tile('Calls', F.int(((d.usage && d.usage.rows) || []).length), { sm: true })}
        </div>` })}
    </div>

    <div class="toolbar">
      <div class="seg" role="group" aria-label="Config file">
        <button ${CFG.section === 'limits' ? 'aria-pressed="true"' : ''} onclick="cfgSection('limits')">limits.yaml — hard safety limits</button>
        <button ${CFG.section === 'settings' ? 'aria-pressed="true"' : ''} onclick="cfgSection('settings')">settings.yaml — behaviour</button>
      </div>
      <span class="spacer"></span>
      <button id="cfgReset" onclick="cfgReload()">Discard changes</button>
      <button onclick="cfgReviewSave()" class="primary">Review & save…</button>
    </div>
    <p class="hint" style="margin:-6px 0 14px">Every field below is typed and bounded by the same schema
      the trading engine validates against, so an out-of-range value is rejected here, not at 3am.
      A timestamped backup is written before any save.</p>
    <div id="cfgForm"></div>

    ${card({ title: 'Recent heartbeats', cls: '', chart: false, body: '<div id="hbT"></div>' })}`;

  renderConfigForm();
  table($('#hbT'), [
    { k: 'ts', label: 'Time', fmt: r => F.dt(r.ts), sortVal: r => +new Date(r.ts) },
    { k: 'job', label: 'Job' },
    { k: 'status', label: 'Status', fmt: r => `<span class="badge ${
      r.status === 'error' ? 'critical' : r.status === 'warn' ? 'warning' : 'good'}">${esc(r.status)}</span>` },
    { k: 'detail', label: 'Detail', fmt: r => clip(r.detail, 110) },
  ], d.hb || [], { sort: { k: 'ts', dir: 'desc' }, empty: 'No heartbeats yet.' });
}

function cfgSection(s) { CFG.section = s; render(); }
function cfgReload() { render(); toast('Changes discarded.'); }

/** Resolve a $ref against the schema's $defs. */
function deref(schema, node) {
  if (node && node.$ref) return schema.$defs[node.$ref.split('/').pop()];
  if (node && node.anyOf) {
    const real = node.anyOf.find(x => x.type !== 'null') || node.anyOf[0];
    return Object.assign({ nullable: true, default: node.default }, deref(schema, real));
  }
  return node;
}

function renderConfigForm() {
  const s = CFG.schema, host = $('#cfgForm');
  if (!s) { host.innerHTML = '<p class="empty">Schema unavailable.</p>'; return; }
  const groups = Object.entries(s.properties || {});
  host.innerHTML = groups.map(([gk, gnode]) => {
    const g = deref(s, gnode);
    const isGroup = g && g.type === 'object' && g.properties;
    const fields = isGroup ? Object.entries(g.properties) : [[gk, g]];
    const body = fields.map(([fk, fnode]) => field(s, isGroup ? [gk, fk] : [fk], deref(s, fnode))).join('');
    return `<details class="cfg-group" ${isGroup ? '' : 'open'}>
      <summary>${esc(isGroup ? gk : 'general')}
        <span class="hint" style="font-weight:400">${esc(g && g.description || '')}</span></summary>
      <div class="cfg-fields">${body}</div></details>`;
  }).join('');
  host.oninput = onCfgInput;
  markChanged();
}

function field(schema, path, node) {
  const key = path[path.length - 1];
  const val = path.reduce((o, k) => (o || {})[k], CFG.data);
  const id = 'f_' + path.join('_');
  const help = CFG.help[key] || node.description || '';
  const t = node.type;
  const bound = [];
  if (node.minimum != null) bound.push(`min ${node.minimum}`);
  if (node.exclusiveMinimum != null) bound.push(`> ${node.exclusiveMinimum}`);
  if (node.maximum != null) bound.push(`max ${node.maximum}`);
  let input;
  if (t === 'boolean') {
    input = `<div class="rowline"><input type="checkbox" id="${id}" data-path="${path.join('.')}"
      data-t="boolean" ${val ? 'checked' : ''}><label for="${id}">${val ? 'enabled' : 'disabled'}</label></div>`;
  } else if (t === 'integer' || t === 'number') {
    const min = node.exclusiveMinimum != null ? node.exclusiveMinimum : node.minimum;
    input = `<div class="rowline"><input type="number" id="${id}" data-path="${path.join('.')}"
      data-t="${t}" value="${val ?? ''}" step="${t === 'integer' ? 1 : 'any'}"
      ${min != null ? `min="${min}"` : ''} ${node.maximum != null ? `max="${node.maximum}"` : ''}></div>`;
  } else if (node.enum) {
    input = `<select id="${id}" data-path="${path.join('.')}" data-t="string">${
      node.enum.map(o => `<option ${o === val ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>`;
  } else if (t === 'array') {
    input = `<input type="text" id="${id}" data-path="${path.join('.')}" data-t="array"
      value="${esc((val || []).join(', '))}" placeholder="comma separated">`;
  } else if (t === 'object' || val instanceof Object) {
    input = `<textarea id="${id}" data-path="${path.join('.')}" data-t="json"
      style="min-height:96px">${esc(JSON.stringify(val ?? {}, null, 1))}</textarea>`;
  } else {
    input = `<input type="text" id="${id}" data-path="${path.join('.')}" data-t="string" value="${esc(val ?? '')}">`;
  }
  return `<div class="field" data-f="${path.join('.')}">
    <label class="name" for="${id}">${esc(key)}</label>
    ${input}
    ${help || bound.length ? `<div class="help">${esc(help)}${
      bound.length ? `<span class="muted"> · ${bound.join(', ')}</span>` : ''}</div>` : ''}
    <div class="err"></div></div>`;
}

function onCfgInput(e) {
  const el = e.target, path = el.dataset.path;
  if (!path) return;
  const parts = path.split('.');
  let v;
  try {
    v = el.dataset.t === 'boolean' ? el.checked
      : el.dataset.t === 'integer' ? parseInt(el.value, 10)
      : el.dataset.t === 'number' ? parseFloat(el.value)
      : el.dataset.t === 'array' ? el.value.split(',').map(s => s.trim()).filter(Boolean)
      : el.dataset.t === 'json' ? JSON.parse(el.value)
      : el.value;
  } catch (err) {
    el.closest('.field').querySelector('.err').textContent = 'invalid JSON';
    return;
  }
  const box = el.closest('.field').querySelector('.err');
  box.textContent = (el.validity && !el.validity.valid) ? 'outside the allowed range' : '';
  let o = CFG.data;
  parts.slice(0, -1).forEach(k => o = o[k]);
  o[parts[parts.length - 1]] = v;
  if (el.dataset.t === 'boolean') el.nextElementSibling.textContent = v ? 'enabled' : 'disabled';
  markChanged();
}

function cfgDiff() {
  const out = [];
  const walk = (a, b, p = []) => {
    Object.keys(b || {}).forEach(k => {
      const av = (a || {})[k], bv = b[k];
      if (bv && typeof bv === 'object' && !Array.isArray(bv)) walk(av, bv, [...p, k]);
      else if (JSON.stringify(av) !== JSON.stringify(bv)) out.push({ path: [...p, k].join('.'), from: av, to: bv });
    });
  };
  walk(CFG.original, CFG.data);
  return out;
}
function markChanged() {
  const changed = new Set(cfgDiff().map(d => d.path));
  $$('.field').forEach(f => f.classList.toggle('changed', changed.has(f.dataset.f)));
  const n = changed.size;
  const btn = $('#cfgReset');
  if (btn) btn.disabled = !n;
}

async function cfgReviewSave() {
  const diff = cfgDiff();
  if (!diff.length) return toast('Nothing changed.');
  const v = await post('/api/config/validate', { section: CFG.section, data: CFG.data });
  openDrawer(`Save ${CFG.section}.yaml?`, `
    ${v.ok ? '<div class="badge good"><span class="dot"></span>Schema-valid</div>'
      : `<div class="badge critical"><span class="dot"></span>Invalid</div>
         <pre class="raw">${esc(v.errors)}</pre>`}
    <div class="card"><header><h3>${diff.length} change${diff.length === 1 ? '' : 's'}</h3></header>
      <div class="diff">${diff.map(d => `<div>${esc(d.path)}<br>
        <span class="del">− ${esc(JSON.stringify(d.from))}</span>
        <span class="add">+ ${esc(JSON.stringify(d.to))}</span></div>`).join('')}</div></div>
    ${CFG.section === 'limits' ? `<p class="hint">These are the hard limits the guardrail engine enforces —
      the model cannot override them. Loosening one widens what the agent may do with real money.</p>` : ''}
    <div class="row">
      <button class="primary" ${v.ok ? '' : 'disabled'} onclick="cfgSave()">Write ${CFG.section}.yaml & reload</button>
      <button onclick="closeDrawer()">Cancel</button></div>`,
    'A timestamped backup is written first.');
}
async function cfgSave() {
  const r = await post('/api/config/save', { section: CFG.section, data: CFG.data });
  closeDrawer();
  if (r.ok) { toast(`Saved ${CFG.section}.yaml — config reloaded.`, 'good'); render(); }
  else toast('Rejected: ' + r.errors, 'critical');
}

/* ==========================================================================
   Drill-down sheets
   ======================================================================== */
async function symbolSheet(sym) {
  openDrawer(sym.toUpperCase(), '<div class="skel" style="height:200px"></div>', 'loading…');
  const d = await api('/api/symbol/' + encodeURIComponent(sym)).catch(() => null);
  if (!d) return;
  const last = d.snapshots && d.snapshots[0];
  openDrawer(d.symbol, `
    <div class="tiles">
      ${tile('Position', d.position ? F.num(d.position.qty) + ' @ ' + F.usd(d.position.avg_entry_price) : 'flat', { sm: true })}
      ${tile('Unrealized', d.position ? signed(d.position.unrealized_pl) : '—', { sm: true })}
      ${tile('Last', last ? F.usd(last.last) : '—', { sm: true })}
      ${tile('Proposals', (d.proposals || []).length, { sm: true })}
    </div>
    ${card({ title: 'Price', cls: '' })}
    ${card({ title: 'Signal history', cls: '' })}
    ${card({ title: 'Proposals for this name', chart: false, body: '<div id="ssProp"></div>' })}
    ${card({ title: 'Realized lots', chart: false, body: '<div id="ssLots"></div>' })}
    ${card({ title: 'Headlines', chart: false, body: '<div id="ssNews"></div>' })}`,
    `<button onclick="go('#/markets/${encodeURIComponent(d.symbol)}');closeDrawer()">Open in Markets →</button>`);

  const hosts = $$('#drawerBody .chart-host');
  const bars = await api(`/api/bars/${encodeURIComponent(d.symbol)}?days=120`).catch(() => null);
  V.candles(hosts[0], { bars: (bars && bars.bars) || [], height: 260, overlays: [{ period: 20 }] });
  const snaps = (d.snapshots || []).slice().reverse();
  V.line(hosts[1], {
    height: 200,
    series: [
      { name: 'Last', points: snaps.map(s => ({ x: +new Date(s.ts), y: s.last })), fmt: F.usd },
      { name: '20d momentum', points: snaps.map(s => ({ x: +new Date(s.ts), y: s.features && s.features.momentum_20 })),
        fmt: v => F.pct(v, 1) },
    ],
    yKind: 'num', empty: 'No snapshots for this symbol.',
  });
  table($('#ssProp'), [
    { k: 'id', label: '#', fmt: r => `<a onclick="go('#/decision/${r.id}')">#${r.id}</a>` },
    { k: 'ts', label: 'Time', fmt: r => F.dt(r.ts) },
    { k: 'side', label: 'Order', fmt: r => `${esc(r.side)} ${F.num(r.qty)}` },
    { k: 'status', label: 'Status', fmt: r => `<span class="badge">${esc((r.status || '').replace(/_/g, ' '))}</span>` },
    { k: 'thesis', label: 'Thesis', fmt: r => clip(r.thesis, 80) },
  ], d.proposals || [], { empty: 'The agent has never proposed a trade in this name.' });
  table($('#ssLots'), [
    { k: 'open_ts', label: 'Opened', fmt: r => F.date(r.open_ts) },
    { k: 'close_ts', label: 'Closed', fmt: r => r.close_ts ? F.date(r.close_ts) : '<span class="badge accent">open</span>' },
    { k: 'qty', label: 'Qty', r: true, fmt: r => F.num(r.qty) },
    { k: 'realized_pnl', label: 'Realized', r: true, fmt: r => r.realized_pnl == null ? '—' : signed(r.realized_pnl) },
    { k: 'term', label: 'Term' },
  ], d.lots || [], { empty: 'No lots.' });
  table($('#ssNews'), [
    { k: 'ts', label: 'Time', fmt: r => F.dt(r.ts) },
    { k: 'headline', label: 'Headline', fmt: r => r.url
      ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.headline)}</a>` : esc(r.headline) },
  ], d.news || [], { empty: 'No headlines stored for this name.' });
  wireCards();
}

async function decisionSheet(id) {
  const d = await api('/api/decisions/' + id).catch(() => null);
  if (!d) return;
  openDrawer('Decision #' + id,
    `<div class="row">${(d.verdicts || []).map(v => `<span class="badge ${
      v.verdict === 'veto' ? 'critical' : v.verdict === 'pass' ? 'good' : 'warning'}">${
      esc(v.rule || v.source)}: ${esc(v.verdict)}</span>`).join('')}</div>
     <pre class="raw" style="max-height:none">${esc(d.full_text || JSON.stringify(d, null, 2))}</pre>`,
    d.symbol ? `${symLink(d.symbol)} · ${esc(d.strategy_tag || '')}` : '');
}

function cycleSheet(i) {
  const r = (window._CYC || [])[i];
  if (!r) return;
  openDrawer('Cycle · ' + F.dt(r.ts), `
    <div class="tiles">
      ${tile('Tool calls', r.tool_count, { sm: true })}
      ${tile('Cost', F.usd(r.cost_usd, 3), { sm: true })}
      ${tile('Symbols', (r.symbols_examined || []).length, { sm: true })}
    </div>
    <div class="row">${(r.symbols_examined || []).map(symLink).join(' ') || '<span class="muted">none examined</span>'}</div>
    <div class="card"><header><h3>The agent's own narrative</h3></header>
      <div class="prose" style="max-height:none">${esc(r.summary || '(no narrative captured)')}</div></div>`);
}

/* ==========================================================================
   Actions
   ======================================================================== */
async function approve(id) {
  if (!confirm(`Approve proposal #${id}? This submits a real order through the guardrail pipeline.`)) return;
  const r = await post('/api/actions/approve/' + id).catch(() => null);
  if (r && r.ok) { toast(`Proposal #${id} approved — ${r.status}.`, 'good'); render(); }
}
async function deny(id) {
  const r = await post('/api/actions/deny/' + id).catch(() => null);
  if (r && r.ok) { toast(`Proposal #${id} denied.`); render(); }
}
async function resetKill() {
  if (!confirm('Reset the kill switch? Trading resumes on the next cycle.')) return;
  await post('/api/actions/reset-kill-switch');
  toast('Kill switch reset.', 'good');
  render();
}
async function runCycle(cycle) {
  if (!confirm(`Run a ${cycle} cycle now? This makes real model calls and costs money.`)) return;
  await post('/api/actions/run-cycle', { cycle });
  toast(`${cycle} cycle started — results appear in a minute or two.`, 'good');
}

/* ==========================================================================
   Command palette
   ======================================================================== */
const COMMANDS = [
  ...Object.entries(VIEWS).map(([k, v]) => ({ label: 'Go to ' + v.label, kind: 'view', run: () => go('#/' + k) })),
  { label: 'Run premarket cycle', kind: 'action', run: () => runCycle('premarket') },
  { label: 'Run intraday cycle', kind: 'action', run: () => runCycle('intraday') },
  { label: 'Reset kill switch', kind: 'action', run: resetKill },
  { label: 'Toggle light / dark theme', kind: 'setting', run: toggleTheme },
  { label: 'Toggle compact density', kind: 'setting', run: toggleDensity },
  { label: 'Toggle colour-blind safe P&L colours', kind: 'setting', run: toggleCvd },
  { label: 'Refresh now', kind: 'action', run: render },
];
let palIdx = 0, palHits = [];
function openPalette() {
  $('#palette').classList.add('on');
  $('#palInput').value = '';
  palFilter('');
  $('#palInput').focus();
}
function closePalette() { $('#palette').classList.remove('on'); }
function palFilter(q) {
  const ql = q.toLowerCase().trim();
  palHits = COMMANDS.filter(c => c.label.toLowerCase().includes(ql));
  if (ql && /^[a-z.]{1,6}$/i.test(ql))
    palHits.unshift({ label: `Open symbol ${ql.toUpperCase()}`, kind: 'symbol',
      run: () => go('#/symbol/' + ql.toUpperCase()) });
  if (ql.length > 3) palHits.push({ label: `Search the reasoning record for “${q}”`, kind: 'search',
    run: () => { go('#/mind'); setTimeout(() => { $('#askBox').value = q; ask(); }, 350); } });
  palIdx = 0;
  paintPalette();
}
function paintPalette() {
  $('#palResults').innerHTML = palHits.map((c, i) =>
    `<div class="res" role="option" aria-selected="${i === palIdx}" data-i="${i}">
      ${esc(c.label)}<span class="kind">${c.kind}</span></div>`).join('')
    || '<div class="empty">No match.</div>';
  $$('#palResults .res').forEach(el => el.onclick = () => { palHits[+el.dataset.i].run(); closePalette(); });
}

/* ==========================================================================
   Settings
   ======================================================================== */
function toggleTheme() {
  const cur = document.documentElement.dataset.theme;
  const next = cur === 'dark' ? 'light' : cur === 'light' ? '' : 'dark';
  if (next) document.documentElement.dataset.theme = next; else delete document.documentElement.dataset.theme;
  localStorage.theme = next;
  toast('Theme: ' + (next || 'follow system'));
  render();
}
function toggleDensity() {
  const on = document.documentElement.dataset.density === 'compact';
  document.documentElement.dataset.density = on ? '' : 'compact';
  localStorage.density = on ? '' : 'compact';
}
function toggleCvd() {
  const on = document.documentElement.dataset.cvd === 'on';
  document.documentElement.dataset.cvd = on ? '' : 'on';
  localStorage.cvd = on ? '' : 'on';
  toast('P&L colours: ' + (on ? 'green / red' : 'blue / orange (colour-blind safe)'));
  render();
}

/* ==========================================================================
   Boot
   ======================================================================== */
function boot() {
  if (localStorage.theme) document.documentElement.dataset.theme = localStorage.theme;
  if (localStorage.density) document.documentElement.dataset.density = localStorage.density;
  if (localStorage.cvd) document.documentElement.dataset.cvd = localStorage.cvd;

  $('#rail').innerHTML = `
    <div class="brand"><div class="brand-mark"></div>
      <div><div class="brand-name">Agentic Trading</div>
        <div class="brand-sub">operations console</div></div></div>
    <div class="rail-group">Monitor</div>
    ${['cockpit', 'portfolio', 'markets'].map(railLink).join('')}
    <div class="rail-group">Understand</div>
    ${['mind', 'performance'].map(railLink).join('')}
    <div class="rail-group">Operate</div>
    ${railLink('control')}
    <div class="rail-foot">
      <button onclick="openPalette()">⌘K  Search & commands</button>
      <div class="row">
        <button class="icon" onclick="toggleTheme()" title="Theme">◐</button>
        <button class="icon" onclick="toggleDensity()" title="Density">≡</button>
        <button class="icon" onclick="toggleCvd()" title="Colour-blind safe P&L">◑</button>
        <button class="icon" onclick="render()" title="Refresh">↻</button>
      </div>
    </div>`;

  addEventListener('hashchange', route);
  $('#scrim').onclick = () => { closeDrawer(); closePalette(); };
  $('#drawerClose').onclick = closeDrawer;
  $('#palInput').oninput = e => palFilter(e.target.value);
  $('#palInput').onkeydown = e => {
    if (e.key === 'ArrowDown') { palIdx = Math.min(palIdx + 1, palHits.length - 1); paintPalette(); }
    else if (e.key === 'ArrowUp') { palIdx = Math.max(palIdx - 1, 0); paintPalette(); }
    else if (e.key === 'Enter' && palHits[palIdx]) { palHits[palIdx].run(); closePalette(); }
  };
  addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') { e.preventDefault(); openPalette(); }
    else if (e.key === 'Escape') { closeDrawer(); closePalette(); }
    else if (e.key === '/' && !/input|textarea|select/i.test(e.target.tagName)) { e.preventDefault(); openPalette(); }
  });

  route();
  // Refresh on a timer, but never while the tab is hidden or a drawer is open.
  setInterval(() => {
    if (document.visibilityState === 'visible' && !$('#drawer').classList.contains('on')
        && !$('#palette').classList.contains('on') && !cfgDiff().length) render();
  }, 20000);
  setInterval(() => $('#clock').textContent = new Date().toLocaleTimeString('en-US'), 1000);
}
const railLink = k => `<a data-v="${k}" href="#/${k}"><span class="ico">${VIEWS[k].ico}</span>
  <span>${VIEWS[k].label}</span></a>`;

document.addEventListener('DOMContentLoaded', boot);
