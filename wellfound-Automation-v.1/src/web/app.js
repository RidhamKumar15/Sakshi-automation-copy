// Genesis Job Automation Dashboard Client

let currentFilter = 'all';
let currentSearch = '';
let allApplications = [];
let isAutomationRunning = false;
let eventSource = null;
let audioCtx = null;
let lastApplicationsSignature = '';

// Pagination State
let currentPage = 1;
let pageSize = 25;

// Live Terminal Buffer State (Bounded Rolling Window)
const MAX_TERMINAL_LINES = 200;
let terminalLines = [];
let pendingLogBatch = [];
let isLogFlushScheduled = false;
let autoScroll = true;

// Initialize Web Audio API & Notification System
function getAudioContext() {
  if (!audioCtx) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (AudioContextClass) {
      audioCtx = new AudioContextClass();
    }
  }
  if (audioCtx && audioCtx.state === 'suspended') {
    audioCtx.resume();
  }
  return audioCtx;
}

function playWebChime(type = 'success') {
  try {
    const ctx = getAudioContext();
    if (!ctx) return;

    const now = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();

    osc.connect(gain);
    gain.connect(ctx.destination);

    if (type === 'success') {
      // Pleasant Ascending Major Chord Arpeggio (C5 -> E5 -> G5 -> C6)
      osc.type = 'sine';
      osc.frequency.setValueAtTime(523.25, now);
      osc.frequency.setValueAtTime(659.25, now + 0.08);
      osc.frequency.setValueAtTime(783.99, now + 0.16);
      osc.frequency.setValueAtTime(1046.50, now + 0.24);
      gain.gain.setValueAtTime(0.2, now);
      gain.gain.exponentialRampToValueAtTime(0.001, now + 0.7);
      osc.start(now);
      osc.stop(now + 0.7);
    } else if (type === 'error' || type === 'failed') {
      // Warning Double Tone
      osc.type = 'triangle';
      osc.frequency.setValueAtTime(340, now);
      osc.frequency.setValueAtTime(240, now + 0.14);
      gain.gain.setValueAtTime(0.25, now);
      gain.gain.exponentialRampToValueAtTime(0.001, now + 0.5);
      osc.start(now);
      osc.stop(now + 0.5);
    } else if (type === 'stopped') {
      // Descending Halt Tone
      osc.type = 'sine';
      osc.frequency.setValueAtTime(440, now);
      osc.frequency.linearRampToValueAtTime(220, now + 0.3);
      gain.gain.setValueAtTime(0.2, now);
      gain.gain.exponentialRampToValueAtTime(0.001, now + 0.35);
      osc.start(now);
      osc.stop(now + 0.35);
    } else {
      // Action Required / Attention Ping
      osc.type = 'sine';
      osc.frequency.setValueAtTime(587.33, now);
      osc.frequency.setValueAtTime(880.00, now + 0.12);
      gain.gain.setValueAtTime(0.22, now);
      gain.gain.exponentialRampToValueAtTime(0.001, now + 0.5);
      osc.start(now);
      osc.stop(now + 0.5);
    }
  } catch (e) {
    console.warn('Audio chime playback error:', e);
  }
}

function requestNotificationPermission() {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }
}

function sendBrowserNotification(title, body, type = 'success') {
  playWebChime(type);
  if ('Notification' in window && Notification.permission === 'granted') {
    try {
      new Notification(title, {
        body: body,
        icon: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">🎯</text></svg>'
      });
    } catch (e) {
      console.warn('Browser notification error:', e);
    }
  }
}

document.addEventListener('DOMContentLoaded', () => {
  initLiveLogs();
  loadStats();
  loadApplications();
  setupEventListeners();

  // Enable audio context & notification permissions on first user click
  document.addEventListener('click', () => {
    getAudioContext();
    requestNotificationPermission();
  }, { once: true });

  // Poll status, stats & application records every 2 seconds
  setInterval(() => {
    checkStatus();
    loadStats();
    loadApplications();
  }, 2000);
});

