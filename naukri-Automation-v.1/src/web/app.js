// Genesis Naukri Automation Dashboard Client

let currentFilter = 'all';
let allApplications = [];
let lastApplicationsJson = '';
let isAutomationRunning = false;
let eventSource = null;
let currentPresets = {};
let unresolvedQuestions = [];

// Pagination State
let currentPage = 1;
let pageSize = 25;

// Console Log Ring-Buffer (capped to prevent memory growth & tab crash)
const MAX_CONSOLE_LINES = 150;
let consoleLogLines = [];

document.addEventListener('DOMContentLoaded', () => {
  initLiveLogs();
  initTabNavigation();
  loadStats();
  loadApplications();
  loadUnresolvedQuestions();
  loadPresets();
  setupEventListeners();
  checkBrowserStatus();
  requestNotificationPermission();

  // Poll status, stats, and records every 2.5 seconds
  setInterval(() => {
    checkStatus();
    checkBrowserStatus();
    loadStats();
    loadApplications();
    loadUnresolvedQuestions();
  }, 2500);
});

// Tab Navigation
function initTabNavigation() {
  const tabs = [
    { btn: document.getElementById('tabBtnTracker'), view: document.getElementById('tabViewTracker') },
    { btn: document.getElementById('tabBtnQuestions'), view: document.getElementById('tabViewQuestions'), onLoad: () => loadUnresolvedQuestions(true) },
    { btn: document.getElementById('tabBtnPresets'), view: document.getElementById('tabViewPresets'), onLoad: () => { loadPresets(); loadCandidateProfile(); } }
  ];

  tabs.forEach(t => {
    if (!t.btn || !t.view) return;
    t.btn.addEventListener('click', () => {
      tabs.forEach(other => {
        other.btn.classList.remove('active');
        other.view.style.display = 'none';
        other.view.classList.remove('active');
      });
      t.btn.classList.add('active');
      t.view.style.display = 'flex';
      t.view.classList.add('active');
      if (t.onLoad) t.onLoad();
    });
  });
}

// Setup Control Listeners
function setupEventListeners() {
  const startBtn = document.getElementById('startBtn');
  const stopBtn = document.getElementById('stopBtn');
  const clearLogsBtn = document.getElementById('clearLogsBtn');
  const copyLogsBtn = document.getElementById('copyLogsBtn');
  const searchInput = document.getElementById('tableSearchInput');
  const modeSelect = document.getElementById('modeSelect');
  const limitInput = document.getElementById('limitInput');
  const refreshQuestionsBtn = document.getElementById('refreshQuestionsBtn');
  const refreshPresetsBtn = document.getElementById('refreshPresetsBtn');
  const saveManualPresetBtn = document.getElementById('saveManualPresetBtn');
  const closeModalBtn = document.getElementById('closeModalBtn');
  const modalDoneBtn = document.getElementById('modalDoneBtn');
  const answersModal = document.getElementById('answersModal');

  modeSelect.addEventListener('change', () => {
    if (modeSelect.value === 'test') {
      limitInput.value = '1';
    } else {
      limitInput.value = '10';
    }
  });

  startBtn.addEventListener('click', startAutomation);
  stopBtn.addEventListener('click', stopAutomation);

  clearLogsBtn.addEventListener('click', () => {
    consoleLogLines = [];
    const terminal = document.getElementById('terminalBody');
    if (terminal) terminal.innerText = '[System] Console logs cleared.';
    updateConsoleLineBadge();
  });

  copyLogsBtn.addEventListener('click', () => {
    const text = consoleLogLines.join('\n') || (document.getElementById('terminalBody')?.innerText || '');
    navigator.clipboard.writeText(text).then(() => {
      copyLogsBtn.innerText = 'Copied!';
      setTimeout(() => { copyLogsBtn.innerText = 'Copy Output'; }, 1500);
    });
  });

  searchInput.addEventListener('input', () => {
    currentPage = 1;
    renderTable();
  });

  // Pagination Listeners
  const pageSizeSelect = document.getElementById('pageSizeSelect');
  if (pageSizeSelect) {
    pageSizeSelect.addEventListener('change', () => {
      pageSize = parseInt(pageSizeSelect.value, 10) || 25;
      currentPage = 1;
      renderTable();
    });
  }

  const btnFirst = document.getElementById('btnFirstPage');
  if (btnFirst) {
    btnFirst.addEventListener('click', () => {
      currentPage = 1;
      renderTable();
    });
  }

  const btnPrev = document.getElementById('btnPrevPage');
  if (btnPrev) {
    btnPrev.addEventListener('click', () => {
      if (currentPage > 1) {
        currentPage--;
        renderTable();
      }
    });
  }

  const btnNext = document.getElementById('btnNextPage');
  if (btnNext) {
    btnNext.addEventListener('click', () => {
      currentPage++;
      renderTable();
    });
  }

  const btnLast = document.getElementById('btnLastPage');
  if (btnLast) {
    btnLast.addEventListener('click', () => {
      currentPage = 999999;
      renderTable();
    });
  }

  if (refreshQuestionsBtn) {
    refreshQuestionsBtn.addEventListener('click', () => {
      loadUnresolvedQuestions();
      showToast('Refreshed questions list.');
    });
  }

  if (refreshPresetsBtn) {
    refreshPresetsBtn.addEventListener('click', () => {
      loadPresets();
      showToast('Reloaded central presets.');
    });
  }

  if (saveManualPresetBtn) {
    saveManualPresetBtn.addEventListener('click', handleSaveManualPreset);
  }

  // Modal Closers
  if (closeModalBtn) closeModalBtn.addEventListener('click', closeAnswersModal);
  if (modalDoneBtn) modalDoneBtn.addEventListener('click', closeAnswersModal);
  if (answersModal) {
    answersModal.addEventListener('click', (e) => {
      if (e.target === answersModal) closeAnswersModal();
    });
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && answersModal && answersModal.style.display !== 'none') {
      closeAnswersModal();
    }
  });

  const browserBadge = document.getElementById('browserStatusBadge');
  if (browserBadge) {
    browserBadge.addEventListener('click', () => {
      const text = document.getElementById('browserStatusText')?.innerText || '';
      if (!text.includes('Connected')) {
        triggerLaunchBrowser();
      }
    });
  }

  // Test Notification & Sound Button
  const testNotifBtn = document.getElementById('testNotificationBtn');
  if (testNotifBtn) {
    testNotifBtn.addEventListener('click', async () => {
      requestNotificationPermission();
      showWebNotification('🔔 Notification Alert', 'Sound & notification system is working!', 'success');
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
      appendLog(`\n[Dashboard] 🚀 Naukri automation run initiated (Mode: ${mode}, Limit: ${limit})...\n`);
      showToast('Automation launched successfully!');
    } else {
      appendLog(`\n[Dashboard] ⚠️ Could not start: ${data.message || 'Unknown error'}\n`);
      showToast(data.message || 'Could not start automation', 'error');
    }
  } catch (err) {
    appendLog(`\n[Dashboard] ❌ Error launching automation: ${err.message}\n`);
  }
}

