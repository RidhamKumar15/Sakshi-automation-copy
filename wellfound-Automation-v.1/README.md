# 🤖 Job Application Automation Agent

An autonomous, precision job-search and application assistant built specifically for entry-level Software Engineering roles with persistent Chrome profile reuse, automated 0-100 scoring, duplicate prevention, and human-in-the-loop safety controls.

---

## 🎯 Key Features & Rules

1. **Dedicated Chrome Profile Reuse**:
   - Uses your dedicated `Automation` profile (`Profile 4` in `~/.config/google-chrome`).
   - Reuses existing authenticated sessions, cookies, and local storage (e.g. Wellfound, LinkedIn).
   - Never touches your personal profile (`Default`).
   - Runs in visible, headed browser mode.

2. **0-100 Weighted Scoring Rubric**:
   - **Skill Match (30 pts)**: Overlap of languages, frameworks, databases, and core concepts.
   - **Experience Match (25 pts)**: Targeted for entry-level / early-career (0-2 years get full 25 pts).
   - **Role Relevance (25 pts)**: Target roles (SDE-1, Junior SE, Backend Developer, Java, Python, Full Stack).
   - **Location & Work Mode (10 pts)**: Remote, Hybrid, India (Bengaluru, Hyderabad, Pune, Delhi-NCR, Mumbai).
   - **Eligibility & Authorization (10 pts)**: CS/Engineering degree match, work authorization.
   - **Thresholds**:
     - `>= 70`: Auto-apply eligible
     - `55 - 69`: Review Queue (`Under Review`)
     - `< 55`: Skip (`Skipped`)

3. **Persistent Application Tracker & Deduplication**:
   - Persistent tracking by `(Company, Role, Job URL, Job ID)`.
   - Real-time Markdown table export in [`APPLICATIONS_TRACKER.md`](file:///run/media/ridhamverma/R/Sakshi_AI/wellfound-Automation-v.1/APPLICATIONS_TRACKER.md).
   - Machine-readable database in `data/tracker.json`.

4. **Human-in-the-Loop Safety**:
   - **Zero CAPTCHA bypasses**: Automatically pauses on CAPTCHAs, Cloudflare challenges, or OTP/2FA prompts and alerts you to complete them in the visible Chrome window.
   - Resumes seamlessly from the exact step once cleared.
   - Never fabricates qualifications or guesses ambiguous/salary fields.

---

## 📂 Project Structure

```
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
│   └── platforms/
│       ├── wellfound.py         # Wellfound (AngelList) discovery & application workflow
│       └── linkedin.py          # LinkedIn Easy Apply workflow
├── APPLICATIONS_TRACKER.md      # Human-readable markdown application tracker table
├── main.py                      # CLI entry point
└── README.md
```

---

## 🚀 Quickstart & Usage

### 1. Launch Chrome with Automation Profile
To open your dedicated `Automation` Chrome window with remote debugging enabled:
```bash
python3 main.py launch-browser
```
*(Log in once to Wellfound / LinkedIn in this window if you haven't already; all cookies and sessions persist across runs).*

### 2. Run Job Automation
Run the discovery, scoring, and application loop for Wellfound:
```bash
python3 main.py run --source wellfound
```

Or for LinkedIn:
```bash
python3 main.py run --source linkedin
```

Or all sources:
```bash
python3 main.py run --source all
```

### 3. Check Application Status & Tracker
View real-time metrics and breakdown of all applications:
```bash
python3 main.py status
```
Or view the live markdown table in [`APPLICATIONS_TRACKER.md`](file:///run/media/ridhamverma/R/Sakshi_AI/wellfound-Automation-v.1/APPLICATIONS_TRACKER.md).

### 4. Inspect Review Queue
Inspect jobs scored between 55 and 69 that are waiting for your review:
```bash
python3 main.py review
```

### 5. Test Scoring Engine
Test the scoring calculation against a sample SDE-1 job:
```bash
python3 main.py test-score
```

---

## ⚙️ Customization

- **Profile & Resume**: Edit [`config/candidate_profile.json`](file:///run/media/ridhamverma/R/Sakshi_AI/wellfound-Automation-v.1/config/candidate_profile.json) to update your skills, project links, or approved salary expectations. Place your resume PDF in `resumes/`.
- **Search Keywords & Thresholds**: Edit [`config/settings.json`](file:///run/media/ridhamverma/R/Sakshi_AI/wellfound-Automation-v.1/config/settings.json) to tweak target roles, locations, or score thresholds.
