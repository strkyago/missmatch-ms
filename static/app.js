/* ============================================================
   gdsync — Frontend JS
   ============================================================ */

'use strict';

// ---- State ----
let currentSession = null;
let currentJobId   = null;
let pollInterval   = null;

// ---- DOM refs ----
const $  = id => document.getElementById(id);
const sectionSession  = $('section-session');
const sectionSync     = $('section-sync');
const sectionProgress = $('section-progress');
const sessionsGrid    = $('sessions-grid');
const syncFlights     = $('sync-flights');
const logBox          = $('log-box');
const progressBar     = $('progress-bar');

// ---- Boot ----
(async () => {
  await loadSessions();
  $('btn-back').addEventListener('click', showSessionPicker);
  $('btn-sync').addEventListener('click', submitSync);
  $('btn-new-session').addEventListener('click', () => location.reload());
})();

// ============================================================
// Sessions
// ============================================================

async function loadSessions() {
  try {
    const data = await api('/api/sessions');
    $('base-dir-label').textContent = shortenPath(data.base_dir);

    if (!data.sessions.length) {
      sessionsGrid.innerHTML = `
        <div class="empty-state">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
          <p>No sessions found in this directory.<br>Add a folder containing <code>gopro.mp4</code> and <code>drone_*.mp4</code>.</p>
        </div>`;
      return;
    }

    sessionsGrid.innerHTML = '';
    for (const name of data.sessions) {
      const card = document.createElement('div');
      card.className = 'session-card fade-in';
      card.id = `session-${slugify(name)}`;
      card.innerHTML = `
        <div class="session-name">${esc(name)}</div>
        <div class="session-meta">Click to configure sync</div>
        <svg class="session-arrow" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>`;
      card.addEventListener('click', () => selectSession(name));
      sessionsGrid.appendChild(card);
    }
  } catch (err) {
    sessionsGrid.innerHTML = `<div class="empty-state"><p>Error loading sessions: ${esc(err.message)}</p></div>`;
  }
}

