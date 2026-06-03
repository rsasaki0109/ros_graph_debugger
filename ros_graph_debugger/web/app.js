/* ros_graph_debugger live UI.
 * Bipartite graph: ROS nodes (boxes) and topics (rounded) linked pub->topic->sub.
 * Streams snapshots over WebSocket and re-lays out only when topology changes. */
'use strict';

const HIDDEN_TOPICS = new Set(['/rosout', '/parameter_events']);
const state = {
  paused: false,
  topoSig: '',
  last: null,
  selected: null,
};

// --- profile (pipeline stage) grouping ---
const STAGE_PALETTE = ['#1f6feb', '#2ea043', '#bb8009', '#a371f7', '#db61a2', '#1f9c9c', '#d2691e'];
const profileState = { groups: null, order: [], colors: {}, compiled: {} };
const STATUS_RANK = { critical: 3, warning: 2, ok: 1, unknown: 0 };

async function loadProfile() {
  try {
    const r = await fetch('/api/v1/profile');
    const p = await r.json();
    if (!p || !p.groups || !Object.keys(p.groups).length) return;
    profileState.groups = p.groups;
    profileState.order = Object.keys(p.groups);
    profileState.order.forEach((k, i) => {
      profileState.colors[k] = STAGE_PALETTE[i % STAGE_PALETTE.length];
      profileState.compiled[k] = (p.groups[k].topic_patterns || []).map(pat => {
        try { return new RegExp(pat); } catch (e) { return null; }
      }).filter(Boolean);
    });
    renderStageLegend();
  } catch (e) { /* no profile: feature stays hidden */ }
}

function stageOfTopic(name) {
  if (!profileState.groups) return null;
  for (const k of profileState.order) {
    for (const re of profileState.compiled[k]) if (re.test(name)) return k;
  }
  return null;
}

function stageOfNode(n) {
  // A node belongs to the stage of its outputs first, then its inputs.
  for (const t of n.publishers) { const s = stageOfTopic(t); if (s) return s; }
  for (const t of n.subscribers) { const s = stageOfTopic(t); if (s) return s; }
  return null;
}

function renderStageLegend() {
  const el = document.getElementById('stage-legend');
  if (!profileState.groups) { el.classList.add('hidden'); return; }
  el.classList.remove('hidden');
  el.innerHTML = profileState.order.map(k =>
    `<span><i style="background:${profileState.colors[k]}"></i>${k}</span>`).join('');
}

function renderReadiness(snap) {
  const bar = document.getElementById('readiness');
  if (!profileState.groups) { bar.style.display = 'none'; return; }
  bar.style.display = 'flex';
  const worst = {};
  profileState.order.forEach(k => { worst[k] = 'unknown'; });
  const bump = (k, st) => { if (k && STATUS_RANK[st] > STATUS_RANK[worst[k]]) worst[k] = st; };

  snap.topics.forEach(t => bump(stageOfTopic(t.name), t.status || 'unknown'));
  snap.issues.forEach(i => {
    const st = i.severity === 'info' ? 'ok' : i.severity;  // info shouldn't alarm a stage
    (i.related_topics || []).forEach(tn => bump(stageOfTopic(tn), st));
  });

  const label = { ok: 'OK', warning: 'WARN', critical: 'ERROR', unknown: '—' };
  bar.innerHTML = profileState.order.map(k => `
    <div class="stage ${worst[k]}" data-stage="${k}">
      <span class="name">${k}</span>
      <span class="verdict">${label[worst[k]]}</span>
    </div>`).join('');
  bar.querySelectorAll('.stage').forEach(el => {
    el.addEventListener('click', () => fitStage(el.dataset.stage));
  });
}

function fitStage(stage) {
  const ids = cy.nodes().filter(n => n.data('stage') === stage);
  if (ids.length) cy.animate({ fit: { eles: ids, padding: 60 } }, { duration: 300 });
}

function applyStageTints() {
  if (!profileState.groups) return;
  cy.nodes().forEach(n => {
    const s = n.data('stage');
    if (s && profileState.colors[s]) {
      n.style('background-color', profileState.colors[s]);
      n.style('background-opacity', n.hasClass('topic') ? 0.18 : 0.3);
    }
  });
}

