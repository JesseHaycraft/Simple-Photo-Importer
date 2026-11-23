'use strict';

const GAP_MINUTES = 120;          // a pause this long inside one day is pointed out as a possible split
const SAMPLE_ROWS = 2;            // rows of thumbnails shown per day before "show all"

const $ = sel => document.querySelector(sel);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k in el) el[k] = v;
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const b = text => h('b', null, text);

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Photo-Importer': '1' },
    body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || 'Something went wrong.');
  return data;
}

const S = {
  config: null, source: '', scan: null, shots: [], events: [],
  joined: new Set(),      // day boundaries the user merged (keyed by the first shot after the boundary)
  splits: new Set(),      // extra breaks inside a day
  asked: {},              // answers to "same event?" for back-to-back days
  meta: {},               // per event (keyed by its first shot): name, existing folder, mode
  excluded: new Set(),    // shots left out of this import
  expanded: new Set(),    // days showing every photo
  folderSet: new Set(), cols: 6, lb: -1, showWhere: false,
};

// ------------------------------------------------------------------ helpers

const nf = n => n.toLocaleString();
const plural = (n, word, many) => `${nf(n)} ${n === 1 ? word : (many || word + 's')}`;
function fmtBytes(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + ' GB';
  if (n >= 1e6) return Math.round(n / 1e6) + ' MB';
  if (!n) return '0 MB';
  return Math.max(1, Math.round(n / 1e3)) + ' KB';
}
function dayDate(day) { const [y, m, d] = day.split('-').map(Number); return new Date(y, m - 1, d); }
function fmtDay(day, year) {
  return dayDate(day).toLocaleDateString(undefined,
    { weekday: 'short', month: 'short', day: 'numeric', ...(year ? { year: 'numeric' } : {}) });
}
function fmtTime(shot) {
  return new Date(shot.ms).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
}
function addDays(day, n) {
  const d = dayDate(day); d.setDate(d.getDate() + n);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function daysBetween(a, b) {
  const [y1, m1, d1] = a.split('-').map(Number), [y2, m2, d2] = b.split('-').map(Number);
  return Math.round((Date.UTC(y2, m2 - 1, d2) - Date.UTC(y1, m1 - 1, d1)) / 864e5);
}
function fmtGap(minutes) {
  const hrs = Math.floor(minutes / 60), min = Math.round(minutes % 60);
  return min ? `${hrs} h ${min} min` : `${hrs} h`;
}
function cleanName(text) {
  return (text || '').replace(/[<>:"/\\|?*\x00-\x1f]/g, ' ').replace(/\s+/g, ' ').trim().replace(/[. ]+$/, '');
}
const baseName = path => path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || path;

// ----------------------------------------------------------- grouping model

const keyAt = i => S.shots[i].key;
const isDayStart = i => i > 0 && S.shots[i].day !== S.shots[i - 1].day;
const dayGap = i => daysBetween(S.shots[i - 1].day, S.shots[i].day);
const isBreak = i => i > 0 && (isDayStart(i) ? !S.joined.has(keyAt(i)) : S.splits.has(keyAt(i)));
const gapMinutes = i => (S.shots[i].ms - S.shots[i - 1].ms) / 60000;

function setBreak(i, on) {
  const k = keyAt(i);
  if (isDayStart(i)) {
    if (on) S.joined.delete(k); else S.joined.add(k);
    if (dayGap(i) === 1) S.asked[k] = on ? 'separate' : 'same';
  } else if (on) S.splits.add(k); else S.splits.delete(k);
  if (!on) {
    // carry a name across when the first half had none
    const first = S.events.find(ev => i - 1 >= ev.a && i - 1 < ev.b);
    const second = S.meta[k];
    if (first && second && !first.meta.name && !first.meta.existing) {
      first.meta.name = second.name; first.meta.existing = second.existing;
    }
  }
}

function targetOf(ev) {
  if (ev.meta.existing) return ev.meta.existing;
  return cleanName(ev.firstDay + ' ' + cleanName(ev.meta.name));
}

function countEvent(ev) {
  ev.count = 0; ev.files = 0; ev.bytes = 0;
  for (let i = ev.a; i < ev.b; i++) {
    const s = S.shots[i];
    if (S.excluded.has(s.key)) continue;
    ev.count++; ev.files += s.files.length; ev.bytes += s.bytes;
  }
}

function computeTargets() {
  const byTarget = new Map();
  for (const ev of S.events) {
    ev.target = targetOf(ev);
    ev.clash = false;
    if (ev.meta.mode !== 'import' || !ev.count) continue;
    const t = ev.target.toLowerCase();
    if (!byTarget.has(t)) byTarget.set(t, []);
    byTarget.get(t).push(ev);
  }
  for (const list of byTarget.values()) {
    if (list.length > 1 && !list.every(ev => ev.meta.existing)) list.forEach(ev => { ev.clash = true; });
  }
}

function computeEvents() {
  const evs = [], n = S.shots.length;
  let a = 0;
  for (let i = 1; i <= n; i++) {
    if (i < n && !isBreak(i)) continue;
    const first = S.shots[a];
    const meta = S.meta[first.key] || (S.meta[first.key] = { name: '', existing: '', mode: 'import' });
    const ev = { a, b: i, key: first.key, meta, firstDay: first.day, lastDay: S.shots[i - 1].day, days: [] };
    let d = a;
    for (let j = a + 1; j <= i; j++) {
      if (j === i || S.shots[j].day !== S.shots[d].day) { ev.days.push({ day: S.shots[d].day, a: d, b: j }); d = j; }
    }
    countEvent(ev);
    evs.push(ev);
    a = i;
  }
  S.events = evs;
  computeTargets();
}

const eventOf = i => S.events.find(ev => i >= ev.a && i < ev.b);
const unanswered = () => {
  const out = [];
  for (let i = 1; i < S.shots.length; i++) if (isDayStart(i) && dayGap(i) === 1 && !S.asked[keyAt(i)]) out.push(i);
  return out;
};

// ------------------------------------------------------- remembering choices

const storeKey = () => `photo-importer|${S.scan.source}|${S.scan.library}`;
function save() {
  if (!S.scan) return;
  try {
    localStorage.setItem(storeKey(), JSON.stringify({
      joined: [...S.joined], splits: [...S.splits], asked: S.asked, meta: S.meta, excluded: [...S.excluded],
    }));
  } catch (e) { /* storage unavailable: choices just aren't remembered */ }
}
function restore() {
  S.joined = new Set(); S.splits = new Set(); S.asked = {}; S.meta = {}; S.excluded = new Set(); S.expanded = new Set();
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(storeKey()) || 'null'); } catch (e) { /* ignore */ }
  if (!saved) return;
  const known = new Set(S.shots.map(s => s.key));
  const keep = list => new Set((list || []).filter(k => known.has(k)));
  S.joined = keep(saved.joined); S.splits = keep(saved.splits); S.excluded = keep(saved.excluded);
  for (const [k, v] of Object.entries(saved.asked || {})) if (known.has(k)) S.asked[k] = v;
  for (const [k, v] of Object.entries(saved.meta || {})) {
    if (known.has(k) && v) S.meta[k] = { name: v.name || '', existing: v.existing || '', mode: v.mode || 'import' };
  }
}

// ------------------------------------------------------- thumbnail loading
// Thumbnails are fetched a few at a time: whatever is on screen first, then
// the rest of the page in the background so that scrolling finds them ready.
// A failed load is retried instead of leaving a blank tile.

const Thumbs = {
  MAX: 4, active: 0, waiting: new Set(), loaded: new Set(), moved: true,

  reset() { this.waiting.clear(); this.moved = true; },

  add(el, img, url) {
    el._img = img; el._url = url; el._tries = 0;
    if (this.loaded.has(url)) { img.src = url; el.classList.add('ready'); return; }
    this.waiting.add(el);
  },

  /** Note which waiting tiles are on (or close to) the screen right now. */
  look() {
    const top = -200, bottom = window.innerHeight + 400;
    for (const el of this.waiting) {
      const r = el.getBoundingClientRect();
      el._near = r.bottom > top && r.top < bottom;
    }
    this.moved = false;
    this.seenY = window.scrollY;
  },

  next() {
    if (this.moved || window.scrollY !== this.seenY) this.look();
    let first = null;
    for (const el of this.waiting) {
      if (!el.isConnected) { this.waiting.delete(el); continue; }
      if (el._near) return el;           // on (or close to) the screen: goes first
      first = first || el;
    }
    return first;                        // otherwise carry on down the page
  },

  pump() {
    while (this.active < this.MAX) {
      const el = this.next();
      if (!el) return;
      this.waiting.delete(el);
      this.start(el);
    }
  },

  start(el) {
    const img = el._img, url = el._url;
    this.active++;
    const finish = ok => {
      img.onload = img.onerror = null;
      this.active--;
      if (ok) { this.loaded.add(url); el.classList.add('ready'); }
      else if (++el._tries < 3) {
        setTimeout(() => { if (el.isConnected) { this.waiting.add(el); this.moved = true; this.pump(); } }, 500 * el._tries);
      } else el.classList.add('noimg');
      this.pump();
    };
    img.onload = () => finish(true);
    img.onerror = () => finish(false);
    img.src = el._tries ? `${url}?retry=${el._tries}` : url;
  },
};
const thumbsMoved = () => { Thumbs.moved = true; Thumbs.pump(); };
window.addEventListener('scroll', thumbsMoved, { passive: true });
window.addEventListener('resize', thumbsMoved);

// ---------------------------------------------------------------- rendering

function measureCols() {
  const width = Math.min($('#main').clientWidth, 1500) - 40 - 34;
  return Math.max(2, Math.floor((width + 6) / (168 + 6)));
}

/** Re-render while keeping the clicked spot under the mouse. */
function rerender(anchorEl) {
  const holder = anchorEl && anchorEl.closest('[data-anchor]');
  const id = holder && holder.dataset.anchor;
  const before = holder ? holder.getBoundingClientRect().top : 0;
  render();
  if (id) {
    const now = document.querySelector(`[data-anchor="${CSS.escape(id)}"]`);
    if (now) window.scrollBy(0, now.getBoundingClientRect().top - before);
  }
}

function render() {
  computeEvents();
  S.cols = measureCols();
  Thumbs.reset();
  const frag = document.createDocumentFragment();
  S.events.forEach((ev, n) => {
    if (n > 0) frag.append(renderConnector(ev));
    frag.append(renderEvent(ev));
  });
  $('#main').replaceChildren(frag);
  paintFooter();
  save();
  Thumbs.pump();
}

function renderConnector(ev) {
  const i = ev.a, k = keyAt(i), prev = S.shots[i - 1], cur = S.shots[i];
  const wrap = h('div', { class: 'connector', dataset: { anchor: 'b:' + k } });
  const join = label => h('button', { class: 'link', onclick: e => { setBreak(i, false); rerender(e.target); } }, label);
  if (!isDayStart(i)) {
    wrap.append(h('div', { class: 'slim' }, `Split on ${fmtDay(cur.day)} at ${fmtTime(cur)}`, join('Undo split')));
  } else if (dayGap(i) === 1 && !S.asked[k]) {
    wrap.append(h('div', { class: 'ask' },
      h('span', null, b(fmtDay(prev.day)), ' and ', b(fmtDay(cur.day)), ' are back-to-back days. Same event or trip?'),
      h('button', { class: 'btn small yes', onclick: e => { setBreak(i, false); rerender(e.target); } }, 'Same event'),
      h('button', { class: 'btn small', onclick: e => { setBreak(i, true); rerender(e.target); } }, 'Separate')));
  } else if (dayGap(i) === 1) {
    wrap.append(h('div', { class: 'slim' }, 'Back-to-back days, kept separate', join('Join into one event')));
  } else {
    wrap.append(h('div', { class: 'slim' }, `${dayGap(i)} days later`, join('Same event? Join')));
  }
  return wrap;
}

function renderEvent(ev) {
  const el = h('section', { class: 'event', dataset: { anchor: 'e:' + ev.key } });
  ev.el = el;
  const multi = ev.firstDay !== ev.lastDay;
  const dateText = multi ? `${fmtDay(ev.firstDay)} – ${fmtDay(ev.lastDay, true)}` : fmtDay(ev.firstDay, true);
  const span = multi ? h('small', null, `${daysBetween(ev.firstDay, ev.lastDay) + 1} days`) : null;

  let nameBox;
  if (ev.meta.existing) {
    nameBox = h('div', { class: 'ev-existing' },
      h('span', null, 'Adding to existing folder ', b(ev.meta.existing)),
      h('button', { class: 'link', onclick: e => { ev.meta.existing = ''; rerender(e.target); } }, 'Change'));
  } else {
    nameBox = h('input', {
      class: 'ev-name', type: 'text', value: ev.meta.name, spellcheck: true, maxLength: 100,
      placeholder: multi ? 'Name this trip or event, e.g. "Week in Mexico"' : 'Name this event (optional), e.g. "Tom\'s birthday"',
      oninput: e => { ev.meta.name = e.target.value; computeTargets(); S.events.forEach(paintFolder); paintFooter(); save(); },
      onkeydown: e => {
        if (e.key !== 'Enter') return;
        const all = [...document.querySelectorAll('.ev-name')];
        const next = all[all.indexOf(e.target) + 1];
        if (next) next.focus(); else e.target.blur();
      },
    });
  }
  const seg = h('div', { class: 'seg' }, ['import', 'later', 'never'].map(mode => h('button', {
    class: (ev.meta.mode === mode ? 'on' : '') + (mode === 'import' ? ' go' : ''),
    title: { import: 'Copy these photos into the library', later: 'Skip for now. They will be offered again next time.',
             never: 'Do not import, and stop offering these photos in future.' }[mode],
    onclick: e => { ev.meta.mode = mode; rerender(e.target); },
  }, { import: 'Import', later: 'Not now', never: 'Never' }[mode])));

  ev.statsEl = h('div', { class: 'ev-stats' });
  ev.folderEl = h('div', { class: 'ev-folder' });
  el.append(h('div', { class: 'ev-head' }, h('div', { class: 'ev-date' }, dateText, span), nameBox, ev.statsEl, seg), ev.folderEl);

  ev.days.forEach((seg, n) => {
    if (n > 0) {
      el.append(h('div', { class: 'inner', dataset: { anchor: 'b:' + keyAt(seg.a) } }, 'same event continues',
        h('button', { class: 'link', onclick: e => { setBreak(seg.a, true); rerender(e.target); } }, 'Split here')));
    }
    el.append(renderDay(seg, ev));
  });
  paintFolder(ev);
  return el;
}

function paintFolder(ev) {
  if (!ev.el) return;
  ev.el.classList.toggle('off', ev.meta.mode !== 'import');
  ev.el.classList.toggle('problem', ev.clash);
  const left = ev.b - ev.a - ev.count;
  ev.statsEl.textContent = `${plural(ev.count, 'photo')} · ${fmtBytes(ev.bytes)}` + (left ? ` · ${nf(left)} left out` : '');
  const sub = S.config.settings.subfolder;
  const kids = [];
  if (ev.meta.mode === 'import') {
    const exists = S.folderSet.has(ev.target.toLowerCase());
    kids.push(h('span', { class: 'dest' }, `${baseName(S.scan.library)} › `, b(ev.target), sub ? ` › ${sub}` : ''));
    if (ev.clash) kids.push(h('span', { class: 'tag bad' }, 'same folder as another event: give them different names, or join them'));
    else if (exists) kids.push(h('span', { class: 'tag exists' }, 'folder already exists, photos will be added to it'));
    else kids.push(h('span', { class: 'tag new' }, 'new folder'));
    if (!ev.meta.existing) {
      const lo = addDays(ev.firstDay, -1), hi = addDays(ev.lastDay, 1);
      const near = S.scan.folders.filter(f => f.date >= lo && f.date <= hi && f.folder.toLowerCase() !== ev.target.toLowerCase());
      near.slice(0, 4).forEach(f => kids.push(h('button', {
        class: 'chip', title: 'Put these photos in this existing folder instead',
        onclick: e => { ev.meta.existing = f.folder; rerender(e.target); },
      }, `Add to "${f.folder}" instead`)));
    }
  } else {
    kids.push(ev.meta.mode === 'later' ? 'Skipped for now. These will be offered again next time.'
      : 'Will not be imported, and will not be offered again.');
  }
  ev.folderEl.replaceChildren(...kids);
}

function renderDay(seg, ev) {
  const n = seg.b - seg.a, first = S.shots[seg.a], last = S.shots[seg.b - 1];
  const id = first.key;
  const limit = S.cols * SAMPLE_ROWS;
  const expanded = S.expanded.has(id);
  let picks;
  if (expanded || n <= limit) {
    picks = Array.from({ length: n }, (_, k) => seg.a + k);
  } else {
    const set = new Set();
    for (let k = 0; k < limit - 1; k++) set.add(seg.a + Math.round(k * (n - 1) / (limit - 2)));
    for (let i = seg.a + 1; i < seg.b; i++) if (gapMinutes(i) >= GAP_MINUTES) { set.add(i - 1); set.add(i); }
    picks = [...set].sort((x, y) => x - y);
  }
  const grid = h('div', { class: 'grid' });
  for (const i of picks) {
    if (i > seg.a && gapMinutes(i) >= GAP_MINUTES) {
      grid.append(h('div', { class: 'cell gap' }, h('span', null, b(fmtGap(gapMinutes(i))), ' pause'),
        h('button', { class: 'btn small', onclick: e => { setBreak(i, true); rerender(e.target); } }, 'Split here')));
    }
    grid.append(renderTile(i, ev));
  }
  const toggle = e => { if (expanded) S.expanded.delete(id); else S.expanded.add(id); rerender(e.currentTarget); };
  if (picks.length < n) {
    grid.append(h('button', { class: 'cell more', onclick: toggle }, b(`+${nf(n - picks.length)} more`), 'Show all'));
  }
  const times = n > 1 ? `${fmtTime(first)} – ${fmtTime(last)}` : fmtTime(first);
  return h('div', { class: 'day', dataset: { anchor: 'd:' + id } },
    h('div', { class: 'day-head' },
      h('span', { class: 'd' }, fmtDay(seg.day)),
      h('span', { class: 'm' }, `${plural(n, 'photo')} · ${times}`),
      expanded && n > limit ? h('button', { class: 'link', onclick: toggle }, 'Show fewer') : null),
    grid);
}

function renderTile(i, ev) {
  const s = S.shots[i];
  const out = S.excluded.has(s.key);
  const img = h('img', { alt: '', decoding: 'async' });
  const xBtn = h('button', { class: 'x', title: 'Leave this photo out of the import (it stays on the card)' }, out ? 'Left out' : 'Leave out');
  const el = h('div', { class: 'tile' + (out ? ' out' : ''), title: `${s.name} · ${fmtDay(s.day)} ${fmtTime(s)}` },
    img, h('div', { class: 'ph' }, s.name),
    h('span', { class: 'time' }, fmtTime(s)),
    s.badge ? h('span', { class: 'kind' }, s.badge) : null,
    h('div', { class: 'acts' },
      i > ev.a ? h('button', { class: 'cut', title: 'Start a new event from this photo',
        onclick: e => { e.stopPropagation(); setBreak(i, true); rerender(e.target); } }, 'Split here') : h('span'),
      xBtn));
  Thumbs.add(el, img, `/thumb/${S.scan.scanId}/${s.thumb}`);
  xBtn.addEventListener('click', e => { e.stopPropagation(); toggleExcluded(i); });
  el.addEventListener('click', () => openLightbox(i));
  s.el = el; s.xBtn = xBtn;
  return el;
}

function toggleExcluded(i) {
  const s = S.shots[i];
  if (S.excluded.has(s.key)) S.excluded.delete(s.key); else S.excluded.add(s.key);
  const out = S.excluded.has(s.key);
  if (s.el && s.el.isConnected) { s.el.classList.toggle('out', out); s.xBtn.textContent = out ? 'Left out' : 'Leave out'; }
  const ev = eventOf(i);
  countEvent(ev); computeTargets(); S.events.forEach(paintFolder); paintFooter(); save();
}

function paintFooter() {
  const bar = $('#bottom');
  if (!S.shots.length) { bar.hidden = true; return; }
  bar.hidden = false;
  const going = S.events.filter(ev => ev.meta.mode === 'import' && ev.count);
  const never = S.events.filter(ev => ev.meta.mode === 'never');
  const shots = going.reduce((n, ev) => n + ev.count, 0);
  const files = going.reduce((n, ev) => n + ev.files, 0);
  const bytes = going.reduce((n, ev) => n + ev.bytes, 0);
  const folders = new Set(going.map(ev => ev.target.toLowerCase())).size;
  const text = $('#bottom-text');
  text.replaceChildren(...(shots
    ? [b(plural(shots, 'photo')), ` (${plural(files, 'file')}, ${fmtBytes(bytes)}) into `, b(plural(folders, 'folder'))]
    : ['Nothing selected to import']),
    S.shots.length - shots ? ` · ${nf(S.shots.length - shots)} not being imported` : '');
  const open = unanswered(), clash = S.events.find(ev => ev.clash);
  const btn = $('#import');
  $('#bulk-separate').hidden = !open.length;
  btn.disabled = false;
  if (open.length) {
    btn.className = 'btn warn';
    btn.textContent = `Answer ${plural(open.length, 'question')} first`;
    btn.onclick = () => scrollToAnchor('b:' + keyAt(open[0]));
  } else if (clash) {
    btn.className = 'btn warn';
    btn.textContent = 'Fix folder names first';
    btn.onclick = () => scrollToAnchor('e:' + clash.key);
  } else {
    btn.className = 'btn primary';
    btn.textContent = shots ? `Import ${plural(shots, 'photo')}…` : 'Save choices';
    btn.disabled = !shots && !never.length;
    btn.onclick = confirmImport;
  }
}

function scrollToAnchor(id) {
  const el = document.querySelector(`[data-anchor="${CSS.escape(id)}"]`);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

// ---------------------------------------------------------------- lightbox

function lockScroll() {
  document.documentElement.style.overflow = ($('#modal').hidden && $('#lightbox').hidden) ? '' : 'hidden';
}
function openLightbox(i) { S.lb = i; paintLightbox(); lockScroll(); }
function closeLightbox() { S.lb = -1; $('#lightbox').hidden = true; $('#lightbox').replaceChildren(); lockScroll(); }
function paintLightbox() {
  const i = S.lb, s = S.shots[i], box = $('#lightbox');
  const ev = eventOf(i), out = S.excluded.has(s.key);
  const go = d => { const j = i + d; if (j >= 0 && j < S.shots.length) { S.lb = j; paintLightbox(); } };
  box.replaceChildren(
    h('div', { class: 'stage', onclick: e => { if (e.target === e.currentTarget) closeLightbox(); } },
      h('img', { src: `/preview/${S.scan.scanId}/${s.thumb}`, alt: s.name })),
    h('div', { class: 'bar' },
      h('span', { class: 'cap' }, b(s.name), ` · ${fmtDay(s.day, true)} ${fmtTime(s)} · `,
        s.files.map(f => `${f.name.split('.').pop().toUpperCase()} ${fmtBytes(f.size)}`).join(' + '),
        ` · photo ${nf(i + 1)} of ${nf(S.shots.length)}`),
      h('button', { class: 'btn small', onclick: () => { toggleExcluded(i); paintLightbox(); } }, out ? 'Include again' : 'Leave out'),
      i > ev.a ? h('button', { class: 'btn small', onclick: () => { setBreak(i, true); render(); paintLightbox(); } },
        'Start a new event here') : null),
    h('button', { class: 'nav prev', title: 'Previous (←)', onclick: () => go(-1) }, '‹'),
    h('button', { class: 'nav next', title: 'Next (→)', onclick: () => go(1) }, '›'),
    h('button', { class: 'btn small close', onclick: closeLightbox }, 'Close'));
  box.hidden = false;
  for (const j of [i + 1, i - 1]) {      // have the neighbours ready for the arrow keys
    if (S.shots[j]) new Image().src = `/preview/${S.scan.scanId}/${S.shots[j].thumb}`;
  }
}
document.addEventListener('keydown', e => {
  if (S.lb < 0) return;
  if (e.key === 'Escape') closeLightbox();
  else if (e.key === 'ArrowLeft' && S.lb > 0) { S.lb--; paintLightbox(); }
  else if (e.key === 'ArrowRight' && S.lb < S.shots.length - 1) { S.lb++; paintLightbox(); }
});

// ------------------------------------------------------------------ dialogs

function showModal(title, body, buttons) {
  const m = $('#modal');
  m.replaceChildren(h('div', { class: 'dialog' }, h('h2', null, title), h('div', { class: 'body' }, body),
    buttons && buttons.length ? h('div', { class: 'foot' }, buttons) : null));
  m.hidden = false;
  lockScroll();
}
function closeModal() { $('#modal').hidden = true; $('#modal').replaceChildren(); lockScroll(); }
const safeNote = text => h('div', { class: 'safe' }, text);

function buildPlan() {
  const events = [], ignore = [];
  for (const ev of S.events) {
    const ids = [];
    for (let i = ev.a; i < ev.b; i++) {
      const s = S.shots[i];
      if (ev.meta.mode === 'never') ignore.push(...s.files.map(f => f.id));
      else if (ev.meta.mode === 'import' && !S.excluded.has(s.key)) ids.push(...s.files.map(f => f.id));
    }
    if (ids.length) events.push({ folder: ev.target, files: ids, ev });
  }
  return { events, ignore };
}

function confirmImport() {
  const plan = buildPlan();
  const sub = S.config.settings.subfolder;
  const files = plan.events.reduce((n, e) => n + e.files.length, 0);
  const bytes = plan.events.reduce((n, e) => n + e.ev.bytes, 0);
  const rows = plan.events.map(e => h('tr', null,
    h('td', null, h('div', { class: 'p' }, e.folder + (sub ? ` \\ ${sub}` : '')),
      S.folderSet.has(e.folder.toLowerCase()) ? h('span', { class: 'tag exists' }, 'existing folder') : h('span', { class: 'tag new' }, 'new folder')),
    h('td', { class: 'n' }, plural(e.ev.count, 'photo')),
    h('td', { class: 'n' }, fmtBytes(e.ev.bytes))));
  const never = S.events.filter(ev => ev.meta.mode === 'never').reduce((n, ev) => n + (ev.b - ev.a), 0);
  const body = [
    files ? h('div', null, 'Copy ', b(plural(files, 'file')), ` (${fmtBytes(bytes)}) into `, b(S.scan.library), ':') : null,
    files ? h('table', { class: 'plan' }, rows) : null,
    never ? h('div', { class: 'note' }, `${plural(never, 'photo')} marked "Never" will be hidden from future imports. They are not deleted from the card.`) : null,
    !S.scan.libraryExists && files ? h('div', { class: 'note' }, 'The library folder does not exist yet and will be created.') : null,
    safeNote('Photos are copied. Nothing on the card is changed or deleted, and no existing library file is overwritten.'),
  ];
  showModal(files ? 'Ready to import' : 'Save choices', body, [
    h('button', { class: 'btn', onclick: closeModal }, 'Back'),
    h('button', { class: 'btn primary', onclick: () => startImport(plan) }, files ? 'Import now' : 'Save'),
  ]);
}

async function startImport(plan) {
  try {
    await api('/api/import', {
      scanId: S.scan.scanId, subfolder: S.config.settings.subfolder, ignore: plan.ignore,
      events: plan.events.map(e => ({ folder: e.folder, files: e.files })),
    });
  } catch (err) {
    return showModal('Could not start the import', h('div', null, err.message), [h('button', { class: 'btn primary', onclick: closeModal }, 'OK')]);
  }
  watchImport();
}

function watchImport() {
  S.importing = true;
  const bar = h('div'), cur = h('div', { class: 'cur' }, 'Starting…'), amount = h('div', { class: 'note' });
  const cancel = h('button', { class: 'btn', onclick: () => { cancel.disabled = true; cancel.textContent = 'Stopping…'; api('/api/cancel', {}); } }, 'Stop');
  showModal('Importing…', [h('div', { class: 'progress' }, bar), cur, amount,
    safeNote('Leave the card plugged in until this finishes.')], [cancel]);
  const tick = async () => {
    let st;
    try { st = await api('/api/status'); } catch (e) { return setTimeout(tick, 800); }
    if (st.state === 'running') {
      if (st.total) bar.style.width = (100 * st.done / st.total).toFixed(1) + '%';
      cur.textContent = st.detail || 'Starting…';
      amount.textContent = st.total ? `${fmtBytes(st.done)} of ${fmtBytes(st.total)}` : '';
      return setTimeout(tick, 300);
    }
    S.importing = false;
    if (st.state === 'error') {
      return showModal('The import did not run', h('div', null, st.error || 'Unknown problem.'),
        [h('button', { class: 'btn primary', onclick: () => { closeModal(); startScan(); } }, 'OK')]);
    }
    showResult(st.result);
  };
  tick();
}

function showResult(r) {
  const open = path => api('/api/open', { path }).catch(() => {});
  const body = [];
  if (r.total) {
    body.push(h('div', null, 'Copied ', b(plural(r.copied, 'file')), ` (${fmtBytes(r.bytes)}) into `, b(plural(r.folders.length, 'folder')), '.'));
    if (r.existed) body.push(h('div', { class: 'note' }, `${plural(r.existed, 'file')} were already in place and were left as they are.`));
    if (r.cancelled) body.push(h('div', { class: 'note' }, 'Stopped before finishing. The photos not yet copied will be offered again next time.'));
    if (r.failed.length) {
      body.push(h('div', { class: 'note' }, `${plural(r.failed.length, 'file')} could not be copied:`),
        h('ul', { class: 'errs' }, r.failed.slice(0, 12).map(f => h('li', null, `${f.name}: ${f.error}`))));
    }
    body.push(h('table', { class: 'plan' }, r.folders.map(f => h('tr', null,
      h('td', null, h('div', { class: 'p' }, f.path.slice(r.library.length + 1))),
      h('td', { class: 'n' }, plural(f.count, 'file')),
      h('td', { class: 'n' }, h('button', { class: 'link', onclick: () => open(f.path) }, 'Open'))))));
  }
  if (r.ignored) body.push(h('div', { class: 'note' }, `${plural(r.ignored, 'file')} will no longer be offered.`));
  body.push(safeNote('Nothing on the card was changed or deleted.'));
  if (r.log) body.push(h('div', { class: 'note' }, 'A list of every file copied was saved. ', h('button', { class: 'link', onclick: () => open(r.log) }, 'Open the log')));
  const title = r.failed.length ? 'Import finished with problems' : r.cancelled ? 'Import stopped' : r.total ? 'Import complete' : 'Saved';
  showModal(title, body, [
    r.total ? h('button', { class: 'btn', onclick: () => open(r.library) }, 'Open library folder') : null,
    h('button', { class: 'btn primary', onclick: () => { closeModal(); startScan(); } }, 'Done'),
  ]);
}

// ---------------------------------------------------------------- scanning

function showEmpty(title, text, button) {
  $('#bottom').hidden = true;
  $('#main').replaceChildren(h('div', { class: 'empty' }, h('h2', null, title), h('div', null, text), button || null));
}

async function startScan() {
  S.scan = null; S.shots = []; S.events = [];
  $('#summary').replaceChildren();
  if (!S.source) {
    return showEmpty('No camera card found',
      'Plug in the card (or the camera in "PC storage" mode), then click Rescan. You can also pick any folder with the From menu.',
      h('button', { class: 'btn primary', onclick: boot }, 'Rescan'));
  }
  try { await api('/api/scan', { source: S.source }); }
  catch (err) { return showEmpty('Could not read the card', err.message, h('button', { class: 'btn primary', onclick: boot }, 'Try again')); }
  watchScan();
}

function watchScan() {
  const bar = h('div'), wrap = h('div', { class: 'progress busy' }, bar), label = h('div', null, 'Starting…');
  $('#bottom').hidden = true;
  $('#main').replaceChildren(h('div', { class: 'empty' }, h('h2', null, 'Looking for new photos'), wrap, label));
  const tick = async () => {
    let st;
    try { st = await api('/api/status'); } catch (e) { return setTimeout(tick, 800); }
    if (st.type !== 'scan') return;
    if (st.state === 'running') {
      const known = st.phase === 'reading' && st.total;
      wrap.classList.toggle('busy', !known);
      bar.style.width = known ? (100 * st.done / st.total).toFixed(1) + '%' : '';
      label.textContent = {
        library: `Checking what is already in your library… ${nf(st.done)} files`,
        listing: `Listing the card… ${nf(st.done)} files`,
        reading: `Reading capture dates… ${nf(st.done)} of ${nf(st.total)}`,
        comparing: 'Comparing with your library…',
      }[st.phase] || 'Working…';
      return setTimeout(tick, 250);
    }
    if (st.state === 'error') {
      return showEmpty('Could not read the card', st.error || '', h('button', { class: 'btn primary', onclick: boot }, 'Try again'));
    }
    setScan(await api('/api/scan-result'));
  };
  tick();
}

function setScan(scan) {
  S.scan = scan;
  S.shots = scan.shots;
  for (const s of S.shots) {
    s.key = s.name + '|' + s.t;
    s.day = s.t.slice(0, 10);
    s.ms = new Date(s.t).getTime();
    s.bytes = s.files.reduce((n, f) => n + f.size, 0);
    const kinds = new Set(s.files.map(f => f.kind));
    s.badge = kinds.has('video') ? '▶ video' : kinds.has('raw') ? (kinds.has('jpg') ? 'RAW+JPG' : 'RAW') : '';
  }
  S.folderSet = new Set(scan.folders.map(f => f.folder.toLowerCase()));
  restore();
  S.showWhere = !S.shots.length;
  paintSummary();
  if (!S.shots.length) {
    showEmpty(scan.cardFiles ? 'Nothing new to import' : 'No photos found',
      scan.cardFiles ? `All ${plural(scan.cardFiles, 'file')} on the card are already accounted for in your library.`
        : `There are no photos or videos in ${scan.source}`);
    return;
  }
  window.scrollTo(0, 0);
  render();
}

function paintSummary() {
  const scan = S.scan, el = $('#summary');
  if (!scan) return el.replaceChildren();
  const days = new Set(S.shots.map(s => s.day)).size;
  const line = h('div', null, b(plural(scan.cardFiles, 'file')), ' on the card · ',
    b(nf(scan.importedFiles)), ' already in your library ',
    scan.importedFiles || S.config.settings.exclude.length
      ? h('button', { class: 'link', onclick: () => { S.showWhere = !S.showWhere; paintSummary(); } }, S.showWhere ? '(hide details)' : '(where?)') : null,
    ' · ', b(nf(scan.newFiles)), ' new',
    S.shots.length ? ` (${plural(S.shots.length, 'photo')} over ${plural(days, 'day')})` : '',
    scan.ignoredFiles ? [` · ${nf(scan.ignoredFiles)} marked "Never" `,
      h('button', { class: 'link', onclick: async () => { await api('/api/forget-ignored', {}); startScan(); } }, '(offer them again)')] : null);
  const kids = [line];
  if (!scan.libraryExists) kids.push(h('div', { class: 'note' }, `The library folder ${scan.library} does not exist yet. It will be created when you import.`));
  if (S.showWhere) {
    const setExclude = async list => { S.config = await api('/api/config', { exclude: list }); startScan(); };
    const excluded = S.config.settings.exclude;
    kids.push(h('div', { class: 'where' },
      scan.importedWhere.length ? h('div', { class: 'note', style: 'margin:0 0 6px' }, 'Files on the card that were found in these library folders:') : null,
      scan.importedWhere.map(w => h('div', { class: 'where-row' },
        h('span', { class: 'n' }, nf(w.count)), h('span', { class: 'f' }, w.folder || '(top of the library)'),
        w.folder ? h('button', { class: 'link', title: 'Treat photos in this folder as not yet imported, e.g. a dumping folder you want to sort out properly',
          onclick: () => setExclude([...excluded, w.folder]) }, 'Don\'t count this folder') : null)),
      excluded.map(x => h('div', { class: 'where-row' },
        h('span', { class: 'n' }, '–'), h('span', { class: 'f' }, `${x} (not counted as imported)`),
        h('button', { class: 'link', onclick: () => setExclude(excluded.filter(y => y !== x)) }, 'Count it again')))));
  }
  el.replaceChildren(...kids);
}

// ------------------------------------------------------------------ top bar

function paintTop() {
  const sel = $('#source'), sources = S.config.sources.slice();
  if (S.source && !sources.some(s => s.path === S.source)) sources.push({ path: S.source, label: S.source });
  sel.replaceChildren(
    ...(sources.length ? [] : [h('option', { value: '' }, 'No card found')]),
    ...sources.map(s => h('option', { value: s.path }, s.label)),
    h('option', { value: '__pick' }, 'Choose a folder…'));
  sel.value = S.source;
  const lib = $('#library');
  lib.textContent = '‎' + S.config.settings.library;
  lib.title = S.config.settings.library + '\nClick to change';
  if (document.activeElement !== $('#subfolder')) $('#subfolder').value = S.config.settings.subfolder;
}

$('#source').addEventListener('change', async e => {
  if (e.target.value === '__pick') {
    const res = await api('/api/pick-folder', { title: 'Choose the card or a folder of photos to import from', initial: '' });
    if (!res.path) { e.target.value = S.source; return; }
    S.source = res.path;
  } else S.source = e.target.value;
  paintTop(); startScan();
});
$('#library').addEventListener('click', async () => {
  const res = await api('/api/pick-folder', { title: 'Choose your photo library folder', initial: S.config.settings.library });
  if (!res.path) return;
  S.config = await api('/api/config', { library: res.path });
  paintTop(); startScan();
});
$('#subfolder').addEventListener('input', e => {
  S.config.settings.subfolder = cleanName(e.target.value);
  S.events.forEach(paintFolder);
});
$('#subfolder').addEventListener('change', async e => {
  S.config = await api('/api/config', { subfolder: e.target.value });
  paintTop(); S.events.forEach(paintFolder);
});
$('#rescan').addEventListener('click', () => boot());
$('#bulk-separate').addEventListener('click', () => {
  for (const i of unanswered()) S.asked[keyAt(i)] = 'separate';
  render();
});
$('#modal').addEventListener('click', e => { if (e.target === e.currentTarget && !S.importing && $('#modal .foot')) closeModal(); });

let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (S.shots.length && measureCols() !== S.cols) render(); }, 200);
});
window.addEventListener('beforeunload', e => { if (S.importing) { e.preventDefault(); e.returnValue = ''; } });

async function boot() {
  try { S.config = await api('/api/config'); }
  catch (err) { return showEmpty('Photo Importer is not running', 'Close this window and start it again.'); }
  const paths = S.config.sources.map(s => s.path);
  const last = S.config.settings.source;
  if (!S.source || (!paths.includes(S.source) && S.source !== last)) {
    S.source = paths.includes(last) ? last : (paths[0] || '');
  }
  paintTop();
  const job = S.config.job;
  if (job.state === 'running' && job.type === 'import') return watchImport();
  if (job.state === 'running' && job.type === 'scan') return watchScan();
  startScan();
}

new EventSource('/api/alive');     // lets the program know this window is still open
boot();