// Setup Control Listeners
function setupEventListeners() {
  const startBtn = document.getElementById('startBtn');
  const applyWaitingBtn = document.getElementById('applyWaitingBtn');
  const stopBtn = document.getElementById('stopBtn');
  const clearLogsBtn = document.getElementById('clearLogsBtn');
  const copyLogsBtn = document.getElementById('copyLogsBtn');
  const autoScrollToggleBtn = document.getElementById('autoScrollToggleBtn');
  const searchInput = document.getElementById('tableSearchInput');
  const pageSizeSelect = document.getElementById('pageSizeSelect');
  const modeSelect = document.getElementById('modeSelect');
  const limitInput = document.getElementById('limitInput');

  modeSelect.addEventListener('change', () => {
    if (modeSelect.value === 'test') {
      limitInput.value = '1';
    } else {
      limitInput.value = '10';
    }
  });

  startBtn.addEventListener('click', () => {
    requestNotificationPermission();
    startAutomation();
  });

  if (applyWaitingBtn) {
    applyWaitingBtn.addEventListener('click', () => {
      requestNotificationPermission();
      applyAllWaiting();
    });
  }

  stopBtn.addEventListener('click', stopAutomation);

  // Terminal Clear
  clearLogsBtn.addEventListener('click', () => {
    terminalLines = [];
    pendingLogBatch = [];
    const terminalBody = document.getElementById('terminalBody');
    if (terminalBody) {
      terminalBody.textContent = '';
    }
    updateTerminalBadge();
  });

  // Terminal Copy
  copyLogsBtn.addEventListener('click', () => {
    const text = terminalLines.join('\n');
    navigator.clipboard.writeText(text).then(() => {
      copyLogsBtn.innerText = 'Copied!';
      setTimeout(() => { copyLogsBtn.innerText = 'Copy Output'; }, 1500);
    });
  });

  // Auto-scroll toggle
  if (autoScrollToggleBtn) {
    autoScrollToggleBtn.addEventListener('click', () => {
      autoScroll = !autoScroll;
      if (autoScroll) {
        autoScrollToggleBtn.innerText = 'Auto-scroll: ON';
        autoScrollToggleBtn.classList.add('active');
        const terminalBody = document.getElementById('terminalBody');
        if (terminalBody) terminalBody.scrollTop = terminalBody.scrollHeight;
      } else {
        autoScrollToggleBtn.innerText = 'Auto-scroll: OFF';
        autoScrollToggleBtn.classList.remove('active');
      }
    });
  }

  // Search input filter
  searchInput.addEventListener('input', () => {
    currentSearch = searchInput.value.toLowerCase().trim();
    currentPage = 1;
    renderTable();
  });

  // Page Size Selector
  if (pageSizeSelect) {
    pageSizeSelect.addEventListener('change', () => {
      pageSize = parseInt(pageSizeSelect.value, 10) || 25;
      currentPage = 1;
      renderTable();
    });
  }

  // Filter Chips
  document.querySelectorAll('.filter-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      currentFilter = chip.getAttribute('data-filter');
      currentPage = 1;
      renderTable();
    });
  });
}

// Start Automation
async function startAutomation() {
  const platform = document.getElementById('platformSelect').value;
  const mode = document.getElementById('modeSelect').value;
  const limit = parseInt(document.getElementById('limitInput').value, 10) || 1;
  const keywords = document.getElementById('keywordsInput').value.trim();

  const payload = {
    source: platform,
    test_mode: mode === 'test',
    limit: limit,
    keywords: keywords ? [keywords] : []
  };

  try {
    const res = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    const data = await res.json();
    if (data.status === 'started' || data.status === 'ok') {
      setRunningState(true);
      appendLog(`[Dashboard] 🚀 Automation run initiated (Platform: ${platform}, Mode: ${mode}, Limit: ${limit})...`);
    } else {
      appendLog(`[Dashboard] ⚠️ Could not start: ${data.message || 'Unknown error'}`);
    }
  } catch (err) {
    appendLog(`[Dashboard] ❌ Error launching automation: ${err.message}`);
  }
}

// Batch Apply to All Waiting Jobs
async function applyAllWaiting() {
  const limitInput = document.getElementById('limitInput');
  const limit = parseInt(limitInput.value, 10) || null;

  try {
    const res = await fetch('/api/apply-waiting', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ limit: limit })
    });

    const data = await res.json();
    if (data.status === 'started') {
      setRunningState(true);
      appendLog(`[Dashboard] ⚡ Batch Application initiated for all waiting jobs...`);
    } else {
      appendLog(`[Dashboard] ⚠️ Could not start batch apply: ${data.message || 'Unknown error'}`);
    }
  } catch (err) {
    appendLog(`[Dashboard] ❌ Error initiating batch apply: ${err.message}`);
  }
}

