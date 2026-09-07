// Genesis Indeed Automation Dashboard Frontend Logic

const MAX_CONSOLE_LINES = 100; // Hard cap on terminal DOM elements to prevent memory leaks and crashes

let currentTab = 'all';
let allApplications = [];
let unresolvedQuestions = [];
let customAnswers = {};

// Pagination State
let currentPage = 1;
let pageSize = 25;
let lastApplicationsJson = '';

// Initialize on DOM Ready
document.addEventListener('DOMContentLoaded', () => {
  initSSE();
  fetchStats();
  fetchApplications();
  fetchQuestions();
  checkAgentStatus();

  // Polling intervals
  setInterval(fetchStats, 4000);
  setInterval(fetchApplications, 5000);
  setInterval(fetchQuestions, 4000);
  setInterval(checkAgentStatus, 3000);

  setupEventListeners();
});

function setupEventListeners() {
  // Tabs
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      e.target.classList.add('active');
      currentTab = e.target.getAttribute('data-tab');
      currentPage = 1; // Reset to page 1 on tab change
      
      const appView = document.getElementById('applicationsView');
      const tablePagination = document.getElementById('tablePagination');
      const qView = document.getElementById('questionsView');

      if (currentTab === 'questions') {
        if (appView) appView.style.display = 'none';
        if (tablePagination) tablePagination.style.display = 'none';
        if (qView) qView.style.display = 'block';
        renderQuestionsView();
      } else {
        if (appView) appView.style.display = 'block';
        if (tablePagination) tablePagination.style.display = 'flex';
        if (qView) qView.style.display = 'none';
        renderApplicationsTable();
      }
    });
  });

  // Filter Search Input
  document.getElementById('tableFilter').addEventListener('input', () => {
    currentPage = 1; // Reset to page 1 on filter
    if (currentTab === 'questions') {
      renderQuestionsView();
    } else {
      renderApplicationsTable();
    }
  });

  // Pagination Controls
  const pageSizeSelect = document.getElementById('pageSizeSelect');
  if (pageSizeSelect) {
    pageSizeSelect.addEventListener('change', (e) => {
      pageSize = parseInt(e.target.value, 10) || 25;
      currentPage = 1;
      renderApplicationsTable();
    });
  }

  const btnFirst = document.getElementById('btnFirstPage');
  if (btnFirst) {
    btnFirst.addEventListener('click', () => {
      currentPage = 1;
      renderApplicationsTable();
    });
  }

  const btnPrev = document.getElementById('btnPrevPage');
  if (btnPrev) {
    btnPrev.addEventListener('click', () => {
      if (currentPage > 1) {
        currentPage--;
        renderApplicationsTable();
      }
    });
  }

  const btnNext = document.getElementById('btnNextPage');
  if (btnNext) {
    btnNext.addEventListener('click', () => {
      currentPage++;
      renderApplicationsTable();
    });
  }

  const btnLast = document.getElementById('btnLastPage');
  if (btnLast) {
    btnLast.addEventListener('click', () => {
      currentPage = 999999; // Will be clamped to totalPages in render
      renderApplicationsTable();
    });
  }

  // Action Buttons
  document.getElementById('btnLaunchBrowser').addEventListener('click', launchBrowser);
  document.getElementById('btnRunTest').addEventListener('click', runTestMode);
  document.getElementById('btnStartBatch').addEventListener('click', startBatchAutomation);
  document.getElementById('btnStopAutomation').addEventListener('click', stopAutomation);
  document.getElementById('btnClearConsole').addEventListener('click', () => {
    const consoleOutput = document.getElementById('consoleOutput');
    consoleOutput.innerHTML = '<div class="console-entry text-muted">[System] Console cleared.</div>';
    updateConsoleLineBadge();
  });
}

// SSE Log Streaming
function initSSE() {
  const eventSource = new EventSource('/api/logs');

  eventSource.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      if (data.log) {
        appendConsoleLog(data.log);
      }
    } catch (err) {
      appendConsoleLog(event.data);
    }
  };

  eventSource.onerror = () => {
    setTimeout(initSSE, 5000);
  };
}

function appendConsoleLog(text) {
  const consoleOutput = document.getElementById('consoleOutput');
  if (!consoleOutput) return;

  // Enforce strict ring buffer cap in DOM to prevent browser tab bloat/crash
  while (consoleOutput.children.length >= MAX_CONSOLE_LINES) {
    consoleOutput.removeChild(consoleOutput.firstElementChild);
  }

  const div = document.createElement('div');
  div.className = 'console-entry';

  if (text.includes('🚨') || text.includes('ERROR') || text.includes('❌')) {
    div.style.color = '#f87171';
  } else if (text.includes('🎉') || text.includes('✅') || text.includes('Submitted') || text.includes('💡')) {
    div.style.color = '#4ade80';
  } else if (text.includes('⚠️') || text.includes('⏳') || text.includes('Under Review') || text.includes('❓')) {
    div.style.color = '#fbbf24';
  } else if (text.includes('🚀') || text.includes('📌') || text.includes('🔍')) {
    div.style.color = '#818cf8';
  }

  div.textContent = text;
  consoleOutput.appendChild(div);
  consoleOutput.scrollTop = consoleOutput.scrollHeight;

  updateConsoleLineBadge();
}