// Stop Automation
async function stopAutomation() {
  try {
    const res = await fetch('/api/stop', { method: 'POST' });
    const data = await res.json();
    appendLog(`\n[Dashboard] 🛑 Stop requested. Process halting...\n`);
    setRunningState(false);
    showToast('Automation stop requested.');
  } catch (err) {
    appendLog(`\n[Dashboard] ⚠️ Error stopping automation: ${err.message}\n`);
  }
}

// Check Backend Status
async function checkStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    setRunningState(data.is_running);
  } catch (e) {}
}

function setRunningState(running) {
  isAutomationRunning = running;
  const statusBadge = document.getElementById('liveStatusBadge');
  const statusText = document.getElementById('statusText');
  const startBtn = document.getElementById('startBtn');
  const stopBtn = document.getElementById('stopBtn');

  if (running) {
    statusBadge.className = 'live-indicator running';
    statusText.innerText = 'Active Running';
    startBtn.disabled = true;
    stopBtn.disabled = false;
  } else {
    statusBadge.className = 'live-indicator';
    statusText.innerText = 'Idle';
    startBtn.disabled = false;
    stopBtn.disabled = true;
  }
}

// Check Chrome Browser CDP Status
async function checkBrowserStatus() {
  const browserBadge = document.getElementById('browserStatusBadge');
  const browserDot = document.getElementById('browserDot');
  const browserText = document.getElementById('browserStatusText');
  if (!browserBadge || !browserDot || !browserText) return;

  try {
    const res = await fetch('/api/browser/status');
    const data = await res.json();
    if (data.connected) {
      browserDot.style.background = '#10b981';
      browserDot.style.boxShadow = '0 0 8px rgba(16, 185, 129, 0.6)';
      browserText.innerText = `Chrome Connected (${data.profile_name || 'Sakshi'})`;
      browserBadge.title = `Chrome CDP is active on port ${data.port}. Direct tab control ready!`;
    } else {
      browserDot.style.background = '#f59e0b';
      browserDot.style.boxShadow = 'none';
      browserText.innerText = '🚀 Launch Chrome';
      browserBadge.title = 'Chrome is not connected on port 9222. Click to launch Chrome with remote debugging!';
    }
  } catch (e) {}
}

async function triggerLaunchBrowser() {
  const browserText = document.getElementById('browserStatusText');
  if (browserText) browserText.innerText = 'Launching Chrome...';
  try {
    const res = await fetch('/api/browser/launch', { method: 'POST' });
    const data = await res.json();
    appendLog(`\n[Browser] 🌐 ${data.message || 'Launching Chrome...'}\n`);
    setTimeout(checkBrowserStatus, 1500);
  } catch (err) {
    appendLog(`\n[Browser] ❌ Failed to launch Chrome: ${err.message}\n`);
  }
}

