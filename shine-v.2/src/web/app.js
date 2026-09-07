// Genesis Dashboard Frontend Logic (Shine Automation)

let eventSource = null;
let isRunning = false;
let isSoundMuted = localStorage.getItem('sound_muted') !== 'false'; // Default to muted
let lastAudioPlayTime = 0;
const AUDIO_COOLDOWN_MS = 20000; // Minimum 20 seconds between sounds

document.addEventListener('DOMContentLoaded', () => {
    initSSE();
    fetchStats();
    fetchApplications();
    setInterval(fetchStats, 5000);
    setInterval(fetchApplications, 6000);

    updateSoundToggleUI();
    setupEventListeners();
});

function updateSoundToggleUI() {
    const iconOn = document.getElementById('icon-sound-on');
    const iconOff = document.getElementById('icon-sound-off');
    const toggleText = document.getElementById('sound-toggle-text');

    if (!iconOn || !iconOff || !toggleText) return;

    if (isSoundMuted) {
        iconOn.classList.add('hidden');
        iconOff.classList.remove('hidden');
        toggleText.textContent = 'Muted';
    } else {
        iconOff.classList.add('hidden');
        iconOn.classList.remove('hidden');
        toggleText.textContent = 'Sound On';
    }
}

function setupEventListeners() {
    // Sound Mute Toggle
    const btnSound = document.getElementById('btn-toggle-sound');
    if (btnSound) {
        btnSound.addEventListener('click', () => {
            isSoundMuted = !isSoundMuted;
            localStorage.setItem('sound_muted', isSoundMuted ? 'true' : 'false');
            updateSoundToggleUI();
            if (!isSoundMuted) {
                playAudio('info', true);
            }
        });
    }

    // Start Run
    document.getElementById('btn-start-run').addEventListener('click', async () => {
        try {
            playAudio('info');
            const res = await fetch('/api/run/start', { method: 'POST' });
            const data = await res.json();
            if (data.status === 'started' || data.status === 'already_running') {
                setRunningState(true);
            }
        } catch (e) {
            console.error('Failed to start run:', e);
        }
    });

    // Apply on Under Review / Failed Jobs
    const btnRetry = document.getElementById('btn-retry-pending');
    if (btnRetry) {
        btnRetry.addEventListener('click', async () => {
            try {
                playAudio('info');
                const res = await fetch('/api/run/retry-pending', { method: 'POST' });
                const data = await res.json();
                if (data.status === 'started' || data.status === 'already_running') {
                    setRunningState(true);
                }
            } catch (e) {
                console.error('Failed to start retry run:', e);
            }
        });
    }

    // Stop Run
    document.getElementById('btn-stop-run').addEventListener('click', async () => {
        try {
            playAudio('warning');
            const res = await fetch('/api/run/stop', { method: 'POST' });
            const data = await res.json();
            setRunningState(false);
        } catch (e) {
            console.error('Failed to stop run:', e);
        }
    });

    // Launch Chrome
    document.getElementById('btn-launch-browser').addEventListener('click', async () => {
        try {
            playAudio('info');
            await fetch('/api/browser/launch', { method: 'POST' });
            appendLog('[System] Google Chrome launched with Automation Profile 4.', 'info');
        } catch (e) {
            console.error('Failed to launch browser:', e);
        }
    });

    // Clear Logs
    document.getElementById('btn-clear-logs').addEventListener('click', () => {
        const terminal = document.getElementById('log-terminal');
        terminal.innerHTML = '';
    });

    // Search & Filter
    document.getElementById('search-input').addEventListener('input', fetchApplications);
    document.getElementById('status-filter').addEventListener('change', fetchApplications);

    // Modal Resolved
    document.getElementById('btn-modal-resolved').addEventListener('click', () => {
        document.getElementById('checkpoint-modal').classList.add('hidden');
    });
}

function setRunningState(running) {
    isRunning = running;
    const btnStart = document.getElementById('btn-start-run');
    const btnStop = document.getElementById('btn-stop-run');

    if (running) {
        btnStart.classList.add('hidden');
        btnStop.classList.remove('hidden');
    } else {
        btnStart.classList.remove('hidden');
        btnStop.classList.add('hidden');
    }
}

function initSSE() {
    if (eventSource) {
        eventSource.close();
    }

    eventSource = new EventSource('/api/logs/stream');
    const dot = document.getElementById('stream-status-dot');

    eventSource.onopen = () => {
        dot.style.backgroundColor = '#10B981';
    };

    eventSource.onmessage = (event) => {
        try {
            const raw = event.data;
            if (raw === ': ping' || !raw) return;

            let logType = 'normal';
            if (raw.includes('[SUCCESS]') || raw.includes('Successfully')) {
                logType = 'success';
                playAudio('success');
                fetchStats();
                fetchApplications();
            } else if (raw.includes('🚨') || raw.includes('Waiting for Input') || raw.includes('ACTION REQUIRED')) {
                logType = 'warning';
                playAudio('warning');
                showCheckpointModal(raw);
                fetchStats();
                fetchApplications();
            } else if (raw.includes('Error') || raw.includes('FAILED') || raw.includes('exception')) {
                logType = 'error';
                playAudio('error');
            } else if (raw.includes('📌') || raw.includes('🔍') || raw.includes('🌐')) {
                logType = 'info';
            }

            appendLog(raw, logType);
        } catch (e) {
            console.error('SSE message parse error:', e);
        }
    };

    eventSource.onerror = () => {
        dot.style.backgroundColor = '#EF4444';
        setTimeout(initSSE, 4000);
    };
}

