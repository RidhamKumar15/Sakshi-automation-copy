import asyncio
import json
import logging
import urllib.parse
from typing import Any, Dict, List, Optional

from src.cdp_client import ChromeCDPClient
from src.scorer import JobScorer
from src.tracker import ApplicationTracker

logger = logging.getLogger("LinkedInPlatform")


class LinkedInAutomation:
    """
    Automates job search, scoring, and Easy Apply on LinkedIn.
    Uses the dedicated Chrome profile via CDP.
    """

    BASE_URL = "https://www.linkedin.com/jobs/search"

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

    async def search_and_process_jobs(self, keywords: Optional[List[str]] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        """
        Executes LinkedIn job search and processing.
        """
        max_to_apply = limit if limit is not None else self.max_applications

        results_summary = {
            "source": "LinkedIn",
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

        search_terms = keywords or self.settings.get("sources", {}).get("linkedin", {}).get("search_keywords", ["Software Engineer"])

        for keyword in search_terms:
            if results_summary["applications_submitted"] >= max_to_apply:
                print(f"[LinkedIn] Reached max applications limit ({max_to_apply}). Stopping search.")
                break

            print(f"\n[LinkedIn] 🔍 Searching jobs for keyword: '{keyword}'...")
            query_param = urllib.parse.quote(keyword)
            # f_AL=true filters for Easy Apply
            search_url = f"{self.BASE_URL}/?keywords={query_param}&location=India&f_AL=true&f_E=1%2C2"

            await self.cdp.navigate(search_url, wait_seconds=4.0)

            # Security / Login Check
            sec_status = await self.cdp.check_for_security_challenge()
            if sec_status.get("has_challenge"):
                if sec_status.get("login_required"):
                    await self.cdp.pause_and_wait_for_human("LinkedIn login required. Please log into LinkedIn in Chrome.")
                else:
                    await self.cdp.pause_and_wait_for_human("Security verification challenge on LinkedIn.")

            # Scroll list
            await self.cdp.evaluate("window.scrollBy(0, 600);")
            await asyncio.sleep(2.0)

            extracted_jobs = await self._extract_job_listings_from_page()
            results_summary["jobs_found"] += len(extracted_jobs)
            print(f"[LinkedIn] Extracted {len(extracted_jobs)} job listings from search.")

            for job in extracted_jobs:
                if results_summary["applications_submitted"] >= self.max_applications:
                    break

                results_summary["jobs_evaluated"] += 1
                comp = job.get("company", "Unknown")
                title = job.get("title", "Unknown")
                url = job.get("job_url", "")
                job_id = job.get("job_id", "")

                print(f"\n------------------------------------------------------------")
                print(f"📌 Evaluating: {title} at {comp}")
                print(f"   URL: {url}")

                # 1. Duplicate Check
                is_dup, prev_record = self.tracker.is_duplicate(comp, title, url, job_id)
                if is_dup and prev_record:
                    print(f"   ⏭️ Skipping duplicate: Already tracked as '{prev_record.get('status')}'")
                    results_summary["duplicates_ignored"] += 1
                    continue

                # 2. Score Job
                score_res = self.scorer.evaluate(job)
                total_score = score_res["total_score"]
                decision = score_res["decision"]
                print(f"   📊 Match Score: {total_score}/100 -> Decision: {decision}")

                # 3. Handle by Decision
                if decision == "SKIP":
                    print(f"   ❌ Score {total_score} < 55. Skipping job.")
                    results_summary["jobs_skipped"] += 1
                    self.tracker.record_job(
                        company=comp,
                        role=title,
                        source="LinkedIn",
                        job_url=url,
                        match_score=total_score,
                        status="Skipped",
                        notes=f"Low match score ({total_score}/100).",
                        job_id=job_id
                    )
                    continue

                elif decision == "REVIEW_QUEUE":
                    print(f"   📝 Score {total_score} in review range (55-69). Adding to Review Queue.")
                    results_summary["jobs_under_review"] += 1
                    results_summary["attention_urls"].append(url)
                    self.tracker.record_job(
                        company=comp,
                        role=title,
                        source="LinkedIn",
                        job_url=url,
                        match_score=total_score,
                        status="Under Review",
                        notes=f"Moderate match ({total_score}/100). Queued for human review.",
                        job_id=job_id
                    )
                    continue

                elif decision == "AUTO_APPLY":
                    print(f"   🚀 High score {total_score} >= 70. Processing Easy Apply...")
                    self.tracker.record_job(
                        company=comp,
                        role=title,
                        source="LinkedIn",
                        job_url=url,
                        match_score=total_score,
                        status="Applying",
                        notes=f"High match ({total_score}/100). Applying now.",
                        job_id=job_id
                    )

                    app_result = await self._apply_to_job(job, score_res)

                    if app_result["status"] == "Submitted":
                        results_summary["applications_submitted"] += 1
                        self.tracker.update_status(url, "Submitted", notes=f"Applied via LinkedIn Easy Apply (Score: {total_score})")
                    elif app_result["status"] == "Waiting for Input":
                        results_summary["applications_manual_action"] += 1
                        results_summary["attention_urls"].append(url)
                        self.tracker.update_status(url, "Waiting for Input", notes=app_result.get("notes", "Requires manual input"))
                    elif app_result["status"] == "Already Applied":
                        results_summary["duplicates_ignored"] += 1
                        self.tracker.update_status(url, "Already Applied", notes="Already applied on LinkedIn")
                    elif app_result["status"] == "Closed":
                        self.tracker.update_status(url, "Closed", notes="Listing no longer accepting applications")
                    else:
                        results_summary["errors"].append(f"{comp} - {title}: {app_result.get('notes', 'Failed')}")
                        self.tracker.update_status(url, "Failed", notes=app_result.get("notes", "Easy Apply failed"))

                await asyncio.sleep(2.5)

        return results_summary

    async def _extract_job_listings_from_page(self) -> List[Dict[str, Any]]:
        """
        Parses visible job cards on LinkedIn Jobs page.
        """
        js = r"""
        (() => {
            const listings = [];
            const cards = document.querySelectorAll('.job-card-container, [data-job-id], li.jobs-search-results__list-item');

            cards.forEach((el, index) => {
                try {
                    const titleEl = el.querySelector('.job-card-list__title, a.job-card-container__link, strong');
                    const compEl = el.querySelector('.job-card-container__primary-description, .artdeco-entity-lockup__subtitle');
                    const linkEl = el.querySelector('a[href*="/jobs/view/"]');
                    const locEl = el.querySelector('.job-card-container__metadata-item, .artdeco-entity-lockup__caption');

                    const title = titleEl ? titleEl.innerText.trim() : "";
                    const company = compEl ? compEl.innerText.trim() : "";
                    const job_url = linkEl ? linkEl.href : "";
                    const location = locEl ? locEl.innerText.trim() : "India";
                    const description = el.innerText || "";

                    const jobIdAttr = el.getAttribute('data-job-id');
                    const idMatch = job_url.match(/view\/(\d+)/);
                    const job_id = jobIdAttr || (idMatch ? idMatch[1] : `li-${index}`);

                    if (title && company) {
                        listings.push({
                            title: title,
                            company: company,
                            job_url: job_url,
                            job_id: job_id,
                            location: location,
                            remote_status: location.toLowerCase().includes("remote") ? "Remote" : "Hybrid/On-site",
                            description: description,
                            source: "LinkedIn"
                        });
                    }
                } catch (e) {}
            });

            return listings;
        })()
        """
        try:
            listings = await self.cdp.evaluate(js)
            return listings or []
        except Exception as e:
            print(f"[LinkedIn] Error extracting job cards: {e}")
            return []

    async def _apply_to_job(self, job: Dict[str, Any], score_res: Dict[str, Any]) -> Dict[str, Any]:
        """
        Navigates to job details and handles Easy Apply modal.
        """
        job_url = job.get("job_url", "")
        if job_url:
            await self.cdp.navigate(job_url, wait_seconds=3.0)

        page_text = (await self.cdp.evaluate("document.body ? document.body.innerText.toLowerCase() : ''")) or ""
        if "no longer accepting applications" in page_text:
            return {"status": "Closed", "notes": "No longer accepting applications"}
        if "applied" in page_text and ("applied on" in page_text or "application submitted" in page_text):
            return {"status": "Already Applied", "notes": "Already applied on LinkedIn"}

        # Click Easy Apply button
        easy_apply_clicked = await self.cdp.evaluate("""
        (() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            const btn = buttons.find(b => {
                const text = b.innerText.trim().toLowerCase();
                return text === 'easy apply' || text.includes('easy apply');
            });
            if (btn) {
                btn.click();
                return true;
            }
            return false;
        })()
        """)

        if not easy_apply_clicked:
            return {"status": "Waiting for Input", "notes": "External application / Easy Apply not available"}

        await asyncio.sleep(2.0)

        # Handle multi-step modal loop (up to 5 steps)
        for step in range(5):
            # Check for CAPTCHA or unexpected forms
            sec = await self.cdp.check_for_security_challenge()
            if sec.get("has_challenge"):
                cleared = await self.cdp.pause_and_wait_for_human("Security challenge during LinkedIn Easy Apply.")
                if not cleared:
                    return {"status": "Waiting for Input", "notes": "Paused at security challenge"}

            modal_state = await self.cdp.evaluate("""
            (() => {
                const modal = document.querySelector('.jobs-easy-apply-modal, [role="dialog"]');
                if (!modal) return { open: false };

                const buttons = Array.from(modal.querySelectorAll('button'));
                const nextBtn = buttons.find(b => b.innerText.trim().toLowerCase() === 'next');
                const reviewBtn = buttons.find(b => b.innerText.trim().toLowerCase() === 'review');
                const submitBtn = buttons.find(b => b.innerText.trim().toLowerCase() === 'submit application');

                const unhandledInputs = Array.from(modal.querySelectorAll('input:required, select:required')).filter(i => !i.value);

                return {
                    open: true,
                    hasNext: Boolean(nextBtn),
                    hasReview: Boolean(reviewBtn),
                    hasSubmit: Boolean(submitBtn),
                    unhandledCount: unhandledInputs.length
                };
            })()
            """)

            if not modal_state or not modal_state.get("open"):
                break

            # If unhandled required inputs exist (e.g. unknown subjective questions), pause for human review
            if modal_state.get("unhandledCount", 0) > 0:
                print("[LinkedIn] Unanswered required application questions found.")
                cleared = await self.cdp.pause_and_wait_for_human("Please fill required custom questions in the LinkedIn modal.")
                if not cleared:
                    return {"status": "Waiting for Input", "notes": "Paused for custom question answer"}

            if modal_state.get("hasSubmit"):
                print(f"[LinkedIn] ✅ Final submit step reached for {job.get('company')}.")
                if self.settings.get("application", {}).get("auto_submit_verified_only", True):
                    await self.cdp.evaluate("""
                    (() => {
                        const buttons = Array.from(document.querySelectorAll('.jobs-easy-apply-modal button, [role="dialog"] button'));
                        const submitBtn = buttons.find(b => b.innerText.trim().toLowerCase() === 'submit application');
                        if (submitBtn) submitBtn.click();
                    })()
                    """)
                    await asyncio.sleep(2.5)
                    return {"status": "Submitted", "notes": "Submitted via LinkedIn Easy Apply"}
                else:
                    return {"status": "Waiting for Input", "notes": "Easy Apply filled, waiting for final user confirmation"}

            elif modal_state.get("hasReview"):
                await self.cdp.evaluate("""
                (() => {
                    const buttons = Array.from(document.querySelectorAll('.jobs-easy-apply-modal button, [role="dialog"] button'));
                    const btn = buttons.find(b => b.innerText.trim().toLowerCase() === 'review');
                    if (btn) btn.click();
                })()
                """)
                await asyncio.sleep(1.5)

            elif modal_state.get("hasNext"):
                await self.cdp.evaluate("""
                (() => {
                    const buttons = Array.from(document.querySelectorAll('.jobs-easy-apply-modal button, [role="dialog"] button'));
                    const btn = buttons.find(b => b.innerText.trim().toLowerCase() === 'next');
                    if (btn) btn.click();
                })()
                """)
                await asyncio.sleep(1.5)

        return {"status": "Submitted", "notes": "Completed LinkedIn Easy Apply flow"}