// Web Audio Synthesizer for instant, zero-latency audio alerts
function playWebSound(type = 'success') {
  try {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) {
      const audio = new Audio(`/static/sounds/${type}.wav`);
      audio.play().catch(() => {});
      return;
    }

    const audioCtx = new AudioContextClass();
    if (audioCtx.state === 'suspended') {
      audioCtx.resume();
    }

    if (type === 'success') {
      // Pleasant 3-note chime: D5 (587 Hz) -> A5 (880 Hz) -> D6 (1175 Hz)
      const notes = [587.33, 880.0, 1174.66];
      notes.forEach((freq, idx) => {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = 'sine';
        osc.frequency.setValueAtTime(freq, audioCtx.currentTime + idx * 0.12);
        gain.gain.setValueAtTime(0.001, audioCtx.currentTime + idx * 0.12);
        gain.gain.exponentialRampToValueAtTime(0.3, audioCtx.currentTime + idx * 0.12 + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + idx * 0.12 + 0.25);
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start(audioCtx.currentTime + idx * 0.12);
        osc.stop(audioCtx.currentTime + idx * 0.12 + 0.28);
      });
    } else if (type === 'warning') {
      // 2-tone alert chime: 880 Hz -> 659 Hz -> 880 Hz
      const notes = [880.0, 659.25, 880.0];
      notes.forEach((freq, idx) => {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = 'triangle';
        osc.frequency.setValueAtTime(freq, audioCtx.currentTime + idx * 0.15);
        gain.gain.setValueAtTime(0.001, audioCtx.currentTime + idx * 0.15);
        gain.gain.exponentialRampToValueAtTime(0.35, audioCtx.currentTime + idx * 0.15 + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + idx * 0.15 + 0.22);
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start(audioCtx.currentTime + idx * 0.15);
        osc.stop(audioCtx.currentTime + idx * 0.15 + 0.25);
      });
    } else if (type === 'error') {
      // Low alert chime: 440 Hz -> 349 Hz
      const notes = [440.0, 349.23];
      notes.forEach((freq, idx) => {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = 'sawtooth';
        osc.frequency.setValueAtTime(freq, audioCtx.currentTime + idx * 0.18);
        gain.gain.setValueAtTime(0.001, audioCtx.currentTime + idx * 0.18);
        gain.gain.exponentialRampToValueAtTime(0.2, audioCtx.currentTime + idx * 0.18 + 0.03);
        gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + idx * 0.18 + 0.25);
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start(audioCtx.currentTime + idx * 0.18);
        osc.stop(audioCtx.currentTime + idx * 0.18 + 0.3);
      });
    } else {
      // Info ping: G5 (784 Hz)
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(783.99, audioCtx.currentTime);
      gain.gain.setValueAtTime(0.001, audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.25, audioCtx.currentTime + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.3);
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.start(audioCtx.currentTime);
      osc.stop(audioCtx.currentTime + 0.32);
    }
  } catch (e) {
    const audio = new Audio(`/static/sounds/${type}.wav`);
    audio.play().catch(() => {});
  }
}

// Request Browser Desktop Notification Permissions
function requestNotificationPermission() {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }
}

// Trigger popup notification and sound chime
function showWebNotification(title, body, type = 'info') {
  playWebSound(type);
  showToast(body, type === 'error' ? 'error' : 'success');

  if ('Notification' in window && Notification.permission === 'granted') {
    try {
      new Notification(title, {
        body: body,
        icon: '/static/icons/icon.png',
        tag: 'naukri-automation'
      });
    } catch (e) {}
  }
}

// Live SSE Log Stream
function initLiveLogs() {
  if (eventSource) {
    eventSource.close();
  }

  eventSource = new EventSource('/api/logs/stream');
  eventSource.onmessage = (e) => {
    appendLog(e.data + '\n');
  };
}

let lastNotificationTime = 0;

function updateConsoleLineBadge() {
  const countBadge = document.getElementById('consoleLineCount');
  if (countBadge) {
    countBadge.innerText = `Auto-capped (${consoleLogLines.length}/${MAX_CONSOLE_LINES} lines)`;
  }
}

function appendLog(text) {
  const terminal = document.getElementById('terminalBody');
  if (!terminal) return;

  const rawLines = text.split('\n');
  for (const line of rawLines) {
    const trimmed = line.trimEnd();
    if (trimmed) {
      consoleLogLines.push(trimmed);
    }
  }

  // Cap ring buffer to prevent DOM bloat and tab crashes
  if (consoleLogLines.length > MAX_CONSOLE_LINES) {
    consoleLogLines.splice(0, consoleLogLines.length - MAX_CONSOLE_LINES);
  }

  terminal.innerText = consoleLogLines.join('\n');
  terminal.scrollTop = terminal.scrollHeight;
  updateConsoleLineBadge();

  const now = Date.now();
  if (now - lastNotificationTime > 15000) {
    if (text.includes('🏁 Automation execution finished') || text.includes('[TARGET LIMIT REACHED]')) {
      lastNotificationTime = now;
      showWebNotification('🎉 Naukri Automation Completed', 'Job application process finished successfully!', 'success');
    } else if (text.includes('[HUMAN ACTION REQUIRED]')) {
      lastNotificationTime = now;
      showWebNotification('🚨 Action Required in Chrome', 'Please complete the verification / CAPTCHA challenge in Chrome.', 'warning');
    } else if (text.includes('🛑 Automation task was cancelled')) {
      lastNotificationTime = now;
      showWebNotification('🛑 Automation Stopped', 'Naukri automation was stopped.', 'info');
    } else if (text.includes('❌ Error during execution') || text.includes('❌ Naukri Automation Failed')) {
      lastNotificationTime = now;
      showWebNotification('❌ Automation Failed', 'Automation encountered an error and stopped.', 'error');
    }
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
  } catch (err) {
    console.error('Error fetching stats:', err);
  }
}

// Load Applications with Smart Diffing
async function loadApplications() {
  try {
    const res = await fetch('/api/applications');
    if (res.ok) {
      const rawText = await res.text();
      // Only trigger re-render if data actually changed to save CPU and DOM performance
      if (rawText !== lastApplicationsJson) {
        lastApplicationsJson = rawText;
        allApplications = JSON.parse(rawText);
        renderTable();
      }
    }
  } catch (err) {
    console.error('Error loading applications:', err);
  }
}

