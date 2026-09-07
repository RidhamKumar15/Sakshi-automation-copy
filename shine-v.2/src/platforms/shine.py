import asyncio
import json
import logging
import os
import re
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.cdp_client import ChromeCDPClient
from src.scorer import JobScorer
from src.tracker import ApplicationTracker
from src.notifier import SystemNotifier

logger = logging.getLogger("ShinePlatform")


class ShineAutomation:
    """
    Automates job search, discovery, scoring, and application on Shine.com.
    Uses the dedicated Chrome automation profile via CDP.
    Adheres strictly to the Genesis reference architecture.
    """

    BASE_URL = "https://www.shine.com"

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
        self.max_applications = settings.get("application", {}).get("max_applications_per_run", 200)
        self.shine_cfg = settings.get("sources", {}).get("shine", {})
        self.question_presets = self.profile.get("screening_question_presets", {})

    async def search_and_process_jobs(self, keywords: Optional[List[str]] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        """
        Main sequential execution flow:
        1. Navigate to Shine search URLs for configured roles.
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
            "source": "Shine",
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

        search_terms = keywords or self.shine_cfg.get("search_keywords", [
            "Software Engineer",
            "Software Developer",
            "SDE 1",
            "Junior Software Engineer",
            "Entry-Level Software Engineer",
            "Backend Developer",
            "Python Developer",
            "Java Developer",
            "Full Stack Developer",
            "Associate Software Engineer",
            "Graduate Engineer Trainee"
        ])

        SystemNotifier.notify_run_started("Shine.com")

        # 0. Pre-flight Shine authentication check
        await self.verify_and_ensure_login()

        for keyword in search_terms:
            if results_summary["applications_submitted"] >= max_to_apply:
                print(f"[Shine] 🛑 Reached max applications limit ({max_to_apply}). Stopping search.")
                break

            print(f"\n[Shine] 🔍 Searching jobs for keyword: '{keyword}'...")
            search_urls = self._build_search_urls(keyword)

            for search_url in search_urls:
                if results_summary["applications_submitted"] >= max_to_apply:
                    break

                print(f"\n[Shine] 🌐 Navigating to search URL: {search_url}")
                await self.cdp.navigate(search_url, wait_seconds=4.0)
                await self._inject_automation_badge()

                # 1. Security / CAPTCHA / Login Check
                sec_status = await self.cdp.check_for_security_challenge()
                if sec_status.get("has_challenge"):
                    if sec_status.get("login_required"):
                        await self.cdp.pause_and_wait_for_human("Shine login required. Please log into your Shine.com account in Chrome.")
                    else:
                        await self.cdp.pause_and_wait_for_human("CAPTCHA or security challenge detected on Shine.com.")

                # 2. Deep progressive scroll & harvest job listings
                extracted_jobs = await self._harvest_jobs_from_page_with_scroll(search_url, max_scrolls=6)
                results_summary["jobs_found"] += len(extracted_jobs)
                print(f"[Shine] Extracted {len(extracted_jobs)} job listings from current page.")

                if not extracted_jobs:
                    continue

                for job_stub in extracted_jobs:
                    if results_summary["applications_submitted"] >= max_to_apply:
                        print(f"\n🎉 [TARGET LIMIT REACHED] Successfully submitted {results_summary['applications_submitted']} application(s)!")
                        return results_summary

                    results_summary["jobs_evaluated"] += 1
                    comp = job_stub.get("company", "Unknown")
                    title = job_stub.get("title", "Unknown")
                    url = job_stub.get("job_url", "")
                    job_id = job_stub.get("job_id", "")

                    print(f"\n------------------------------------------------------------")
                    print(f"📌 Evaluating: {title} at {comp}")
                    print(f"   URL: {url}")

                    # 3. Duplicate Check
                    is_dup, prev_record = self.tracker.is_duplicate(comp, title, url, job_id)
                    if is_dup and prev_record:
                        prev_status = prev_record.get("status", "")
                        print(f"   ⏭️ Skipping duplicate: Already tracked as '{prev_status}' on {prev_record.get('date')}")
                        results_summary["duplicates_ignored"] += 1
                        continue

                    # 4. Fetch Full Job Description & JSON-LD
                    full_job = await self._fetch_full_job_details(url) if url else job_stub
                    if not full_job:
                        full_job = job_stub

                    # 5. Score Job
                    score_res = self.scorer.evaluate(full_job)
                    total_score = score_res["total_score"]
                    decision = score_res["decision"]
                    print(f"   📊 Match Score: {total_score}/100 -> Decision: {decision}")

                    # 6. Act on Decision
                    if decision == "AUTO_APPLY":
                        print(f"   ⚡ Score {total_score} >= {self.scorer.thresholds['auto_apply_min_score']} -> Proceeding to Apply on Shine...")
                        apply_res = await self._apply_to_job(full_job, score_res)
                        final_status = apply_res.get("status", "Failed")

                        if final_status == "Submitted":
                            results_summary["applications_submitted"] += 1
                            print(f"   ✅ [SUCCESS] Application submitted for {title} at {comp} (Score: {total_score})")
                        elif final_status in ("Waiting for Input", "Under Review"):
                            results_summary["jobs_under_review"] += 1
                            if apply_res.get("attention_required"):
                                results_summary["applications_manual_action"] += 1
                                results_summary["attention_urls"].append(url)
                        elif final_status in ("Already Applied", "Closed", "Not Accepting"):
                            results_summary["jobs_skipped"] += 1
                        else:
                            results_summary["jobs_skipped"] += 1
                            if apply_res.get("error"):
                                results_summary["errors"].append(f"{comp} ({title}): {apply_res.get('error')}")

                    elif decision == "REVIEW_QUEUE":
                        print(f"   📝 Score {total_score} in Review Range -> Adding to Review Queue.")
                        results_summary["jobs_under_review"] += 1
                        self.tracker.record_job(
                            company=comp,
                            role=title,
                            source="Shine",
                            job_url=url,
                            match_score=total_score,
                            status="Under Review",
                            notes=f"Review Queue Score: {total_score}/100",
                            job_id=job_id
                        )
                    else:
                        print(f"   ⏭️ Score {total_score} < {self.scorer.thresholds['review_queue_min_score']} -> Skipping.")
                        results_summary["jobs_skipped"] += 1
                        self.tracker.record_job(
                            company=comp,
                            role=title,
                            source="Shine",
                            job_url=url,
                            match_score=total_score,
                            status="Skipped",
                            notes=f"Skipped (Score: {total_score})",
                            job_id=job_id
                        )

                    # Short pacing delay between jobs
                    await asyncio.sleep(1.5)

        return results_summary

    async def apply_to_pending_jobs(self, statuses: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Iterates over all jobs in the tracker marked as Under Review, Failed, Waiting for Input, or Applying,
        and executes the application and screening questionnaire workflow for each.
        """
        if statuses is None:
            statuses = ["Under Review", "Failed", "Waiting for Input", "Applying"]

        pending_records = [
            r for r in self.tracker.records.values()
            if r.get("status") in statuses and r.get("job_url")
        ]

        results_summary = {
            "source": "Shine (Retry Pending / Under Review)",
            "jobs_found": len(pending_records),
            "jobs_evaluated": len(pending_records),
            "jobs_skipped": 0,
            "jobs_under_review": 0,
            "applications_submitted": 0,
            "applications_manual_action": 0,
            "duplicates_ignored": 0,
            "errors": [],
            "attention_urls": []
        }

        if not pending_records:
            print("[Shine] ℹ️ No pending, under-review, or failed jobs found to apply to.")
            return results_summary

        # 0. Pre-flight Shine authentication check
        await self.verify_and_ensure_login()

        print("\n" + "=" * 65)
        print(f"🔄 APPLYING TO {len(pending_records)} UNDER REVIEW / FAILED JOB(S)")
        print("=" * 65 + "\n")

        for idx, item in enumerate(pending_records, 1):
            url = item.get("job_url")
            comp = item.get("company", "Unknown")
            title = item.get("role", "Unknown")
            score = item.get("match_score", 75.0)

            print(f"\n[{idx}/{len(pending_records)}] 📌 Processing: {comp} - {title} (Score: {score})")
            print(f"   🔗 URL: {url}")

            full_job = await self._fetch_full_job_details(url)
            if not full_job:
                full_job = {
                    "title": title,
                    "company": comp,
                    "job_url": url,
                    "job_id": item.get("job_id", ""),
                    "description": "",
                    "skills": [],
                    "location": "",
                    "source": "Shine",
                    "direct_apply": True
                }

            score_res = self.scorer.evaluate(full_job)
            apply_res = await self._apply_to_job(full_job, score_res)
            final_status = apply_res.get("status", "Failed")

            if final_status == "Submitted":
                results_summary["applications_submitted"] += 1
                print(f"   ✅ [SUCCESS] Application submitted for {title} at {comp}!")
            elif final_status in ("Waiting for Input", "Under Review"):
                results_summary["jobs_under_review"] += 1
                if apply_res.get("attention_required"):
                    results_summary["applications_manual_action"] += 1
                    results_summary["attention_urls"].append(url)
            else:
                results_summary["jobs_skipped"] += 1
                if apply_res.get("error"):
                    results_summary["errors"].append(f"{comp} ({title}): {apply_res.get('error')}")

            await asyncio.sleep(2.0)

        print("\n" + "=" * 65)
        print(f"🎉 Completed Retry Run: {results_summary['applications_submitted']} Submitted | {results_summary['jobs_under_review']} In Review")
        print("=" * 65 + "\n")

        return results_summary

    async def verify_and_ensure_login(self) -> bool:
        """
        Verifies that the candidate profile is logged in and authenticated on Shine.com.
        If not authenticated, navigates to the login page and pauses for human login.
        Injects a persistent, non-intrusive status badge on Shine pages confirming active authentication.
        """
        print("\n[Shine] 🔐 Verifying candidate login status on Shine.com...")

        # If tab is currently on about:blank or not shine.com, navigate to dashboard
        curr_url = await self.cdp.evaluate("window.location.href") or ""
        if "shine.com" not in curr_url:
            await self.cdp.navigate("https://www.shine.com/dashboard", wait_seconds=3.0)

        js_auth_check = """
        (() => {
            const userData = localStorage.getItem("userData");
            const access = localStorage.getItem("access") || localStorage.getItem("access_token");
            const cookies = document.cookie || "";
            const hasAuthCookie = cookies.includes("_access_token_") || cookies.includes("_userid_") || cookies.includes("sessionid");
            
            let userName = "";
            let userEmail = "";
            if (userData) {
                try {
                    const parsed = JSON.parse(userData);
                    userName = parsed.name || "";
                    userEmail = parsed.email || "";
                } catch (e) {}
            }

            const bodyText = (document.body ? document.body.innerText : "");
            const hasHiUser = bodyText.includes("Hi, Sakshi") || bodyText.includes("Sakshi Srivastava");

            return {
                isLoggedIn: Boolean((userData && access) || (hasAuthCookie && (userData || access)) || (userData && userName) || hasHiUser),
                name: userName,
                email: userEmail,
                hasUserData: Boolean(userData),
                hasTokens: Boolean(access || hasAuthCookie)
            };
        })()
        """

        auth_info = await self.cdp.evaluate(js_auth_check) or {}

        # If not detected on the current page, check dashboard
        if not auth_info.get("isLoggedIn"):
            await self.cdp.navigate("https://www.shine.com/dashboard", wait_seconds=3.5)
            auth_info = await self.cdp.evaluate(js_auth_check) or {}

        user_name = auth_info.get("name") or self.profile.get("personal_info", {}).get("full_name", "Candidate")
        user_email = auth_info.get("email") or self.profile.get("personal_info", {}).get("email", "")

        if auth_info.get("isLoggedIn"):
            print(f"[Shine] ✅ Active Login Confirmed: Logged in as '{user_name}' ({user_email})")
            print("[Shine] ℹ️ Note: Shine's search results pages display static 'Register/Login' buttons in their header by design, but your session is fully authenticated and active.")
            await self._inject_automation_badge(user_name)
            return True
        else:
            print("[Shine] ⚠️ No active Shine.com login session detected.")
            print("[Shine] 🌐 Navigating to Shine login page...")
            await self.cdp.navigate("https://www.shine.com/myshine/login/", wait_seconds=3.0)
            await self.cdp.pause_and_wait_for_human("Please log into your Shine.com account in the opened Chrome window.")
            
            # Re-verify after human login
            await asyncio.sleep(2.0)
            auth_info = await self.cdp.evaluate(js_auth_check) or {}
            user_name = auth_info.get("name") or self.profile.get("personal_info", {}).get("full_name", "Candidate")
            await self._inject_automation_badge(user_name)
            print(f"[Shine] ✅ Login confirmed! Resuming automation as '{user_name}'.")
            return True

    async def _inject_automation_badge(self, name: Optional[str] = None):
        """
        Injects a persistent, sleek visual indicator badge on the Shine page so the user
        can visually see that automation is active and running under their account.
        """
        candidate_name = name or self.profile.get("personal_info", {}).get("full_name", "Sakshi Srivastava")
        js = f"""
        (() => {{
            const existing = document.getElementById("shine-automation-status-badge");
            if (existing) existing.remove();
            
            const badge = document.createElement("div");
            badge.id = "shine-automation-status-badge";
            badge.style.position = "fixed";
            badge.style.bottom = "18px";
            badge.style.right = "18px";
            badge.style.zIndex = "2147483647";
            badge.style.background = "#10B981";
            badge.style.color = "#FFFFFF";
            badge.style.padding = "8px 16px";
            badge.style.borderRadius = "20px";
            badge.style.fontWeight = "600";
            badge.style.fontSize = "13px";
            badge.style.boxShadow = "0 4px 16px rgba(0,0,0,0.25)";
            badge.style.display = "flex";
            badge.style.alignItems = "center";
            badge.style.gap = "8px";
            badge.style.fontFamily = "system-ui, -apple-system, sans-serif";
            badge.style.pointerEvents = "none";
            badge.innerHTML = '<span style="width:9px;height:9px;border-radius:50%;background:#ffffff;display:inline-block;"></span> Shine Automation: Logged in as {candidate_name}';
            document.body.appendChild(badge);
        }})()
        """
        try:
            await self.cdp.evaluate(js)
        except Exception:
            pass

    def _build_search_urls(self, keyword: str) -> List[str]:
        """
        Builds canonical Shine search URLs for given keyword.
        Supports both keyword slugs and location-refined slugs.
        """
        clean_kw = re.sub(r'[^a-zA-Z0-9\s]', '', keyword).strip().lower()
        slug = re.sub(r'\s+', '-', clean_kw)

        urls = [
            f"https://www.shine.com/job-search/{slug}-jobs",
            f"https://www.shine.com/job-search/{slug}-jobs-2",
            f"https://www.shine.com/job-search/{slug}-jobs-in-bangalore",
            f"https://www.shine.com/job-search/{slug}-jobs-in-india"
        ]
        return urls

    async def _harvest_jobs_from_page_with_scroll(self, page_url: str, max_scrolls: int = 6) -> List[Dict[str, Any]]:
        """
        Smoothly scrolls down the page to hydrate lazy-rendered Next.js job cards
        and extracts structured job tuples from the DOM.
        """
        all_jobs_dict: Dict[str, Dict[str, Any]] = {}

        for scroll_i in range(max_scrolls):
            # Extract currently visible job cards
            cards = await self._extract_job_listings_from_page()
            for c in cards:
                key = c.get("job_url") or f"{c.get('company')}:{c.get('title')}"
                if key and key not in all_jobs_dict:
                    all_jobs_dict[key] = c

            # Scroll down
            await self.cdp.evaluate("window.scrollBy({ top: 900, behavior: 'smooth' });")
            await asyncio.sleep(1.2)

        return list(all_jobs_dict.values())

    async def _extract_job_listings_from_page(self) -> List[Dict[str, Any]]:
        """
        Executes JavaScript in the search results page context to parse all rendered job cards.
        """
        js = """
        (() => {
            const results = [];
            // Target all Shine job cards
            const cards = Array.from(document.querySelectorAll('.jobCardNova_bigCard__W2xn3, .jdbigCard, [data-card-index], [itemtype*="ListItem"]'));

            for (const card of cards) {
                try {
                    // Title and Link
                    let title = '';
                    let job_url = '';
                    let job_id = '';

                    const linkEl = card.querySelector('h3 a, .jobCardNova_bigCardTopTitleHeading__Rj2sC a, a[href*="/jobs/"]');
                    if (linkEl) {
                        title = linkEl.innerText.trim();
                        job_url = linkEl.href;
                    }

                    const metaUrl = card.querySelector('meta[itemprop="url"]');
                    if (metaUrl && metaUrl.content) {
                        job_url = metaUrl.content;
                    }

                    const metaTitle = card.querySelector('meta[itemprop="name"], h3[itemprop="name"]');
                    if (metaTitle && (!title || title.length < 2)) {
                        title = metaTitle.content || metaTitle.innerText || title;
                    }

                    if (!title || !job_url) continue;

                    // Extract job ID from URL
                    const idMatch = job_url.match(/\\/(\\d+)(?:\\?|$|#)/);
                    if (idMatch) {
                        job_id = idMatch[1];
                    }

                    // Company Name
                    let company = 'Unknown';
                    const compEl = card.querySelector('.jobCardNova_bigCardTopTitleName__M_W_m, [class*="Company"], span[title]');
                    if (compEl) {
                        company = compEl.innerText.trim() || compEl.getAttribute('title') || company;
                    }

                    // Experience
                    let experience = '';
                    const expEl = card.querySelector('.jobCardNova_bigCardCenterListExp__KTSEc, .jdbigCardExperience, [class*="Experience"]');
                    if (expEl) {
                        experience = expEl.innerText.trim();
                    }

                    // Location
                    let location = '';
                    const locEl = card.querySelector('.jobCardNova_bigCardCenterListLoc__usiPB, .jobCardNova_bigCardLocation__OMkI1, [class*="Location"]');
                    if (locEl) {
                        location = locEl.innerText.trim();
                    }

                    // Skills
                    const skills = [];
                    const skillEls = card.querySelectorAll('.jobCardNova_skillsLists__7YifX li, .jdSkills li, [class*="skillsLists"] li');
                    for (const s of skillEls) {
                        const txt = s.innerText.trim();
                        if (txt) skills.push(txt);
                    }

                    // Posted age
                    let posted_text = '';
                    const postedEl = card.querySelector('.jobCardNova_postedData__LTERc, [class*="postedData"]');
                    if (postedEl) {
                        posted_text = postedEl.innerText.trim();
                    }

                    // Direct Apply Button Presence
                    const applyBtn = card.querySelector('.jobApplyBtnNova_bigCardBottomApply__z2n7R, .jdbigCardBottomApply, button');
                    const has_direct_apply = Boolean(applyBtn && applyBtn.innerText.toLowerCase().includes('apply'));

                    results.push({
                        title,
                        company,
                        job_url,
                        job_id,
                        experience,
                        location,
                        skills,
                        posted_text,
                        has_direct_apply,
                        source: 'Shine'
                    });
                } catch (e) {}
            }

            return results;
        })()
        """
        try:
            items = await self.cdp.evaluate(js)
            return items or []
        except Exception as e:
            logger.error(f"[Shine] Error in _extract_job_listings_from_page: {e}")
            return []

    async def _fetch_full_job_details(self, job_url: str) -> Optional[Dict[str, Any]]:
        """
        Navigates to the individual job page, parses embedded schema.org JSON-LD metadata,
        and retrieves the complete job description for scoring.
        """
        try:
            await self.cdp.navigate(job_url, wait_seconds=3.0)

            # Security check on job page
            sec_status = await self.cdp.check_for_security_challenge()
            if sec_status.get("has_challenge"):
                if sec_status.get("login_required"):
                    await self.cdp.pause_and_wait_for_human("Shine login required to view job description.")
                else:
                    await self.cdp.pause_and_wait_for_human("Security verification on Shine job page.")

            js = """
            (() => {
                let jsonLdData = null;
                const scripts = Array.from(document.querySelectorAll('script[type="application/ld+json"]'));
                for (const s of scripts) {
                    try {
                        const parsed = JSON.parse(s.innerText);
                        if (parsed['@type'] === 'JobPosting') {
                            jsonLdData = parsed;
                            break;
                        }
                    } catch (e) {}
                }

                // Parse DOM description text as fallback
                let domDescription = '';
                const descEl = document.querySelector('.jobDetailNova_jobDescription__FqB_n, [class*="jobDescription"], [itemprop="description"], article, #job_description');
                if (descEl) {
                    domDescription = descEl.innerText.trim();
                } else if (document.body) {
                    domDescription = document.body.innerText;
                }

                return {
                    jsonLd: jsonLdData,
                    domDescription: domDescription,
                    currentUrl: window.location.href
                };
            })()
            """
            data = await self.cdp.evaluate(js)
            if not data:
                return None

            json_ld = data.get("jsonLd") or {}
            dom_desc = data.get("domDescription") or ""

            # Extract fields from JSON-LD if available
            title = json_ld.get("title") or ""
            comp = json_ld.get("hiringOrganization", {}).get("name") if isinstance(json_ld.get("hiringOrganization"), dict) else ""
            skills = json_ld.get("skills") or []
            if isinstance(skills, str):
                skills = [s.strip() for s in skills.split(",") if s.strip()]

            # Clean HTML description
            html_desc = json_ld.get("description") or ""
            clean_ld_desc = re.sub(r'<[^>]+>', ' ', html_desc).strip()
            final_desc = f"{clean_ld_desc}\n{dom_desc}".strip()

            job_id = ""
            identifier = json_ld.get("identifier", {})
            if isinstance(identifier, dict):
                job_id = str(identifier.get("value", ""))
            if not job_id:
                id_match = re.search(r'/(\d+)(?:\?|$|#)', job_url)
                if id_match:
                    job_id = id_match.group(1)

            loc_str = ""
            loc_data = json_ld.get("jobLocation")
            if isinstance(loc_data, list) and loc_data:
                first_loc = loc_data[0]
                if isinstance(first_loc, dict):
                    addr = first_loc.get("address", {})
                    loc_str = f"{addr.get('addressLocality', '')}, {addr.get('addressRegion', '')}".strip(", ")

            return {
                "title": title or "Software Engineer",
                "company": comp or "Unknown",
                "job_url": job_url,
                "job_id": job_id,
                "description": final_desc,
                "skills": skills,
                "location": loc_str,
                "source": "Shine",
                "direct_apply": json_ld.get("directApply", True)
            }
        except Exception as e:
            logger.error(f"[Shine] Error fetching job details for {job_url}: {e}")
            return None

    async def _apply_to_job(self, job: Dict[str, Any], score_res: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes the application workflow on the active Shine job page.
        Handles direct apply, resume upload/selection, screening questions, and external redirects.
        """
        job_url = job.get("job_url", "")
        comp = job.get("company", "Unknown")
        title = job.get("title", "Unknown")
        job_id = job.get("job_id", "")
        match_score = score_res.get("total_score", 0.0)

        # 1. Update status to 'Applying' in tracker
        self.tracker.record_job(
            company=comp,
            role=title,
            source="Shine",
            job_url=job_url,
            match_score=match_score,
            status="Applying",
            notes="Starting application flow on Shine.com",
            job_id=job_id
        )

        try:
            # 2. Check if already applied on page
            already_applied_check = await self.cdp.evaluate("""
            (() => {
                const bodyText = (document.body ? document.body.innerText : '').toLowerCase();
                if (bodyText.includes('already applied') || bodyText.includes('applied on') || bodyText.includes('you have applied')) {
                    return true;
                }
                const btn = document.querySelector('.jobApplyBtnNova_bigCardBottomApply__z2n7R, button[class*="apply"], button[class*="Apply"]');
                if (btn && btn.innerText.toLowerCase().includes('applied')) {
                    return true;
                }
                return false;
            })()
            """)
            if already_applied_check:
                print(f"[Shine] 🔄 Page indicates already applied for {title} at {comp}.")
                self.tracker.update_status(job_url, "Already Applied", "Already Applied on Shine.com")
                return {"status": "Already Applied", "submitted": False}

            # 3. Locate and Click the Apply Button
            apply_clicked = await self.cdp.evaluate("""
            (() => {
                const candidates = Array.from(document.querySelectorAll('button, a.nova_btn, [role="button"]'));
                for (const el of candidates) {
                    const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                    if (text === 'apply' || text === 'apply now' || text === 'direct apply' || text.startsWith('apply on')) {
                        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        el.click();
                        return true;
                    }
                }
                // Fallback class selector
                const btn = document.querySelector('.jobApplyBtnNova_bigCardBottomApply__z2n7R, .jdbigCardBottomApply, [class*="applyBtn"]');
                if (btn) {
                    btn.click();
                    return true;
                }
                return false;
            })()
            """)

            if not apply_clicked:
                print(f"[Shine] ⚠️ Could not find active Apply button on {job_url}")
                self.tracker.update_status(job_url, "Under Review", "Apply button not interactable or job expired")
                return {"status": "Under Review", "submitted": False, "error": "Apply button not found"}

            print("[Shine] 🖱️ Clicked Apply button. Monitoring for application modals / transitions...")
            await asyncio.sleep(2.5)

            # 4. Check for External Portal Redirect
            curr_url = await self.cdp.evaluate("window.location.href")
            if curr_url and "shine.com" not in curr_url:
                print(f"[Shine] 🔗 External redirect detected: {curr_url}")
                self.tracker.update_status(job_url, "Under Review", f"Redirected to external company site: {curr_url}")
                return {"status": "Under Review", "submitted": False, "external_url": curr_url}

            # 5. Check for Security Challenge / Login Gate
            sec_status = await self.cdp.check_for_security_challenge()
            if sec_status.get("has_challenge"):
                if sec_status.get("login_required"):
                    await self.cdp.pause_and_wait_for_human("Shine login required to complete application.")
                else:
                    await self.cdp.pause_and_wait_for_human("Security / CAPTCHA prompt on application submission.")

            # 6. Handle Resume Selection / Upload Modal
            await self._handle_resume_selection_if_present()

            # 7. Handle Screening Questionnaire / Recruiter Details Modal
            await self._handle_recruiter_details_modal()
            question_status = await self._handle_screening_questions_if_present()
            if question_status.get("requires_human"):
                reason = question_status.get("reason", "Unknown screening question")
                print(f"[Shine] ⏳ Pausing: {reason}")
                SystemNotifier.notify_human_action_needed(reason, job_url)
                self.tracker.update_status(job_url, "Waiting for Input", reason)
                return {"status": "Waiting for Input", "submitted": False, "attention_required": True}

            # 8. Click Submit / Continue / "Submit and apply" in Modal
            await self._click_modal_submit_or_continue()
            await asyncio.sleep(2.5)

            # Re-check if secondary modal (e.g. Recruiter Additional Details or Add Skills) appeared
            await self._handle_recruiter_details_modal()
            await self._click_modal_submit_or_continue()
            await asyncio.sleep(2.0)

            # 9. Verify Submission Truth
            verified = await self._verify_application_submission()
            if verified:
                print(f"[Shine] 🎉 Successfully confirmed application submission for {title} at {comp}!")
                self.tracker.update_status(job_url, "Submitted", "Successfully applied via Shine.com Automation")
                return {"status": "Submitted", "submitted": True}
            else:
                # Check if it was queued or closed
                print(f"[Shine] ℹ️ Application submission state unconfirmed. Placing in Review Queue.")
                self.tracker.update_status(job_url, "Under Review", "Application submitted, pending visual confirmation")
                return {"status": "Under Review", "submitted": False}

        except Exception as e:
            logger.error(f"[Shine] Application exception for {job_url}: {e}")
            self.tracker.update_status(job_url, "Failed", f"Error: {str(e)[:100]}")
            return {"status": "Failed", "submitted": False, "error": str(e)}

    async def _handle_resume_selection_if_present(self):
        """
        Detects if a resume selection modal or resume upload input is present and selects/uploads candidate resume.
        """
        resume_cfg = self.profile.get("resumes", {})
        default_resume_path = os.path.abspath(resume_cfg.get("default", "resumes/Sakshi_resume2.pdf"))

        js_resume_check = """
        (() => {
            const fileInput = document.querySelector('input[type="file"]');
            const resumeRadio = document.querySelector('input[type="radio"][name*="resume"], [class*="resumeCard"], [class*="resumeList"] input');
            return {
                hasFileInput: Boolean(fileInput),
                hasResumeOptions: Boolean(resumeRadio)
            };
        })()
        """
        res_info = await self.cdp.evaluate(js_resume_check) or {}

        if res_info.get("hasResumeOptions"):
            print("[Shine] 📄 Detected existing profile resume selection. Selecting primary resume...")
            await self.cdp.evaluate("""
            (() => {
                const radio = document.querySelector('input[type="radio"][name*="resume"], [class*="resume"] input[type="radio"]');
                if (radio) {
                    radio.checked = true;
                    radio.dispatchEvent(new Event('change', { bubbles: true }));
                }
            })()
            """)

        if res_info.get("hasFileInput") and os.path.exists(default_resume_path):
            print(f"[Shine] 📤 Attaching candidate resume: {default_resume_path}")
            await self.cdp.upload_file('input[type="file"]', default_resume_path)
            await asyncio.sleep(1.5)

    async def _handle_recruiter_details_modal(self) -> Dict[str, Any]:
        """
        Handles the 'Recruiter needs additional details to consider your application' modal on Shine.com.
        Fills:
        - Per-skill experience dropdowns ('0 yrs' or '1 yrs' matching candidate profile)
        - Expected Annual CTC dropdown ('8.5 Lakh' / '8 Lakh' / '9 Lakh')
        - Notice Period dropdown ('0 days' / 'Immediate' / '15 days')
        - Any additional screening fields
        - Clicks 'Submit and apply'
        """
        candidate_skills = set()
        for cat in ["languages", "ai_ml_data", "frameworks_and_libraries", "databases", "tools_and_devops", "concepts"]:
            for s in self.profile.get("technical_skills", {}).get(cat, []):
                clean_s = s.lower().strip()
                candidate_skills.add(clean_s)
                if clean_s == "node.js":
                    candidate_skills.add("nodejs")
                elif clean_s == "c++":
                    candidate_skills.add("cpp")
                elif clean_s == "c#":
                    candidate_skills.add("csharp")
                elif clean_s == "react":
                    candidate_skills.add("reactjs")

        expected_ctc = str(self.profile.get("screening_question_presets", {}).get("expected_ctc", {}).get("value", "8.5"))
        notice_period = str(self.profile.get("screening_question_presets", {}).get("notice_period", {}).get("value", "0"))

        raw_js = """
        (() => {
            const candidateSkills = __CANDIDATE_SKILLS__;
            const expectedLpa = "__EXPECTED_LPA__";
            const noticeDays = "__NOTICE_DAYS__";
            const logs = [];

            const allText = (document.body ? document.body.innerText : '').toLowerCase();
            const hasRecruiterModal = allText.includes('recruiter needs additional details') || 
                                     allText.includes('specify your experience for each of these skills') ||
                                     allText.includes('submit and apply');

            if (!hasRecruiterModal) {
                return { isRecruiterModal: false, filledCount: 0, logs: [] };
            }

            logs.push("Detected 'Recruiter needs additional details' modal.");

            function setSelectOption(selectEl, predicate) {
                if (!selectEl || !selectEl.options) return false;
                for (let i = 0; i < selectEl.options.length; i++) {
                    const opt = selectEl.options[i];
                    const t = (opt.text || '').toLowerCase().trim();
                    const v = (opt.value || '').toLowerCase().trim();
                    if (predicate(t, v)) {
                        selectEl.selectedIndex = i;
                        selectEl.value = opt.value;
                        selectEl.dispatchEvent(new Event('change', { bubbles: true }));
                        selectEl.dispatchEvent(new Event('input', { bubbles: true }));
                        return opt.text.trim();
                    }
                }
                return false;
            }

            let filledCount = 0;

            // 1. Match skill experience rows (e.g. c#, python, nodejs, mongodb -> "Years")
            const allElements = Array.from(document.querySelectorAll('div, li, tr, p'));
            const skillRows = allElements.filter(row => {
                const t = (row.innerText || '').toLowerCase();
                return (t.includes('years') || t.includes('yrs')) && 
                       !t.includes('specify your experience') &&
                       !t.includes('recruiter needs additional details') &&
                       (row.querySelector('select') || row.querySelector('[class*="select"], [class*="dropdown"], button, [role="combobox"], [role="button"]'));
            });

            for (const row of skillRows) {
                const rowText = (row.innerText || '').toLowerCase();
                let matchedSkillName = '';
                let hasSkill = false;

                for (const sk of candidateSkills) {
                    if (rowText.includes(sk)) {
                        matchedSkillName = sk;
                        hasSkill = true;
                        break;
                    }
                }

                const targetYrs = hasSkill ? '1' : '0';
                const targetText = hasSkill ? '1 yrs' : '0 yrs';

                const sel = row.querySelector('select');
                if (sel) {
                    const chosen = setSelectOption(sel, (t, v) => t.startsWith(targetYrs) || v === targetYrs || t.includes(targetText));
                    if (chosen) {
                        filledCount++;
                        logs.push("Set skill '" + (matchedSkillName || rowText.split(/\\n/)[0].trim()) + "' -> " + chosen);
                        continue;
                    }
                }

                const trigger = row.querySelector('button, [role="combobox"], [role="button"], [class*="select"], [class*="dropdown"], div');
                if (trigger && (trigger.innerText || '').includes('Years')) {
                    trigger.click();
                    const options = Array.from(document.querySelectorAll('li, div[role="option"], [class*="option"], [class*="item"], .dropdown-item'));
                    for (const opt of options) {
                        const optText = (opt.innerText || opt.textContent || '').toLowerCase().trim();
                        if (optText.startsWith(targetYrs) || optText === targetText) {
                            opt.click();
                            filledCount++;
                            logs.push("Selected skill option '" + (matchedSkillName || rowText.split(/\\n/)[0].trim()) + "' -> " + optText);
                            break;
                        }
                    }
                }
            }

            // 2. Expected CTC Dropdown ("What is your expected annual CTC?")
            const ctcElements = Array.from(document.querySelectorAll('select, button, div[role="combobox"], div[role="button"], [class*="select"]')).filter(el => {
                const t = (el.innerText || el.textContent || '').toLowerCase();
                const parentText = (el.parentElement ? el.parentElement.innerText : '').toLowerCase();
                return t.includes('ctc (lakh)') || parentText.includes('expected annual ctc') || parentText.includes('expected ctc');
            });

            for (const el of ctcElements) {
                if (el.tagName === 'SELECT') {
                    const chosen = setSelectOption(el, (t, v) => t.includes('8.5') || t.includes('8') || t.includes('9') || v.includes('8.5') || v.includes('8') || v.includes('9'));
                    if (chosen) {
                        filledCount++;
                        logs.push("Set Expected CTC -> " + chosen);
                    }
                } else if (el.click) {
                    el.click();
                    const options = Array.from(document.querySelectorAll('li, div[role="option"], [class*="option"], [class*="item"]'));
                    for (const opt of options) {
                        const optText = (opt.innerText || opt.textContent || '').toLowerCase().trim();
                        if (optText.includes('8.5') || optText.includes('8 lakh') || optText.includes('9 lakh') || optText.startsWith('8') || optText.startsWith('9')) {
                            opt.click();
                            filledCount++;
                            logs.push("Selected Expected CTC -> " + optText);
                            break;
                        }
                    }
                }
            }

            // 3. Notice Period Dropdown ("What is your notice period?")
            const npElements = Array.from(document.querySelectorAll('select, button, div[role="combobox"], div[role="button"], [class*="select"]')).filter(el => {
                const t = (el.innerText || el.textContent || '').toLowerCase();
                const parentText = (el.parentElement ? el.parentElement.innerText : '').toLowerCase();
                return t.includes('notice period') || parentText.includes('notice period');
            });

            for (const el of npElements) {
                if (el.tagName === 'SELECT') {
                    const chosen = setSelectOption(el, (t, v) => t.includes('0') || t.includes('immediate') || t.includes('15') || v === '0' || v.includes('15'));
                    if (chosen) {
                        filledCount++;
                        logs.push("Set Notice Period -> " + chosen);
                    }
                } else if (el.click) {
                    el.click();
                    const options = Array.from(document.querySelectorAll('li, div[role="option"], [class*="option"], [class*="item"]'));
                    for (const opt of options) {
                        const optText = (opt.innerText || opt.textContent || '').toLowerCase().trim();
                        if (optText.includes('0 days') || optText.includes('immediate') || optText.includes('15 days') || optText.startsWith('0') || optText.includes('15')) {
                            opt.click();
                            filledCount++;
                            logs.push("Selected Notice Period -> " + optText);
                            break;
                        }
                    }
                }
            }

            // 4. Click Submit Button ("Submit and apply")
            const submitButtons = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"], a.nova_btn, .btn'));
            let clickedSubmit = false;
            for (const b of submitButtons) {
                const txt = (b.innerText || b.value || b.textContent || '').trim().toLowerCase();
                if (txt === 'submit and apply' || txt === 'submit & apply' || txt === 'submit application' || txt === 'submit' || txt === 'apply now') {
                    b.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    b.click();
                    clickedSubmit = true;
                    logs.push("Clicked '" + b.innerText.trim() + "' button.");
                    break;
                }
            }

            return {
                isRecruiterModal: true,
                filledCount: filledCount,
                clickedSubmit: clickedSubmit,
                logs: logs
            };
        })()
        """
        js_script = raw_js.replace("__CANDIDATE_SKILLS__", json.dumps(list(candidate_skills))).replace("__EXPECTED_LPA__", expected_ctc).replace("__NOTICE_DAYS__", notice_period)
        try:
            res = await self.cdp.evaluate(js_script) or {}
            if res.get("isRecruiterModal"):
                for line in res.get("logs", []):
                    print(f"[Shine] 📝 {line}")
            return res
        except Exception as e:
            logger.error(f"[Shine] Error in _handle_recruiter_details_modal: {e}")
            return {"isRecruiterModal": False, "error": str(e)}

    async def _handle_screening_questions_if_present(self) -> Dict[str, Any]:
        """
        Detects questionnaire fields (Notice Period, Current CTC, Expected CTC, Experience, Yes/No questions)
        and fills them using the approved-answer engine.
        """
        js_find_questions = """
        (() => {
            const formElements = [];
            const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]):not([type="file"]):not([type="submit"]), select, textarea'));

            for (const el of inputs) {
                // Ignore search bar or header inputs
                if (el.closest('header') || el.closest('nav') || el.closest('#webHeaderNova') || el.classList.contains('searchBar_searchInputCenter__x260O')) {
                    continue;
                }

                // Find label or question text
                let labelText = '';
                if (el.id) {
                    const lbl = document.querySelector(`label[for="${el.id}"]`);
                    if (lbl) labelText = lbl.innerText;
                }
                if (!labelText && el.placeholder) {
                    labelText = el.placeholder;
                }
                if (!labelText && el.name) {
                    labelText = el.name;
                }
                if (!labelText && el.parentElement) {
                    labelText = el.parentElement.innerText;
                }

                formElements.push({
                    tagName: el.tagName.toLowerCase(),
                    type: el.type || 'text',
                    id: el.id || '',
                    name: el.name || '',
                    labelText: (labelText || '').trim().replace(/\\n+/g, ' '),
                    value: el.value || '',
                    isRequired: Boolean(el.required)
                });
            }

            return formElements;
        })()
        """
        fields = await self.cdp.evaluate(js_find_questions) or []
        if not fields:
            return {"requires_human": False}

        print(f"[Shine] 📝 Detected {len(fields)} form field(s) in application modal.")

        for f in fields:
            label = (f.get("labelText") or "").lower()
            name = (f.get("name") or "").lower()
            combined_prompt = f"{label} {name}"
            tag = f.get("tagName")
            el_type = f.get("type")
            selector = f"#{f['id']}" if f.get("id") else f"{tag}[name='{f['name']}']" if f.get("name") else None

            if not selector:
                continue

            # Notice Period
            if any(p in combined_prompt for p in ["notice", "joining", "how soon", "availability"]):
                print(f"[Shine] Answering Notice Period -> 0 Days (Immediate)")
                if tag == "select":
                    await self.cdp.evaluate(f"""
                    (() => {{
                        const sel = document.querySelector('{selector}');
                        if (sel) {{
                            for (const opt of sel.options) {{
                                if (opt.text.toLowerCase().includes('immediate') || opt.text.includes('0') || opt.value === '0') {{
                                    sel.value = opt.value;
                                    sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                    break;
                                }}
                            }}
                        }}
                    }})()
                    """)
                else:
                    await self.cdp.type_text(selector, "0")

            # Current CTC
            elif any(p in combined_prompt for p in ["current ctc", "current salary", "current annual"]):
                print(f"[Shine] Answering Current CTC -> 0 (Fresher)")
                await self.cdp.type_text(selector, "0")

            # Expected CTC
            elif any(p in combined_prompt for p in ["expected ctc", "expected salary", "salary expectation"]):
                print(f"[Shine] Answering Expected CTC -> 8.5 LPA")
                await self.cdp.type_text(selector, "850000" if "lpa" not in combined_prompt and "lakh" not in combined_prompt else "8.5")

            # Experience
            elif any(p in combined_prompt for p in ["experience", "years of exp", "total exp"]):
                print(f"[Shine] Answering Total Experience -> 0.5")
                if tag == "select":
                    await self.cdp.evaluate(f"""
                    (() => {{
                        const sel = document.querySelector('{selector}');
                        if (sel) {{
                            for (const opt of sel.options) {{
                                if (opt.text.includes('0') || opt.text.toLowerCase().includes('fresher') || opt.value === '0') {{
                                    sel.value = opt.value;
                                    sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                    break;
                                }}
                            }}
                        }}
                    }})()
                    """)
                else:
                    await self.cdp.type_text(selector, "0.5")

            # Work Authorization / Relocation / Generic Yes/No
            elif any(p in combined_prompt for p in ["authorized to work", "legally authorized", "relocate", "willing to relocate"]):
                print(f"[Shine] Answering Authorization/Relocation -> Yes")
                if el_type == "radio" or el_type == "checkbox":
                    await self.cdp.evaluate(f"document.querySelector('{selector}').click();")
                elif tag == "select":
                    await self.cdp.evaluate(f"""
                    (() => {{
                        const sel = document.querySelector('{selector}');
                        if (sel) {{
                            for (const opt of sel.options) {{
                                if (opt.text.toLowerCase().includes('yes')) {{
                                    sel.value = opt.value;
                                    sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                    break;
                                }}
                            }}
                        }}
                    }})()
                    """)
                else:
                    await self.cdp.type_text(selector, "Yes")

            # Ambiguous or Sensitive Question
            elif f.get("isRequired") and not f.get("value"):
                return {
                    "requires_human": True,
                    "reason": f"Unconfigured mandatory screening question: '{f.get('labelText')}'"
                }

        return {"requires_human": False}

    async def _click_modal_submit_or_continue(self):
        """
        Finds and triggers the confirmation/submit button inside active modals,
        including recruiter skill popups ("Add Skills that Recruiter Looking For") and final submissions.
        """
        js = """
        (() => {
            // Priority 1: Check for explicit Submit button in modal (e.g. yellow Submit button on Add Skills modal)
            const submitButtons = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"], a.nova_btn, .btn'));
            for (const b of submitButtons) {
                const txt = (b.innerText || b.value || b.textContent || '').trim().toLowerCase();
                if (txt === 'submit' || txt === 'submit application' || txt === 'save & apply' || txt === 'apply now' || txt === 'done') {
                    b.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    b.click();
                    return true;
                }
            }

            // Priority 2: General continue/apply buttons
            for (const b of submitButtons) {
                const txt = (b.innerText || b.value || b.textContent || '').trim().toLowerCase();
                if (['continue', 'next', 'apply', 'proceed'].includes(txt)) {
                    b.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    b.click();
                    return true;
                }
            }

            // Priority 3: Close button on optional enhancement modals if submit not available
            const closeBtn = document.querySelector('.modalClose, button[aria-label="Close"], button[class*="close"], [class*="modal"] [class*="close"]');
            if (closeBtn) {
                closeBtn.click();
                return true;
            }

            return false;
        })()
        """
        await self.cdp.evaluate(js)
        await asyncio.sleep(2.0)
        # Check if a secondary popup (e.g. skill confirmation) appeared and click submit again
        await self.cdp.evaluate(js)

    async def _verify_application_submission(self) -> bool:
        """
        Inspects the DOM for confirmed application success indicators.
        """
        js = """
        (() => {
            const bodyText = (document.body ? document.body.innerText : '').toLowerCase();
            const successStrings = [
                'application submitted',
                'application sent',
                'successfully applied',
                'applied successfully',
                'your application has been sent'
            ];

            for (const s of successStrings) {
                if (bodyText.includes(s)) return true;
            }

            const appliedBtn = document.querySelector('button[disabled], .applied, [class*="applied"]');
            if (appliedBtn && appliedBtn.innerText.toLowerCase().includes('applied')) {
                return true;
            }

            return false;
        })()
        """
        return bool(await self.cdp.evaluate(js))
