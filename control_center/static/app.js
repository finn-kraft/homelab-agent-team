const state = {
  csrfToken: '', overview: null, projects: [], events: [], view: 'dashboard',
  jobId: null, streamGeneration: 0, eventFilter: '', eventSearch: {}, humanDraft: '',
  telemetry: null, telemetryTimer: null, telemetryInFlight: false,
  telemetryHistory: [], telemetryHistoryTimer: null, telemetryHistoryInFlight: false,
  lastEventId: 0, humanHistory: [], humanHistoryInFlight: false,
};

const $ = selector => document.querySelector(selector);
const esc = value => {
  const node = document.createElement('div');
  node.textContent = value ?? '';
  return node.innerHTML;
};

const icons = {
  activity: '<path d="M3 12h4l2.2-6 4.1 12 2.1-6H21"/>',
  alert: '<path d="M12 3 2.8 20h18.4L12 3Zm-1 6h2v5h-2V9Zm0 7h2v2h-2v-2Z"/>',
  brain: '<path d="M9 4a3 3 0 0 0-3 3v1a3 3 0 0 0-1 5.2A3.5 3.5 0 0 0 8.5 18H10V5.2A3 3 0 0 0 9 4Zm6 0a3 3 0 0 1 3 3v1a3 3 0 0 1 1 5.2A3.5 3.5 0 0 1 15.5 18H14V5.2A3 3 0 0 1 15 4Z"/>',
  check: '<path d="m5 12 4 4L19 6l2 2L9 20 3 14l2-2Z"/>',
  chip: '<path d="M8 2h2v3h4V2h2v3h1a2 2 0 0 1 2 2v1h3v2h-3v4h3v2h-3v1a2 2 0 0 1-2 2h-1v3h-2v-3h-4v3H8v-3H7a2 2 0 0 1-2-2v-1H2v-2h3v-4H2V8h3V7a2 2 0 0 1 2-2h1V2Zm1 7v6h6V9H9Z"/>',
  cloud: '<path d="M7 18h11a4 4 0 0 0 .5-8A6 6 0 0 0 7.2 8.1 5 5 0 0 0 7 18Z"/>',
  code: '<path d="m9 7-5 5 5 5 1.4-1.4L6.8 12l3.6-3.6L9 7Zm6 0-1.4 1.4 3.6 3.6-3.6 3.6L15 17l5-5-5-5Z"/>',
  database: '<path d="M12 3C7 3 4 4.5 4 6.5v11C4 19.5 7 21 12 21s8-1.5 8-3.5v-11C20 4.5 17 3 12 3Zm0 2c3.8 0 6 1 6 1.5S15.8 8 12 8 6 7 6 6.5 8.2 5 12 5Zm0 14c-3.8 0-6-1-6-1.5V15c1.4.7 3.5 1 6 1s4.6-.3 6-1v2.5c0 .5-2.2 1.5-6 1.5Z"/>',
  folder: '<path d="M3 6a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6Z"/>',
  git: '<path d="m21 11-8-8a1.4 1.4 0 0 0-2 0L8.7 5.3l2 2a2.5 2.5 0 0 1 3 3l2 2a2.5 2.5 0 1 1-1.4 1.4l-2-2v5.1a2.5 2.5 0 1 1-2 0V10a2.5 2.5 0 0 1-1-3.3L7.3 4.7 3 9a1.4 1.4 0 0 0 0 2l8 8a1.4 1.4 0 0 0 2 0l8-8Z"/>',
  pulse: '<path d="M2 13h4l2-6 4 12 3-9 2 3h5v2h-6l-1-1-3 9L8 12l-.6 3H2v-2Z"/>',
  review: '<path d="M5 3h14v18H5V3Zm3 4v2h8V7H8Zm0 4v2h8v-2H8Zm0 4v2h5v-2H8Z"/>',
  route: '<path d="M5 4a3 3 0 1 0 0 6 3 3 0 0 0 0-6Zm14 10a3 3 0 1 0 0 6 3 3 0 0 0 0-6ZM8 6h5a4 4 0 0 1 4 4v2h-2v-2a2 2 0 0 0-2-2H8V6Zm1 10h5v2H9v3l-5-4 5-4v3Z"/>',
  server: '<path d="M4 3h16v7H4V3Zm3 3v2h2V6H7Zm-3 5h16v10H4V11Zm3 3v2h2v-2H7Zm5 0v2h5v-2h-5Z"/>',
  thermometer: '<path d="M10 5a4 4 0 0 1 8 0v8.2a6 6 0 1 1-8 0V5Zm4-2a2 2 0 0 0-2 2v9.3l-.5.3a4 4 0 1 0 5 0l-.5-.3V5a2 2 0 0 0-2-2Z"/>',
};
function icon(name) {
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.activity}</svg>`;
}

async function api(path, options = {}) {
  const headers = {...(options.headers || {})};
  const method = String(options.method || 'GET').toUpperCase();
  if (method !== 'GET') headers['X-CSRF-Token'] = state.csrfToken;
  if (options.body) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {...options, headers, credentials: 'same-origin'});
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const payload = await response.json();
      message = payload.detail || payload.error || message;
      if (payload.operation_id) message += ` (operation ${payload.operation_id})`;
    } catch (_) { /* no JSON body */ }
    throw new Error(message.replaceAll('_', ' '));
  }
  return response.json();
}

function age(value) {
  if (!value) return '—';
  const seconds = Math.max(0, (Date.now() - new Date(value)) / 1000);
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function bytes(value) {
  const amount = Number(value || 0);
  if (!amount) return '';
  if (amount > 1024 ** 3) return `${(amount / 1024 ** 3).toFixed(1)} GiB`;
  return `${Math.round(amount / 1024 ** 2)} MiB`;
}

function statusClass(value) {
  return String(value || 'unknown').toLowerCase().replace(/[^a-z0-9_-]/g, '');
}

function phaseLabel(value) {
  const labels = {coder: 'Engineering', coding: 'Engineering', engineering: 'Engineering',
    coder_revision: 'Engineering revision', engineering_revision: 'Engineering revision',
    review: 'Review', verification: 'Verification', checkpoint: 'Commit'};
  return labels[String(value || '').toLowerCase()] || value || 'Waiting';
}

function badge(value) {
  const raw = String(value || 'unknown').toLowerCase();
  const labels = {coder: 'Engineering', coding: 'Engineering', engineering: 'Engineering',
    coder_revision: 'Engineering revision', engineering_revision: 'Engineering revision'};
  const label = labels[raw] || raw.replaceAll('_', ' ');
  return `<span class="status ${statusClass(value)}">${esc(label)}</span>`;
}

function toast(title, detail = '', error = false) {
  const node = document.createElement('div');
  node.className = `toast${error ? ' error-toast' : ''}`;
  node.innerHTML = `<strong>${esc(title)}</strong>${detail ? `<small>${esc(detail)}</small>` : ''}`;
  $('#toasts').append(node);
  setTimeout(() => node.remove(), 4200);
}

function setConnection(kind, label) {
  $('#liveDot').classList.toggle('online', kind === 'online');
  $('#liveText').textContent = label;
}

function setBusy(button, busy, label = 'Working…') {
  if (!button) return;
  if (busy) {
    button.dataset.original = button.innerHTML;
    button.innerHTML = label;
    button.disabled = true;
  } else {
    button.innerHTML = button.dataset.original || button.innerHTML;
    button.disabled = false;
  }
}

function projectFor(repository) {
  return state.projects.find(project => project.repository === repository);
}

function showView(name) {
  if (!state.csrfToken) return;
  state.view = name;
  document.querySelectorAll('.view').forEach(node => { node.hidden = true; });
  $(`#${name}View`).hidden = false;
  document.querySelectorAll('nav button').forEach(button => {
    const active = button.dataset.view === name;
    button.classList.toggle('active', active);
    button.setAttribute('aria-current', active ? 'page' : 'false');
  });
  const titles = {
    dashboard: ['Overview', 'Your autonomous development team at a glance.'],
    projects: ['Projects', 'Repositories the team is authorized to change.'],
    agents: ['Agents', 'Live assignments backed by durable workflow evidence.'],
    jobs: ['Jobs', 'Track and manage every workflow in one place.'],
    missions: ['Missions', 'Roadmap packages, human gates, and integration status.'],
    events: ['Events', 'Technical workflow history, routes, and recovery evidence.'],
    job: ['Job detail', 'Progress, decisions, verification, and human gates.'],
  };
  $('#pageTitle').textContent = titles[name][0];
  $('#breadcrumb').textContent = titles[name][0];
  $('#pageSubtitle').textContent = titles[name][1];
  render();
  if (name === 'projects') refreshProjects();
  if (name === 'events') loadEvents();
  if (name === 'missions') loadHumanHistory();
}

