import os
import json
import asyncio
import logging
import subprocess
from aiohttp import web
from typing import Dict, Any, List, Optional
from datetime import datetime

from src.tracker import ApplicationTracker
from src.notifier import SystemNotifier
from src.scorer import JobScorer
from src.cdp_client import ChromeCDPClient
from src.platforms.shine import ShineAutomation
from src.reporter import RunReporter

# Global state for server
active_task: Optional[asyncio.Task] = None
log_subscribers: List[asyncio.Queue] = []
recent_logs: List[str] = []
MAX_SAVED_LOGS = 400


def broadcast_log(message: str):
    """
    Sends log message to all connected SSE clients and caches in memory.
    """
    clean_msg = message.strip()
    if not clean_msg:
        return
    
    recent_logs.append(clean_msg)
    while len(recent_logs) > MAX_SAVED_LOGS:
        recent_logs.pop(0)

    for q in list(log_subscribers):
        try:
            q.put_nowait(clean_msg)
        except (asyncio.QueueFull, Exception):
            pass


class CustomStdoutLogger:
    """
    Intercepts stdout to stream automation logs directly into the web dashboard.
    """
    def __init__(self, original_stdout):
        self.original_stdout = original_stdout

    def write(self, text):
        self.original_stdout.write(text)
        if text.strip():
            broadcast_log(text.strip())

    def flush(self):
        self.original_stdout.flush()


async def serve_index(request: web.Request) -> web.Response:
    html_path = os.path.join(os.path.dirname(__file__), "web", "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")
    return web.Response(text="Genesis Dashboard index.html not found.", status=404)


async def api_get_stats(request: web.Request) -> web.Response:
    tracker = ApplicationTracker()
    records = list(tracker.records.values())

    total = len(records)
    submitted = sum(1 for r in records if r.get("status") == "Submitted")
    review = sum(1 for r in records if r.get("status") == "Under Review")
    waiting = sum(1 for r in records if r.get("status") == "Waiting for Input")
    failed = sum(1 for r in records if r.get("status") in ["Failed", "Applying"])
    skipped = sum(1 for r in records if r.get("status") in ["Skipped", "Closed", "Not Accepting", "Already Applied"])
    pending_total = review + waiting + failed

    scores = [float(r.get("match_score", 0)) for r in records if r.get("match_score") is not None]
    avg_score = (sum(scores) / len(scores)) if scores else 0.0

    return web.json_response({
        "total_evaluated": total,
        "submitted": submitted,
        "under_review": review,
        "waiting_for_input": waiting,
        "failed": failed,
        "pending_total": pending_total,
        "skipped": skipped,
        "average_score": round(avg_score, 1),
        "is_running": bool(active_task and not active_task.done())
    })


async def api_get_applications(request: web.Request) -> web.Response:
    tracker = ApplicationTracker()
    records = list(tracker.records.values())
    # Sort descending by date
    records.sort(key=lambda x: x.get("date", ""), reverse=True)

    # Optional query filters
    status_filter = request.query.get("status")
    if status_filter and status_filter != "all":
        if status_filter == "Skipped":
            records = [r for r in records if r.get("status") in ["Skipped", "Closed", "Not Accepting"]]
        else:
            records = [r for r in records if r.get("status") == status_filter]

    search_query = request.query.get("search", "").strip().lower()
    if search_query:
        records = [
            r for r in records
            if search_query in (r.get("company") or "").lower()
            or search_query in (r.get("role") or "").lower()
            or search_query in (r.get("notes") or "").lower()
        ]

    return web.json_response(records)


async def api_stream_logs(request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*'
        }
    )
    await response.prepare(request)

    q = asyncio.Queue(maxsize=150)
    log_subscribers.append(q)

    try:
        # Replay recent logs
        for log in recent_logs[-40:]:
            await response.write(f"data: {log}\n\n".encode('utf-8'))

        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=20.0)
                await response.write(f"data: {msg}\n\n".encode('utf-8'))
            except asyncio.TimeoutError:
                # Keep-alive heartbeat
                await response.write(b": ping\n\n")
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        if q in log_subscribers:
            log_subscribers.remove(q)

    return response