function appendLog(message, type = 'normal') {
    const terminal = document.getElementById('log-terminal');
    const line = document.createElement('div');
    line.className = `log-line ${type}`;
    line.textContent = message;
    terminal.appendChild(line);
    terminal.scrollTop = terminal.scrollHeight;
}

async function fetchStats() {
    try {
        const res = await fetch('/api/stats');
        if (!res.ok) return;
        const stats = await res.json();

        document.getElementById('stat-total').textContent = stats.total_evaluated || 0;
        document.getElementById('stat-submitted').textContent = stats.submitted || 0;
        document.getElementById('stat-review').textContent = stats.under_review || 0;
        document.getElementById('stat-avg-score').textContent = (stats.average_score || 0.0).toFixed(1);

        const countPendingEl = document.getElementById('count-pending');
        if (countPendingEl) {
            countPendingEl.textContent = stats.pending_total || stats.under_review || 0;
        }

        if (stats.is_running !== undefined) {
            setRunningState(stats.is_running);
        }
    } catch (e) {
        console.error('Failed to fetch stats:', e);
    }
}

async function fetchApplications() {
    try {
        const searchQuery = document.getElementById('search-input').value.trim();
        const statusFilter = document.getElementById('status-filter').value;

        const params = new URLSearchParams();
        if (searchQuery) params.append('search', searchQuery);
        if (statusFilter && statusFilter !== 'all') params.append('status', statusFilter);

        const res = await fetch(`/api/applications?${params.toString()}`);
        if (!res.ok) return;
        const records = await res.json();

        renderTable(records);
    } catch (e) {
        console.error('Failed to fetch applications:', e);
    }
}

function renderTable(records) {
    const tbody = document.getElementById('table-body');
    const countBadge = document.getElementById('table-count');
    countBadge.textContent = `${records.length} items`;

    if (!records || records.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" class="table-empty">No matching application records found.</td></tr>`;
        return;
    }

    tbody.innerHTML = records.map(r => {
        const score = r.match_score || 0;
        let scoreClass = 'low';
        if (score >= 70) scoreClass = 'high';
        else if (score >= 55) scoreClass = 'mid';

        const status = r.status || 'Found';
        let statusClass = 'skipped';
        if (status === 'Submitted') statusClass = 'submitted';
        else if (status === 'Under Review') statusClass = 'review';
        else if (status === 'Applying') statusClass = 'applying';
        else if (status === 'Waiting for Input') statusClass = 'waiting';

        const dateDisplay = (r.date || '').split(' ')[0] || '-';
        
        let actionCell = '<div class="table-actions">';
        if (r.job_url) {
            actionCell += `<a href="${r.job_url}" target="_blank" class="btn-ghost" title="Open Job Listing">View</a>`;
        }
        if (['Under Review', 'Waiting for Input', 'Failed', 'Applying'].includes(status) && r.job_url) {
            actionCell += `<button class="btn-action-apply" onclick="applySingleJob(this, '${encodeURIComponent(r.job_url)}')">⚡ Apply</button>`;
        }
        actionCell += '</div>';

        return `
            <tr>
                <td>${dateDisplay}</td>
                <td><strong>${escapeHtml(r.company || 'Unknown')}</strong></td>
                <td>${escapeHtml(r.role || 'Unknown')}</td>
                <td><span class="score-pill ${scoreClass}">${score}</span></td>
                <td><span class="status-badge ${statusClass}">${escapeHtml(status)}</span></td>
                <td>${actionCell}</td>
            </tr>
        `;
    }).join('');
}

window.applySingleJob = async function(btnEl, encodedUrl) {
    const jobUrl = decodeURIComponent(encodedUrl);
    if (!jobUrl) return;

    if (btnEl) {
        btnEl.disabled = true;
        btnEl.textContent = '⏳ Applying...';
    }

    try {
        playAudio('info');
        const res = await fetch('/api/applications/apply-single', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ job_url: jobUrl })
        });
        const data = await res.json();
        if (data.status === 'started' || data.status === 'already_running') {
            setRunningState(true);
            appendLog(`[System] Started application workflow for ${jobUrl}`, 'info');
        }
    } catch (e) {
        console.error('Failed to trigger single apply:', e);
        if (btnEl) {
            btnEl.disabled = false;
            btnEl.textContent = '⚡ Apply';
        }
    }
};

function showCheckpointModal(reasonText) {
    const modal = document.getElementById('checkpoint-modal');
    const desc = document.getElementById('modal-checkpoint-reason');
    desc.textContent = reasonText;
    modal.classList.remove('hidden');
}

function playAudio(type, bypassCooldown = false) {
    if (isSoundMuted) return;

    const now = Date.now();
    if (!bypassCooldown && (now - lastAudioPlayTime) < AUDIO_COOLDOWN_MS) {
        return;
    }

    try {
        const audio = document.getElementById(`audio-${type}`);
        if (audio) {
            audio.volume = 0.15; // Set soft, gentle volume (15%)
            audio.currentTime = 0;
            audio.play().catch(() => {});
            lastAudioPlayTime = now;
        }
    } catch (e) {}
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