async function connect() {
  const button = $('#connect');
  const password = $('#password').value;
  $('#loginError').textContent = '';
  if (!password) {
    $('#loginError').textContent = 'Enter your Control Center password.';
    return;
  }
  setBusy(button, true, 'Connecting…');
  try {
    const login = await api('/api/login', {method: 'POST', body: JSON.stringify({password})});
    state.csrfToken = login.csrf_token;
    [state.overview, state.projects, state.telemetry, state.telemetryHistory] = await Promise.all([api('/api/overview'), api('/api/projects'), api('/api/telemetry'), api('/api/telemetry/history?hours=6')]);
    $('#password').value = '';
    document.body.classList.add('connected');
    $('#login').hidden = true;
    $('#app').hidden = false;
    populateProjects();
    showView('dashboard');
    stream(++state.streamGeneration);
    startTelemetry(state.streamGeneration);
  } catch (error) {
    state.csrfToken = '';
    $('#loginError').textContent = 'Could not sign in. Check the password and server status.';
    setConnection('offline', 'Disconnected');
  } finally {
    setBusy(button, false);
  }
}

async function disconnect() {
  state.streamGeneration += 1;
  try { await api('/api/logout', {method: 'POST', body: '{}'}); } catch (_) { /* session may already be expired */ }
  state.csrfToken = '';
  state.overview = null;
  state.telemetry = null;
  state.telemetryHistory = [];
  state.projects = [];
  state.events = [];
  state.humanHistory = [];
  state.lastEventId = 0;
  if (state.telemetryTimer) clearInterval(state.telemetryTimer);
  if (state.telemetryHistoryTimer) clearInterval(state.telemetryHistoryTimer);
  state.telemetryTimer = null;
  state.telemetryHistoryTimer = null;
  document.body.classList.remove('connected');
  $('#app').hidden = true;
  $('#login').hidden = false;
  $('#password').value = '';
  setConnection('offline', 'Disconnected');
  toast('Disconnected', 'Your session was cleared from this tab.');
}

async function stream(generation) {
  if (!state.csrfToken || generation !== state.streamGeneration) return;
  try {
    const response = await fetch('/api/stream', {
      credentials: 'same-origin',
      headers: state.lastEventId ? {'Last-Event-ID': String(state.lastEventId)} : {},
    });
    if (!response.ok) throw new Error('stream unavailable');
    setConnection('online', 'Live');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (generation === state.streamGeneration) {
      const {value, done} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      const frames = buffer.split('\n\n');
      buffer = frames.pop();
      for (const frame of frames) {
        const line = frame.split('\n').find(item => item.startsWith('data: '));
        if (!line) continue;
        const idLine = frame.split('\n').find(item => item.startsWith('id: '));
        if (idLine) state.lastEventId = Number(idLine.slice(4)) || state.lastEventId;
        const typeLine = frame.split('\n').find(item => item.startsWith('event: '));
        if (typeLine && typeLine.slice(7) !== 'snapshot') {
          if (state.view === 'events') loadEvents(true);
          continue;
        }
        state.overview = JSON.parse(line.slice(6));
        if (state.overview.telemetry) state.telemetry = state.overview.telemetry;
        render();
        if (state.view === 'events') loadEvents(true);
      }
    }
  } catch (_) {
    if (generation !== state.streamGeneration) return;
  }
  if (generation === state.streamGeneration) {
    setConnection('offline', 'Reconnecting…');
    setTimeout(() => stream(generation), 2000);
  }
}

async function refreshTelemetry(generation) {
  if (!state.csrfToken || generation !== state.streamGeneration || state.telemetryInFlight) return;
  state.telemetryInFlight = true;
  try {
    state.telemetry = await api('/api/telemetry');
    if (state.overview) state.overview.telemetry = state.telemetry;
    if (state.view === 'dashboard') renderDashboard();
  } catch (_) {
    // The one-second loop keeps trying; the last good reading stays visible.
  } finally {
    state.telemetryInFlight = false;
  }
}

async function refreshTelemetryHistory(generation) {
  if (!state.csrfToken || generation !== state.streamGeneration || state.telemetryHistoryInFlight) return;
  state.telemetryHistoryInFlight = true;
  try {
    state.telemetryHistory = await api('/api/telemetry/history?hours=6');
    if (state.view === 'dashboard') renderDashboard();
  } catch (_) {
    // Historical data is additive; retain the last successful chart.
  } finally {
    state.telemetryHistoryInFlight = false;
  }
}

function startTelemetry(generation) {
  if (state.telemetryTimer) clearInterval(state.telemetryTimer);
  refreshTelemetry(generation);
  state.telemetryTimer = setInterval(() => refreshTelemetry(generation), 1000);
  refreshTelemetryHistory(generation);
  state.telemetryHistoryTimer = setInterval(() => refreshTelemetryHistory(generation), 10_000);
}

function render() {
  if (!state.overview) return;
  if (state.view === 'dashboard') renderDashboard();
  if (state.view === 'projects') renderProjects();
  if (state.view === 'agents') renderAgents();
  if (state.view === 'jobs') renderJobs();
  if (state.view === 'missions') renderMissions();
  if (state.view === 'events') renderEventView();
  if (state.view === 'job' && state.jobId) renderJob(state.jobId);
}

function renderJobs() {
  $('#jobsView').innerHTML = `<div class="section-head"><div class="section-title"><h2>All jobs</h2><small>Running, waiting, blocked, and completed work</small></div><button data-action="worktree-cleanup">Clean up worktrees</button></div>${jobsTable(state.overview.jobs || [])}`;
}

function metricCard(label, value, detail, iconName, stateName = '') {
  return `<article class="card metric ${stateName ? `metric-${stateName}` : ''}">
    <div class="metric-head"><span>${esc(label)}</span><span class="metric-icon">${icon(iconName)}</span></div>
    <div><strong>${esc(value)}</strong><small>${esc(detail || '')}</small></div>
  </article>`;
}

function serviceCard(name, online, detail, iconName) {
  return metricCard(name, online ? 'Online' : 'Offline', detail, iconName, online ? 'online' : 'offline');
}