cytoscape.use(window.cytoscapeDagre);

const cy = cytoscape({
  container: document.getElementById('cy'),
  wheelSensitivity: 0.2,
  style: [
    { selector: 'node', style: {
        'label': 'data(label)', 'color': '#c9d1d9', 'font-size': 10,
        'text-wrap': 'wrap', 'text-valign': 'center', 'text-halign': 'center',
    }},
    { selector: 'node.rosnode', style: {
        'shape': 'round-rectangle', 'background-color': '#1c2330',
        'border-width': 2, 'border-color': '#3b4252',
        'width': 'label', 'height': 'label', 'padding': '10px',
        'text-max-width': 160,
    }},
    { selector: 'node.topic', style: {
        'shape': 'round-rectangle', 'background-color': '#11161f',
        'border-width': 1, 'border-color': '#2d333b', 'font-size': 9,
        'width': 'label', 'height': 'label', 'padding': '5px',
        'text-max-width': 200, 'color': '#8b949e',
    }},
    { selector: 'node.ok', style: { 'border-color': '#3fb950' }},
    { selector: 'node.warning', style: { 'border-color': '#d29922' }},
    { selector: 'node.critical', style: { 'border-color': '#f85149', 'border-width': 3 }},
    { selector: 'node.selected', style: { 'border-color': '#58a6ff', 'border-width': 3 }},
    { selector: 'edge', style: {
        'width': 1.5, 'line-color': '#3b4252', 'target-arrow-color': '#3b4252',
        'target-arrow-shape': 'triangle', 'curve-style': 'bezier', 'arrow-scale': 0.8,
    }},
    { selector: 'edge.critical', style: { 'line-color': '#f85149', 'target-arrow-color': '#f85149', 'width': 2.5 }},
    { selector: 'edge.warning', style: { 'line-color': '#d29922', 'target-arrow-color': '#d29922', 'width': 2 }},
    { selector: 'edge.ok', style: { 'line-color': '#3fb950', 'target-arrow-color': '#3fb950' }},
  ],
});

const layout = () => cy.layout({
  name: 'dagre', rankDir: 'LR', nodeSep: 18, rankSep: 60, edgeSep: 8, animate: false,
}).run();

function shortType(t) { return t ? t.split('/').slice(-1)[0] : ''; }
function fmtRate(v) { return (typeof v === 'number') ? v.toFixed(1) + ' Hz' : ''; }
function fmtBw(v) {
  if (typeof v !== 'number') return '';
  if (v >= 1e6) return (v / 1e6).toFixed(1) + ' MB/s';
  if (v >= 1e3) return (v / 1e3).toFixed(1) + ' KB/s';
  return v.toFixed(0) + ' B/s';
}
function fmtBytes(v) {
  if (typeof v !== 'number') return '—';
  if (v >= 1e6) return (v / 1e6).toFixed(1) + ' MB';
  if (v >= 1e3) return (v / 1e3).toFixed(1) + ' KB';
  return v + ' B';
}

function buildElements(snap) {
  const els = [];
  const nodeStatus = {};
  snap.nodes.forEach(n => { nodeStatus[n.id] = n.status || 'ok'; });

  snap.nodes.forEach(n => {
    let label = n.name;
    if (typeof n.cpu_percent === 'number') {
      label += `\nCPU ${n.cpu_percent.toFixed(0)}%`;
      if (typeof n.rss_bytes === 'number') label += `  ${fmtBytes(n.rss_bytes)}`;
    }
    els.push({ data: { id: 'N:' + n.id, label, kind: 'node', ref: n.id, stage: stageOfNode(n) },
               classes: 'rosnode ' + (n.status || 'ok') });
  });

  snap.topics.forEach(t => {
    if (HIDDEN_TOPICS.has(t.name)) return;
    const tid = 'T:' + t.name;
    let label = t.name;
    const metr = [fmtRate(t.rate_hz), fmtBw(t.bandwidth_bps)].filter(Boolean).join(' · ');
    if (metr) label += '\n' + metr;
    if (t.qos_status === 'mismatch') label += '\n⚠ QoS mismatch';
    const cls = 'topic ' + (t.status || 'unknown');
    els.push({ data: { id: tid, label, kind: 'topic', ref: t.name, stage: stageOfTopic(t.name) }, classes: cls });

    const estatus = t.status || 'unknown';
    t.publishers.forEach(p => {
      if (nodeStatus[p] === undefined) return;
      els.push({ data: { id: `E:${p}->${t.name}`, source: 'N:' + p, target: tid }, classes: estatus });
    });
    t.subscribers.forEach(s => {
      if (nodeStatus[s] === undefined) return;
      els.push({ data: { id: `E:${t.name}->${s}`, source: tid, target: 'N:' + s }, classes: estatus });
    });
  });
  return els;
}

