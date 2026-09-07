import json
import os
import shutil
import subprocess
import time
from typing import Optional


class SystemNotifier:
    """
    Sends native system desktop notifications with rate-limiting and sound suppression.
    """
    _last_notify_time = 0.0
    _cooldown = 20.0

    @classmethod
    def _is_enabled(cls) -> bool:
        try:
            settings_path = os.path.join(os.path.dirname(__file__), "..", "config", "settings.json")
            if os.path.exists(settings_path):
                with open(settings_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f).get("notifications", {})
                    cls._cooldown = float(cfg.get("cooldown_seconds", 20.0))
                    return cfg.get("desktop_enabled", True)
        except Exception:
            pass
        return True

    @classmethod
    def send(cls, title: str, message: str, urgency: str = "low", icon: str = "dialog-information", force: bool = False):
        """
        Invokes notify-send on Linux desktop environments safely with rate limiting.
        """
        now = time.time()
        if not force and (now - cls._last_notify_time) < cls._cooldown:
            return

        if not cls._is_enabled():
            return

        if shutil.which("notify-send"):
            try:
                subprocess.run(
                    ["notify-send", "-u", urgency, "-i", icon, title, message],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3
                )
                cls._last_notify_time = now
            except Exception:
                pass

    @classmethod
    def notify_run_started(cls, source: str = "Shine"):
        cls.send(
            title=f"🚀 {source} Job Automation Started",
            message="Discovering, evaluating, and applying to matching roles autonomously.",
            urgency="normal"
        )

    @classmethod
    def notify_run_completed(cls, submitted: int, review: int, total: int):
        cls.send(
            title="🎉 Job Automation Run Complete!",
            message=f"Submitted: {submitted} | In Review: {review} | Total Evaluated: {total}",
            urgency="normal"
        )

    @classmethod
    def notify_human_action_needed(cls, reason: str, job_url: Optional[str] = None):
        msg = f"Action required: {reason}"
        if job_url:
            msg += f"\nURL: {job_url}"
        cls.send(
            title="🚨 Human Checkpoint Required",
            message=msg,
            urgency="critical",
            icon="dialog-warning"
        )

    @classmethod
    def notify_run_stopped(cls, reason: str = "Cancelled by user"):
        cls.send(
            title="🛑 Automation Stopped",
            message=f"Process halted: {reason}",
            urgency="normal"
        )

    @classmethod
    def notify_run_failed(cls, error_msg: str):
        cls.send(
            title="❌ Automation Error",
            message=f"Execution error: {error_msg[:120]}",
            urgency="critical",
            icon="dialog-error"
        )


def notify_started(source: str = "Shine"):
    SystemNotifier.notify_run_started(source)


def notify_completion(submitted_count: int, total_evaluated: int, review_count: int = 0):
    SystemNotifier.notify_run_completed(submitted=submitted_count, review=review_count, total=total_evaluated)


def notify_human_input(reason: str, job_url: str = ""):
    SystemNotifier.notify_human_action_needed(reason=reason, job_url=job_url)


def notify_stopped(reason: str = "Cancelled by user"):
    SystemNotifier.notify_run_stopped(reason)


def notify_error(msg: str):
    SystemNotifier.notify_run_failed(msg)