function renderDashboard() {
  const overview = state.overview;
  const telemetry = state.telemetry || overview.telemetry || {};
  const ollama = telemetry.ollama || {};
  const gpu = telemetry.gpu || {};
  const router = telemetry.routing_agent || {};
  const orchestrator = (overview.workers || []).find(worker => worker.component === 'orchestrator');
  const jobs = overview.jobs || [];
  const active = jobs.filter(job => !['complete', 'cancelled', 'failed'].includes(job.status));
  const attention = jobs.filter(job => ['needs_human', 'blocked', 'failed'].includes(job.status));
  const loaded = ollama.loaded_model || {};
  const model = loaded.name || loaded.model || 'No model loaded';
  const context = loaded.context_length || ollama.context_length || gpu.context_length;
  const modelVram = loaded.size_vram || ollama.size_vram;
  const services = [orchestrator?.online, true, ollama.status === 'online', router.status === 'online'];
  const healthyServices = services.filter(Boolean).length;
  const cloudSpend = Number(overview.inference?.estimated_cloud_spend || 0);
  const utilization = Math.max(0, Number(gpu.utilization_percent || 0));
  const vramTotal = Number(gpu.vram_total_mb || 0);
  const vramUsed = Number(gpu.vram_used_mb || 0);
  const vramPercent = vramTotal ? Math.round(vramUsed / vramTotal * 100) : 0;
  const phaseMetrics = overview.phase_metrics || [];
  const slowestPhase = phaseMetrics.reduce((best, item) => Number(item.max_seconds || 0) > Number(best?.max_seconds || 0) ? item : best, null);
  const ollamaVersion = ollama.version ? `Ollama ${ollama.version}` : 'Ollama version unavailable';
  const ollamaModels = Array.isArray(ollama.models) ? ollama.models : [];

  $('#dashboardView').innerHTML = `
    <div class="grid">
      ${metricCard('Active jobs', active.length, `${jobs.filter(j => j.status === 'complete').length} completed`, 'activity')}
      ${metricCard('Needs attention', attention.length, attention.length ? 'Operator decision required' : 'Nothing waiting on you', 'alert', attention.length ? 'offline' : 'online')}
      ${metricCard('Healthy services', `${healthyServices} / 4`, healthyServices === 4 ? 'All systems nominal' : 'Check service health below', 'server', healthyServices === 4 ? 'online' : 'offline')}
      ${metricCard('Cloud spend', `$${cloudSpend.toFixed(3)}`, `${overview.inference?.cloud_requests || 0} routed requests`, 'cloud')}
      ${metricCard('Slowest phase', slowestPhase ? `${Number(slowestPhase.max_seconds).toFixed(1)}s` : '—', slowestPhase ? `${slowestPhase.phase} · ${Number(slowestPhase.max_prompt_chars || 0).toLocaleString()} prompt chars` : 'Telemetry begins after migration', 'activity')}
    </div>
    ${attention.length || (overview.human_queue || []).length ? `<div class="section-head"><div class="section-title"><h2>Needs your attention</h2><small>Jobs and durable decisions waiting for you</small></div></div>${attentionPanel(attention)}${humanQueuePanel(overview.human_queue || [])}` : ''}
    <div class="section-head"><div class="section-title"><h2>Active jobs</h2><small>Work currently moving through the delivery loop</small></div><span>${active.length} running</span></div>
    ${jobsTable(active)}
    <div class="section-head"><div class="section-title"><h2>System health</h2><small>Live service and model availability</small></div><span>Updated just now</span></div>
    <div class="grid">
      ${serviceCard('Orchestrator', Boolean(orchestrator?.online), orchestrator?.current_action || 'No recent heartbeat', 'route')}
      ${serviceCard('PostgreSQL', true, 'Durable workflow connected', 'database')}
      ${serviceCard('Ollama', ollama.status === 'online', `${ollamaVersion} · ${model}${context ? ` · ${context} context` : ''}${modelVram ? ` · ${bytes(modelVram)} VRAM` : ''}`, 'brain')}
      ${serviceCard('Routing agent', router.status === 'online', router.service || router.status || 'Not configured', 'route')}
    </div>
    <div class="section-head"><div class="section-title"><h2>Compute</h2><small>Local inference capacity and routing</small></div></div>
    <div class="grid">
      <article class="card metric"><div class="metric-head"><span>GPU utilization</span><span class="metric-icon">${icon('chip')}</span></div><div><div class="metric-value"><strong>${esc(gpu.utilization_percent ?? '—')}</strong><em>%</em></div><small>${esc(gpu.name || 'GPU telemetry not configured')}</small><div class="bar"><span style="width:${Math.min(utilization, 100)}%"></span></div></div></article>
      <article class="card metric"><div class="metric-head"><span>VRAM allocation</span><span class="metric-icon">${icon('brain')}</span></div><div><div class="metric-value"><strong>${vramTotal ? `${vramUsed} / ${vramTotal}` : '—'}</strong><em>MB</em></div><small>${vramTotal ? `${vramPercent}% allocated` : 'No VRAM telemetry'}</small><div class="bar"><span style="width:${vramPercent}%"></span></div></div></article>
      <article class="card metric"><div class="metric-head"><span>Thermals</span><span class="metric-icon">${icon('thermometer')}</span></div><div><div class="metric-value"><strong>${esc(gpu.temperature_c ?? '—')}</strong><em>°C</em></div><small>${gpu.power_w ? `${esc(gpu.power_w)} watts` : 'Power data unavailable'}</small></div></article>
      <article class="card metric"><div class="metric-head"><span>Inference routes</span><span class="metric-icon">${icon('route')}</span></div><div class="routing-split"><div><b>${overview.inference?.local_requests || 0}</b><small>Local</small></div><div><b>${overview.inference?.cloud_requests || 0}</b><small>Cloud</small></div><div><b>${overview.inference?.fallback_requests || 0}</b><small>Fallback</small></div></div></article>
    </div>
    ${telemetryChart(state.telemetryHistory || [])}
    ${ollamaHistoryChart(state.telemetryHistory || [])}
    ${inferenceChart(state.telemetryHistory || [])}
    ${alertsPanel(overview.alerts || [])}
    <div class="section-head"><div class="section-title"><h2>Ollama detail</h2><small>Read-only data refreshed every second</small></div><span>${esc(ollama.loaded_count || 0)} loaded · ${esc(ollama.model_count || ollamaModels.length || 0)} installed</span></div>
    <div class="card telemetry-detail"><div class="facts"><div class="fact"><span>API</span>${badge(ollama.status || 'unavailable')}</div><div class="fact"><span>Version</span><strong>${esc(ollama.version || '—')}</strong></div><div class="fact"><span>Loaded model</span><strong>${esc(model)}</strong></div><div class="fact"><span>Processor</span><strong>${esc(loaded.processor || ollama.processor || '—')}</strong></div><div class="fact"><span>Model size</span><strong>${bytes(loaded.size || ollama.size) || '—'}</strong></div><div class="fact"><span>VRAM used by model</span><strong>${bytes(modelVram) || '—'}</strong></div><div class="fact"><span>Context</span><strong>${esc(context || '—')}</strong></div><div class="fact"><span>Keep-alive until</span><strong>${esc(ollama.expires_at || '—')}</strong></div></div><div class="chips">${ollamaModels.slice(0, 24).map(item => `<span class="chip">${esc(item.name || item.model || item.digest || 'model')}</span>`).join('') || '<span class="muted">No installed models reported</span>'}</div></div>
    <div class="section-head"><div class="section-title"><h2>Recent autonomous commits</h2><small>Reviewer-approved checkpoints produced by the team</small></div></div>
    ${commitsList(overview.recent_commits || [])}`;
}

function telemetryChart(samples) {
  const values = (samples || []).slice(-180).map(item => ({
    utilization: Number(item.gpu?.utilization_percent),
    temperature: Number(item.gpu?.temperature_c),
    time: item.sampled_at || item.observed_at,
  })).filter(item => Number.isFinite(item.utilization) || Number.isFinite(item.temperature));
  if (values.length < 2) return '<div class="section-head"><div class="section-title"><h2>Telemetry history</h2><small>GPU history appears after the first few samples.</small></div></div>';
  const width = 720, height = 160, pad = 18;
  const line = (key, color, max) => {
    const points = values.map((item, index) => {
      const value = Number.isFinite(item[key]) ? item[key] : 0;
      return `${pad + index * (width - pad * 2) / Math.max(1, values.length - 1)},${height - pad - Math.min(1, Math.max(0, value / max)) * (height - pad * 2)}`;
    }).join(' ');
    return `<polyline points="${points}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
  };
  return `<div class="section-head"><div class="section-title"><h2>Telemetry history</h2><small>Last ${values.length} samples · up to six hours</small></div><span>Live + persisted</span></div><article class="card telemetry-chart"><div class="chart-legend"><span><i class="legend-util"></i>GPU utilization</span><span><i class="legend-temp"></i>Temperature</span></div><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="GPU utilization and temperature history"><line x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}" class="chart-axis"/>${line('utilization', '#55d9d1', 100)}${line('temperature', '#f5bc67', 100)}</svg><div class="chart-caption"><span>${esc(age(values[0].time))}</span><span>Now</span></div></article>`;
}

