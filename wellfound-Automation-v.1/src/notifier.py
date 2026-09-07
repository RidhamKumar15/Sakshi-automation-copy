"""
Desktop Notification and Audio Alert System for Genesis Job Automation.
Dispatches native OS desktop notifications via notify-send and audio alerts via Linux sound servers (PulseAudio/PipeWire/ALSA).
"""

import os
import sys
import asyncio
import subprocess
import threading
from typing import Optional, Dict, Any


class SystemNotifier:
    """
    Manages desktop popup notifications and audible chimes for automation lifecycle events.
    """

    SOUND_MAP = {
        "success": "/usr/share/sounds/freedesktop/stereo/complete.oga",
        "complete": "/usr/share/sounds/freedesktop/stereo/complete.oga",
        "warning": "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga",
        "attention": "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga",
        "action_required": "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga",
        "error": "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga",
        "failed": "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
        "stopped": "/usr/share/sounds/freedesktop/stereo/service-logout.oga",
        "submitted": "/usr/share/sounds/freedesktop/stereo/message-new-instant.oga",
        "login": "/usr/share/sounds/freedesktop/stereo/service-login.oga"
    }

    @staticmethod
    def _get_host_env() -> Dict[str, str]:
        """
        Constructs complete environment containing user D-Bus and PulseAudio socket paths.
        """
        env = os.environ.copy()
        uid = os.getuid() if hasattr(os, "getuid") else 1000

        if "XDG_RUNTIME_DIR" not in env:
            env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"

        if "DBUS_SESSION_BUS_ADDRESS" not in env:
            bus_path = f"/run/user/{uid}/bus"
            if os.path.exists(bus_path):
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"

        if "DISPLAY" not in env:
            env["DISPLAY"] = ":0"

        return env

    @classmethod
    def _play_sound_sync(cls, sound_type: str = "complete"):
        """
        Plays system sound using the first available audio binary.
        """
        env = cls._get_host_env()
        sound_file = cls.SOUND_MAP.get(sound_type, cls.SOUND_MAP["complete"])

        # Strategy 1: PulseAudio / PipeWire paplay
        if os.path.exists(sound_file) and subprocess.run(["which", "paplay"], capture_output=True).returncode == 0:
            try:
                res = subprocess.run(["paplay", sound_file], env=env, capture_output=True)
                if res.returncode == 0:
                    return
            except Exception:
                pass

        # Strategy 2: libcanberra canberra-gtk-play
        theme_sound_name = "complete" if sound_type in ["success", "complete"] else ("dialog-warning" if sound_type in ["warning", "error", "action_required"] else "service-logout")
        if subprocess.run(["which", "canberra-gtk-play"], capture_output=True).returncode == 0:
            try:
                res = subprocess.run(["canberra-gtk-play", "-i", theme_sound_name], env=env, capture_output=True)
                if res.returncode == 0:
                    return
            except Exception:
                pass

        # Strategy 3: PipeWire pw-play
        if os.path.exists(sound_file) and subprocess.run(["which", "pw-play"], capture_output=True).returncode == 0:
            try:
                res = subprocess.run(["pw-play", sound_file], env=env, capture_output=True)
                if res.returncode == 0:
                    return
            except Exception:
                pass

        # Strategy 4: ALSA aplay
        alsa_fallback = "/usr/share/sounds/alsa/Front_Center.wav"
        if os.path.exists(alsa_fallback) and subprocess.run(["which", "aplay"], capture_output=True).returncode == 0:
            try:
                res = subprocess.run(["aplay", "-q", alsa_fallback], env=env, capture_output=True)
                if res.returncode == 0:
                    return
            except Exception:
                pass

        # Strategy 5: Terminal Bell ASCII 7
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass

    @classmethod
    def _send_popup_sync(cls, title: str, message: str, urgency: str = "normal"):
        """
        Dispatches native desktop popup using notify-send.
        Forces 'normal' priority so desktop notification banners reliably pop out across all Linux desktop managers.
        """
        env = cls._get_host_env()
        # Always enforce normal urgency as requested by user to ensure popout banners
        safe_urgency = "normal"
        try:
            subprocess.run(
                [
                    "notify-send",
                    "-a", "Genesis Automation",
                    "-u", safe_urgency,
                    "-t", "5000",
                    title,
                    message
                ],
                env=env,
                capture_output=True
            )
        except Exception:
            pass

    @classmethod
    def notify(
        cls,
        title: str,
        message: str,
        sound_type: str = "complete",
        urgency: str = "normal",
        play_sound: bool = True
    ):
        """
        Dispatches desktop notification and audio chime asynchronously in a background thread.
        Always uses normal urgency for reliable desktop banner display.
        """
        def _dispatch():
            cls._send_popup_sync(title, message, urgency="normal")
            if play_sound:
                cls._play_sound_sync(sound_type)

        threading.Thread(target=_dispatch, daemon=True).start()

    # Lifecycle Event Helpers

    @classmethod
    def notify_run_completed(cls, submitted: int = 0, review: int = 0, total: int = 0):
        """
        Fires when an automation run successfully completes.
        """
        title = "🎯 Genesis Automation Completed"
        msg = f"Run finished! Submitted: {submitted} | Review Queue: {review} | Evaluated: {total}"
        cls.notify(title, msg, sound_type="complete", urgency="normal")

    @classmethod
    def notify_run_stopped(cls, reason: str = "User requested stop"):
        """
        Fires when automation is manually cancelled or stopped.
        """
        title = "🛑 Genesis Automation Stopped"
        msg = f"Automation execution halted: {reason}"
        cls.notify(title, msg, sound_type="stopped", urgency="normal")

    @classmethod
    def notify_run_failed(cls, error_msg: str):
        """
        Fires when an automation execution encounters an unhandled error or crashes.
        """
        title = "❌ Genesis Automation Error"
        msg = f"Automation stopped due to error: {error_msg[:120]}"
        cls.notify(title, msg, sound_type="error", urgency="normal")

    @classmethod
    def notify_action_required(cls, company: str, role: str, reason: str = "Manual input needed"):
        """
        Fires when a job application modal opens and requires candidate attention.
        """
        title = f"⚠️ Action Required: {company}"
        msg = f"Job '{role}' requires input: {reason}"
        cls.notify(title, msg, sound_type="action_required", urgency="normal")

    @classmethod
    def notify_job_submitted(cls, company: str, role: str):
        """
        Fires when a job application is successfully verified as submitted.
        """
        title = f"✅ Applied: {company}"
        msg = f"Application officially submitted for '{role}' on Wellfound!"
        cls.notify(title, msg, sound_type="submitted", urgency="normal")


# Singleton shorthand
notifier = SystemNotifier