async def run_automation_task(limit: Optional[int] = None, keywords: Optional[List[str]] = None):
    """
    Background worker that runs the Shine.com discovery & application loop.
    """
    import sys
    orig_stdout = sys.stdout
    sys.stdout = CustomStdoutLogger(orig_stdout)

    try:
        with open("config/candidate_profile.json", "r", encoding="utf-8") as f:
            profile = json.load(f)
        with open("config/settings.json", "r", encoding="utf-8") as f:
            settings = json.load(f)

        browser_cfg = settings.get("browser", {})
        cdp = ChromeCDPClient(
            port=browser_cfg.get("remote_debugging_port", 9222),
            user_data_dir=browser_cfg.get("user_data_dir", "~/.config/google-chrome-automation"),
            profile_directory=browser_cfg.get("profile_directory", "Profile 4"),
            profile_name=browser_cfg.get("profile_name", "Sakshi-Automation")
        )

        connected = await cdp.connect(target_url_match="shine.com", create_new=False)
        if not connected:
            print("[Agent] ❌ Could not connect to Chrome debugging port 9222.")
            return

        scorer = JobScorer(profile, settings)
        tracker = ApplicationTracker()
        reporter = RunReporter()
        run_results = []

        print("\n[Agent] 🚀 Starting Autonomous Shine Automation Workflow...")
        shine = ShineAutomation(cdp, scorer, tracker, profile, settings)
        res = await shine.search_and_process_jobs(keywords=keywords, limit=limit)
        run_results.append(res)

        total_sub = sum(r.get("applications_submitted", 0) for r in run_results)
        total_rev = sum(r.get("jobs_under_review", 0) for r in run_results)
        total_eval = sum(r.get("jobs_evaluated", 0) for r in run_results)
        SystemNotifier.notify_run_completed(submitted=total_sub, review=total_rev, total=total_eval)

        # Output final report
        report = reporter.generate_report(run_results)
        print(report)

        await cdp.close()

    except asyncio.CancelledError:
        print("\n[Agent] 🛑 Automation run was cancelled.")
        SystemNotifier.notify_run_stopped("Run cancelled from Genesis Dashboard.")
    except Exception as e:
        print(f"[Agent] Unexpected error: {e}")
        SystemNotifier.notify_run_failed(str(e))
    finally:
        sys.stdout = orig_stdout


async def run_retry_pending_task():
    """
    Background worker that applies to all Under Review, Failed, and Waiting jobs in the tracker.
    """
    import sys
    orig_stdout = sys.stdout
    sys.stdout = CustomStdoutLogger(orig_stdout)

    try:
        with open("config/candidate_profile.json", "r", encoding="utf-8") as f:
            profile = json.load(f)
        with open("config/settings.json", "r", encoding="utf-8") as f:
            settings = json.load(f)

        browser_cfg = settings.get("browser", {})
        cdp = ChromeCDPClient(
            port=browser_cfg.get("remote_debugging_port", 9222),
            user_data_dir=browser_cfg.get("user_data_dir", "~/.config/google-chrome-automation"),
            profile_directory=browser_cfg.get("profile_directory", "Profile 4"),
            profile_name=browser_cfg.get("profile_name", "Sakshi-Automation")
        )

        connected = await cdp.connect(target_url_match="shine.com", create_new=False)
        if not connected:
            print("[Agent] ❌ Could not connect to Chrome debugging port 9222.")
            return

        scorer = JobScorer(profile, settings)
        tracker = ApplicationTracker()
        reporter = RunReporter()

        print("\n[Agent] 🔄 Starting Retry Loop for Under Review & Failed Jobs...")
        shine = ShineAutomation(cdp, scorer, tracker, profile, settings)
        res = await shine.apply_to_pending_jobs()

        SystemNotifier.notify_run_completed(
            submitted=res.get("applications_submitted", 0),
            review=res.get("jobs_under_review", 0),
            total=res.get("jobs_evaluated", 0)
        )

        report = reporter.generate_report([res])
        print(report)

        await cdp.close()

    except asyncio.CancelledError:
        print("\n[Agent] 🛑 Retry task was cancelled.")
        SystemNotifier.notify_run_stopped("Retry task cancelled from Genesis Dashboard.")
    except Exception as e:
        print(f"[Agent] Unexpected error during retry: {e}")
        SystemNotifier.notify_run_failed(str(e))
    finally:
        sys.stdout = orig_stdout


async def api_start_run(request: web.Request) -> web.Response:
    global active_task
    if active_task and not active_task.done():
        return web.json_response({"status": "already_running", "message": "Automation is currently in progress."})

    active_task = asyncio.create_task(run_automation_task())
    return web.json_response({"status": "started", "message": "Shine automation workflow started."})


async def api_retry_pending(request: web.Request) -> web.Response:
    global active_task
    if active_task and not active_task.done():
        return web.json_response({"status": "already_running", "message": "Another automation task is currently in progress."})

    active_task = asyncio.create_task(run_retry_pending_task())
    return web.json_response({"status": "started", "message": "Applying to Under Review & Failed jobs."})


async def api_stop_run(request: web.Request) -> web.Response:
    global active_task
    if active_task and not active_task.done():
        active_task.cancel()
        return web.json_response({"status": "stopped", "message": "Automation task cancellation requested."})
    return web.json_response({"status": "idle", "message": "No active automation task is running."})


