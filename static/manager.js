/* SAW manager - shared page helpers. Nothing here touches the page when it
   loads: only functions, called by each page's own script. Names, groups and
   errors come from the units - typed by whoever had their web UI open - so
   they are always set as text, never as HTML. */
'use strict';

const SLOT_NAMES = {loop: 'Loop', trigger1: 'Trigger 1', trigger2: 'Trigger 2',
                    trigger3: 'Trigger 3', override: 'Override'};
const ACTIONS = [['override_on', 'Override on'], ['override_off', 'Override off'],
                 ['restart', 'Restart loop'], ['play', 'Play'], ['pause', 'Pause'],
                 ['trigger_1', 'T1'], ['trigger_2', 'T2'], ['trigger_3', 'T3']];

function toggleTheme() {
  const t = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', t);
  try { localStorage.setItem('sawTheme', t); } catch (e) {}
}

function toast(msg, ok) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.className = ok ? 't-ok' : 't-err';
  el.style.display = 'block';
  clearTimeout(el._t);
  el._t = setTimeout(function () { el.style.display = 'none'; }, 4000);
}

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}

function ago(s) {
  if (s === null || s === undefined) return 'never';
  if (s < 60) return s + ' s ago';
  if (s < 3600) return Math.round(s / 60) + ' min ago';
  if (s < 86400) return Math.round(s / 3600) + ' h ago';
  return Math.round(s / 86400) + ' d ago';
}

function mb(n) {
  if (n === null || n === undefined) return '';
  return n >= 1073741824 ? (n / 1073741824).toFixed(1) + ' GB' : Math.max(1, Math.round(n / 1048576)) + ' MB';
}

function unitName(u) { return u.name || u.hostname || u.id; }

/* Every POST carries X-SAW: a page on another site cannot add it without the
   browser asking the manager first, which it refuses - so a stray link cannot
   send commands with the password the browser remembers. */
async function postJSON(url, body) {
  const r = await fetch(url, {method: 'POST', body: JSON.stringify(body || {}),
                              headers: {'Content-Type': 'application/json', 'X-SAW': '1'}});
  let d = {};
  try { d = await r.json(); } catch (e) {}
  if (!r.ok || d.ok === false) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}

function uploadRaw(url, file, onProgress) {
  return new Promise(function (resolve, reject) {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.setRequestHeader('X-SAW', '1');
    xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
    xhr.upload.onprogress = function (e) { if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total); };
    xhr.onload = function () {
      let d = {};
      try { d = JSON.parse(xhr.responseText); } catch (e) {}
      if (xhr.status === 200 && d.ok) resolve(d); else reject(new Error(d.error || ('HTTP ' + xhr.status)));
    };
    xhr.onerror = function () { reject(new Error('connection lost')); };
    xhr.send(file);
  });
}

async function command(ids, action, label, extra) {
  if (!ids.length) { toast('No units', false); return; }
  if (ids.length > 1 && !confirm(label + ' on ' + ids.length + ' units?')) return;
  try {
    const d = await postJSON('/api/command', Object.assign({units: ids, action: action}, extra || {}));
    toast(label + ': sent to ' + d.sent + (d.offline ? ', ' + d.offline + ' offline - not sent' : ''), !d.offline);
  } catch (e) { toast(e.message, false); }
}

/* A row of remote-control buttons for some units. getIds is called on each
   click, so a group's buttons always act on its units as they are now. */
function controlBar(getIds, scopeLabel) {
  const box = el('div', null, 'controls');
  ACTIONS.forEach(function (a) {
    const tone = a[0].indexOf('override') === 0 ? 'btn-orange' : a[0].indexOf('trigger') === 0 ? 'btn-blue' : 'btn-grey';
    const b = el('button', a[1], 'btn btn-sm ' + tone);
    b.title = a[1] + (scopeLabel ? ' - ' + scopeLabel : '');
    b.onclick = function () { command(getIds(), a[0], a[1] + (scopeLabel ? ' (' + scopeLabel + ')' : '')); };
    box.appendChild(b);
  });
  return box;
}

/* Units grouped by their group, each group with an "all" tick; keeps what was
   ticked across refreshes. -> the box; picked(box) gives the ids. */
function renderPicker(box, units) {
  const was = {};
  box.querySelectorAll('input[data-unit]:checked').forEach(function (c) { was[c.value] = 1; });
  box.textContent = '';
  if (!units.length) { box.appendChild(el('span', 'No units have joined yet.', 'hint')); return; }
  const groups = {};
  units.forEach(function (u) { (groups[u.group || ''] = groups[u.group || ''] || []).push(u); });
  Object.keys(groups).sort(function (a, b) { return (a === '') - (b === '') || a.localeCompare(b); }).forEach(function (g) {
    const wrap = el('div', null, 'picker-group');
    const head = el('label', null, 'gname');
    const all = el('input'); all.type = 'checkbox';
    head.appendChild(all); head.appendChild(document.createTextNode(' ' + (g || 'No group')));
    wrap.appendChild(head);
    const grid = el('div', null, 'units-pick');
    groups[g].forEach(function (u) {
      const lab = el('label');
      const cb = el('input'); cb.type = 'checkbox'; cb.value = u.id; cb.dataset.unit = '1'; cb.checked = !!was[u.id];
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode(' ' + unitName(u) + (u.online ? '' : ' (offline)')));
      grid.appendChild(lab);
    });
    all.onchange = function () { grid.querySelectorAll('input').forEach(function (c) { c.checked = all.checked; }); };
    wrap.appendChild(grid);
    box.appendChild(wrap);
  });
}

function picked(box) {
  const ids = [];
  box.querySelectorAll('input[data-unit]:checked').forEach(function (c) { ids.push(c.value); });
  return ids;
}

function pickAll(box, on) {
  box.querySelectorAll('input[type=checkbox]').forEach(function (c) { c.checked = on; });
}