function render(snap) {
  state.last = snap;
  const els = buildElements(snap);
  const sig = els.filter(e => !e.data.source).map(e => e.data.id).sort().join('|') + '#' +
              els.filter(e => e.data.source).map(e => e.data.id).sort().join('|');

  if (sig !== state.topoSig) {
    cy.elements().remove();
    cy.add(els);
    layout();
    applyStageTints();
    state.topoSig = sig;
  } else {
    // Same topology: update labels and status classes in place.
    els.forEach(e => {
      const ele = cy.getElementById(e.data.id);
      if (ele.length === 0) return;
      if (e.data.label !== undefined) ele.data('label', e.data.label);
      ele.classes(e.classes || '');
    });
    if (state.selected) cy.getElementById(state.selected).addClass('selected');
  }
  updateIssues(snap.issues);
  updateChrome(snap);
  renderReadiness(snap);
  if (state.selected) refreshInspector();
}

function updateChrome(snap) {
  document.getElementById('counts').textContent =
    `${snap.nodes.length} nodes · ${snap.topics.length} topics`;
  const chip = document.getElementById('profile-chip');
  if (snap.profile) { chip.textContent = 'profile: ' + snap.profile; chip.classList.remove('hidden'); }
  else chip.classList.add('hidden');
}

function updateIssues(issues) {
  const badge = document.getElementById('issue-badge');
  badge.textContent = issues.length;
  badge.classList.toggle('zero', issues.length === 0);
  const box = document.getElementById('tab-issues');
  if (!issues.length) { box.innerHTML = '<p class="hint">No issues detected. 🎉</p>'; return; }
  box.innerHTML = issues.map(i => `
    <div class="issue ${i.severity}" data-nodes='${JSON.stringify(i.related_nodes || [])}'
         data-topics='${JSON.stringify(i.related_topics || [])}'>
      <h4><span class="sev">${i.severity}</span>${escapeHtml(i.title)}</h4>
      ${i.explanation ? `<p>${escapeHtml(i.explanation)}</p>` : ''}
      ${i.evidence && i.evidence.length ? `<ul>${i.evidence.map(e => `<li>${escapeHtml(e)}</li>`).join('')}</ul>` : ''}
      ${i.suggested_actions && i.suggested_actions.length ?
        `<p class="actions">→ ${i.suggested_actions.map(escapeHtml).join(' · ')}</p>` : ''}
    </div>`).join('');
  box.querySelectorAll('.issue').forEach(el => {
    el.addEventListener('click', () => {
      const topics = JSON.parse(el.dataset.topics || '[]');
      const nodes = JSON.parse(el.dataset.nodes || '[]');
      const target = topics[0] ? 'T:' + topics[0] : (nodes[0] ? 'N:' + nodes[0] : null);
      if (target) { selectElement(target); cy.animate({ center: { eles: cy.getElementById(target) } }, { duration: 300 }); }
    });
  });
}

function selectElement(id) {
  cy.elements().removeClass('selected');
  const ele = cy.getElementById(id);
  if (ele.length === 0) return;
  ele.addClass('selected');
  state.selected = id;
  switchTab('detail');
  refreshInspector();
}

function refreshInspector() {
  if (!state.last || !state.selected) return;
  const box = document.getElementById('tab-detail');
  if (state.selected.startsWith('N:')) {
    const id = state.selected.slice(2);
    const n = state.last.nodes.find(x => x.id === id);
    if (!n) return;
    box.innerHTML = nodeDetail(n);
  } else if (state.selected.startsWith('T:')) {
    const name = state.selected.slice(2);
    const t = state.last.topics.find(x => x.name === name);
    if (!t) return;
    box.innerHTML = topicDetail(t);
  }
}

