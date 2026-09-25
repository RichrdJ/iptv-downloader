/* IPTV Downloader — gedeelde frontend-logica */
const IPTV = (() => {
  const DL_MODE = document.body.dataset.dlMode || 'browser';

  // ---------- helpers ----------
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  async function api(url, body, method) {
    const opts = {method: method || (body !== undefined ? 'POST' : 'GET'), headers: {}};
    if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    const r = await fetch(url, opts);
    let data = {};
    try { data = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error(data.error || (data.errors && data.errors[0]) || `HTTP ${r.status}`);
    return data;
  }

  function toast(msg, type = 'info', ms = 3500) {
    let box = $('#toasts');
    if (!box) { box = document.createElement('div'); box.id = 'toasts'; document.body.appendChild(box); }
    const t = document.createElement('div');
    t.className = `toast ${type}`;
    t.innerHTML = msg;
    box.appendChild(t);
    setTimeout(() => { t.style.transition = 'opacity .3s'; t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, ms);
  }

  function fmtBytes(b) {
    if (!b) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
    while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
    return `${b.toFixed(i > 1 ? 1 : 0)} ${u[i]}`;
  }

  function fmtEta(sec) {
    if (!isFinite(sec) || sec <= 0) return '';
    const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = Math.floor(sec % 60);
    return h ? `${h}u ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
  }

  // ---------- rename dialog ----------
  function rename(defaultName, title = 'Bestandsnaam') {
    return new Promise(resolve => {
      const dlg = $('#rename-dialog'), input = $('#rename-input');
      $('#rename-title').textContent = title;
      input.value = defaultName;
      const done = val => { cleanup(); dlg.close('ok'); resolve(val); };
      const onSubmit = e => { e.preventDefault(); done(input.value.trim() || defaultName); };
      const onCancel = () => { cleanup(); resolve(null); };
      function cleanup() { $('#rename-form').removeEventListener('submit', onSubmit); dlg.removeEventListener('close', onClose); }
      function onClose() { if (dlg.returnValue !== 'ok') onCancel(); }
      $('#rename-form').addEventListener('submit', onSubmit);
      dlg.addEventListener('close', onClose);
      dlg.returnValue = '';
      dlg.showModal();
      const dot = input.value.lastIndexOf('.');
      input.focus();
      input.setSelectionRange(0, dot > 0 ? dot : input.value.length);
    });
  }

  // ---------- downloads ----------
  /** items: [{type:'episode'|'movie', id, ext, filename, show, season, title, year, label}] */
  async function download(items) {
    if (!items.length) return false;
    if (DL_MODE === 'server') {
      try {
        const r = await api('/api/downloads', {items});
        const extra = r.skipped ? ` (${r.skipped} stond al in de wachtrij)` : '';
        toast(`${r.added} ${r.added === 1 ? 'download' : 'downloads'} toegevoegd${extra} <a href="/downloads">Bekijk →</a>`, 'ok');
        pollSummary();
        return true;
      } catch (e) { toast(`Mislukt: ${esc(e.message)}`, 'error'); return false; }
    }
    // Browsermodus: downloads één voor één starten
    if (items.length > 3 && !confirm(`${items.length} downloads tegelijk in de browser starten?\n\nDe meeste providers staan maar 1–2 verbindingen toe. Gebruik bij grote aantallen liever de servermodus (Instellingen).`)) return false;
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      const base = it.type === 'movie' ? '/stream/movie/' : '/stream/';
      const a = document.createElement('a');
      a.href = `${base}${encodeURIComponent(it.id)}?ext=${encodeURIComponent(it.ext)}&filename=${encodeURIComponent(it.filename)}`;
      a.download = it.filename;
      document.body.appendChild(a); a.click(); a.remove();
      if (i < items.length - 1) await new Promise(r => setTimeout(r, 1500));
    }
    api('/history/add', {ep_ids: items.map(i => i.type === 'movie' ? `m${i.id}` : String(i.id))}).catch(() => {});
    toast(`${items.length} ${items.length === 1 ? 'download' : 'downloads'} gestart in de browser`, 'ok');
    return true;
  }

  function itemFromEl(el) {
    const d = el.dataset;
    return {type: d.type, id: d.id, ext: d.ext, filename: d.filename, show: d.show || '',
            season: +d.season || 0, title: d.title || '', year: d.year || '', label: d.label || d.filename};
  }

  // ---------- nav badge ----------
  let pollTimer;
  async function pollSummary() {
    clearTimeout(pollTimer);
    const badge = $('#dl-badge');
    if (!badge) return;
    try {
      const s = await api('/api/downloads/summary');
      const n = s.active + s.queued;
      badge.textContent = n;
      badge.hidden = !n;
      badge.classList.toggle('pulse', s.active > 0);
      pollTimer = setTimeout(pollSummary, n ? 4000 : 20000);
    } catch (_) { pollTimer = setTimeout(pollSummary, 30000); }
  }

  // ---------- grid filter & sort ----------
  function initGrid(grid) {
    const input = $('#filter-input'), sort = $('#sort-select'), count = $('#count-badge'), none = $('#no-results');
    if (!grid || !input) return;
    const cards = $$('.card-wrap', grid);
    const key = `sort:${location.pathname.split('/')[1] || 'x'}`;
    try { if (localStorage.getItem(key)) sort.value = localStorage.getItem(key); } catch (_) {}
    const cmp = {
      'name-asc': (a, b) => a.dataset.name.localeCompare(b.dataset.name),
      'name-desc': (a, b) => b.dataset.name.localeCompare(a.dataset.name),
      'rating-desc': (a, b) => b.dataset.rating - a.dataset.rating,
      'added-desc': (a, b) => b.dataset.added - a.dataset.added,
    };
    let t;
    function apply() {
      const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
      const vis = cards.filter(c => words.every(w => c.dataset.name.includes(w)));
      vis.sort(cmp[sort.value] || cmp['name-asc']);
      const frag = document.createDocumentFragment();
      cards.forEach(c => c.hidden = true);
      vis.forEach(c => { c.hidden = false; frag.appendChild(c); });
      grid.appendChild(frag);
      if (count) count.textContent = vis.length;
      if (none) none.hidden = vis.length > 0;
    }
    input.addEventListener('input', () => { clearTimeout(t); t = setTimeout(apply, 120); });
    sort.addEventListener('change', () => { try { localStorage.setItem(key, sort.value); } catch (_) {} apply(); });
    apply();
  }

  // ---------- global event delegation ----------
  document.addEventListener('click', async e => {
    const fav = e.target.closest('.fav-btn, .fav-toggle');
    if (fav) {
      e.preventDefault();
      const d = fav.dataset;
      try {
        const r = await api('/favorites/toggle', {type: d.type, id: +d.id, name: d.name, cover: d.cover});
        fav.classList.toggle('active', r.is_fav);
        if (fav.classList.contains('fav-toggle')) fav.textContent = r.is_fav ? '♥ Favoriet' : '♡ Favoriet';
        if (!r.is_fav && fav.dataset.removeOnUnfav) {
          const w = fav.closest('.card-wrap');
          w.style.transition = 'opacity .25s'; w.style.opacity = 0; setTimeout(() => w.remove(), 250);
        }
      } catch (err) { toast(esc(err.message), 'error'); }
      return;
    }
    const dl = e.target.closest('[data-download]');
    if (dl) {
      e.preventDefault();
      const it = itemFromEl(dl);
      const name = await rename(it.filename, 'Downloaden als');
      if (name === null) return;
      it.filename = name;
      if (await download([it])) markDone([dl]);
    }
  });

  function markDone(els) {
    els.forEach(el => {
      el.closest('.ep')?.classList.add('done');
      const wrap = el.closest('.card-wrap');
      if (wrap && !$('.done-flag', wrap)) wrap.insertAdjacentHTML('afterbegin', '<span class="done-flag">✓</span>');
    });
  }

  // ---------- init ----------
  document.addEventListener('DOMContentLoaded', () => {
    $('.menu-btn')?.addEventListener('click', () => $('nav').classList.toggle('open'));
    $$('.poster-grid[data-filterable]').forEach(initGrid);
    pollSummary();
    // Toetsenbord: "/" focust zoeken
    document.addEventListener('keydown', e => {
      if (e.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
        e.preventDefault(); $('#nav-q')?.focus();
      }
    });
  });

  return {$, $$, api, toast, esc, rename, download, itemFromEl, markDone, fmtBytes, fmtEta, pollSummary, DL_MODE};
})();
