import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Optional, Dict, Any


SOUNDS_DIR = os.path.join(os.path.dirname(__file__), "web", "sounds")

_last_sound_time = 0.0
_sound_lock = threading.Lock()


def play_sound(sound_type: str = "success", force: bool = False, cooldown: float = 10.0):
    """
    Plays an alert sound asynchronously in a background thread with rate-limiting.
    Uses dedicated WAV sound assets or Linux system sound daemons.
    """
    global _last_sound_time
    now = time.time()
    with _sound_lock:
        if not force and (now - _last_sound_time < cooldown):
            return
        _last_sound_time = now

    def _worker():
        wav_file = os.path.join(SOUNDS_DIR, f"{sound_type}.wav")
        
        # 1. Try playing custom synthesized WAV file with paplay, pw-play, or aplay
        players = [
            ("paplay", ["paplay"]),
            ("pw-play", ["pw-play"]),
            ("aplay", ["aplay", "-q"]),
            ("ffplay", ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"])
        ]
        
        if os.path.exists(wav_file):
            for binary, cmd_prefix in players:
                if shutil.which(binary):
                    try:
                        res = subprocess.run(
                            cmd_prefix + [wav_file],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=4
                        )
                        if res.returncode == 0:
                            return
                    except Exception:
                        pass

        # 2. Try canberra-gtk-play with system sound theme
        if shutil.which("canberra-gtk-play"):
            sound_names = {
                "success": "complete",
                "warning": "dialog-warning",
                "error": "dialog-error",
                "info": "message"
            }
            theme_name = sound_names.get(sound_type, "complete")
            try:
                res = subprocess.run(
                    ["canberra-gtk-play", "-i", theme_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3
                )
                if res.returncode == 0:
                    return
            except Exception:
                pass

        # 3. Fallback to terminal bell
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


def send_desktop_notification(
    title: str,
    message: str,
    urgency: str = "normal",
    sound: Optional[str] = None,
    timeout_ms: int = 7000
):
    """
    Triggers a system popup notification and optional audio sound alert.
    sound: 'success' | 'warning' | 'error' | 'info'
    urgency: 'low' | 'normal' | 'critical'
    """
    if sound:
        play_sound(sound)

    icon_map = {
        "success": "emblem-default",
        "warning": "dialog-warning",
        "error": "dialog-error",
        "info": "dialog-information"
    }
    icon = icon_map.get(sound or "info", "dialog-information")
    safe_urgency = "normal" if urgency == "critical" else urgency

    if shutil.which("notify-send"):
        try:
            env = os.environ.copy()
            if "DISPLAY" not in env:
                env["DISPLAY"] = ":0"
            if "WAYLAND_DISPLAY" not in env and os.path.exists(f"/run/user/{os.getuid()}/wayland-0"):
                env["WAYLAND_DISPLAY"] = "wayland-0"
            if "DBUS_SESSION_BUS_ADDRESS" not in env and os.path.exists(f"/run/user/{os.getuid()}/bus"):
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"

            cmd = [
                "notify-send",
                f"--urgency={safe_urgency}",
                f"--expire-time={timeout_ms}",
                "--app-name=Naukri Automation",
                f"--icon={icon}",
                title,
                message
            ]
            subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            pass


def notify_completion(submitted_count: int, total_evaluated: int = 0, manual_action_count: int = 0):
    """
    Notify user that the automation run has successfully completed.
    Includes count of jobs requiring manual input/review.
    """
    title = "🎉 Naukri Automation Completed"
    if manual_action_count > 0:
        msg = f"Application run finished! {submitted_count} submitted ({total_evaluated} evaluated). {manual_action_count} job(s) waiting for input/review."
    else:
        msg = f"Application run finished! {submitted_count} application(s) submitted ({total_evaluated} evaluated)."
    send_desktop_notification(title, msg, urgency="normal", sound="success", timeout_ms=8000)


def notify_attention_required(reason: str):
    """
    Notify user that critical human action (CAPTCHA / Login / Security Verification) is needed.
    """
    title = "🚨 [Action Required] Naukri Automation"
    msg = f"{reason} - Please check your Chrome browser."
    send_desktop_notification(title, msg, urgency="normal", sound="warning", timeout_ms=6000)


def notify_stopped(reason: str = "Automation stopped by user"):
    """
    Notify user that automation was stopped.
    """
    title = "🛑 Naukri Automation Stopped"
    send_desktop_notification(title, reason, urgency="normal", sound="info", timeout_ms=4000)


def notify_error(error_msg: str):
    """
    Notify user that automation failed or encountered an error.
    """
    title = "❌ Naukri Automation Failed"
    msg = f"Error: {error_msg}"
    send_desktop_notification(title, msg, urgency="normal", sound="error", timeout_ms=5000)
