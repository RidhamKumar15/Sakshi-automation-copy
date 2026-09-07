import os
import json
import asyncio
import logging
from aiohttp import web
from typing import Dict, Any, List, Optional
from datetime import datetime

from src.tracker import ApplicationTracker
from src.notifier import SystemNotifier

# Global state for server
active_task: Optional[asyncio.Task] = None
log_subscribers: List[asyncio.Queue] = []
recent_logs: List[str] = []
MAX_SAVED_LOGS = 300


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


class SSELogHandler(logging.Handler):
    """
    Captures Python logging output and streams to web clients.
    """
    def emit(self, record):
        try:
            msg = self.format(record)
            broadcast_log(msg)
        except Exception:
            pass


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
    not_accepting = sum(1 for r in records if r.get("status") == "Not Accepting")
    skipped = sum(1 for r in records if r.get("status") in ["Skipped", "Closed", "Not Accepting"])

    scores = [float(r.get("match_score", 0)) for r in records if r.get("match_score") is not None]
    avg_score = (sum(scores) / len(scores)) if scores else 0.0

    return web.json_response({
        "total_evaluated": total,
        "submitted": submitted,
        "under_review": review,
        "waiting_for_input": waiting,
        "not_accepting": not_accepting,
        "skipped": skipped,
        "average_score": round(avg_score, 1)
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
        elif status_filter == "Not Accepting":
            records = [r for r in records if r.get("status") == "Not Accepting"]
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

    page_param = request.query.get("page")
    if page_param is not None:
        try:
            page = max(1, int(page_param))
            limit = max(1, int(request.query.get("limit") or 25))
            start_idx = (page - 1) * limit
            end_idx = start_idx + limit
            paged_records = records[start_idx:end_idx]
            return web.json_response({
                "records": paged_records,
                "total": len(records),
                "page": page,
                "limit": limit,
                "total_pages": max(1, (len(records) + limit - 1) // limit)
            })
        except Exception:
            pass

    return web.json_response(records)


async def api_get_status(request: web.Request) -> web.Response:
    global active_task
    is_running = active_task is not None and not active_task.done()
    return web.json_response({
        "is_running": is_running
    })


async def _run_automation_task(source: str, test_mode: bool, limit: Optional[int], keywords: Optional[List[str]]):
    from main import run_automation
    import sys

    # Intercept print statements to stream to SSE
    class StdoutStreamer:
        def __init__(self, original_stdout):
            self.original = original_stdout

        def write(self, s):
            self.original.write(s)
            self.original.flush()
            if s.strip():
                broadcast_log(s.strip())

        def flush(self):
            self.original.flush()

    old_stdout = sys.stdout
    sys.stdout = StdoutStreamer(old_stdout)

    try:
        broadcast_log(f"[Genesis] 🚀 Launching application engine (Source: {source}, Test Mode: {test_mode}, Limit: {limit})...")
        await run_automation(source=source, keywords=keywords, limit=limit, test_mode=test_mode)
        broadcast_log("[Genesis] 🏁 Automation execution finished successfully.")
    except asyncio.CancelledError:
        broadcast_log("[Genesis] 🛑 Automation task was cancelled by user.")
        SystemNotifier.notify_run_stopped("Automation process cancelled by user.")
    except Exception as e:
        broadcast_log(f"[Genesis] ❌ Error during execution: {e}")
        SystemNotifier.notify_run_failed(str(e))
    finally:
        sys.stdout = old_stdout


async def _run_apply_waiting_task(limit: Optional[int] = None, single_url: Optional[str] = None):
    from main import load_json
    from src.cdp_client import ChromeCDPClient
    from src.scorer import JobScorer
    from src.tracker import ApplicationTracker
    from src.platforms.wellfound import WellfoundAutomation
    import sys

    # Intercept print statements to stream to SSE
    class StdoutStreamer:
        def __init__(self, original_stdout):
            self.original = original_stdout

        def write(self, s):
            self.original.write(s)
            self.original.flush()
            if s.strip():
                broadcast_log(s.strip())

        def flush(self):
            self.original.flush()

    old_stdout = sys.stdout
    sys.stdout = StdoutStreamer(old_stdout)

    try:
        profile = load_json("config/candidate_profile.json")
        settings = load_json("config/settings.json")
        browser_cfg = settings.get("browser", {})

        cdp = ChromeCDPClient(
            port=browser_cfg.get("remote_debugging_port", 9222),
            user_data_dir=browser_cfg.get("user_data_dir", "/home/ridhamverma/.config/google-chrome-automation"),
            profile_directory=browser_cfg.get("profile_directory", "Profile 4"),
            profile_name=browser_cfg.get("profile_name", "Sakshi-Automation")
        )

        connected = await cdp.connect()
        if not connected:
            broadcast_log("[Genesis] ❌ Could not connect to Chrome on port 9222. Please launch Chrome.")
            SystemNotifier.notify_run_failed("Could not connect to Chrome on port 9222.")
            return

        scorer = JobScorer(profile, settings)
        tracker = ApplicationTracker()
        wf = WellfoundAutomation(cdp, scorer, tracker, profile, settings)

        if single_url:
            broadcast_log(f"[Genesis] 🚀 Direct applying to: {single_url}...")
            job_rec = None
            for r in tracker.records.values():
                if r.get("job_url") == single_url:
                    job_rec = r
                    break
            
            job_comp = job_rec.get("company", "Hiring Startup") if job_rec else "Hiring Startup"
            job_title = job_rec.get("role", "Software Engineer") if job_rec else "Software Engineer"
            job_dict = {
                "title": job_title,
                "company": job_comp,
                "job_url": single_url,
                "location": "India",
                "description": job_rec.get("role", "") if job_rec else ""
            }
            score_res = {
                "total_score": float(job_rec.get("match_score", 90.0)) if job_rec else 90.0,
                "decision": "AUTO_APPLY",
                "status": "Applying",
                "breakdown": {}
            }
            res = await wf._apply_to_job(job_dict, score_res)
            st = res.get("status", "Submitted")
            notes = res.get("notes", "Applied via Genesis Direct Apply")
            tracker.update_status(single_url, st, notes=notes)
            broadcast_log(f"[Genesis] 🏁 Direct apply result: {st} ({notes})")

            if st == "Submitted":
                SystemNotifier.notify_job_submitted(job_dict.get("company", job_comp), job_dict.get("title", job_title))
            elif st == "Not Accepting":
                broadcast_log(f"[Genesis] 🚫 Application not accepted by {job_comp}: {notes}")
            else:
                SystemNotifier.notify_action_required(job_dict.get("company", job_comp), job_dict.get("title", job_title), notes)
        else:
            broadcast_log(f"[Genesis] ⚡ Starting Batch Apply for all Waiting jobs (Limit: {limit or 'All'})...")
            res = await wf.apply_to_waiting_jobs(limit=limit)
            sub_cnt = res.get("submitted", 0)
            fail_cnt = res.get("failed", 0)
            broadcast_log(f"[Genesis] 🏁 Batch apply finished! Submitted: {sub_cnt}, Failed: {fail_cnt}")
            SystemNotifier.notify(
                "⚡ Genesis Batch Apply Complete",
                f"Batch completed: {sub_cnt} submitted, {fail_cnt} review/failed.",
                sound_type="complete" if sub_cnt > 0 else "warning"
            )

        await cdp.close()
    except asyncio.CancelledError:
        broadcast_log("[Genesis] 🛑 Application task was cancelled by user.")
        SystemNotifier.notify_run_stopped("Batch apply was cancelled by user.")
    except Exception as e:
        broadcast_log(f"[Genesis] ❌ Error during batch application: {e}")
        SystemNotifier.notify_run_failed(str(e))
    finally:
        sys.stdout = old_stdout


async def api_run_automation(request: web.Request) -> web.Response:
    global active_task

    if active_task is not None and not active_task.done():
        return web.json_response({"status": "error", "message": "Automation is already running."}, status=400)

    try:
        data = await request.json()
    except Exception:
        data = {}

    source = data.get("source", "wellfound")
    test_mode = data.get("test_mode", True)
    limit = data.get("limit", 1 if test_mode else None)
    keywords = data.get("keywords") or None

    active_task = asyncio.create_task(_run_automation_task(source, test_mode, limit, keywords))
    return web.json_response({"status": "started", "source": source, "test_mode": test_mode, "limit": limit})


async def api_apply_waiting(request: web.Request) -> web.Response:
    global active_task

    if active_task is not None and not active_task.done():
        return web.json_response({"status": "error", "message": "Automation is already running."}, status=400)

    try:
        data = await request.json()
    except Exception:
        data = {}

    limit = data.get("limit") or None
    active_task = asyncio.create_task(_run_apply_waiting_task(limit=limit))
    return web.json_response({"status": "started", "mode": "batch_apply_waiting", "limit": limit})


async def api_apply_single(request: web.Request) -> web.Response:
    global active_task

    if active_task is not None and not active_task.done():
        return web.json_response({"status": "error", "message": "Automation is already running."}, status=400)

    try:
        data = await request.json()
    except Exception:
        data = {}

    url = data.get("job_url")
    if not url:
        return web.json_response({"status": "error", "message": "Missing job_url parameter."}, status=400)

    active_task = asyncio.create_task(_run_apply_waiting_task(single_url=url))
    return web.json_response({"status": "started", "job_url": url})


async def api_stop_automation(request: web.Request) -> web.Response:
    global active_task
    if active_task and not active_task.done():
        active_task.cancel()
        active_task = None
        broadcast_log("[Genesis] Stop signal dispatched.")
        return web.json_response({"status": "stopped"})
    return web.json_response({"status": "idle", "message": "No active automation process."})


async def sse_logs_stream(request: web.Request) -> web.StreamResponse:
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

    queue = asyncio.Queue(maxsize=500)
    log_subscribers.append(queue)

    try:
        # Send recent history
        for msg in recent_logs[-30:]:
            await response.write(f"data: {msg}\n\n".encode("utf-8"))

        while True:
            msg = await queue.get()
            await response.write(f"data: {msg}\n\n".encode("utf-8"))
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        if queue in log_subscribers:
            log_subscribers.remove(queue)

    return response


def create_app() -> web.Application:
    app = web.Application()

    static_dir = os.path.join(os.path.dirname(__file__), "web")

    app.router.add_get("/", serve_index)
    app.router.add_get("/api/stats", api_get_stats)
    app.router.add_get("/api/applications", api_get_applications)
    app.router.add_get("/api/status", api_get_status)
    app.router.add_post("/api/run", api_run_automation)
    app.router.add_post("/api/apply-waiting", api_apply_waiting)
    app.router.add_post("/api/apply-single", api_apply_single)
    app.router.add_post("/api/stop", api_stop_automation)
    app.router.add_get("/api/logs/stream", sse_logs_stream)

    app.router.add_static("/static", path=static_dir, name="static")

    return app


def run_dashboard(host: str = "127.0.0.1", port: int = 8000):
    print(f"\n==============================================================")
    print(f"✨ GENESIS UI DASHBOARD RUNNING")
    print(f"🌐 Dashboard URL: http://{host}:{port}")
    print(f"==============================================================\n")
    app = create_app()
    web.run_app(app, host=host, port=port, print=None)


if __name__ == "__main__":
    run_dashboard()