// Apply to Single Job
async function applySingleJob(jobUrl, btnEl) {
  if (!jobUrl) return;
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.innerText = 'Applying...';
  }

  try {
    const res = await fetch('/api/apply-single', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_url: jobUrl })
    });

    const data = await res.json();
    if (data.status === 'started') {
      setRunningState(true);
      appendLog(`[Dashboard] 🚀 Direct apply initiated for: ${jobUrl}...`);
    } else {
      appendLog(`[Dashboard] ⚠️ Direct apply error: ${data.message || 'Unknown error'}`);
      if (btnEl) {
        btnEl.disabled = false;
        btnEl.innerText = '⚡ Apply';
      }
    }
  } catch (err) {
    appendLog(`[Dashboard] ❌ Direct apply failed: ${err.message}`);
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.innerText = '⚡ Apply';
    }
  }
}

// Stop Automation
async function stopAutomation() {
  try {
    const res = await fetch('/api/stop', { method: 'POST' });
    const data = await res.json();
    appendLog(`[Dashboard] 🛑 Stop requested. Process halting...`);
    setRunningState(false);
  } catch (err) {
    appendLog(`[Dashboard] ⚠️ Error stopping automation: ${err.message}`);
  }
}

// Check Backend Status
async function checkStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    setRunningState(data.is_running);
  } catch (e) {
    // Server might be busy
  }
}

function setRunningState(running) {
  isAutomationRunning = running;
  const statusBadge = document.getElementById('liveStatusBadge');
  const statusText = document.getElementById('statusText');
  const startBtn = document.getElementById('startBtn');
  const applyWaitingBtn = document.getElementById('applyWaitingBtn');
  const stopBtn = document.getElementById('stopBtn');

  if (running) {
    statusBadge.className = 'live-indicator running';
    statusText.innerText = 'Active Running';
    startBtn.disabled = true;
    if (applyWaitingBtn) applyWaitingBtn.disabled = true;
    stopBtn.disabled = false;
  } else {
    statusBadge.className = 'live-indicator';
    statusText.innerText = 'Idle';
    startBtn.disabled = false;
    if (applyWaitingBtn) applyWaitingBtn.disabled = false;
    stopBtn.disabled = true;
  }
}

// Live SSE Log Stream with Bounded Rolling Buffer
function initLiveLogs() {
  if (eventSource) {
    eventSource.close();
  }

  // Pre-seed initial ready message
  terminalLines = ["[Genesis Engine] Ready. Choose options above and click 'Start Automation' to launch."];
  renderTerminalBuffer();

  eventSource = new EventSource('/api/logs/stream');
  eventSource.onmessage = (e) => {
    if (e.data) {
      appendLog(e.data);
    }
  };

  eventSource.onerror = () => {
    // Reconnects automatically
  };
}

function appendLog(text) {
  const lines = text.split('\n');
  for (const line of lines) {
    const cleanLine = line.trim();
    if (cleanLine) {
      pendingLogBatch.push(cleanLine);
      processNotificationTriggers(cleanLine);
    }
  }

  // Throttle and batch DOM updates using requestAnimationFrame
  if (!isLogFlushScheduled) {
    isLogFlushScheduled = true;
    requestAnimationFrame(flushPendingLogs);
  }
}

function flushPendingLogs() {
  isLogFlushScheduled = false;
  if (pendingLogBatch.length === 0) return;

  for (const line of pendingLogBatch) {
    terminalLines.push(line);
  }
  pendingLogBatch = [];

  // Bounded buffer: trim oldest lines if exceeding threshold
  if (terminalLines.length > MAX_TERMINAL_LINES) {
    terminalLines = terminalLines.slice(-MAX_TERMINAL_LINES);
  }

  renderTerminalBuffer();
}

function renderTerminalBuffer() {
  const terminal = document.getElementById('terminalBody');
  if (!terminal) return;

  terminal.textContent = terminalLines.join('\n');
  if (autoScroll) {
    terminal.scrollTop = terminal.scrollHeight;
  }
  updateTerminalBadge();
}

function updateTerminalBadge() {
  const badge = document.getElementById('terminalBufferBadge');
  if (badge) {
    badge.innerText = `Buffer: ${terminalLines.length} / ${MAX_TERMINAL_LINES} lines`;
  }
}

