#!/usr/bin/env python3
"""
Shine Job Application Automation Agent CLI
Autonomous job-search and application assistant with dedicated Chrome profile integration for Shine.com.
Adheres strictly to the Genesis reference architecture.
"""

import argparse
import asyncio
import json
import os
import sys
import subprocess
from typing import Any, Dict, List, Optional

from src.cdp_client import ChromeCDPClient
from src.scorer import JobScorer
from src.tracker import ApplicationTracker
from src.reporter import RunReporter
from src.notifier import SystemNotifier
from src.platforms.shine import ShineAutomation


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


async def run_automation(source: str = "shine", keywords: Optional[List[str]] = None, limit: Optional[int] = None, test_mode: bool = False):
    """
    Main orchestration entrypoint for autonomous Shine.com job search & application.
    """
    profile = load_json("config/candidate_profile.json")
    settings = load_json("config/settings.json")
    browser_cfg = settings.get("browser", {})

    if test_mode:
        limit = 1
        print("\n🧪 [TEST MODE ACTIVE] Will process and apply to exactly 1 target company on Shine for verification.")

    cdp = ChromeCDPClient(
        port=browser_cfg.get("remote_debugging_port", 9222),
        user_data_dir=browser_cfg.get("user_data_dir", "~/.config/google-chrome-automation"),
        profile_directory=browser_cfg.get("profile_directory", "Profile 4"),
        profile_name=browser_cfg.get("profile_name", "Sakshi-Automation")
    )

    print("\n" + "=" * 65)
    print("🚀 STARTING SHINE.COM JOB APPLICATION AUTOMATION AGENT")
    print(f"👤 Candidate: {profile.get('personal_info', {}).get('full_name')}")
    print(f"🌐 Chrome Profile: {cdp.profile_name} ({cdp.profile_directory})")
    print(f"🎯 Thresholds: Auto-Apply >= {settings.get('scoring', {}).get('thresholds', {}).get('auto_apply_min_score')} | Review Queue >= {settings.get('scoring', {}).get('thresholds', {}).get('review_queue_min_score')}")
    if limit is not None:
        print(f"🛑 Run Application Limit: {limit}")
    print("=" * 65 + "\n")

    connected = await cdp.connect(target_url_match="shine.com", create_new=False)
    if not connected:
        print(f"[Agent] ❌ Could not connect to Chrome with profile '{cdp.profile_name}'.")
        print("[Agent] Please launch Chrome with remote debugging:\n  python3 main.py launch-browser\n")
        SystemNotifier.notify_run_failed("Could not connect to Chrome debugging port 9222.")
        return

    scorer = JobScorer(profile, settings)
    tracker = ApplicationTracker()
    reporter = RunReporter()
    run_results = []

    try:
        print("\n[Agent] Starting Shine.com Workflow...")
        shine = ShineAutomation(cdp, scorer, tracker, profile, settings)
        res = await shine.search_and_process_jobs(keywords=keywords, limit=limit)
        run_results.append(res)

        # Notify completion
        total_sub = sum(r.get("applications_submitted", 0) for r in run_results)
        total_rev = sum(r.get("jobs_under_review", 0) for r in run_results)
        total_eval = sum(r.get("jobs_evaluated", 0) for r in run_results)
        SystemNotifier.notify_run_completed(submitted=total_sub, review=total_rev, total=total_eval)

    except asyncio.CancelledError:
        print("\n[Agent] 🛑 Automation cancelled by user.")
        SystemNotifier.notify_run_stopped("Shine automation cancelled by user.")
    except Exception as e:
        print(f"[Agent] Unexpected error during run: {e}")
        SystemNotifier.notify_run_failed(str(e))
        run_results.append({
            "source": "Shine",
            "jobs_found": 0,
            "jobs_evaluated": 0,
            "jobs_skipped": 0,
            "jobs_under_review": 0,
            "applications_submitted": 0,
            "applications_manual_action": 0,
            "duplicates_ignored": 0,
            "errors": [str(e)],
            "attention_urls": []
        })
    finally:
        await cdp.close()

    # Generate Final Report
    report_output = reporter.generate_report(run_results)
    print(report_output)


