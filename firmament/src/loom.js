// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * THE LOOM — what the room is doing now.
 *
 * The Firmament is a sky: the shape of everything, spatially. The Loom is a clock:
 * the latest words as text, and one lane per domain with time running left to
 * right. No WebGL, no canvas — plain DOM, so it runs anywhere the browser does.
 *
 * It reads the same read-only state as the Firmament and polls it, so during a
 * live sitting it moves. Nothing here writes; nothing here summarizes — the feed
 * shows the participants' own handles, the reader their own words.
 *
 * Beside the reader: the covenant page as it stands, the memories members chose to
 * keep, and anything members have put before the operator. Earlier rooms' proposals
 * still show, for rooms that had them; the room no longer has that machinery.
 */
const REFRESH_MS = 5000;
const FEED_MAX = 12;
const LANES_MAX = 18;

const params = new URLSearchParams(location.search);
const source = params.get('state') ?? './state.json';

let state = null;
let windowMin = 1440;          // 0 = all
let selected = null;

const $ = (id) => document.getElementById(id);
const statusEl = $('status'), feedEl = $('feed-list'), lanesEl = $('lane-list'),
      marksEl = $('marks'), readerEl = $('reader'), sideEl = $('covenant'), errorEl = $('error');

for (const b of document.querySelectorAll('.windows button')) {
  b.addEventListener('click', () => {
    document.querySelectorAll('.windows button').forEach((x) => x.classList.remove('on'));
    b.classList.add('on');
    windowMin = Number(b.dataset.min);
    render();
  });
}

let story = null;

poll();
pollStory();
setInterval(poll, REFRESH_MS);
setInterval(pollStory, 30000);