// Render Applications Tracker Table (Page A with Dynamic Pagination)
function renderTable() {
  const tbody = document.getElementById('applicationsTableBody');
  const search = (document.getElementById('tableSearchInput').value || '').toLowerCase().trim();
  const countBadge = document.getElementById('recordCountText');

  let filtered = allApplications.filter(app => {
    // Status Filter
    if (currentFilter !== 'all') {
      if (currentFilter === 'Submitted' && app.status !== 'Submitted') return false;
      if (currentFilter === 'Under Review' && app.status !== 'Under Review') return false;
      if (currentFilter === 'Waiting for Input' && app.status !== 'Waiting for Input') return false;
      if (currentFilter === 'Skipped' && app.status !== 'Skipped' && app.status !== 'Closed') return false;
    }

    // Search Filter
    if (search) {
      const matchComp = (app.company || '').toLowerCase().includes(search);
      const matchRole = (app.role || '').toLowerCase().includes(search);
      const matchNotes = (app.notes || '').toLowerCase().includes(search);
      return matchComp || matchRole || matchNotes;
    }

    return true;
  });

  const totalRecords = filtered.length;
  const totalPages = Math.ceil(totalRecords / pageSize) || 1;
  currentPage = Math.max(1, Math.min(currentPage, totalPages));

  const startIndex = (currentPage - 1) * pageSize;
  const endIndex = Math.min(startIndex + pageSize, totalRecords);
  const pageRecords = filtered.slice(startIndex, endIndex);

  countBadge.innerText = `Showing ${filtered.length} of ${allApplications.length} records`;

  // Update Pagination Controls UI
  const totalRecordsText = document.getElementById('totalRecordsText');
  const pageRangeText = document.getElementById('pageRangeText');
  const pageIndicator = document.getElementById('pageIndicator');
  const btnFirst = document.getElementById('btnFirstPage');
  const btnPrev = document.getElementById('btnPrevPage');
  const btnNext = document.getElementById('btnNextPage');
  const btnLast = document.getElementById('btnLastPage');

  if (totalRecordsText) totalRecordsText.innerText = totalRecords;
  if (pageRangeText) {
    pageRangeText.innerText = totalRecords === 0 ? '0–0' : `${startIndex + 1}–${endIndex}`;
  }
  if (pageIndicator) {
    pageIndicator.innerText = `Page ${currentPage} / ${totalPages}`;
  }
  if (btnFirst) btnFirst.disabled = currentPage <= 1;
  if (btnPrev) btnPrev.disabled = currentPage <= 1;
  if (btnNext) btnNext.disabled = currentPage >= totalPages;
  if (btnLast) btnLast.disabled = currentPage >= totalPages;

  if (pageRecords.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="9" style="text-align: center; padding: 40px; color: var(--text-secondary);">
          No application records matching current filter.
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = pageRecords.map(app => {
    const score = parseFloat(app.match_score) || 0;
    let scoreClass = 'score-low';
    if (score >= 70) scoreClass = 'score-high';
    else if (score >= 55) scoreClass = 'score-mid';

    let statusPillClass = 'status-skipped';
    if (app.status === 'Submitted') statusPillClass = 'status-submitted';
    else if (app.status === 'Under Review') statusPillClass = 'status-review';
    else if (app.status === 'Waiting for Input' || app.status === 'Applying') statusPillClass = 'status-waiting';
    else if (app.status === 'Failed' || app.status === 'Rejected') statusPillClass = 'status-failed';

    const url = app.job_url || '#';
    const filledList = app.filled_answers || [];
    const answersCount = filledList.length;

    const answersBtnHtml = `
      <button type="button" class="btn-answers" onclick="openAnswersModal('${escapeHtml(app.key)}')">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
        <span>View Answers (${answersCount})</span>
      </button>
    `;

    return `
      <tr>
        <td style="font-family: var(--font-code); font-size: 12px; color: var(--text-secondary);">${app.date || '-'}</td>
        <td class="company-cell">${escapeHtml(app.company || 'Unknown')}</td>
        <td class="role-cell">${escapeHtml(app.role || 'Software Engineer')}</td>
        <td><span class="brand-badge">${escapeHtml(app.source || 'Naukri')}</span></td>
        <td><span class="score-badge ${scoreClass}">${score.toFixed(1)}</span></td>
        <td><span class="status-pill ${statusPillClass}">${escapeHtml(app.status || 'Tracked')}</span></td>
        <td style="font-size: 12px; color: var(--text-secondary); max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(app.notes || '')}">
          ${escapeHtml(app.notes || '-')}
        </td>
        <td>${answersBtnHtml}</td>
        <td>
          <a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" class="job-link">
            <span>View</span>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
          </a>
        </td>
      </tr>
    `;
  }).join('');
}

// Open Filled Answers Modal (Transparency Tracker)
window.openAnswersModal = function(appKey) {
  const app = allApplications.find(a => a.key === appKey);
  if (!app) return;

  const modal = document.getElementById('answersModal');
  const modalJobTitle = document.getElementById('modalJobTitle');
  const modalJobSub = document.getElementById('modalJobSub');
  const banner = document.getElementById('modalStatusBanner');
  const tbody = document.getElementById('modalAnswersTableBody');

  modalJobTitle.innerText = `${app.role} · ${app.company}`;
  modalJobSub.innerText = `Platform: ${app.source || 'Naukri'} | Match Score: ${app.match_score || 0}/100 | Applied Date: ${app.date || '-'}`;

  if (app.status === 'Submitted') {
    banner.className = 'modal-banner success';
    banner.innerHTML = `✅ <strong>Live Verified Submission:</strong> This application was officially confirmed and submitted on Naukri.`;
  } else if (app.status === 'Under Review') {
    banner.className = 'modal-banner review';
    banner.innerHTML = `⚠️ <strong>Under Review:</strong> ${escapeHtml(app.notes || 'Awaiting user action or questionnaire resolution.')}`;
  } else {
    banner.className = 'modal-banner';
    banner.innerHTML = `📌 <strong>Status: ${escapeHtml(app.status)}</strong> — ${escapeHtml(app.notes || '')}`;
  }

  const answers = app.filled_answers || [];

  // Provide preset standard values if empty
  const defaultEntries = [
    { question: "Current Location", answer: "Gurugram (Preset)" },
    { question: "Current CTC", answer: "0 LPA (Entry Level Baseline)" },
    { question: "Expected Salary (CTC)", answer: "Dynamic JD Mid-Upper Point (Default: 9.0 LPA)" },
    { question: "Are you willing to relocate?", answer: "Yes (Consent Granted)" },
    { question: "Notice Period", answer: "0 Days (Immediate Joiner)" },
    { question: "Work Authorization in India", answer: "Yes (Authorized to work)" },
    { question: "Visa Sponsorship Requirement", answer: "No (Does not require sponsorship)" },
    { question: "Gender", answer: "Female" },
    { question: "Tailored Resume Version", answer: app.resume_version || "Sakshi_Java_Backend_Resume.pdf" }
  ];

  const displayList = answers.length > 0 ? answers : defaultEntries;

  tbody.innerHTML = displayList.map(item => `
    <tr>
      <td style="font-weight: 600; color: var(--text-primary); font-size: 13px;">${escapeHtml(item.question || 'Form Prompt')}</td>
      <td style="font-family: var(--font-code); font-size: 13px; color: var(--primary); font-weight: 500;">${escapeHtml(item.answer || '-')}</td>
    </tr>
  `).join('');

  modal.style.display = 'flex';
};

function closeAnswersModal() {
  const modal = document.getElementById('answersModal');
  if (modal) modal.style.display = 'none';
}

// Draft answers tracking to prevent wiping user input during polling
const userDraftAnswers = {};
let lastUnresolvedHash = '';

window.trackDraftAnswer = function(qId, val) {
  userDraftAnswers[qId] = val;
};

window.fillDraftFromOption = function(qId, val) {
  userDraftAnswers[qId] = val;
  const input = document.getElementById(`ans_input_${qId}`);
  if (input) {
    input.value = val;
    input.focus();
  }
};

// Load Unresolved Questions (Page B)
async function loadUnresolvedQuestions(forceRender = false) {
  try {
    const res = await fetch('/api/unresolved-questions');
    unresolvedQuestions = await res.json();

    const badge = document.getElementById('unresolvedBadge');
    const countText = document.getElementById('unresolvedCountText');
    const container = document.getElementById('unresolvedListContainer');

    const pendingCount = (unresolvedQuestions || []).length;
    if (badge) {
      badge.innerText = pendingCount;
      badge.style.display = pendingCount > 0 ? 'inline-block' : 'none';
    }

    if (countText) {
      countText.innerText = `${pendingCount} pending question${pendingCount === 1 ? '' : 's'}`;
    }

    if (!container) return;

    // Check if the user is currently typing in an input inside container
    const isUserTyping = container.contains(document.activeElement) && document.activeElement.tagName === 'INPUT';
    
    // Hash check to avoid re-rendering DOM if data hasn't changed
    const currentHash = JSON.stringify(unresolvedQuestions.map(q => q.id || q.question));
    if (!forceRender && currentHash === lastUnresolvedHash && container.children.length > 0) {
      return; // No changes, keep DOM completely untouched!
    }

    if (isUserTyping && !forceRender) {
      return; // Do not interrupt user while typing!
    }

    lastUnresolvedHash = currentHash;

    if (pendingCount === 0) {
      container.innerHTML = `
        <div style="text-align: center; padding: 48px 24px; color: var(--text-secondary);">
          <div style="font-size: 36px; margin-bottom: 12px;">🎉</div>
          <h4 style="font-size: 16px; font-weight: 600; color: var(--text-primary); margin-bottom: 6px;">All Questions Resolved!</h4>
          <p style="font-size: 13px; max-width: 420px; margin: 0 auto;">No unmapped questionnaire prompts are currently pending. The bot is fully equipped with preset answers.</p>
        </div>
      `;
      return;
    }

    container.innerHTML = unresolvedQuestions.map(q => {
      const qId = q.id || `q_${Math.random()}`;
      const draftVal = userDraftAnswers[qId] !== undefined ? userDraftAnswers[qId] : '';
      const optionsHtml = (q.options && q.options.length > 0)
        ? `<div style="margin-top: 6px;"><span style="font-size: 11px; color: var(--neutral); font-weight: 600;">CLICK TO SELECT:</span> ${q.options.map(opt => `<button type="button" class="filter-chip" style="margin-left: 4px; padding: 2px 8px; font-size: 11px; cursor: pointer;" onclick="fillDraftFromOption('${qId}', '${escapeHtml(opt)}')">${escapeHtml(opt)}</button>`).join('')}</div>`
        : '';

      return `
        <div class="unresolved-card" id="card_${qId}">
          <div class="unresolved-header">
            <div>
              <div class="unresolved-q-title">❓ "${escapeHtml(q.question)}"</div>
              <div class="unresolved-meta">
                <span>🏢 <strong>Company:</strong> ${escapeHtml(q.company || 'Hiring Employer')}</span>
                <span>💼 <strong>Role:</strong> ${escapeHtml(q.job_title || 'Software Developer')}</span>
                <span>⏰ <strong>Encountered:</strong> ${escapeHtml(q.first_seen || 'Recently')}</span>
              </div>
              ${optionsHtml}
            </div>
          </div>

          <div class="unresolved-input-row">
            <input type="text" id="ans_input_${qId}" class="text-input" placeholder="Type approved answer (e.g. Yes, 1.0, Gurugram, 9 LPA)..." value="${escapeHtml(draftVal)}" oninput="trackDraftAnswer('${qId}', this.value)" onkeydown="if (event.key === 'Enter') resolveQuestion('${qId}', '${encodeURIComponent(q.question)}')">
            <button type="button" class="btn btn-primary" onclick="resolveQuestion('${qId}', '${encodeURIComponent(q.question)}')">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
              <span>Save & Remember Preset</span>
            </button>
            <button type="button" class="btn btn-secondary" onclick="dismissQuestion('${qId}')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
              <span>Dismiss</span>
            </button>
          </div>
        </div>
      `;
    }).join('');

  } catch (err) {
    console.error('Error loading unresolved questions:', err);
  }
}

// Resolve Question (Save to Presets Database)
window.resolveQuestion = async function(qId, encodedQuestion) {
  const question = decodeURIComponent(encodedQuestion);
  const input = document.getElementById(`ans_input_${qId}`);
  let answer = (input ? input.value : (userDraftAnswers[qId] || '')).trim();

  if (!answer && userDraftAnswers[qId]) {
    answer = userDraftAnswers[qId].trim();
  }

  if (!answer) {
    showToast('Please type an answer before saving.', 'error');
    if (input) input.focus();
    return;
  }

  try {
    const res = await fetch('/api/unresolved-questions/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: qId,
        question: question,
        answer: answer
      })
    });

    const data = await res.json();
    if (data.status === 'ok') {
      delete userDraftAnswers[qId];
      lastUnresolvedHash = ''; // Reset hash so DOM updates immediately
      showToast(`Saved preset: "${question}" -> "${answer}"`);
      await loadUnresolvedQuestions(true);
      await loadPresets();
    } else {
      showToast(data.message || 'Failed to save preset.', 'error');
    }
  } catch (err) {
    showToast(`Error: ${err.message}`, 'error');
  }
};