function updateConsoleLineBadge() {
  const consoleOutput = document.getElementById('consoleOutput');
  const countBadge = document.getElementById('consoleLineCount');
  if (countBadge && consoleOutput) {
    countBadge.textContent = `Auto-capped (${consoleOutput.children.length}/${MAX_CONSOLE_LINES})`;
  }
}

// API Calls
async function fetchStats() {
  try {
    const res = await fetch('/api/stats');
    if (res.ok) {
      const data = await res.json();
      document.getElementById('statEvaluated').textContent = data.total_evaluated || 0;
      document.getElementById('statSubmitted').textContent = data.submitted || 0;
      document.getElementById('statReview').textContent = data.under_review || 0;
      document.getElementById('statWaiting').textContent = data.waiting_for_input || 0;
      document.getElementById('statAvgScore').textContent = data.average_score || '0.0';
      document.getElementById('reviewCountBadge').textContent = (data.under_review || 0) + (data.waiting_for_input || 0);
    }
  } catch (e) {}
}

async function fetchApplications() {
  try {
    const res = await fetch('/api/applications');
    if (res.ok) {
      const rawText = await res.text();
      // Only trigger re-render if data actually changed
      if (rawText !== lastApplicationsJson) {
        lastApplicationsJson = rawText;
        allApplications = JSON.parse(rawText);
        if (currentTab !== 'questions') {
          renderApplicationsTable();
        }
      }
    }
  } catch (e) {}
}

async function fetchQuestions() {
  try {
    const res = await fetch('/api/questions');
    if (res.ok) {
      const data = await res.json();
      unresolvedQuestions = data.unresolved || [];
      customAnswers = data.custom_answers || {};
      
      const badge = document.getElementById('unresolvedCountBadge');
      if (badge) badge.textContent = unresolvedQuestions.length;

      if (currentTab === 'questions') {
        renderQuestionsView();
      }
    }
  } catch (e) {}
}

async function checkAgentStatus() {
  try {
    const res = await fetch('/api/status');
    if (res.ok) {
      const data = await res.json();
      const badge = document.getElementById('agentStatusBadge');
      const text = document.getElementById('agentStatusText');
      const btnStart = document.getElementById('btnStartBatch');
      const btnStop = document.getElementById('btnStopAutomation');

      if (data.is_running) {
        badge.className = 'status-indicator running';
        text.textContent = 'Agent Running...';
        btnStart.style.display = 'none';
        btnStop.style.display = 'inline-flex';
      } else {
        badge.className = 'status-indicator idle';
        text.textContent = 'System Ready';
        btnStart.style.display = 'inline-flex';
        btnStop.style.display = 'none';
      }
    }
  } catch (e) {}
}

async function launchBrowser() {
  appendConsoleLog('[Genesis] 🌐 Requesting Google Chrome browser launch with Automation profile...');
  try {
    const res = await fetch('/api/launch-browser', { method: 'POST' });
    const data = await res.json();
    appendConsoleLog(`[Genesis] ${data.message}`);
  } catch (e) {
    appendConsoleLog(`[Genesis] ❌ Error launching browser: ${e}`);
  }
}

async function runTestMode() {
  appendConsoleLog('[Genesis] 🧪 Starting single-company test mode on Indeed...');
  try {
    const res = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ test_mode: true, source: 'indeed' })
    });
    const data = await res.json();
    appendConsoleLog(`[Genesis] ${data.message}`);
    checkAgentStatus();
  } catch (e) {
    appendConsoleLog(`[Genesis] ❌ Error starting test: ${e}`);
  }
}

async function startBatchAutomation() {
  const limitVal = parseInt(document.getElementById('limitInput').value, 10) || 5;
  const kwInput = document.getElementById('customKeywords').value.trim();
  const keywords = kwInput ? kwInput.split(',').map(k => k.trim()).filter(Boolean) : null;

  appendConsoleLog(`[Genesis] 🚀 Initiating batch auto-apply (Limit: ${limitVal})...`);
  try {
    const res = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        test_mode: false,
        source: 'indeed',
        limit: limitVal,
        keywords: keywords
      })
    });
    const data = await res.json();
    appendConsoleLog(`[Genesis] ${data.message}`);
    checkAgentStatus();
  } catch (e) {
    appendConsoleLog(`[Genesis] ❌ Error launching batch: ${e}`);
  }
}