def show_status():
    tracker = ApplicationTracker()
    tracker.print_summary()


def review_queue():
    tracker = ApplicationTracker()
    records = tracker.records
    review_items = [v for v in records.values() if v.get("status") in ("Under Review", "Waiting for Input")]
    print(f"\n=======================================================")
    print(f"📝 SHINE REVIEW QUEUE ({len(review_items)} jobs waiting for your decision)")
    print(f"=======================================================")
    if not review_items:
        print("No jobs currently in review queue. Great job!")
        return

    for i, item in enumerate(review_items, 1):
        print(f"\n[{i}] {item.get('company')} - {item.get('role')}")
        print(f"    Match Score: {item.get('match_score')}/100")
        print(f"    Status:      {item.get('status')}")
        print(f"    URL:         {item.get('job_url')}")
        print(f"    Notes:       {item.get('notes')}")
        print("    " + "-" * 50)


def test_score():
    profile = load_json("config/candidate_profile.json")
    settings = load_json("config/settings.json")
    scorer = JobScorer(profile, settings)

    sample_job = {
        "title": "Software Engineer - SDE 1 (Python / Backend)",
        "company": "FastTech Innovations",
        "description": "We are seeking a junior Software Engineer with 0-2 years of experience. Stack: Python, FastAPI, Django, REST APIs, PostgreSQL, Docker, Git. Looking for strong fundamentals in OOP, Data Structures and Algorithms.",
        "skills": ["Python", "FastAPI", "PostgreSQL", "REST APIs", "Git", "Docker"],
        "location": "Bengaluru, India",
        "job_url": "https://www.shine.com/jobs/software-engineer/fasttech-innovations/12345678",
        "source": "Shine"
    }

    result = scorer.evaluate(sample_job)
    print("\n" + "=" * 60)
    print("🧪 TEST SCORING ENGINE EVALUATION (SHINE)")
    print("=" * 60)
    print(f"Job Title:   {sample_job['title']}")
    print(f"Company:     {sample_job['company']}")
    print(f"Skills:      {sample_job['skills']}")
    print("Experience:  0-2 years")
    print("-" * 60)
    print(f"🎯 Total Score: {result['total_score']} / 100")
    print(f"📋 Decision:    {result['decision']} (Status: {result['status']})")
    print("\nScore Breakdown:")
    for k, v in result.get("breakdown", {}).items():
        print(f"  • {k.replace('_', ' ').title()}: {v['score']}/{v['max']} pts ({v.get('details', '')})")
    print("=" * 60 + "\n")


def launch_browser():
    settings = load_json("config/settings.json")
    browser_cfg = settings.get("browser", {})
    port = browser_cfg.get("remote_debugging_port", 9222)
    user_data_dir = os.path.abspath(os.path.expanduser(browser_cfg.get("user_data_dir", "~/.config/google-chrome-automation")))
    profile_name = browser_cfg.get("profile_name", "Sakshi-Automation")
    profile_dir = browser_cfg.get("profile_directory", "Profile 4")

    os.makedirs(user_data_dir, exist_ok=True)

    env = os.environ.copy()
    if "DISPLAY" not in env:
        env["DISPLAY"] = ":0"
    if "WAYLAND_DISPLAY" not in env and os.path.exists(f"/run/user/{os.getuid()}/wayland-0"):
        env["WAYLAND_DISPLAY"] = "wayland-0"
    if "XDG_RUNTIME_DIR" not in env:
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    if "DBUS_SESSION_BUS_ADDRESS" not in env and os.path.exists(f"/run/user/{os.getuid()}/bus"):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"

    cmd = [
        "google-chrome",
        f"--user-data-dir={user_data_dir}",
        f"--profile-directory={profile_dir}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "http://127.0.0.1:8002/"
    ]
    print(f"[Launcher] Launching Google Chrome profile '{profile_name}' ({profile_dir}) with remote debugging on port {port}...")
    try:
        subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setsid if hasattr(os, "setsid") else None)
        print("[Launcher] Chrome launched successfully!")
    except Exception as e:
        print(f"[Launcher] Error launching Chrome: {e}")


