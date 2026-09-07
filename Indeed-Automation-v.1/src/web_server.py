import os
import json
import asyncio
import logging
from aiohttp import web
from typing import Dict, Any, List, Optional
from datetime import datetime

from src.tracker import ApplicationTracker

active_task: Optional[asyncio.Task] = None
log_subscribers: List[asyncio.Queue] = []
recent_logs: List[str] = []
MAX_SAVED_LOGS = 100


def broadcast_log(message: str):
    """
    Sends log message to all connected SSE clients and caches in memory.
    """
    clean_msg = message.strip()
    if not clean_msg:
        return
    
    recent_logs.append(clean_msg)
    if len(recent_logs) > MAX_SAVED_LOGS:
        recent_logs.pop(0)

    for q in list(log_subscribers):
        try:
            q.put_nowait(clean_msg)
        except Exception:
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
    skipped = sum(1 for r in records if r.get("status") in ["Skipped", "Closed"])

    scores = [float(r.get("match_score", 0)) for r in records if r.get("match_score") is not None]
    avg_score = (sum(scores) / len(scores)) if scores else 0.0

    return web.json_response({
        "total_evaluated": total,
        "submitted": submitted,
        "under_review": review,
        "waiting_for_input": waiting,
        "skipped": skipped,
        "average_score": round(avg_score, 1)
    })


async def api_get_applications(request: web.Request) -> web.Response:
    tracker = ApplicationTracker()
    records = list(tracker.records.values())
    records.sort(key=lambda x: x.get("date", ""), reverse=True)
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
        broadcast_log(f"[Genesis] 🚀 Launching Indeed application engine (Test Mode: {test_mode}, Limit: {limit})...")
        await run_automation(source="indeed", keywords=keywords, limit=limit, test_mode=test_mode)
        broadcast_log("[Genesis] 🏁 Automation execution finished.")
    except asyncio.CancelledError:
        broadcast_log("[Genesis] 🛑 Automation task was cancelled by user.")
    except Exception as e:
        broadcast_log(f"[Genesis] ❌ Error during execution: {e}")
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

    source = data.get("source", "indeed")
    test_mode = bool(data.get("test_mode", False))
    limit = data.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
        except Exception:
            limit = None
    keywords = data.get("keywords")

    active_task = asyncio.create_task(_run_automation_task(source, test_mode, limit, keywords))
    return web.json_response({"status": "started", "message": "Indeed automation launched."})


async def api_stop_automation(request: web.Request) -> web.Response:
    global active_task
    if active_task and not active_task.done():
        active_task.cancel()
        broadcast_log("[Genesis] 🛑 Cancelling automation execution...")
        return web.json_response({"status": "stopped", "message": "Stop signal sent to automation."})
    return web.json_response({"status": "idle", "message": "No automation task currently active."})


