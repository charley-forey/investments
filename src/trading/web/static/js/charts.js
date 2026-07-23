/* ============================================================================
   charts.js — SVG chart primitives for the operations console.

   No dependency, no build step. Every chart here follows the same contract:
     · thin marks, hairline recessive grid, generous padding
     · one y-axis, never two
     · colour by entity (fixed slot order), never by rank
     · a hover layer by default — crosshair + tooltip on continuous forms,
       per-mark tooltip on discrete ones
     · a table-view twin, so no value is reachable only through colour or hover
     · re-render on resize; hold the previous paint at reduced opacity on refetch
   ========================================================================= */
(function (global) {
'use strict';

const NS = 'http://www.w3.org/2000/svg';
const svgEl = (tag, attrs, parent) => {
  const n = document.createElementNS(NS, tag);
  for (const k in attrs) if (attrs[k] != null) n.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(n);
  return n;
};
const tok = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const series = i => tok('--series-' + ((i % 8) + 1));
const seq = i => tok('--seq-' + Math.min(Math.max(i, 1), 7));

/* -- formatting ------------------------------------------------------------ */
const fmt = {
  usd: (n, d) => n == null || isNaN(n) ? '—' :
    (n < 0 ? '-' : '') + '$' + Math.abs(n).toLocaleString('en-US',
      { minimumFractionDigits: d ?? 2, maximumFractionDigits: d ?? 2 }),
  usdc: n => n == null || isNaN(n) ? '—' : (Math.abs(n) >= 1000
    ? (n < 0 ? '-' : '') + '$' + (Math.abs(n) / 1000).toFixed(Math.abs(n) >= 10000 ? 0 : 1) + 'k'
    : fmt.usd(n, 0)),
  num: (n, d = 2) => n == null || isNaN(n) ? '—' :
    n.toLocaleString('en-US', { maximumFractionDigits: d }),
  pct: (n, d = 1) => n == null || isNaN(n) ? '—' : (n * 100).toFixed(d) + '%',
  sig: n => n == null ? '' : n > 0 ? '▲' : n < 0 ? '▼' : '',
  int: n => n == null || isNaN(n) ? '—' : Math.round(n).toLocaleString('en-US'),
  date: t => new Date(t).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
  time: t => new Date(t).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' }),
  dt: t => new Date(t).toLocaleString('en-US',
    { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }),
};

/* -- scales & ticks -------------------------------------------------------- */
function niceTicks(min, max, count = 5) {
  if (!isFinite(min) || !isFinite(max)) return [0];
  if (min === max) { min -= 1; max += 1; }
  const raw = (max - min) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm >= 7.5 ? 10 : norm >= 3.5 ? 5 : norm >= 1.5 ? 2 : 1) * mag;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}
const lin = (d0, d1, r0, r1) => v => d1 === d0 ? r0 : r0 + (v - d0) * (r1 - r0) / (d1 - d0);

/** Axis labels sized to the tick STEP, not the magnitude — otherwise a curve that
    moves 0.3% on a $100k account prints "$100k" at every gridline. */
function autoFmt(ticks, kind) {
  const step = Math.abs((ticks[1] ?? 0) - (ticks[0] ?? 0)) || Math.abs(ticks[0]) || 1;
  const dp = step >= 10 ? 0 : step >= 1 ? 1 : step >= .1 ? 2 : 3;
  if (kind === 'pct') return v => fmt.pct(v, step >= .01 ? 1 : step >= .001 ? 2 : 3);
  if (kind === 'num') return v => fmt.num(v, dp);
  return step >= 1000 ? fmt.usdc : v => fmt.usd(v, dp);
}

/* -- shared tooltip -------------------------------------------------------- */
let tip;
function tipEl() {
  if (!tip) { tip = document.createElement('div'); tip.className = 'viz-tip'; document.body.appendChild(tip); }
  return tip;
}
function showTip(x, y, html) {
  const t = tipEl();
  t.innerHTML = html; t.classList.add('on');
  const r = t.getBoundingClientRect();
  let left = x + 14, top = y - r.height / 2;
  if (left + r.width > innerWidth - 8) left = x - r.width - 14;
  t.style.left = Math.max(8, left) + 'px';
  t.style.top = Math.min(Math.max(8, top), innerHeight - r.height - 8) + 'px';
}
const hideTip = () => tip && tip.classList.remove('on');
const tipRow = (name, val, color) =>
  `<div class="r"><span class="n">${color ? `<span class="swatch" style="width:9px;height:9px;border-radius:2px;background:${color}"></span>` : ''}${name}</span><span>${val}</span></div>`;

/* -- frame ----------------------------------------------------------------- */
function frame(host, o) {
  const w = Math.max(host.clientWidth || host.parentElement?.clientWidth || 320, 200);
  const h = o.height || 230;
  const m = Object.assign({ t: 12, r: 16, b: 26, l: 52 }, o.margin);
  host.innerHTML = '';
  const svg = svgEl('svg', { width: w, height: h, viewBox: `0 0 ${w} ${h}`, role: 'img' }, host);
  if (o.desc) svgEl('title', {}, svg).textContent = o.desc;
  return { svg, w, h, m, iw: w - m.l - m.r, ih: h - m.t - m.b };
}
function gridY(f, ticks, y, fmtFn) {
  const g = svgEl('g', { class: 'chart-grid' }, f.svg);
  const a = svgEl('g', { class: 'chart-axis' }, f.svg);
  ticks.forEach(t => {
    const yy = Math.round(y(t)) + .5;
    svgEl('line', { x1: f.m.l, x2: f.m.l + f.iw, y1: yy, y2: yy }, g);
    svgEl('text', { x: f.m.l - 8, y: yy + 3.5, 'text-anchor': 'end' }, a).textContent = fmtFn(t);
  });
}
function axisX(f, ticks, x, fmtFn) {
  const a = svgEl('g', { class: 'chart-axis' }, f.svg);
  ticks.forEach((t, i) => {
    const anchor = i === 0 ? 'start' : i === ticks.length - 1 ? 'end' : 'middle';
    svgEl('text', { x: x(t), y: f.h - 8, 'text-anchor': anchor }, a).textContent = fmtFn(t);
  });
}

/* -- re-render on resize; every chart registers its own draw --------------- */
const RO = new ResizeObserver(es => es.forEach(e => {
  const d = e.target._draw;
  if (d && e.contentRect.width > 0) { clearTimeout(e.target._rt); e.target._rt = setTimeout(d, 60); }
}));
function register(host, draw) {
  if (host._ro !== true) { RO.observe(host); host._ro = true; }
  host._draw = draw;
  draw();
}

/* -- table twin ------------------------------------------------------------ */
function attachTable(host, cols, rows) { host._table = { cols, rows }; }
function showTable(host, on) {
  host._showTable = on;
  if (!on) { host._draw && host._draw(); return; }
  const t = host._table;
  if (!t) return;
  host.innerHTML = `<div class="table-wrap"><table><thead><tr>${
    t.cols.map((c, i) => `<th class="${i ? 'r' : ''}">${c}</th>`).join('')
  }</tr></thead><tbody>${
    t.rows.map(r => `<tr>${r.map((v, i) =>
      `<td data-l="${t.cols[i]}" class="${i ? 'r' : ''}">${v}</td>`).join('')}</tr>`).join('')
  }</tbody></table></div>`;
}

/* ==========================================================================
   Time series — line / area, multi-series, crosshair, optional brush.
   ======================================================================== */
function line(host, o) {
  register(host, () => {
    if (host._showTable && host._table) return showTable(host, true);
    const all = o.series.filter(s => s.points && s.points.length);
    if (!all.length) return void (host.innerHTML = `<p class="empty">${o.empty || 'No data yet.'}</p>`);
    const f = frame(host, o);
    const xs = all.flatMap(s => s.points.map(p => +p.x));
    const ys = all.flatMap(s => s.points.map(p => p.y)).filter(v => v != null && isFinite(v));
    let [x0, x1] = [Math.min(...xs), Math.max(...xs)];
    let [y0, y1] = [Math.min(...ys), Math.max(...ys)];
    if (o.zeroBase) y0 = Math.min(0, y0);
    const padY = (y1 - y0) * .1 || Math.abs(y1 || 1) * .1;
    y0 -= padY; y1 += padY;
    const ticks = niceTicks(y0, y1, o.height > 260 ? 6 : 4);
    y0 = Math.min(y0, ticks[0]); y1 = Math.max(y1, ticks[ticks.length - 1]);
    const x = lin(x0, x1, f.m.l, f.m.l + f.iw), y = lin(y0, y1, f.m.t + f.ih, f.m.t);
    const yFmt = o.yFmt || autoFmt(ticks, o.yKind);
    const tipFmt = o.tipFmt || (o.yKind === 'pct' ? (v => fmt.pct(v, 2))
      : o.yKind === 'num' ? (v => fmt.num(v, 2)) : (v => fmt.usd(v, 2)));
    gridY(f, ticks, y, yFmt);
    const n = Math.max(2, Math.min(5, Math.floor(f.iw / 110)));
    axisX(f, Array.from({ length: n }, (_, i) => x0 + (x1 - x0) * i / (n - 1)), x, o.xFmt || fmt.date);

    // Reference bands (e.g. a limit) sit under the data as a soft wash.
    (o.bands || []).forEach(b => svgEl('rect', {
      x: f.m.l, width: f.iw, y: y(b.to), height: Math.abs(y(b.from) - y(b.to)),
      fill: b.color, opacity: .12,
    }, f.svg));

    all.forEach((s, i) => {
      if (s.hidden) return;
      const col = s.color || series(o.colorOffset ? i + o.colorOffset : i);
      const pts = s.points.filter(p => p.y != null && isFinite(p.y));
      if (!pts.length) return;
      const d = pts.map((p, k) => (k ? 'L' : 'M') + x(+p.x).toFixed(1) + ' ' + y(p.y).toFixed(1)).join(' ');
      if (o.area && all.length === 1) {
        const base = y(Math.max(y0, o.areaBase ?? y0));
        svgEl('path', { d: `M${x(+pts[0].x).toFixed(1)} ${base} ` + d.slice(1) +
          ` L${x(+pts[pts.length - 1].x).toFixed(1)} ${base} Z`, fill: col, opacity: .13 }, f.svg);
      }
      svgEl('path', { d, class: 'chart-line', stroke: col }, f.svg);
      // Selective direct label: the endpoint only, never a number per point.
      if (o.labelEnd !== false) {
        const last = pts[pts.length - 1];
        svgEl('circle', { cx: x(+last.x), cy: y(last.y), r: 3.5, fill: col, class: 'chart-marker' }, f.svg);
      }
    });

    // Event markers (fills, cycles) as small ticks on the baseline.
    (o.markers || []).forEach(mk => {
      const mx = x(+mk.x);
      if (mx < f.m.l - 1 || mx > f.m.l + f.iw + 1) return;
      svgEl('line', { x1: mx, x2: mx, y1: f.m.t + f.ih - 7, y2: f.m.t + f.ih,
        stroke: mk.color || tok('--ink-muted'), 'stroke-width': 2, opacity: .8 }, f.svg);
    });

    // --- hover / crosshair layer
    const cross = svgEl('line', { class: 'chart-crosshair', y1: f.m.t, y2: f.m.t + f.ih,
      opacity: 0 }, f.svg);
    const dots = svgEl('g', { opacity: 0 }, f.svg);
    const hit = svgEl('rect', { class: 'chart-hit', x: f.m.l, y: f.m.t, width: f.iw, height: f.ih }, f.svg);
    const inv = px => x0 + (px - f.m.l) * (x1 - x0) / (f.iw || 1);

    const move = ev => {
      const r = f.svg.getBoundingClientRect();
      const px = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
      const tx = inv(Math.min(Math.max(px, f.m.l), f.m.l + f.iw));
      cross.setAttribute('x1', x(tx)); cross.setAttribute('x2', x(tx));
      cross.setAttribute('opacity', .55);
      dots.innerHTML = ''; dots.setAttribute('opacity', 1);
      let head = null;
      const rows = all.filter(s => !s.hidden).map((s, i) => {
        const col = s.color || series(o.colorOffset ? i + o.colorOffset : i);
        const p = s.points.reduce((a, b) => Math.abs(+b.x - tx) < Math.abs(+a.x - tx) ? b : a);
        if (p.y == null) return '';
        head = head ?? p.x;
        svgEl('circle', { cx: x(+p.x), cy: y(p.y), r: 4, fill: col, class: 'chart-marker' }, dots);
        return tipRow(s.name, (s.fmt || tipFmt)(p.y), col);
      }).join('');
      showTip(ev.touches ? ev.touches[0].clientX : ev.clientX,
        ev.touches ? ev.touches[0].clientY : ev.clientY,
        `<div class="t">${(o.xTipFmt || fmt.dt)(head)}</div>${rows}`);
    };
    const leave = () => { cross.setAttribute('opacity', 0); dots.setAttribute('opacity', 0); hideTip(); };
    hit.addEventListener('pointermove', move);
    hit.addEventListener('pointerleave', leave);

    // --- brush: drag to scope every panel on the page to a time window
    if (o.onBrush) {
      let sx = null;
      const sel = svgEl('rect', { y: f.m.t, height: f.ih, fill: tok('--accent'), opacity: .15,
        width: 0, 'pointer-events': 'none' }, f.svg);
      hit.addEventListener('pointerdown', ev => {
        sx = ev.offsetX; sel.setAttribute('x', sx); sel.setAttribute('width', 0);
        hit.setPointerCapture(ev.pointerId);
      });
      hit.addEventListener('pointermove', ev => {
        if (sx == null) return;
        sel.setAttribute('x', Math.min(sx, ev.offsetX));
        sel.setAttribute('width', Math.abs(ev.offsetX - sx));
      });
      hit.addEventListener('pointerup', ev => {
        if (sx == null) return;
        const a = inv(Math.min(sx, ev.offsetX)), b = inv(Math.max(sx, ev.offsetX));
        sx = null; leave();
        if (Math.abs(b - a) > (x1 - x0) / 80) o.onBrush(a, b); else sel.setAttribute('width', 0);
      });
      hit.addEventListener('dblclick', () => o.onBrush(null, null));
    }

    if (all.length > 1 && o.legend !== false) legend(host, all, o);
    attachTable(host,
      [o.xLabel || 'Time', ...all.map(s => s.name)],
      (all[0].points || []).slice().reverse().map((p, i, arr) => {
        const k = arr.length - 1 - i;
        return [(o.xTipFmt || fmt.dt)(p.x),
          ...all.map(s => (s.fmt || tipFmt)(s.points[k] && s.points[k].y))];
      }));
  });
}

function legend(host, all, o) {
  const box = document.createElement('div');
  box.className = 'legend';
  box.innerHTML = all.map((s, i) => `<span class="item" role="button" tabindex="0" data-i="${i}"
    aria-pressed="${!s.hidden}"><span class="swatch" style="background:${
      s.color || series(o.colorOffset ? i + o.colorOffset : i)}"></span>${s.name}</span>`).join('');
  box.addEventListener('click', e => {
    const it = e.target.closest('.item'); if (!it) return;
    all[+it.dataset.i].hidden = !all[+it.dataset.i].hidden;
    host._draw();
  });
  host.appendChild(box);
}

/* ==========================================================================
   Candlestick + volume — the price chart.
   ======================================================================== */
function candles(host, o) {
  register(host, () => {
    if (host._showTable && host._table) return showTable(host, true);
    const bars = (o.bars || []).filter(b => b && isFinite(b.close));
    if (bars.length < 2) return void (host.innerHTML =
      `<p class="empty">${o.empty || 'No price history for this symbol yet.'}</p>`);
    const f = frame(host, Object.assign({ height: 320, margin: { t: 12, r: 16, b: 26, l: 58 } }, o));
    const volH = Math.round(f.ih * .18), priceH = f.ih - volH - 10;
    const lo = Math.min(...bars.map(b => b.low)), hi = Math.max(...bars.map(b => b.high));
    const pad = (hi - lo) * .06 || 1;
    const ticks = niceTicks(lo - pad, hi + pad, 5);
    const y = lin(Math.min(lo - pad, ticks[0]), Math.max(hi + pad, ticks[ticks.length - 1]),
      f.m.t + priceH, f.m.t);
    const step = f.iw / bars.length;
    const bw = Math.max(1, Math.min(11, step * .62));
    const cx = i => f.m.l + step * (i + .5);

    const g = svgEl('g', { class: 'chart-grid' }, f.svg);
    const a = svgEl('g', { class: 'chart-axis' }, f.svg);
    ticks.forEach(t => {
      const yy = Math.round(y(t)) + .5;
      svgEl('line', { x1: f.m.l, x2: f.m.l + f.iw, y1: yy, y2: yy }, g);
      svgEl('text', { x: f.m.l - 8, y: yy + 3.5, 'text-anchor': 'end' }, a).textContent = fmt.usd(t, 2);
    });

    const up = tok('--up'), down = tok('--down');
    bars.forEach((b, i) => {
      const rising = b.close >= b.open, col = rising ? up : down;
      svgEl('line', { x1: cx(i), x2: cx(i), y1: y(b.high), y2: y(b.low), stroke: col,
        'stroke-width': Math.max(1, bw * .16) }, f.svg);
      const top = y(Math.max(b.open, b.close)), bot = y(Math.min(b.open, b.close));
      svgEl('rect', { x: cx(i) - bw / 2, y: top, width: bw, height: Math.max(1, bot - top),
        fill: rising ? 'none' : col, stroke: col, 'stroke-width': 1.4, rx: 1 }, f.svg);
    });

    // Moving averages — identity series, categorical slots.
    (o.overlays || []).forEach((ov, k) => {
      const pts = sma(bars.map(b => b.close), ov.period);
      const d = pts.map((v, i) => v == null ? null : (i ? 'L' : 'M') + cx(i).toFixed(1) + ' ' + y(v).toFixed(1))
        .filter(Boolean).join(' ').replace(/^L/, 'M');
      svgEl('path', { d, class: 'chart-line', stroke: series(k), 'stroke-width': 1.5, opacity: .95 }, f.svg);
    });

    // Volume panel — one series, one colour, anchored to its own baseline.
    const vmax = Math.max(...bars.map(b => b.volume || 0)) || 1;
    const vy = lin(0, vmax, f.m.t + f.ih, f.m.t + priceH + 10);
    bars.forEach((b, i) => {
      const h = f.m.t + f.ih - vy(b.volume || 0);
      if (h < .5) return;
      svgEl('rect', { x: cx(i) - bw / 2, y: vy(b.volume || 0), width: bw, height: h,
        fill: tok('--ink-muted'), opacity: .38, rx: Math.min(2, bw / 3) }, f.svg);
    });
    svgEl('text', { x: f.m.l - 8, y: f.m.t + f.ih, 'text-anchor': 'end', class: 'chart-axis' },
      svgEl('g', { class: 'chart-axis' }, f.svg)).textContent = 'vol';

    const n = Math.max(2, Math.min(6, Math.floor(f.iw / 90)));
    const ax = svgEl('g', { class: 'chart-axis' }, f.svg);
    for (let i = 0; i < n; i++) {
      const idx = Math.round(i * (bars.length - 1) / (n - 1));
      svgEl('text', { x: cx(idx), y: f.h - 8,
        'text-anchor': i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle' }, ax)
        .textContent = fmt.date(bars[idx].date);
    }

    const cross = svgEl('line', { class: 'chart-crosshair', y1: f.m.t, y2: f.m.t + f.ih, opacity: 0 }, f.svg);
    const hit = svgEl('rect', { class: 'chart-hit', x: f.m.l, y: f.m.t, width: f.iw, height: f.ih }, f.svg);
    hit.addEventListener('pointermove', ev => {
      const r = f.svg.getBoundingClientRect();
      const i = Math.min(bars.length - 1, Math.max(0, Math.floor((ev.clientX - r.left - f.m.l) / step)));
      const b = bars[i];
      cross.setAttribute('x1', cx(i)); cross.setAttribute('x2', cx(i)); cross.setAttribute('opacity', .5);
      const ch = i ? (b.close - bars[i - 1].close) / bars[i - 1].close : 0;
      showTip(ev.clientX, ev.clientY, `<div class="t">${fmt.date(b.date)}</div>` +
        tipRow('Open', fmt.usd(b.open)) + tipRow('High', fmt.usd(b.high)) +
        tipRow('Low', fmt.usd(b.low)) + tipRow('Close', fmt.usd(b.close)) +
        tipRow('Change', fmt.sig(ch) + ' ' + fmt.pct(ch)) +
        tipRow('Volume', fmt.int(b.volume)));
    });
    hit.addEventListener('pointerleave', () => { cross.setAttribute('opacity', 0); hideTip(); });

    if (o.overlays && o.overlays.length) {
      const box = document.createElement('div');
      box.className = 'legend';
      box.innerHTML = o.overlays.map((ov, k) =>
        `<span class="item"><span class="swatch" style="background:${series(k)}"></span>SMA ${ov.period}</span>`).join('')
        + `<span class="item"><span class="swatch" style="background:${up}"></span>Up day</span>`
        + `<span class="item"><span class="swatch" style="background:${down}"></span>Down day</span>`;
      host.appendChild(box);
    }
    attachTable(host, ['Date', 'Open', 'High', 'Low', 'Close', 'Volume'],
      bars.slice(-80).reverse().map(b => [fmt.date(b.date), fmt.usd(b.open), fmt.usd(b.high),
        fmt.usd(b.low), fmt.usd(b.close), fmt.int(b.volume)]));
  });
}
function sma(vals, p) {
  let sum = 0;
  return vals.map((v, i) => {
    sum += v;
    if (i >= p) sum -= vals[i - p];
    return i >= p - 1 ? sum / p : null;
  });
}

/* ==========================================================================
   Bars — one measure across categories. Horizontal when labels are words.
   ======================================================================== */
function bar(host, o) {
  register(host, () => {
    if (host._showTable && host._table) return showTable(host, true);
    const data = (o.data || []).filter(d => d.value != null);
    if (!data.length) return void (host.innerHTML = `<p class="empty">${o.empty || 'Nothing recorded yet.'}</p>`);
    const horiz = o.horizontal !== false;
    const vFmt = o.fmt || fmt.int;
    const f = frame(host, Object.assign({
      height: o.height || Math.max(120, data.length * 30 + 34),
      margin: horiz ? { t: 8, r: 60, b: 22, l: Math.min(180, Math.max(...data.map(d => d.label.length)) * 6.6 + 12) }
                    : { t: 12, r: 12, b: 44, l: 52 },
    }, { height: o.height }));
    const vals = data.map(d => d.value);
    const max = Math.max(0, ...vals), min = Math.min(0, ...vals);

    if (horiz) {
      const x = lin(min, max || 1, f.m.l, f.m.l + f.iw);
      const bh = Math.min(20, f.ih / data.length - 6);
      const zero = x(0);
      data.forEach((d, i) => {
        const yy = f.m.t + (f.ih / data.length) * i + (f.ih / data.length - bh) / 2;
        const col = d.color || (o.diverging ? (d.value >= 0 ? tok('--up') : tok('--down'))
          : o.ordinal ? seq(2 + Math.round(i * 4 / Math.max(1, data.length - 1))) : series(0));
        const w = Math.max(1, Math.abs(x(d.value) - zero));
        // 4px rounded data-end, square against the baseline.
        svgEl('rect', { x: d.value >= 0 ? zero : zero - w, y: yy, width: w, height: bh,
          fill: col, rx: 4 }, f.svg);
        svgEl('text', { x: f.m.l - 9, y: yy + bh / 2 + 3.6, 'text-anchor': 'end', class: 'chart-axis' },
          svgEl('g', { class: 'chart-axis' }, f.svg)).textContent = d.label;
        svgEl('text', { x: (d.value >= 0 ? zero + w : zero - w) + (d.value >= 0 ? 7 : -7),
          y: yy + bh / 2 + 3.8, 'text-anchor': d.value >= 0 ? 'start' : 'end', class: 'chart-label' },
          f.svg).textContent = vFmt(d.value);
        const hit = svgEl('rect', { x: f.m.l, y: yy - 4, width: f.iw, height: bh + 8,
          fill: 'transparent' }, f.svg);
        hit.addEventListener('pointermove', ev => showTip(ev.clientX, ev.clientY,
          `<div class="t">${d.label}</div>${tipRow(o.measure || 'Value', vFmt(d.value), col)}${
            d.note ? `<div class="r"><span class="n">${d.note}</span></div>` : ''}`));
        hit.addEventListener('pointerleave', hideTip);
        if (o.onPick) { hit.style.cursor = 'pointer'; hit.addEventListener('click', () => o.onPick(d)); }
      });
      if (min < 0) svgEl('line', { x1: zero, x2: zero, y1: f.m.t, y2: f.m.t + f.ih,
        stroke: tok('--axis') }, f.svg);
    } else {
      const ticks = niceTicks(min, max, 4);
      const y = lin(Math.min(min, ticks[0]), Math.max(max, ticks[ticks.length - 1]), f.m.t + f.ih, f.m.t);
      gridY(f, ticks, y, vFmt);
      const step = f.iw / data.length, bw = Math.min(38, step - 6);   // 2px+ surface gap
      const zero = y(0);
      data.forEach((d, i) => {
        const xx = f.m.l + step * i + (step - bw) / 2;
        const col = d.color || (o.diverging ? (d.value >= 0 ? tok('--up') : tok('--down')) : series(0));
        const h = Math.max(1, Math.abs(y(d.value) - zero));
        svgEl('rect', { x: xx, y: d.value >= 0 ? y(d.value) : zero, width: bw, height: h,
          fill: col, rx: 4 }, f.svg);
        const lab = svgEl('text', { x: xx + bw / 2, y: f.h - 10, 'text-anchor': 'end', class: 'chart-axis',
          transform: `rotate(-38 ${xx + bw / 2} ${f.h - 10})` }, svgEl('g', { class: 'chart-axis' }, f.svg));
        lab.textContent = d.label;
        const hit = svgEl('rect', { x: f.m.l + step * i, y: f.m.t, width: step, height: f.ih,
          fill: 'transparent' }, f.svg);
        hit.addEventListener('pointermove', ev => showTip(ev.clientX, ev.clientY,
          `<div class="t">${d.label}</div>${tipRow(o.measure || 'Value', vFmt(d.value), col)}`));
        hit.addEventListener('pointerleave', hideTip);
        if (o.onPick) { hit.style.cursor = 'pointer'; hit.addEventListener('click', () => o.onPick(d)); }
      });
    }
    attachTable(host, [o.dimension || 'Item', o.measure || 'Value'],
      data.map(d => [d.label, vFmt(d.value)]));
  });
}

/* ==========================================================================
   Stacked time bars — composition over time (cost by agent, etc.)
   ======================================================================== */
function stack(host, o) {
  register(host, () => {
    if (host._showTable && host._table) return showTable(host, true);
    const rows = o.rows || [], keys = o.keys || [];
    if (!rows.length) return void (host.innerHTML = `<p class="empty">${o.empty || 'Nothing recorded yet.'}</p>`);
    const f = frame(host, o);
    const totals = rows.map(r => keys.reduce((s, k) => s + (r[k] || 0), 0));
    const ticks = niceTicks(0, Math.max(...totals, 0.0001), 4);
    const y = lin(0, ticks[ticks.length - 1], f.m.t + f.ih, f.m.t);
    const vFmt = o.fmt || (v => fmt.usd(v, 2));
    gridY(f, ticks, y, vFmt);
    const step = f.iw / rows.length, bw = Math.min(40, Math.max(3, step - 3));
    rows.forEach((r, i) => {
      let acc = 0;
      keys.forEach((k, ki) => {
        const v = r[k] || 0; if (!v) return;
        const top = y(acc + v), bot = y(acc);
        acc += v;
        // 2px surface gap between stacked segments
        svgEl('rect', { x: f.m.l + step * i + (step - bw) / 2, y: top, width: bw,
          height: Math.max(1, bot - top - 2), fill: series(ki), rx: 2 }, f.svg);
      });
      const hit = svgEl('rect', { x: f.m.l + step * i, y: f.m.t, width: step, height: f.ih,
        fill: 'transparent' }, f.svg);
      hit.addEventListener('pointermove', ev => showTip(ev.clientX, ev.clientY,
        `<div class="t">${r.label}</div>` +
        keys.map((k, ki) => r[k] ? tipRow(k, vFmt(r[k]), series(ki)) : '').join('') +
        tipRow('Total', vFmt(totals[i]))));
      hit.addEventListener('pointerleave', hideTip);
    });
    const n = Math.max(2, Math.min(6, Math.floor(f.iw / 80)));
    const ax = svgEl('g', { class: 'chart-axis' }, f.svg);
    for (let i = 0; i < n; i++) {
      const idx = Math.round(i * (rows.length - 1) / (n - 1));
      svgEl('text', { x: f.m.l + step * (idx + .5), y: f.h - 8,
        'text-anchor': i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle' }, ax)
        .textContent = rows[idx].label;
    }
    const box = document.createElement('div');
    box.className = 'legend';
    box.innerHTML = keys.map((k, ki) =>
      `<span class="item"><span class="swatch" style="background:${series(ki)}"></span>${k}</span>`).join('');
    host.appendChild(box);
    attachTable(host, [o.dimension || 'Bucket', ...keys, 'Total'],
      rows.map((r, i) => [r.label, ...keys.map(k => vFmt(r[k] || 0)), vFmt(totals[i])]));
  });
}

/* ==========================================================================
   Heatmap — one sequential hue, light -> dark. Symbols x time.
   ======================================================================== */
function heat(host, o) {
  register(host, () => {
    if (host._showTable && host._table) return showTable(host, true);
    const rows = o.rows || [];
    if (!rows.length) return void (host.innerHTML = `<p class="empty">${o.empty || 'No signal history yet.'}</p>`);
    const cols = o.cols || [];
    const labW = Math.min(96, Math.max(...rows.map(r => r.label.length)) * 7.4 + 10);
    const f = frame(host, { height: o.height || rows.length * 24 + 34,
      margin: { t: 8, r: 8, b: 24, l: labW } });
    const cw = f.iw / Math.max(1, cols.length), ch = Math.min(20, f.ih / rows.length - 3);
    const vals = rows.flatMap(r => r.cells.map(c => c.v)).filter(v => v != null && isFinite(v));
    const lo = o.min ?? Math.min(...vals), hi = o.max ?? Math.max(...vals);
    const vFmt = o.fmt || (v => fmt.pct(v, 1));
    const shade = v => {
      if (v == null || !isFinite(v)) return tok('--surface-2');
      if (o.diverging) {
        const m = Math.max(Math.abs(lo), Math.abs(hi)) || 1;
        const t = Math.min(1, Math.abs(v) / m);
        return v === 0 ? tok('--surface-3')
          : `color-mix(in oklab, ${v > 0 ? tok('--up') : tok('--down')} ${(18 + t * 72).toFixed(0)}%, ${tok('--surface-1')})`;
      }
      return seq(1 + Math.round(((v - lo) / ((hi - lo) || 1)) * 6));
    };
    rows.forEach((r, ri) => {
      const yy = f.m.t + (f.ih / rows.length) * ri;
      const g = svgEl('g', { class: 'chart-axis' }, f.svg);
      const t = svgEl('text', { x: f.m.l - 8, y: yy + ch / 2 + 3.6, 'text-anchor': 'end' }, g);
      t.textContent = r.label;
      if (o.onPick) { t.style.cursor = 'pointer'; t.addEventListener('click', () => o.onPick(r.label)); }
      r.cells.forEach((c, ci) => {
        const cell = svgEl('rect', { x: f.m.l + cw * ci + 1, y: yy, width: Math.max(1, cw - 2),
          height: ch, fill: shade(c.v), rx: 2 }, f.svg);
        cell.addEventListener('pointermove', ev => showTip(ev.clientX, ev.clientY,
          `<div class="t">${r.label} · ${c.label}</div>${tipRow(o.measure || 'Value', vFmt(c.v))}`));
        cell.addEventListener('pointerleave', hideTip);
        if (o.onPick) { cell.style.cursor = 'pointer'; cell.addEventListener('click', () => o.onPick(r.label)); }
      });
    });
    const ax = svgEl('g', { class: 'chart-axis' }, f.svg);
    [0, Math.floor(cols.length / 2), cols.length - 1].filter((v, i, a) => a.indexOf(v) === i && v >= 0)
      .forEach((ci, i, arr) => svgEl('text', { x: f.m.l + cw * (ci + .5), y: f.h - 7,
        'text-anchor': i === 0 ? 'start' : i === arr.length - 1 ? 'end' : 'middle' }, ax)
        .textContent = cols[ci] || '');
    // Scale legend — mandatory for a continuous colour scale.
    const box = document.createElement('div');
    box.className = 'legend';
    box.innerHTML = `<span class="item">${vFmt(lo)}</span>` +
      [1, 2, 3, 4, 5, 6, 7].map(i => `<span class="swatch" style="background:${
        o.diverging ? shade(lo + (hi - lo) * (i - 1) / 6) : seq(i)}"></span>`).join('') +
      `<span class="item">${vFmt(hi)}</span>`;
    host.appendChild(box);
    attachTable(host, ['Symbol', ...cols],
      rows.map(r => [r.label, ...r.cells.map(c => vFmt(c.v))]));
  });
}

/* ==========================================================================
   Funnel — ordered stages, ordinal ramp, drop-off called out in words.
   ======================================================================== */
function funnel(host, o) {
  register(host, () => {
    if (host._showTable && host._table) return showTable(host, true);
    const st = o.stages || [];
    if (!st.length) return void (host.innerHTML = '<p class="empty">No pipeline activity yet.</p>');
    const max = Math.max(...st.map(s => s.count), 1);
    host.innerHTML = st.map((s, i) => {
      const prev = i ? st[i - 1].count : null;
      const drop = prev != null && prev > 0 ? 1 - s.count / prev : null;
      // Ordinal ramp: start no lighter than step 3 so the first stage clears 2:1.
      const col = seq(3 + Math.round(i * 4 / Math.max(1, st.length - 1)));
      return `<div class="bullet">
        <div class="top"><span>${s.stage}</span>
          <span class="num"><b>${fmt.int(s.count)}</b>${
            drop != null && drop > 0 ? ` <span class="muted">−${fmt.pct(drop, 0)}</span>` : ''}</span></div>
        <div class="track"><div class="fill" style="width:${(s.count / max * 100).toFixed(1)}%;background:${col}"></div></div>
      </div>`;
    }).join('');
    host.className = 'chart-host bullets';
    attachTable(host, ['Stage', 'Count'], st.map(s => [s.stage, fmt.int(s.count)]));
  });
}

/* ==========================================================================
   Scatter — risk vs return, R-multiples. Nearest-point hover.
   ======================================================================== */
function scatter(host, o) {
  register(host, () => {
    if (host._showTable && host._table) return showTable(host, true);
    const pts = (o.points || []).filter(p => isFinite(p.x) && isFinite(p.y));
    if (!pts.length) return void (host.innerHTML = `<p class="empty">${o.empty || 'Not enough closed trades yet.'}</p>`);
    const f = frame(host, Object.assign({ height: 280, margin: { t: 14, r: 18, b: 40, l: 58 } }, o));
    const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
    const xt = niceTicks(Math.min(0, ...xs), Math.max(...xs), 4);
    const yt = niceTicks(Math.min(0, ...ys), Math.max(...ys), 4);
    const x = lin(xt[0], xt[xt.length - 1], f.m.l, f.m.l + f.iw);
    const y = lin(yt[0], yt[yt.length - 1], f.m.t + f.ih, f.m.t);
    gridY(f, yt, y, o.yFmt || fmt.usdc);
    axisX(f, xt, x, o.xFmt || fmt.num);
    if (yt[0] < 0) svgEl('line', { x1: f.m.l, x2: f.m.l + f.iw, y1: y(0), y2: y(0),
      stroke: tok('--axis') }, f.svg);
    pts.forEach(p => {
      const col = p.value != null ? (p.value >= 0 ? tok('--up') : tok('--down')) : series(0);
      const c = svgEl('circle', { cx: x(p.x), cy: y(p.y), r: 5.5, fill: col, opacity: .85,
        class: 'chart-marker' }, f.svg);   // 2px surface ring
      const hit = svgEl('circle', { cx: x(p.x), cy: y(p.y), r: 13, fill: 'transparent' }, f.svg);
      hit.addEventListener('pointermove', ev => showTip(ev.clientX, ev.clientY,
        `<div class="t">${p.label}</div>` +
        tipRow(o.xLabel || 'x', (o.xFmt || fmt.num)(p.x), col) +
        tipRow(o.yLabel || 'y', (o.yFmt || fmt.usdc)(p.y))));
      hit.addEventListener('pointerleave', hideTip);
      if (o.onPick) { hit.style.cursor = 'pointer'; hit.addEventListener('click', () => o.onPick(p)); }
    });
    svgEl('text', { x: f.m.l + f.iw / 2, y: f.h - 24, 'text-anchor': 'middle', class: 'chart-label' },
      f.svg).textContent = o.xLabel || '';
    attachTable(host, [o.dimension || 'Item', o.xLabel || 'x', o.yLabel || 'y'],
      pts.map(p => [p.label, (o.xFmt || fmt.num)(p.x), (o.yFmt || fmt.usdc)(p.y)]));
  });
}

/* ==========================================================================
   Sparkline — inline trend inside a tile or table row. No axes, no hover.
   ======================================================================== */
function spark(host, vals, o) {
  o = o || {};
  const v = (vals || []).filter(n => n != null && isFinite(n));
  if (v.length < 2) { host.innerHTML = ''; return; }
  const w = Math.max(host.clientWidth || 80, 40), h = o.height || 26;
  const lo = Math.min(...v), hi = Math.max(...v);
  const x = lin(0, v.length - 1, 1, w - 1), y = lin(lo, hi, h - 2, 2);
  const col = o.color || (v[v.length - 1] >= v[0] ? tok('--up') : tok('--down'));
  const d = v.map((n, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(n).toFixed(1)).join(' ');
  host.innerHTML = `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
    <path d="${d} L${x(v.length - 1).toFixed(1)} ${h} L${x(0).toFixed(1)} ${h} Z" fill="${col}" opacity=".12"/>
    <path d="${d}" fill="none" stroke="${col}" stroke-width="1.6" stroke-linejoin="round"/>
    <circle cx="${x(v.length - 1).toFixed(1)}" cy="${y(v[v.length - 1]).toFixed(1)}" r="2.2" fill="${col}"/>
  </svg>`;
}

/* ==========================================================================
   Bullet row — a measured value against the limit that governs it.
   ======================================================================== */
function bullets(host, items) {
  host.className = 'chart-host bullets';
  host.innerHTML = items.map(it => {
    const cap = it.cap || 0;
    const use = cap > 0 ? Math.abs(it.value) / cap : 0;
    const state = use >= 1 ? 'critical' : use >= .8 ? 'warning' : '';
    const label = use >= 1 ? 'over limit' : use >= .8 ? 'near limit' : '';
    return `<div class="bullet">
      <div class="top">
        <span>${it.label}${label ? ` <span class="badge ${state}"><span class="dot"></span>${label}</span>` : ''}</span>
        <span class="num">${it.fmt(it.value)}${cap ? ` <span class="muted">/ ${it.fmt(cap)}</span>` : ''}</span>
      </div>
      <div class="track">
        <div class="fill ${state}" style="width:${Math.min(100, use * 100).toFixed(1)}%"></div>
        ${cap ? '<div class="cap" style="right:0"></div>' : ''}
      </div>
      ${it.note ? `<div class="help muted" style="font-size:11.5px;margin-top:4px">${it.note}</div>` : ''}
    </div>`;
  }).join('');
}

global.V = { line, candles, bar, stack, heat, funnel, scatter, spark, bullets,
  fmt, series, seq, tok, showTable, showTip, hideTip, niceTicks, sma };
})(window);