async function selectSession(name) {
  try {
    const data = await api(`/api/session/${encodeURIComponent(name)}`);
    currentSession = data;
    renderSyncForm(data);
    show(sectionSync);
    hide(sectionSession);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (err) {
    alert(`Could not load session: ${err.message}`);
  }
}

// ============================================================
// Sync form
// ============================================================

function renderSyncForm(data) {
  $('sync-subtitle').textContent = `Session: ${data.name}`;
  $('gopro-filename').textContent = data.gopro.name;
  $('gopro-duration').textContent = data.gopro.duration_fmt;
  $('drone-count').textContent    = data.drone_files.length;

  syncFlights.innerHTML = '';
  for (let i = 0; i < data.drone_files.length; i++) {
    const df = data.drone_files[i];
    const ex = data.existing_syncs[df.name];
    const row = document.createElement('div');
    row.className = 'flight-row fade-in';
    row.innerHTML = `
      <div class="flight-header">
        <span class="flight-badge">Flight ${i + 1}</span>
        <span class="flight-filename">${esc(df.name)}</span>
        <span class="flight-duration">${esc(df.duration_fmt)}</span>
      </div>
      <div class="fields-row">
        <div class="field-group">
          <label class="field-label" for="gopro-marker-${i}">
            GoPro marker <span class="chip chip-gopro">GoPro</span>
          </label>
          <input
            class="field-input"
            id="gopro-marker-${i}"
            type="text"
            placeholder="e.g. 1:33.5"
            value="${ex ? fmtSeconds(ex.gopro_marker) : ''}"
            data-flight="${i}"
            data-type="gopro"
          />
          <span class="field-hint">Timestamp in gopro.mp4 when the marker card appears</span>
        </div>
        <div class="field-group">
          <label class="field-label" for="drone-marker-${i}">
            Drone marker <span class="chip chip-drone">Drone</span>
          </label>
          <input
            class="field-input"
            id="drone-marker-${i}"
            type="text"
            placeholder="e.g. 0:12.0"
            value="${ex ? fmtSeconds(ex.drone_marker) : ''}"
            data-flight="${i}"
            data-type="drone"
          />
          <span class="field-hint">Timestamp in ${esc(df.name)} when the marker appears</span>
        </div>
      </div>`;
    syncFlights.appendChild(row);
  }
}

function showSessionPicker() {
  hide(sectionSync);
  show(sectionSession);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// ============================================================
// Submit sync
// ============================================================

async function submitSync() {
  const data = currentSession;
  const syncs = [];
  let valid = true;

  for (let i = 0; i < data.drone_files.length; i++) {
    const df   = data.drone_files[i];
    const gEl  = $(`gopro-marker-${i}`);
    const dEl  = $(`drone-marker-${i}`);
    const gVal = gEl.value.trim();
    const dVal = dEl.value.trim();

    gEl.classList.remove('error');
    dEl.classList.remove('error');

    const gSec = parseTimestamp(gVal);
    const dSec = parseTimestamp(dVal);

    if (gSec === null) { gEl.classList.add('error'); valid = false; }
    if (dSec === null) { dEl.classList.add('error'); valid = false; }
    if (dSec !== null && (dSec < 0 || dSec > df.duration)) {
      dEl.classList.add('error'); valid = false;
    }

    if (valid) {
      syncs.push({ drone_file: df.name, gopro_marker: gSec, drone_marker: dSec });
    }
  }

  if (!valid) {
    shakeInvalid();
    return;
  }

  const height = parseInt($('output-height').value, 10);

  try {
    $('btn-sync').disabled = true;
    const result = await api('/api/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session: data.name, syncs, height }),
    });

    currentJobId = result.job_id;
    showProgress();
    startPolling();
  } catch (err) {
    $('btn-sync').disabled = false;
    alert(`Error starting sync: ${err.message}`);
  }
}

// ============================================================
// Progress & polling
// ============================================================

function showProgress() {
  hide(sectionSync);
  show(sectionProgress);
  progressBar.style.width = '5%';
  logBox.innerHTML = '';
  $('outputs-section').classList.add('hidden');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function startPolling() {
  let logCount = 0;
  pollInterval = setInterval(async () => {
    try {
      const job = await api(`/api/job/${currentJobId}`);

      // Append new log lines
      if (job.logs && job.logs.length > logCount) {
        const newLines = job.logs.slice(logCount);
        for (const line of newLines) {
          const span = document.createElement('span');
          span.className = 'log-line';
          span.textContent = line;
          logBox.appendChild(span);
        }
        logCount = job.logs.length;
        logBox.scrollTop = logBox.scrollHeight;

        // Rough progress estimate from log line count
        const estimatedTotal = (currentSession.drone_files.length * 3) + 4;
        const pct = Math.min(95, Math.round((logCount / estimatedTotal) * 100));
        progressBar.style.width = pct + '%';
      }

      if (job.status === 'done') {
        clearInterval(pollInterval);
        progressBar.style.width = '100%';
        $('step-badge-3')?.classList?.remove('active');
        $('progress-subtitle').textContent = 'All done! Your files are ready.';

        // Show output cards
        $('dl-gopro').href = job.outputs.gopro;
        $('dl-drone').href = job.outputs.drone;
        $('outputs-section').classList.remove('hidden');

        const doneLog = document.createElement('span');
        doneLog.className = 'log-line ok';
        doneLog.textContent = '✓ Render complete.';
        logBox.appendChild(doneLog);
        logBox.scrollTop = logBox.scrollHeight;
      }

      if (job.status === 'error') {
        clearInterval(pollInterval);
        $('progress-subtitle').textContent = 'An error occurred.';
        progressBar.style.background = 'var(--gopro)';
        const errLog = document.createElement('span');
        errLog.className = 'log-line err';
        errLog.textContent = '✗ ' + (job.error || 'Unknown error');
        logBox.appendChild(errLog);
        logBox.scrollTop = logBox.scrollHeight;
      }
    } catch (e) {
      // Network blip — keep polling
    }
  }, 1000);
}

// ============================================================
// Helpers
// ============================================================

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

function parseTimestamp(s) {
  if (!s) return null;
  // Accept: 93.5 | 1:33.5 | 1:02:33.5
  const sec = /^(\d+(?:\.\d+)?)$/.exec(s);
  if (sec) return parseFloat(sec[1]);
  const hms = /^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$/.exec(s);
  if (hms) {
    const h  = hms[1] ? parseInt(hms[1], 10) : 0;
    const m  = parseInt(hms[2], 10);
    const ss = parseFloat(hms[3]);
    if (m >= 60 || ss >= 60) return null;
    return h * 3600 + m * 60 + ss;
  }
  return null;
}

function fmtSeconds(t) {
  const h  = Math.floor(t / 3600);
  const m  = Math.floor((t % 3600) / 60);
  const s  = (t - h * 3600 - m * 60).toFixed(1);
  if (h) return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(4,'0')}`;
  return `${m}:${String(s).padStart(4,'0')}`;
}

function shortenPath(p) {
  const home = p.match(/\/Users\/([^/]+)\//);
  if (home) return p.replace(`/Users/${home[1]}`, '~');
  return p;
}

function esc(s) {
  return String(s)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

function slugify(s) {
  return s.toLowerCase().replace(/[^a-z0-9]/g, '-');
}

function show(el) { el.classList.remove('hidden'); }
function hide(el) { el.classList.add('hidden'); }

function shakeInvalid() {
  const btn = $('btn-sync');
  btn.style.animation = 'none';
  btn.offsetHeight; // reflow
  btn.style.animation = 'shake 0.4s ease';
  setTimeout(() => { btn.style.animation = ''; btn.disabled = false; }, 400);
}

// inject shake keyframes
const style = document.createElement('style');
style.textContent = `
  @keyframes shake {
    0%,100% { transform: translateX(0); }
    20%      { transform: translateX(-6px); }
    40%      { transform: translateX(6px); }
    60%      { transform: translateX(-4px); }
    80%      { transform: translateX(4px); }
  }`;
document.head.appendChild(style);
