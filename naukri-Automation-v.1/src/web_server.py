import os
import json
import asyncio
import logging
from aiohttp import web
from typing import Dict, Any, List, Optional
from datetime import datetime

from src.tracker import ApplicationTracker

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
        broadcast_log(f"[Genesis] 🚀 Launching Naukri application engine (Test Mode: {test_mode}, Limit: {limit})...")
        await run_automation(source="naukri", keywords=keywords, limit=limit, test_mode=test_mode)
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

    source = data.get("source", "naukri")
    test_mode = bool(data.get("test_mode", False))
    limit = data.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
        except Exception:
            limit = None
    keywords = data.get("keywords")

    active_task = asyncio.create_task(_run_automation_task(source, test_mode, limit, keywords))
    return web.json_response({"status": "started", "message": "Naukri automation launched."})


async def api_stop_automation(request: web.Request) -> web.Response:
    global active_task
    if active_task and not active_task.done():
        active_task.cancel()
        from src.notifier import notify_stopped
        notify_stopped("Automation task was stopped from Web Dashboard.")
        return web.json_response({"status": "stopping", "message": "Stop signal sent."})
    return web.json_response({"status": "idle", "message": "No active automation."})


async def api_test_notification(request: web.Request) -> web.Response:
    from src.notifier import notify_completion, notify_attention_required, notify_error
    try:
        data = await request.json()
    except Exception:
        data = {}
    ntype = data.get("type", "success")
    if ntype == "warning":
        notify_attention_required("Test Alert: CAPTCHA / Human Action Needed")
    elif ntype == "error":
        notify_error("Test Alert: Automation Failed / Stopped")
    else:
        notify_completion(submitted_count=1, total_evaluated=3)
    return web.json_response({"status": "ok", "message": f"Test {ntype} notification sent."})


async def api_sse_logs(request: web.Request) -> web.StreamResponse:
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

    # Send recent back-buffer logs on connect
    for line in recent_logs:
        payload = f"data: {line}\n\n"
        await response.write(payload.encode('utf-8'))

    queue = asyncio.Queue()
    log_subscribers.append(queue)

    try:
        while True:
            msg = await queue.get()
            payload = f"data: {msg}\n\n"
            await response.write(payload.encode('utf-8'))
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        if queue in log_subscribers:
            log_subscribers.remove(queue)

    return response


async def api_get_browser_status(request: web.Request) -> web.Response:
    from src.cdp_client import ChromeCDPClient
    settings_path = os.path.join(os.path.dirname(__file__), "..", "config", "settings.json")
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        browser_cfg = settings.get("browser", {})
    except Exception:
        browser_cfg = {}

    cdp = ChromeCDPClient(
        port=browser_cfg.get("remote_debugging_port", 9222),
        user_data_dir=browser_cfg.get("user_data_dir", "~/.config/google-chrome-automation"),
        profile_directory=browser_cfg.get("profile_directory", "Default"),
        profile_name=browser_cfg.get("profile_name", "Sakshi-Automation")
    )
    running = await cdp.is_chrome_running()
    return web.json_response({
        "connected": running,
        "port": cdp.port,
        "profile_name": cdp.profile_name,
        "profile_directory": cdp.profile_directory
    })


async def api_launch_browser(request: web.Request) -> web.Response:
    from main import launch_browser
    try:
        launch_browser()
        return web.json_response({"status": "ok", "message": "Chrome launched with remote debugging."})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=500)