async def api_launch_browser(request: web.Request) -> web.Response:
    try:
        with open("config/settings.json", "r", encoding="utf-8") as f:
            settings = json.load(f)
        browser_cfg = settings.get("browser", {})
        port = browser_cfg.get("remote_debugging_port", 9222)
        user_data_dir = os.path.abspath(os.path.expanduser(browser_cfg.get("user_data_dir", "~/.config/google-chrome-automation")))
        profile_name = browser_cfg.get("profile_name", "Sakshi-Automation")
        profile_dir = browser_cfg.get("profile_directory", "Profile 4")

        os.makedirs(user_data_dir, exist_ok=True)

        env = os.environ.copy()
        if "DISPLAY" not in env:
            env["DISPLAY"] = ":0"

        cmd = [
            "google-chrome",
            f"--user-data-dir={user_data_dir}",
            f"--profile-directory={profile_dir}",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            f"http://127.0.0.1:{request.app.get('port', 8002)}/"
        ]

        subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, preexec_fn=os.setsid if hasattr(os, "setsid") else None)
        return web.json_response({"status": "launched", "port": port, "profile": profile_name})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def run_apply_single_task(job_url: str):
    """
    Background worker to apply directly to a single job.
    """
    import sys
    orig_stdout = sys.stdout
    sys.stdout = CustomStdoutLogger(orig_stdout)

    try:
        with open("config/candidate_profile.json", "r", encoding="utf-8") as f:
            profile = json.load(f)
        with open("config/settings.json", "r", encoding="utf-8") as f:
            settings = json.load(f)

        browser_cfg = settings.get("browser", {})
        cdp = ChromeCDPClient(
            port=browser_cfg.get("remote_debugging_port", 9222),
            user_data_dir=browser_cfg.get("user_data_dir", "~/.config/google-chrome-automation"),
            profile_directory=browser_cfg.get("profile_directory", "Profile 4"),
            profile_name=browser_cfg.get("profile_name", "Sakshi-Automation")
        )

        connected = await cdp.connect(target_url_match="shine.com", create_new=False)
        if not connected:
            print("[Agent] ❌ Could not connect to Chrome debugging port 9222.")
            return

        scorer = JobScorer(profile, settings)
        tracker = ApplicationTracker()
        shine = ShineAutomation(cdp, scorer, tracker, profile, settings)

        record = tracker.records.get(job_url) or next((r for r in tracker.records.values() if r.get("job_url") == job_url), {})
        comp = record.get("company", "Unknown")
        title = record.get("role", "Software Engineer")
        print(f"\n[Agent] ⚡ Manually applying to: {comp} - {title} ({job_url})")

        full_job = await shine._fetch_full_job_details(job_url)
        if not full_job:
            full_job = {
                "title": title,
                "company": comp,
                "job_url": job_url,
                "job_id": record.get("job_id", ""),
                "description": "",
                "skills": [],
                "location": "",
                "source": "Shine",
                "direct_apply": True
            }

        score_res = scorer.evaluate(full_job)
        apply_res = await shine._apply_to_job(full_job, score_res)
        final_status = apply_res.get("status", "Failed")

        if final_status == "Submitted":
            print(f"[Agent] ✅ Application successfully submitted for {title} at {comp}!")
            SystemNotifier.notify_run_completed(submitted=1, review=0, total=1)
        else:
            print(f"[Agent] ℹ️ Application finished with status: {final_status}")

        await cdp.close()

    except Exception as e:
        print(f"[Agent] Error during single application: {e}")
        SystemNotifier.notify_run_failed(str(e))
    finally:
        sys.stdout = orig_stdout


async def api_apply_single(request: web.Request) -> web.Response:
    global active_task
    if active_task and not active_task.done():
        return web.json_response({"status": "already_running", "message": "Another automation task is currently in progress."})

    try:
        data = await request.json()
        job_url = data.get("job_url")
        if not job_url:
            return web.json_response({"status": "error", "message": "Missing job_url parameter"}, status=400)

        active_task = asyncio.create_task(run_apply_single_task(job_url))
        return web.json_response({"status": "started", "job_url": job_url})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500)


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", serve_index)
    app.router.add_get("/api/stats", api_get_stats)
    app.router.add_get("/api/applications", api_get_applications)
    app.router.add_get("/api/logs/stream", api_stream_logs)
    app.router.add_post("/api/run/start", api_start_run)
    app.router.add_post("/api/run/retry-pending", api_retry_pending)
    app.router.add_post("/api/applications/apply-single", api_apply_single)
    app.router.add_post("/api/run/stop", api_stop_run)
    app.router.add_post("/api/browser/launch", api_launch_browser)

    static_path = os.path.join(os.path.dirname(__file__), "web")
    app.router.add_static("/static/", static_path, show_index=False)
    return app


def run_dashboard(host: str = "127.0.0.1", port: int = 8002):
    """
    Launches Genesis Dashboard web server.
    """
    app = create_app()
    app["port"] = port
    print(f"\n=======================================================")
    print(f"✨ GENESIS DASHBOARD - SHINE.COM AUTOMATION")
    print(f"🌐 Server URL: http://{host}:{port}/")
    print(f"=======================================================\n")
    web.run_app(app, host=host, port=port)