function processNotificationTriggers(clean) {
  if (clean.includes('[Genesis] 🏁 Automation execution finished') || clean.includes('[Genesis] 🏁 Batch apply finished')) {
    sendBrowserNotification('🎯 Genesis Automation Finished', 'The application process has finished successfully!', 'success');
  } else if (clean.includes('[Genesis] 🛑 Automation task was cancelled') || clean.includes('[Genesis] 🛑 Application task was cancelled') || clean.includes('Stop signal dispatched')) {
    sendBrowserNotification('🛑 Genesis Automation Stopped', 'The application process was halted.', 'stopped');
  } else if (clean.includes('[Genesis] ❌ Error during')) {
    sendBrowserNotification('❌ Genesis Automation Error', clean.replace('[Genesis] ❌', '').trim(), 'error');
  } else if (clean.includes('[Truth Check Passed]: Verified live on Wellfound!')) {
    sendBrowserNotification('✅ Application Verified & Submitted', 'Application was officially submitted on Wellfound!', 'success');
  } else if (clean.includes('Action Required') || clean.includes('[Truth Check Failed]')) {
    sendBrowserNotification('⚠️ Action Required', 'An application requires your manual confirmation.', 'warning');
  }
}

// Load Stats
async function loadStats() {
  try {
    const res = await fetch('/api/stats');
    const stats = await res.json();

    document.getElementById('statTotal').innerText = stats.total_evaluated || 0;
    document.getElementById('statSubmitted').innerText = stats.submitted || 0;
    document.getElementById('statReview').innerText = stats.under_review || 0;
    document.getElementById('statAvgScore').innerText = (stats.average_score || 0).toFixed(1);

    const waitingBadge = document.getElementById('waitingCountBadge');
    if (waitingBadge) {
      waitingBadge.innerText = (stats.waiting_for_input || 0) + (stats.under_review || 0);
    }
  } catch (err) {
    console.error('Error fetching stats:', err);
  }
}

// Load Applications with Differential Fingerprint Check
async function loadApplications() {
  try {
    const res = await fetch('/api/applications');
    const data = await res.json();

    // Compute simple signature to avoid DOM re-render if data is identical
    const signature = data.length > 0
      ? `${data.length}_${data[0].date || ''}_${data[0].status || ''}_${data[data.length - 1].key || ''}`
      : 'empty';

    if (signature !== lastApplicationsSignature || allApplications.length !== data.length) {
      lastApplicationsSignature = signature;
      allApplications = data;
      renderTable();
    }
  } catch (err) {
    console.error('Error loading applications:', err);
  }
}

