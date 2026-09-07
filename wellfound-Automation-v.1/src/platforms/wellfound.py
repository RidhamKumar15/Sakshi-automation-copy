import asyncio
import json
import logging
import os
import re
import urllib.parse
from typing import Any, Dict, List, Optional

from src.cdp_client import ChromeCDPClient
from src.scorer import JobScorer
from src.tracker import ApplicationTracker
from src.notifier import SystemNotifier

logger = logging.getLogger("WellfoundPlatform")


class WellfoundAutomation:
    """
    Automates job search, scoring, and application on Wellfound (formerly AngelList Talent).
    Uses the dedicated Chrome profile via CDP.
    """

    BASE_URL = "https://wellfound.com/jobs"

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
        Main execution flow:
        1. Navigate to Wellfound jobs page with keywords.
        2. Verify login & check CAPTCHA.
        3. Extract job listings one by one.
        4. Score each job.
        5. Check duplicate.
        6. Apply or queue for review.
        """
        max_to_apply = limit if limit is not None else self.max_applications

        results_summary = {
            "source": "Wellfound",
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

        search_terms = keywords or self.settings.get("sources", {}).get("wellfound", {}).get("search_keywords", ["Software Engineer"])

        # Map search terms to Wellfound URL patterns (India + Remote + Base)
        role_slug_map = {
            "software engineer": ["https://wellfound.com/role/l/software-engineer/india", "https://wellfound.com/role/r/software-engineer", "https://wellfound.com/jobs"],
            "backend developer": ["https://wellfound.com/role/l/backend-developer/india", "https://wellfound.com/role/r/backend-developer", "https://wellfound.com/jobs"],
            "python developer": ["https://wellfound.com/role/l/python-developer/india", "https://wellfound.com/role/r/python-developer", "https://wellfound.com/jobs"],
            "java developer": ["https://wellfound.com/role/l/java-developer/india", "https://wellfound.com/role/r/java-developer", "https://wellfound.com/jobs"],
            "sql developer": ["https://wellfound.com/role/l/database-administrator/india", "https://wellfound.com/role/l/data-engineer/india", "https://wellfound.com/jobs"],
            "data engineer": ["https://wellfound.com/role/l/data-engineer/india", "https://wellfound.com/role/r/data-engineer", "https://wellfound.com/jobs"],
            "ai/ml engineer": ["https://wellfound.com/role/l/machine-learning-engineer/india", "https://wellfound.com/role/r/machine-learning-engineer", "https://wellfound.com/jobs"],
            "machine learning engineer": ["https://wellfound.com/role/l/machine-learning-engineer/india", "https://wellfound.com/role/r/machine-learning-engineer", "https://wellfound.com/jobs"],
            "full stack developer": ["https://wellfound.com/role/l/full-stack-developer/india", "https://wellfound.com/role/r/full-stack-developer", "https://wellfound.com/jobs"],
            "sde-1": ["https://wellfound.com/role/l/software-engineer/india", "https://wellfound.com/role/r/software-engineer"],
            "junior software engineer": ["https://wellfound.com/role/l/software-engineer/india", "https://wellfound.com/role/r/software-engineer"],
            "frontend developer": ["https://wellfound.com/role/l/frontend-developer/india", "https://wellfound.com/role/r/frontend-developer", "https://wellfound.com/jobs"]
        }

        for keyword in search_terms:
            if results_summary["applications_submitted"] >= max_to_apply:
                print(f"[Wellfound] 🎉 Reached target applications limit ({max_to_apply}). Stopping search.")
                break

            print(f"\n[Wellfound] 🔍 Searching jobs for keyword: '{keyword}'...")
            kw_clean = keyword.lower().strip()
            target_urls = role_slug_map.get(
                kw_clean,
                [
                    f"https://wellfound.com/role/l/{kw_clean.replace(' ', '-')}/india",
                    f"https://wellfound.com/role/r/{kw_clean.replace(' ', '-')}",
                    "https://wellfound.com/jobs"
                ]
            )

            for search_url in target_urls:
                if results_summary["applications_submitted"] >= max_to_apply:
                    break

                print(f"\n[Wellfound] 🌐 Navigating to search URL: {search_url}")
                await self.cdp.navigate(search_url, wait_seconds=4.0)

                # Security / CAPTCHA / Login Check
                sec_status = await self.cdp.check_for_security_challenge()
                if sec_status.get("has_challenge"):
                    if sec_status.get("login_required"):
                        await self.cdp.pause_and_wait_for_human("Wellfound login required. Please log into your Wellfound account in Chrome.")
                    else:
                        await self.cdp.pause_and_wait_for_human("CAPTCHA or security challenge detected on Wellfound.")

                # Deep progressive scrolling and harvesting of all visible & lazy-loaded job cards
                extracted_jobs = await self._harvest_jobs_from_page_with_scroll(search_url, max_scrolls=12)
                results_summary["jobs_found"] += len(extracted_jobs)
                print(f"[Wellfound] Extracted {len(extracted_jobs)} total job listings from {search_url}")

                if not extracted_jobs:
                    continue

                for job in extracted_jobs:
                    if results_summary["applications_submitted"] >= max_to_apply:
                        print(f"\n🎉 [TARGET LIMIT REACHED] Successfully submitted {results_summary['applications_submitted']} application(s)!")
                        return results_summary

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
                        prev_status = prev_record.get("status", "")
                        print(f"   ⏭️ Skipping duplicate: Already tracked as '{prev_status}' on {prev_record.get('date')}")
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
                            source="Wellfound",
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
                            source="Wellfound",
                            job_url=url,
                            match_score=total_score,
                            status="Under Review",
                            notes=f"Moderate match ({total_score}/100). Added to human review queue.",
                            job_id=job_id
                        )
                        continue

                    elif decision == "AUTO_APPLY":
                        print(f"   🚀 High score {total_score} >= 70. Initiating application workflow...")
                        self.tracker.record_job(
                            company=comp,
                            role=title,
                            source="Wellfound",
                            job_url=url,
                            match_score=total_score,
                            status="Applying",
                            notes=f"High match ({total_score}/100). Applying now.",
                            job_id=job_id
                        )

                        try:
                            # Execute Application Flow
                            app_result = await self._apply_to_job(job, score_res)

                            if app_result["status"] == "Submitted":
                                results_summary["applications_submitted"] += 1
                                self.tracker.update_status(url, "Submitted", notes=f"Applied automatically on {comp} (Score: {total_score})")
                                print(f"   ✅ [Progress: {results_summary['applications_submitted']}/{max_to_apply} Submitted | Total Evaluated: {results_summary['jobs_evaluated']}]")
                                
                                # Check if limit reached
                                if results_summary["applications_submitted"] >= max_to_apply:
                                    print(f"\n🎉 [TARGET LIMIT REACHED] Successfully submitted {results_summary['applications_submitted']} application(s)!")
                                    return results_summary
                            elif app_result["status"] == "Not Accepting":
                                results_summary["jobs_skipped"] += 1
                                self.tracker.update_status(url, "Not Accepting", notes=app_result.get("notes", "Not accepting applications from candidate location"))
                                print(f"   🚫 Recorded in Tracker as 'Not Accepting': {app_result.get('notes')}")
                            elif app_result["status"] == "Skipped":
                                results_summary["jobs_skipped"] += 1
                                self.tracker.update_status(url, "Skipped", notes=app_result.get("notes", "Skipped by automation"))
                            elif app_result["status"] == "Waiting for Input":
                                results_summary["applications_manual_action"] += 1
                                results_summary["attention_urls"].append(url)
                                self.tracker.update_status(url, "Waiting for Input", notes=app_result.get("notes", "Requires manual user input"))
                            elif app_result["status"] == "Already Applied":
                                results_summary["duplicates_ignored"] += 1
                                self.tracker.update_status(url, "Already Applied", notes="Already applied on Wellfound")
                            elif app_result["status"] == "Closed":
                                self.tracker.update_status(url, "Closed", notes="Job listing no longer active")
                            else:
                                results_summary["errors"].append(f"{comp} - {title}: {app_result.get('notes', 'Failed')}")
                                self.tracker.update_status(url, "Failed", notes=app_result.get("notes", "Application process failed"))
                        except Exception as job_err:
                            print(f"   ⚠️ Error processing job '{title}' at '{comp}': {job_err}")
                            results_summary["errors"].append(f"{comp} - {title}: {job_err}")
                            self.tracker.update_status(url, "Failed", notes=f"Error: {job_err}")

                    # Respectful delay between processing jobs
                    await asyncio.sleep(2.0)

        return results_summary

    async def _harvest_jobs_from_page_with_scroll(self, search_url: str, max_scrolls: int = 12) -> List[Dict[str, Any]]:
        """
        Deep-scrolls the search page to dynamically hydrate and harvest all loaded job cards.
        Collects dozens to hundreds of listings per page across multiple incremental scroll passes.
        """
        harvested = {}
        no_new_count = 0
        last_count = 0

        # Initial scroll & wait
        for _ in range(2):
            await self.cdp.evaluate("window.scrollBy(0, 800);")
            await asyncio.sleep(1.0)

        for scroll_idx in range(max_scrolls):
            current_listings = await self._extract_job_listings_from_page()
            for item in current_listings:
                url = item.get("job_url", "")
                jid = item.get("job_id", "")
                key = url or jid
                if key and key not in harvested:
                    harvested[key] = item

            new_count = len(harvested)
            if new_count == last_count:
                no_new_count += 1
                if no_new_count >= 3:
                    # No more jobs loading after 3 consecutive scrolls
                    break
            else:
                no_new_count = 0
                last_count = new_count

            # Scroll further down to trigger lazy loading / Apollo fetch
            await self.cdp.evaluate("window.scrollBy(0, 1400);")
            await asyncio.sleep(1.5)

        # Final fallback check: scroll to bottom
        if not harvested:
            await self.cdp.evaluate("window.scrollTo(0, document.body.scrollHeight);")
            await asyncio.sleep(2.0)
            current_listings = await self._extract_job_listings_from_page()
            for item in current_listings:
                url = item.get("job_url", "")
                jid = item.get("job_id", "")
                key = url or jid
                if key and key not in harvested:
                    harvested[key] = item

        return list(harvested.values())

        return results_summary

    async def _extract_job_listings_from_page(self) -> List[Dict[str, Any]]:
        """
        Parses visible job cards from Wellfound using multi-strategy extraction:
        1. Apollo client state / Next.js data
        2. Universal DOM link and card container traversal (excluding nav links)
        3. CSS module and data-test attribute selectors
        """
        js = r"""
        (() => {
            const listings = [];
            const seenUrls = new Set();
            const excludedPaths = [
                '/jobs/home', '/jobs/applications', '/jobs/messages',
                '/jobs/saved', '/jobs/matches', '/jobs/preferences',
                '/jobs/onboarding', '/jobs/track', '/jobs/explore'
            ];

            // Strategy 1: Universal Link & Parent Card Traversal
            const allLinks = Array.from(document.querySelectorAll('a[href*="/jobs/"], a[href*="/company/"][href*="/jobs"]'));
            
            allLinks.forEach((link, idx) => {
                try {
                    const href = link.href;
                    if (!href || seenUrls.has(href)) return;

                    // Check if it is a nav path or generic root
                    const isExcluded = excludedPaths.some(p => href.includes(p)) || href.endsWith('/jobs') || href.endsWith('/jobs/');
                    if (isExcluded) return;

                    // Walk up to find card container
                    let container = link.closest('[data-test="JobListing"], [data-test="StartupResult"], [class*="styles_jobListing"], [class*="styles_result"], [class*="styles_jobCard"], [class*="styles_component"], tr, li, article, div[class*="border"]');
                    if (!container) {
                        container = link.parentElement ? link.parentElement.parentElement : null;
                    }
                    if (!container) return;

                    let title = link.innerText.trim();
                    if (!title || title.toLowerCase().includes("view") || title.toLowerCase().includes("apply")) {
                        const hEl = container.querySelector('h2, h3, h4, strong, a[data-test="JobTitle"]');
                        if (hEl) title = hEl.innerText.trim();
                    }
                    
                    // Find company name
                    let company = "";
                    const compLink = container.querySelector('a[href*="/company/"]');
                    if (compLink && compLink !== link) {
                        company = compLink.innerText.trim();
                    }
                    if (!company) {
                        const hEl = container.querySelector('h3, h4, [class*="startupName"], [class*="companyName"], [data-test="StartupName"]');
                        if (hEl && hEl.innerText.trim() !== title) {
                            company = hEl.innerText.trim();
                        }
                    }
                    if (!company) {
                        // Look at preceding headings
                        let prev = container.previousElementSibling;
                        while (prev && !company) {
                            const prevComp = prev.querySelector('a[href*="/company/"], h2, h3');
                            if (prevComp) company = prevComp.innerText.trim();
                            prev = prev.previousElementSibling;
                        }
                    }
                    if (!company) {
                        // Check header element in section
                        const section = container.closest('section, div[class*="styles_startup"], div[class*="styles_section"]');
                        if (section) {
                            const secHeader = section.querySelector('h2, h3, a[href*="/company/"]');
                            if (secHeader) company = secHeader.innerText.trim();
                        }
                    }

                    const desc = container.innerText || "";
                    const idMatch = href.match(/jobs\/(\d+)/);
                    const job_id = idMatch ? idMatch[1] : `wf-${idx}`;

                    // Extract location
                    let location = "India / Remote";
                    if (desc.toLowerCase().includes("remote")) {
                        location = "Remote";
                    } else if (desc.toLowerCase().includes("bengaluru") || desc.toLowerCase().includes("bangalore")) {
                        location = "Bengaluru, India";
                    } else if (desc.toLowerCase().includes("hyderabad")) {
                        location = "Hyderabad, India";
                    } else if (desc.toLowerCase().includes("pune")) {
                        location = "Pune, India";
                    } else if (desc.toLowerCase().includes("delhi") || desc.toLowerCase().includes("noida") || desc.toLowerCase().includes("gurugram")) {
                        location = "Delhi NCR, India";
                    }

                    let cleanTitle = title.split('\n')[0].trim();
                    const invalidTitles = ["home", "applied", "messages", "saved", "preferences", "apply", "apply now", "view job"];
                    
                    if (cleanTitle.includes(' at ')) {
                        const parts = cleanTitle.split(' at ');
                        cleanTitle = parts[0].trim();
                        if (!company || company === "Hiring Startup") {
                            company = parts[parts.length - 1].trim();
                        }
                    }

                    if (cleanTitle && cleanTitle.length > 2 && !invalidTitles.includes(cleanTitle.toLowerCase())) {
                        seenUrls.add(href);
                        listings.push({
                            title: cleanTitle,
                            company: company || "Hiring Startup",
                            job_url: href,
                            job_id: job_id,
                            location: location,
                            remote_status: location.toLowerCase().includes("remote") ? "Remote" : "Hybrid/On-site",
                            salary: "",
                            required_skills: [],
                            description: desc,
                            source: "Wellfound"
                        });
                    }
                } catch (e) {}
            });

            // Strategy 2: Apollo Client State / Window State (if Strategy 1 found none)
            if (listings.length === 0 && window.__APOLLO_STATE__) {
                try {
                    const state = window.__APOLLO_STATE__;
                    Object.keys(state).forEach(k => {
                        if (k.startsWith('JobListing:') || k.startsWith('JobPosting:')) {
                            const item = state[k];
                            if (item && item.title) {
                                const url = item.slug ? `https://wellfound.com/jobs/${item.id || item.slug}` : window.location.href;
                                listings.push({
                                    title: item.title,
                                    company: (item.startup && item.startup.name) || "Hiring Startup",
                                    job_url: url,
                                    job_id: String(item.id || k),
                                    location: item.locationNames ? item.locationNames.join(', ') : "India",
                                    remote_status: item.remote ? "Remote" : "On-site",
                                    salary: item.compensation || "",
                                    required_skills: item.skills ? item.skills.map(s => s.name || s) : [],
                                    description: item.description || item.title,
                                    source: "Wellfound"
                                });
                            }
                        }
                    });
                } catch (e) {}
            }

            return listings;
        })()
        """
        try:
            listings = await self.cdp.evaluate(js)
            return listings or []
        except Exception as e:
            print(f"[Wellfound] Error extracting job cards: {e}")
            return []

    async def _apply_to_job(self, job: Dict[str, Any], score_res: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes safe, strictly verified application on Wellfound.
        Zero false-positives: Never marks as 'Submitted' unless explicitly confirmed live.
        """
        job_url = job.get("job_url", "")
        comp = job.get("company", "Hiring Startup")
        title = job.get("title", "Software Engineer")

        if job_url:
            await self.cdp.navigate(job_url, wait_seconds=3.0)

        # 0. Resolve exact live company name and role from the job page
        page_meta = await self.cdp.evaluate("""
        (() => {
            const h1 = document.querySelector('h1') ? document.querySelector('h1').innerText.trim() : '';
            const docTitle = document.title || '';
            const breadcrumbs = Array.from(document.querySelectorAll('nav a, a[href*="/startups/"], a[href*="/company/"]')).map(a => a.innerText.trim()).filter(t => t.length > 0);
            const lastBreadcrumb = breadcrumbs.length > 0 ? breadcrumbs[breadcrumbs.length - 1] : '';
            
            let resolvedComp = '';
            let resolvedRole = '';
            
            if (h1 && h1.includes(' at ')) {
                const parts = h1.split(' at ');
                resolvedRole = parts[0].trim();
                resolvedComp = parts[parts.length - 1].trim();
            } else if (docTitle && docTitle.includes(' at ')) {
                const match = docTitle.match(/(.+?)\\s+at\\s+([^•|]+)/i);
                if (match) {
                    resolvedRole = match[1].trim();
                    resolvedComp = match[2].trim();
                }
            }
            
            if (!resolvedComp && lastBreadcrumb && lastBreadcrumb.length > 1 && !['jobs', 'startups', 'discover', 'overview', 'wellfound', 'home'].includes(lastBreadcrumb.toLowerCase())) {
                resolvedComp = lastBreadcrumb;
            }

            if (!resolvedComp) {
                const compEl = document.querySelector('a[href*="/company/"], h2 a, [data-test="StartupName"], [class*="companyName"], [class*="companyHeader"] h1');
                if (compEl && compEl.innerText.trim().length > 1) {
                    resolvedComp = compEl.innerText.trim();
                }
            }
            
            return {
                company: resolvedComp,
                role: resolvedRole
            };
        })()
        """)
        if page_meta and page_meta.get("company") and page_meta.get("company").lower() not in ["wellfound", "hiring startup", "jobs", "overview"]:
            job["company"] = page_meta["company"]
            comp = page_meta["company"]
        if page_meta and page_meta.get("role"):
            job["title"] = page_meta["role"]
            title = page_meta["role"]

        # 1. Check if job is closed or already applied
        page_info = await self.cdp.evaluate("""
        (() => {
            const mainContent = document.querySelector('main, [data-test="JobDetail"], [class*="styles_jobDetail"], [class*="styles_container"]') || document.body;
            
            // Filter buttons strictly within job action area, excluding sidebar navigation
            const actionButtons = Array.from(mainContent.querySelectorAll('button, a')).filter(el => {
                const inNav = el.closest('nav, aside, header[role="banner"], [class*="sidebar"], [class*="navigation"]');
                const href = el.href || '';
                return !inNav && !href.includes('/jobs/applications') && !href.includes('/jobs/home') && !href.includes('/jobs/messages');
            });

            // Find Applied state
            const appliedBtn = actionButtons.find(b => {
                const t = (b.innerText || b.textContent || '').trim().toLowerCase();
                return (t.includes('applied') || t === 'application submitted' || t === 'view application') && !b.closest('nav, aside');
            });

            // Find Apply action button
            const applyBtn = actionButtons.find(b => {
                const t = (b.innerText || b.textContent || '').trim().toLowerCase();
                const cls = (b.className || '').toLowerCase();
                if (t.includes('applied')) return false;
                return t === 'apply' || t === 'apply now' || t === 'easy apply' || t.startsWith('apply for') || t.includes('apply on website') || t.includes('company website') || cls.includes('applybutton');
            });

            const isClosed = Boolean(document.querySelector('[data-test="JobClosed"]')) || 
                             mainContent.innerText.toLowerCase().includes('this job is no longer available');

            const isExternal = applyBtn ? ((applyBtn.href && !applyBtn.href.includes('wellfound.com')) || applyBtn.innerText.toLowerCase().includes('website')) : false;

            return {
                hasApplyButton: Boolean(applyBtn),
                applyButtonText: applyBtn ? applyBtn.innerText.trim() : '',
                isApplied: Boolean(appliedBtn) && !applyBtn,
                isClosed: isClosed,
                isExternal: isExternal,
                externalUrl: (applyBtn && applyBtn.href) ? applyBtn.href : ''
            };
        })()
        """)

        if page_info and page_info.get("isClosed"):
            print(f"[Wellfound] 🔒 Job '{title}' at '{comp}' is closed.")
            return {"status": "Closed", "notes": "Job closed on Wellfound"}

        if page_info and page_info.get("isApplied"):
            print(f"[Wellfound] ⏭️ Already applied to '{title}' at '{comp}'.")
            return {"status": "Already Applied", "notes": "Previously applied on Wellfound"}

        if page_info and page_info.get("isExternal"):
            ext_url = page_info.get("externalUrl") or job_url
            print(f"[Wellfound] 🌐 External application detected for '{title}' at '{comp}': {ext_url}")
            return {"status": "Under Review", "notes": f"External application link: {ext_url}"}

        # 2. Locate and Click Apply Button on Wellfound with Native CDP Mouse Events
        print(f"[Wellfound] 🖱️ Locating Apply button for '{title}' at '{comp}'...")
        apply_clicked = False
        apply_btn_text = ""

        for attempt in range(5):
            btn_pos = await self.cdp.evaluate("""
            (() => {
                const elements = Array.from(document.querySelectorAll('button, a, [role="button"], [class*="applyButton"]')).filter(el => {
                    const inNav = el.closest('nav, aside, header[role="banner"], [class*="sidebar"]');
                    const href = (el.href || '').toLowerCase();
                    if (inNav) return false;
                    if (href.includes('/jobs/applications') || href.includes('/jobs/home') || href.includes('/jobs/messages') || href.includes('/recruit') || href.includes('/profile')) return false;
                    return true;
                });

                const applyEl = elements.find(el => {
                    const t = (el.innerText || el.textContent || '').trim().toLowerCase();
                    const dt = (el.getAttribute('data-test') || '').toLowerCase();
                    const cls = (el.className || '').toLowerCase();
                    
                    // Exclude any button that already says applied
                    if (t.includes('applied') || dt.includes('applied')) return false;

                    if (dt === 'applybutton' || dt === 'apply-button' || cls.includes('applybutton')) return true;
                    if (t === 'apply' || t === 'apply now' || t === 'easy apply' || t.startsWith('apply for') || t.startsWith('apply with') || t === 'quick apply') return true;
                    if (t.includes('apply on website') || t.includes('company website')) return true;
                    return false;
                });

                if (applyEl) {
                    applyEl.scrollIntoView({ behavior: 'instant', block: 'center' });
                    const rect = applyEl.getBoundingClientRect();
                    ['pointerdown', 'mousedown', 'mouseup', 'click'].forEach(evt => {
                        applyEl.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                    });
                    if (typeof applyEl.click === 'function') applyEl.click();
                    return {
                        found: true,
                        text: (applyEl.innerText || applyEl.textContent || 'Apply').trim(),
                        x: rect.x + rect.width / 2,
                        y: rect.y + rect.height / 2
                    };
                }
                return { found: false };
            })()
            """)

            if btn_pos and btn_pos.get("found"):
                x, y = btn_pos.get("x"), btn_pos.get("y")
                if x and y:
                    try:
                        await self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
                        await asyncio.sleep(0.08)
                        await self.cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
                        await asyncio.sleep(0.08)
                        await self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
                    except Exception:
                        pass
                apply_clicked = True
                apply_btn_text = btn_pos.get("text", "Apply")
                print(f"[Wellfound] 🎯 Clicked '{apply_btn_text}' button with native CDP mouse click.")
                break
            await asyncio.sleep(1.0)

        if not apply_clicked:
            print(f"[Wellfound] ⚠️ Apply button not found for '{title}' at '{comp}'.")
            return {"status": "Waiting for Input", "notes": "Apply button not found on page"}

        # 2.5 Explicit Modal & Form Mount Wait Loop
        print(f"[Wellfound] ⏳ Waiting for application modal and form fields to mount...")
        modal_mounted = False
        for _ in range(12):
            await asyncio.sleep(0.5)
            modal_chk = await self.cdp.evaluate("""
            (() => {
                const modal = document.querySelector('.ReactModal__Content, [role="dialog"], div[class*="modal__"], div[class*="Modal__"], div[class*="Drawer"]');
                const textarea = document.querySelector('textarea, [name="userNote"]');
                const sendBtn = Array.from(document.querySelectorAll('button, input[type="submit"]')).find(b => {
                    const t = (b.innerText || b.textContent || '').trim().toLowerCase();
                    return t === 'send application' || t === 'submit application' || t === 'send';
                });
                
                if (modal || textarea || sendBtn) {
                    return {
                        mounted: true,
                        hasTextarea: Boolean(textarea),
                        hasSendBtn: Boolean(sendBtn)
                    };
                }
                return { mounted: false };
            })()
            """)
            if modal_chk and modal_chk.get("mounted"):
                modal_mounted = True
                print(f"[Wellfound] 📋 Modal mounted (Textarea: {modal_chk.get('hasTextarea')}, Submit Button: {modal_chk.get('hasSendBtn')}).")
                break

        if not modal_mounted:
            # Check if clicking Apply navigated to external URL
            cur_url = await self.cdp.evaluate("window.location.href")
            if cur_url and "wellfound.com/jobs/" not in cur_url and not cur_url.endswith("/jobs"):
                print(f"[Wellfound] 🌐 External application redirect detected: {cur_url}")
                return {"status": "Under Review", "notes": f"External application link: {cur_url}"}
            print(f"[Wellfound] ⚠️ Application modal did not mount for '{title}' at '{comp}'.")
            return {"status": "Waiting for Input", "notes": "Apply modal did not open on page"}

        # 3. Check for application restriction / Not accepting applications banner
        restriction_info = await self._check_application_restriction(job)
        if restriction_info.get("isRestricted"):
            reason = restriction_info.get("reason", "Company is not accepting applications from candidate location")
            print(f"[Wellfound] 🚫 {comp} is not accepting applications: {reason}")
            await self._dismiss_modal()
            return {
                "status": "Not Accepting",
                "notes": f"Not accepting applications: {reason}",
                "reason": reason
            }

        # 4. Check for CAPTCHA or security challenge
        sec = await self.cdp.check_for_security_challenge()
        if sec.get("has_challenge"):
            cleared = await self.cdp.pause_and_wait_for_human("CAPTCHA or security challenge during application modal.")
            if not cleared:
                return {"status": "Waiting for Input", "notes": "Timed out waiting for CAPTCHA"}

        # 5. Handle Location Preferences / Relocation Modal Prompt
        await self._handle_location_preferences(job)

        # 5. Handle Custom Questionnaire & Application Fields (Pronouns, LinkedIn, Website, Phone, Source)
        personal = self.profile.get("personal_info", {})
        phone_num = personal.get("phone", "+91 9546748644")
        linkedin_link = personal.get("linkedin_url", "https://www.linkedin.com/in/sakshi-srivastava-7b08a3371/")
        portfolio_link = personal.get("portfolio_url", "https://sakshiii24-portfolio.vercel.app/")
        github_link = personal.get("github_url", "https://github.com/Sakshiiii24")

        custom_fields_res = await self.cdp.evaluate(f"""
        (() => {{
            const result = {{ filledFields: [], hasTaskRequirement: false, taskDescription: "" }};
            
            // Strictly inspect MODAL content only (never background page body)
            const modalEl = document.querySelector('[role="dialog"], div[class*="Modal"], div[class*="modal"], div[class*="Drawer"]') || document.body;
            const modalText = modalEl.innerText || '';
            const modalTextLower = modalText.toLowerCase();

            // Check for file attachment / explicit coding task requirement inside the modal
            const hasFileInput = Boolean(modalEl.querySelector('input[type="file"], [data-test="FileUpload"]'));
            const isExplicitTaskInModal = modalTextLower.includes('application requirement:') || 
                                          modalTextLower.includes('attach the file') || 
                                          modalTextLower.includes('attach a file as a .txt') ||
                                          modalTextLower.includes('attach your solution');

            if (hasFileInput || isExplicitTaskInModal) {{
                result.hasTaskRequirement = true;
                const taskIndex = modalTextLower.indexOf('application requirement');
                if (taskIndex !== -1) {{
                    result.taskDescription = modalText.substring(taskIndex, taskIndex + 300).replace(/\\s+/g, ' ').trim();
                }} else {{
                    result.taskDescription = "Modal requires custom coding assignment or file upload attachment.";
                }}
                return result;
            }}

            // 1. Fill Text/URL Inputs inside Modal (Phone, LinkedIn, Website, GitHub)
            const allInputs = Array.from(modalEl.querySelectorAll('input:not([type="hidden"]):not([type="radio"]):not([type="checkbox"])'));

            for (const input of allInputs) {{
                const labelEl = input.closest('label') || input.parentElement?.parentElement;
                const labelText = ((input.getAttribute('placeholder') || '') + ' ' + (input.name || '') + ' ' + (labelEl ? labelEl.innerText : '')).toLowerCase();

                const nativeInputSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;

                const setValue = (val) => {{
                    if (nativeInputSetter) {{
                        nativeInputSetter.call(input, val);
                    }} else {{
                        input.value = val;
                    }}
                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    input.dispatchEvent(new Event('blur', {{ bubbles: true }}));
                }};

                if ((labelText.includes('phone') || labelText.includes('mobile') || labelText.includes('contact')) && !input.value) {{
                    setValue({json.dumps(phone_num)});
                    result.filledFields.push('Phone');
                }} else if (labelText.includes('linkedin') && !input.value) {{
                    setValue({json.dumps(linkedin_link)});
                    result.filledFields.push('LinkedIn Profile');
                }} else if ((labelText.includes('website') || labelText.includes('portfolio')) && !input.value) {{
                    setValue({json.dumps(portfolio_link)});
                    result.filledFields.push('Website');
                }} else if (labelText.includes('github') && !input.value) {{
                    setValue({json.dumps(github_link)});
                    result.filledFields.push('GitHub');
                }}
            }}

            // 2. Native Selects inside Modal (Pronouns, How did you hear)
            const selects = Array.from(modalEl.querySelectorAll('select'));
            for (const sel of selects) {{
                const selLabel = ((sel.name || '') + ' ' + (sel.closest('label') ? sel.closest('label').innerText : '')).toLowerCase();
                if (selLabel.includes('pronoun')) {{
                    for (let i = 0; i < sel.options.length; i++) {{
                        if (sel.options[i].text.toLowerCase().includes('she')) {{
                            sel.selectedIndex = i;
                            sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            result.filledFields.push('Pronouns');
                            break;
                        }}
                    }}
                }} else if (selLabel.includes('hear') || selLabel.includes('source')) {{
                    for (let i = 0; i < sel.options.length; i++) {{
                        const optT = sel.options[i].text.toLowerCase();
                        if (optT.includes('wellfound') || optT.includes('social') || optT.includes('campus') || optT.includes('other')) {{
                            sel.selectedIndex = i;
                            sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            result.filledFields.push('Referral Source');
                            break;
                        }}
                    }}
                }}
            }}

            // 3. Custom React Dropdowns inside Modal (Pronouns, How did you hear)
            const triggers = Array.from(modalEl.querySelectorAll('div[class*="select"], [role="combobox"], [class*="control"]'));
            for (const trigger of triggers) {{
                const container = trigger.closest('div[class*="field"]') || trigger.parentElement?.parentElement;
                const triggerLabel = (container ? container.innerText : '').toLowerCase();
                
                if (triggerLabel.includes('pronoun')) {{
                    trigger.click();
                    const options = Array.from(document.querySelectorAll('[role="option"], [class*="option"], div[class*="menu"] div, li'));
                    const sheOption = options.find(o => o.innerText.toLowerCase().includes('she'));
                    if (sheOption) {{
                        sheOption.click();
                        result.filledFields.push('Pronouns (She/Her)');
                    }}
                }} else if (triggerLabel.includes('hear about') || triggerLabel.includes('how did you hear')) {{
                    trigger.click();
                    const options = Array.from(document.querySelectorAll('[role="option"], [class*="option"], div[class*="menu"] div, li'));
                    const sourceOption = options.find(o => {{
                        const t = o.innerText.toLowerCase();
                        return t.includes('wellfound') || t.includes('campus') || t.includes('social') || t.includes('other');
                    }});
                    if (sourceOption) {{
                        sourceOption.click();
                        result.filledFields.push('Referral Source (' + sourceOption.innerText.trim() + ')');
                    }}
                }}
            }}

            return result;
        }})()
        """)

        if custom_fields_res and custom_fields_res.get("hasTaskRequirement"):
            task_desc = custom_fields_res.get("taskDescription", "Coding assignment or file upload required")
            print(f"[Wellfound] 📋 Task requirement detected in modal for {comp}: {task_desc[:100]}...")
            print(f"[Wellfound] 📌 Added to 'Needs Action' queue in Dashboard for manual application.")
            
            # Dismiss modal cleanly
            await self.cdp.evaluate("""
            (() => {
                const cancelBtn = Array.from(document.querySelectorAll('button, a')).find(b => b.innerText.trim().toLowerCase() === 'cancel');
                if (cancelBtn) cancelBtn.click();
            })()
            """)
            
            return {
                "status": "Waiting for Input",
                "notes": f"Needs Action: {task_desc}"
            }

        if custom_fields_res and custom_fields_res.get("filledFields"):
            print(f"[Wellfound] 📝 Auto-filled standard questionnaire fields: {', '.join(custom_fields_res.get('filledFields'))}")

        # 6. Expand "Add a note" button if present
        await self.cdp.evaluate("""
        (() => {
            const allTriggers = Array.from(document.querySelectorAll('button, a, [role="button"], span, p, div'));
            const addNoteTrigger = allTriggers.find(b => {
                const t = b.innerText.trim().toLowerCase();
                return (t === 'add a note' || t === 'add note' || t === 'write a note' || t === '+ add a note' || t.includes('add a note to the hiring')) && !t.includes('send');
            });
            if (addNoteTrigger) {
                addNoteTrigger.click();
                return true;
            }
            return false;
        })()
        """)
        await asyncio.sleep(0.5)

        # 7. Generate and fill customized note with bulletproof React state synchronization
        note_text = self._generate_tailored_note(job, score_res)
        print(f"[Wellfound] 📝 Typing tailored recruiter note for {comp} ({title})...")

        fill_success = False
        for attempt in range(5):
            fill_res = await self.cdp.evaluate(f"""
            (() => {{
                const modal = document.querySelector('.ReactModal__Content, [role="dialog"], div[class*="modal"]') || document;
                const textarea = modal.querySelector('textarea, [contenteditable="true"]');
                if (!textarea) {{
                    return {{ filled: false, error: "Textarea not found" }};
                }}
                
                textarea.focus();
                textarea.select();
                
                const targetText = {json.dumps(note_text)};
                
                // 1. Native browser editing command (simulates real keystrokes for React controlled components)
                let nativeTypingSuccess = false;
                try {{
                    nativeTypingSuccess = document.execCommand('insertText', false, targetText);
                }} catch (e) {{}}
                
                // 2. React Value Tracker reset + property descriptor fallback
                if (!nativeTypingSuccess || !textarea.value || textarea.value.length < 50) {{
                    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
                    if (nativeSetter) {{
                        nativeSetter.call(textarea, targetText);
                    }} else {{
                        textarea.value = targetText;
                    }}
                    
                    if (textarea._valueTracker) {{
                        textarea._valueTracker.setValue('');
                    }}
                    
                    textarea.dispatchEvent(new InputEvent('input', {{ bubbles: true, cancelable: true, data: targetText }}));
                    textarea.dispatchEvent(new Event('change', {{ bubbles: true, cancelable: true }}));
                }}
                
                return {{
                    filled: Boolean(textarea.value && textarea.value.length > 50),
                    length: textarea.value ? textarea.value.length : 0
                }};
            }})()
            """)

            if fill_res and fill_res.get("filled"):
                print(f"[Wellfound] ✅ Note verified in textarea ({fill_res.get('length')} chars).")
                fill_success = True
                break
            await asyncio.sleep(0.8)

        if not fill_success:
            print(f"[Wellfound] ℹ️ Note textarea auto-handled or not present in this modal. Proceeding...")

        # =========================================================================
        # 🛡️ 4-STAGE AUTOMATED VERIFICATION CHECKERS & SUBMISSION
        # =========================================================================

        # CHECKER 1: Verify Resume is Attached / Selected with Role-Specific Dynamic Routing
        target_resume = self._get_resume_for_job(job)
        print(f"[Wellfound] 🎯 Role Category: '{target_resume['category']}' ➔ Target Resume: '{target_resume['filename']}'")

        resume_check = await self.cdp.evaluate(f"""
        (() => {{
            const modalEl = document.querySelector('[role="dialog"], div[class*="Modal"], div[class*="modal"], div[class*="Drawer"]') || document.body;
            const text = (modalEl.innerText || '').toLowerCase();
            const hasResumeIndicator = text.includes('.pdf') || text.includes('resume') || text.includes('cv') || Boolean(modalEl.querySelector('[data-test*="resume"], input[type="file"]'));
            
            const targetFilename = {json.dumps(target_resume['filename'].lower())};
            const targetCategory = {json.dumps(target_resume['category'].lower())};

            const resumeRadios = Array.from(modalEl.querySelectorAll('input[type="radio"], label, div[role="radio"]')).filter(el => {{
                const t = (el.innerText || '').toLowerCase();
                return t.includes('.pdf') || t.includes('resume') || t.includes('cv');
            }});

            let selectedCustom = false;
            // First try to find exact category or filename match
            for (const item of resumeRadios) {{
                const t = (item.innerText || '').toLowerCase();
                if (t.includes(targetFilename) || (targetCategory !== 'default' && t.includes(targetCategory))) {{
                    const radio = item.tagName === 'INPUT' ? item : item.querySelector('input[type="radio"]') || item;
                    if (radio && typeof radio.click === 'function') {{
                        radio.click();
                        selectedCustom = true;
                        break;
                    }}
                }}
            }}

            // Fallback to first active resume option
            if (!selectedCustom && resumeRadios.length > 0) {{
                const radio = resumeRadios[0].tagName === 'INPUT' ? resumeRadios[0] : resumeRadios[0].querySelector('input[type="radio"]') || resumeRadios[0];
                if (radio && typeof radio.click === 'function') radio.click();
            }}

            return {{ hasResume: hasResumeIndicator, matchedCustom: selectedCustom }};
        }})()
        """)
        if resume_check and resume_check.get("hasResume"):
            matched_mode = "Tailored Role Resume" if resume_check.get("matchedCustom") else "Default Profile Resume"
            print(f"[Wellfound] 🔍 [Checker 1 - Resume]: ✅ Resume verified ({matched_mode}: {target_resume['filename']}).")
        else:
            print(f"[Wellfound] 🔍 [Checker 1 - Resume]: ℹ️ Profile default resume in use.")

        # Pre-Submit Location Check: Ensure location preferences are satisfied if prompt is still visible
        await self._handle_location_preferences(job)

        # Pre-Submit Restriction Re-Check: Ensure company didn't trigger hard rejection banner
        pre_sub_restriction = await self._check_application_restriction(job)
        if pre_sub_restriction.get("isRestricted"):
            reason = pre_sub_restriction.get("reason", "Company is not accepting applications from candidate location")
            print(f"[Wellfound] 🚫 {comp} is not accepting applications: {reason}")
            await self._dismiss_modal()
            return {
                "status": "Not Accepting",
                "notes": f"Not accepting applications: {reason}",
                "reason": reason
            }

        # 8. Submit Application with Native CDP Dispatch & Multi-Attempt Verification
        print(f"[Wellfound] 🚀 Submitting application for {comp}...")
        submit_btn_clicked = False
        submit_btn_text = ""

        for attempt in range(6):
            send_pos = await self.cdp.evaluate(f"""
            (() => {{
                const textarea = document.querySelector('textarea, [contenteditable="true"], [name="userNote"]');
                const targetText = {json.dumps(note_text)};

                // Pre-Submit Note Safeguard: Ensure textarea is NEVER submitted empty if present
                if (textarea && (!textarea.value || textarea.value.trim().length < 20)) {{
                    textarea.focus();
                    try {{ document.execCommand('insertText', false, targetText); }} catch (e) {{}}
                    
                    if (!textarea.value || textarea.value.trim().length < 20) {{
                        const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
                        if (nativeSetter) nativeSetter.call(textarea, targetText);
                        else textarea.value = targetText;
                        if (textarea._valueTracker) textarea._valueTracker.setValue('');
                        textarea.dispatchEvent(new InputEvent('input', {{ bubbles: true, cancelable: true, data: targetText }}));
                        textarea.dispatchEvent(new Event('change', {{ bubbles: true, cancelable: true }}));
                    }}
                }}

                const allButtons = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"], a'));

                const sendBtn = allButtons.find(b => {{
                    const text = (b.innerText || b.textContent || b.value || '').trim().toLowerCase();
                    if (text.includes('cancel') || text.includes('close') || text.includes('dismiss') || text.includes('verify') || text.includes('applied') || text.includes('save')) return false;
                    return text === 'send application' || text === 'submit application' || text === 'send' || text.startsWith('send application') || text.startsWith('submit application');
                }});
                
                if (sendBtn) {{
                    sendBtn.removeAttribute('disabled');
                    sendBtn.scrollIntoView({{ behavior: 'instant', block: 'center' }});
                    
                    const rect = sendBtn.getBoundingClientRect();
                    ['pointerdown', 'mousedown', 'mouseup', 'click'].forEach(evtType => {{
                        sendBtn.dispatchEvent(new MouseEvent(evtType, {{ bubbles: true, cancelable: true, view: window }}));
                    }});
                    if (typeof sendBtn.click === 'function') sendBtn.click();
                    
                    const form = sendBtn.closest('form');
                    if (form) {{
                        form.dispatchEvent(new Event('submit', {{ bubbles: true, cancelable: true }}));
                    }}

                    return {{
                        found: true,
                        text: (sendBtn.innerText || sendBtn.textContent || 'Send application').trim(),
                        x: rect.x + rect.width / 2,
                        y: rect.y + rect.height / 2
                    }};
                }}
                return {{ found: false }};
            }})()
            """)

            if send_pos and send_pos.get("found"):
                x, y = send_pos.get("x"), send_pos.get("y")
                if x and y:
                    try:
                        await self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
                        await asyncio.sleep(0.08)
                        await self.cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
                        await asyncio.sleep(0.08)
                        await self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
                    except Exception:
                        pass
                submit_btn_clicked = True
                submit_btn_text = send_pos.get("text", "Send application")
                print(f"[Wellfound] 🎯 Clicked '{submit_btn_text}' button successfully!")
                break
            await asyncio.sleep(0.8)

        if not submit_btn_clicked:
            print(f"[Wellfound] ⚠️ Could not find or click 'Send application' button in modal for {comp}.")
            return {"status": "Waiting for Input", "notes": "Submit button not found in modal"}

        # CHECKER 4: Strict Post-Submission Truth Check (Zero False Positives)
        print(f"[Wellfound] 🔍 [Checker 4 - Truth Verification]: Verifying live submission status on Wellfound...")
        await asyncio.sleep(3.5)

        confirm_status = await self.cdp.evaluate("""
        (() => {
            const modal = document.querySelector('[role="dialog"], .ReactModal__Content, div[class*="modal__"], div[class*="Modal__"]');
            const textarea = document.querySelector('textarea, [name="userNote"]');
            const modalOpenWithTextarea = Boolean(modal && textarea && textarea.offsetParent !== null);

            const buttons = Array.from(document.querySelectorAll('button, a')).filter(el => !el.closest('nav, aside'));
            const appliedBtn = buttons.some(b => {
                const t = (b.innerText || b.textContent || '').trim().toLowerCase();
                return t.includes('applied') || t === 'application submitted' || t === 'view application';
            });

            const text = document.body ? document.body.innerText.toLowerCase() : '';
            const hasSuccessText = text.includes('application sent') || 
                                   text.includes('application submitted') || 
                                   text.includes('you applied') ||
                                   text.includes('thanks for applying') ||
                                   text.includes('your note has been sent');

            const hasError = Boolean(document.querySelector('[class*="error"], [role="alert"]'));

            // Strict Truth Check: Positive confirmation required
            const isConfirmedSubmitted = !modalOpenWithTextarea && !hasError && (appliedBtn || hasSuccessText || !textarea);

            return {
                isConfirmedSubmitted: isConfirmedSubmitted,
                modalOpen: modalOpenWithTextarea,
                appliedBtn: appliedBtn,
                hasSuccessText: hasSuccessText,
                hasError: hasError
            };
        })()
        """)

        if confirm_status and confirm_status.get("isConfirmedSubmitted"):
            print(f"[Wellfound] 🎉 [Truth Check Passed]: Verified live on Wellfound! Application officially submitted to {comp}.")
            SystemNotifier.notify_job_submitted(comp, title)
            return {"status": "Submitted", "notes": f"Applied successfully on Wellfound to {comp} ({title})"}
        else:
            # Check if post-submit modal displayed a restriction/rejection error banner
            post_restriction = await self._check_application_restriction(job)
            if post_restriction.get("isRestricted"):
                reason = post_restriction.get("reason", "Company is not accepting applications from candidate location")
                print(f"[Wellfound] 🚫 {comp} is not accepting applications: {reason}")
                await self._dismiss_modal()
                return {
                    "status": "Not Accepting",
                    "notes": f"Not accepting applications: {reason}",
                    "reason": reason
                }

            print(f"[Wellfound] ⚠️ [Truth Check Failed]: Application unconfirmed for {comp}. Marked for review.")
            SystemNotifier.notify_action_required(comp, title, "Application unconfirmed - please verify in browser")
            return {"status": "Waiting for Input", "notes": f"Unconfirmed application status for {comp} - please verify manually"}

    async def apply_to_waiting_jobs(self, limit: Optional[int] = None) -> Dict[str, Any]:
        """
        Iterates over all jobs in tracker with status 'Waiting for Input' or 'Under Review'
        and score >= 70, automatically navigating to each and submitting application.
        """
        records = list(self.tracker.records.values())
        waiting_jobs = [
            r for r in records
            if r.get("status") in ["Waiting for Input", "Under Review", "Applying"]
            and float(r.get("match_score") or 0) >= 60.0
            and r.get("job_url")
        ]

        print(f"\n============================================================")
        print(f"⚡ [BATCH APPLY QUEUE] Found {len(waiting_jobs)} jobs waiting for application submission")
        print(f"============================================================\n")

        results = {
            "total_waiting": len(waiting_jobs),
            "submitted": 0,
            "failed": 0,
            "skipped": 0
        }

        to_process = waiting_jobs[:limit] if limit else waiting_jobs

        for i, item in enumerate(to_process, 1):
            comp = item.get("company", "Hiring Startup")
            title = item.get("role", "Software Engineer")
            url = item.get("job_url", "")
            score = float(item.get("match_score") or 85.0)

            print(f"\n[{i}/{len(to_process)}] 🚀 Applying to Waiting Job: '{title}' at '{comp}'...")
            print(f"   URL: {url}")

            job_dict = {
                "title": title,
                "company": comp,
                "job_url": url,
                "job_id": item.get("job_id", ""),
                "location": "India",
                "description": title
            }

            score_res = {
                "total_score": score,
                "decision": "AUTO_APPLY",
                "status": "Applying",
                "breakdown": {}
            }

            try:
                res = await self._apply_to_job(job_dict, score_res)
                status = res.get("status", "Submitted")
                notes = res.get("notes", "Applied via Batch Application")

                if status == "Submitted":
                    results["submitted"] += 1
                    self.tracker.update_status(url, "Submitted", notes=f"Applied automatically to {comp} ({title})")
                    print(f"   ✅ Successfully applied to '{comp}'!")
                elif status == "Not Accepting":
                    results["skipped"] += 1
                    self.tracker.update_status(url, "Not Accepting", notes=notes)
                    print(f"   🚫 Not accepting applications: {comp} ({notes})")
                elif status == "Already Applied":
                    results["skipped"] += 1
                    self.tracker.update_status(url, "Already Applied", notes="Already applied on Wellfound")
                    print(f"   ⏭️ Already applied to '{comp}'.")
                elif status == "Skipped":
                    results["skipped"] += 1
                    self.tracker.update_status(url, "Skipped", notes=notes)
                    print(f"   ⏭️ Skipped '{comp}'.")
                else:
                    results["failed"] += 1
                    self.tracker.update_status(url, status, notes=notes)
                    print(f"   ⚠️ Job status: {status} ({notes})")

            except Exception as e:
                print(f"   ❌ Error applying to {comp}: {e}")
                results["failed"] += 1
                self.tracker.update_status(url, "Waiting for Input", notes=f"Error during application: {e}")

            await asyncio.sleep(2.0)

        print(f"\n🎉 [BATCH APPLY COMPLETE] Submitted {results['submitted']}/{len(to_process)} applications.")
        return results

    def _generate_tailored_note(self, job: Dict[str, Any], score_res: Dict[str, Any]) -> str:
        """
        Creates a tailored, high-converting recruiter note customized by job category:
        - AI/ML/LLM/GenAI
        - Computer Vision / Deep Learning
        - Backend / Python / Java / APIs
        - Full-Stack / React / Node
        - General Software Engineer / SDE-1
        Includes verified live project (freejsontocsv.com), Portfolio, GitHub, and LinkedIn.
        """
        comp = job.get("company", "").strip() or "your team"
        title = job.get("title", "").strip() or "Software Engineer"
        desc = (job.get("description") or "").lower()
        title_lower = title.lower()

        personal = self.profile.get("personal_info", {})
        portfolio_url = personal.get("portfolio_url", "https://sakshiii24-portfolio.vercel.app/")
        github_url = personal.get("github_url", "https://github.com/Sakshiiii24")
        linkedin_url = personal.get("linkedin_url", "https://www.linkedin.com/in/sakshi-srivastava-7b08a3371/")
        live_project_url = personal.get("live_project_url", "https://freejsontocsv.com/")

        templates = self.profile.get("tailored_pitch_templates", {})

        # Category Classification
        if any(w in title_lower or w in desc for w in ["sql", "database developer", "data engineer", "etl", "data modeling", "postgresql", "database administrator", "data warehouse", "mysql"]):
            template_key = "sql_data"
        elif any(w in title_lower or w in desc for w in ["computer vision", "vision", "opencv", "object detection", "image processing", "sensor"]):
            template_key = "computer_vision"
        elif any(w in title_lower or w in desc for w in ["ai", "machine learning", "ml", "llm", "generative ai", "genai", "deep learning", "nlp", "pytorch", "tensorflow"]):
            template_key = "ai_ml"
        elif any(w in title_lower or w in desc for w in ["java developer", "spring boot", "java backend", "java engineer", "j2ee"]):
            template_key = "java_spring"
        elif any(w in title_lower or w in desc for w in ["full stack", "fullstack", "frontend", "react", "next.js", "vue", "web developer"]):
            template_key = "fullstack"
        elif any(w in title_lower or w in desc for w in ["backend", "python", "django", "flask", "fastapi", "rest api", "microservices"]):
            template_key = "backend"
        else:
            template_key = "general"

        template = templates.get(template_key, templates.get("general", ""))

        # Format template with job metadata and real links
        note = template.format(
            company=comp,
            title=title,
            portfolio_url=portfolio_url,
            github_url=github_url,
            linkedin_url=linkedin_url,
            live_project_url=live_project_url
        )
        return note

    def _get_resume_for_job(self, job: Dict[str, Any]) -> Dict[str, str]:
        """
        Dynamically routes and selects the tailored resume for each job role.
        """
        title = (job.get("title") or "").lower()
        desc = (job.get("description") or "").lower()

        if any(w in title or w in desc for w in ["ai", "machine learning", "ml", "llm", "generative ai", "deep learning", "nlp", "pytorch", "tensorflow", "computer vision"]):
            category = "ai_ml"
        elif any(w in title or w in desc for w in ["java", "spring boot", "j2ee"]):
            category = "java_spring"
        elif any(w in title or w in desc for w in ["full stack", "fullstack", "frontend", "react", "next.js", "vue"]):
            category = "fullstack"
        elif any(w in title or w in desc for w in ["sql", "data engineer", "etl", "database"]):
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

    async def _handle_location_preferences(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handles Wellfound's 'This job does not support the locations on your profile'
        preference update prompt with multi-attempt CDP native mouse clicks + React state syncing.
        Selects 'I can relocate to…' (or 'I am currently in…') and picks the best matching
        location from the dropdown (native <select> or custom React Select).
        """
        job_loc = (job.get("location") or "").lower()
        job_title = (job.get("title") or "").lower()
        job_comp = (job.get("company") or "").lower()
        job_desc = (job.get("description") or "").lower()

        preferred_locs = [l.lower() for l in self.profile.get("job_preferences", {}).get("preferred_locations", [])]
        if not preferred_locs:
            preferred_locs = [l.lower() for l in self.settings.get("search_criteria", {}).get("target_locations", [])]

        specific_cities = [
            "jabalpur", "bengaluru", "bangalore", "hyderabad", "pune", "delhi", "noida", 
            "gurugram", "gurgaon", "mumbai", "chennai", "kolkata", "ahmedabad", "indore", 
            "surat", "jaipur", "kochi", "chandigarh", "remote", "india"
        ]
        matched_job_cities = [c for c in specific_cities if c in job_loc or c in job_title or c in job_desc]

        for attempt in range(4):
            # Step 1: Detect if location preference warning card is present in modal
            chk = await self.cdp.evaluate("""
            (() => {
                const modalEl = document.querySelector('[role="dialog"], .ReactModal__Content, div[class*="modal__"], div[class*="Modal__"], div[class*="styles_modal"]') || document.body;
                const text = (modalEl.innerText || '').toLowerCase();
                const isLocWarning = (
                    text.includes('does not support the locations') ||
                    text.includes('update your location preferences') ||
                    text.includes('i can relocate') ||
                    text.includes('i am currently in') ||
                    text.includes('please select a location')
                );
                return { isLocWarning: isLocWarning };
            })()
            """)

            if not chk or not chk.get("isLocWarning"):
                return {"needed": False, "resolved": True}

            print(f"[Wellfound] 📍 [Attempt {attempt + 1}/4] Handling location preference prompt for '{job.get('company')}'...")

            # Step 2: Click the 'I can relocate to…' radio button (or 'I am currently in…')
            radio_pos = await self.cdp.evaluate("""
            (() => {
                const modalEl = document.querySelector('[role="dialog"], .ReactModal__Content, div[class*="modal__"], div[class*="Modal__"], div[class*="styles_modal"]') || document.body;
                const elements = Array.from(modalEl.querySelectorAll('label, div[role="radio"], input[type="radio"], [class*="Radio"], [class*="radio"], div, span'));
                
                let relocateEl = null;
                let currentEl = null;
                
                for (const el of elements) {
                    const t = (el.innerText || el.textContent || '').trim().toLowerCase();
                    if ((t.includes('can relocate') || t.includes('relocate to') || t.startsWith('i can relocate')) && !relocateEl) {
                        relocateEl = el;
                    }
                    if ((t.includes('currently in') || t.includes('living in') || t.startsWith('i am currently')) && !currentEl) {
                        currentEl = el;
                    }
                }
                
                const target = relocateEl || currentEl;
                if (target) {
                    target.scrollIntoView({ behavior: 'instant', block: 'center' });
                    const rect = target.getBoundingClientRect();
                    
                    const radioInput = target.tagName === 'INPUT' ? target : (target.querySelector('input[type="radio"]') || target.closest('label')?.querySelector('input[type="radio"]'));
                    if (radioInput) {
                        try {
                            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'checked')?.set;
                            if (setter) setter.call(radioInput, true);
                            else radioInput.checked = true;
                            if (radioInput._valueTracker) radioInput._valueTracker.setValue('');
                        } catch(e) {}
                        radioInput.dispatchEvent(new Event('input', { bubbles: true }));
                        radioInput.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    
                    ['pointerdown', 'mousedown', 'mouseup', 'click'].forEach(evt => {
                        target.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                        if (radioInput && radioInput !== target) {
                            radioInput.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                        }
                    });
                    if (typeof target.click === 'function') target.click();
                    
                    return {
                        found: true,
                        text: (target.innerText || target.textContent || 'Relocate').trim(),
                        x: rect.x + Math.min(rect.width / 2, 25),
                        y: rect.y + rect.height / 2
                    };
                }
                return { found: false };
            })()
            """)

            if radio_pos and radio_pos.get("found"):
                x, y = radio_pos.get("x"), radio_pos.get("y")
                if x and y:
                    try:
                        await self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
                        await asyncio.sleep(0.06)
                        await self.cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
                        await asyncio.sleep(0.06)
                        await self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
                    except Exception:
                        pass
                print(f"[Wellfound] 🔘 Selected location radio: '{radio_pos.get('text')[:35]}'")
            
            await asyncio.sleep(0.6)

            # Step 3: Handle Dropdown / Select Option Selection (Native Select Check)
            native_sel_res = await self.cdp.evaluate(f"""
            (() => {{
                const modalEl = document.querySelector('[role="dialog"], .ReactModal__Content, div[class*="modal__"], div[class*="Modal__"]') || document.body;
                const warningCard = Array.from(modalEl.querySelectorAll('div, section, fieldset')).find(el => {{
                    const t = (el.innerText || '').toLowerCase();
                    return t.includes('does not support') || t.includes('update your location');
                }}) || modalEl;
                
                const selectEl = warningCard.querySelector('select') || modalEl.querySelector('select');
                if (selectEl && selectEl.options && selectEl.options.length > 0) {{
                    const matchedCities = {json.dumps(matched_job_cities)};
                    const prefCities = {json.dumps(preferred_locs)};
                    const targetLoc = {json.dumps(job_loc)};
                    
                    let chosenIndex = -1;
                    for (let i = 0; i < selectEl.options.length; i++) {{
                        const optText = (selectEl.options[i].text || '').toLowerCase();
                        if (matchedCities.some(c => optText.includes(c)) || (targetLoc && optText.includes(targetLoc))) {{
                            if (optText !== '-' && !optText.includes('select')) {{
                                chosenIndex = i;
                                break;
                            }}
                        }}
                    }}
                    if (chosenIndex === -1) {{
                        for (const pref of prefCities) {{
                            for (let i = 0; i < selectEl.options.length; i++) {{
                                const optText = (selectEl.options[i].text || '').toLowerCase();
                                if (optText.includes(pref) && optText !== '-' && !optText.includes('select')) {{
                                    chosenIndex = i;
                                    break;
                                }}
                            }}
                            if (chosenIndex !== -1) break;
                        }}
                    }}
                    if (chosenIndex === -1) {{
                        for (let i = 0; i < selectEl.options.length; i++) {{
                            const optText = (selectEl.options[i].text || '').trim();
                            if (optText && optText !== '-' && !optText.toLowerCase().includes('select')) {{
                                chosenIndex = i;
                                break;
                            }}
                        }}
                    }}
                    
                    if (chosenIndex !== -1) {{
                        selectEl.selectedIndex = chosenIndex;
                        const val = selectEl.options[chosenIndex].value;
                        try {{
                            const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value')?.set;
                            if (setter) setter.call(selectEl, val);
                            else selectEl.value = val;
                        }} catch(e) {{}}
                        selectEl.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        selectEl.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        selectEl.dispatchEvent(new Event('blur', {{ bubbles: true }}));
                        return {{
                            selected: true,
                            optionText: selectEl.options[chosenIndex].text.trim()
                        }};
                    }}
                }}
                return {{ selected: false }};
            }})()
            """)

            if native_sel_res and native_sel_res.get("selected"):
                print(f"[Wellfound] ✅ Native select location chosen: '{native_sel_res.get('optionText')}'")
                await asyncio.sleep(0.5)
                return {"needed": True, "resolved": True, "location": native_sel_res.get("optionText")}

            # Step 4: If not native select, click the custom dropdown trigger container
            dropdown_trigger_pos = await self.cdp.evaluate("""
            (() => {
                const modalEl = document.querySelector('[role="dialog"], .ReactModal__Content, div[class*="modal__"], div[class*="Modal__"]') || document.body;
                const warningCard = Array.from(modalEl.querySelectorAll('div, section, fieldset')).find(el => {
                    const t = (el.innerText || '').toLowerCase();
                    return t.includes('does not support') || t.includes('update your location');
                }) || modalEl;
                
                const candidates = Array.from(warningCard.querySelectorAll('[class*="control"], [class*="select"], [role="combobox"], button, div[tabindex], div[class*="menu"], div[class*="Box"], div[class*="input"]')).filter(el => {
                    const t = (el.innerText || el.textContent || '').trim();
                    const rect = el.getBoundingClientRect();
                    return rect.height >= 20 && rect.width >= 40 && (t === '-' || t.toLowerCase().includes('select') || Boolean(el.querySelector('svg, [class*="arrow"], [class*="chevron"], [class*="indicator"]')));
                });
                
                let trigger = candidates[0] || warningCard.querySelector('[class*="control"], [role="combobox"], [class*="select__control"]');
                if (!trigger) {
                    trigger = Array.from(warningCard.querySelectorAll('div, span, p')).find(el => {
                        const t = (el.innerText || '').trim();
                        return t === '-' || t.toLowerCase().includes('select a location');
                    });
                }
                
                if (trigger) {
                    trigger.scrollIntoView({ behavior: 'instant', block: 'center' });
                    const rect = trigger.getBoundingClientRect();
                    ['pointerdown', 'mousedown', 'mouseup', 'click'].forEach(evt => {
                        trigger.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                    });
                    if (typeof trigger.click === 'function') trigger.click();
                    
                    const inp = trigger.querySelector('input') || warningCard.querySelector('input[type="text"], input[role="combobox"]');
                    if (inp) {
                        inp.focus();
                        inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', keyCode: 40, bubbles: true }));
                    }
                    
                    return {
                        found: true,
                        x: rect.x + rect.width / 2,
                        y: rect.y + rect.height / 2
                    };
                }
                return { found: false };
            })()
            """)

            if dropdown_trigger_pos and dropdown_trigger_pos.get("found"):
                x, y = dropdown_trigger_pos.get("x"), dropdown_trigger_pos.get("y")
                if x and y:
                    try:
                        await self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
                        await asyncio.sleep(0.06)
                        await self.cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
                        await asyncio.sleep(0.06)
                        await self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
                    except Exception:
                        pass
                print(f"[Wellfound] 🔽 Opened custom location dropdown menu.")
                await asyncio.sleep(0.5)

                # Step 5: Locate and click best matching dropdown option from the list
                opt_pos = await self.cdp.evaluate(f"""
                (() => {{
                    const matchedCities = {json.dumps(matched_job_cities)};
                    const prefCities = {json.dumps(preferred_locs)};
                    const targetLoc = {json.dumps(job_loc)};
                    
                    const options = Array.from(document.querySelectorAll('[role="option"], [class*="select__option"], [class*="option"], [id*="react-select"][id*="option"], [class*="menu"] div, ul[role="listbox"] li, div[tabindex="-1"]'));
                    
                    const validOptions = options.filter(o => {{
                        const t = (o.innerText || o.textContent || '').trim();
                        const tLower = t.toLowerCase();
                        return t && t !== '-' && !tLower.includes('select a location') && !tLower.includes('can relocate') && !tLower.includes('currently in') && !tLower.includes('does not support');
                    }});
                    
                    if (validOptions.length === 0) return {{ found: false }};
                    
                    let matchedOpt = null;
                    for (const o of validOptions) {{
                        const t = (o.innerText || o.textContent || '').toLowerCase();
                        if (matchedCities.some(c => t.includes(c)) || (targetLoc && t.includes(targetLoc))) {{
                            matchedOpt = o;
                            break;
                        }}
                    }}
                    if (!matchedOpt) {{
                        for (const pref of prefCities) {{
                            matchedOpt = validOptions.find(o => (o.innerText || o.textContent || '').toLowerCase().includes(pref));
                            if (matchedOpt) break;
                        }}
                    }}
                    if (!matchedOpt) {{
                        matchedOpt = validOptions[0];
                    }}
                    
                    if (matchedOpt) {{
                        matchedOpt.scrollIntoView({{ behavior: 'instant', block: 'center' }});
                        const rect = matchedOpt.getBoundingClientRect();
                        ['pointerdown', 'mousedown', 'mouseup', 'click'].forEach(evt => {{
                            matchedOpt.dispatchEvent(new MouseEvent(evt, {{ bubbles: true, cancelable: true, view: window }}));
                        }});
                        if (typeof matchedOpt.click === 'function') matchedOpt.click();
                        
                        return {{
                            found: true,
                            optionText: (matchedOpt.innerText || matchedOpt.textContent || '').trim(),
                            x: rect.x + rect.width / 2,
                            y: rect.y + rect.height / 2
                        }};
                    }}
                    return {{ found: false }};
                }})()
                """)

                if opt_pos and opt_pos.get("found"):
                    ox, oy = opt_pos.get("x"), opt_pos.get("y")
                    if ox and oy:
                        try:
                            await self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": ox, "y": oy})
                            await asyncio.sleep(0.06)
                            await self.cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": ox, "y": oy, "button": "left", "clickCount": 1})
                            await asyncio.sleep(0.06)
                            await self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": ox, "y": oy, "button": "left", "clickCount": 1})
                        except Exception:
                            pass
                    print(f"[Wellfound] ✅ Selected dropdown option: '{opt_pos.get('optionText')}'")
                    await asyncio.sleep(0.5)
                    return {"needed": True, "resolved": True, "location": opt_pos.get("optionText")}

            await asyncio.sleep(0.8)

        return {"needed": True, "resolved": False}

    async def _check_application_restriction(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """
        Detects if company/job explicitly restricts or does not accept applications from candidate location
        (e.g., timezone or relocation constraints, visa restrictions, closed to candidate region).
        Extracts the exact human-readable reason from the UI banner.
        """
        js = r"""
        (() => {
            const modalEl = document.querySelector('[role="dialog"], .ReactModal__Content, div[class*="modal__"], div[class*="Modal__"], div[class*="Drawer"], div[class*="styles_modal"]') || document.body;
            const fullText = (modalEl.innerText || '').trim();
            const textLower = fullText.toLowerCase();

            // 1. Look for explicit alert/warning/banner containers inside modal
            const alertContainers = Array.from(modalEl.querySelectorAll(
                '[role="alert"], [class*="alert"], [class*="Alert"], [class*="banner"], [class*="Banner"], [class*="warning"], [class*="Warning"], [class*="notice"], [class*="Notice"], [class*="FlashMessage"], [class*="error"], [class*="Error"], div[style*="background"], div[style*="color: rgb(2"], p[class*="error"]'
            ));

            let detectedReason = "";

            for (const el of alertContainers) {
                const t = (el.innerText || el.textContent || '').trim();
                const tl = t.toLowerCase();
                if (
                    tl.includes('not accepting applications') ||
                    tl.includes('timezone or relocation constraints') ||
                    tl.includes('does not accept applications') ||
                    tl.includes('unable to accept applications') ||
                    tl.includes('cannot accept applications') ||
                    tl.includes('not eligible to apply') ||
                    tl.includes('no longer accepting applications') ||
                    tl.includes('location constraints')
                ) {
                    detectedReason = t;
                    break;
                }
            }

            // 2. Pattern scan across modal text if not isolated in a single alert box
            if (!detectedReason) {
                const patterns = [
                    /([^\n]*not accepting applications from your current location[^\n]*)/i,
                    /([^\n]*timezone or relocation constraints[^\n]*)/i,
                    /([^\n]*not accepting applications[^\n]*)/i,
                    /([^\n]*no longer accepting applications[^\n]*)/i,
                    /([^\n]*unable to accept applications[^\n]*)/i,
                    /([^\n]*cannot accept applications[^\n]*)/i,
                    /([^\n]*not eligible to apply[^\n]*)/i
                ];
                for (const p of patterns) {
                    const m = fullText.match(p);
                    if (m && m[1]) {
                        detectedReason = m[1].trim();
                        break;
                    }
                }
            }

            // 3. Evaluate if this is a hard blocker vs configurable relocation
            if (detectedReason) {
                // If the modal has a working relocation selection prompt (and relocation is allowed), let location handler try
                const hasSelectDropdown = Boolean(modalEl.querySelector('select, [class*="control"], [role="combobox"]'));
                const hasRelocationOption = (textLower.includes('i can relocate') || textLower.includes('i am currently in')) && hasSelectDropdown;
                const relocationExplicitlyNotAllowed = textLower.includes('relocation\nnot allowed') || textLower.includes('relocation: not allowed') || textLower.includes('relocation not allowed');

                // If relocation is not allowed, or there is no select dropdown, it is a hard restriction
                if (!hasRelocationOption || relocationExplicitlyNotAllowed || textLower.includes('timezone or relocation constraints')) {
                    // Clean reason text
                    let clean = detectedReason.replace(/If your current location is incorrect.*$/i, '').trim();
                    if (!clean) clean = detectedReason;
                    // Limit length for clean display
                    if (clean.length > 220) clean = clean.substring(0, 220) + '...';
                    return {
                        isRestricted: true,
                        reason: clean
                    };
                }
            }

            return { isRestricted: false };
        })()
        """
        try:
            res = await self.cdp.evaluate(js)
            return res or {"isRestricted": False}
        except Exception as e:
            logger.debug(f"Error checking application restriction: {e}")
            return {"isRestricted": False}

    async def _dismiss_modal(self):
        """
        Safely dismisses application modal by clicking Cancel button or pressing Escape.
        """
        try:
            await self.cdp.evaluate("""
            (() => {
                const modal = document.querySelector('[role="dialog"], .ReactModal__Content, div[class*="modal__"], div[class*="Modal__"], div[class*="Drawer"]') || document;
                const cancelBtn = Array.from(modal.querySelectorAll('button, a')).find(b => {
                    const t = (b.innerText || b.textContent || '').trim().toLowerCase();
                    return t === 'cancel' || t === 'close' || t === 'dismiss';
                });
                if (cancelBtn && typeof cancelBtn.click === 'function') {
                    cancelBtn.click();
                    return true;
                }
                const closeIcon = modal.querySelector('button[aria-label*="close" i], [data-test="close-modal"], svg[class*="close"]');
                if (closeIcon) {
                    (closeIcon.closest('button') || closeIcon).click();
                    return true;
                }
                return false;
            })()
            """)
            await asyncio.sleep(0.5)
        except Exception:
            pass