// Dismiss Unresolved Question
window.dismissQuestion = async function(qId) {
  try {
    const res = await fetch('/api/unresolved-questions/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: qId })
    });

    const data = await res.json();
    if (data.status === 'ok') {
      delete userDraftAnswers[qId];
      lastUnresolvedHash = '';
      showToast('Question dismissed.');
      await loadUnresolvedQuestions(true);
    }
  } catch (err) {
    showToast(`Error: ${err.message}`, 'error');
  }
};

// Manual Preset Addition
async function handleSaveManualPreset() {
  const qInput = document.getElementById('manualQuestionInput');
  const aInput = document.getElementById('manualAnswerInput');
  const question = (qInput ? qInput.value : '').trim();
  const answer = (aInput ? aInput.value : '').trim();

  if (!question || !answer) {
    showToast('Please enter both question phrasing and approved answer.', 'error');
    return;
  }

  try {
    const res = await fetch('/api/unresolved-questions/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, answer })
    });

    const data = await res.json();
    if (data.status === 'ok') {
      showToast(`Added preset: "${question}" -> "${answer}"`);
      if (qInput) qInput.value = '';
      if (aInput) aInput.value = '';
      loadUnresolvedQuestions();
      loadPresets();
    } else {
      showToast(data.message || 'Could not save preset', 'error');
    }
  } catch (err) {
    showToast(`Error: ${err.message}`, 'error');
  }
}