function inferenceChart(samples) {
  const values = (samples || []).slice(-180).map(item => ({
    local: Number(item.inference?.local_requests),
    cloud: Number(item.inference?.cloud_requests),
    fallback: Number(item.inference?.fallback_requests),
    time: item.sampled_at || item.observed_at,
  })).filter(item => Number.isFinite(item.local) || Number.isFinite(item.cloud) || Number.isFinite(item.fallback));
  const latest = values.at(-1);
  if (!latest) return '';
  const routes = [
    ['Local', Number(latest.local || 0), 'var(--cyan)'],
    ['Cloud', Number(latest.cloud || 0), 'var(--violet)'],
    ['Fallback', Number(latest.fallback || 0), 'var(--amber)'],
  ];
  const max = Math.max(1, ...routes.map(item => item[1]));
  const width = 720, height = 120, pad = 18;
  const routeLine = (key, color) => {
    const points = values.map((item, index) => {
      const value = Number.isFinite(item[key]) ? item[key] : 0;
      const x = pad + index * (width - pad * 2) / Math.max(1, values.length - 1);
      const y = height - pad - Math.min(1, Math.max(0, value / max)) * (height - pad * 2);
      return `${x},${y}`;
    }).join(' ');
    return `<polyline points="${points}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
  };
  const history = values.length > 1 ? `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Inference route history"><line x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}" class="chart-axis"/>${routeLine('local', '#55d9d1')}${routeLine('cloud', '#a98bff')}${routeLine('fallback', '#f5bc67')}</svg><div class="chart-caption"><span>${esc(age(values[0].time))}</span><span>Now</span></div>` : '';
  return `<div class="section-head"><div class="section-title"><h2>Inference history</h2><small>Persisted route totals from the workflow database</small></div><span>Latest sample · ${values.length} points</span></div><article class="card inference-chart"><div class="chart-legend"><span><i class="legend-util"></i>Local</span><span><i class="legend-temp"></i>Cloud</span><span><i class="legend-cloud"></i>Fallback</span></div>${history}${routes.map(item => `<div class="inference-bar"><span>${item[0]}</span><div class="bar"><i style="width:${Math.min(100, item[1] / max * 100)}%;background:${item[2]}"></i></div><strong>${item[1].toLocaleString()}</strong></div>`).join('')}</article>`;
}

function ollamaHistoryChart(samples) {
  const values = (samples || []).slice(-180).map(item => ({
    loaded: Number(item.ollama?.loaded_count ?? item.ollama?.model_count),
    time: item.sampled_at || item.observed_at,
  })).filter(item => Number.isFinite(item.loaded));
  if (values.length < 2) return '';
  const width = 720, height = 120, pad = 18;
  const max = Math.max(1, ...values.map(item => item.loaded));
  const points = values.map((item, index) => {
    const x = pad + index * (width - pad * 2) / Math.max(1, values.length - 1);
    const y = height - pad - Math.min(1, Math.max(0, item.loaded / max)) * (height - pad * 2);
    return `${x},${y}`;
  }).join(' ');
  return `<div class="section-head"><div class="section-title"><h2>Ollama history</h2><small>Loaded model count · last ${values.length} samples</small></div><span>${values.at(-1).loaded} loaded now</span></div><article class="card telemetry-chart"><div class="chart-legend"><span><i class="legend-util"></i>Loaded models</span></div><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Ollama loaded model history"><line x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}" class="chart-axis"/><polyline points="${points}" fill="none" stroke="#55d9d1" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg><div class="chart-caption"><span>${esc(age(values[0].time))}</span><span>Now</span></div></article>`;
}

function alertsPanel(alerts) {
  if (!alerts?.length) return '';
  return `<div class="section-head"><div class="section-title"><h2>Alerts</h2><small>Actionable health warnings from the latest observation</small></div><span>${alerts.length}</span></div><div class="card alert-list">${alerts.map(alert => `<div class="alert-row"><span class="status ${alert.severity === 'critical' ? 'failed' : 'changes_requested'}">${esc(alert.severity)}</span><strong>${esc(alert.message)}</strong><small>${esc(alert.source)}</small></div>`).join('')}</div>`;
}

function humanQueuePanel(items) {
  if (!items.length) return '';
  return `<div class="card attention"><div class="attention-head">${icon('alert')}<h3>Human queue</h3></div>${items.map(item => `<button class="attention-row" data-action="open-job" data-job-id="${Number(item.job_id || 0)}"><span>${badge(item.kind)}</span><strong>${esc(item.question)}</strong><small>${item.job_id ? `Job #${Number(item.job_id)}` : 'Mission decision'} · ${age(item.created_at)}</small><b>Review →</b></button>`).join('')}</div>`;
}

async function loadHumanHistory() {
  if (state.humanHistoryInFlight) return;
  state.humanHistoryInFlight = true;
  try {
    state.humanHistory = await api('/api/human-queue?status=all&limit=100');
    if (state.view === 'missions') renderMissions();
  } catch (error) { toast('Could not load human history', error.message, true); }
  finally { state.humanHistoryInFlight = false; }
}

function humanDecisionHistory(items) {
  const current = (items || []).filter(item => item.status === 'open');
  const resolved = (items || []).filter(item => item.status !== 'open');
  const rows = group => group.map(item => {
    const events = (item.history || []).map(event => `<li><b>${esc(event.event_type.replaceAll('_', ' '))}</b><span>${esc(event.actor || 'workflow')}</span><time>${esc(age(event.created_at))}</time></li>`).join('');
    return `<details class="human-history-row"><summary>${badge(item.status)}<strong>${esc(item.question)}</strong><small>${esc(item.owner || 'Unassigned')} · ${esc(item.resolution || 'Awaiting decision')}</small></summary><div class="human-history-detail"><p>${item.answer ? `<b>Answer:</b> ${esc(item.answer)}` : 'No answer recorded.'}</p>${item.outcome?.type ? `<p><b>Outcome:</b> ${esc(item.outcome.type)}</p>` : ''}<ol>${events}</ol></div></details>`;
  }).join('');
  return `<div class="section-head"><div class="section-title"><h2>Human decision history</h2><small>Append-only evidence, ownership, answers, resolution, and outcomes</small></div><span>${current.length} open · ${resolved.length} resolved</span></div><div class="card human-history"><h3>Current requests</h3>${rows(current) || '<p class="muted">No decisions are waiting.</p>'}<h3>Resolved history</h3>${rows(resolved) || '<p class="muted">No resolved decisions yet.</p>'}</div>`;
}

function renderMissions() {
  const missions = state.overview.missions || [];
  $('#missionsView').innerHTML = `<div class="section-head"><div class="section-title"><h2>Mission control</h2><small>Roadmap work is materialized into bounded packages before engineering starts.</small></div><span>${missions.length} mission${missions.length === 1 ? '' : 's'}</span></div>${missions.length ? `<div class="project-list">${missions.map(mission => `<article class="card project-card"><div class="section-head tight"><div><span class="kicker">MISSION #${Number(mission.id)}</span><h3>${esc(mission.goal)}</h3></div>${badge(mission.status)}</div><p class="path">${esc(mission.repository)} · ${esc(mission.branch)}</p><div class="project-meta"><div><span>Packages</span><b>${Number(mission.completed_packages || 0)} / ${Number(mission.package_count || 0)} complete</b></div><div><span>Roadmap pending</span><b>${Number(mission.roadmap_pending_packages || 0)}</b></div><div><span>Blocked</span><b>${Number(mission.blocked_packages || 0)}</b></div><div><span>Human queue</span><b>${Number(mission.open_human_requests || 0)}</b></div><div><span>Updated</span><b>${esc(age(mission.updated_at))}</b></div></div><button class="primary" data-action="open-mission" data-mission-id="${Number(mission.id)}">Open mission →</button></article>`).join('')}</div>` : '<div class="card empty"><div><strong>No V2 missions yet</strong>Create one with the orchestrator mission command, then let the roadmap manager populate packages.</div></div>'}${humanDecisionHistory(state.humanHistory)}`;
}

function missionControls(mission) {
  const id = Number(mission.id);
  const status = String(mission.status || '');
  const pause = ['active'].includes(status);
  const resume = ['paused', 'blocked'].includes(status);
  const cancel = !['complete', 'cancelled'].includes(status);
  return `${pause ? `<button data-action="mission-action" data-mission-action="pause" data-mission-id="${id}">Pause mission</button>` : ''}
    ${resume ? `<button class="primary" data-action="mission-action" data-mission-action="resume" data-mission-id="${id}">Resume mission</button>` : ''}
    ${cancel ? `<button class="danger" data-action="mission-action" data-mission-action="cancel" data-mission-id="${id}">Cancel mission</button>` : ''}`;
}

function dependencyGraph(packages) {
  if (!packages?.length) return '';
  const nodes = packages.map(pkg => {
    const dependencies = Array.isArray(pkg.dependencies) ? pkg.dependencies.map(Number) : [];
    const unmet = new Set((pkg.unmet_dependencies || []).map(Number));
    const links = dependencies.length
      ? dependencies.map(id => `<span class="dependency-link ${unmet.has(id) ? 'waiting' : 'satisfied'}">#${id} ${unmet.has(id) ? 'waiting' : 'complete'}</span>`).join('')
      : '<span class="dependency-root">Starts independently</span>';
    return `<article class="dependency-node dependency-${esc(pkg.dependency_state || 'ready')}">
      <div class="dependency-node-head"><span class="kicker">PACKAGE #${Number(pkg.id)}</span>${badge(pkg.status)}</div>
      <strong>${esc(pkg.objective)}</strong>
      <div class="dependency-links"><span>Depends on</span>${links}</div>
    </article>`;
  }).join('');
  return `<div class="section-head"><div class="section-title"><h2>Dependencies</h2><small>Derived from each Work Package dependency list; waiting packages unlock when every prerequisite completes.</small></div></div><div class="dependency-graph">${nodes}</div>`;
}

function durationLabel(seconds) {
  const value = Number(seconds || 0);
  if (value < 60) return `${value.toFixed(1)}s`;
  if (value < 3600) return `${(value / 60).toFixed(1)}m`;
  if (value < 86400) return `${(value / 3600).toFixed(1)}h`;
  return `${(value / 86400).toFixed(1)}d`;
}

function missionMetrics(metrics) {
  if (!metrics) return '';
  const progress = metrics.package_progress || {};
  const quality = metrics.quality || {};
  const verification = metrics.verification || {};
  const latency = metrics.latency || {};
  const models = metrics.models || {};
  const phases = Object.entries(metrics.phases || {});
  const providers = Object.entries(models.providers || {});
  return `<div class="section-head"><div class="section-title"><h2>Mission metrics</h2><small>Calculated from durable workflow evidence; cancelled packages are reported but excluded from the progress denominator.</small></div><span>${Number(progress.denominator || 0)} delivery packages</span></div>
    <div class="grid mission-metrics">
      ${metricCard('Mission progress', `${Number(progress.percent || 0).toFixed(0)}%`, `${Number(progress.evidence_complete || 0)} of ${Number(progress.denominator || 0)} evidence-complete`, 'activity')}
      ${metricCard('Quality and revisions', Number(quality.revision_cycles || 0), `${Number(quality.first_pass_approval_percent || 0).toFixed(0)}% first-pass review`, 'review')}
      ${metricCard('Verification', `${Number(verification.first_pass_percent || 0).toFixed(0)}%`, `${Number(verification.passed || 0)} passed · ${Number(verification.failed || 0)} failed`, 'check')}
      ${metricCard('End-to-end latency', durationLabel(latency.elapsed_seconds), `${durationLabel(latency.recorded_phase_seconds)} recorded phase time`, 'activity')}
      ${metricCard('Model usage', Number(models.calls || 0), `${Number(models.input_tokens || 0).toLocaleString()} in · ${Number(models.output_tokens || 0).toLocaleString()} out`, 'brain')}
      ${metricCard('Cloud cost', `$${Number(models.estimated_cloud_cost || 0).toFixed(3)}`, `${Number(models.cloud_calls || 0)} cloud · ${Number(models.fallback_calls || 0)} fallback`, 'cloud')}
    </div>
    ${phases.length ? `<div class="card metric-breakdown"><h3>Phase latency</h3>${phases.map(([name, item]) => `<div><span>${esc(phaseLabel(name))}</span><b>${Number(item.runs || 0)} runs</b><small>${durationLabel(item.average_seconds)} avg · ${durationLabel(item.max_seconds)} max</small></div>`).join('')}</div>` : ''}
    ${providers.length ? `<div class="card metric-breakdown"><h3>Provider usage</h3>${providers.map(([name, item]) => `<div><span>${esc(name)}</span><b>${Number(item.calls || 0)} calls</b><small>${durationLabel(item.average_latency_seconds)} avg · $${Number(item.estimated_cloud_cost || 0).toFixed(3)}</small></div>`).join('')}</div>` : ''}`;
}

async function openMission(id) {
  try {
    const mission = await api(`/api/missions/${Number(id)}`);
    const packageRows = (mission.packages || []).map(pkg => {
      const evidence = pkg.completion_evidence;
      const evidenceLabel = evidence ? (evidence.eligible ? 'Roadmap complete' : (evidence.ready_for_integration ? 'Awaiting integration' : 'Evidence incomplete')) : '';
      return `<div class="commit-row"><span>${badge(pkg.status)}</span><div class="commit-main"><strong>${esc(pkg.objective)}</strong><small>${esc(pkg.roadmap_reference || 'manual package')} · ${esc(pkg.branch)}${evidenceLabel ? ` · ${evidenceLabel}` : ''}</small></div><span class="code">${esc((pkg.resulting_commit || 'not committed').slice(0, 10))}</span></div>`;
    }).join('');
    const operations = (mission.control_operations || []).map(operation => `<div class="operation-row">${badge(operation.status)}<strong>${esc(operation.action)}</strong><span class="code">${esc(String(operation.operation_id || '').slice(0, 12))}</span><small>${age(operation.created_at)}</small></div>`).join('');
    $('#missionsView').innerHTML = `<div class="section-head mission-head"><div class="section-title"><span class="kicker">MISSION #${Number(mission.id)}</span><h2>${esc(mission.goal)} ${badge(mission.status)}</h2><small>${esc(mission.repository)} · ${esc(mission.branch)}</small></div><div class="job-actions">${missionControls(mission)}<button data-action="back-missions">← All missions</button></div></div>${missionMetrics(mission.metrics)}${dependencyGraph(mission.packages || [])}<div class="section-head"><div class="section-title"><h2>Work packages</h2><small>Durable execution and completion evidence</small></div></div><div class="card commit-list">${packageRows || '<div class="empty"><div><strong>No packages yet</strong>The roadmap manager will materialize the next bounded items.</div></div>'}</div>${(mission.integrations || []).length ? `<div class="section-head"><div class="section-title"><h2>Integration</h2><small>Verified package commits and branch updates</small></div></div><div class="card commit-list">${mission.integrations.map(item => `<div class="commit-row">${badge(item.status)}<div class="commit-main"><strong>${esc(item.source_branch)} → ${esc(item.target_branch)}</strong><small>${esc(item.error || item.commit_sha || 'Pending')}</small></div></div>`).join('')}</div>` : ''}${operations ? `<div class="section-head"><div class="section-title"><h2>Control history</h2><small>Durable mission operation audit</small></div></div><div class="card operation-list">${operations}</div>` : ''}`;
  } catch (error) { toast('Could not load mission', error.message, true); }
}

async function missionAction(action, button) {
  const missionId = Number(button.dataset.missionId);
  if (action === 'cancel' && !confirm('Cancel this entire mission? Unfinished packages will stop and durable evidence will be kept.')) return;
  setBusy(button, true);
  try {
    const result = await api(`/api/missions/${missionId}/${action}`, {method: 'POST', body: JSON.stringify({confirm: action === 'cancel'})});
    toast(`Mission ${action === 'pause' ? 'paused' : action === 'resume' ? 'resumed' : 'cancelled'}`, `Operation ${String(result.operation_id || '').slice(0, 12)} was applied.`);
    state.overview = await api('/api/overview');
    await openMission(missionId);
  } catch (error) { toast('Mission action failed', error.message, true); }
  finally { setBusy(button, false); }
}

function attentionPanel(jobs) {
  return `<div class="card attention"><div class="attention-head">${icon('alert')}<h3>Operator action requested</h3></div>${jobs.map(job => `
    <button class="attention-row" data-action="open-job" data-job-id="${Number(job.id)}">
      <span>${badge(job.status)}</span><strong>${esc(job.goal)}</strong><small>Job #${Number(job.id)} · ${esc(projectFor(job.repository)?.name || job.repository)}</small><b>Review →</b>
    </button>`).join('')}</div>`;
}

function progressFor(job) {
  const done = Number(job.completed_steps || 0);
  const total = done + Number(job.open_steps || 0);
  return {done, total, percent: total ? Math.round(done / total * 100) : 0};
}

function jobsTable(jobs) {
  // Removed jobs remain in PostgreSQL for auditability, but are no longer
  // part of the operator's active queue.
  jobs = jobs.filter(job => job.status !== 'cancelled');
  if (!jobs.length) return '<div class="card empty"><div><strong>No active jobs</strong>Launch a job when you are ready to put the team to work.</div></div>';
  return `<div class="card table-card"><table><thead><tr><th>Mission</th><th>Project</th><th>Status</th><th>Current phase</th><th>Progress</th><th>Blocker / action</th><th></th></tr></thead><tbody>${jobs.map(job => {
    const progress = progressFor(job);
    const blocker = job.blocker ? (typeof job.blocker === 'string' ? job.blocker : (job.blocker.reason || job.blocker.message || JSON.stringify(job.blocker))) : '';
    const lease = job.planner_worker_id ? `${job.planner_worker_id} · expires ${age(job.planner_lease_expires_at)}` : 'Unleased';
    const progressState = job.progress_classification ? ` · ${job.progress_classification.replaceAll('_', ' ')}` : '';
    const remove = job.status !== 'cancelled' ? `<button class="row-remove" data-action="job-action" data-job-action="remove" data-job-id="${Number(job.id)}" aria-label="Remove job ${Number(job.id)}" title="Remove from queue">×</button>` : '';
    return `<tr class="clickable"><td class="job-goal">${esc(job.goal)}</td><td>${esc(projectFor(job.repository)?.name || job.repository)}</td><td>${badge(job.status)}</td><td>${esc(phaseLabel(job.current_phase))}<small class="table-meta">${esc(lease)}${esc(progressState)}</small></td><td><div class="progress"><div class="progress-label"><span>${progress.done}/${progress.total || '—'} steps</span><span>${progress.percent}%</span></div><div class="bar"><span style="width:${progress.percent}%"></span></div></div></td><td class="job-blocker">${esc(blocker || (['blocked','needs_human','failed'].includes(job.status) ? 'Needs attention' : '—'))}</td><td class="job-row-actions">${remove}<button class="row-open" data-action="open-job" data-job-id="${Number(job.id)}" aria-label="Manage job ${Number(job.id)}">→</button></td></tr>`;
  }).join('')}</tbody></table></div>`;
}

function commitsList(items) {
  if (!items.length) return '<div class="card empty"><div><strong>No checkpoints yet</strong>Approved commits will appear here with review and verification evidence.</div></div>';
  return `<div class="card commit-list">${items.map(commit => `<div class="commit-row">
    <span class="code commit-sha">${esc((commit.commit_sha || '').slice(0, 9))}</span>
    <div class="commit-main"><strong>${esc(commit.title)}</strong><small>${esc(projectFor(commit.repository)?.name || commit.repository)}</small></div>
    <small>${(commit.approved_files || []).length} files</small>
    ${badge(commit.reviewer_verdict)}${badge(commit.verification_status)}
    <button class="row-open" data-action="open-job" data-job-id="${Number(commit.job_id)}" title="${esc(age(commit.completed_at))}">→</button>
  </div>`).join('')}</div>`;
}

async function refreshProjects() {
  try {
    state.projects = await api('/api/projects');
    populateProjects();
    if (state.view === 'projects') renderProjects();
  } catch (error) { toast('Could not refresh projects', error.message, true); }
}

function renderProjects() {
  $('#projectsView').innerHTML = `<div class="project-list">${state.projects.map(project => `
    <article class="card project-card">
      <div class="section-head tight"><div class="project-title"><span class="project-icon">${icon('folder')}</span><div><h3>${esc(project.name)}</h3><span class="path">${esc(project.id)}</span></div></div>${badge(project.available ? project.git_status : 'unavailable')}</div>
      <p class="path">${esc(project.repository)}</p>
      <div class="project-meta"><div><span>Target branch</span><b>${esc(project.branch)}</b></div><div><span>Checked out</span><b>${esc(project.current_branch || 'Unavailable')}</b></div><div><span>Roadmap</span><b>${project.roadmap_present ? esc(project.roadmap) : 'Not found'}</b></div><div><span>Active job</span><b>${project.current_job ? `#${Number(project.current_job.id)}` : 'None'}</b></div></div>
      <div class="latest-commit"><span class="kicker">LATEST CHECKPOINT</span><strong><span class="code">${esc((project.latest_autonomous_commit?.commit_sha || '—').slice(0, 10))}</span> ${esc(project.latest_autonomous_commit?.title || project.latest_commit?.message || 'No Git history')}</strong></div>
      <button class="primary" data-action="new-job" data-project-id="${esc(project.id)}">${icon('activity')} Start job</button>
    </article>`).join('')}</div>`;
}

function derivedAgents() {
  const work = state.overview.active_work || [];
  const events = Object.fromEntries((state.overview.agent_events || []).map(event => [event.agent, event]));
  const orchestrator = (state.overview.workers || []).find(worker => worker.component === 'orchestrator');
  const active = work[0];
  return [
    {name: 'Planner', role: 'Strategy & decomposition', icon: 'brain', state: active?.current_phase === 'planning' ? 'planning' : 'idle', detail: events['planner-agent']?.event_type, meta: active?.provider ? `${active.provider} / ${active.model}` : 'Ready for the next roadmap decision'},
    {name: 'EngineeringAgent', role: 'Implementation & debugging', icon: 'code', state: active?.status === 'running' ? 'engineering' : 'idle', detail: active?.status === 'running' ? active.title : (events['engineering-agent']?.event_type || events['coder-agent']?.event_type), meta: active?.files_changed?.length ? `${active.files_changed.length} files · ${active.command_count} recorded commands` : 'No implementation currently claimed'},
    {name: 'Reviewer', role: 'Independent quality gate', icon: 'review', state: active?.status === 'review' ? 'reviewing' : 'idle', detail: active?.verdict || events['reviewer-agent']?.event_type, meta: active?.open_issue_count ? `${active.open_issue_count} open review issues` : 'Waiting for reviewable work'},
    {name: 'Orchestrator', role: 'Deterministic coordination', icon: 'route', state: orchestrator?.online ? 'running' : 'stopped', detail: orchestrator?.current_action, meta: orchestrator?.started_at ? `Started ${age(orchestrator.started_at)} · heartbeat ${age(orchestrator.heartbeat_at)}` : 'No durable heartbeat'},
  ];
}

function renderAgents() {
  const workCount = (state.overview.active_work || []).length;
  $('#agentsView').innerHTML = `<div class="section-head"><div class="section-title"><h2>Development team</h2><small>${workCount} active workflow ${workCount === 1 ? 'step' : 'steps'} across all projects</small></div></div><div class="agent-list">${derivedAgents().map(agent => `
    <article class="card agent-card"><div class="section-head tight"><div class="agent-title"><span class="agent-avatar">${icon(agent.icon)}</span><div><h3>${agent.name}</h3><span class="muted">${agent.role}</span></div></div>${badge(agent.state)}</div><div class="agent-current"><span class="kicker">LATEST ACTIVITY</span><p>${esc((agent.detail || 'No active assignment').replaceAll('_', ' '))}</p></div><div class="agent-foot">${esc(agent.meta)}</div></article>`).join('')}</div>`;
}

function renderEventView() {
  const search = state.eventSearch;
  $('#eventsView').innerHTML = `<div class="card">
    <div class="filters"><button data-action="event-filter" data-filter="" class="${state.eventFilter === '' ? 'active' : ''}">All</button><button data-action="event-filter" data-filter="failures=1" class="${state.eventFilter === 'failures=1' ? 'active' : ''}">Failures</button><button data-action="event-filter" data-filter="routing=1" class="${state.eventFilter === 'routing=1' ? 'active' : ''}">Model routes</button><form class="filter-search" id="eventSearch"><input name="job_id" inputmode="numeric" placeholder="Job ID" value="${esc(search.job_id || '')}"><input name="agent" placeholder="Agent" value="${esc(search.agent || '')}"><button type="submit">Filter</button></form><button data-action="security-audit">Security audit</button></div>
    <div class="event-list" id="eventList">${eventRows(state.events)}</div>
  </div>`;
}

function eventRows(events) {
  return events.map(event => `<div class="event"><span class="event-dot"></span><time>${age(event.created_at)}</time><b>${esc(event.agent)}</b><div><span class="event-name">${esc(String(event.event_type || '').replaceAll('_', ' '))}</span>${event.correlation_id ? `<small class="event-correlation">correlation ${esc(String(event.correlation_id).slice(0, 12))}</small>` : ''}<details><summary>Technical context</summary><pre class="code">${esc(JSON.stringify(event.structured_payload, null, 2))}</pre></details></div></div>`).join('') || '<div class="empty"><div><strong>No matching events</strong>Try a different filter.</div></div>';
}

async function loadEvents(silent = false) {
  try {
    const search = new URLSearchParams(state.eventSearch);
    const suffix = [state.eventFilter, search.toString()].filter(Boolean).join('&');
    state.events = await api(`/api/events${suffix ? `?${suffix}` : ''}`);
    if (state.view === 'events') renderEventView();
  } catch (error) { if (!silent) toast('Could not load events', error.message, true); }
}

async function securityAudit(button) {
  setBusy(button, true, 'Scanning…');
  try {
    const result = await api('/api/security/audit');
    if (result.clean) toast('Security audit passed', `Checked ${result.checked} recent records.`);
    else toast('Security audit found patterns', result.findings.join(', '), true);
  } catch (error) { toast('Security audit failed', error.message, true); }
  finally { setBusy(button, false); }
}

async function openJob(id) {
  state.jobId = Number(id);
  showView('job');
  await renderJob(state.jobId);
}

function stageFor(status) {
  return {pending: 'planner', planning: 'planner', queued: 'engineer', running: 'engineer',
    coder: 'engineer', coding: 'engineer', engineering: 'engineer', coder_revision: 'engineer',
    reviewing: 'reviewer', review: 'reviewer', changes_requested: 'engineer',
    verifying: 'verification', verification: 'verification', checkpointing: 'commit',
    checkpoint: 'commit', complete: 'commit'}[status] || 'planner';
}

function jobControls(job) {
  const active = ['pending', 'planning', 'running', 'reviewing', 'verifying', 'checkpointing'].includes(job.status);
  const resumable = ['paused', 'blocked', 'failed'].includes(job.status);
  const cancellable = !['complete', 'cancelled'].includes(job.status);
  const removable = job.status !== 'cancelled';
  const canRetry = ['blocked', 'failed'].includes(job.status);
  const canRefreshPlanning = !['complete', 'cancelled'].includes(job.status);
  const canRerunVerification = ['verifying', 'verification', 'changes_requested'].includes(job.status) || job.current_phase === 'verification';
  const id = Number(job.id);
  return `${active ? `<button type="button" data-action="job-action" data-job-action="pause" data-job-id="${id}">Pause</button>` : ''}${resumable ? `<button type="button" class="primary" data-action="job-action" data-job-action="resume" data-job-id="${id}">Resume</button>` : ''}${canRetry ? `<button type="button" class="primary" data-action="job-action" data-job-action="retry" data-job-id="${id}">Retry job</button>` : ''}${canRerunVerification ? `<button type="button" data-action="job-action" data-job-action="rerun-verification" data-job-id="${id}">Rerun verification</button>` : ''}${active ? `<button type="button" data-action="job-action" data-job-action="recover-lease" data-job-id="${id}">Recover lease</button>` : ''}${canRefreshPlanning ? `<button type="button" data-action="job-action" data-job-action="refresh-planning" data-job-id="${id}">Refresh planning</button>` : ''}${removable ? `<button type="button" class="danger" data-action="job-action" data-job-action="remove" data-job-id="${id}">Remove from queue</button>` : ''}${cancellable ? `<button type="button" class="danger" data-action="job-action" data-job-action="cancel" data-job-id="${id}">Cancel</button>` : ''}`;
}

function operationsPanel(operations) {
  if (!operations?.length) return '';
  return `<div class="section-head"><div class="section-title"><h2>Operator actions</h2><small>Durable control requests and their outcomes</small></div></div><div class="card operation-list">${operations.slice(0, 12).map(operation => `<div class="operation-row"><span class="code">${esc(String(operation.operation_id || '').slice(0, 8))}</span>${badge(operation.action)}${badge(operation.status)}<small>${esc(age(operation.created_at))}${operation.error ? ` · ${esc(operation.error)}` : ''}</small></div>`).join('')}</div>`;
}

async function renderJob(id) {
  try {
    const detail = await api(`/api/jobs/${id}`);
    if (state.jobId !== id || state.view !== 'job') return;
    const job = detail.job;
    const current = detail.current_step_detail;
    const stage = stageFor(current?.status || job.status);
    const stages = ['planner', 'engineer', 'reviewer', 'verification', 'commit'];
    const stageNames = {planner: 'Planner', engineer: 'Engineering', reviewer: 'Reviewer', verification: 'Verification', commit: 'Commit'};
    const activeIndex = stages.indexOf(stage);
    const activeHumanAnswer = document.activeElement?.id === 'humanAnswer';
    const humanAnswer = $('#humanAnswer');
    const humanSelection = humanAnswer ? {
	start: humanAnswer.selectionStart,
  	end: humanAnswer.selectionEnd
    } : null;
    $('#jobView').innerHTML = `
      ${detail.needs_attention ? attentionCard(id, detail.needs_attention) : ''}
      <article class="card job-hero"><div class="section-head tight"><div><span class="kicker">JOB #${Number(job.id)}</span><h2>${esc(job.goal)}</h2><span>${badge(job.status)} <span class="muted">· ${esc(projectFor(job.repository)?.name || job.repository)} · ${esc(job.branch)}</span></span></div><div class="job-actions">${jobControls(job)}</div></div><div class="job-flow"><div class="flow">${stages.map((item, index) => `${index ? '<span class="arrow">›</span>' : ''}<span class="stage ${item === stage ? 'active' : ''} ${index < activeIndex || job.status === 'complete' ? 'done' : ''}"><i>${index + 1}</i>${stageNames[item]}</span>${item === 'reviewer' && current?.status === 'changes_requested' ? '<span class="loop">↩ revision</span>' : ''}`).join('')}</div></div></article>
      ${current ? stepCard(current) : ''}
      ${operationsPanel(detail.operations)}
      <div class="section-head"><div class="section-title"><h2>Completed steps</h2><small>Durable checkpoints already accepted</small></div><span>${detail.steps.filter(step => step.status === 'complete').length} complete</span></div>
      <div class="card timeline">${detail.steps.filter(step => step.status === 'complete').map(stepTimeline).join('') || '<div class="empty"><div><strong>No completed steps yet</strong>The first checkpoint will appear here.</div></div>'}</div>`;
    if (activeHumanAnswer) {
      const restored = $('#humanAnswer');
      if (restored) {
        restored.focus({preventScroll: true});
        if (humanSelection) restored.setSelectionRange(humanSelection.start, humanSelection.end);
      }
    }
  } catch (error) { toast('Could not load job', error.message, true); }
}

function attentionCard(id, attention) {
  const context = attention.context && Object.keys(attention.context).length
    ? `<details><summary>Technical context</summary><pre class="code">${esc(JSON.stringify(attention.context, null, 2))}</pre></details>` : '';
  return `<article class="card attention"><div class="attention-head">${icon('alert')}<h3>Needs attention</h3></div><p><strong>${esc(attention.reason)}</strong></p><p>${esc(attention.question)}</p>${context}${attention.can_answer ? `<textarea id="humanAnswer" maxlength="10000" placeholder="Enter your decision or instructions">${esc(state.humanDraft)}</textarea><button class="primary" data-action="answer-job" data-job-id="${Number(id)}">Continue workflow <span>→</span></button>` : '<p class="muted">Fix the reported issue, then use Resume. The team will reassess the same workflow step.</p>'}</article>`;
}

function stepCard(step) {
  const review = step.reviews?.at(-1);
  const verification = step.verification_runs?.at(-1);
  const route = step.model_routes?.at(-1);
  const phase = step.phase_metrics?.at(-1);
  const issues = (step.review_issues || []).filter(issue => issue.status === 'open');
  const stale = step.stale_warning ? `<p class="stale-warning">${icon('alert')}${esc(step.stale_warning)}</p>` : '';
  const command = Array.isArray(step.commands?.at(-1)?.argv) ? step.commands.at(-1).argv.join(' ') : '';
  return `<div class="section-head"><div class="section-title"><h2>Current step</h2><small>Live implementation and quality evidence</small></div>${badge(step.status)}</div>${stale}<div class="step-summary"><article class="card"><span class="kicker">OBJECTIVE</span><h3>${esc(step.title)}</h3><p>${esc(step.objective)}</p><h4>Acceptance criteria</h4><ul class="criteria">${(step.acceptance_criteria || []).map(item => `<li>${esc(item)}</li>`).join('') || '<li>No criteria recorded</li>'}</ul><h4>Changed files</h4><div class="chips">${(step.files_changed || []).map(item => `<span class="chip">${esc(item)}</span>`).join('') || '<span class="muted">No changed files recorded</span>'}</div>${issues.length ? `<h4>Reviewer issues</h4><ul class="criteria">${issues.map(issue => `<li><strong>${esc(issue.severity)}</strong> · ${esc(issue.problem)}</li>`).join('')}</ul>` : ''}</article><article class="card"><span class="kicker">EXECUTION EVIDENCE</span><div class="facts"><div class="fact"><span>Attempt</span><strong>${Number(step.attempt_count || 0)}</strong></div><div class="fact"><span>Elapsed</span><strong>${age(step.started_at)}</strong></div><div class="fact"><span>Lease owner</span><strong>${esc(step.lease_owner || 'Unleased')}</strong></div><div class="fact"><span>Lease</span><strong>${step.stale ? 'Expired' : age(step.lease_expires_at || step.review_lease_expires_at || step.orchestrator_lease_expires_at)}</strong></div><div class="fact"><span>Worker heartbeat</span><strong>${step.heartbeat_at ? age(step.heartbeat_at) : '—'}</strong></div><div class="fact"><span>Model route</span><strong>${esc(route?.model || step.model_used || '—')} · ${esc(route?.provider || '—')}</strong></div><div class="fact"><span>Last model call</span><strong>${esc(age(route?.created_at))}</strong></div><div class="fact"><span>Reviewer</span>${badge(review?.verdict || 'waiting')}</div><div class="fact"><span>Verification</span>${badge(verification?.status || 'waiting')}</div><div class="fact"><span>Commands / tests</span><strong>${(step.commands || []).length}</strong></div><div class="fact"><span>Current command</span><span class="code">${esc(command || '—')}</span></div><div class="fact"><span>Last phase</span><strong>${esc(phase ? `${phase.phase} · ${Number(phase.duration_seconds || 0).toFixed(1)}s` : '—')}</strong></div><div class="fact"><span>Prompt size</span><strong>${phase ? `${Number(phase.prompt_chars || 0).toLocaleString()} chars` : '—'}</strong></div><div class="fact"><span>Resulting commit</span><span class="code">${esc((step.resulting_commit || '—').slice(0, 12))}</span></div></div></article></div>`;
}

function stepTimeline(step) {
  return `<article><h3>${esc(step.title)} ${badge(step.status)}</h3><p>${esc(step.objective)}</p><span class="code">${esc(step.resulting_commit || '')}</span></article>`;
}

async function jobAction(action, button) {
  const jobId = Number(button?.dataset.jobId || state.jobId);
  const confirmation = action === 'remove'
    ? 'Remove this job from the queue? It will stop running and be kept only in the audit history.'
    : 'Cancel this job? The team will stop claiming new work.';
  if (['cancel', 'remove'].includes(action) && !confirm(confirmation)) return;
  setBusy(button, true);
  try {
    await api(`/api/jobs/${jobId}/${action}`, {method: 'POST', body: JSON.stringify({confirm: ['cancel', 'remove'].includes(action)})});
    const labels = {pause: 'paused', resume: 'resumed', cancel: 'cancelled', remove: 'removed', retry: 'requeued', 'recover-lease': 'recovered', 'rerun-verification': 'sent to verification', 'refresh-planning': 'returned to planning'};
    toast(`Job ${labels[action] || action}`, `Job #${jobId} was updated.`);
    state.overview = await api('/api/overview');
    state.humanDraft = '';
    if (action === 'remove') {
      if (state.jobId === jobId) state.jobId = null;
      if (state.view === 'job') showView('jobs');
      else render();
      return;
    }
    await renderJob(jobId);
  } catch (error) { toast('Job action failed', error.message, true); }
  finally { setBusy(button, false); }
}

async function answerJob(id, button) {
  const answer = $('#humanAnswer')?.value.trim();
  if (!answer) return toast('An answer is required', 'Tell the team how to continue.', true);
  setBusy(button, true, 'Saving…');
  try {
    await api(`/api/jobs/${id}/answer`, {method: 'POST', body: JSON.stringify({answer})});
    toast('Decision recorded', 'The same workflow step has been returned to EngineeringAgent.');
    state.overview = await api('/api/overview');
    await renderJob(id);
  } catch (error) { toast('Could not save your answer', error.message, true); }
  finally { setBusy(button, false); }
}

function populateProjects(selected) {
  $('#project').innerHTML = state.projects.filter(project => project.available).map(project => `<option value="${esc(project.id)}" ${project.id === selected ? 'selected' : ''}>${esc(project.name)} — ${esc(project.branch)}</option>`).join('');
}

function newJob(projectId = '') {
  if (!state.csrfToken) return;
  populateProjects(projectId);
  $('#jobDialog').showModal();
  setTimeout(() => $('#goal').focus(), 50);
}

async function submitJob(event) {
  event.preventDefault();
  const button = event.submitter || $('#jobForm button[type="submit"]');
  setBusy(button, true, 'Launching…');
  try {
    const result = await api('/api/jobs', {method: 'POST', body: JSON.stringify({project_id: $('#project').value, goal: $('#goal').value, priority: Number($('#priority').value), max_iterations: Number($('#maxIterations').value)})});
    $('#jobDialog').close();
    $('#jobForm').reset();
    toast('Job launched', `The team is preparing job #${result.job_id}.`);
    state.overview = await api('/api/overview');
    await openJob(result.job_id);
  } catch (error) { toast('Could not create job', error.message, true); }
  finally { setBusy(button, false); }
}

function updateClock() {
  const now = new Date();
  $('#clock').textContent = `${now.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}\n${now.toLocaleDateString([], {month: 'short', day: 'numeric'})}`;
}

document.addEventListener('click', event => {
  const source = event.target;
  const target = source && typeof source.closest === 'function'
    ? source.closest('[data-action], nav button, [data-close]') : null;
  if (!target) return;
  if (target.matches('nav button')) return showView(target.dataset.view);
  if (target.hasAttribute('data-close')) return $('#jobDialog').close();
  const action = target.dataset.action;
  if (action === 'new-job') return newJob(target.dataset.projectId);
  if (action === 'open-job') return openJob(target.dataset.jobId);
  if (action === 'open-mission') return openMission(target.dataset.missionId);
  if (action === 'back-missions') return renderMissions();
  if (action === 'mission-action') {
    event.preventDefault();
    return void missionAction(target.dataset.missionAction, target);
  }
  if (action === 'event-filter') { state.eventFilter = target.dataset.filter; return loadEvents(); }
  if (action === 'job-action') {
    event.preventDefault();
    event.stopPropagation();
    return void jobAction(target.dataset.jobAction, target);
  }
  if (action === 'answer-job') return answerJob(Number(target.dataset.jobId), target);
  if (action === 'worktree-cleanup') return cleanupWorktrees(target);
  if (action === 'security-audit') return securityAudit(target);
});
document.addEventListener('input', event => { if (event.target.id === 'humanAnswer') state.humanDraft = event.target.value; });

async function cleanupWorktrees(button) {
  if (!confirm('Remove only worktrees that are no longer referenced by active packages?')) return;
  setBusy(button, true, 'Cleaning…');
  try {
    const result = await api('/api/worktrees/cleanup', {method: 'POST', body: JSON.stringify({confirm: true})});
    toast('Worktrees reconciled', `${(result.detail?.removed || []).length} orphaned worktrees removed.`);
    state.overview = await api('/api/overview');
    render();
  } catch (error) { toast('Worktree cleanup failed', error.message, true); }
  finally { setBusy(button, false); }
}

$('#connect').addEventListener('click', connect);
$('#disconnect').addEventListener('click', disconnect);
$('#password').addEventListener('keydown', event => { if (event.key === 'Enter') connect(); });
$('#newJobButton').addEventListener('click', () => newJob());
$('#jobForm').addEventListener('submit', submitJob);
document.addEventListener('submit', event => {
  if (event.target.id !== 'eventSearch') return;
  event.preventDefault();
  const form = new FormData(event.target);
  state.eventSearch = Object.fromEntries([...form].filter(([, value]) => String(value).trim()));
  loadEvents();
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && $('#jobDialog').open) $('#jobDialog').close();
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k' && state.csrfToken) { event.preventDefault(); newJob(); }
});

updateClock();
setInterval(updateClock, 30_000);

async function restoreSession() {
  try {
    const session = await api('/api/session');
    if (!session.authenticated) return;
    state.csrfToken = session.csrf_token;
    [state.overview, state.projects, state.telemetry, state.telemetryHistory] = await Promise.all([api('/api/overview'), api('/api/projects'), api('/api/telemetry'), api('/api/telemetry/history?hours=6')]);
    document.body.classList.add('connected'); $('#login').hidden = true; $('#app').hidden = false;
    populateProjects(); showView('dashboard'); stream(++state.streamGeneration); startTelemetry(state.streamGeneration);
  } catch (_) { /* no existing session */ }
}
restoreSession();