async def retry_pending_automation():
    """
    Orchestration entrypoint to apply to all Under Review, Failed, and Waiting jobs.
    """
    profile = load_json("config/candidate_profile.json")
    settings = load_json("config/settings.json")
    browser_cfg = settings.get("browser", {})

    cdp = ChromeCDPClient(
        port=browser_cfg.get("remote_debugging_port", 9222),
        user_data_dir=browser_cfg.get("user_data_dir", "~/.config/google-chrome-automation"),
        profile_directory=browser_cfg.get("profile_directory", "Profile 4"),
        profile_name=browser_cfg.get("profile_name", "Sakshi-Automation")
    )

    connected = await cdp.connect(target_url_match="shine.com", create_new=False)
    if not connected:
        print(f"[Agent] ❌ Could not connect to Chrome debugging port 9222.")
        print("[Agent] Please launch Chrome with remote debugging:\n  python3 main.py launch-browser\n")
        return

    scorer = JobScorer(profile, settings)
    tracker = ApplicationTracker()
    reporter = RunReporter()

    try:
        shine = ShineAutomation(cdp, scorer, tracker, profile, settings)
        res = await shine.apply_to_pending_jobs()

        SystemNotifier.notify_run_completed(
            submitted=res.get("applications_submitted", 0),
            review=res.get("jobs_under_review", 0),
            total=res.get("jobs_evaluated", 0)
        )

        report = reporter.generate_report([res])
        print(report)
    finally:
        await cdp.close()


def main():
    parser = argparse.ArgumentParser(description="Shine Job Application Automation Agent")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # run command
    run_parser = subparsers.add_parser("run", help="Run job discovery and application loop on Shine.com")
    run_parser.add_argument("--source", default="shine", help="Job portal source (default: shine)")
    run_parser.add_argument("--keywords", nargs="+", help="Custom search keywords")
    run_parser.add_argument("--limit", type=int, default=None, help="Maximum number of applications to submit in this run")
    run_parser.add_argument("--test", action="store_true", help="Test mode: send application to 1 company only for verification")

    # retry-pending command
    subparsers.add_parser("retry-pending", aliases=["apply-pending", "apply-review"], help="Apply to all Under Review, Failed, and Waiting jobs")

    # status command
    subparsers.add_parser("status", help="Show application tracker statistics")

    # review command
    subparsers.add_parser("review", help="Inspect jobs currently in review queue")

    # test-score command
    subparsers.add_parser("test-score", help="Test scoring engine with sample job")

    # launch-browser command
    subparsers.add_parser("launch-browser", help="Launch Chrome with Automation profile and debugging port")

    # ui / dashboard command
    ui_parser = subparsers.add_parser("ui", aliases=["serve", "dashboard"], help="Launch Genesis Web Dashboard UI")
    ui_parser.add_argument("--port", type=int, default=8002, help="Port to run dashboard on (default: 8002)")
    ui_parser.add_argument("--host", default="127.0.0.1", help="Host address (default: 127.0.0.1)")

    args = parser.parse_args()

    if args.command == "run":
        asyncio.run(run_automation(source="shine", keywords=args.keywords, limit=args.limit, test_mode=args.test))
    elif args.command in ["retry-pending", "apply-pending", "apply-review"]:
        asyncio.run(retry_pending_automation())
    elif args.command in ["ui", "serve", "dashboard"]:
        from src.web_server import run_dashboard
        run_dashboard(host=args.host, port=args.port)
    elif args.command == "status":
        show_status()
    elif args.command == "review":
        review_queue()
    elif args.command == "test-score":
        test_score()
    elif args.command == "launch-browser":
        launch_browser()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