let candidateProfile = {};
let editingRows = {};

// Candidate Profile Preferences Management
async function loadCandidateProfile() {
  try {
    const res = await fetch('/api/profile');
    candidateProfile = await res.json();

    const prefs = candidateProfile.preferences || {};
    const locations = prefs.preferred_locations || [];
    const roles = prefs.preferred_roles || [];
    const salary = prefs.approved_salary_expectation_inr || {};

    const locList = document.getElementById('prefLocationsList');
    if (locList) {
      locList.innerHTML = locations.map(loc => `
        <span class="chip-tag">
          <span>${escapeHtml(loc)}</span>
          <button type="button" class="chip-del-btn" onclick="removeLocationTag('${escapeHtml(loc)}')">&times;</button>
        </span>
      `).join('') || '<span style="color: var(--neutral); font-size: 13px;">No locations configured.</span>';
    }

    const roleList = document.getElementById('prefRolesList');
    if (roleList) {
      roleList.innerHTML = roles.map(role => `
        <span class="chip-tag">
          <span>${escapeHtml(role)}</span>
          <button type="button" class="chip-del-btn" onclick="removeRoleTag('${escapeHtml(role)}')">&times;</button>
        </span>
      `).join('') || '<span style="color: var(--neutral); font-size: 13px;">No roles configured.</span>';
    }

    const minSal = document.getElementById('profileMinSalary');
    const expSal = document.getElementById('profileExpectedSalary');
    const notice = document.getElementById('profileNoticeDays');

    if (minSal) minSal.value = salary.min_lpa || 6.0;
    if (expSal) expSal.value = salary.expected_lpa || 9.0;
    if (notice) notice.value = prefs.notice_period_days !== undefined ? prefs.notice_period_days : 0;

  } catch (err) {
    console.error('Error loading candidate profile:', err);
  }
}

window.addLocationTag = async function() {
  const input = document.getElementById('newLocationInput');
  const val = (input ? input.value : '').trim();
  if (!val) return;

  if (!candidateProfile.preferences) candidateProfile.preferences = {};
  if (!candidateProfile.preferences.preferred_locations) candidateProfile.preferences.preferred_locations = [];

  if (!candidateProfile.preferences.preferred_locations.includes(val)) {
    candidateProfile.preferences.preferred_locations.push(val);
    await saveCandidateProfile();
  }
  if (input) input.value = '';
};