async function stopAutomation() {
  appendConsoleLog('[Genesis] 🛑 Sending stop signal to active automation...');
  try {
    const res = await fetch('/api/stop', { method: 'POST' });
    const data = await res.json();
    appendConsoleLog(`[Genesis] ${data.message}`);
    checkAgentStatus();
  } catch (e) {
    appendConsoleLog(`[Genesis] ❌ Error stopping automation: ${e}`);
  }
}

// Render Table with Lightweight Pagination Slicing
function renderApplicationsTable() {
  const tbody = document.getElementById('applicationsTableBody');
  const filterVal = (document.getElementById('tableFilter').value || '').toLowerCase().trim();

  let records = allApplications;
  if (currentTab === 'review') {
    records = records.filter(r => r.status === 'Under Review' || r.status === 'Waiting for Input');
  }

  if (filterVal) {
    records = records.filter(r => {
      const c = (r.company || '').toLowerCase();
      const ro = (r.role || '').toLowerCase();
      const s = (r.status || '').toLowerCase();
      return c.includes(filterVal) || ro.includes(filterVal) || s.includes(filterVal);
    });
  }

  const totalRecords = records.length;
  const totalPages = Math.ceil(totalRecords / pageSize) || 1;
  currentPage = Math.max(1, Math.min(currentPage, totalPages));

  const startIndex = (currentPage - 1) * pageSize;
  const endIndex = Math.min(startIndex + pageSize, totalRecords);
  const pageRecords = records.slice(startIndex, endIndex);

  // Update Pagination Controls UI
  const totalRecordsText = document.getElementById('totalRecordsText');
  const pageRangeText = document.getElementById('pageRangeText');
  const pageIndicator = document.getElementById('pageIndicator');
  const btnFirst = document.getElementById('btnFirstPage');
  const btnPrev = document.getElementById('btnPrevPage');
  const btnNext = document.getElementById('btnNextPage');
  const btnLast = document.getElementById('btnLastPage');

  if (totalRecordsText) totalRecordsText.textContent = totalRecords.toLocaleString();
  if (pageRangeText) pageRangeText.textContent = totalRecords === 0 ? '0–0' : `${startIndex + 1}–${endIndex}`;
  if (pageIndicator) pageIndicator.textContent = `Page ${currentPage} / ${totalPages}`;

  if (btnFirst) btnFirst.disabled = (currentPage === 1);
  if (btnPrev) btnPrev.disabled = (currentPage === 1);
  if (btnNext) btnNext.disabled = (currentPage >= totalPages);
  if (btnLast) btnLast.disabled = (currentPage >= totalPages);

  if (pageRecords.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="text-center text-muted py-8">No applications found in this view.</td></tr>`;
    return;
  }

  // Render ONLY the current slice (e.g. 25 rows instead of 10,000)
  tbody.innerHTML = pageRecords.map(r => {
    const score = r.match_score !== undefined ? `${r.match_score}` : 'N/A';
    const statusBadge = getStatusBadge(r.status);
    const jobUrl = r.job_url ? `<a href="${r.job_url}" target="_blank" class="text-indigo" title="Open Job Listing">${escapeHtml(r.company)}</a>` : escapeHtml(r.company);

    return `
      <tr>
        <td class="text-muted" style="white-space: nowrap;">${escapeHtml(r.date || '')}</td>
        <td style="font-weight: 600;">${jobUrl}</td>
        <td>${escapeHtml(r.role || '')}</td>
        <td><span class="badge ${score >= 70 ? 'badge-success' : score >= 55 ? 'badge-warning' : 'badge-secondary'}">${score}</span></td>
        <td>${statusBadge}</td>
        <td>
          <div style="display: flex; gap: 4px;">
            ${r.job_url ? `<a href="${r.job_url}" target="_blank" class="btn btn-ghost btn-sm" title="View Job Details">🔗 View</a>` : ''}
          </div>
        </td>
      </tr>
    `;
  }).join('');
}

// Render Questions View
function renderQuestionsView() {
  const unresolvedListEl = document.getElementById('unresolvedQuestionsList');
  const learnedListEl = document.getElementById('learnedAnswersList');
  const filterVal = (document.getElementById('tableFilter').value || '').toLowerCase().trim();

  // 1. Unresolved Questions
  let filteredUnresolved = unresolvedQuestions;
  if (filterVal) {
    filteredUnresolved = filteredUnresolved.filter(q => 
      (q.question || '').toLowerCase().includes(filterVal) || 
      (q.company || '').toLowerCase().includes(filterVal) ||
      (q.role || '').toLowerCase().includes(filterVal)
    );
  }

  if (filteredUnresolved.length === 0) {
    unresolvedListEl.innerHTML = `<div class="empty-state text-muted">No unresolved questions pending. All forms are handled automatically!</div>`;
  } else {
    unresolvedListEl.innerHTML = filteredUnresolved.map(q => {
      const qId = q.id;
      const optionsHtml = q.options && q.options.length > 0 
        ? `<div class="text-xs text-muted mt-1">Options: ${q.options.map(o => escapeHtml(typeof o === 'object' ? (o.label || o.text || o.value) : o)).join(', ')}</div>`
        : '';

      return `
        <div class="question-card" id="card_${qId}">
          <div class="question-header">
            <div class="question-title">${escapeHtml(q.question)}</div>
            <div class="question-meta">
              <span class="badge badge-info">${escapeHtml(q.company || 'Job Application')}</span>
              <span class="badge badge-secondary">${escapeHtml(q.field_type || 'text')}</span>
            </div>
          </div>
          ${optionsHtml}
          <div class="question-input-row mt-2">
            <input type="text" id="ans_${qId}" placeholder="Enter your answer (e.g. 0, 850000, Immediately, Yes)..." class="form-input form-input-sm" />
            <button class="btn btn-primary btn-sm" onclick="saveAnswer('${escapeHtml(q.question)}', '${qId}')">💾 Save & Learn</button>
            <button class="btn btn-ghost btn-sm" onclick="dismissQuestion('${qId}')">✕</button>
          </div>
        </div>
      `;
    }).join('');
  }

  // 2. Learned Answers
  const learnedKeys = Object.keys(customAnswers);
  let filteredKeys = learnedKeys;
  if (filterVal) {
    filteredKeys = filteredKeys.filter(k => 
      k.toLowerCase().includes(filterVal) || 
      String(customAnswers[k]).toLowerCase().includes(filterVal)
    );
  }

  if (filteredKeys.length === 0) {
    learnedListEl.innerHTML = `<div class="empty-state text-muted">No custom learned answers yet. Answer questions above to build your automated knowledge base!</div>`;
  } else {
    learnedListEl.innerHTML = filteredKeys.map(k => `
      <div class="learned-item">
        <span class="learned-key">${escapeHtml(k)}</span>
        <div style="display: flex; align-items: center; gap: 8px;">
          <span class="learned-val">${escapeHtml(String(customAnswers[k]))}</span>
          <button class="btn btn-ghost btn-sm" onclick="deleteLearnedAnswer('${escapeHtml(k)}')" title="Delete rule">🗑️</button>
        </div>
      </div>
    `).join('');
  }
}

// Answer Actions
async function saveAnswer(question, qId) {
  const inputEl = document.getElementById(`ans_${qId}`);
  if (!inputEl) return;
  const answer = inputEl.value.trim();

  if (!answer) {
    alert('Please enter an answer before saving.');
    return;
  }

  try {
    const res = await fetch('/api/save_question_answer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: question, answer: answer })
    });
    const data = await res.json();
    if (data.status === 'success') {
      appendConsoleLog(`[Genesis] 💡 Saved learned answer for: "${question}" -> "${answer}"`);
      await fetchQuestions();
    }
  } catch (e) {
    alert(`Error saving answer: ${e}`);
  }
}

async function dismissQuestion(qId) {
  try {
    await fetch('/api/delete_question', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: qId })
    });
    await fetchQuestions();
  } catch (e) {}
}

async function deleteLearnedAnswer(pattern) {
  if (!confirm(`Delete memory rule for "${pattern}"?`)) return;
  try {
    await fetch('/api/delete_question', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pattern: pattern })
    });
    appendConsoleLog(`[Genesis] 🗑️ Deleted memory rule for: "${pattern}"`);
    await fetchQuestions();
  } catch (e) {}
}

function getStatusBadge(status) {
  switch (status) {
    case 'Submitted':
      return `<span class="badge badge-success">Submitted</span>`;
    case 'Under Review':
      return `<span class="badge badge-warning">Under Review</span>`;
    case 'Waiting for Input':
      return `<span class="badge badge-info">Waiting for Input</span>`;
    case 'Applying':
      return `<span class="badge badge-applying"><span class="spinner-dot"></span>Applying...</span>`;
    case 'Already Applied':
      return `<span class="badge badge-secondary">Already Applied</span>`;
    case 'Skipped':
    case 'Closed':
      return `<span class="badge badge-secondary">${status}</span>`;
    case 'Failed':
      return `<span class="badge badge-danger">Failed</span>`;
    default:
      return `<span class="badge badge-secondary">${status || 'Found'}</span>`;
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
