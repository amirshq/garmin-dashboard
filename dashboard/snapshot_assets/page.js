/* Snapshot page behaviour. Globals injected by dashboard/snapshot.py: HERO, FIGS, DATE_FIGS, END. */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  let reduce = false; try { reduce = matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) {}
  const store = { get: k => { try { return localStorage.getItem(k); } catch (e) { return null; } },
                  set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} } };
  const H = HERO;

  /* ---------- helpers ---------- */
  function swap(el, text, cls) {           // crossfade a text change
    if (!el) return;
    if (el.textContent === text && (cls === undefined || el.dataset.cls === cls)) return;
    const apply = () => { el.textContent = text; if (cls !== undefined) { if (el.dataset.cls) el.classList.remove(el.dataset.cls); if (cls) el.classList.add(cls); el.dataset.cls = cls; } };
    if (reduce || !el.textContent) { apply(); return; }
    el.classList.add('fade', 'out');
    setTimeout(() => { apply(); el.classList.remove('out'); }, 200);
  }
  const hello = () => { const h = new Date().getHours(); return h < 5 ? 'Good night' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening'; };
  const tone = t => t === 'good' ? 'good' : t === 'warn' ? 'warn' : 'muted';

  /* ---------- segmented switches ---------- */
  function initSwitch(sw, onPick, initial) {
    const ind = $('.ind', sw), btns = $$('button', sw);
    const place = b => { ind.style.width = b.offsetWidth + 'px'; ind.style.transform = `translateX(${b.offsetLeft - btns[0].offsetLeft}px)`; };
    const pick = (b, first) => { btns.forEach(x => x.setAttribute('aria-selected', String(x === b))); place(b); if (!first) onPick(b.dataset.v); };
    btns.forEach(b => b.addEventListener('click', () => pick(b)));
    const start = btns.find(b => b.dataset.v === initial) || btns[0];
    pick(start, true); onPick(start.dataset.v, true);
    addEventListener('resize', () => place(btns.find(b => b.getAttribute('aria-selected') === 'true')));
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => place(btns.find(b => b.getAttribute('aria-selected') === 'true')));
    addEventListener('load', () => place(btns.find(b => b.getAttribute('aria-selected') === 'true')));
    requestAnimationFrame(() => place(btns.find(b => b.getAttribute('aria-selected') === 'true')));
  }

  /* ---------- odometer ---------- */
  const odo = $('#odo');
  const cols = $$('.dcol .s', odo);
  cols.forEach(s => { for (const d of [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]) { const e = document.createElement('div'); e.textContent = d; s.appendChild(e); } });
  function setOdo(v) {
    if (v === null || v === undefined) { odo.classList.add('none'); return; }
    odo.classList.remove('none');
    const s = String(Math.round(v)).padStart(3, ' ');
    cols.forEach((c, i) => { const ch = s[i]; c.parentElement.classList.toggle('off', ch === ' '); c.style.transform = `translateY(${-(ch === ' ' ? 0 : +ch) * 1.05}em)`; });
  }

  /* ---------- energy bars ---------- */
  const bars = $('#bars'), tip = $('#tip'), bw = () => parseFloat(getComputedStyle(bars).getPropertyValue('--bw')), gap = () => parseFloat(getComputedStyle(bars).getPropertyValue('--gap'));
  const B = [];
  for (let i = 0; i < 7; i++) {
    const tr = document.createElement('div'); tr.className = 'track';
    const f = document.createElement('div'); f.className = 'fill';
    const h = document.createElement('div'); h.className = 'hot'; f.appendChild(h); tr.appendChild(f);
    const bl = document.createElement('div'); bl.className = 'bl'; const sp = document.createElement('span'); bl.appendChild(sp);
    bars.insertBefore(tr, tip); bars.appendChild(bl); B.push({ tr, f, h, bl, sp });
  }
  function layoutBars() { B.forEach((b, i) => { const x = i * (bw() + gap()) + 'px'; b.tr.style.left = x; b.bl.style.left = x; }); }
  function setBars(vals, labels) {
    layoutBars();
    let hi = -1; vals.forEach((v, i) => { if (v !== null && (hi < 0 || v > vals[hi])) hi = i; });
    B.forEach((b, i) => {
      const v = vals[i];
      b.f.style.height = v === null ? '0%' : Math.max(3, Math.min(100, v)) + '%';
      b.h.style.opacity = i === hi ? 1 : 0;
      swap(b.sp, labels[i]);
    });
    if (hi >= 0) {
      tip.style.opacity = 1;
      const x = hi * (bw() + gap()) + bw() / 2, q = 1 - Math.min(100, vals[hi]) / 100;
      tip.style.transform = `translate(${x}px, calc(${q.toFixed(3)} * var(--th) - 40px)) translateX(-50%)`;
      swap(tip, String(Math.round(vals[hi])));
    } else tip.style.opacity = 0;
  }

  /* ---------- views ---------- */
  function renderEnergy(v, intro) {
    const d = H.energy[v]; if (!d) return;
    swap($('#eLbl'), d.label);
    swap($('#eDelta'), d.delta.text, tone(d.delta.tone));
    if (intro) { setOdo(0); setBars(d.bars.map(() => 0), d.labels); requestAnimationFrame(() => setTimeout(() => { setOdo(d.value); setBars(d.bars, d.labels); }, 120)); }
    else { setOdo(d.value); setBars(d.bars, d.labels); }
  }
  const STAGE_COL = { Deep: 'var(--deep)', Light: 'var(--light)', REM: 'var(--rem)', Awake: 'var(--awake)' };
  function renderSleep(v) {
    const S = H.sleep; if (!S || !S[v]) return;
    const d = S[v];
    swap($('#sTotal'), d.total); swap($('#sLine'), d.line); swap($('#sWindow'), d.window); swap($('#sLabel'), d.label);
    const tot = d.stages.reduce((a, s) => a + (s.s || 0), 0) || 1;
    d.stages.forEach((s, i) => { swap($(`#st${i} .v`), s.txt); $(`#sb${i}`).style.width = ((s.s || 0) / tot * 100) + '%'; });
  }
  const C = 2 * Math.PI * 76;
  function renderActivity(v, intro) {
    const d = H.activity[v]; if (!d) return;
    const p = Math.max(0, Math.min(1, d.pct || 0));
    const set = q => { for (const r of $$('.ring, .ring-g')) r.setAttribute('stroke-dasharray', `${(C * q).toFixed(1)} ${C.toFixed(1)}`);
      const a = (-90 + 360 * q) * Math.PI / 180; for (const c of $$('.rdot')) { c.setAttribute('cx', 88 + 76 * Math.cos(a)); c.setAttribute('cy', 88 + 76 * Math.sin(a)); c.style.opacity = q > .005 ? 1 : 0; } };
    if (intro) { set(0); setTimeout(() => set(p), 200); } else set(p);
    swap($('#aVal'), d.value); swap($('#aOf'), d.of);
    const chip = $('#aChip'), done = (d.pct || 0) >= 1;
    chip.classList.toggle('on', done);
    swap($('#aChipT'), done ? 'Goal reached' : `${Math.round((d.pct || 0) * 100)}% of goal`);
    swap($('#aTitle'), d.title); swap($('#aSub'), d.sub);
  }
  const W = $$('#wave .wb');
  function renderHeart(v, intro) {
    const d = H.heart[v]; if (!d) return;
    const vals = d.wave, ok = vals.filter(x => x !== null && x !== undefined);
    if (!ok.length) { W.forEach(b => { b.style.height = '4px'; b.className = 'wb'; }); }
    const lo = Math.min(...ok), hi = Math.max(...ok), r = (hi - lo) || 1;
    const go = () => W.forEach((b, i) => {
      const x = vals[i];
      if (x === null || x === undefined) { b.style.height = '4px'; b.className = 'wb'; return; }
      const q = (x - lo) / r;  // sqrt lifts the quiet stretches so the shape reads as a waveform
      b.style.height = `calc(10px + ${Math.sqrt(q).toFixed(3)} * (100% - 14px))`;
      b.className = 'wb on' + (q > .85 ? ' hi' : '');
    });
    if (intro) { W.forEach(b => { b.style.height = '8px'; b.className = 'wb'; }); setTimeout(go, 260); } else go();
    swap($('#hTitle'), d.title); swap($('#hSub'), d.sub); swap($('#hRight'), d.right); swap($('#wCap'), d.wave_caption);
  }
  function render(v, intro) { renderEnergy(v, intro); renderSleep(v); renderActivity(v, intro); renderHeart(v, intro); store.set('gd-view', v); }

  /* ---------- sleep expand (FLIP-free: animate absolute box) ---------- */
  const hero = $('#hero'), sc = $('#sleep');
  let ph = null;
  function rectIn(el) { const a = el.getBoundingClientRect(), b = hero.getBoundingClientRect(); return { l: a.left - b.left, t: a.top - b.top, w: a.width, h: a.height }; }
  function boxTo(r) { Object.assign(sc.style, { left: r.l + 'px', top: r.t + 'px', width: r.w + 'px', height: r.h + 'px' }); }
  function openSleep() {
    if (sc.classList.contains('open')) return;
    const r0 = rectIn(sc);
    ph = document.createElement('div'); ph.style.height = r0.h + 'px'; sc.after(ph);
    sc.classList.add('float'); boxTo(r0);
    // measure the open layout's natural height at full width
    sc.classList.add('open'); sc.style.height = 'auto'; sc.style.width = hero.offsetWidth + 'px';
    const target = Math.max(sc.scrollHeight, innerWidth > 900 ? hero.offsetHeight : 0);
    sc.classList.remove('open'); boxTo(r0); void sc.offsetWidth;
    hero.style.minHeight = target + 'px';
    hero.classList.add('expanded'); sc.classList.add('open');
    boxTo({ l: 0, t: 0, w: hero.offsetWidth, h: target });
    $('#sOpen').setAttribute('aria-expanded', 'true');
    setTimeout(drawTrend, reduce ? 0 : 350);
    $('#sClose').focus({ preventScroll: true });
  }
  function closeSleep() {
    if (!sc.classList.contains('open')) return;
    const r1 = rectIn(ph);
    sc.classList.remove('open'); hero.classList.remove('expanded');
    boxTo(r1); hero.style.minHeight = '';
    const done = () => { sc.classList.remove('float'); sc.removeAttribute('style'); if (ph) { ph.remove(); ph = null; } };
    reduce ? done() : setTimeout(done, 620);
    $('#sOpen').setAttribute('aria-expanded', 'false'); $('#sOpen').focus({ preventScroll: true });
  }
  $('#sOpen').addEventListener('click', openSleep);
  $('#sClose').addEventListener('click', closeSleep);
  addEventListener('keydown', e => { if (e.key === 'Escape') { closeSleep(); closeMenu(); } });
  addEventListener('resize', () => { if (sc.classList.contains('open')) { boxTo({ l: 0, t: 0, w: hero.offsetWidth, h: sc.offsetHeight }); drawTrend(); } });

  /* sleep score curve (30 nights), drawn like a hypnogram line */
  function drawTrend() {
    const T = (H.sleep && H.sleep.trend) || [], svg = $('#trendSvg'); if (!svg || T.length < 2) return;
    const w = svg.clientWidth || 600, h = 200, L = 34, R = 8, top = 14, bot = 30;
    const lo = Math.min(40, Math.min(...T.map(t => t.score)) - 5), hi = 100;
    const x = i => L + (w - L - R) * i / (T.length - 1), y = s => top + (h - top - bot) * (1 - (s - lo) / (hi - lo));
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`); svg.setAttribute('height', h);
    const P = T.map((t, i) => [x(i), y(t.score)]);
    let d = `M${P[0][0].toFixed(1)} ${P[0][1].toFixed(1)}`;
    for (let i = 1; i < P.length; i++) { const [x0, y0] = P[i - 1], [x1, y1] = P[i], mx = (x0 + x1) / 2; d += ` C${mx.toFixed(1)} ${y0.toFixed(1)} ${mx.toFixed(1)} ${y1.toFixed(1)} ${x1.toFixed(1)} ${y1.toFixed(1)}`; }
    const grid = [60, 80, 100].filter(v => v > lo).map(v => `<line x1="${L}" x2="${w - R}" y1="${y(v)}" y2="${y(v)}" stroke="rgba(255,255,255,.06)" stroke-dasharray="2 6"/><text x="0" y="${y(v) + 4}">${v}</text>`).join('');
    const fmt = s => new Date(s + 'T12:00:00').toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
    const xl = [0, Math.floor((T.length - 1) / 2), T.length - 1].map((i, k) => `<text x="${x(i)}" y="${h - 6}" text-anchor="${k === 0 ? 'start' : k === 2 ? 'end' : 'middle'}">${fmt(T[i].d)}</text>`).join('');
    const last = P[P.length - 1];
    svg.innerHTML = `<defs>
        <linearGradient id="hl" gradientUnits="userSpaceOnUse" x1="${L}" y1="0" x2="${w}" y2="0"><stop offset="0" stop-color="#8b78f6"/><stop offset=".5" stop-color="#b6a5ff"/><stop offset=".74" stop-color="#eaa2bf"/><stop offset="1" stop-color="#f7955a"/></linearGradient>
        <linearGradient id="ha" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#9483f7" stop-opacity=".30"/><stop offset="1" stop-color="#9483f7" stop-opacity="0"/></linearGradient>
        <filter id="glow" x="-20%" y="-50%" width="140%" height="200%"><feGaussianBlur stdDeviation="5"/></filter></defs>
      <g font-family="'Geist Mono', ui-monospace, monospace" font-size="11" fill="#77777e">${grid}${xl}</g>
      <path id="tArea" d="${d} L${last[0]} ${h - bot} L${P[0][0]} ${h - bot} Z" fill="url(#ha)" opacity="0"/>
      <path id="tGlow" d="${d}" fill="none" stroke="url(#hl)" stroke-width="7" filter="url(#glow)" opacity=".55"/>
      <path id="tLine" d="${d}" fill="none" stroke="url(#hl)" stroke-width="2.6" stroke-linecap="round"/>
      <circle id="tHalo" cx="${last[0]}" cy="${last[1]}" r="13" fill="#fff" opacity=".25" filter="url(#glow)"/>
      <circle id="tHead" cx="${last[0]}" cy="${last[1]}" r="5" fill="#fff"/>`;
    const line = $('#tLine'), glow = $('#tGlow'), len = line.getTotalLength();
    if (!reduce) {
      for (const p of [line, glow]) { p.style.strokeDasharray = `${len} ${len + 10}`; p.style.strokeDashoffset = len;
        p.animate([{ strokeDashoffset: len }, { strokeDashoffset: 0 }], { duration: 1500, easing: 'cubic-bezier(.65,0,.35,1)', fill: 'forwards' }); }
      for (const c of [$('#tHead'), $('#tHalo')]) c.animate([{ opacity: 0 }, { opacity: c.id === 'tHalo' ? .25 : 1 }], { duration: 300, delay: 1350, fill: 'both' });
    }
    $('#tArea').animate([{ opacity: 0 }, { opacity: 1 }], { duration: reduce ? 0 : 900, delay: reduce ? 0 : 700, fill: 'forwards' });
    const tt = $('#trendTip');
    svg.onpointermove = e => {
      const bx = svg.getBoundingClientRect(), px = (e.clientX - bx.left) * (w / bx.width);
      const i = Math.max(0, Math.min(T.length - 1, Math.round((px - L) / (w - L - R) * (T.length - 1))));
      $('#tHead').setAttribute('cx', P[i][0]); $('#tHead').setAttribute('cy', P[i][1]); $('#tHalo').setAttribute('cx', P[i][0]); $('#tHalo').setAttribute('cy', P[i][1]);
      tt.textContent = `${fmt(T[i].d)} · score ${T[i].score} · ${T[i].h.toFixed(1)} h`;
      tt.style.left = (P[i][0] / w * bx.width) + 'px'; tt.style.opacity = 1;
    };
    svg.onpointerleave = () => { tt.style.opacity = 0; $('#tHead').setAttribute('cx', last[0]); $('#tHead').setAttribute('cy', last[1]); $('#tHalo').setAttribute('cx', last[0]); $('#tHalo').setAttribute('cy', last[1]); };
  }

  /* ---------- signals menu ---------- */
  const bell = $('#bell'), menu = $('#menu');
  function closeMenu() { menu.classList.remove('open'); bell.setAttribute('aria-expanded', 'false'); }
  bell.addEventListener('click', e => { e.stopPropagation(); const o = !menu.classList.contains('open'); menu.classList.toggle('open', o); bell.setAttribute('aria-expanded', String(o)); });
  const strip = $('#strip'); if (strip) strip.addEventListener('click', e => { e.stopPropagation(); menu.classList.add('open'); bell.setAttribute('aria-expanded', 'true'); });
  document.addEventListener('click', e => { if (!menu.contains(e.target)) closeMenu(); });
  $('#lock').addEventListener('click', () => {
    try { Object.keys(localStorage).filter(k => k.startsWith('gd-key')).forEach(k => localStorage.removeItem(k)); } catch (e) {}
    location.reload();
  });
  $('#aGo').addEventListener('click', () => $('#workouts').scrollIntoView({ behavior: reduce ? 'auto' : 'smooth' }));

  /* ---------- trends (Plotly) ---------- */
  const CFG = { displayModeBar: false, responsive: true };
  for (const [k, f] of Object.entries(FIGS)) {
    f.layout.font = Object.assign({}, f.layout.font, { family: "Geist, system-ui, sans-serif" });
    f.layout.hoverlabel = { bgcolor: '#f4f4f5', bordercolor: '#f4f4f5', font: { family: 'Geist, system-ui, sans-serif', size: 12, color: '#111' } };
    Plotly.newPlot('c-' + k, f.data, f.layout, CFG);
  }
  function setRange(days) {
    const e = new Date(END + 'T12:00:00'), s = new Date(e); s.setDate(s.getDate() - days + 1); s.setHours(0);
    const iso = d => d.toISOString().slice(0, 13).replace('T', ' ') + ':00';
    for (const k of DATE_FIGS) Plotly.relayout('c-' + k, { 'xaxis.range': [iso(s), iso(e)] });
  }

  /* ---------- boot ---------- */
  $('#hello').textContent = hello();
  const saved = store.get('gd-view');
  initSwitch($('#viewSwitch'), (v, first) => render(v, !!first), ['today', 'week', 'month'].includes(saved) ? saved : 'today');
  initSwitch($('#rangeSwitch'), (v, first) => { if (!first) setRange(+v); }, '90');
  addEventListener('resize', () => { const v = $('#viewSwitch [aria-selected="true"]').dataset.v; setBars(H.energy[v].bars, H.energy[v].labels); });
})();