// Render Table with Dynamic Genesis Pagination
function renderTable() {
  const tbody = document.getElementById('applicationsTableBody');
  const countBadge = document.getElementById('recordCountText');
  const infoText = document.getElementById('paginationInfoText');
  const navContainer = document.getElementById('paginationNav');

  const filtered = allApplications.filter(app => {
    // Status Filter
    if (currentFilter !== 'all') {
      if (currentFilter === 'Submitted' && app.status !== 'Submitted') return false;
      if (currentFilter === 'Under Review' && app.status !== 'Under Review') return false;
      if (currentFilter === 'Waiting for Input' && app.status !== 'Waiting for Input') return false;
      if (currentFilter === 'Not Accepting' && app.status !== 'Not Accepting') return false;
      if (currentFilter === 'Skipped' && app.status !== 'Skipped' && app.status !== 'Closed') return false;
    }

    // Search Filter
    if (currentSearch) {
      const matchComp = (app.company || '').toLowerCase().includes(currentSearch);
      const matchRole = (app.role || '').toLowerCase().includes(currentSearch);
      const matchNotes = (app.notes || '').toLowerCase().includes(currentSearch);
      return matchComp || matchRole || matchNotes;
    }

    return true;
  });

  const totalFiltered = filtered.length;
  const totalPages = Math.max(1, Math.ceil(totalFiltered / pageSize));

  // Clamp current page
  if (currentPage > totalPages) {
    currentPage = totalPages;
  }
  if (currentPage < 1) {
    currentPage = 1;
  }

  // Calculate slice
  const startIndex = (currentPage - 1) * pageSize;
  const endIndex = Math.min(startIndex + pageSize, totalFiltered);
  const pageSlice = totalFiltered > 0 ? filtered.slice(startIndex, endIndex) : [];

  // Update Header Count Badge
  if (countBadge) {
    countBadge.innerText = `Showing ${totalFiltered} of ${allApplications.length} total records`;
  }

  // Update Pagination Info
  if (infoText) {
    if (totalFiltered === 0) {
      infoText.innerText = 'No matching records';
    } else {
      infoText.innerText = `Showing ${startIndex + 1}–${endIndex} of ${totalFiltered} records (Page ${currentPage} of ${totalPages})`;
    }
  }

  // Render Table Rows (Only the visible slice is added to DOM)
  if (pageSlice.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="8" style="text-align: center; padding: 40px; color: var(--text-secondary);">
          No application records matching current filter.
        </td>
      </tr>
    `;
  } else {
    tbody.innerHTML = pageSlice.map(app => {
      const score = parseFloat(app.match_score) || 0;
      let scoreClass = 'score-low';
      if (score >= 70) scoreClass = 'score-high';
      else if (score >= 55) scoreClass = 'score-mid';

      let statusPillClass = 'status-skipped';
      if (app.status === 'Submitted') statusPillClass = 'status-submitted';
      else if (app.status === 'Under Review') statusPillClass = 'status-review';
      else if (app.status === 'Waiting for Input' || app.status === 'Applying') statusPillClass = 'status-waiting';
      else if (app.status === 'Not Accepting') statusPillClass = 'status-not-accepting';
      else if (app.status === 'Failed' || app.status === 'Rejected') statusPillClass = 'status-failed';

      const url = app.job_url || '#';
      const isWaiting = (app.status === 'Waiting for Input' || app.status === 'Under Review' || app.status === 'Applying');

      return `
        <tr>
          <td style="font-family: var(--font-code); font-size: 12px; color: var(--text-secondary);">${app.date || '-'}</td>
          <td class="company-cell">${escapeHtml(app.company || 'Unknown')}</td>
          <td class="role-cell">${escapeHtml(app.role || 'Software Engineer')}</td>
          <td><span class="brand-badge">${escapeHtml(app.source || 'Wellfound')}</span></td>
          <td><span class="score-badge ${scoreClass}">${score.toFixed(1)}</span></td>
          <td><span class="status-pill ${statusPillClass}">${escapeHtml(app.status || 'Tracked')}</span></td>
          <td style="font-size: 12px; color: var(--text-secondary); max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(app.notes || '')}">
            ${escapeHtml(app.notes || '-')}
          </td>
          <td>
            <div style="display: inline-flex; align-items: center; gap: 8px;">
              ${isWaiting ? `
                <button onclick="applySingleJob('${escapeHtml(url)}', this)" class="btn" style="padding: 4px 10px; font-size: 11px; font-weight: 600; background-color: #6366F1; color: #FFFFFF; border: none; border-radius: 4px; cursor: pointer;">
                  ⚡ Apply
                </button>
              ` : ''}
              <a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" class="job-link">
                <span>View</span>
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
              </a>
            </div>
          </td>
        </tr>
      `;
    }).join('');
  }

  // Render Dynamic Pagination Controls
  renderPaginationControls(navContainer, totalPages);
}

function renderPaginationControls(container, totalPages) {
  if (!container) return;

  if (totalPages <= 1) {
    container.innerHTML = '';
    return;
  }

  let html = '';

  // First & Prev buttons
  const prevDisabled = currentPage <= 1 ? 'disabled' : '';
  html += `<button class="page-btn" ${prevDisabled} onclick="goToPage(1)" title="First Page">«</button>`;
  html += `<button class="page-btn" ${prevDisabled} onclick="goToPage(${currentPage - 1})" title="Previous Page">‹</button>`;

  // Smart page numbers with ellipsis
  const maxButtons = 5;
  let startPage = Math.max(1, currentPage - Math.floor(maxButtons / 2));
  let endPage = Math.min(totalPages, startPage + maxButtons - 1);

  if (endPage - startPage + 1 < maxButtons) {
    startPage = Math.max(1, endPage - maxButtons + 1);
  }

  if (startPage > 1) {
    html += `<button class="page-btn" onclick="goToPage(1)">1</button>`;
    if (startPage > 2) {
      html += `<span class="page-ellipsis">…</span>`;
    }
  }

  for (let p = startPage; p <= endPage; p++) {
    const activeClass = p === currentPage ? 'active' : '';
    html += `<button class="page-btn ${activeClass}" onclick="goToPage(${p})">${p}</button>`;
  }

  if (endPage < totalPages) {
    if (endPage < totalPages - 1) {
      html += `<span class="page-ellipsis">…</span>`;
    }
    html += `<button class="page-btn" onclick="goToPage(${totalPages})">${totalPages}</button>`;
  }

  // Next & Last buttons
  const nextDisabled = currentPage >= totalPages ? 'disabled' : '';
  html += `<button class="page-btn" ${nextDisabled} onclick="goToPage(${currentPage + 1})" title="Next Page">›</button>`;
  html += `<button class="page-btn" ${nextDisabled} onclick="goToPage(${totalPages})" title="Last Page">»</button>`;

  container.innerHTML = html;
}

function goToPage(page) {
  currentPage = page;
  renderTable();
  // Smooth scroll to top of table if scrolled down
  const tableCard = document.querySelector('.table-card');
  if (tableCard && tableCard.getBoundingClientRect().top < 0) {
    tableCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}
