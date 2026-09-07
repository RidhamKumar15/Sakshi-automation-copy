import asyncio
import json
import logging
import os
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from src.cdp_client import ChromeCDPClient
from src.scorer import JobScorer
from src.tracker import ApplicationTracker

logger = logging.getLogger("NaukriPlatform")


class NaukriAutomation:
    """
    Automates job search, scoring, and application on Naukri.com.
    Uses the dedicated Chrome automation profile via CDP.
    Adheres strictly to the Genesis reference architecture.
    """

    BASE_URL = "https://www.naukri.com"

    def __init__(
        self,
        cdp_client: ChromeCDPClient,
        scorer: JobScorer,
        tracker: ApplicationTracker,
        candidate_profile: Dict[str, Any],
        settings: Dict[str, Any]
    ):
        self.cdp = cdp_client
        self.scorer = scorer
        self.tracker = tracker
        self.profile = candidate_profile
        self.settings = settings
        self.max_applications = settings.get("application", {}).get("max_applications_per_run", 25)
        self.naukri_cfg = settings.get("sources", {}).get("naukri", {})

    async def search_and_process_jobs(self, keywords: Optional[List[str]] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        """
        Main sequential execution flow:
        1. Navigate to Naukri search page for each keyword with experience & location filters.
        2. Verify login & check CAPTCHA / OTP challenges.
        3. Extract job listings one by one from the search results.
        4. Open full job description for comprehensive requirement evaluation.
        5. Score each job using the 0-100 system.
        6. Check for duplicates in persistent tracker.
        7. Apply directly, answer questionnaires, or queue for review.
        8. Verify live submission truth before updating tracker.
        """
        max_to_apply = limit if limit is not None else self.max_applications

        results_summary = {
            "source": "Naukri",
            "jobs_found": 0,
            "jobs_evaluated": 0,
            "jobs_skipped": 0,
            "jobs_under_review": 0,
            "applications_submitted": 0,
            "applications_manual_action": 0,
            "duplicates_ignored": 0,
            "errors": [],
            "attention_urls": []
        }

        search_terms = keywords or self.naukri_cfg.get("search_keywords", [
            "Software Engineer",
            "Backend Developer",
            "Python Developer",
            "Java Developer",
            "SDE 1",
            "Full Stack Developer"
        ])

        freshness_tiers = [1, 3, 7, 15]
        tier_names = {1: "Today / Last 24 Hours", 3: "Last 3 Days", 7: "Last 7 Days", 15: "Last 15 Days (Max Limit)"}

        for keyword in search_terms:
            if results_summary["applications_submitted"] >= max_to_apply:
                print(f"[Naukri] 🛑 Reached max applications limit ({max_to_apply}). Stopping search.")
                break

            # Search progressively by freshness: Today -> 3 Days -> 7 Days -> 15 Days
            for freshness in freshness_tiers:
                if results_summary["applications_submitted"] >= max_to_apply:
                    break

                tier_label = tier_names.get(freshness, f"Last {freshness} Days")
                print(f"\n[Naukri] 🔍 Searching jobs for keyword: '{keyword}' [Freshness Tier: {tier_label}]...")
                search_urls = self._build_search_urls(keyword, freshness=freshness)

                for search_url in search_urls:
                    if results_summary["applications_submitted"] >= max_to_apply:
                        break

                    print(f"[Naukri] 🌐 Navigating to search URL: {search_url}")
                    await self.cdp.navigate(search_url, wait_seconds=4.0)

                    # 1. Security / CAPTCHA / Login Check
                    sec_status = await self.cdp.check_for_security_challenge()
                    if sec_status.get("has_challenge"):
                        if sec_status.get("login_required"):
                            await self.cdp.pause_and_wait_for_human("Naukri login required. Please log into your Naukri account in Chrome.")
                        else:
                            await self.cdp.pause_and_wait_for_human("CAPTCHA or security challenge detected on Naukri.")

                    # 2. Smooth scroll to load lazy-rendered job tuples
                    for _ in range(3):
                        await self.cdp.evaluate("window.scrollBy(0, 600);")
                        await asyncio.sleep(1.0)

                    # 3. Extract job cards from page with DOM-level metadata
                    extracted_jobs = await self._extract_job_listings_from_page()
                    if not extracted_jobs:
                        # Retry scroll to bottom and wait
                        await self.cdp.evaluate("window.scrollTo(0, document.body.scrollHeight / 2);")
                        await asyncio.sleep(2.0)
                        extracted_jobs = await self._extract_job_listings_from_page()

                    results_summary["jobs_found"] += len(extracted_jobs)
                    print(f"[Naukri] Extracted {len(extracted_jobs)} job listings from search (Tier: {tier_label}).")

                    for job in extracted_jobs:
                        if results_summary["applications_submitted"] >= max_to_apply:
                            break

                        results_summary["jobs_evaluated"] += 1
                        comp = job.get("company", "Unknown")
                        title = job.get("title", "Unknown")
                        url = job.get("job_url", "")
                        job_id = job.get("job_id", "")
                        posted_date = job.get("posted_date", "")

                        print(f"\n------------------------------------------------------------")
                        print(f"📌 [Naukri] Evaluating: {title} at {comp}")
                        print(f"   URL: {url} | Posted: {posted_date or 'Recent'}")

                        # A. Freshness Verification: Max 15 Days
                        age_days = self._parse_posting_age_days(posted_date)
                        if age_days > 15:
                            print(f"   ⏳ [POSTING AGE FILTER] Job is {age_days} days old ('{posted_date}'). Exceeds 15-day limit. Skipping.")
                            results_summary["jobs_skipped"] += 1
                            continue

                        # B. DOM-level Check: Already Applied
                        if job.get("is_already_applied"):
                            print(f"   ⏭️ [CARD DOM CHECK] Already applied on Naukri ('{comp}' - '{title}'). Skipping navigation.")
                            results_summary["duplicates_ignored"] += 1
                            self.tracker.record_job(
                                company=comp,
                                role=title,
                                source="Naukri",
                                job_url=url,
                                match_score=0,
                                status="Already Applied",
                                notes="Already applied (detected from search card DOM badge)",
                                job_id=job_id
                            )
                            continue

                        # C. Duplicate Check in Tracker
                        is_dup, prev_record = self.tracker.is_duplicate(comp, title, url, job_id)
                        if is_dup and prev_record:
                            prev_status = prev_record.get("status", "")
                            print(f"   ⏭️ Skipping duplicate: Already tracked as '{prev_status}' on {prev_record.get('date')}")
                            results_summary["duplicates_ignored"] += 1
                            continue

                        # D. DOM-level Check: External Company Website Apply
                        if job.get("is_external_portal"):
                            print(f"   🏢 [CARD DOM CHECK] External company portal application detected at card DOM level ('{comp}' - '{title}'). Flagging as Under Review without opening URL.")
                            results_summary["jobs_under_review"] += 1
                            results_summary["attention_urls"].append(url)
                            self.tracker.record_job(
                                company=comp,
                                role=title,
                                source="Naukri",
                                job_url=url,
                                match_score=65,
                                status="Under Review",
                                notes="External company website redirect (detected on card DOM - requires manual company portal apply)",
                                job_id=job_id
                            )
                            continue

                        # E. Deep JD Extraction (only for direct Naukri application jobs)
                        if url and len(job.get("description", "")) < 200:
                            print(f"   📖 Fetching full job description for deep evaluation...")
                            full_jd = await self._fetch_full_job_details(url)
                            if full_jd:
                                job.update(full_jd)

                        # F. Score Job
                        score_res = self.scorer.evaluate(job)
                        total_score = score_res["total_score"]
                        decision = score_res["decision"]
                        print(f"   📊 Match Score: {total_score}/100 -> Decision: {decision}")

                        # G. Handle by Decision
                        min_auto = self.scorer.thresholds.get("auto_apply_min_score", 30)
                        min_review = self.scorer.thresholds.get("review_queue_min_score", 15)

                        if decision == "SKIP":
                            print(f"   ❌ Score {total_score} < {min_review}. Skipping job.")
                            results_summary["jobs_skipped"] += 1
                            self.tracker.record_job(
                                company=comp,
                                role=title,
                                source="Naukri",
                                job_url=url,
                                match_score=total_score,
                                status="Skipped",
                                notes=f"Low match score ({total_score}/100 < {min_review}).",
                                job_id=job_id
                            )
                            continue

                        elif decision == "REVIEW_QUEUE":
                            print(f"   📝 Score {total_score} in review range ({min_review}-{min_auto - 1}). Adding to Review Queue.")
                            results_summary["jobs_under_review"] += 1
                            results_summary["attention_urls"].append(url)
                            self.tracker.record_job(
                                company=comp,
                                role=title,
                                source="Naukri",
                                job_url=url,
                                match_score=total_score,
                                status="Under Review",
                                notes=f"Review range match ({total_score}/100). Added to review queue.",
                                job_id=job_id
                            )
                            continue

                        elif decision == "AUTO_APPLY":
                            print(f"   🚀 Match score {total_score} >= {min_auto} (Threshold: 30%). Initiating application workflow...")
                            self.tracker.record_job(
                                company=comp,
                                role=title,
                                source="Naukri",
                                job_url=url,
                                match_score=total_score,
                                status="Applying",
                                notes=f"Match ({total_score}/100 >= {min_auto}). Applying now.",
                                job_id=job_id
                            )

                            try:
                                app_result = await self._apply_to_job(job, score_res)
                                job_filled_answers = app_result.get("filled_answers", [])

                                if app_result["status"] == "Submitted":
                                    results_summary["applications_submitted"] += 1
                                    self.tracker.update_status(url, "Submitted", notes=f"Applied on Naukri ({comp}, Score: {total_score})", filled_answers=job_filled_answers)

                                    if limit and results_summary["applications_submitted"] >= limit:
                                        print(f"\n🎉 [TARGET LIMIT REACHED] Successfully submitted {results_summary['applications_submitted']} application(s)!")
                                        from src.notifier import notify_completion
                                        manual_action = results_summary.get("applications_manual_action", 0) + results_summary.get("jobs_under_review", 0)
                                        notify_completion(
                                            results_summary["applications_submitted"],
                                            results_summary["jobs_evaluated"],
                                            manual_action_count=manual_action
                                        )
                                        return results_summary

                                elif app_result["status"] == "Waiting for Input":
                                    results_summary["applications_manual_action"] += 1
                                    results_summary["attention_urls"].append(url)
                                    self.tracker.update_status(url, "Waiting for Input", notes=app_result.get("notes", "Requires manual user input"), filled_answers=job_filled_answers)

                                elif app_result["status"] == "Under Review":
                                    results_summary["jobs_under_review"] += 1
                                    results_summary["attention_urls"].append(url)
                                    self.tracker.update_status(url, "Under Review", notes=app_result.get("notes", "Application under review"), filled_answers=job_filled_answers)

                                elif app_result["status"] == "Already Applied":
                                    results_summary["duplicates_ignored"] += 1
                                    self.tracker.update_status(url, "Already Applied", notes="Already applied on Naukri", filled_answers=job_filled_answers)

                                elif app_result["status"] == "Closed":
                                    self.tracker.update_status(url, "Closed", notes="Job listing no longer active on Naukri")

                                elif app_result["status"] == "Skipped":
                                    results_summary["jobs_skipped"] += 1
                                    self.tracker.update_status(url, "Skipped", notes=app_result.get("notes", "Skipped constraint"))

                                else:
                                    results_summary["errors"].append(f"{comp} - {title}: {app_result.get('notes', 'Failed')}")
                                    self.tracker.update_status(url, "Failed", notes=app_result.get("notes", "Application process failed"))

                            except Exception as job_err:
                                print(f"   ⚠️ Error processing job '{title}' at '{comp}': {job_err}")
                                results_summary["errors"].append(f"{comp} - {title}: {job_err}")
                                self.tracker.update_status(url, "Failed", notes=f"Error: {job_err}")

                        # Respectful delay between jobs
                        await asyncio.sleep(2.0)

                    # If this freshness tier processed listings and limit is reached
                    if limit and results_summary["applications_submitted"] >= limit:
                        return results_summary

        return results_summary

    def _parse_posting_age_days(self, posted_text: str) -> int:
        """
        Parses posting date text (e.g. 'Just Now', 'Today', '1 Day Ago', '3 Days Ago', '15 Days Ago', '30+ Days Ago')
        into an integer representing approximate days ago.
        """
        if not posted_text:
            return 0
        t = posted_text.lower().strip()
        if any(w in t for w in ["just now", "few hours", "hour", "today", "moment", "new"]):
            return 0
        if "day" in t or "d ago" in t:
            m = re.search(r"(\d+)", t)
            if m:
                return int(m.group(1))
            return 1
        if "month" in t or "year" in t or "30+" in t or "30d" in t:
            return 30
        return 0

    def _build_search_urls(self, keyword: str, freshness: int = 1) -> List[str]:
        """
        Constructs canonical Naukri search URLs with experience, freshness, and sort order (newest first).
        freshness: 1 (Today/24h), 3 (Last 3 days), 7 (Last 7 days), 15 (Last 15 days)
        """
        kw_clean = keyword.lower().strip()
        slug_map = {
            "software engineer": "software-engineer",
            "software developer": "software-developer",
            "backend developer": "backend-developer",
            "python developer": "python-developer",
            "java developer": "java-developer",
            "sde 1": "sde-1",
            "sde-1": "sde-1",
            "full stack developer": "full-stack-developer",
            "data engineer": "data-engineer",
            "ai/ml engineer": "machine-learning-engineer",
            "junior software engineer": "junior-software-engineer",
            "associate software engineer": "associate-software-engineer",
            "graduate engineer trainee": "graduate-engineer-trainee"
        }

        filters = self.naukri_cfg.get("search_filters", {})
        exp = filters.get("experience", "0")

        urls = []
        if kw_clean in slug_map:
            slug = slug_map[kw_clean]
            urls.append(f"https://www.naukri.com/{slug}-jobs-in-india?experience={exp}&freshness={freshness}&sort=f")

        # Fallback parameterized search with sort by date and freshness filter
        encoded_kw = urllib.parse.quote(keyword)
        urls.append(f"https://www.naukri.com/jobs-in-india?k={encoded_kw}&experience={exp}&freshness={freshness}&sort=f")
        return urls

    async def _extract_job_listings_from_page(self) -> List[Dict[str, Any]]:
        """
        Parses visible job tuples from Naukri search results using multi-strategy extraction:
        1. Standard SRP JobTuple wrappers (`.srp-jobtuple-wrapper`, `article.jobTuple`, `div.cust-job-tuple`)
        2. Direct links with `naukri.com/job-listings`
        3. Universal DOM card container traversal
        """
        js = r"""
        (() => {
            const listings = [];
            const seenUrls = new Set();

            // Strategy 1: Find all job tuple container elements
            const cardSelectors = [
                '.srp-jobtuple-wrapper',
                'article.jobTuple',
                'div.cust-job-tuple',
                'div.srp-tuple',
                'div[class*="jobTuple"]',
                'div[class*="job-tuple"]',
                'div[class*="tupleWrapper"]',
                'div[class*="srp-jobtuple"]'
            ];

            let tupleElements = Array.from(document.querySelectorAll(cardSelectors.join(', ')));

            // Strategy 2: If no tuples found by class, find all job-listings links and their parent card
            if (tupleElements.length === 0) {
                const links = Array.from(document.querySelectorAll('a[href*="job-listings"]'));
                const containers = new Set();
                links.forEach(l => {
                    let p = l.closest('article, div[class*="tuple"], div[class*="card"], div[class*="row"], div[class*="container"]');
                    if (!p) p = l.parentElement?.parentElement;
                    if (p) containers.add(p);
                });
                tupleElements = Array.from(containers);
            }

            tupleElements.forEach((card, idx) => {
                try {
                    // Extract Title & Job Link
                    const titleEl = card.querySelector('a.title, [class*="title"], a[href*="job-listings"], h2 a, h3 a');
                    if (!titleEl) return;

                    const title = (titleEl.innerText || titleEl.textContent || '').trim();
                    const jobUrl = titleEl.href || '';
                    if (!jobUrl || seenUrls.has(jobUrl)) return;

                    // Extract Company Name
                    let company = '';
                    const compEl = card.querySelector('a.comp-name, [class*="comp-name"], [class*="companyName"], [class*="company"], a[class*="subTitle"]');
                    if (compEl) {
                        company = (compEl.innerText || compEl.textContent || '').trim();
                    }

                    // Extract Experience
                    let expText = '';
                    const expEl = card.querySelector('span.expwdth, [class*="expwdth"], [class*="experience"], [class*="exp-wrap"], span[class*="exp"]');
                    if (expEl) {
                        expText = (expEl.innerText || expEl.textContent || '').trim();
                    }

                    // Extract Location
                    let locationText = '';
                    const locEl = card.querySelector('span.locWdth, [class*="locWdth"], [class*="location"], span[class*="loc"]');
                    if (locEl) {
                        locationText = (locEl.innerText || locEl.textContent || '').trim();
                    }

                    // Extract Salary
                    let salaryText = '';
                    const salEl = card.querySelector('span.sal-wrap, span.sal, [class*="salary"], span[class*="sal"]');
                    if (salEl) {
                        salaryText = (salEl.innerText || salEl.textContent || '').trim();
                    }

                    // Extract Skills / Tags
                    const skillEls = Array.from(card.querySelectorAll('ul.tags-gt li, [class*="tag"], [class*="skill"], [class*="chip"]'));
                    const skills = skillEls.map(s => (s.innerText || '').trim()).filter(Boolean);

                    // Extract Posting Date
                    let postedDate = '';
                    const dateEl = card.querySelector('span.date, [class*="postedDate"], [class*="job-post-day"], [class*="date"]');
                    if (dateEl) {
                        postedDate = (dateEl.innerText || dateEl.textContent || '').trim();
                    }

                    // Extract Job ID
                    let jobId = card.getAttribute('data-job-id') || card.getAttribute('id') || '';
                    if (!jobId) {
                        const idMatch = jobUrl.match(/job-listings-.*-(\d+)/) || jobUrl.match(/(\d{8,})/);
                        jobId = idMatch ? idMatch[1] : `naukri-${idx}`;
                    }

                    // Extract Description snippet
                    const descEl = card.querySelector('span.job-desc, [class*="job-desc"], [class*="jobDescription"], [class*="snippet"]');
                    const desc = descEl ? (descEl.innerText || '').trim() : (card.innerText || '').substring(0, 400);

                    // Determine Remote Status and Apply Type from Card DOM
                    const fullCardText = (card.innerText || '').toLowerCase();
                    let remoteStatus = 'On-site';
                    if (fullCardText.includes('remote') || fullCardText.includes('work from home') || fullCardText.includes('wfh')) {
                        remoteStatus = 'Remote';
                    } else if (fullCardText.includes('hybrid')) {
                        remoteStatus = 'Hybrid';
                    }

                    // DOM-Level Check: External Company Portal Apply vs Direct Naukri Apply
                    const isExternalApply = (
                        fullCardText.includes('apply on company site') ||
                        fullCardText.includes('apply on company website') ||
                        fullCardText.includes('company site') ||
                        fullCardText.includes('company website') ||
                        fullCardText.includes('redirects to company') ||
                        fullCardText.includes('apply externally') ||
                        fullCardText.includes('apply on employer site') ||
                        Boolean(card.querySelector('[class*="external-apply"], [class*="company-site"], [class*="apply-type-external"], [class*="redirect-apply"], a[href*="apply-on-company"]'))
                    );

                    // DOM-Level Check: Already Applied Badge
                    const isAlreadyApplied = (
                        fullCardText.includes('already applied') ||
                        fullCardText.includes('applied on ') ||
                        Boolean(card.querySelector('[class*="applied-badge"], [class*="already-applied"]'))
                    );

                    const cleanTitle = title.split('\n')[0].trim();
                    const invalidTitles = ['sign in', 'register', 'login', 'jobs by location', 'search', 'services'];

                    if (cleanTitle && cleanTitle.length > 2 && !invalidTitles.includes(cleanTitle.toLowerCase())) {
                        seenUrls.add(jobUrl);
                        listings.push({
                            title: cleanTitle,
                            company: company || 'Hiring Company',
                            job_url: jobUrl,
                            job_id: jobId,
                            location: locationText || 'India',
                            remote_status: remoteStatus,
                            salary: salaryText,
                            experience_required: expText,
                            required_skills: skills,
                            description: desc,
                            posted_date: postedDate,
                            is_external_portal: isExternalApply,
                            is_already_applied: isAlreadyApplied,
                            apply_type: isExternalApply ? 'external_company_portal' : 'naukri_direct',
                            source: 'Naukri'
                        });
                    }
                } catch (err) {}
            });

            return listings;
        })()
        """
        try:
            listings = await self.cdp.evaluate(js)
            return listings or []
        except Exception as e:
            print(f"[Naukri] Error extracting job cards: {e}")
            return []

    async def _fetch_full_job_details(self, job_url: str) -> Optional[Dict[str, Any]]:
        """
        Navigates to the job details page to extract the complete JD text and requirements.
        """
        try:
            await self.cdp.navigate(job_url, wait_seconds=3.0)

            # Check for security challenge
            sec = await self.cdp.check_for_security_challenge()
            if sec.get("has_challenge"):
                if sec.get("login_required"):
                    await self.cdp.pause_and_wait_for_human("Naukri login required to view job details.")
                else:
                    await self.cdp.pause_and_wait_for_human("CAPTCHA/Verification detected on job page.")

            js = r"""
            (() => {
                const details = {};

                // 1. Company Name
                const compEl = document.querySelector('a.styles_amb-comp-name__..., a[class*="comp-name"], div[class*="company-name"], a[href*="/company/"]');
                if (compEl && compEl.innerText.trim()) {
                    details.company = compEl.innerText.trim();
                }

                // 2. Title
                const titleEl = document.querySelector('h1.styles_jd-header-title__..., h1[class*="header-title"], h1');
                if (titleEl && titleEl.innerText.trim()) {
                    details.title = titleEl.innerText.trim();
                }

                // 3. Full Job Description text
                const jdSelectors = [
                    'section.styles_job-desc-container__...',
                    'div.styles_JDRelatedJob__...',
                    'div[class*="job-desc-container"]',
                    'div[class*="job-desc"]',
                    'section[class*="job-description"]',
                    'div.jd-container',
                    'div.dang-inner-html'
                ];

                for (const sel of jdSelectors) {
                    const el = document.querySelector(sel);
                    if (el && el.innerText.trim().length > 100) {
                        details.description = el.innerText.trim();
                        break;
                    }
                }

                if (!details.description) {
                    const mainContent = document.querySelector('main') || document.body;
                    details.description = mainContent.innerText ? mainContent.innerText.substring(0, 3000) : '';
                }

                // 4. Skills
                const skillEls = Array.from(document.querySelectorAll('div.styles_key-skill__... a, a.styles_chip__..., div[class*="key-skill"] a, div[class*="tags"] a, span[class*="chip"]'));
                if (skillEls.length > 0) {
                    details.required_skills = skillEls.map(s => s.innerText.trim()).filter(Boolean);
                }

                // 5. Experience requirement
                const expEl = document.querySelector('div.styles_jhc__exp__..., div[class*="experience"], span[class*="exp"]');
                if (expEl && expEl.innerText.trim()) {
                    details.experience_required = expEl.innerText.trim();
                }

                // 6. Location
                const locEl = document.querySelector('div.styles_jhc__loc__..., div[class*="location"], span[class*="loc"]');
                if (locEl && locEl.innerText.trim()) {
                    details.location = locEl.innerText.trim();
                }

                // 7. Salary
                const salEl = document.querySelector('div.styles_jhc__salary__..., div[class*="salary"], span[class*="sal"]');
                if (salEl && salEl.innerText.trim()) {
                    details.salary = salEl.innerText.trim();
                }

                return details;
            })()
            """
            return await self.cdp.evaluate(js)
        except Exception as e:
            print(f"[Naukri] Error fetching full JD: {e}")
            return None

    async def _apply_to_job(self, job: Dict[str, Any], score_res: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes safe, verified application on Naukri.com:
        1. Checks for closed or already applied status.
        2. Detects direct apply vs external company website redirect.
        3. Clicks Apply button with retry.
        4. Handles Naukri questionnaire forms (CTC, experience, notice period, skill assessments).
        5. Verifies tailored resume selection or upload.
        6. Confirms real-world submission truth before returning success.
        """
        job_url = job.get("job_url", "")
        comp = job.get("company", "Hiring Company")
        title = job.get("title", "Software Engineer")

        if job_url:
            await self.cdp.navigate(job_url, wait_seconds=3.0)

        # 0. Check for Login / Security Challenge
        sec = await self.cdp.check_for_security_challenge()
        if sec.get("has_challenge"):
            if sec.get("login_required"):
                cleared = await self.cdp.pause_and_wait_for_human("Naukri login required. Please log into your Naukri account.")
            else:
                cleared = await self.cdp.pause_and_wait_for_human("CAPTCHA or security challenge during Naukri application.")
            if not cleared:
                return {"status": "Waiting for Input", "notes": "Timed out waiting for manual verification"}

        # 1. Page Info & State Check
        page_info = await self.cdp.evaluate(r"""
        (() => {
            const bodyText = document.body ? document.body.innerText.toLowerCase() : '';

            // Check if job is expired or closed
            const isClosed = (
                bodyText.includes('this job is no longer available') ||
                bodyText.includes('this job has expired') ||
                Boolean(document.querySelector('[class*="job-expired"], [class*="expired-job"]'))
            );

            // Check if already applied
            const appliedIndicators = Array.from(document.querySelectorAll('button, a, div, span')).filter(el => {
                const t = (el.innerText || '').trim().toLowerCase();
                return t === 'already applied' || t.startsWith('applied on') || t === 'application submitted' || t === 'you have applied';
            });
            const isApplied = appliedIndicators.length > 0;

            // Detect Apply Buttons
            const buttons = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="button"]')).filter(el => {
                const inNav = el.closest('nav, header, footer, aside, [class*="sidebar"]');
                return !inNav;
            });

            const directApplyBtn = buttons.find(b => {
                const t = (b.innerText || '').trim().toLowerCase();
                const cls = (typeof b.className === 'string' ? b.className : '').toLowerCase();
                const id = (b.id || '').toLowerCase();
                if (t.includes('applied')) return false;
                return (
                    t === 'apply' ||
                    t === 'apply now' ||
                    t === 'apply on naukri' ||
                    t === 'i am interested' ||
                    t === 'quick apply' ||
                    t === 'easy apply' ||
                    id.includes('apply') ||
                    cls.includes('apply-button') ||
                    cls.includes('applybtn')
                ) && !t.includes('company site') && !t.includes('website');
            });

            const externalApplyBtn = buttons.find(b => {
                const t = (b.innerText || '').trim().toLowerCase();
                return (
                    t.includes('apply on company site') ||
                    t.includes('apply on website') ||
                    t.includes('company website')
                );
            });

            return {
                isClosed: isClosed,
                isApplied: isApplied,
                hasDirectApply: Boolean(directApplyBtn),
                hasExternalApply: Boolean(externalApplyBtn),
                externalUrl: externalApplyBtn && externalApplyBtn.href ? externalApplyBtn.href : ''
            };
        })()
        """)

        if page_info and page_info.get("isClosed"):
            print(f"[Naukri] 🔒 Job '{title}' at '{comp}' is closed.")
            return {"status": "Closed", "notes": "Job closed on Naukri"}

        if page_info and page_info.get("isApplied"):
            print(f"[Naukri] ⏭️ Already applied to '{title}' at '{comp}'.")
            return {"status": "Already Applied", "notes": "Previously applied on Naukri"}

        if page_info and page_info.get("hasExternalApply") and not page_info.get("hasDirectApply"):
            ext_url = page_info.get("externalUrl") or job_url
            print(f"[Naukri] 🌐 External application detected for '{title}' at '{comp}': {ext_url}")
            return {"status": "Under Review", "notes": f"External application link: {ext_url}"}

        # 2. Locate and Click Apply Button
        print(f"[Naukri] 🖱️ Locating Apply button for '{title}' at '{comp}'...")
        apply_clicked = False

        for attempt in range(4):
            click_res = await self.cdp.evaluate(r"""
            (() => {
                const buttons = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]')).filter(el => {
                    const inNav = el.closest('nav, header, footer, aside');
                    return !inNav;
                });

                const applyBtn = buttons.find(b => {
                    const t = (b.innerText || '').trim().toLowerCase();
                    const cls = (typeof b.className === 'string' ? b.className : '').toLowerCase();
                    const id = (b.id || '').toLowerCase();
                    if (t.includes('applied')) return false;
                    return (
                        t === 'apply' ||
                        t === 'apply now' ||
                        t === 'apply on naukri' ||
                        t === 'i am interested' ||
                        t === 'quick apply' ||
                        t === 'easy apply' ||
                        id.includes('apply') ||
                        cls.includes('apply-button') ||
                        cls.includes('applybtn')
                    );
                });

                if (applyBtn) {
                    applyBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    ['pointerdown', 'mousedown', 'mouseup', 'click'].forEach(evt => {
                        applyBtn.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                    });
                    if (typeof applyBtn.click === 'function') applyBtn.click();
                    return { clicked: true, text: applyBtn.innerText.trim() };
                }
                return { clicked: false };
            })()
            """)

            if click_res and click_res.get("clicked"):
                apply_clicked = True
                print(f"[Naukri] 🎯 Clicked '{click_res.get('text', 'Apply')}' button successfully!")
                break
            await asyncio.sleep(1.0)

        if not apply_clicked:
            print(f"[Naukri] ⚠️ Apply button not found for '{title}' at '{comp}'.")
            return {"status": "Waiting for Input", "notes": "Apply button not found on page"}

        await asyncio.sleep(2.0)

        # 3. Check for Security Challenge Post-Click
        sec_post = await self.cdp.check_for_security_challenge()
        if sec_post.get("has_challenge"):
            cleared = await self.cdp.pause_and_wait_for_human("Verification prompt during application.")
            if not cleared:
                return {"status": "Waiting for Input", "notes": "Timed out waiting for verification"}

        # 4. Wait for Application Flow / Side Panel to Initialize (Poll up to 6s)
        print(f"[Naukri] ⏳ Waiting for application flow / side panel to initialize...")
        flow_detected = "waiting"
        for poll_idx in range(6):
            await asyncio.sleep(1.0)
            state = await self.cdp.evaluate(r"""
            (() => {
                const text = (document.body ? document.body.innerText : '').toLowerCase();
                const url = window.location.href.toLowerCase();

                // 1. Rejection check
                if (text.includes('application was not accepted') || text.includes('incomplete information') || text.includes('mandatory questions') || text.includes('unable to submit')) {
                    return { type: "rejected", msg: "Application was not accepted: incomplete mandatory questions" };
                }

                // 2. Direct Success check
                if (text.includes('application submitted') || text.includes('applied successfully') || text.includes('successfully applied') || text.includes('thank you for your responses') || text.includes('thank you for applying')) {
                    return { type: "success" };
                }

                // 3. Chatbot / Side Panel Drawer check (excluding top nav)
                const drawer = Array.from(document.querySelectorAll('._chatBotContainer, .chatbot_Drawer, .chatbot_DrawerContentWrapper, .singleselect-radiobutton, div[class*="chat-container"], div.apply-message, div.apply-dialog, div[class*="apply-drawer"]')).find(el => {
                    const inNav = el.closest('header, nav, .nI-gNb-header, .nI-gNb-drawer');
                    return !inNav && el.offsetParent !== null && el.clientHeight > 80;
                });
                if (drawer) return { type: "chatbot" };

                // 4. Modal dialog check
                const modal = document.querySelector('[role="dialog"], [class*="Modal"], [class*="modal"], form.apply-form');
                if (modal && modal.offsetParent !== null && !modal.closest('.nI-gNb-header, .nI-gNb-drawer')) {
                    return { type: "modal" };
                }

                return { type: "waiting" };
            })()
            """)

            if state and state.get("type") in ["rejected", "success", "chatbot", "modal"]:
                flow_detected = state.get("type")
                break

        if flow_detected == "rejected":
            print(f"[Naukri] ❌ Application was rejected by Naukri (incomplete mandatory questions). Queueing for review.")
            return {"status": "Waiting for Input", "notes": "Application rejected on Naukri: incomplete mandatory questions"}

        if flow_detected == "success":
            target_resume = self._get_resume_for_job(job)
            print(f"[Naukri] 🎉 [Truth Check Passed]: Application officially submitted to {comp} on Naukri!")
            return {"status": "Submitted", "notes": f"Applied on Naukri ({target_resume['filename']})"}

        # 5. Handle Interactive Naukri Chatbot / Side Drawer Questionnaire Flow
        print(f"[Naukri] 🤖 Interacting with Naukri questionnaire side panel...")
        filled_answers: List[Dict[str, Any]] = []

        chatbot_res = await self._handle_naukri_chatbot_flow(job, score_res)
        if chatbot_res.get("filled_answers"):
            filled_answers.extend(chatbot_res["filled_answers"])

        if chatbot_res.get("completed"):
            target_resume = self._get_resume_for_job(job)
            print(f"[Naukri] 🎉 [Truth Check Passed]: Application officially submitted to {comp} on Naukri via chatbot flow!")
            return {
                "status": "Submitted",
                "notes": f"Applied on Naukri chatbot flow ({target_resume['filename']})",
                "filled_answers": filled_answers
            }
        elif chatbot_res.get("status") == "Under Review":
            return {
                "status": "Under Review",
                "notes": chatbot_res.get("notes", "Unmapped question encountered"),
                "filled_answers": filled_answers
            }

        # 6. Handle Traditional Questionnaire Modal / Application Form (if present)
        questionnaire_res = await self._handle_application_form(job, score_res)
        if questionnaire_res.get("filled_answers"):
            filled_answers.extend(questionnaire_res["filled_answers"])

        if questionnaire_res.get("status") in ["Waiting for Input", "Skipped", "Failed"]:
            questionnaire_res["filled_answers"] = filled_answers
            return questionnaire_res

        # 7. Handle Resume Upload / Selection if file input present
        target_resume = self._get_resume_for_job(job)
        print(f"[Naukri] 🎯 Target Resume: '{target_resume['filename']}' ({target_resume['category']})")

        has_file_input = await self.cdp.evaluate("Boolean(document.querySelector('input[type=\"file\"]'))")
        if has_file_input and os.path.exists(target_resume["path"]):
            print(f"[Naukri] 📎 Attaching tailored resume: {target_resume['path']}")
            await self.cdp.upload_file("input[type=\"file\"]", target_resume["path"])
            await asyncio.sleep(1.0)

        # 8. Submit Application Modal (Only inside active modal / dialog container)
        submit_res = await self.cdp.evaluate(r"""
        (() => {
            const modal = document.querySelector('[role="dialog"], [class*="Modal"], [class*="modal"], [class*="drawer"], div.drawer-wrapper, div.chatbot_Drawer');
            if (!modal || modal.offsetParent === null) {
                return { clicked: false, note: "no_modal" };
            }

            const buttons = Array.from(modal.querySelectorAll('.sendMsg, .send, .sendMsgbtn_container, button[type="submit"], button, a[role="button"]'));
            const submitBtn = buttons.find(b => {
                const t = (b.innerText || '').trim().toLowerCase();
                return (
                    t === 'save' ||
                    t === 'submit' ||
                    t === 'apply' ||
                    t === 'submit application' ||
                    t === 'save & apply' ||
                    t === 'save and apply' ||
                    t === 'send application' ||
                    t === 'continue' ||
                    t === 'confirm'
                ) && !t.includes('cancel') && !t.includes('close');
            });

            if (submitBtn) {
                ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(evt => {
                    submitBtn.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                });
                if (typeof submitBtn.click === 'function') submitBtn.click();
                return { clicked: true, text: submitBtn.innerText.trim() };
            }
            return { clicked: false, note: "no_submit_btn" };
        })()
        """)
        if submit_res and submit_res.get("clicked"):
            print(f"[Naukri] 🚀 Clicked '{submit_res.get('text')}' inside modal for {comp}...")

        # 9. Real-World Post-Submission Truth Verification Check
        print(f"[Naukri] 🔍 [Truth Verification]: Confirming live submission status on Naukri...")
        await asyncio.sleep(3.0)

        confirm_status = await self.cdp.evaluate(r"""
        (() => {
            const text = (document.body ? document.body.innerText : '').toLowerCase();
            const url = window.location.href.toLowerCase();

            // Explicit Rejection Check
            if (text.includes('application was not accepted') || text.includes('incomplete information') || text.includes('mandatory questions') || text.includes('unable to submit')) {
                return { isSubmitted: false, isRejected: true, note: "Application rejected by Naukri: incomplete mandatory questions" };
            }

            const hasSuccessMsg = (
                text.includes('application submitted') ||
                text.includes('successfully applied') ||
                text.includes('applied successfully') ||
                text.includes('you have applied') ||
                text.includes('application sent') ||
                text.includes('thank you for your responses') ||
                text.includes('thank you for applying')
            );

            const buttons = Array.from(document.querySelectorAll('button, a, div, span'));
            const hasAppliedBadge = buttons.some(b => {
                const t = (b.innerText || '').trim().toLowerCase();
                return t === 'already applied' || t.startsWith('applied on') || t === 'application submitted';
            });

            const isSubmitted = Boolean(hasSuccessMsg || hasAppliedBadge);

            return {
                isSubmitted: isSubmitted,
                isRejected: false,
                hasSuccessMsg: hasSuccessMsg,
                hasAppliedBadge: hasAppliedBadge
            };
        })()
        """)

        if confirm_status and confirm_status.get("isSubmitted"):
            print(f"[Naukri] 🎉 [Truth Check Passed]: Application officially submitted to {comp} on Naukri!")
            return {
                "status": "Submitted",
                "notes": f"Verified live on Naukri ({target_resume['filename']})",
                "filled_answers": filled_answers
            }
        elif confirm_status and confirm_status.get("isRejected"):
            print(f"[Naukri] ❌ [Truth Check Failed]: Application was rejected by Naukri: {confirm_status.get('note')}")
            return {
                "status": "Waiting for Input",
                "notes": confirm_status.get('note'),
                "filled_answers": filled_answers
            }
        else:
            print(f"[Naukri] ⚠️ [Truth Check Warning]: Application unconfirmed. Queueing for manual verification.")
            return {
                "status": "Waiting for Input",
                "notes": "Real-world truth check: submission unconfirmed or required additional input",
                "filled_answers": filled_answers
            }

    def _load_question_presets(self) -> Dict[str, Any]:
        """Loads central question presets and user-resolved answers."""
        presets_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "question_presets.json")
        if os.path.exists(presets_path):
            try:
                with open(presets_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "core_presets": {
                "current_location": "Gurugram",
                "current_ctc_lpa": "0",
                "work_authorization": "Yes",
                "requires_visa_sponsorship": "No",
                "gender": "Female",
                "willing_to_relocate": "Yes",
                "notice_period_days": "0",
                "base_experience_years": "1.0",
                "default_expected_ctc_lpa": "9.0"
            }
        }

    def _calculate_dynamic_expected_salary(self, job: Dict[str, Any]) -> str:
        """
        Determines the candidate's approved expected CTC in LPA.
        Uses candidate profile preferences (default 9.0 LPA, approved min 6.0 LPA).
        Never returns low or invalid figures (< 6.0 LPA) for engineering positions.
        """
        profile_expected = float(self.profile.get("preferences", {}).get("approved_salary_expectation_inr", {}).get("expected_lpa", 9.0))
        profile_min = float(self.profile.get("preferences", {}).get("approved_salary_expectation_inr", {}).get("min_lpa", 6.0))

        salary_field = str(job.get("salary", "")).lower()
        if salary_field and "not disclosed" not in salary_field and "unspecified" not in salary_field:
            range_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*(?:lpa|lacs|lakhs|lac|lakh|per annum|p\.a\.)', salary_field)
            if range_match:
                try:
                    min_val = float(range_match.group(1))
                    max_val = float(range_match.group(2))
                    if min_val >= profile_min and max_val >= profile_expected:
                        mid_upper = round(min_val + 0.65 * (max_val - min_val), 1)
                        return str(mid_upper)
                except Exception:
                    pass

        return str(profile_expected)

    def _determine_target_experience(self, job: Dict[str, Any]) -> str:
        """
        Determines experience value based on user rule:
        1.5 years default base experience. Never uses max values.
        """
        return "1.5"

    async def _handle_naukri_chatbot_flow(self, job: Dict[str, Any], score_res: Dict[str, Any]) -> Dict[str, Any]:
        """
        Interactively answers questions inside the live Naukri Chatbot / Side Panel Drawer turn by turn:
        1. Evaluates each question against central presets & dynamic JD rules.
        2. Selects matching radio options or quick answer chips.
        3. Fills text/number inputs using React native setters.
        4. Dispatches realistic coordinate mouse clicks to Save / Submit.
        5. Logs filled answers and flags unmapped questions for user review.
        """
        presets = self._load_question_presets()
        target_exp = self._determine_target_experience(job)
        expected_salary_lpa = self._calculate_dynamic_expected_salary(job)
        job_location = str(job.get("location", "Gurugram")).strip()
        comp = job.get("company", "")
        title = job.get("title", "")
        job_url = job.get("job_url", "")

        filled_answers: List[Dict[str, str]] = []

        context_rules = {
            "target_exp": target_exp,
            "expected_salary_lpa": str(expected_salary_lpa),
            "job_location": job_location,
            "current_location": "Gurugram",
            "current_ctc": "0",
            "notice_period": "0",
            "work_auth": "Yes",
            "visa": "No",
            "gender": "Female",
            "willing_to_relocate": "Yes",
            "custom_resolved": presets.get("custom_resolved_questions", {}),
            "question_mappings": presets.get("question_mappings", {})
        }

        for turn in range(15):
            await asyncio.sleep(1.5)

            bot_turn = await self.cdp.evaluate(f"""
            (async () => {{
                const rules = {json.dumps(context_rules)};

                // 1. Locate Chatbot container while strictly ignoring top header/navbar
                const drawer = Array.from(document.querySelectorAll('._chatBotContainer, .chatbot_Drawer, .chatbot_DrawerContentWrapper, div[class*="chat-container"], div.apply-message, div.apply-dialog, div[class*="apply-drawer"]')).find(el => {{
                    const inNav = el.closest('header, nav, .nI-gNb-header, .nI-gNb-drawer');
                    return !inNav && el.offsetParent !== null && el.clientHeight > 100;
                }});

                if (!drawer) {{
                    return {{ hasDrawer: false, isCompleted: false }};
                }}

                const drawerText = (drawer.innerText || '').toLowerCase();

                // 2. Check for Completed Status
                const isCompleted = (
                    drawerText.includes('thank you for your responses') ||
                    drawerText.includes('application submitted') ||
                    drawerText.includes('applied successfully') ||
                    drawerText.includes('successfully applied') ||
                    drawerText.includes('thank you for applying') ||
                    drawerText.includes('application sent') ||
                    drawerText.includes('you have applied') ||
                    Boolean(drawer.querySelector('[class*="success"], [class*="check-icon"], [class*="completed"]'))
                );

                if (isCompleted) {{
                    return {{ hasDrawer: true, isCompleted: true, action: "completed" }};
                }}

                // 3. User messages tracking to isolate the CURRENT turn and ignore previous turns
                const userMsgs = Array.from(drawer.querySelectorAll('.user-msg, .userMsg, [class*="user-msg"], [class*="userMsg"], div.user-message'));
                const lastUserMsg = userMsgs.length > 0 ? userMsgs[userMsgs.length - 1] : null;

                const isCurrentTurnElement = (el) => {{
                    if (!el || el.offsetParent === null) return false;
                    if (lastUserMsg) {{
                        const pos = el.compareDocumentPosition(lastUserMsg);
                        if (pos & Node.DOCUMENT_POSITION_FOLLOWING) {{
                            return false;
                        }}
                    }}
                    return true;
                }};

                // 4. Helper to extract the TRUE bot question prompt
                const getCleanBotQuestion = () => {{
                    const candidateEls = Array.from(drawer.querySelectorAll(
                        '.botMsg, .bot-msg, div[class*="bot-msg"], div[class*="botMsg"], .chatbot_BotMessage, div[class*="message"]:not([class*="user"]), div[class*="bot_msg"]'
                    )).filter(el => {{
                        if (el.closest('.user-msg, .userMsg, [class*="user-msg"], .singleselect-radiobutton, .ssrc__radio-btn-container, .sendMsg, .skipMsg')) {{
                            return false;
                        }}
                        const t = (el.innerText || '').trim();
                        if (t.length < 3) return false;
                        const tLow = t.toLowerCase();
                        if (tLow === 'skip this question' || tLow === 'skip' || tLow === 'save' || tLow === 'save & apply' || tLow === 'submit' || tLow === 'yes no' || tLow === 'yes' || tLow === 'no') return false;
                        return true;
                    }});

                    if (candidateEls.length > 0) {{
                        const lastEl = candidateEls[candidateEls.length - 1];
                        const clone = lastEl.cloneNode(true);
                        clone.querySelectorAll('.singleselect-radiobutton, .ssrc__radio-btn-container, .skipMsg, .sendMsg, button, input, label, .chipMsg, .bot-btn').forEach(n => n.remove());
                        const text = (clone.innerText || clone.textContent || '').trim();
                        if (text.length > 3 && !text.toLowerCase().includes('skip this question')) {{
                            return text;
                        }}
                    }}

                    // Fallback: look for text paragraphs in drawer
                    const pEls = Array.from(drawer.querySelectorAll('p, div.msg-text, span.msg-text, h3, h4')).filter(p => {{
                        if (p.closest('.singleselect-radiobutton, .ssrc__radio-btn-container, .sendMsg, .skipMsg, .user-msg, button')) return false;
                        const t = (p.innerText || '').trim();
                        const tLow = t.toLowerCase();
                        return t.length > 5 && !tLow.includes('skip this question') && !tLow.includes('save') && !tLow.includes('apply');
                    }});
                    if (pEls.length > 0) {{
                        return (pEls[pEls.length - 1].innerText || '').trim();
                    }}

                    return "Application Questionnaire";
                }};

                const latestPrompt = getCleanBotQuestion();

                // Helper to locate "Skip this question" button / link
                const getSkipButton = () => {{
                    const elements = Array.from(drawer.querySelectorAll(
                        '.skipMsg, .skipBtn, [class*="skipMsg"], [class*="skip-btn"], a.skip, button.skip, div.skip, span.skip, button, a, div[role="button"], span'
                    )).filter(el => {{
                        if (!isCurrentTurnElement(el)) return false;
                        const t = (el.innerText || '').trim().toLowerCase();
                        return t === 'skip this question' || t === 'skip' || t.startsWith('skip this');
                    }});
                    return elements.length > 0 ? elements[0] : null;
                }};

                // Helper to dispatch realistic coordinate mouse events
                const dispatchCoordClick = (element) => {{
                    if (!element) return;
                    const r = element.getBoundingClientRect();
                    const opts = {{
                        bubbles: true,
                        cancelable: true,
                        view: window,
                        clientX: r.x + Math.min(20, r.width / 2),
                        clientY: r.y + Math.min(15, r.height / 2)
                    }};
                    ['pointerover', 'pointerenter', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(evt => {{
                        element.dispatchEvent(new MouseEvent(evt, opts));
                    }});
                    if (typeof element.click === 'function') element.click();
                }};

                // Helper to click Save / Submit / Send button in drawer
                const clickDrawerSubmitBtn = async () => {{
                    await new Promise(r => setTimeout(r, 400));
                    const sendDiv = drawer.querySelector('.send');
                    if (sendDiv) sendDiv.classList.remove('disabled');

                    const allDrawerBtns = Array.from(drawer.querySelectorAll(
                        '.sendMsg, .send, .sendMsgbtn_container, button, a[role="button"], div[role="button"], input[type="submit"], input[type="button"], [class*="save"], [class*="submit"], [class*="send"]'
                    )).filter(b => {{
                        if (b.offsetParent === null) return false;
                        const t = (b.innerText || b.value || '').trim().toLowerCase();
                        return t === 'save' || t === 'save & apply' || t === 'submit' || t === 'apply' || t === 'continue' || t === 'send' || t === 'confirm' || (b.className && typeof b.className === 'string' && b.className.includes('sendMsg'));
                    }});

                    const targetBtn = allDrawerBtns[0] || drawer.querySelector('.sendMsg, .send, .sendMsgbtn_container, button[class*="save"]');
                    if (targetBtn) {{
                        if (targetBtn.disabled) targetBtn.disabled = false;
                        dispatchCoordClick(targetBtn);
                        const innerMsg = targetBtn.querySelector('.sendMsg') || drawer.querySelector('.sendMsg');
                        if (innerMsg && innerMsg !== targetBtn) {{
                            dispatchCoordClick(innerMsg);
                        }}
                        return true;
                    }}
                    return false;
                }};

                // 5. Intelligent matching helper for Radio options and Chips
                const selectMatchingOption = (containers, promptText) => {{
                    if (!containers || containers.length === 0) return null;
                    const pLower = (promptText || '').toLowerCase().trim();

                    // A. Check custom presets and question mappings
                    const mappings = Object.assign({{}}, rules.question_mappings || {{}}, rules.custom_resolved || {{}});
                    for (const [pattern, targetVal] of Object.entries(mappings)) {{
                        if (pLower.includes(pattern.toLowerCase().trim())) {{
                            let valLow = (targetVal || '').toLowerCase().trim();
                            if (valLow === 'use_jd_location') valLow = (rules.job_location || 'gurugram').toLowerCase().trim();
                            else if (valLow === 'use_jd_experience') valLow = rules.target_exp;
                            else if (valLow === 'use_jd_salary') valLow = rules.expected_salary_lpa;

                            const matched = containers.find(c => {{
                                const t = (c.innerText || c.textContent || '').toLowerCase().trim();
                                return t === valLow || t.includes(valLow);
                            }});
                            if (matched) return matched;
                        }}
                    }}

                    // B. Location & Relocation questions:
                    if (pLower.includes('residing') || pLower.includes('relocate') || pLower.includes('reallocate') || pLower.includes('location') || pLower.includes('city') || pLower.includes('preferred') || pLower.includes('located') || pLower.includes('stay') || pLower.includes('live')) {{
                        const yesOpt = containers.find(c => {{
                            const t = (c.innerText || c.textContent || '').toLowerCase().trim();
                            return t === 'yes' || t.startsWith('yes ') || t.includes('willing') || t.includes('residing') || t.includes('relocate');
                        }});
                        if (yesOpt && (pLower.includes('are you') || pLower.includes('willing to') || pLower.includes('willing and able') || pLower.includes('residing in') || pLower.includes('comfortable') || pLower.includes('interested') || pLower.includes('open to') || pLower.includes('able to work'))) {{
                            return yesOpt;
                        }}

                        // Distinguish Candidate's CURRENT location vs PREFERRED / JOB Relocation:
                        const isCurrentLocQ = pLower.includes('currently located') || pLower.includes('where are you located') || pLower.includes('current location') || pLower.includes('current city') || pLower.includes('present location') || pLower.includes('present city') || pLower.includes('where do you stay') || pLower.includes('where do you live') || pLower.includes('your location');

                        if (isCurrentLocQ) {{
                            // Candidate's actual location preference: Delhi NCR / Gurugram:
                            const currentLocTargets = [
                                'delhi / ncr', 'delhi ncr', 'delhi/ncr', 'delhi', 'gurugram', 'gurgaon', 'ncr'
                            ];
                            for (const tgt of currentLocTargets) {{
                                const matchLoc = containers.find(c => {{
                                    const t = (c.innerText || c.textContent || '').toLowerCase().trim();
                                    return t === tgt || t.startsWith(tgt) || t.includes(tgt);
                                }});
                                if (matchLoc) return matchLoc;
                            }}
                        }} else {{
                            // Preferred / Relocation / Job Location:
                            const prefLocTargets = [
                                (rules.job_location || '').toLowerCase().trim(),
                                'delhi / ncr', 'delhi ncr', 'delhi', 'gurugram', 'gurgaon', 'noida', 'bengaluru', 'bangalore', 'hyderabad', 'pune', 'mumbai', 'remote', 'any'
                            ].filter(Boolean);

                            for (const tgt of prefLocTargets) {{
                                const matchLoc = containers.find(c => (c.innerText || c.textContent || '').toLowerCase().includes(tgt));
                                if (matchLoc) return matchLoc;
                            }}
                        }}

                        if (yesOpt) return yesOpt;
                    }}

                    // C. Company affiliation / Ex-Employee / Conflict of Interest / Relatives:
                    if (pLower.includes('ex-') || pLower.includes('former') || pLower.includes('previous') || pLower.includes('worked with') || pLower.includes('employed with') || pLower.includes('work experience with the company') || pLower.includes('relative') || pLower.includes('conflict of interest') || pLower.includes('worked here') || pLower.includes('employee or intern') || pLower.includes('contractor') || pLower.includes('worked before')) {{
                        const noPrev = containers.find(c => {{
                            const t = (c.innerText || c.textContent || '').toLowerCase().trim();
                            return t.includes('no previous work experience') || t.includes('no previous') || t.includes('none of the above') || t.includes('never worked') || t === 'no' || t.startsWith('no ') || t === 'na' || t.includes('not applicable');
                        }});
                        if (noPrev) return noPrev;
                    }}

                    // D. Negative legal / criminal / visa sponsorship questions:
                    if (pLower.includes('criminal') || pLower.includes('convicted') || pLower.includes('disciplinary') || pLower.includes('visa sponsorship') || pLower.includes('require sponsorship') || pLower.includes('terminated')) {{
                        const negOpt = containers.find(c => {{
                            const t = (c.innerText || c.textContent || '').toLowerCase().trim();
                            return t === 'no' || t.startsWith('no ') || t.includes('none') || t.includes('not applicable');
                        }});
                        if (negOpt) return negOpt;
                    }}

                    // E. Education / Degree / Qualification / Graduation:
                    if (pLower.includes('qualification') || pLower.includes('degree') || pLower.includes('education') || pLower.includes('graduation') || pLower.includes('highest')) {{
                        const eduTargets = ['b.tech', 'b.e', 'bachelor', 'graduation', 'graduate', 'btech', 'any graduate', 'computer science', 'bca', 'b.sc', 'm.tech', 'mca', '2024', '2025'];
                        for (const tgt of eduTargets) {{
                            const matchEdu = containers.find(c => {{
                                const t = (c.innerText || c.textContent || '').toLowerCase().trim();
                                return t === tgt || t.startsWith(tgt) || t.includes(tgt);
                            }});
                            if (matchEdu) return matchEdu;
                        }}
                    }}

                    // F. Positive consent / work authorization / shift / walk-in attendance:
                    if (pLower.includes('authorized') || pLower.includes('authorization') || pLower.includes('legally authorized') || pLower.includes('shift') || pLower.includes('rotational') || pLower.includes('comfortable') || pLower.includes('agree') || pLower.includes('available') || pLower.includes('attend') || pLower.includes('walk-in') || pLower.includes('walk in')) {{
                        const posOpt = containers.find(c => {{
                            const t = (c.innerText || c.textContent || '').toLowerCase().trim();
                            return t === 'yes' || t.startsWith('yes ') || t.includes('i will attend') || t.includes('agree') || t.includes('authorized');
                        }});
                        if (posOpt) return posOpt;
                    }}

                    // G. Notice period / Joining availability:
                    if (pLower.includes('notice') || pLower.includes('joining') || pLower.includes('join') || pLower.includes('how soon') || pLower.includes('availability') || pLower.includes('earliest')) {{
                        const noticeTargets = ['15 days or less', '15 days', '0-15', 'immediate', 'serving notice period', '0', '1 month', '30 days or less', '< 15', '< 30'];
                        for (const tgt of noticeTargets) {{
                            const matchNotice = containers.find(c => (c.innerText || c.textContent || '').toLowerCase().trim().includes(tgt));
                            if (matchNotice) return matchNotice;
                        }}
                    }}

                    // H. Experience questions (Only if question actually asks about experience / years):
                    if (pLower.includes('experience') || pLower.includes('years') || pLower.includes('how many years') || pLower.includes('total exp') || pLower.includes('exp in')) {{
                        const expTargets = [
                            rules.target_exp + " year", rules.target_exp + " years", rules.target_exp,
                            "1.5 years", "1.5", "1 year", "1-2 years", "0-2 years", "0-1 years", "< 2 years", "< 1 year", "1", "fresher"
                        ];
                        for (const tgt of expTargets) {{
                            const matchExp = containers.find(c => {{
                                const t = (c.innerText || c.textContent || '').toLowerCase().trim();
                                return t === tgt || t.startsWith(tgt) || t.includes(tgt);
                            }});
                            if (matchExp) return matchExp;
                        }}
                    }}

                    // I. Expected / Current CTC questions:
                    if (pLower.includes('expected') && (pLower.includes('salary') || pLower.includes('ctc') || pLower.includes('lpa') || pLower.includes('package') || pLower.includes('ectc'))) {{
                        const salaryTargets = [
                            rules.expected_salary_lpa, rules.expected_salary_lpa + " lpa", rules.expected_salary_lpa + " lakhs",
                            "6-10", "8-10", "6-8", "9", "8", "7", "6", "9.0", "8.0", "7.0", "6.0"
                        ];
                        for (const tgt of salaryTargets) {{
                            const matchSal = containers.find(c => (c.innerText || c.textContent || '').toLowerCase().includes(tgt));
                            if (matchSal) return matchSal;
                        }}
                    }} else if (pLower.includes('current ctc') || pLower.includes('current salary') || pLower.includes('cctc')) {{
                        const curTargets = ['0', '0-3', '< 3', 'fresher', 'not applicable', '0 lpa'];
                        for (const tgt of curTargets) {{
                            const matchCur = containers.find(c => (c.innerText || c.textContent || '').toLowerCase().includes(tgt));
                            if (matchCur) return matchCur;
                        }}
                    }}

                    // J. General binary Yes/No:
                    const yesOpt = containers.find(c => {{
                        const t = (c.innerText || c.textContent || '').toLowerCase().trim();
                        return t === 'yes' || t.startsWith('yes ');
                    }});
                    const noOpt = containers.find(c => {{
                        const t = (c.innerText || c.textContent || '').toLowerCase().trim();
                        return t === 'no' || t.startsWith('no ');
                    }});

                    if (yesOpt && noOpt) {{
                        const isNeg = pLower.includes('crime') || pLower.includes('disability') || pLower.includes('sponsorship') || pLower.includes('former') || pLower.includes('previous') || pLower.includes('ex-') || pLower.includes('terminated') || pLower.includes('convicted');
                        return isNeg ? noOpt : yesOpt;
                    }}

                    return null;
                }};

                // 6. Radio Options handling
                const radioContainers = Array.from(drawer.querySelectorAll('.singleselect-radiobutton, .ssrc__radio-btn-container, div[class*="radio-btn"], div[class*="radio"]')).filter(r => {{
                    return isCurrentTurnElement(r);
                }});

                if (radioContainers.length > 0) {{
                    const targetRadio = selectMatchingOption(radioContainers, latestPrompt);

                    if (targetRadio) {{
                        const inputInside = targetRadio.querySelector('input[type="radio"]') || targetRadio;
                        const labelText = targetRadio.innerText.trim();
                        
                        dispatchCoordClick(targetRadio);
                        if (inputInside && inputInside !== targetRadio) {{
                            inputInside.checked = true;
                            inputInside.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        }}

                        await clickDrawerSubmitBtn();

                        return {{
                            hasDrawer: true,
                            isCompleted: false,
                            action: "clicked_radio",
                            prompt: latestPrompt,
                            answer: labelText
                        }};
                    }}

                    const skipBtn = getSkipButton();
                    if (skipBtn) {{
                        dispatchCoordClick(skipBtn);
                        return {{
                            hasDrawer: true,
                            isCompleted: false,
                            action: "skipped_question",
                            prompt: latestPrompt,
                            answer: "Skipped (Optional)"
                        }};
                    }}

                    const allRadioTexts = radioContainers.map(r => (r.innerText || '').trim()).filter(Boolean);
                    return {{
                        hasDrawer: true,
                        isCompleted: false,
                        action: "unmapped_question",
                        prompt: latestPrompt,
                        options: allRadioTexts
                    }};
                }}

                // 7. Option Chips / Quick-Answer Buttons
                const chips = Array.from(drawer.querySelectorAll('button.bot-btn, div.chipMsg, button.chip, div.chip, button[class*="chip"], div[class*="chip"]')).filter(c => {{
                    if (!isCurrentTurnElement(c)) return false;
                    if (c.closest('.sendMsg, .skipMsg, [class*="skip"], .singleselect-radiobutton')) return false;
                    const t = (c.innerText || '').trim().toLowerCase();
                    if (t === 'skip this question' || t === 'skip' || t === 'save' || t === 'submit') return false;
                    return !c.disabled && t.length > 0;
                }});

                if (chips.length > 0) {{
                    const targetChip = selectMatchingOption(chips, latestPrompt);

                    if (targetChip) {{
                        const chipText = targetChip.innerText.trim();
                        dispatchCoordClick(targetChip);

                        await clickDrawerSubmitBtn();

                        return {{
                            hasDrawer: true,
                            isCompleted: false,
                            action: "clicked_chip",
                            prompt: latestPrompt,
                            answer: chipText
                        }};
                    }}

                    const skipBtn = getSkipButton();
                    if (skipBtn) {{
                        dispatchCoordClick(skipBtn);
                        return {{
                            hasDrawer: true,
                            isCompleted: false,
                            action: "skipped_question",
                            prompt: latestPrompt,
                            answer: "Skipped (Optional)"
                        }};
                    }}

                    const allChipTexts = chips.map(c => (c.innerText || '').trim()).filter(Boolean);
                    return {{
                        hasDrawer: true,
                        isCompleted: false,
                        action: "unmapped_question",
                        prompt: latestPrompt,
                        options: allChipTexts
                    }};
                }}

                // 8. Text / Number Inputs & ContentEditable Area handling
                const inputs = Array.from(drawer.querySelectorAll('input[type="text"], input[type="number"], textarea, div.textArea[contenteditable="true"]')).filter(i => {{
                    return isCurrentTurnElement(i) && !i.disabled && !i.classList.contains('d-none');
                }});

                if (inputs.length > 0) {{
                    const inp = inputs[0];
                    const promptLow = (latestPrompt || '').toLowerCase().trim();
                    const inputFieldMeta = ((inp.name || '') + ' ' + (inp.placeholder || '')).toLowerCase();
                    
                    let fillValue = null;

                    // A. Check custom presets and mappings against latestPrompt and field metadata
                    const mappings = Object.assign({{}}, rules.question_mappings || {{}}, rules.custom_resolved || {{}});
                    for (const [pattern, targetVal] of Object.entries(mappings)) {{
                        const patLow = pattern.toLowerCase().trim();
                        if (promptLow.includes(patLow) || (patLow.length > 4 && inputFieldMeta.includes(patLow))) {{
                            let val = targetVal;
                            if (val === 'USE_JD_LOCATION') val = rules.job_location || 'Delhi NCR';
                            else if (val === 'USE_JD_EXPERIENCE') val = rules.target_exp;
                            else if (val === 'USE_JD_SALARY') val = rules.expected_salary_lpa;
                            fillValue = val;
                            break;
                        }}
                    }}

                    // B. Education / Qualification / Degree (e.g. "Kindly mention your Highest Education Qualification")
                    if (fillValue === null && (promptLow.includes('qualification') || promptLow.includes('degree') || promptLow.includes('education') || promptLow.includes('graduation'))) {{
                        if (promptLow.includes('year') || promptLow.includes('passing')) {{
                            fillValue = "2024";
                        }} else {{
                            fillValue = "B.Tech";
                        }}
                    }}

                    // C. Ex-employee / Employee ID questions
                    else if (fillValue === null && (promptLow.includes('ex-') || promptLow.includes('employee id') || promptLow.includes('mention na') || (promptLow.includes('if yes') && promptLow.includes('if no')) || promptLow.includes('former employee'))) {{
                        fillValue = "NA";
                    }}

                    // D. Location / City / Residing
                    else if (fillValue === null && (promptLow.includes('current city') || promptLow.includes('current location') || promptLow.includes('where are you located') || promptLow.includes('where are you currently located') || promptLow.includes('where do you stay') || promptLow.includes('where do you live') || promptLow.includes('present city') || promptLow.includes('present location') || promptLow.includes('residing in') || promptLow.includes('reside in') || promptLow.includes('which city') || promptLow.includes('willing to relocate'))) {{
                        fillValue = "Delhi NCR";
                    }}

                    // E. Notice period / Joining time
                    else if (fillValue === null && (promptLow.includes('notice') || promptLow.includes('joining') || promptLow.includes('how soon') || promptLow.includes('earliest joining') || promptLow.includes('join within') || promptLow.includes('30 days or less') || promptLow.includes('15 days'))) {{
                        fillValue = "0";
                    }}

                    // F. Expected CTC / Salary
                    else if (fillValue === null && (promptLow.includes('expected') && (promptLow.includes('salary') || promptLow.includes('ctc') || promptLow.includes('lpa') || promptLow.includes('lacs') || promptLow.includes('annum') || promptLow.includes('package') || promptLow.includes('starting salary') || promptLow.includes('ectc')))) {{
                        fillValue = rules.expected_salary_lpa;
                    }}

                    // G. Current CTC
                    else if (fillValue === null && (promptLow.includes('current ctc') || promptLow.includes('current salary') || promptLow.includes('current fixed') || promptLow.includes('current package') || promptLow.includes('cctc'))) {{
                        fillValue = "0";
                    }}

                    // H. Experience (STRICT CHECK: Must explicitly ask for years of experience or tech experience in promptLow)
                    else if (fillValue === null && (promptLow.includes('how many years') || promptLow.includes('total experience') || promptLow.includes('years of experience') || promptLow.includes('experience do you have in') || promptLow.includes('experience in technical') || (promptLow.includes('experience in') && (promptLow.includes('java') || promptLow.includes('python') || promptLow.includes('react') || promptLow.includes('backend') || promptLow.includes('sql') || promptLow.includes('fullstack')))) && !promptLow.includes('work experience with the company') && !promptLow.includes('previous')) {{
                        fillValue = rules.target_exp;
                    }}

                    // I. Qualitative Project & Behavioral Questions
                    else if (fillValue === null && (promptLow.includes('project') || promptLow.includes('what did you build') || promptLow.includes('resume you know best'))) {{
                        fillValue = "I built a live full-stack JSON-to-CSV data conversion tool with Python, REST APIs, and structured data validation pipelines.";
                    }} else if (fillValue === null && (promptLow.includes('broke') || promptLow.includes('went wrong') || promptLow.includes('find the cause'))) {{
                        fillValue = "Encountered memory bottlenecks with large nested JSON structures; resolved by implementing streaming chunk processing and schema validation.";
                    }} else if (fillValue === null && (promptLow.includes('stuck') || promptLow.includes('technical problem'))) {{
                        fillValue = "Debugging async race conditions in concurrent data processing; resolved with structured async locks and retry mechanisms.";
                    }} else if (fillValue === null && (promptLow.includes('disagreed') || promptLow.includes('technical decision') || promptLow.includes('disagreement'))) {{
                        fillValue = "Advocated for modular REST API endpoints over monolithic handlers, improving testability and code maintainability.";
                    }} else if (fillValue === null && (promptLow.includes('team size') || promptLow.includes('work with daily') || promptLow.includes('depended on others'))) {{
                        fillValue = "Collaborated closely with developers and QA in agile sprints, maintaining clear API contracts and Git version control.";
                    }} else if (fillValue === null && (promptLow.includes('hardest') || promptLow.includes('without looking things up'))) {{
                        fillValue = "Complex Kubernetes cluster configurations, which I actively reference standard documentation to ensure accuracy.";
                    }} else if (fillValue === null && (promptLow.includes('last 6 months') || promptLow.includes('not required by your job'))) {{
                        fillValue = "Deepened expertise in LLM agent workflows, RAG architectures, and FastAPI backend services through independent projects.";
                    }} else if (fillValue === null && (promptLow.includes('day split') || promptLow.includes('working day split') || promptLow.includes('time-eater'))) {{
                        fillValue = "70% feature development and unit testing, 15% code reviews and technical design, 15% documentation and debugging.";
                    }} else if (fillValue === null && (promptLow.includes('manager') || promptLow.includes('contribution'))) {{
                        fillValue = "Strong problem-solving capability, rapid learning agility, and delivering clean, well-tested code on schedule.";
                    }}

                    // If unmapped question, check if there is an optional "Skip this question" button
                    const skipBtn = getSkipButton();
                    if (fillValue === null && skipBtn) {{
                        dispatchCoordClick(skipBtn);
                        return {{
                            hasDrawer: true,
                            isCompleted: false,
                            action: "skipped_question",
                            prompt: latestPrompt,
                            answer: "Skipped (Optional)"
                        }};
                    }}

                    // If still unmapped and NO skip button: DO NOT GUESS! Flag for review
                    if (fillValue === null) {{
                        return {{
                            hasDrawer: true,
                            isCompleted: false,
                            action: "unmapped_question",
                            prompt: latestPrompt,
                            options: []
                        }};
                    }}

                    inp.focus();
                    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                    const nativeTextAreaSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;

                    if (inp.tagName === 'INPUT' && nativeSetter) {{
                        nativeSetter.call(inp, fillValue);
                    }} else if (inp.tagName === 'TEXTAREA' && nativeTextAreaSetter) {{
                        nativeTextAreaSetter.call(inp, fillValue);
                    }} else if (inp.isContentEditable) {{
                        inp.innerText = fillValue;
                        inp.textContent = fillValue;
                    }} else {{
                        inp.value = fillValue;
                    }}

                    inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    inp.dispatchEvent(new Event('blur', {{ bubbles: true }}));

                    const didClick = await clickDrawerSubmitBtn();
                    if (!didClick) {{
                        inp.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }}));
                    }}

                    return {{
                        hasDrawer: true,
                        isCompleted: false,
                        action: "filled_input",
                        prompt: latestPrompt,
                        answer: fillValue
                    }};
                }}

                // 9. Direct Skip Button if present without active input
                const standaloneSkipBtn = getSkipButton();
                if (standaloneSkipBtn) {{
                    dispatchCoordClick(standaloneSkipBtn);
                    return {{
                        hasDrawer: true,
                        isCompleted: false,
                        action: "skipped_question",
                        prompt: latestPrompt,
                        answer: "Skipped (Optional)"
                    }};
                }}

                // 10. Direct Save / Submit Button inside Drawer
                const didSubmit = await clickDrawerSubmitBtn();
                if (didSubmit) {{
                    return {{ hasDrawer: true, isCompleted: false, action: "clicked_submit", text: "Save" }};
                }}

                // 11. Check if unmapped question is blocking without matching input
                return {{
                    hasDrawer: true,
                    isCompleted: false,
                    action: "unmapped_question",
                    prompt: latestPrompt,
                    options: []
                }};
            }})()
            """)

            if not bot_turn or not bot_turn.get("hasDrawer"):
                break

            if bot_turn.get("isCompleted"):
                print(f"[Naukri] 🎉 [Chatbot]: Application confirmed completed by Naukri bot!")
                return {"completed": True, "status": "Submitted", "filled_answers": filled_answers}

            act = bot_turn.get("action")
            q_text = bot_turn.get("prompt", "Question")
            a_text = bot_turn.get("answer", "")
            opts = bot_turn.get("options", [])

            if act == "skipped_question":
                print(f"[Naukri] ⏭️ [Chatbot Turn {turn+1}]: Skipped optional question: \"{q_text}\"")
                filled_answers.append({
                    "question": q_text,
                    "answer": "Skipped (Optional)",
                    "turn": turn + 1
                })
            elif act in ["clicked_radio", "clicked_chip", "filled_input"]:
                print(f"[Naukri] 🤖 [Chatbot Turn {turn+1}]: Answered '{a_text}' to: \"{q_text}\"")
                filled_answers.append({
                    "question": q_text,
                    "answer": a_text,
                    "turn": turn + 1
                })
            elif act == "clicked_submit":
                print(f"[Naukri] 🤖 [Chatbot Turn {turn+1}]: Clicked 'Save' button in drawer")
            elif act == "unmapped_question":
                print(f"[Naukri] ⚠️ Unmapped question encountered without preset: \"{q_text}\"")
                self.tracker.record_unresolved_question(
                    question_text=q_text,
                    company=comp,
                    job_title=title,
                    job_url=job_url,
                    options=opts
                )
                return {
                    "completed": False,
                    "status": "Under Review",
                    "notes": f"Unmapped question flagged for review: \"{q_text}\"",
                    "filled_answers": filled_answers
                }

        return {"completed": False, "filled_answers": filled_answers}

    async def _handle_application_form(self, job: Dict[str, Any], score_res: Dict[str, Any]) -> Dict[str, Any]:
        """
        Detects and auto-fills questionnaire fields (CTC, experience, notice period, location, skill answers)
        from candidate profile approved answers without hallucinating.
        If an unrecognized question or coding task is found, returns 'Waiting for Input'.
        """
        personal = self.profile.get("personal_info", {})
        prefs = self.profile.get("preferences", {})
        edu_info = self.profile.get("education", [{}])[0]

        total_exp_years = self._determine_target_experience(job)
        expected_ctc_lpa = self._calculate_dynamic_expected_salary(job)
        notice_days = prefs.get("notice_period_days", 0)
        current_ctc_lpa = 0.0  # Entry level / fresher baseline
        location = "Gurugram"  # Fixed location per user preset rule
        gender = "Female"

        context = {
            "total_exp_years": total_exp_years,
            "notice_days": notice_days,
            "expected_ctc_lpa": expected_ctc_lpa,
            "current_ctc_lpa": current_ctc_lpa,
            "location": location,
            "gender": gender,
            "degree": edu_info.get("degree", "B.Tech"),
            "field": edu_info.get("field_of_study", "Computer Science"),
            "grad_year": edu_info.get("graduation_year", 2024),
            "phone": personal.get("phone", ""),
            "email": personal.get("email", "")
        }

        form_res = await self.cdp.evaluate(f"""
        (() => {{
            const ctx = {json.dumps(context)};
            const modalEl = document.querySelector('[role="dialog"], [class*="Modal"], [class*="modal"], [class*="drawer"], form') || document.body;
            const text = (modalEl.innerText || '').toLowerCase();

            // Check for complex coding assignment or assessment
            const hasComplexTask = (
                text.includes('coding assessment') ||
                text.includes('take a test') ||
                text.includes('hackerEarth') ||
                text.includes('hirepro') ||
                text.includes('complete the assignment')
            );

            if (hasComplexTask) {{
                return {{ hasTask: true, taskName: "Assessment / Assignment required" }};
            }}

            const filledFields = [];

            // 1. Text Inputs & Numbers (CTC, Experience, Notice Period)
            const inputs = Array.from(modalEl.querySelectorAll('input[type="text"], input[type="number"], input:not([type])'));
            inputs.forEach(inp => {{
                const label = ((inp.name || '') + ' ' + (inp.placeholder || '') + ' ' + (inp.closest('label') ? inp.closest('label').innerText : '') + ' ' + (inp.parentElement ? inp.parentElement.innerText : '')).toLowerCase();
                let fillVal = null;
                let qTitle = inp.name || inp.placeholder || "Input Field";

                if ((label.includes('experience') || label.includes('total exp') || label.includes('years of exp')) && !label.includes('work experience with the company') && !label.includes('previous')) {{
                    fillVal = String(ctx.total_exp_years);
                    filledFields.push({{ question: "Total Experience", answer: fillVal + " Years" }});
                }} else if (label.includes('notice') || label.includes('notice period') || label.includes('joining')) {{
                    fillVal = String(ctx.notice_days);
                    filledFields.push({{ question: "Notice Period", answer: fillVal + " Days" }});
                }} else if (label.includes('expected ctc') || label.includes('expected salary') || label.includes('ectc')) {{
                    fillVal = String(ctx.expected_ctc_lpa);
                    filledFields.push({{ question: "Expected CTC", answer: fillVal + " LPA" }});
                }} else if (label.includes('current ctc') || label.includes('current salary') || label.includes('cctc')) {{
                    fillVal = String(ctx.current_ctc_lpa);
                    filledFields.push({{ question: "Current CTC", answer: fillVal + " LPA" }});
                }} else if (label.includes('city') || label.includes('current location') || label.includes('residing in')) {{
                    fillVal = ctx.location;
                    filledFields.push({{ question: "Current Location", answer: fillVal }});
                }} else if (label.includes('ex-') || label.includes('employee id') || label.includes('mention na')) {{
                    fillVal = "NA";
                    filledFields.push({{ question: "Ex-Employee Verification", answer: fillVal }});
                }}

                if (fillVal !== null && (!inp.value || inp.value.trim() === '')) {{
                    inp.focus();
                    inp.value = fillVal;
                    inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
            }});

            // 2. Radio Buttons & Checkboxes (Work Authorization, Immediate Joiner, Relocation, Residing)
            const radios = Array.from(modalEl.querySelectorAll('input[type="radio"], input[type="checkbox"]'));
            radios.forEach(r => {{
                const rLabel = ((r.name || '') + ' ' + (r.closest('label') ? r.closest('label').innerText : '') + ' ' + (r.parentElement ? r.parentElement.innerText : '')).toLowerCase();
                
                // Authorize to work / Immediate Joiner / Relocate / Reside -> Yes
                if (rLabel.includes('authorized') || rLabel.includes('immediate') || rLabel.includes('relocate') || rLabel.includes('residing') || rLabel.includes('reside') || rLabel.includes('willing to work')) {{
                    if (rLabel.includes('yes') || r.value.toLowerCase() === 'yes' || r.value.toLowerCase() === 'true') {{
                        r.checked = true;
                        r.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        filledFields.push({{ question: "Consent / Authorization / Location", answer: "Yes" }});
                    }}
                }}
            }});

            return {{ hasTask: false, filledFields: filledFields }};
        }})()
        """)

        if form_res and form_res.get("hasTask"):
            print(f"[Naukri] 📋 Assessment requirement detected: {form_res.get('taskName')}")
            return {"status": "Waiting for Input", "notes": f"Needs Action: {form_res.get('taskName')}"}

        filled_answers = form_res.get("filledFields", []) if form_res else []
        if filled_answers:
            print(f"[Naukri] 📝 Auto-filled questionnaire fields: {', '.join([a.get('question', '') + ': ' + a.get('answer', '') for a in filled_answers])}")

        return {"status": "OK", "filled_answers": filled_answers}

    def _get_resume_for_job(self, job: Dict[str, Any]) -> Dict[str, str]:
        """
        Dynamically routes category-tailored resume based on job title and description.
        """
        title = (job.get("title") or "").lower()
        desc = (job.get("description") or "").lower()

        if any(w in title or w in desc for w in ["ai", "machine learning", "ml", "llm", "generative ai", "deep learning", "nlp", "pytorch", "tensorflow", "computer vision"]):
            category = "ai_ml"
        elif any(w in title or w in desc for w in ["java", "spring boot", "j2ee", "java developer", "java backend"]):
            category = "java_spring"
        elif any(w in title or w in desc for w in ["full stack", "fullstack", "frontend", "react", "next.js", "vue"]):
            category = "fullstack"
        elif any(w in title or w in desc for w in ["sql", "data engineer", "etl", "database", "database developer"]):
            category = "sql_data"
        elif any(w in title or w in desc for w in ["backend", "python", "django", "fastapi", "flask"]):
            category = "backend"
        else:
            category = "default"

        resumes_cfg = self.profile.get("resumes", {})
        resume_path = resumes_cfg.get(category) or resumes_cfg.get("default", "resumes/Sakshi_resume2.pdf")
        filename = os.path.basename(resume_path)

        return {
            "category": category,
            "filename": filename,
            "path": resume_path
        }