async def api_launch_browser(request: web.Request) -> web.Response:
    from main import launch_browser
    try:
        launch_browser()
        broadcast_log("[Genesis] 🌐 Chrome Automation Browser launched.")
        return web.json_response({"status": "success", "message": "Chrome launched."})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def api_update_record(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON"}, status=400)

    key = data.get("key")
    new_status = data.get("status")
    notes = data.get("notes")

    if not key or not new_status:
        return web.json_response({"status": "error", "message": "Missing key or status"}, status=400)

    tracker = ApplicationTracker()
    try:
        tracker.update_status(key, new_status, notes=notes)
        broadcast_log(f"[Genesis] ✏️ Updated '{key}' to status '{new_status}'")
        return web.json_response({"status": "success", "message": "Record updated."})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def sse_logs(request: web.Request) -> web.StreamResponse:
    """
    Server-Sent Events endpoint for real-time console log streaming.
    """
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

    q = asyncio.Queue()
    log_subscribers.append(q)

    try:
        # Send initial backlog of logs
        for log in recent_logs[-50:]:
            await response.write(f"data: {json.dumps({'log': log})}\n\n".encode('utf-8'))

        while True:
            msg = await q.get()
            await response.write(f"data: {json.dumps({'log': msg})}\n\n".encode('utf-8'))
    except asyncio.CancelledError:
        pass
    finally:
        if q in log_subscribers:
            log_subscribers.remove(q)

    return response


async def api_get_questions(request: web.Request) -> web.Response:
    """
    Returns unresolved employer questions and saved custom questions.
    """
    uq_path = "data/unresolved_questions.json"
    unresolved = []
    if os.path.exists(uq_path):
        try:
            with open(uq_path, "r", encoding="utf-8") as f:
                unresolved = json.load(f).get("unresolved", [])
        except Exception:
            pass

    prof_path = "config/candidate_profile.json"
    custom_qa = {}
    if os.path.exists(prof_path):
        try:
            with open(prof_path, "r", encoding="utf-8") as f:
                custom_qa = json.load(f).get("custom_question_answers", {})
        except Exception:
            pass

    return web.json_response({
        "unresolved": unresolved,
        "custom_answers": custom_qa
    })


async def api_save_question_answer(request: web.Request) -> web.Response:
    """
    Saves a user answer for a question into candidate_profile.json and clears it from unresolved questions.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON"}, status=400)

    question = (data.get("question") or "").strip()
    answer = (data.get("answer") or "").strip()
    clean_key = re.sub(r'[*:\n\r]+', ' ', question).strip().lower()

    if not question or not answer:
        return web.json_response({"status": "error", "message": "Question and answer are required."}, status=400)

    # 1. Update candidate_profile.json
    prof_path = "config/candidate_profile.json"
    try:
        prof = {}
        if os.path.exists(prof_path):
            with open(prof_path, "r", encoding="utf-8") as f:
                prof = json.load(f)

        if "custom_question_answers" not in prof:
            prof["custom_question_answers"] = {}

        prof["custom_question_answers"][clean_key] = answer
        with open(prof_path, "w", encoding="utf-8") as f:
            json.dump(prof, f, indent=2, ensure_ascii=False)
    except Exception as e:
        return web.json_response({"status": "error", "message": f"Failed to update profile: {e}"}, status=500)

    # 2. Remove from unresolved_questions.json
    uq_path = "data/unresolved_questions.json"
    if os.path.exists(uq_path):
        try:
            with open(uq_path, "r", encoding="utf-8") as f:
                uq_data = json.load(f)
            remaining = [
                u for u in uq_data.get("unresolved", [])
                if re.sub(r'[*:\n\r]+', ' ', u.get("question", "")).strip().lower() != clean_key
            ]
            uq_data["unresolved"] = remaining
            with open(uq_path, "w", encoding="utf-8") as f:
                json.dump(uq_data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    broadcast_log(f"[Genesis] 💡 Learned new answer for: '{question}' -> '{answer}'")
    return web.json_response({"status": "success", "message": f"Learned answer for '{question}'"})


async def api_delete_question(request: web.Request) -> web.Response:
    """
    Deletes an item from unresolved questions or custom learned answers.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON"}, status=400)

    q_id = data.get("id")
    pattern = data.get("pattern")

    if q_id:
        uq_path = "data/unresolved_questions.json"
        if os.path.exists(uq_path):
            try:
                with open(uq_path, "r", encoding="utf-8") as f:
                    uq_data = json.load(f)
                uq_data["unresolved"] = [u for u in uq_data.get("unresolved", []) if u.get("id") != q_id]
                with open(uq_path, "w", encoding="utf-8") as f:
                    json.dump(uq_data, f, indent=2, ensure_ascii=False)
            except Exception:
                pass

    if pattern:
        prof_path = "config/candidate_profile.json"
        if os.path.exists(prof_path):
            try:
                with open(prof_path, "r", encoding="utf-8") as f:
                    prof = json.load(f)
                if "custom_question_answers" in prof and pattern in prof["custom_question_answers"]:
                    del prof["custom_question_answers"][pattern]
                    with open(prof_path, "w", encoding="utf-8") as f:
                        json.dump(prof, f, indent=2, ensure_ascii=False)
            except Exception:
                pass

    return web.json_response({"status": "success", "message": "Item deleted."})


def create_app() -> web.Application:
    app = web.Application()

    app.router.add_get("/", serve_index)
    app.router.add_get("/api/stats", api_get_stats)
    app.router.add_get("/api/applications", api_get_applications)
    app.router.add_get("/api/status", api_get_status)
    app.router.add_get("/api/questions", api_get_questions)
    app.router.add_post("/api/save_question_answer", api_save_question_answer)
    app.router.add_post("/api/delete_question", api_delete_question)
    app.router.add_post("/api/run", api_run_automation)
    app.router.add_post("/api/stop", api_stop_automation)
    app.router.add_post("/api/launch-browser", api_launch_browser)
    app.router.add_post("/api/update-record", api_update_record)
    app.router.add_get("/api/logs", sse_logs)

    # Static assets
    static_dir = os.path.join(os.path.dirname(__file__), "web")
    app.router.add_static("/static/", static_dir)

    return app


def run_dashboard(host: str = "127.0.0.1", port: int = 8002):
    tracker = ApplicationTracker()
    tracker.cleanup_stale_applying_records()
    app = create_app()
    print(f"\n=======================================================")
    print(f"🚀 GENESIS DASHBOARD RUNNING (INDEED AUTOMATION)")
    print(f"🌐 Access Web UI: http://{host}:{port}/")
    print(f"=======================================================\n")
    web.run_app(app, host=host, port=port, print=None)