async def api_get_unresolved_questions(request: web.Request) -> web.Response:
    unresolved_file = os.path.join(os.path.dirname(__file__), "..", "data", "unresolved_questions.json")
    if os.path.exists(unresolved_file):
        try:
            with open(unresolved_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return web.json_response(data.get("unresolved", []))
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    return web.json_response([])


async def api_resolve_unresolved_question(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON"}, status=400)

    q_id = str(body.get("id", "")).strip()
    question = str(body.get("question", "")).strip()
    answer = str(body.get("answer", "")).strip()

    if not question or not answer:
        return web.json_response({"status": "error", "message": "Question and answer are required."}, status=400)

    # 1. Update presets file
    presets_file = os.path.join(os.path.dirname(__file__), "..", "config", "question_presets.json")
    presets_data = {"core_presets": {}, "question_mappings": {}, "custom_resolved_questions": {}}
    if os.path.exists(presets_file):
        try:
            with open(presets_file, "r", encoding="utf-8") as f:
                presets_data = json.load(f)
        except Exception:
            pass

    if "custom_resolved_questions" not in presets_data:
        presets_data["custom_resolved_questions"] = {}
    if "question_mappings" not in presets_data:
        presets_data["question_mappings"] = {}

    presets_data["custom_resolved_questions"][question] = answer
    presets_data["question_mappings"][question.lower().strip()] = answer

    os.makedirs(os.path.dirname(presets_file), exist_ok=True)
    with open(presets_file, "w", encoding="utf-8") as f:
        json.dump(presets_data, f, indent=2, ensure_ascii=False)

    # 2. Remove question from unresolved_questions.json
    unresolved_file = os.path.join(os.path.dirname(__file__), "..", "data", "unresolved_questions.json")
    if os.path.exists(unresolved_file):
        try:
            with open(unresolved_file, "r", encoding="utf-8") as f:
                unresolved_data = json.load(f)
            
            unresolved_list = unresolved_data.get("unresolved", [])
            unresolved_list = [
                q for q in unresolved_list
                if (q_id and q.get("id") != q_id) and (q.get("question", "").strip().lower() != question.lower())
            ]
            unresolved_data["unresolved"] = unresolved_list
            with open(unresolved_file, "w", encoding="utf-8") as f:
                json.dump(unresolved_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[WebServer] Warning updating unresolved file: {e}")

    broadcast_log(f"[Genesis] 💾 Persisted preset answer for: \"{question}\" -> \"{answer}\"")
    return web.json_response({"status": "ok", "message": f"Saved answer for '{question}' to central presets."})


async def api_delete_unresolved_question(request: web.Request) -> web.Response:
    q_id = request.query.get("id")
    if not q_id:
        try:
            body = await request.json()
            q_id = body.get("id")
        except Exception:
            pass

    if not q_id:
        return web.json_response({"status": "error", "message": "ID is required."}, status=400)

    unresolved_file = os.path.join(os.path.dirname(__file__), "..", "data", "unresolved_questions.json")
    if os.path.exists(unresolved_file):
        try:
            with open(unresolved_file, "r", encoding="utf-8") as f:
                unresolved_data = json.load(f)
            unresolved_data["unresolved"] = [q for q in unresolved_data.get("unresolved", []) if q.get("id") != q_id]
            with open(unresolved_file, "w", encoding="utf-8") as f:
                json.dump(unresolved_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    return web.json_response({"status": "ok", "message": "Question dismissed."})


async def api_get_presets(request: web.Request) -> web.Response:
    presets_file = os.path.join(os.path.dirname(__file__), "..", "config", "question_presets.json")
    if os.path.exists(presets_file):
        try:
            with open(presets_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return web.json_response(data)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    return web.json_response({})


async def api_update_presets(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON"}, status=400)

    presets_file = os.path.join(os.path.dirname(__file__), "..", "config", "question_presets.json")
    os.makedirs(os.path.dirname(presets_file), exist_ok=True)
    with open(presets_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    broadcast_log("[Genesis] ⚙️ Updated central question presets configuration.")
    return web.json_response({"status": "ok", "message": "Presets updated successfully."})


async def api_get_profile(request: web.Request) -> web.Response:
    profile_file = os.path.join(os.path.dirname(__file__), "..", "config", "candidate_profile.json")
    if os.path.exists(profile_file):
        try:
            with open(profile_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return web.json_response(data)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    return web.json_response({})


async def api_update_profile(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON"}, status=400)

    profile_file = os.path.join(os.path.dirname(__file__), "..", "config", "candidate_profile.json")
    os.makedirs(os.path.dirname(profile_file), exist_ok=True)
    with open(profile_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    broadcast_log("[Genesis] 👤 Candidate profile and preferences updated from UI.")
    return web.json_response({"status": "ok", "message": "Candidate profile updated successfully."})


async def api_update_preset_mapping(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON"}, status=400)

    question = str(body.get("question", "")).strip()
    answer = str(body.get("answer", "")).strip()

    if not question:
        return web.json_response({"status": "error", "message": "Question key is required."}, status=400)

    presets_file = os.path.join(os.path.dirname(__file__), "..", "config", "question_presets.json")
    presets_data = {"core_presets": {}, "question_mappings": {}, "custom_resolved_questions": {}}
    if os.path.exists(presets_file):
        try:
            with open(presets_file, "r", encoding="utf-8") as f:
                presets_data = json.load(f)
        except Exception:
            pass

    if "question_mappings" not in presets_data:
        presets_data["question_mappings"] = {}
    if "custom_resolved_questions" not in presets_data:
        presets_data["custom_resolved_questions"] = {}

    presets_data["question_mappings"][question.lower()] = answer
    presets_data["custom_resolved_questions"][question] = answer

    os.makedirs(os.path.dirname(presets_file), exist_ok=True)
    with open(presets_file, "w", encoding="utf-8") as f:
        json.dump(presets_data, f, indent=2, ensure_ascii=False)

    broadcast_log(f"[Genesis] 💾 Preset updated from UI: \"{question}\" -> \"{answer}\"")
    return web.json_response({"status": "ok", "message": f"Updated preset for '{question}'."})


async def api_delete_preset_mapping(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON"}, status=400)

    question = str(body.get("question", "")).strip()
    if not question:
        return web.json_response({"status": "error", "message": "Question key is required."}, status=400)

    presets_file = os.path.join(os.path.dirname(__file__), "..", "config", "question_presets.json")
    if os.path.exists(presets_file):
        try:
            with open(presets_file, "r", encoding="utf-8") as f:
                presets_data = json.load(f)
            
            q_lower = question.lower()
            if "question_mappings" in presets_data and q_lower in presets_data["question_mappings"]:
                del presets_data["question_mappings"][q_lower]
            if "custom_resolved_questions" in presets_data and question in presets_data["custom_resolved_questions"]:
                del presets_data["custom_resolved_questions"][question]

            with open(presets_file, "w", encoding="utf-8") as f:
                json.dump(presets_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)}, status=500)

    broadcast_log(f"[Genesis] 🗑️ Preset removed from UI: \"{question}\"")
    return web.json_response({"status": "ok", "message": f"Deleted preset '{question}'."})


async def api_update_preset_core(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON"}, status=400)

    key = str(body.get("key", "")).strip()
    value = str(body.get("value", "")).strip()

    if not key:
        return web.json_response({"status": "error", "message": "Key is required."}, status=400)

    presets_file = os.path.join(os.path.dirname(__file__), "..", "config", "question_presets.json")
    presets_data = {"core_presets": {}, "question_mappings": {}, "custom_resolved_questions": {}}
    if os.path.exists(presets_file):
        try:
            with open(presets_file, "r", encoding="utf-8") as f:
                presets_data = json.load(f)
        except Exception:
            pass

    if "core_presets" not in presets_data:
        presets_data["core_presets"] = {}

    presets_data["core_presets"][key] = value

    os.makedirs(os.path.dirname(presets_file), exist_ok=True)
    with open(presets_file, "w", encoding="utf-8") as f:
        json.dump(presets_data, f, indent=2, ensure_ascii=False)

    # Sync with candidate profile if location
    if key == "current_location":
        profile_file = os.path.join(os.path.dirname(__file__), "..", "config", "candidate_profile.json")
        if os.path.exists(profile_file):
            try:
                with open(profile_file, "r", encoding="utf-8") as pf:
                    pdata = json.load(pf)
                pdata.setdefault("personal_info", {})["city"] = value
                pdata.setdefault("personal_info", {})["current_location"] = value
                with open(profile_file, "w", encoding="utf-8") as pf:
                    json.dump(pdata, pf, indent=2, ensure_ascii=False)
            except Exception:
                pass

    broadcast_log(f"[Genesis] ⚙️ Core preset updated from UI: {key} -> \"{value}\"")
    return web.json_response({"status": "ok", "message": f"Updated core preset '{key}'."})


def create_app() -> web.Application:
    app = web.Application()
    
    # Routes
    app.router.add_get("/", serve_index)
    app.router.add_get("/api/stats", api_get_stats)
    app.router.add_get("/api/applications", api_get_applications)
    app.router.add_get("/api/status", api_get_status)
    app.router.add_get("/api/browser/status", api_get_browser_status)
    app.router.add_post("/api/browser/launch", api_launch_browser)
    app.router.add_post("/api/run", api_run_automation)
    app.router.add_post("/api/stop", api_stop_automation)
    app.router.add_post("/api/test-notification", api_test_notification)
    app.router.add_get("/api/logs/stream", api_sse_logs)

    # Dynamic Questions & Presets APIs
    app.router.add_get("/api/unresolved-questions", api_get_unresolved_questions)
    app.router.add_post("/api/unresolved-questions/resolve", api_resolve_unresolved_question)
    app.router.add_post("/api/unresolved-questions/delete", api_delete_unresolved_question)
    app.router.add_get("/api/presets", api_get_presets)
    app.router.add_post("/api/presets", api_update_presets)
    app.router.add_post("/api/presets/update-mapping", api_update_preset_mapping)
    app.router.add_post("/api/presets/delete-mapping", api_delete_preset_mapping)
    app.router.add_post("/api/presets/update-core", api_update_preset_core)

    # Candidate Profile & Preferences APIs
    app.router.add_get("/api/profile", api_get_profile)
    app.router.add_post("/api/profile", api_update_profile)

    # Serve static assets from src/web
    static_dir = os.path.join(os.path.dirname(__file__), "web")
    app.router.add_static("/static/", path=static_dir, name="static")

    return app


def run_dashboard(host: str = "127.0.0.1", port: int = 8001):
    print(f"\n=======================================================")
    print(f"🌐 GENESIS NAUKRI AUTOMATION DASHBOARD")
    print(f"👉 Open in browser: http://{host}:{port}")
    print(f"=======================================================\n")
    app = create_app()
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    run_dashboard()
