import os
import shutil
import subprocess
import sys
import threading
from typing import Optional, Dict, Any


SOUNDS_DIR = os.path.join(os.path.dirname(__file__), "web", "sounds")


def play_sound(sound_type: str = "success"):
    """
    Plays an alert sound asynchronously in a background thread.
    Uses dedicated WAV sound assets or Linux system sound daemons.
    sound_type: 'success' | 'warning' | 'error' | 'info'
    """
    def _worker():
        wav_file = os.path.join(SOUNDS_DIR, f"{sound_type}.wav")
        
        # 1. Try custom synthesized WAV file with paplay, pw-play, aplay, or ffplay
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


_last_notifications = {}


def send_desktop_notification(
    title: str,
    message: str,
    urgency: str = "normal",
    sound: Optional[str] = None,
    timeout_ms: int = 4000
):
    """
    Triggers a system popup notification and audio sound alert.
    All notifications are sent as normal/transient priority so GNOME auto-dismisses them.
    sound: 'success' | 'warning' | 'error' | 'info'
    urgency: 'low' | 'normal'
    """
    import time
    now = time.time()
    notif_key = f"{title}:{message}"
    
    # 10-second debounce / cooldown for identical notifications to avoid spamming GNOME
    if notif_key in _last_notifications and (now - _last_notifications[notif_key]) < 10:
        return
    _last_notifications[notif_key] = now

    if sound:
        play_sound(sound)

    icon_map = {
        "success": "emblem-default",
        "warning": "dialog-warning",
        "error": "dialog-error",
        "info": "dialog-information"
    }
    icon = icon_map.get(sound or "info", "dialog-information")
    # Strictly enforce normal or low priority (never critical to avoid sticky/infinite GNOME banners)
    safe_urgency = "low" if urgency == "low" else "normal"

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
                "--hint=int:transient:1",
                "--hint=string:x-canonical-private-synchronous:indeed-alert",
                "--app-name=Indeed Automation",
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
        except Exception:
            pass


def notify_started(mode: str = "Indeed"):
    """
    Notify user that the Indeed automation has started.
    """
    title = f"🚀 {mode} Automation Started"
    msg = f"Job search and application loop is now running for {mode}."
    send_desktop_notification(title, msg, urgency="normal", sound="info")


def notify_completion(submitted_count: int, total_evaluated: int = 0):
    """
    Notify user that the Indeed automation run has successfully completed.
    """
    title = "🎉 Indeed Automation Completed"
    msg = f"Application run finished! {submitted_count} application(s) submitted ({total_evaluated} evaluated)."
    send_desktop_notification(title, msg, urgency="normal", sound="success")


def notify_attention_required(reason: str):
    """
    Notify user that human action (CAPTCHA / Login / Security Verification) is needed.
    """
    title = "🚨 [Action Required] Indeed Security Check / CAPTCHA"
    msg = f"{reason} - Please switch to Chrome and complete it. Automation is paused."
    send_desktop_notification(title, msg, urgency="normal", sound="warning", timeout_ms=5000)


def notify_captcha_cleared():
    """
    Notify user that CAPTCHA / challenge has disappeared and automation is resuming.
    """
    title = "✅ CAPTCHA / Verification Cleared"
    msg = "Challenge resolved. Resuming application workflow..."
    send_desktop_notification(title, msg, urgency="normal", sound="success")


def notify_stopped(reason: str = "Automation stopped by user"):
    """
    Notify user that automation was stopped.
    """
    title = "🛑 Indeed Automation Stopped"
    send_desktop_notification(title, reason, urgency="normal", sound="info")


def notify_error(error_msg: str):
    """
    Notify user that automation failed or encountered an error.
    """
    title = "❌ Indeed Automation Failed"
    msg = f"Error: {error_msg}"
    send_desktop_notification(title, msg, urgency="normal", sound="error", timeout_ms=5000)


class SystemNotifier:
    """
    Class wrapper for backward compatibility.
    """
    @staticmethod
    def notify_run_started(mode: str = "Indeed"):
        notify_started(mode)

    @staticmethod
    def notify_run_completed(submitted: int, review: int = 0, total: int = 0):
        notify_completion(submitted, total)

    @staticmethod
    def notify_run_failed(error_msg: str):
        notify_error(error_msg)

    @staticmethod
    def notify_run_stopped(reason: str):
        notify_stopped(reason)

    @staticmethod
    def notify_attention(reason: str):
        notify_attention_required(reason)