window.removeLocationTag = async function(loc) {
  if (candidateProfile.preferences && candidateProfile.preferences.preferred_locations) {
    candidateProfile.preferences.preferred_locations = candidateProfile.preferences.preferred_locations.filter(l => l !== loc);
    await saveCandidateProfile();
  }
};

window.addRoleTag = async function() {
  const input = document.getElementById('newRoleInput');
  const val = (input ? input.value : '').trim();
  if (!val) return;

  if (!candidateProfile.preferences) candidateProfile.preferences = {};
  if (!candidateProfile.preferences.preferred_roles) candidateProfile.preferences.preferred_roles = [];

  if (!candidateProfile.preferences.preferred_roles.includes(val)) {
    candidateProfile.preferences.preferred_roles.push(val);
    await saveCandidateProfile();
  }
  if (input) input.value = '';
};

window.removeRoleTag = async function(role) {
  if (candidateProfile.preferences && candidateProfile.preferences.preferred_roles) {
    candidateProfile.preferences.preferred_roles = candidateProfile.preferences.preferred_roles.filter(r => r !== role);
    await saveCandidateProfile();
  }
};

async function saveCandidateProfile() {
  if (!candidateProfile.preferences) candidateProfile.preferences = {};
  if (!candidateProfile.preferences.approved_salary_expectation_inr) candidateProfile.preferences.approved_salary_expectation_inr = {};

  const minSal = document.getElementById('profileMinSalary');
  const expSal = document.getElementById('profileExpectedSalary');
  const notice = document.getElementById('profileNoticeDays');

  if (minSal) candidateProfile.preferences.approved_salary_expectation_inr.min_lpa = parseFloat(minSal.value) || 6.0;
  if (expSal) candidateProfile.preferences.approved_salary_expectation_inr.expected_lpa = parseFloat(expSal.value) || 9.0;
  if (notice) candidateProfile.preferences.notice_period_days = parseInt(notice.value, 10) || 0;

  try {
    const res = await fetch('/api/profile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(candidateProfile)
    });

    const data = await res.json();
    if (data.status === 'ok') {
      showToast('Candidate preferences saved successfully!');
      loadCandidateProfile();
    }
  } catch (err) {
    showToast(`Error saving profile: ${err.message}`, 'error');
  }
}

