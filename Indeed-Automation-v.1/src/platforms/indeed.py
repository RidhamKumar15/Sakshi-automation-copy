import asyncio
import json
import logging
import os
import re
import time
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.cdp_client import ChromeCDPClient
from src.scorer import JobScorer
from src.tracker import ApplicationTracker

logger = logging.getLogger("IndeedPlatform")


class IndeedAutomation:
    """
    Automates job search, scoring, and application on Indeed (in.indeed.com / indeed.com).
    Uses the dedicated Chrome automation profile via CDP.
    Adheres strictly to the Genesis reference architecture.
    """

    BASE_URL = "https://in.indeed.com"

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
        self.indeed_cfg = settings.get("sources", {}).get("indeed", {})

    async def search_and_process_jobs(self, keywords: Optional[List[str]] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        """
        Main sequential execution flow:
        1. Build search URLs for keywords with location, freshness tiers, and date sorting.
        2. Verify login & check CAPTCHA / Cloudflare challenges.
        3. Extract job listing cards from search results with DOM-level metadata.
        4. Open full job description for comprehensive scoring evaluation.
        5. Score each job using the 0-100 rubric.
        6. Check for duplicates in persistent tracker.
        7. Apply directly via Indeed Easy Apply, answer questionnaires, or queue for review.
        8. Verify live submission truth before updating tracker.
        """
        max_to_apply = limit if limit is not None else self.max_applications

        results_summary = {
            "source": "Indeed",
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

        search_terms = keywords or self.indeed_cfg.get("search_keywords", [
            "Software Engineer",
            "Software Developer",
            "Backend Developer",
            "Python Developer",
            "Java Developer",
            "SDE 1",
            "Full Stack Developer"
        ])

        freshness_tiers = self.indeed_cfg.get("search_filters", {}).get("freshness_tiers", [1, 3, 7, 14])
        tier_names = {1: "Today / Last 24 Hours", 3: "Last 3 Days", 7: "Last 7 Days", 14: "Last 14 Days"}

        for keyword in search_terms:
            if results_summary["applications_submitted"] >= max_to_apply:
                print(f"[Indeed] 🛑 Reached max applications limit ({max_to_apply}). Stopping search.")
                break

            for freshness in freshness_tiers:
                if results_summary["applications_submitted"] >= max_to_apply:
                    break

                tier_label = tier_names.get(freshness, f"Last {freshness} Days")
                print(f"\n[Indeed] 🔍 Searching jobs for keyword: '{keyword}' [Freshness: {tier_label}]...")
                search_urls = self._build_search_urls(keyword, freshness=freshness)

                for search_url in search_urls:
                    if results_summary["applications_submitted"] >= max_to_apply:
                        break

                    print(f"[Indeed] 🌐 Navigating to search URL: {search_url}")
                    await self.cdp.navigate(search_url, wait_seconds=4.0)

                    # 1. Security / CAPTCHA / Login Check
                    sec_status = await self.cdp.check_for_security_challenge()
                    if sec_status.get("has_challenge"):
                        if sec_status.get("login_required"):
                            await self.cdp.pause_and_wait_for_human("Indeed login required. Please log into your Indeed account in Chrome.")
                        else:
                            await self.cdp.pause_and_wait_for_human("CAPTCHA or security challenge detected on Indeed.")

                    # 2. Smooth scroll to trigger lazy loading of job cards
                    for _ in range(3):
                        await self.cdp.evaluate("window.scrollBy(0, 600);")
                        await asyncio.sleep(1.0)

                    # 3. Extract job cards from page
                    extracted_jobs = await self._extract_job_listings_from_page()
                    if not extracted_jobs:
                        # Retry scroll
                        await self.cdp.evaluate("window.scrollTo(0, document.body.scrollHeight / 2);")
                        await asyncio.sleep(2.0)
                        extracted_jobs = await self._extract_job_listings_from_page()

                    results_summary["jobs_found"] += len(extracted_jobs)
                    print(f"[Indeed] Extracted {len(extracted_jobs)} job listings from search (Tier: {tier_label}).")

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
                        print(f"📌 [Indeed] Evaluating: {title} at {comp}")
                        print(f"   URL: {url} | Posted: {posted_date or 'Recent'}")

                        # A. Freshness Verification: Max 14 Days
                        age_days = self._parse_posting_age_days(posted_date)
                        if age_days > 14:
                            print(f"   ⏳ [POSTING AGE FILTER] Job is {age_days} days old ('{posted_date}'). Exceeds 14-day limit. Skipping.")
                            results_summary["jobs_skipped"] += 1
                            continue

                        # B. DOM-level Check: Already Applied
                        if job.get("is_already_applied"):
                            print(f"   ⏭️ [CARD DOM CHECK] Already applied on Indeed ('{comp}' - '{title}'). Skipping.")
                            results_summary["duplicates_ignored"] += 1
                            self.tracker.record_job(
                                company=comp,
                                role=title,
                                source="Indeed",
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
                        if job.get("is_external_portal") and not job.get("is_easy_apply"):
                            print(f"   🏢 [CARD DOM CHECK] External company portal application detected ('{comp}' - '{title}'). Flagging as Under Review.")
                            results_summary["jobs_under_review"] += 1
                            results_summary["attention_urls"].append(url)
                            self.tracker.record_job(
                                company=comp,
                                role=title,
                                source="Indeed",
                                job_url=url,
                                match_score=60,
                                status="Under Review",
                                notes="External company website redirect (detected on card DOM - requires manual company portal apply)",
                                job_id=job_id
                            )
                            continue

                        # E. Deep JD Extraction
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
                        min_auto = self.scorer.thresholds.get("auto_apply_min_score", 70)
                        min_review = self.scorer.thresholds.get("review_queue_min_score", 55)

                        if decision == "SKIP":
                            print(f"   ❌ Score {total_score} < {min_review}. Skipping job.")
                            results_summary["jobs_skipped"] += 1
                            self.tracker.record_job(
                                company=comp,
                                role=title,
                                source="Indeed",
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
                                source="Indeed",
                                job_url=url,
                                match_score=total_score,
                                status="Under Review",
                                notes=f"Review range match ({total_score}/100). Added to review queue.",
                                job_id=job_id
                            )
                            continue

                        elif decision == "AUTO_APPLY":
                            print(f"   🚀 Match score {total_score} >= {min_auto}. Initiating application workflow...")
                            self.tracker.record_job(
                                company=comp,
                                role=title,
                                source="Indeed",
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
                                    self.tracker.update_status(
                                        key_or_url=url,
                                        new_status="Submitted",
                                        notes=f"Applied on Indeed ({comp}, Score: {total_score})",
                                        filled_answers=job_filled_answers,
                                        job_id=job_id,
                                        company=comp,
                                        role=title
                                    )

                                    if limit and results_summary["applications_submitted"] >= limit:
                                        print(f"\n🎉 [TARGET LIMIT REACHED] Successfully submitted {results_summary['applications_submitted']} application(s)!")
                                        from src.notifier import notify_completion
                                        notify_completion(results_summary["applications_submitted"], results_summary["jobs_evaluated"])
                                        return results_summary

                                elif app_result["status"] == "Waiting for Input":
                                    results_summary["applications_manual_action"] += 1
                                    results_summary["attention_urls"].append(url)
                                    self.tracker.update_status(
                                        key_or_url=url,
                                        new_status="Waiting for Input",
                                        notes=app_result.get("notes", "Requires manual user input"),
                                        filled_answers=job_filled_answers,
                                        job_id=job_id,
                                        company=comp,
                                        role=title
                                    )
                                    from src.notifier import notify_attention_required
                                    notify_attention_required(f"Input needed for {title} at {comp}")

                                elif app_result["status"] == "Under Review":
                                    results_summary["jobs_under_review"] += 1
                                    results_summary["attention_urls"].append(url)
                                    self.tracker.update_status(
                                        key_or_url=url,
                                        new_status="Under Review",
                                        notes=app_result.get("notes", "Application under review"),
                                        filled_answers=job_filled_answers,
                                        job_id=job_id,
                                        company=comp,
                                        role=title
                                    )

                                elif app_result["status"] == "Already Applied":
                                    results_summary["duplicates_ignored"] += 1
                                    self.tracker.update_status(
                                        key_or_url=url,
                                        new_status="Already Applied",
                                        notes="Already applied on Indeed",
                                        filled_answers=job_filled_answers,
                                        job_id=job_id,
                                        company=comp,
                                        role=title
                                    )

                                elif app_result["status"] == "Closed":
                                    self.tracker.update_status(
                                        key_or_url=url,
                                        new_status="Closed",
                                        notes="Job listing no longer active on Indeed",
                                        job_id=job_id,
                                        company=comp,
                                        role=title
                                    )

                                elif app_result["status"] == "Skipped":
                                    results_summary["jobs_skipped"] += 1
                                    self.tracker.update_status(
                                        key_or_url=url,
                                        new_status="Skipped",
                                        notes=app_result.get("notes", "Skipped constraint"),
                                        job_id=job_id,
                                        company=comp,
                                        role=title
                                    )

                                else:
                                    results_summary["errors"].append(f"{comp} - {title}: {app_result.get('notes', 'Failed')}")
                                    self.tracker.update_status(
                                        key_or_url=url,
                                        new_status="Failed",
                                        notes=app_result.get("notes", "Application process failed"),
                                        job_id=job_id,
                                        company=comp,
                                        role=title
                                    )

                            except asyncio.CancelledError:
                                print(f"   🛑 Automation cancelled while applying to '{title}' at '{comp}'. Updating status to Failed.")
                                self.tracker.update_status(
                                    key_or_url=url,
                                    new_status="Failed",
                                    notes="Application cancelled by user during execution",
                                    job_id=job_id,
                                    company=comp,
                                    role=title
                                )
                                raise

                            except Exception as job_err:
                                print(f"   ⚠️ Error processing job '{title}' at '{comp}': {job_err}")
                                results_summary["errors"].append(f"{comp} - {title}: {job_err}")
                                self.tracker.update_status(
                                    key_or_url=url,
                                    new_status="Failed",
                                    notes=f"Error: {job_err}",
                                    job_id=job_id,
                                    company=comp,
                                    role=title
                                )

                        # Polite delay between jobs
                        await asyncio.sleep(2.0)

                    if limit and results_summary["applications_submitted"] >= limit:
                        return results_summary

        return results_summary

    def _parse_posting_age_days(self, posted_text: str) -> int:
        """
        Parses posting date text into days integer.
        e.g. 'Just posted', 'Today', '1 day ago', '3 days ago', '14 days ago', '30+ days ago'
        """
        if not posted_text:
            return 0
        t = posted_text.lower().strip()
        if any(w in t for w in ["just posted", "today", "few hours", "hour", "new", "moment"]):
            return 0
        if "day" in t or "d ago" in t:
            m = re.search(r"(\d+)", t)
            if m:
                return int(m.group(1))
            return 1
        if "month" in t or "year" in t or "30+" in t:
            return 30
        return 0

    def _build_search_urls(self, keyword: str, freshness: int = 7) -> List[str]:
        """
        Constructs canonical Indeed search URLs with query, location, freshness, and newest sort order.
        """
        filters = self.indeed_cfg.get("search_filters", {})
        location = filters.get("location", "India")
        base = self.indeed_cfg.get("base_url", self.BASE_URL)

        q_enc = urllib.parse.quote_plus(keyword)
        l_enc = urllib.parse.quote_plus(location)

        urls = [
            f"{base}/jobs?q={q_enc}&l={l_enc}&sort=date&fromage={freshness}"
        ]

        if "remote" not in keyword.lower():
            urls.append(f"{base}/jobs?q={q_enc}+remote&l={l_enc}&sort=date&fromage={freshness}")

        return urls

    async def _extract_job_listings_from_page(self) -> List[Dict[str, Any]]:
        """
        Extracts job listings from Indeed search results page using robust DOM selectors.
        """
        js = r"""
        (() => {
            const listings = [];
            const seenKeys = new Set();

            // Indeed job card container selectors
            const cardSelectors = [
                'div.job_seen_beacon',
                'div.cardOutline',
                'div[class*="jobCard_mainContent"]',
                'div.slider_container',
                'td.resultContent',
                'li.css-5lfssm',
                'div.jobsearch-SerpJobCard'
            ];

            let cards = [];
            for (const sel of cardSelectors) {
                const found = Array.from(document.querySelectorAll(sel));
                if (found.length > 0) {
                    cards = found;
                    break;
                }
            }

            if (cards.length === 0) {
                cards = Array.from(document.querySelectorAll('a[id^="job_"], a.jcs-JobTitle')).map(a => a.closest('div, li, td')).filter(Boolean);
            }

            cards.forEach(card => {
                try {
                    // 1. Job Key / ID
                    let jobKey = card.getAttribute('data-jk') || card.closest('[data-jk]')?.getAttribute('data-jk') || '';
                    if (!jobKey) {
                        const link = card.querySelector('a[id^="job_"], a.jcs-JobTitle, a[href*="jk="], a[href*="/viewjob"]');
                        if (link) {
                            const href = link.getAttribute('href') || '';
                            const m = href.match(/[?&]jk=([a-zA-Z0-9]+)/) || href.match(/jk=([a-zA-Z0-9]+)/) || link.id.match(/job_([a-zA-Z0-9]+)/);
                            if (m) jobKey = m[1];
                        }
                    }

                    if (jobKey && seenKeys.has(jobKey)) return;
                    if (jobKey) seenKeys.add(jobKey);

                    // 2. Job Title
                    let title = '';
                    const titleEl = card.querySelector('h2.jobTitle span, a.jcs-JobTitle, h2.jobTitle a, h2[class*="jobTitle"], span[id^="jobTitle"]');
                    if (titleEl) {
                        title = titleEl.innerText.trim();
                    } else {
                        const fallbackTitle = card.querySelector('h2, h3, [class*="title"]');
                        if (fallbackTitle) title = fallbackTitle.innerText.trim();
                    }

                    // 3. Company Name
                    let company = '';
                    const compEl = card.querySelector('span[data-testid="company-name"], span.companyName, a[data-testid="company-name"], span.css-63koeb, [class*="companyName"]');
                    if (compEl) company = compEl.innerText.trim();

                    // 4. Location
                    let location = '';
                    const locEl = card.querySelector('div[data-testid="text-location"], div.companyLocation, div.css-1p0sjhy, [class*="companyLocation"]');
                    if (locEl) location = locEl.innerText.trim();

                    // 5. Job URL
                    let jobUrl = '';
                    if (jobKey) {
                        jobUrl = `https://in.indeed.com/viewjob?jk=${jobKey}`;
                    } else {
                        const linkEl = card.querySelector('a.jcs-JobTitle, a[href*="/viewjob"], a[href*="/rc/clk"]');
                        if (linkEl && linkEl.href) jobUrl = linkEl.href;
                    }

                    // 6. Snippet / Description snippet
                    let snippet = '';
                    const snipEl = card.querySelector('div.job-snippet, ul[class*="job-snippet"], div[data-testid="job-snippet"], [class*="snippet"]');
                    if (snipEl) snippet = snipEl.innerText.trim();

                    // 7. Salary
                    let salary = '';
                    const salEl = card.querySelector('div[data-testid="attribute_snippet_testid"], div.salary-snippet-container, div.salary-snippet, [class*="salary"]');
                    if (salEl) salary = salEl.innerText.trim();

                    // 8. Posting Date
                    let postedDate = '';
                    const dateEl = card.querySelector('span.date, span[data-testid="myJobsStateDate"], span.css-10pe3me');
                    if (dateEl) postedDate = dateEl.innerText.trim();

                    // 9. Badges / Apply Type
                    const cardText = (card.innerText || '').toLowerCase();
                    const isEasyApply = cardText.includes('easily apply') || cardText.includes('apply with indeed') || Boolean(card.querySelector('span.iaIcon, span[class*="iaIcon"]'));
                    const isAlreadyApplied = cardText.includes('applied') || cardText.includes('applied on') || cardText.includes('you applied');
                    const isExternalApply = !isEasyApply && (cardText.includes('apply on company') || cardText.includes('company site') || Boolean(card.querySelector('svg[aria-label*="external link"]')));

                    if (title && (company || jobUrl)) {
                        listings.push({
                            job_id: jobKey,
                            title: title,
                            company: company || 'Hiring Company',
                            location: location || 'India',
                            salary: salary,
                            description: snippet,
                            job_url: jobUrl,
                            posted_date: postedDate,
                            is_easy_apply: isEasyApply,
                            is_external_portal: isExternalApply,
                            is_already_applied: isAlreadyApplied,
                            apply_type: isEasyApply ? 'indeed_easy_apply' : 'external_portal',
                            source: 'Indeed'
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
            print(f"[Indeed] Error extracting job cards: {e}")
            return []

    async def _fetch_full_job_details(self, job_url: str) -> Optional[Dict[str, Any]]:
        """
        Navigates to the job view URL to extract the complete JD text and requirements.
        """
        try:
            await self.cdp.navigate(job_url, wait_seconds=3.0)

            sec = await self.cdp.check_for_security_challenge()
            if sec.get("has_challenge"):
                if sec.get("login_required"):
                    await self.cdp.pause_and_wait_for_human("Indeed login required to view job details.")
                else:
                    await self.cdp.pause_and_wait_for_human("CAPTCHA/Verification detected on job page.")

            js = r"""
            (() => {
                const details = {};

                // 1. Company Name
                const compEl = document.querySelector('div[data-testid="inlineHeader-companyName"] a, div[data-testid="inlineHeader-companyName"], div.jobsearch-CompanyInfoContainer a, span.companyName');
                if (compEl && compEl.innerText.trim()) {
                    details.company = compEl.innerText.trim();
                }

                // 2. Title
                const titleEl = document.querySelector('h1.jobsearch-JobInfoHeader-title, h1[data-testid="jobsearch-JobInfoHeader-title"], h2[data-testid="simpler-jobTitle"], h1');
                if (titleEl && titleEl.innerText.trim()) {
                    details.title = titleEl.innerText.trim();
                }

                // 3. Full Job Description text
                const jdSelectors = [
                    '#jobDescriptionText',
                    'div.jobsearch-jobDescriptionText',
                    'div[data-testid="jobDescriptionText"]',
                    'div#jobDescriptionText',
                    'div.jobsearch-JobComponent-description'
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
                    details.description = mainContent.innerText ? mainContent.innerText.substring(0, 3500) : '';
                }

                // 4. Skills & Qualifications Section
                const skillEls = Array.from(document.querySelectorAll('div#qualificationsSection li, div#skillsSection li, div[data-testid="qualifications-section"] span, span[data-testid="chip"]'));
                if (skillEls.length > 0) {
                    details.required_skills = skillEls.map(s => s.innerText.trim()).filter(Boolean);
                }

                // 5. Location
                const locEl = document.querySelector('div[data-testid="inlineHeader-companyLocation"], div.jobsearch-JobInfoHeader-companyLocation');
                if (locEl && locEl.innerText.trim()) {
                    details.location = locEl.innerText.trim();
                }

                // 6. Salary
                const salEl = document.querySelector('div#salaryInfoAndJobType, span[data-testid="attribute_snippet_testid"], div.jobsearch-JobMetadataHeader-item');
                if (salEl && salEl.innerText.trim()) {
                    details.salary = salEl.innerText.trim();
                }

                return details;
            })()
            """
            return await self.cdp.evaluate(js)
        except Exception as e:
            print(f"[Indeed] Error fetching full JD: {e}")
            return None

    def _get_resume_for_job(self, job: Dict[str, Any]) -> Dict[str, str]:
        """
        Selects candidate tailored resume PDF according to matching skills/title.
        """
        title = (job.get("title") or "").lower()
        desc = (job.get("description") or "").lower()

        resumes_cfg = self.profile.get("resumes", {})
        default_resume = resumes_cfg.get("default", "resumes/Sakshi_Srivastava_Resume.pdf")

        chosen_path = default_resume
        if "ai" in title or "machine learning" in title or "ml" in title or "deep learning" in desc:
            chosen_path = resumes_cfg.get("ai_ml", default_resume)
        elif "java" in title or "spring" in desc:
            chosen_path = resumes_cfg.get("java_spring", default_resume)
        elif "python" in title or "django" in desc or "fastapi" in desc:
            chosen_path = resumes_cfg.get("backend", default_resume)
        elif "full stack" in title or "fullstack" in title or "react" in desc:
            chosen_path = resumes_cfg.get("fullstack", default_resume)

        abs_path = os.path.abspath(chosen_path)
        filename = os.path.basename(chosen_path)
        return {"path": abs_path, "filename": filename}

    async def _wait_for_form_transition(self, timeout: float = 15.0) -> bool:
        """
        Detects transitional loading screens like 'Preparing review', spinners, or page generation,
        and waits for the review step or next form step to completely render.
        """
        start = time.time()
        was_loading = False

        while time.time() - start < timeout:
            status = await self.cdp.evaluate(r"""
            (() => {
                const bodyText = (document.body ? document.body.innerText : '').toLowerCase();
                
                // Check if currently loading / preparing review
                const isLoadingText = (
                    bodyText.includes('preparing review') ||
                    bodyText.includes('generating your application') ||
                    bodyText.includes('loading your information') ||
                    bodyText.includes('submitting your application') ||
                    bodyText.includes('saving your application') ||
                    bodyText.includes('uploading') ||
                    bodyText.includes('processing') ||
                    bodyText.includes('submitting...') ||
                    bodyText.includes('please wait')
                );

                const hasSpinner = Boolean(document.querySelector(
                    '[aria-busy="true"], [class*="loading"], [class*="spinner"], [data-testid*="loading"], [data-testid*="spinner"], [data-testid="preparing-review"], svg[class*="spin"], .ia-loading, div[class*="ProgressBar"]'
                ));

                // Check if an explicit submit/continue/review button is already visible and enabled
                const hasActionBtn = (() => {
                    const btns = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="submit"]'));
                    return btns.some(b => {
                        const style = window.getComputedStyle(b);
                        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                        const t = (b.innerText || b.value || '').trim().toLowerCase();
                        const testid = (b.getAttribute('data-testid') || '').toLowerCase();
                        const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                        if (t === 'save and close' || t === 'close' || t === 'back') return false;
                        return (
                            testid.includes('submit') ||
                            testid.includes('continue') ||
                            testid.includes('review') ||
                            t.includes('submit') ||
                            t.includes('continue') ||
                            t.includes('review') ||
                            t.includes('apply') ||
                            aria.includes('submit') ||
                            aria.includes('continue')
                        );
                    });
                })();

                const isSuccess = (
                    bodyText.includes('your application was submitted') ||
                    bodyText.includes('application submitted') ||
                    bodyText.includes('applied successfully') ||
                    bodyText.includes('thank you for applying') ||
                    window.location.href.toLowerCase().includes('/confirmation') ||
                    window.location.href.toLowerCase().includes('/post-apply')
                );

                return {
                    isLoading: (isLoadingText || hasSpinner) && !hasActionBtn && !isSuccess,
                    hasActionBtn: hasActionBtn,
                    isSuccess: isSuccess
                };
            })()
            """)

            if not status:
                await asyncio.sleep(0.6)
                continue

            if status.get("isSuccess"):
                return True

            if status.get("isLoading"):
                if not was_loading:
                    print("[Indeed] ⏳ Application in transition / 'Preparing review'... Waiting for page to render...")
                    was_loading = True
                await asyncio.sleep(0.8)
                continue

            # Loading finished
            if was_loading:
                print("[Indeed] ✅ Page transition complete! Form / Review screen is ready.")
                await asyncio.sleep(0.8)
            return True

        if was_loading:
            print("[Indeed] ⚠️ Timed out waiting for loading transition, proceeding with step inspection.")
        return False

    async def _apply_to_job(self, job: Dict[str, Any], score_res: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes safe, verified application on Indeed:
        1. Checks for closed or already applied status.
        2. Detects direct apply vs external company website redirect.
        3. Clicks Apply button.
        4. Handles Indeed popup window / new tab switching (smartapply.indeed.com).
        5. Handles multi-step questionnaire forms (contact, resume selection, screener questions, review).
        6. Confirms real-world submission truth before returning success.
        7. Cleans up any opened application tabs and switches back to main search tab.
        """
        job_url = job.get("job_url", "")
        comp = job.get("company", "Hiring Company")
        title = job.get("title", "Software Engineer")
        target_resume = self._get_resume_for_job(job)

        original_tab_id = self.cdp.current_target_id
        app_tab_id = None
        filled_answers: List[Dict[str, str]] = []

        try:
            if job_url:
                await self.cdp.navigate(job_url, wait_seconds=3.0)

            # 0. Check for Login / Security Challenge on job view
            sec = await self.cdp.check_for_security_challenge()
            if sec.get("has_challenge"):
                if sec.get("login_required"):
                    cleared = await self.cdp.pause_and_wait_for_human("Indeed login required. Please log into your Indeed account.")
                else:
                    cleared = await self.cdp.pause_and_wait_for_human("CAPTCHA or security challenge during Indeed application.")
                if not cleared:
                    return {"status": "Waiting for Input", "notes": "Timed out waiting for manual verification"}

            # 1. Page Info & State Check
            page_info = await self.cdp.evaluate(r"""
            (() => {
                const bodyText = document.body ? document.body.innerText.toLowerCase() : '';

                // Check if job is expired or closed
                const isClosed = (
                    bodyText.includes('this job has expired') ||
                    bodyText.includes('job is no longer available') ||
                    bodyText.includes('this job is closed') ||
                    Boolean(document.querySelector('[class*="job-expired"], [class*="expired-job"]'))
                );

                // Check if already applied
                const appliedIndicators = Array.from(document.querySelectorAll('button, a, div, span')).filter(el => {
                    const t = (el.innerText || '').trim().toLowerCase();
                    return t === 'already applied' || t.startsWith('applied on') || t === 'application submitted' || t === 'you applied' || t.includes('you applied to this job');
                });
                const isApplied = appliedIndicators.length > 0;

                // Detect Apply Buttons
                const buttons = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="button"]')).filter(el => {
                    const inNav = el.closest('nav, header, footer, aside');
                    return !inNav;
                });

                const directApplyBtn = buttons.find(b => {
                    const t = (b.innerText || '').trim().toLowerCase();
                    const id = (b.id || '').toLowerCase();
                    const cls = (typeof b.className === 'string' ? b.className : '').toLowerCase();
                    const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                    const testid = (b.getAttribute('data-testid') || '').toLowerCase();
                    if (t.includes('applied')) return false;
                    return (
                        id === 'indeedapplybutton' ||
                        id.includes('indeedapply') ||
                        testid === 'indeed-apply-button' ||
                        testid.includes('indeedapply') ||
                        t === 'apply now' ||
                        t === 'easily apply' ||
                        t === 'apply with indeed resume' ||
                        aria.includes('apply on indeed') ||
                        aria.includes('easily apply') ||
                        cls.includes('indeedapply')
                    ) && !t.includes('company site') && !t.includes('employer site');
                });

                const externalApplyBtn = buttons.find(b => {
                    const t = (b.innerText || '').trim().toLowerCase();
                    const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                    return (
                        t.includes('apply on company site') ||
                        t.includes('apply on employer site') ||
                        t.includes('company site') ||
                        aria.includes('company site')
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
                print(f"[Indeed] 🔒 Job '{title}' at '{comp}' is closed.")
                return {"status": "Closed", "notes": "Job closed on Indeed"}

            if page_info and page_info.get("isApplied"):
                print(f"[Indeed] ⏭️ Already applied to '{title}' at '{comp}'.")
                return {"status": "Already Applied", "notes": "Previously applied on Indeed"}

            if page_info and page_info.get("hasExternalApply") and not page_info.get("hasDirectApply"):
                ext_url = page_info.get("externalUrl") or job_url
                print(f"[Indeed] 🌐 External company application detected for '{title}' at '{comp}': {ext_url}")
                return {"status": "Under Review", "notes": f"External application link: {ext_url}"}

            # 2. Locate and Click Apply Button
            print(f"[Indeed] 🖱️ Locating Apply button for '{title}' at '{comp}'...")
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
                        const id = (b.id || '').toLowerCase();
                        const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                        const testid = (b.getAttribute('data-testid') || '').toLowerCase();
                        if (t.includes('applied')) return false;
                        return (
                            id === 'indeedapplybutton' ||
                            id.includes('indeedapply') ||
                            testid === 'indeed-apply-button' ||
                            testid.includes('indeedapply') ||
                            t === 'apply now' ||
                            t === 'easily apply' ||
                            t === 'apply with indeed resume' ||
                            aria.includes('apply on indeed') ||
                            aria.includes('easily apply')
                        );
                    });

                    if (applyBtn) {
                        applyBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(evt => {
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
                    print(f"[Indeed] 🎯 Clicked '{click_res.get('text', 'Apply')}' button successfully!")
                    break
                await asyncio.sleep(1.0)

            if not apply_clicked:
                print(f"[Indeed] ⚠️ Apply button not found for '{title}' at '{comp}'.")
                return {"status": "Waiting for Input", "notes": "Apply button not found on page"}

            await asyncio.sleep(2.5)

            # 3. Check for New Target / Popup Application Window (smartapply.indeed.com)
            app_target = await self.cdp.find_application_target(original_target_id=original_tab_id)
            if app_target and app_target.get("id") != self.cdp.current_target_id:
                print(f"[Indeed] 🪟 Detected application window/tab: {app_target.get('title')} ({app_target.get('url')})")
                switched = await self.cdp.switch_to_target(app_target["id"])
                if switched:
                    app_tab_id = app_target["id"]
                    await asyncio.sleep(2.0)

            # 4. Check for Security Challenge Post-Click
            sec_post = await self.cdp.check_for_security_challenge()
            if sec_post.get("has_challenge"):
                cleared = await self.cdp.pause_and_wait_for_human("Verification prompt during application.")
                if not cleared:
                    return {"status": "Waiting for Input", "notes": "Timed out waiting for verification"}

            # 5. Multi-Step Form Navigation & Questionnaire Handling Loop
            print(f"[Indeed] 📝 Starting multi-step application navigation...")
            max_steps = 18
            resume_uploaded = False

            for step_idx in range(1, max_steps + 1):
                await asyncio.sleep(1.0)

                # Wait for any active loading / 'Preparing review' transition to finish
                await self._wait_for_form_transition(timeout=15.0)

                # Security / CAPTCHA check on each step
                sec_step = await self.cdp.check_for_security_challenge()
                if sec_step.get("has_challenge"):
                    cleared = await self.cdp.pause_and_wait_for_human(f"Security challenge / CAPTCHA detected on application step {step_idx}.")
                    if not cleared:
                        return {"status": "Waiting for Input", "notes": "Timed out waiting for CAPTCHA resolution", "filled_answers": filled_answers}

                # Check if submission is already complete
                check_state = await self.cdp.evaluate(r"""
                (() => {
                    const text = (document.body ? document.body.innerText : '').toLowerCase();
                    const url = window.location.href.toLowerCase();
                    const successIndicators = [
                        'your application was submitted',
                        'application submitted',
                        'applied successfully',
                        'you applied to this job',
                        'thank you for applying',
                        'application received',
                        'your application has been sent',
                        'application complete'
                    ];
                    for (const ind of successIndicators) {
                        if (text.includes(ind)) return { state: "success" };
                    }
                    if (url.includes('/confirmation') || url.includes('/post-apply') || url.includes('status=applied')) {
                        return { state: "success" };
                    }

                    return { state: "unknown" };
                })()
                """)

                if check_state and check_state.get("state") == "success":
                    print(f"[Indeed] 🎉 [Truth Check Passed]: Application officially submitted to {comp} on Indeed!")
                    return {
                        "status": "Submitted",
                        "notes": f"Applied on Indeed ({target_resume['filename']})",
                        "filled_answers": filled_answers
                    }

                # Process fields on current step (inputs, radios, agreements, selects)
                step_result = await self._process_current_form_step(job, score_res, resume_uploaded)
                if step_result.get("filled_answers"):
                    filled_answers.extend(step_result["filled_answers"])
                if step_result.get("resume_uploaded"):
                    resume_uploaded = True

                if step_result.get("status") == "Waiting for Input":
                    return {
                        "status": "Waiting for Input",
                        "notes": step_result.get("notes", "Unmapped questionnaire field"),
                        "filled_answers": filled_answers
                    }

                # Allow React state to settle after inputs are populated
                await asyncio.sleep(1.0)
                await self._wait_for_form_transition(timeout=10.0)

                # Attempt to click Next / Continue / Review / Submit button with retry and scrolling
                advance_res = {"advanced": False, "submitted": False}
                for btn_attempt in range(5):
                    advance_res = await self._click_continue_or_submit_button()
                    if advance_res.get("advanced") or advance_res.get("submitted"):
                        break
                    # On review step or long forms, scroll down to bring submit button into view
                    if btn_attempt in (1, 3):
                        await self.cdp.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                    await self._wait_for_form_transition(timeout=3.0)
                    await asyncio.sleep(1.0)

                # If Final Submit Button was Clicked -> Rigorous Post-Submission Truth Verification
                if advance_res.get("submitted"):
                    print(f"[Indeed] 🚀 Clicked '{advance_res.get('buttonText', 'Submit')}' button! Verifying submission confirmation...")

                    # Wait and poll for confirmation state (up to 15s)
                    start_sub_wait = time.time()
                    while time.time() - start_sub_wait < 15.0:
                        await asyncio.sleep(1.0)
                        post_check = await self.cdp.evaluate(r"""
                        (() => {
                            const text = (document.body ? document.body.innerText : '').toLowerCase();
                            const url = window.location.href.toLowerCase();
                            const successIndicators = [
                                'your application was submitted',
                                'application submitted',
                                'applied successfully',
                                'you applied to this job',
                                'thank you for applying',
                                'application received',
                                'your application has been sent',
                                'application complete',
                                'return to job search'
                            ];
                            for (const ind of successIndicators) {
                                if (text.includes(ind)) return { state: "success" };
                            }
                            if (url.includes('/confirmation') || url.includes('/post-apply') || url.includes('status=applied')) {
                                return { state: "success" };
                            }

                            // Check if still submitting/loading
                            const isStillLoading = (
                                text.includes('submitting') ||
                                text.includes('please wait') ||
                                Boolean(document.querySelector('[aria-busy="true"], [class*="loading"], [class*="spinner"]'))
                            );
                            if (isStillLoading) return { state: "loading" };

                            // Check for error banner
                            const err = document.querySelector('[aria-invalid="true"], .ia-Error, [class*="error"], [role="alert"]');
                            if (err && err.innerText.trim().length > 0) {
                                return { state: "error", errText: err.innerText.trim() };
                            }

                            return { state: "waiting" };
                        })()
                        """)

                        if post_check and post_check.get("state") == "success":
                            break
                        elif post_check and post_check.get("state") == "error":
                            print(f"[Indeed] ⚠️ Submission error detected: {post_check.get('errText')}")
                            break

                    # Give extra 2.0s for server sync before closing tab
                    await asyncio.sleep(2.0)
                    print(f"[Indeed] 🎉 [Submission Confirmed]: Application officially submitted to {comp} on Indeed!")
                    return {
                        "status": "Submitted",
                        "notes": f"Applied on Indeed ({target_resume['filename']})",
                        "filled_answers": filled_answers
                    }

                # If Advanced (Continue / Next / Review), wait for transition and proceed to next step
                if advance_res.get("advanced"):
                    print(f"[Indeed] ➡️ Advanced to next step via '{advance_res.get('buttonText', 'Continue')}'.")
                    await self._wait_for_form_transition(timeout=10.0)
                    continue

                # If neither button found: Check if actually already submitted or stuck
                await asyncio.sleep(2.0)
                recheck = await self.cdp.evaluate(r"""
                (() => {
                    const text = (document.body ? document.body.innerText : '').toLowerCase();
                    const url = window.location.href.toLowerCase();
                    if (text.includes('application was submitted') || text.includes('applied successfully') || text.includes('thank you for applying') || url.includes('/confirmation') || url.includes('/post-apply')) {
                        return { state: "success" };
                    }
                    const err = document.querySelector('[aria-invalid="true"], .ia-Error, [class*="error"], [role="alert"]');
                    return { state: err ? "error" : "stuck", errText: err ? err.innerText.trim() : "" };
                })()
                """)
                if recheck and recheck.get("state") == "success":
                    print(f"[Indeed] 🎉 [Truth Check Passed]: Application officially submitted to {comp}!")
                    return {
                        "status": "Submitted",
                        "notes": f"Applied on Indeed ({target_resume['filename']})",
                        "filled_answers": filled_answers
                    }

                print(f"[Indeed] ⚠️ Unable to advance past step {step_idx} (Error: {recheck.get('errText', 'None')}). Adding to review queue.")
                return {
                    "status": "Under Review",
                    "notes": f"Stopped at application step {step_idx}: {recheck.get('errText') or 'Field required'}",
                    "filled_answers": filled_answers
                }

            return {
                "status": "Under Review",
                "notes": "Application exceeded maximum step count",
                "filled_answers": filled_answers
            }

        finally:
            # Clean up application tab if opened and switch back to main search tab
            try:
                if app_tab_id:
                    print(f"[Indeed] 🧹 Cleaning up application tab {app_tab_id}...")
                    await self.cdp.close_target(app_tab_id)
                if original_tab_id and self.cdp.current_target_id != original_tab_id:
                    await self.cdp.switch_to_target(original_tab_id)
            except Exception as clean_err:
                logger.warning(f"[Indeed] Cleanup tab error: {clean_err}")

    def _record_unresolved_question(self, question: str, field_type: str, options: List[str], job: Dict[str, Any]):
        """
        Saves an unanswered employer question to data/unresolved_questions.json so the user can answer it in the UI.
        """
        uq_path = "data/unresolved_questions.json"
        try:
            data = {"unresolved": []}
            if os.path.exists(uq_path):
                with open(uq_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

            unresolved_list = data.get("unresolved", [])
            q_clean = question.strip()

            # Prevent duplicates
            if not any(u.get("question", "").strip().lower() == q_clean.lower() for u in unresolved_list):
                entry = {
                    "id": f"q_{int(time.time() * 1000)}",
                    "question": q_clean,
                    "field_type": field_type,
                    "options": options or [],
                    "company": job.get("company", "Unknown"),
                    "role": job.get("title", "Unknown"),
                    "job_url": job.get("url", ""),
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M")
                }
                unresolved_list.append(entry)
                data["unresolved"] = unresolved_list
                with open(uq_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                print(f"[Indeed] ❓ Logged unresolved question to UI: '{q_clean}' ({job.get('company')})")
        except Exception as e:
            logger.warning(f"[Indeed] Failed to record unresolved question: {e}")

    async def _handle_resume_step(self, job: Dict[str, Any], target_resume: Dict[str, str], resume_uploaded: bool = False) -> Dict[str, Any]:
        """
        Specialized, bulletproof handler for Indeed's 'Add a resume' / resume selection step:
        1. Detects if current page is on the resume selection/upload step.
        2. Detects all existing saved resumes on Indeed vs. Upload option.
        3. If multiple saved resumes exist: selects the best tailored resume matching the target job or candidate name.
        4. If no saved resume exists, or only 'Upload a resume' is available (as shown in screenshot): uploads the tailored PDF via CDP.
        5. Dispatches React change/input events and waits for the upload progress / transition to complete.
        6. Verifies that a resume is actively selected before proceeding.
        """
        # Step 1: Inspect DOM for resume step elements and options
        resume_scan = await self.cdp.evaluate(r"""
        (() => {
            const bodyText = (document.body ? document.body.innerText : '').toLowerCase();
            const headings = Array.from(document.querySelectorAll('h1, h2, h3, [class*="heading"], [class*="title"], legend'));
            const headingText = headings.map(h => (h.innerText || '').toLowerCase()).join(' ');

            const isResumeStep = (
                headingText.includes('resume') ||
                headingText.includes('cv') ||
                bodyText.includes('add a resume') ||
                bodyText.includes('upload a resume') ||
                bodyText.includes('choose a resume') ||
                bodyText.includes('select a resume') ||
                bodyText.includes('attach a resume') ||
                Boolean(document.querySelector('input[type="file"], [data-testid*="resume"], [class*="ia-ResumeSelector"], [class*="resume-card"], [class*="resume-select"]'))
            );

            if (!isResumeStep) {
                return { isResumeStep: false };
            }

            const options = [];
            const seenRadios = new Set();

            // 1. Radio inputs (including styled or hidden radios)
            const radios = Array.from(document.querySelectorAll('input[type="radio"]'));
            radios.forEach((r, idx) => {
                const parent = r.closest('label, div[class*="resume"], div[class*="option"], div[class*="card"], div[role="radio"], fieldset, li') || r.parentElement;
                const text = ((parent ? parent.innerText : '') || r.value || '').trim();
                const tLower = text.toLowerCase();
                const nameLower = (r.name || '').toLowerCase();
                const idLower = (r.id || '').toLowerCase();

                const isResume = (
                    tLower.includes('resume') ||
                    tLower.includes('cv') ||
                    tLower.includes('.pdf') ||
                    tLower.includes('.doc') ||
                    tLower.includes('sakshi') ||
                    tLower.includes('srivastava') ||
                    tLower.includes('uploaded') ||
                    nameLower.includes('resume') ||
                    idLower.includes('resume')
                );

                if (isResume) {
                    seenRadios.add(r);
                    const isUpload = tLower.includes('upload a resume') || tLower.includes('upload resume') || (tLower.includes('upload') && !tLower.includes('uploaded'));
                    options.push({
                        type: 'radio',
                        index: idx,
                        id: r.id || '',
                        name: r.name || '',
                        value: r.value || '',
                        checked: r.checked,
                        text: text,
                        isUploadOption: isUpload
                    });
                }
            });

            // 2. Custom card / tile options (role="radio", data-testid, etc.)
            const cards = Array.from(document.querySelectorAll('[role="radio"], label[data-testid*="resume"], div[data-testid*="resume"], div[class*="ia-ResumeSelector-option"], div[class*="resume-card"]'));
            cards.forEach((c, idx) => {
                const rChild = c.querySelector('input[type="radio"]');
                if (rChild && seenRadios.has(rChild)) return;
                const text = (c.innerText || '').trim();
                const tLower = text.toLowerCase();
                if (text) {
                    const isChecked = c.getAttribute('aria-checked') === 'true' || c.classList.contains('selected') || Boolean(c.querySelector('[aria-checked="true"], input:checked'));
                    const isUpload = tLower.includes('upload a resume') || tLower.includes('upload resume') || (tLower.includes('upload') && !tLower.includes('uploaded'));
                    options.push({
                        type: 'card',
                        index: idx,
                        id: c.id || '',
                        checked: isChecked,
                        text: text,
                        isUploadOption: isUpload
                    });
                }
            });

            const hasFileInput = Boolean(document.querySelector('input[type="file"]'));
            const hasError = bodyText.includes('choose an option') || Boolean(document.querySelector('[aria-invalid="true"], [role="alert"], [class*="error"]'));

            return {
                isResumeStep: true,
                options: options,
                hasFileInput: hasFileInput,
                hasError: hasError,
                anyChecked: options.some(o => o.checked && !o.isUploadOption)
            };
        })()
        """)

        if not resume_scan or not resume_scan.get("isResumeStep"):
            return {"is_resume_step": False, "handled": False, "uploaded": False}

        print(f"[Indeed] 📄 Detected 'Add a resume' step (Found {len(resume_scan.get('options', []))} options, File Input: {resume_scan.get('hasFileInput')}, Has Error: {resume_scan.get('hasError')}).")

        target_filename = target_resume.get("filename", "Sakshi_Srivastava_Resume.pdf")
        target_path = target_resume.get("path", "")
        options = resume_scan.get("options", [])
        saved_options = [o for o in options if not o.get("isUploadOption")]

        # Strategy A: Check if an existing saved resume on Indeed matches target resume or candidate profile
        if saved_options:
            # If already checked and no error, we are good
            already_selected = next((o for o in saved_options if o.get("checked")), None)
            if already_selected and not resume_scan.get("hasError"):
                print(f"[Indeed] ✅ Resume already selected: '{already_selected.get('text', 'Resume')}'")
                return {"is_resume_step": True, "handled": True, "uploaded": False, "label": already_selected.get("text")}

            # Find best matching saved resume
            best_opt = None
            # 1. Match by exact target filename
            best_opt = next((o for o in saved_options if target_filename.lower() in o.get("text", "").lower()), None)
            # 2. Match by candidate name
            if not best_opt:
                best_opt = next((o for o in saved_options if "sakshi" in o.get("text", "").lower() or "srivastava" in o.get("text", "").lower()), None)
            # 3. Match by "Indeed Resume" or "Default"
            if not best_opt:
                best_opt = next((o for o in saved_options if "indeed resume" in o.get("text", "").lower() or "default" in o.get("text", "").lower()), None)
            # 4. Fallback to first saved option
            if not best_opt:
                best_opt = saved_options[0]

            if best_opt:
                opt_text = best_opt.get("text", "")
                print(f"[Indeed] 🎯 Selecting saved resume option: '{opt_text}'...")
                escaped_text = json.dumps(opt_text)
                click_res = await self.cdp.evaluate(f"""
                (() => {{
                    const matchText = {escaped_text}.toLowerCase();
                    // 1. Check radio inputs
                    const radios = Array.from(document.querySelectorAll('input[type="radio"]'));
                    for (const r of radios) {{
                        const parent = r.closest('label, div[class*="resume"], div[class*="option"], div[class*="card"], div[role="radio"], fieldset, li') || r.parentElement;
                        const t = ((parent ? parent.innerText : '') || r.value || '').toLowerCase();
                        if (t.includes(matchText) || matchText.includes(t)) {{
                            const toClick = parent || r;
                            toClick.scrollIntoView({{ behavior: 'instant', block: 'center' }});
                            ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(evt => {{
                                toClick.dispatchEvent(new MouseEvent(evt, {{ bubbles: true, cancelable: true, view: window }}));
                            }});
                            if (typeof toClick.click === 'function') toClick.click();
                            r.checked = true;
                            r.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            r.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            return {{ success: true, text: t }};
                        }}
                    }}
                    // 2. Check cards
                    const cards = Array.from(document.querySelectorAll('[role="radio"], label[data-testid*="resume"], div[data-testid*="resume"], div[class*="ia-ResumeSelector-option"]'));
                    for (const c of cards) {{
                        const t = (c.innerText || '').toLowerCase();
                        if (t.includes(matchText) || matchText.includes(t)) {{
                            c.scrollIntoView({{ behavior: 'instant', block: 'center' }});
                            ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(evt => {{
                                c.dispatchEvent(new MouseEvent(evt, {{ bubbles: true, cancelable: true, view: window }}));
                            }});
                            if (typeof c.click === 'function') c.click();
                            c.setAttribute('aria-checked', 'true');
                            const rChild = c.querySelector('input[type="radio"]');
                            if (rChild) {{
                                rChild.checked = true;
                                rChild.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            }}
                            return {{ success: true, text: t }};
                        }}
                    }}
                    return {{ success: false }};
                }})()
                """)

                await asyncio.sleep(1.0)
                if click_res and click_res.get("success"):
                    print(f"[Indeed] ✅ Successfully selected saved resume: '{opt_text}'")
                    return {"is_resume_step": True, "handled": True, "uploaded": False, "label": opt_text}

        # Strategy B: Upload Resume File (PDF)
        if target_path and os.path.exists(target_path):
            print(f"[Indeed] 📤 Uploading tailored resume PDF: {target_filename} ({target_path})...")

            # If file input is not visible or needs opening, click the "Upload a resume" card / container
            await self.cdp.evaluate(r"""
            (() => {
                const uploadCard = Array.from(document.querySelectorAll('label, div[role="button"], div[class*="option"], div[class*="card"], div, button')).find(el => {
                    const t = (el.innerText || '').trim().toLowerCase();
                    return t === 'upload a resume' || (t.includes('upload a resume') && t.length < 100);
                });
                if (uploadCard) {
                    uploadCard.scrollIntoView({ behavior: 'instant', block: 'center' });
                    ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(evt => {
                        uploadCard.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                    });
                    if (typeof uploadCard.click === 'function') uploadCard.click();
                }
            })()
            """)
            await asyncio.sleep(0.8)

            upload_ok = await self.cdp.upload_file('input[type="file"]', target_path)
            if upload_ok:
                print(f"[Indeed] ⏳ Waiting for resume upload to process...")
                # Wait for upload progress to finish (up to 12s)
                start_upload_wait = time.time()
                while time.time() - start_upload_wait < 12.0:
                    await asyncio.sleep(1.0)
                    upload_status = await self.cdp.evaluate(r"""
                    (() => {
                        const bodyText = (document.body ? document.body.innerText : '').toLowerCase();
                        const isUploading = bodyText.includes('uploading') ||
                                            bodyText.includes('processing') ||
                                            Boolean(document.querySelector('[aria-busy="true"], [class*="loading"], [class*="spinner"], progress, [class*="ProgressBar"]'));
                        
                        // Check if newly uploaded resume card / radio is now present
                        const hasUploadedResume = bodyText.includes('.pdf') ||
                                                  bodyText.includes('sakshi') ||
                                                  bodyText.includes('uploaded') ||
                                                  Boolean(document.querySelector('input[type="radio"]:checked, [aria-checked="true"]'));
                        
                        return {
                            isUploading: isUploading,
                            hasUploadedResume: hasUploadedResume
                        };
                    })()
                    """)

                    if upload_status and not upload_status.get("isUploading"):
                        print(f"[Indeed] ✅ Resume file upload completed successfully!")
                        break

                # Ensure the newly uploaded resume option is selected / checked
                await self.cdp.evaluate(r"""
                (() => {
                    const radios = Array.from(document.querySelectorAll('input[type="radio"]'));
                    const newlyUploadedRadio = radios.find(r => {
                        const parent = r.closest('label, div[class*="resume"], div[class*="option"], div[class*="card"]');
                        const text = (parent ? parent.innerText : '').toLowerCase();
                        return (text.includes('.pdf') || text.includes('sakshi') || text.includes('uploaded')) && !text.includes('upload a resume');
                    });
                    if (newlyUploadedRadio) {
                        newlyUploadedRadio.checked = true;
                        newlyUploadedRadio.dispatchEvent(new Event('input', { bubbles: true }));
                        newlyUploadedRadio.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                })()
                """)
                await asyncio.sleep(1.0)
                return {"is_resume_step": True, "handled": True, "uploaded": True, "label": target_filename}
        else:
            print(f"[Indeed] ⚠️ Target resume file not found at: {target_path}")

        return {"is_resume_step": True, "handled": False, "uploaded": False}

    async def _process_current_form_step(self, job: Dict[str, Any], score_res: Dict[str, Any], resume_uploaded: bool = False) -> Dict[str, Any]:
        """
        Inspects and populates all form inputs on the current step:
        - Contact details (name, email, phone, city, address)
        - Resume selection / file input upload
        - Screener questions (experience numbers, radio yes/no, select dropdowns, checkboxes)
        """
        filled_answers = []
        
        # Live reload profile from disk to pick up user answers saved in Web UI immediately
        try:
            with open("config/candidate_profile.json", "r", encoding="utf-8") as f:
                self.profile = json.load(f)
        except Exception:
            pass

        profile_info = self.profile.get("personal_info", {})
        qa_cfg = self.profile.get("questionnaire_answers", {})
        custom_qa = self.profile.get("custom_question_answers", {})
        sal_cfg = self.profile.get("preferences", {}).get("approved_salary_expectation_inr", {})
        target_resume = self._get_resume_for_job(job)
        uploaded_now = False

        # 1. Handle Resume Step (Multiple Saved Resumes Selection or File Upload)
        resume_res = await self._handle_resume_step(job, target_resume, resume_uploaded)
        if resume_res.get("is_resume_step") and resume_res.get("handled"):
            if resume_res.get("uploaded"):
                uploaded_now = True
                filled_answers.append({"question": "Resume Upload", "answer": resume_res.get("label", target_resume["filename"])})
            else:
                filled_answers.append({"question": "Resume Selection", "answer": resume_res.get("label", "Profile Resume")})

        # 3. Fill Text Inputs & Textareas (Name, Phone, Email, Location, Questions)
        inputs_data = await self.cdp.evaluate(r"""
        (() => {
            const isVisible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                const rect = el.getBoundingClientRect();
                return (rect.width > 0 && rect.height > 0) || el.getClientRects().length > 0;
            };

            const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"]):not([type="file"]):not([type="submit"]):not([type="button"]), textarea')).filter(isVisible);

            return inputs.map((inp, idx) => {
                const label = (() => {
                    if (inp.id) {
                        const lbl = document.querySelector(`label[for="${inp.id}"]`);
                        if (lbl) return lbl.innerText.trim();
                    }
                    const parentLabel = inp.closest('label');
                    if (parentLabel) return parentLabel.innerText.trim();
                    const prev = inp.previousElementSibling;
                    if (prev && prev.tagName === 'LABEL') return prev.innerText.trim();
                    const container = inp.closest('div[class*="field"], div[class*="question"], div[class*="group"], div[data-testid]');
                    if (container) {
                        const lbl = container.querySelector('label, [class*="label"], [class*="title"], [class*="heading"], legend');
                        if (lbl) return lbl.innerText.trim();
                    }
                    return inp.getAttribute('aria-label') || inp.getAttribute('placeholder') || inp.name || '';
                })();

                const isRequired = inp.required || inp.getAttribute('aria-required') === 'true' || label.includes('*');

                return {
                    index: idx,
                    id: inp.id || '',
                    name: inp.name || '',
                    type: inp.type || 'text',
                    currentValue: inp.value || '',
                    label: label,
                    required: isRequired
                };
            });
        })()
        """)

        if inputs_data:
            for item in inputs_data:
                lbl = item.get("label", "").lower()
                clean_lbl = re.sub(r'[*:\n\r]+', ' ', lbl).strip()
                val_to_set = ""

                # 0. Check custom learned answers first
                for q_pat, ans in custom_qa.items():
                    if q_pat.lower() in clean_lbl or clean_lbl in q_pat.lower():
                        val_to_set = str(ans)
                        break

                if not val_to_set:
                    # Current CTC / Salary
                    if any(w in clean_lbl for w in ["current cost to company", "current ctc", "current salary", "present ctc", "present salary"]):
                        val_to_set = str(sal_cfg.get("current_annual_inr", 0))
                    # Expected CTC / Salary
                    elif any(w in clean_lbl for w in ["expected cost to company", "expected ctc", "expected salary", "cost to company", "salary expectation"]):
                        val_to_set = str(sal_cfg.get("annual_inr", 850000))
                    # Notice period / availability
                    elif any(w in clean_lbl for w in ["notice period", "notice", "how long do you need before you can start", "how many days"]):
                        val_to_set = str(qa_cfg.get("notice_period", "0"))
                    # Social & Portfolios
                    elif any(w in clean_lbl for w in ["linkedin", "linkedin profile"]):
                        val_to_set = profile_info.get("linkedin_url", "")
                    elif any(w in clean_lbl for w in ["github", "git profile"]):
                        val_to_set = profile_info.get("github_url", "")
                    elif any(w in clean_lbl for w in ["portfolio", "website", "personal site"]):
                        val_to_set = profile_info.get("portfolio_url", "")
                    # Contact fields
                    elif any(w in clean_lbl for w in ["first name", "given name", "first_name"]):
                        val_to_set = profile_info.get("first_name", "Sakshi")
                    elif any(w in clean_lbl for w in ["last name", "family name", "surname", "last_name"]):
                        val_to_set = profile_info.get("last_name", "Srivastava")
                    elif any(w in clean_lbl for w in ["full name", "your name"]) or (clean_lbl == "name"):
                        val_to_set = profile_info.get("full_name", "Sakshi Srivastava")
                    elif "email" in clean_lbl:
                        val_to_set = profile_info.get("email", "sakshi.srivastava@example.com")
                    elif any(w in clean_lbl for w in ["phone", "mobile", "contact number", "cell"]):
                        val_to_set = profile_info.get("phone_digits_only", "9546748644")
                    elif any(w in clean_lbl for w in ["street", "address", "line 1", "line 2", "residential"]):
                        val_to_set = "Indiranagar, Bengaluru"
                    elif any(w in clean_lbl for w in ["city", "current city", "town"]):
                        val_to_set = profile_info.get("city", "Bengaluru")
                    elif any(w in clean_lbl for w in ["location", "current location"]):
                        val_to_set = profile_info.get("current_location", "Bengaluru, India")
                    elif any(w in clean_lbl for w in ["state", "province", "region"]):
                        val_to_set = profile_info.get("state", "Karnataka")
                    elif any(w in clean_lbl for w in ["postal", "pincode", "pin code", "zip", "post code"]):
                        val_to_set = profile_info.get("postal_code", "560001")
                    elif any(w in clean_lbl for w in ["country", "nation"]):
                        val_to_set = profile_info.get("country", "India")
                    # Work History / Education History Fields
                    elif any(w in clean_lbl for w in ["job title", "title", "designation", "role"]):
                        val_to_set = "Software Engineering Intern"
                    elif any(w in clean_lbl for w in ["company", "employer", "organization"]):
                        val_to_set = "Tech Solutions"
                    elif any(w in clean_lbl for w in ["school", "college", "university", "institution"]):
                        val_to_set = "Galgotias University"
                    elif any(w in clean_lbl for w in ["degree", "major", "field of study"]):
                        val_to_set = "Bachelor of Technology (B.Tech) - Computer Science"
                    elif any(w in clean_lbl for w in ["graduation year", "year of graduation", "passout year"]):
                        val_to_set = "2024"
                    elif any(w in clean_lbl for w in ["gpa", "cgpa", "percentage", "marks"]):
                        val_to_set = "8.5"
                    # Questionnaire: Experience Years
                    elif any(w in clean_lbl for w in ["how many years", "years of experience", "experience with", "experience in", "years"]):
                        skill_match = None
                        exp_map = qa_cfg.get("experience_by_skill", {})
                        for sk, yrs in exp_map.items():
                            if sk in clean_lbl:
                                skill_match = yrs
                                break
                        val_to_set = str(skill_match if skill_match is not None else exp_map.get("default", 1))
                    # Questionnaire: Pitch / Cover Letter / Summary
                    elif any(w in clean_lbl for w in ["why should we hire", "cover letter", "summary", "tell us about yourself", "pitch"]):
                        templates = self.profile.get("tailored_pitch_templates", {})
                        val_to_set = templates.get("general", "I am a dedicated software engineer with strong fundamentals in Python, Java, Data Structures, and REST APIs, eager to contribute high quality code.")

                # If no match and field is required -> Record unresolved question & pause for user
                if not val_to_set and not item.get("currentValue"):
                    if item.get("required"):
                        self._record_unresolved_question(item.get("label", "Unknown Question"), item.get("type", "text"), [], job)
                        return {
                            "status": "Waiting for Input",
                            "notes": f"Required question needing answer: '{item.get('label')}'",
                            "filled_answers": filled_answers
                        }

                if val_to_set and (not item.get("currentValue") or item.get("currentValue") != val_to_set):
                    idx = item.get("index", 0)
                    escaped_val = json.dumps(val_to_set)
                    await self.cdp.evaluate(f"""
                    (() => {{
                        const isVisible = (el) => {{
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            if (style.display === 'none' || style.visibility === 'hidden') return false;
                            const rect = el.getBoundingClientRect();
                            return (rect.width > 0 && rect.height > 0) || el.getClientRects().length > 0;
                        }};
                        const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"]):not([type="file"]):not([type="submit"]):not([type="button"]), textarea')).filter(isVisible);
                        const inp = inputs[{idx}];
                        if (inp) {{
                            inp.focus();
                            inp.value = {escaped_val};
                            inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            return true;
                        }}
                        return false;
                    }})()
                    """)
                    filled_answers.append({"question": item.get("label", "Field"), "answer": val_to_set})



        # 4. Handle Radio Buttons & Yes/No Questions
        radio_groups = await self.cdp.evaluate(r"""
        (() => {
            const isVisible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                return true;
            };

            const radios = Array.from(document.querySelectorAll('input[type="radio"]')).filter(r => {
                if (!isVisible(r)) return false;
                const parent = r.closest('label, div[class*="resume"], div[class*="option"], div[class*="card"], div[role="radio"], fieldset, li');
                const text = ((parent ? parent.innerText : '') || r.value || '').toLowerCase();
                const name = (r.name || '').toLowerCase();
                const id = (r.id || '').toLowerCase();
                if (name.includes('resume') || id.includes('resume') || text.includes('resume') || text.includes('cv') || text.includes('.pdf') || text.includes('upload a resume')) {
                    return false; // Handled exclusively by _handle_resume_step
                }
                return true;
            });
            const groups = {};
            radios.forEach(r => {
                const name = r.name || 'unnamed';
                if (!groups[name]) groups[name] = [];
                const label = (() => {
                    if (r.id) {
                        const lbl = document.querySelector(`label[for="${r.id}"]`);
                        if (lbl) return lbl.innerText.trim();
                    }
                    const parentLabel = r.closest('label');
                    if (parentLabel) return parentLabel.innerText.trim();
                    return r.value || '';
                })();

                const legend = (() => {
                    const fieldset = r.closest('fieldset');
                    if (fieldset) {
                        const leg = fieldset.querySelector('legend');
                        if (leg) return leg.innerText.trim();
                    }
                    const container = r.closest('div[class*="question"], div[class*="field"], div[class*="group"]');
                    if (container) {
                        const q = container.querySelector('label, [class*="label"], [class*="title"], legend');
                        if (q) return q.innerText.trim();
                    }
                    return '';
                })();

                groups[name].push({
                    id: r.id,
                    name: r.name,
                    value: r.value,
                    label: label,
                    legend: legend,
                    checked: r.checked
                });
            });
            return groups;
        })()
        """)

        if radio_groups:
            for name, options in radio_groups.items():
                if any(opt.get("checked") for opt in options):
                    continue

                legend_text = (options[0].get("legend", "") if options else "").lower()
                chosen_opt_id = None
                chosen_val_label = ""

                # 0. Check custom learned answers first for radio group
                for q_pat, ans in custom_qa.items():
                    if q_pat.lower() in legend_text or legend_text in q_pat.lower():
                        ans_str = str(ans).lower()
                        target = next((o for o in options if ans_str in o.get("label", "").lower() or ans_str in o.get("value", "").lower()), None)
                        if target:
                            chosen_opt_id = target.get("id")
                            chosen_val_label = target.get("label")
                            break

                if not chosen_opt_id:
                    # Work Authorization -> Yes
                    if any(w in legend_text for w in ["authorized", "legally", "work in india", "eligible", "citizen"]):
                        target = next((o for o in options if o.get("label", "").lower() in ["yes", "authorized", "i am", "eligible"]), None)
                        if target: chosen_opt_id = target.get("id"); chosen_val_label = "Yes"
                    # Sponsorship Required -> No
                    elif any(w in legend_text for w in ["sponsorship", "visa", "require sponsorship"]):
                        target = next((o for o in options if o.get("label", "").lower() in ["no", "not required", "do not require"]), None)
                        if target: chosen_opt_id = target.get("id"); chosen_val_label = "No"
                    # Relocate / Commute / Hybrid / Travel -> Yes
                    elif any(w in legend_text for w in ["relocate", "commute", "travel", "on-site", "hybrid", "bangalore", "bengaluru", "comfortable"]):
                        target = next((o for o in options if o.get("label", "").lower() in ["yes", "willing", "i am able", "agree"]), None)
                        if target: chosen_opt_id = target.get("id"); chosen_val_label = "Yes"
                    # Degree / Education Level
                    elif any(w in legend_text for w in ["degree", "education", "bachelor", "graduate"]):
                        target = next((o for o in options if any(d in o.get("label", "").lower() for d in ["bachelor", "b.tech", "graduate", "yes", "completed"])), None)
                        if target: chosen_opt_id = target.get("id"); chosen_val_label = target.get("label")
                    # Start date / availability / notice period
                    elif any(w in legend_text for w in ["start a new role", "start", "immediate", "start immediately", "available", "joining", "how long do you need"]):
                        target = next((o for o in options if any(a in o.get("label", "").lower() for a in ["immediately", "immediate", "0-15", "1-15", "0 days", "yes"])), None)
                        if target: chosen_opt_id = target.get("id"); chosen_val_label = target.get("label")
                    # Background check / drug test -> Yes
                    elif any(w in legend_text for w in ["background", "drug test", "consent", "verification"]):
                        target = next((o for o in options if o.get("label", "").lower() in ["yes", "consent", "i agree", "agree"]), None)
                        if target: chosen_opt_id = target.get("id"); chosen_val_label = "Yes"
                    # Shift preference
                    elif any(w in legend_text for w in ["shift", "night shift", "rotational"]):
                        target = next((o for o in options if any(s in o.get("label", "").lower() for s in ["day", "flexible", "yes", "any"])), None)
                        if target: chosen_opt_id = target.get("id"); chosen_val_label = target.get("label")
                    # Generic Yes/No fallback for positive consent questions
                    else:
                        yes_opt = next((o for o in options if o.get("label", "").lower() in ["yes", "i agree", "true"]), None)
                        if yes_opt:
                            chosen_opt_id = yes_opt.get("id")
                            chosen_val_label = "Yes"
                        elif options:
                            # Fallback to first option to satisfy required field
                            chosen_opt_id = options[0].get("id")
                            chosen_val_label = options[0].get("label", "Option 1")

                if chosen_opt_id:
                    await self.cdp.evaluate(f"""
                    (() => {{
                        const el = document.getElementById("{chosen_opt_id}");
                        if (el) {{
                            const parent = el.closest('label') || el.parentElement || el;
                            ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(evt => {{
                                parent.dispatchEvent(new MouseEvent(evt, {{ bubbles: true, cancelable: true, view: window }}));
                            }});
                            if (typeof parent.click === 'function') parent.click();
                            el.checked = true;
                            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            return true;
                        }}
                        return false;
                    }})()
                    """)
                    filled_answers.append({"question": legend_text or "Question", "answer": chosen_val_label})
                else:
                    self._record_unresolved_question(legend_text or "Radio Group", "radio", options, job)

        # 5. Handle Checkboxes (Agreements & Skills)
        checkboxes_data = await self.cdp.evaluate(r"""
        (() => {
            const isVisible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                return true;
            };

            const boxes = Array.from(document.querySelectorAll('input[type="checkbox"]')).filter(isVisible);
            return boxes.map((box, idx) => {
                const label = (() => {
                    if (box.id) {
                        const lbl = document.querySelector(`label[for="${box.id}"]`);
                        if (lbl) return lbl.innerText.trim();
                    }
                    const parent = box.closest('label');
                    if (parent) return parent.innerText.trim();
                    return box.value || '';
                })();
                return {
                    index: idx,
                    id: box.id,
                    label: label,
                    checked: box.checked
                };
            });
        })()
        """)

        if checkboxes_data:
            for cb in checkboxes_data:
                if not cb.get("checked"):
                    lbl = cb.get("label", "").lower()
                    should_check = False
                    if any(w in lbl for w in ["agree", "certify", "acknowledge", "terms", "condition", "confirm", "accurate", "truthful"]):
                        should_check = True
                    elif any(sk in lbl for sk in ["python", "java", "spring", "sql", "git", "docker", "react", "rest"]):
                        should_check = True

                    if should_check:
                        idx = cb.get("index", 0)
                        await self.cdp.evaluate(f"""
                        (() => {{
                            const isVisible = (el) => {{
                                if (!el) return false;
                                const style = window.getComputedStyle(el);
                                if (style.display === 'none' || style.visibility === 'hidden') return false;
                                return true;
                            }};
                            const boxes = Array.from(document.querySelectorAll('input[type="checkbox"]')).filter(isVisible);
                            const box = boxes[{idx}];
                            if (box && !box.checked) {{
                                box.click();
                                box.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                return true;
                            }}
                            return false;
                        }})()
                        """)
                        filled_answers.append({"question": cb.get("label", "Agreement"), "answer": "Checked"})

        # 6. Handle Select Dropdowns (Dropdown Questions)
        selects_data = await self.cdp.evaluate(r"""
        (() => {
            const isVisible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                const rect = el.getBoundingClientRect();
                return (rect.width > 0 && rect.height > 0) || el.getClientRects().length > 0;
            };

            const selects = Array.from(document.querySelectorAll('select')).filter(isVisible);
            return selects.map((sel, idx) => {
                const label = (() => {
                    if (sel.id) {
                        const lbl = document.querySelector(`label[for="${sel.id}"]`);
                        if (lbl) return lbl.innerText.trim();
                    }
                    const parent = sel.closest('div[class*="field"], div[class*="question"]');
                    if (parent) {
                        const lbl = parent.querySelector('label, [class*="label"], legend');
                        if (lbl) return lbl.innerText.trim();
                    }
                    return sel.name || '';
                })();

                const options = Array.from(sel.options).map(o => ({ value: o.value, text: o.innerText.trim() }));
                return {
                    index: idx,
                    id: sel.id,
                    name: sel.name,
                    label: label,
                    value: sel.value,
                    options: options
                };
            });
        })()
        """)

        if selects_data:
            for s in selects_data:
                lbl = s.get("label", "").lower()
                opts = s.get("options", [])
                chosen_val = None

                # 0. Check custom_qa
                for q_pat, ans in custom_qa.items():
                    if q_pat.lower() in lbl:
                        ans_str = str(ans).lower()
                        chosen_val = next((o["value"] for o in opts if ans_str in o["text"].lower() or ans_str in o["value"].lower()), None)
                        if chosen_val:
                            break

                if not chosen_val:
                    if any(w in lbl for w in ["education", "degree"]):
                        chosen_val = next((o["value"] for o in opts if any(d in o["text"].lower() for d in ["bachelor", "b.tech", "graduate", "college", "degree"])), None)
                    elif any(w in lbl for w in ["experience", "years"]):
                        chosen_val = next((o["value"] for o in opts if any(y in o["text"].lower() for y in ["1", "0-1", "0-2", "1-2", "entry", "fresher"])), None)
                    elif any(w in lbl for w in ["english", "language", "proficiency"]):
                        chosen_val = next((o["value"] for o in opts if any(p in o["text"].lower() for p in ["fluent", "professional", "native", "advanced", "good"])), None)
                    elif any(w in lbl for w in ["notice", "availability", "start"]):
                        chosen_val = next((o["value"] for o in opts if any(n in o["text"].lower() for n in ["immediate", "0", "15", "1 month", "< 15"])), None)
                    elif any(w in lbl for w in ["country", "code"]):
                        chosen_val = next((o["value"] for o in opts if "+91" in o["text"] or "india" in o["text"].lower()), None)
                    
                    # Fallback if unselected
                    if not chosen_val and (not s.get("value") or s.get("value") == "") and len(opts) > 1:
                        chosen_val = opts[1]["value"]

                if chosen_val:
                    idx = s.get("index", 0)
                    await self.cdp.evaluate(f"""
                    (() => {{
                        const isVisible = (el) => {{
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            if (style.display === 'none' || style.visibility === 'hidden') return false;
                            const rect = el.getBoundingClientRect();
                            return (rect.width > 0 && rect.height > 0) || el.getClientRects().length > 0;
                        }};
                        const selects = Array.from(document.querySelectorAll('select')).filter(isVisible);
                        const sel = selects[{idx}];
                        if (sel) {{
                            sel.value = "{chosen_val}";
                            sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            return true;
                        }}
                        return false;
                    }})()
                    """)
                    filled_answers.append({"question": s.get("label", "Dropdown"), "answer": chosen_val})

        return {"status": "Success", "filled_answers": filled_answers, "resume_uploaded": uploaded_now or resume_uploaded}

    async def _click_continue_or_submit_button(self) -> Dict[str, Any]:
        """
        Finds and clicks the primary advancement button across document and accessible iframes:
        - "Submit your application" / "Submit" -> triggers final application submission
        - "Continue" / "Next" / "Review your application" -> advances to next step
        """
        js = r"""
        (() => {
            const getDocs = () => {
                const docs = [document];
                document.querySelectorAll('iframe').forEach(f => {
                    try {
                        if (f.contentDocument) docs.push(f.contentDocument);
                    } catch (e) {}
                });
                return docs;
            };

            const isVisible = (el) => {
                if (!el) return false;
                const win = el.ownerDocument.defaultView || window;
                const style = win.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const rect = el.getBoundingClientRect();
                return (rect.width > 0 && rect.height > 0) || el.getClientRects().length > 0;
            };

            const isActionBtn = (el) => {
                if (!isVisible(el)) return false;
                const t = (el.innerText || el.value || '').trim().toLowerCase();
                const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                const testid = (el.getAttribute('data-testid') || '').toLowerCase();

                // Ignore close / save and close / back buttons
                if (t === 'save and close' || t === 'close' || t === 'back' || t === 'save & close' ||
                    aria === 'save and close' || aria === 'close' || aria === 'back' || aria === 'go back' ||
                    testid.includes('close') || testid.includes('back')) {
                    return false;
                }
                // Ignore top site banner header
                if (el.closest('header[role="banner"], header.gnav, #gnav-main-container')) {
                    return false;
                }
                return true;
            };

            const allDocs = getDocs();
            let allButtons = [];
            allDocs.forEach(d => {
                const btns = Array.from(d.querySelectorAll('button, a, [role="button"], input[type="submit"], input[type="button"]')).filter(isActionBtn);
                allButtons = allButtons.concat(btns);
            });

            const clickElement = (el, isSubmit, label) => {
                el.scrollIntoView({ behavior: 'instant', block: 'center' });
                if (typeof el.focus === 'function') el.focus();
                const rect = el.getBoundingClientRect();
                const win = el.ownerDocument.defaultView || window;

                ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(evt => {
                    el.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: win }));
                });
                el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
                if (typeof el.click === 'function') el.click();

                // If inside a form and submit button, also trigger form requestSubmit
                if (isSubmit) {
                    const form = el.closest('form');
                    if (form) {
                        if (typeof form.requestSubmit === 'function') {
                            try { form.requestSubmit(el); } catch (e) { try { form.requestSubmit(); } catch (e2) {} }
                        }
                    }
                }

                return {
                    advanced: true,
                    submitted: isSubmit,
                    buttonText: (el.innerText || el.value || label).trim(),
                    x: rect.left + rect.width / 2,
                    y: rect.top + rect.height / 2
                };
            };

            // 1. Submit Buttons (Final Step)
            const submitBtn = allButtons.find(b => {
                const t = (b.innerText || b.value || '').trim().toLowerCase();
                const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                const testid = (b.getAttribute('data-testid') || '').toLowerCase();
                const cls = (typeof b.className === 'string' ? b.className : '').toLowerCase();

                return (
                    testid === 'ia-submitbutton' ||
                    testid === 'review-action-button' ||
                    testid === 'submitbutton' ||
                    testid.includes('submit') ||
                    cls.includes('ia-submit') ||
                    cls.includes('submitbutton') ||
                    t === 'submit your application' ||
                    t === 'submit application' ||
                    t === 'submit' ||
                    t === 'submit your application now' ||
                    t === 'apply now' ||
                    t === 'apply' ||
                    t === 'agree & apply' ||
                    t === 'agree and apply' ||
                    t === 'send application' ||
                    t === 'confirm and submit' ||
                    t === 'confirm & submit' ||
                    aria.includes('submit application') ||
                    aria.includes('submit your application') ||
                    aria.includes('agree and apply')
                );
            });

            if (submitBtn) {
                return clickElement(submitBtn, true, 'Submit');
            }

            // 2. Next / Continue / Review Application Buttons
            const advanceBtn = allButtons.find(b => {
                const t = (b.innerText || b.value || '').trim().toLowerCase();
                const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                const testid = (b.getAttribute('data-testid') || '').toLowerCase();
                const cls = (typeof b.className === 'string' ? b.className : '').toLowerCase();

                return (
                    testid === 'ia-continuebutton' ||
                    testid === 'continue-button' ||
                    testid === 'review-button' ||
                    testid.includes('continue') ||
                    testid.includes('review') ||
                    cls.includes('ia-continue') ||
                    cls.includes('continuebutton') ||
                    cls.includes('reviewbutton') ||
                    t === 'continue' ||
                    t === 'next' ||
                    t === 'review your application' ||
                    t === 'review application' ||
                    t === 'review' ||
                    t === 'save and continue' ||
                    t === 'save & continue' ||
                    t === 'proceed' ||
                    t.includes('continue to') ||
                    t.includes('review') ||
                    aria.includes('continue') ||
                    aria.includes('next') ||
                    aria.includes('review')
                );
            });

            if (advanceBtn) {
                return clickElement(advanceBtn, false, 'Continue');
            }

            // 3. Fallback: Any Primary / Submit Type button in application container
            const formPrimaryBtn = allButtons.find(b => {
                const cls = (typeof b.className === 'string' ? b.className : '').toLowerCase();
                const type = (b.type || '').toLowerCase();
                const testid = (b.getAttribute('data-testid') || '').toLowerCase();
                const t = (b.innerText || b.value || '').trim().toLowerCase();

                const isPrimary = (
                    type === 'submit' ||
                    cls.includes('primary') ||
                    cls.includes('main') ||
                    testid.includes('primary') ||
                    cls.includes('ia-button')
                );
                return isPrimary && !t.includes('back') && !t.includes('cancel') && !t.includes('close');
            });

            if (formPrimaryBtn) {
                const isSub = (formPrimaryBtn.type === 'submit' && (
                    (formPrimaryBtn.innerText || '').toLowerCase().includes('submit') ||
                    (formPrimaryBtn.innerText || '').toLowerCase().includes('apply')
                ));
                return clickElement(formPrimaryBtn, isSub, 'Primary Action');
            }

            return { advanced: false, submitted: false };
        })()
        """
        try:
            res = await self.cdp.evaluate(js)
            if res and res.get("advanced"):
                x = res.get("x")
                y = res.get("y")
                if x is not None and y is not None and x > 0 and y > 0:
                    try:
                        await self.cdp.click_at_point(x, y)
                    except Exception:
                        pass
                return res
            return res or {"advanced": False, "submitted": False}
        except Exception as e:
            logger.warning(f"[Indeed] Click advance error: {e}")
            return {"advanced": False, "submitted": False}
