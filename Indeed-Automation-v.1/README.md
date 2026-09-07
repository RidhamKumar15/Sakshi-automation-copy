# 🎯 Indeed Job Application Automation Agent

Autonomous job-search, scoring, and application agent specifically built for **Indeed** (`in.indeed.com` / `indeed.com`), utilizing a dedicated persistent Chrome automation profile via direct Chrome DevTools Protocol (CDP).

---

## ⚡ Quick Start

```bash
# Switch into the isolated Indeed automation directory
cd /run/media/ridhamverma/R/Sakshi_AI/job-automation/Indeed-Automation-v.1

# 1. Launch Chrome with Automation profile
python main.py launch-browser

# 2. Launch Genesis Web Dashboard UI (Port 8002)
python main.py ui --port 8002

# 3. Or run autonomous search & application via CLI (Test Mode: 1 company)
python main.py run --test

# 4. Or run full batch (e.g. limit 10)
python main.py run --limit 10
```

---

## 📋 CLI Commands

| Command | Description |
| ------- | ----------- |
| `python main.py run --test` | Runs test mode: finds and evaluates jobs, applies to 1 company on Indeed for verification. |
| `python main.py run --limit N` | Runs autonomous application loop up to `N` applications. |
| `python main.py run --keywords "Java Developer" "Backend"` | Runs search with custom keywords. |
| `python main.py ui --port 8002` | Launches the Genesis Web Dashboard on `http://127.0.0.1:8002`. |
| `python main.py status` | Displays current statistics of all tracked Indeed job applications. |
| `python main.py review` | Lists all jobs currently waiting in the human review queue. |
| `python main.py test-score` | Tests the 0–100 scoring engine with a sample candidate evaluation. |
| `python main.py launch-browser` | Spawns Google Chrome with the persistent `Automation` profile. |

---

## 📁 Architecture Overview

```text
Indeed-Automation-v.1/
├── config/
│   ├── candidate_profile.json  # Candidate details, skills, approved screener answers & pitch templates
│   └── settings.json           # Indeed search parameters, freshness tiers, scoring weights & thresholds
├── src/
│   ├── cdp_client.py           # Direct WebSocket Chrome DevTools Protocol client with CAPTCHA detection
│   ├── scorer.py               # 0–100 multi-criteria job scoring engine
│   ├── tracker.py              # Deduplication & JSON / Markdown state management
│   ├── reporter.py             # Structured run summaries
│   ├── notifier.py             # Audio sound alerts & desktop notifications
│   ├── web_server.py           # SSE real-time streaming web dashboard server (Port 8002)
│   ├── platforms/
│   │   └── indeed.py           # Dedicated Indeed extraction, questionnaires & apply flow
│   └── web/                    # Genesis Dashboard UI assets (HTML, CSS, JS, sound effects)
├── data/
│   ├── tracker.json            # Persistent JSON database of tracked jobs
│   ├── unresolved_questions.json # Unmapped screening questions
│   └── latest_run_report.md    # Auto-generated end-of-run report
├── resumes/                    # Candidate tailored PDF resumes
├── main.py                     # CLI entrypoint
├── APPLICATIONS_TRACKER.md     # Auto-exported live markdown application tracker
└── README.md
```