// Load Central Presets (Tab 3) with Full Inline Editability
async function loadPresets() {
  try {
    const res = await fetch('/api/presets');
    currentPresets = await res.json();

    // Update Core Preset Card Values
    const core = currentPresets.core_presets || {};
    const pLoc = document.getElementById('presetLocation');
    const pCtc = document.getElementById('presetCurrentCtc');
    const pAuth = document.getElementById('presetWorkAuth');
    const pVisa = document.getElementById('presetVisa');
    const pGen = document.getElementById('presetGender');
    const pRel = document.getElementById('presetRelocate');
    const pExp = document.getElementById('presetExp');
    const pExpCtc = document.getElementById('presetExpectedCtc');

    if (pLoc) pLoc.innerText = core.current_location || 'Gurugram';
    if (pCtc) pCtc.innerText = `${core.current_ctc_lpa || '0'} LPA`;
    if (pAuth) pAuth.innerText = core.work_authorization || 'Yes (Authorized)';
    if (pVisa) pVisa.innerText = core.requires_visa_sponsorship || 'No (Not Required)';
    if (pGen) pGen.innerText = core.gender || 'Female';
    if (pRel) pRel.innerText = core.willing_to_relocate || 'Yes (Always)';
    if (pExp) pExp.innerText = `${core.base_experience_years || '1.0'} Year (or 1.5 if JD specifies)`;
    if (pExpCtc) pExpCtc.innerText = `Dynamic JD Mid-Upper Point (Default: ${core.default_expected_ctc_lpa || '9.0'} LPA)`;

    const tbody = document.getElementById('customPresetsTableBody');
    const countBadge = document.getElementById('customPresetsCountText');
    const customResolved = currentPresets.custom_resolved_questions || {};
    const mappings = currentPresets.question_mappings || {};

    const allKeys = Array.from(new Set([...Object.keys(mappings), ...Object.keys(customResolved)]));
    if (countBadge) {
      countBadge.innerText = `${allKeys.length} active preset${allKeys.length === 1 ? '' : 's'}`;
    }

    if (!tbody) return;

    if (allKeys.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="3" style="text-align: center; padding: 32px; color: var(--text-secondary);">
            No preset mappings configured. Use the form above to add one!
          </td>
        </tr>
      `;
      return;
    }

    tbody.innerHTML = allKeys.map((key, index) => {
      const isCustom = Boolean(customResolved[key]);
      const currentVal = customResolved[key] || mappings[key] || '';
      const rowId = `preset_row_${index}`;
      const isEditing = Boolean(editingRows[key]);

      let valueCellHtml = '';
      let actionsHtml = '';

      if (isEditing) {
        valueCellHtml = `
          <div style="display: flex; gap: 6px; align-items: center;">
            <input type="text" id="edit_val_${rowId}" class="inline-edit-input" value="${escapeHtml(currentVal)}" onkeydown="if(event.key==='Enter') saveInlineEdit('${rowId}', '${encodeURIComponent(key)}')">
          </div>
        `;
        actionsHtml = `
          <button type="button" class="btn btn-primary" style="height: 30px; padding: 0 10px; font-size: 12px;" onclick="saveInlineEdit('${rowId}', '${encodeURIComponent(key)}')">
            💾 Save
          </button>
          <button type="button" class="btn btn-secondary" style="height: 30px; padding: 0 8px; font-size: 12px; margin-left: 4px;" onclick="cancelInlineEdit('${encodeURIComponent(key)}')">
            ✕
          </button>
        `;
      } else {
        valueCellHtml = `
          <span style="font-family: var(--font-code); font-size: 13px; color: var(--success); font-weight: 600;">
            ${escapeHtml(currentVal)}
          </span>
        `;
        actionsHtml = `
          <button type="button" class="btn btn-secondary" style="height: 28px; padding: 0 8px; font-size: 11px;" onclick="startInlineEdit('${encodeURIComponent(key)}')">
            ✏️ Edit
          </button>
          <button type="button" class="btn btn-destructive" style="height: 28px; padding: 0 8px; font-size: 11px; margin-left: 4px;" onclick="deletePresetMapping('${encodeURIComponent(key)}')">
            🗑️
          </button>
        `;
      }

      return `
        <tr id="${rowId}">
          <td style="font-weight: 600; color: var(--text-primary); font-size: 13px;">
            ${escapeHtml(key)}
            ${isCustom ? '<span class="brand-badge" style="color: var(--primary); margin-left: 6px;">Custom User Preset</span>' : ''}
          </td>
          <td>${valueCellHtml}</td>
          <td style="text-align: right; white-space: nowrap;">${actionsHtml}</td>
        </tr>
      `;
    }).join('');

  } catch (err) {
    console.error('Error loading presets:', err);
  }
}

// Inline Presets Editing Actions
window.startInlineEdit = function(encodedKey) {
  const key = decodeURIComponent(encodedKey);
  editingRows[key] = true;
  loadPresets();
};

window.cancelInlineEdit = function(encodedKey) {
  const key = decodeURIComponent(encodedKey);
  delete editingRows[key];
  loadPresets();
};

window.saveInlineEdit = async function(rowId, encodedKey) {
  const key = decodeURIComponent(encodedKey);
  const input = document.getElementById(`edit_val_${rowId}`);
  const newVal = (input ? input.value : '').trim();

  if (!newVal) {
    showToast('Value cannot be empty.', 'error');
    return;
  }

  try {
    const res = await fetch('/api/presets/update-mapping', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: key, answer: newVal })
    });

    const data = await res.json();
    if (data.status === 'ok') {
      delete editingRows[key];
      showToast(`Updated: "${key}" -> "${newVal}"`);
      await loadPresets();
    }
  } catch (err) {
    showToast(`Error: ${err.message}`, 'error');
  }
};

window.addNewPresetMapping = async function() {
  const qInput = document.getElementById('newMappingKeyInput');
  const vInput = document.getElementById('newMappingValInput');
  const question = (qInput ? qInput.value : '').trim();
  const answer = (vInput ? vInput.value : '').trim();

  if (!question || !answer) {
    showToast('Please enter both question pattern and preset value.', 'error');
    return;
  }

  try {
    const res = await fetch('/api/presets/update-mapping', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, answer })
    });

    const data = await res.json();
    if (data.status === 'ok') {
      showToast(`Added preset: "${question}" -> "${answer}"`);
      if (qInput) qInput.value = '';
      if (vInput) vInput.value = '';
      await loadPresets();
    }
  } catch (err) {
    showToast(`Error: ${err.message}`, 'error');
  }
};

window.deletePresetMapping = async function(encodedKey) {
  const key = decodeURIComponent(encodedKey);
  if (!confirm(`Are you sure you want to delete preset for "${key}"?`)) return;

  try {
    const res = await fetch('/api/presets/delete-mapping', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: key })
    });

    const data = await res.json();
    if (data.status === 'ok') {
      delete editingRows[key];
      showToast(`Deleted preset: "${key}"`);
      await loadPresets();
    }
  } catch (err) {
    showToast(`Error: ${err.message}`, 'error');
  }
};

window.promptEditCorePreset = async function(key, label) {
  const currentVal = currentPresets.core_presets ? (currentPresets.core_presets[key] || '') : '';
  const newVal = prompt(`Update ${label}:`, currentVal);
  if (newVal === null) return;

  const trimmed = newVal.trim();
  if (!trimmed) {
    showToast('Value cannot be empty.', 'error');
    return;
  }

  try {
    const res = await fetch('/api/presets/update-core', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key, value: trimmed })
    });

    const data = await res.json();
    if (data.status === 'ok') {
      showToast(`Updated ${label} to "${trimmed}"`);
      await loadPresets();
      await loadCandidateProfile();
    }
  } catch (err) {
    showToast(`Error: ${err.message}`, 'error');
  }
};

// Hook into DOMContentLoaded for profile loading
document.addEventListener('DOMContentLoaded', () => {
  loadCandidateProfile();
  const saveBtn = document.getElementById('saveCandidateProfileBtn');
  if (saveBtn) saveBtn.addEventListener('click', saveCandidateProfile);
});

// Toast Notification System
function showToast(message, type = 'success') {
  const container = document.getElementById('toastContainer');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = 'toast';
  if (type === 'error') {
    toast.style.borderLeftColor = 'var(--error)';
  }

  const icon = type === 'error' ? '⚠️' : '✅';
  toast.innerHTML = `<span>${icon}</span><span>${escapeHtml(message)}</span>`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    toast.style.transition = 'all 0.2s ease-out';
    setTimeout(() => { toast.remove(); }, 250);
  }, 3500);
}

function escapeHtml(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}