function nodeDetail(n) {
  const cpu = typeof n.cpu_percent === 'number'
    ? `${n.cpu_percent.toFixed(0)}% (${n.process_mapping_confidence})` : 'unknown';
  return `<h3>${escapeHtml(n.name)}</h3>
    <dl class="kv">
      <dt>full name</dt><dd>${escapeHtml(n.id)}</dd>
      <dt>pid</dt><dd>${n.pid ?? '—'}</dd>
      <dt>cpu</dt><dd>${cpu}</dd>
      <dt>memory</dt><dd>${fmtBytes(n.rss_bytes)}</dd>
    </dl>
    <div class="section-title">Publishers (${n.publishers.length})</div>
    ${n.publishers.map(p => `<span class="pill" onclick="window._sel('T:${p}')">${p}</span>`).join('') || '<span class="hint">none</span>'}
    <div class="section-title">Subscribers (${n.subscribers.length})</div>
    ${n.subscribers.map(s => `<span class="pill" onclick="window._sel('T:${s}')">${s}</span>`).join('') || '<span class="hint">none</span>'}`;
}

function topicDetail(t) {
  const qos = t.qos_endpoints.map(e =>
    `<span class="pill ${t.qos_status === 'mismatch' ? 'mismatch' : ''}">${e.endpoint_type}: ${e.reliability}/${e.durability} d${e.depth}</span>`).join('');
  return `<h3>${escapeHtml(t.name)}</h3>
    <dl class="kv">
      <dt>type</dt><dd>${escapeHtml(t.type || '—')}</dd>
      <dt>status</dt><dd><span class="pill ${t.status}">${t.status}</span></dd>
      <dt>rate</dt><dd>${fmtRate(t.rate_hz) || (t.probed ? 'measuring…' : 'not probed')}</dd>
      <dt>bandwidth</dt><dd>${fmtBw(t.bandwidth_bps) || '—'}</dd>
      <dt>avg size</dt><dd>${fmtBytes(t.avg_msg_size_bytes)}</dd>
      <dt>p95 size</dt><dd>${fmtBytes(t.p95_msg_size_bytes)}</dd>
      <dt>qos</dt><dd>${t.qos_status}</dd>
    </dl>
    <div class="section-title">Publishers (${t.publisher_count})</div>
    ${t.publishers.map(p => `<span class="pill" onclick="window._sel('N:${p}')">${p}</span>`).join('') || '<span class="hint">none</span>'}
    <div class="section-title">Subscribers (${t.subscriber_count})</div>
    ${t.subscribers.map(s => `<span class="pill" onclick="window._sel('N:${s}')">${s}</span>`).join('') || '<span class="hint">none</span>'}
    <div class="section-title">QoS endpoints</div>${qos || '<span class="hint">unknown</span>'}`;
}

window._sel = selectElement;

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

cy.on('tap', 'node', evt => selectElement(evt.target.id()));

// --- tabs ---
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.getElementById('tab-issues').classList.toggle('active', name === 'issues');
  document.getElementById('tab-detail').classList.toggle('active', name === 'detail');
}
document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));
document.getElementById('fit-btn').addEventListener('click', () => cy.fit(undefined, 40));
document.getElementById('pause-btn').addEventListener('click', e => {
  state.paused = !state.paused;
  e.target.textContent = state.paused ? '▶ Resume' : '⏸ Pause';
});

// --- websocket stream ---
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/api/v1/stream`);
  const conn = document.getElementById('conn');
  ws.onopen = () => { conn.textContent = 'live'; conn.className = 'chip conn-on'; };
  ws.onclose = () => { conn.textContent = 'reconnecting…'; conn.className = 'chip conn-off'; setTimeout(connect, 1500); };
  ws.onmessage = ev => { if (!state.paused) { try { render(JSON.parse(ev.data)); } catch (e) { console.error(e); } } };
}

loadProfile().finally(connect);