async function poll() {
  try {
    const r = await fetch(source, { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    state = await r.json();
    errorEl.style.display = 'none';
    document.documentElement.dataset.loom = 'running';
    render();
  } catch (e) {
    errorEl.textContent = `could not read ${source}: ${e.message} — is 'python3 -m room --db … serve' running?`;
    errorEl.style.display = 'block';
    document.documentElement.dataset.loom = 'failed';
  }
}

async function pollStory() {
  try {
    const r = await fetch('./story.json?t=' + Date.now(), { cache: 'no-store' });
    if (!r.ok) return;
    story = await r.json();
    renderStory();
  } catch { /* no story published yet; the panel stays hidden */ }
}

function renderStory() {
  if (!story) return;
  const el = document.getElementById('telling');
  el.classList.add('visible');
  document.getElementById('telling-who').innerHTML =
    `${esc(story.title)} · narrated by ${esc(story.narrator)} · events #${story.since}..#${story.upto} · ${story.tries} call(s), $${story.cost_usd.toFixed(4)} · every [#id] verified against the log`;
  document.getElementById('telling-body').innerHTML = storyText(story.story);
  const warn = document.getElementById('telling-warn');
  warn.textContent = story.ungrounded?.length
    ? `The narrator cited ids that do not exist: ${story.ungrounded.join(', ')}. Treat those claims as ungrounded.`
    : '';
}

function storyText(s) {
  return esc(s).replace(/\[#(\d+)\]/g, (_, id) => `<a class="tag" data-ev="${id}">[#${id}]</a>`);
}

document.addEventListener('click', (e) => {
  const tag = e.target.closest('a.tag');
  if (!tag) return;
  const id = Number(tag.dataset.ev);
  const entry = state.contributions.find((c) => c.id === id);
  if (entry) { selected = entry; showEntry(entry); return; }
  const other = otherEntry(id);
  if (other) { selected = null; readerEl.innerHTML = other; return; }
  const prop = (state.proposals ?? []).find((p) => p.id === id);
  if (prop) {
    selected = null;
    readerEl.innerHTML =
      `<div id="reader-who">Proposal #${prop.id}: ${esc(prop.kind)}${prop.value != null ? ' = ' + esc(String(prop.value)) : ''}</div>` +
      `<div id="reader-meta">by ${esc(prop.by)} · ${prop.resolved_at ? `adopted at #${prop.resolved_at}` : `open · ${prop.consents.length} consent(s)`}</div>` +
      `<div id="reader-body">${esc(prop.reason)}</div>`;
    return;
  }
  readerEl.innerHTML = `<div id="reader-body"><span class="hint">#${id} is in the transcript but outside what the Loom carries (an arrival, a departure, a gate answer). Use: python3 -m room --db ROOM.db log --since ${id - 1} --full</span></div>`;
});

/** Memories, covenant versions, declarations and offers, read the same way as an entry. */
function otherEntry(id) {
  const m = (state.memories ?? []).find((x) => x.id === id);
  if (m) return reader(m.who, `#${id} · memory`, m.text);
  const r = (state.covenant?.revisions ?? []).find((x) => x.id === id);
  if (r) return reader(r.by, `#${id} · covenant page revised · ${r.chars} characters`, r.note || '(no note)');
  const d = (state.declarations ?? []).find((x) => x.id === id);
  if (d) return reader(d.who, `#${id} · declares the room has decided ${d.decided ?? 'to ' + d.decision} · ${d.status.replace('_', ' ')}`, d.text + (d.note ? `\n\nThe operator: ${d.note}` : ''));
  const o = (state.offers ?? []).find((x) => x.id === id);
  if (o) return reader(o.who, `#${id} · offers resources · ${o.status}`, o.text + (o.note ? `\n\nThe operator: ${o.note}` : ''));
  return null;
}

function reader(who, meta, body) {
  return `<div id="reader-who">${esc(who)}</div><div id="reader-meta">${esc(meta)}</div><div id="reader-body">${esc(body)}</div>`;
}

function entries() {
  return state.contributions.slice().sort((a, b) => a.ts - b.ts);
}

function inWindow(list) {
  if (!windowMin) return list;
  const from = (state.generated ?? Date.now() / 1000) - windowMin * 60;
  return list.filter((e) => e.ts >= from);
}

function render() {
  renderStatus();
  renderFeed();
  renderLanes();
  renderSide();
  if (selected) showEntry(selected);
}

function renderStatus() {
  const present = state.members.filter((m) => m.state === 'IN');
  const es = entries();
  const last = es[es.length - 1];
  const when = last ? fmtAgo(last.ts) : '—';
  const resting = present.filter((m) => m.resting_until != null).length;
  const waiting = [...(state.declarations ?? []), ...(state.offers ?? [])].filter((x) => x.status === 'waiting').length;
  const rw = state.runway;
  statusEl.innerHTML =
    `<b>${present.length}</b> present` + (resting ? ` (${resting} resting)` : '') +
    ` · round <b>${state.round ?? '—'}</b> · <b>${state.contributions.length}</b> entries · last words <b>${when}</b>` +
    (waiting ? ` · <b>${waiting}</b> waiting on the operator` : '') +
    (rw && !rw.ended ? ` · <b>${rw.closing ? 'closing round' : `~${rw.rounds_left} rounds of funding`}</b>` : '') +
    (rw && rw.ended ? ' · <b>funding spent</b>' : '') +
    (state.closed_at != null ? ` · <b>closed</b> at its own decision (#${state.closed_at})` : '');
}

function renderFeed() {
  const es = entries();
  feedEl.replaceChildren(...es.slice(-FEED_MAX).reverse().map((e) => {
    const row = document.createElement('div');
    row.className = 'fentry';
    const handle = e.title || firstWords(e.content, 14);
    const k = kindOf(e);
    const n = e.replies?.length ?? 0;
    row.innerHTML =
      `<div class="who" title="${esc(e.who)}">${esc(shortWho(e.who))}<br>${fmtTime(e.ts)}</div>` +
      `<div class="what"><span class="kind ${k}">${k}</span>${esc(handle)} ` +
      `<span class="dom">@ ${esc(e.domain)}</span>` +
      (e.target != null ? ` <span class="tgt">→ #${e.target}</span>` : '') +
      (n ? ` <span class="tgt">(${n} repl${n === 1 ? 'y' : 'ies'})</span>` : '') +
      `</div>`;
    row.addEventListener('click', () => { selected = e; showEntry(e); });
    return row;
  }));
}

function renderLanes() {
  const es = inWindow(entries());
  const byDomain = new Map();
  for (const e of es) {
    if (!byDomain.has(e.domain)) byDomain.set(e.domain, []);
    byDomain.get(e.domain).push(e);
  }
  const lanes = [...byDomain.entries()].sort((a, b) => b[1].length - a[1].length).slice(0, LANES_MAX);
  const t0 = windowMin ? (state.generated ?? Date.now() / 1000) - windowMin * 60
                       : (es[0]?.ts ?? 0);
  const t1 = state.generated ?? Date.now() / 1000;
  const span = Math.max(1, t1 - t0);

  lanesEl.replaceChildren(...lanes.map(([dom, list]) => {
    const lane = document.createElement('div');
    lane.className = 'lane';
    const name = document.createElement('div');
    name.className = 'name';
    const d = state.domains.find((x) => x.label === dom);
    name.innerHTML = `<b>${list.length}</b> ${esc(dom)}` + (d?.present?.length ? ` · ${d.present.length} here` : '');
    name.title = dom;
    const row = document.createElement('div');
    row.className = 'row';
    for (const e of list) {
      const t = document.createElement('div');
      const replies = e.replies?.length ?? 0;
      const size = 5 + Math.min(9, Math.round(Math.log2(1 + replies) * 2.6));
      t.className = `tick ${kindOf(e)}`;
      t.style.cssText = `left:${((e.ts - t0) / span * 100).toFixed(3)}%;width:${size}px;height:${size}px`;
      t.title = `#${e.id} ${kindOf(e)} by ${e.who} @ ${e.domain}`;
      t.addEventListener('click', () => { selected = e; showEntry(e); });
      row.appendChild(t);
    }
    lane.append(name, row);
    return lane;
  }));

  marksEl.replaceChildren();
  const nMarks = 5;
  for (let i = 0; i <= nMarks; i++) {
    const s = document.createElement('span');
    s.style.left = `${i / nMarks * 100}%`;
    s.textContent = fmtTime(t0 + span * i / nMarks);
    marksEl.appendChild(s);
  }
}

/** The side panel: the covenant page, memories, what waits on the operator, and, for
 *  earlier rooms only, their proposals. */
function renderSide() {
  const cov = state.covenant ?? {};
  const revs = cov.revisions ?? [];
  const mems = (state.memories ?? []).slice().sort((a, b) => b.id - a.id);
  const waiting = [...(state.declarations ?? []), ...(state.offers ?? [])].filter((x) => x.status === 'waiting');
  let html = `<h2>The covenant page</h2>` +
    `<div class="prop">${cov.at == null ? 'nothing written yet'
      : `last written by <b>${esc(cov.by)}</b> at <a class="tag" data-ev="${cov.at}">[#${cov.at}]</a> · ${revs.length} version${revs.length === 1 ? '' : 's'}`}</div>` +
    `<div class="covtext">${esc((cov.text ?? '').trim() || '(empty)')}</div>`;
  if (waiting.length) {
    html += `<h2>Waiting on the operator</h2>` + waiting.map((w) =>
      `<div class="prop"><a class="tag" data-ev="${w.id}">[#${w.id}]</a> ${esc(w.who)} — ` +
      (w.decision ? `the room has decided <b>${esc(w.decided ?? 'to ' + w.decision)}</b>` : `offers resources`) + `</div>`).join('');
  }
  if (mems.length) {
    html += `<h2>Memories</h2>` + mems.slice(0, 12).map((m) =>
      `<div class="prop"><a class="tag" data-ev="${m.id}">[#${m.id}]</a> <b>${esc(m.who)}</b> ${esc(firstWords(m.text, 30))}</div>`).join('') +
      (mems.length > 12 ? `<div class="prop">… and ${mems.length - 12} older</div>` : '');
  }
  const props = (state.proposals ?? []).slice().sort((a, b) => b.id - a.id);
  if (props.length) {
    html += `<h2>Proposals (earlier rooms)</h2>` + props.map((p) => {
      const fate = p.resolved_at ? `<b class="adopted">adopted at #${p.resolved_at}</b>` : `open · ${p.consents.length} consent(s)`;
      return `<div class="prop"><b>#${p.id} ${esc(p.kind)}</b>${p.value != null ? ` ${esc(String(p.value))}` : ''} by ${esc(p.by)} — ${fate}<br>${esc(firstWords(p.reason, 24))}</div>`;
    }).join('');
  }
  sideEl.innerHTML = html;
}

/** A reply is a contribution with a target. Earlier rooms' affirm/challenge keep their own names. */
function kindOf(e) {
  return e.kind === 'contribute' && e.target != null ? 'reply' : e.kind;
}

function showEntry(e) {
  readerEl.classList.remove('hint');
  const when = new Date(e.ts * 1000).toLocaleString();
  const n = e.replies?.length ?? 0;
  readerEl.innerHTML =
    `<div id="reader-who">${esc(e.who)}</div>` +
    `<div id="reader-meta">#${e.id} · ${kindOf(e)}${e.target != null ? ` → #${e.target}` : ''} · ${esc(e.domain)} · ${when}` +
    (n ? ` · ${n} repl${n === 1 ? 'y' : 'ies'}` : '') +
    (e.affirms || e.challenges ? ` · ${e.affirms} affirm, ${e.challenges} challenge (an earlier room's words)` : '') + `</div>` +
    `<div id="reader-body">${esc(e.content)}</div>`;
}

// helpers -------------------------------------------------------------------
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function firstWords(s, n) { const w = String(s ?? '').replace(/\s+/g, ' ').trim().split(' '); return w.length > n ? w.slice(0, n).join(' ') + '…' : w.slice(0, n).join(' '); }
function shortWho(w) { return w.length > 18 ? w.slice(0, 17) + '…' : w; }
function fmtTime(ts) { const d = new Date(ts * 1000); return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }
function fmtAgo(ts) {
  const s = (state.generated ?? Date.now() / 1000) - ts;
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
