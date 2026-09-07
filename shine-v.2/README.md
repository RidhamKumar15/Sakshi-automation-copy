# 🤖 Shine.com Job Application Automation Agent

An autonomous, precision job-search and application assistant built specifically for entry-level Software Engineering roles on **Shine.com** with persistent Chrome profile reuse, automated 0–100 scoring, duplicate prevention, approved questionnaire answering, and Genesis design system dashboard.

---

## 🎯 Key Features & Rules

1. **Dedicated Chrome Profile Reuse**:
   - Uses your dedicated `Automation` profile (`Profile 4` in `~/.config/google-chrome`).
   - Reuses existing authenticated sessions, cookies, and local storage on Shine.com.
   - Runs in visible, headed browser mode via Chrome DevTools Protocol (CDP).

2. **0–100 Weighted Scoring Rubric**:
   - **Skill Match (30 pts)**: Overlap of languages, frameworks, databases, and core concepts.
   - **Experience Match (25 pts)**: Targeted for entry-level / early-career (0–2 years get full 25 pts). Disqualifying keywords (senior, lead, architect) set to 0 pts.
   - **Role Relevance (25 pts)**: Target roles (SDE-1, Junior SE, Backend Developer, Java, Python, Full Stack, Graduate Engineer Trainee).
   - **Location & Work Mode (10 pts)**: Remote, Hybrid, India (Bengaluru, Hyderabad, Pune, Delhi-NCR, Mumbai).
   - **Eligibility & Authorization (10 pts)**: CS/Engineering degree match, work authorization in India.
   - **Thresholds**:
     - `>= 70`: Auto-apply eligible
     - `55 – 69`: Review Queue (`Under Review`)
     - `< 55`: Skip (`Skipped`)

3. **Persistent Application Tracker & Deduplication**:
   - Persistent tracking by `(Company, Role, Job URL, Job ID)`.
   - Real-time Markdown table export in [`APPLICATIONS_TRACKER.md`](APPLICATIONS_TRACKER.md).
   - Machine-readable database in `data/tracker.json`.

4. **Human-in-the-Loop Safety & Question Presets**:
   - **Zero CAPTCHA bypasses**: Automatically pauses on CAPTCHAs, Cloudflare challenges, or OTP/2FA prompts and alerts you to complete them in the visible Chrome window.
   - Automatically answers standard screening questions (Notice Period: 0 days, Current CTC: 0, Expected CTC: 8.5 LPA, Total Experience: 0.5 Yrs) using approved candidate presets.
   - Pauses on unapproved, sensitive, or ambiguous questions.

5. **Genesis Web Dashboard UI**:
   - Modern, editorial precision interface conforming to `design.md`.
   - Real-time Server-Sent Events (SSE) logs, metric cards, sound effects, and interactive tracker table.

---

## 📂 Project Structure

```
shine-v.2/
├── config/
│   ├── candidate_profile.json   # Your skills, education, links, preferences & approved answers
│   └── settings.json            # Chrome profile config, scoring weights & search parameters
├── data/
│   ├── tracker.json             # Persistent application state database
│   └── latest_run_report.md     # Auto-generated end-of-run report
├── resumes/                     # Directory for your resume PDF files
├── src/
│   ├── cdp_client.py            # Direct Chrome DevTools Protocol client with CAPTCHA detection
│   ├── scorer.py                # 0-100 Job scoring and evaluation engine
│   ├── tracker.py               # Application tracker and deduplication manager
│   ├── reporter.py              # End-of-run summary generator
│   ├── notifier.py              # Desktop notifications
│   ├── web_server.py            # Genesis Dashboard REST & SSE web server
│   ├── web/                     # Genesis UI frontend assets (HTML, CSS, JS, audio)
│   │   ├── index.html
│   │   ├── style.css
│   │   ├── app.js
│   │   └── sounds/
│   └── platforms/
│       └── shine.py             # Shine.com discovery, extraction, scoring & apply workflow
├── APPLICATIONS_TRACKER.md      # Human-readable markdown application tracker table
├── main.py                      # CLI entry point
├── design.md                    # Genesis design system specification
└── README.md
```

---

## 🚀 Quickstart & Usage

### 1. Launch Chrome with Automation Profile
To open your dedicated `Automation` Chrome window with remote debugging enabled:
```bash
python3 main.py launch-browser
```
*(Log in once to Shine.com in this window if you haven't already; all cookies and sessions persist across runs).*

### 2. Run Job Automation
Run the autonomous discovery, scoring, and application loop for Shine.com:
```bash
python3 main.py run
```
With custom search keywords:
```bash
python3 main.py run --keywords "Software Engineer" "Python Developer" "SDE 1"
```
Test mode (processes exactly 1 target job for verification):
```bash
python3 main.py run --test
```

### 3. Launch Genesis Web Dashboard
Open the live dashboard at `http://127.0.0.1:8002/`:
```bash
python3 main.py ui --port 8002
```

### 4. Apply to Under Review / Failed Jobs
Directly apply to all jobs currently in the review queue or failed states:
```bash
python3 main.py retry-pending
```
*(Or click the **Apply to Review / Failed** button directly in the Genesis Web Dashboard).*

### 5. Check Application Status & Tracker
View real-time metrics and breakdown of all applications:
```bash
python3 main.py status
```
Or view the live markdown table in [`APPLICATIONS_TRACKER.md`](APPLICATIONS_TRACKER.md).

### 6. Inspect Review Queue
Inspect jobs scored between 55 and 69 that are waiting for your review:
```bash
python3 main.py review
```

### 7. Test Scoring Engine
Test the scoring calculation against a sample SDE-1 job:
```bash
python3 main.py test-score
```
