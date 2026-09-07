import asyncio
import json
import logging
import os
import subprocess
import time
from typing import Any, Dict, List, Optional
import aiohttp

logger = logging.getLogger("CDPClient")


class ChromeCDPClient:
    """
    Direct Chrome DevTools Protocol (CDP) WebSocket client.
    Connects to real Google Chrome with the dedicated 'Automation' profile.
    Requires no external npm/pip binaries and preserves all cookies, sessions, and storage.
    """

    def __init__(
        self,
        port: int = 9222,
        user_data_dir: str = "~/.config/google-chrome-automation",
        profile_directory: str = "Default",
        profile_name: str = "Sakshi-Automation"
    ):
        self.port = port
        self.user_data_dir = os.path.abspath(os.path.expanduser(user_data_dir))
        self.profile_name = profile_name
        self.profile_directory = self._resolve_profile_directory(profile_directory)
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.current_target_id: Optional[str] = None
        self._msg_id = 0
        self._pending_requests: Dict[int, asyncio.Future] = {}
        self._listener_task: Optional[asyncio.Task] = None

    def _resolve_profile_directory(self, fallback: str) -> str:
        """
        Scans Local State in user_data_dir to find the folder name matching profile_name.
        """
        local_state_path = os.path.join(self.user_data_dir, "Local State")
        if os.path.exists(local_state_path):
            try:
                with open(local_state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cache = data.get("profile", {}).get("info_cache", {})
                for p_dir, p_info in cache.items():
                    name = p_info.get("name", "").strip()
                    if name.lower() == self.profile_name.strip().lower():
                        return p_dir
            except Exception as e:
                logger.warning(f"Could not parse Local State: {e}")
        return fallback

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def is_chrome_running(self) -> bool:
        """
        Checks if Chrome is listening on the remote debugging port.
        """
        url = f"http://127.0.0.1:{self.port}/json/version"
        try:
            headers = {"Host": f"localhost:{self.port}"}
            async with aiohttp.ClientSession(headers=headers) as temp_session:
                async with temp_session.get(url, timeout=2.0) as resp:
                    if resp.status == 200:
                        return True
        except Exception:
            return False
        return False

    def launch_chrome_process(self) -> Optional[subprocess.Popen]:
        """
        Launches Google Chrome with remote debugging enabled and the Automation profile.
        """
        user_data_path = self.user_data_dir
        os.makedirs(user_data_path, exist_ok=True)

        candidates = [
            "google-chrome",
            "google-chrome-stable",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/opt/google/chrome/google-chrome",
            "/opt/google/chrome/chrome",
            "chromium",
            "/usr/bin/chromium"
        ]

        flags = [
            f"--user-data-dir={user_data_path}",
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=*",
            f"--profile-directory={self.profile_directory}",
            "--no-first-run",
            "--no-default-browser-check"
        ]

        env = os.environ.copy()
        if "DISPLAY" not in env:
            env["DISPLAY"] = ":0"
        if "WAYLAND_DISPLAY" not in env and os.path.exists(f"/run/user/{os.getuid()}/wayland-0"):
            env["WAYLAND_DISPLAY"] = "wayland-0"
        if "XDG_RUNTIME_DIR" not in env:
            env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
        if "DBUS_SESSION_BUS_ADDRESS" not in env and os.path.exists(f"/run/user/{os.getuid()}/bus"):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"

        for binary in candidates:
            cmd = [binary] + flags
            try:
                proc = subprocess.Popen(
                    cmd,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    preexec_fn=os.setsid if hasattr(os, "setsid") else None
                )
                print(f"[CDP] Launched Chrome with profile '{self.profile_name}' ({self.profile_directory}) using '{binary}'")
                return proc
            except Exception:
                continue

        print(f"[CDP] Could not auto-launch Chrome. Please start Chrome with:\n  google-chrome --user-data-dir=\"{user_data_path}\" --profile-directory=\"{self.profile_directory}\" --remote-debugging-port={self.port} --remote-allow-origins=*")
        return None

    async def connect(self, target_url_match: Optional[str] = None, create_new: bool = True) -> bool:
        """
        Connects to Chrome CDP endpoint and attaches to an active tab or creates one.
        """
        if not await self.is_chrome_running():
            print(f"[CDP] Chrome is not responding on port {self.port}. Attempting launch...")
            self.launch_chrome_process()
            # Wait up to 5 seconds for Chrome to start
            for _ in range(10):
                await asyncio.sleep(0.5)
                if await self.is_chrome_running():
                    print(f"[CDP] Chrome connected successfully on port {self.port}!")
                    break
            else:
                print(f"[CDP] ERROR: Could not connect to Chrome on port {self.port}.")
                print(f"[CDP] Please run Google Chrome with:\n  google-chrome --remote-debugging-port={self.port} --profile-directory=\"{self.profile_directory}\"")
                return False

        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers={"Host": f"localhost:{self.port}"})

        # Fetch active targets
        async with self.session.get(f"http://127.0.0.1:{self.port}/json/list") as resp:
            targets: List[Dict[str, Any]] = await resp.json()

        # Hostnames to protect from automation override (e.g. Genesis Dashboard UI)
        protected_hosts = ["localhost:8000", "127.0.0.1:8000", "localhost:8001", "127.0.0.1:8001", "chrome://", "devtools://"]

        page_target = None
        if target_url_match:
            for t in targets:
                if t.get("type") == "page":
                    url = t.get("url", "")
                    if target_url_match in url and not any(ph in url for ph in protected_hosts):
                        page_target = t
                        break

        # If no matching portal tab exists or create_new is requested, open a clean dedicated automation tab
        if not page_target:
            if create_new:
                # Open a new tab in Chrome so the Genesis Dashboard stays open
                async with self.session.put(f"http://127.0.0.1:{self.port}/json/new?about:blank") as resp:
                    page_target = await resp.json()
            else:
                for t in targets:
                    if t.get("type") == "page":
                        url = t.get("url", "")
                        if not any(ph in url for ph in protected_hosts):
                            page_target = t
                            break

        if not page_target:
            async with self.session.put(f"http://127.0.0.1:{self.port}/json/new?about:blank") as resp:
                page_target = await resp.json()

        if not page_target:
            print("[CDP] No available page targets found.")
            return False

        ws_url = page_target.get("webSocketDebuggerUrl")
        self.current_target_id = page_target.get("id")
        print(f"[CDP] Attaching to automation tab: {page_target.get('title')} ({page_target.get('url')})")

        self.ws = await self.session.ws_connect(ws_url, max_msg_size=10 * 1024 * 1024)
        self._listener_task = asyncio.create_task(self._listen_loop())

        # Enable essential CDP domains
        await self.send("Page.enable")
        await self.send("Runtime.enable")
        await self.send("DOM.enable")
        return True

    async def _listen_loop(self):
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    req_id = data.get("id")
                    if req_id is not None and req_id in self._pending_requests:
                        future = self._pending_requests.pop(req_id)
                        if not future.done():
                            if "error" in data:
                                future.set_exception(RuntimeError(data["error"]))
                            else:
                                future.set_result(data.get("result", {}))
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except Exception as e:
            logger.error(f"[CDP] Listen loop exception: {e}")

    async def reconnect(self) -> bool:
        """
        Re-establishes the WebSocket connection to the active page target.
        """
        try:
            if self._listener_task and not self._listener_task.done():
                self._listener_task.cancel()
            if self.ws and not self.ws.closed:
                await self.ws.close()
            
            if self.session is None or self.session.closed:
                self.session = aiohttp.ClientSession(headers={"Host": f"localhost:{self.port}"})

            async with self.session.get(f"http://127.0.0.1:{self.port}/json/list") as resp:
                targets = await resp.json()

            page_target = None
            for t in targets:
                if t.get("type") == "page":
                    if self.current_target_id and t.get("id") == self.current_target_id:
                        page_target = t
                        break
                    elif not page_target:
                        page_target = t

            if not page_target:
                return False

            ws_url = page_target.get("webSocketDebuggerUrl")
            self.current_target_id = page_target.get("id")
            self.ws = await self.session.ws_connect(ws_url, max_msg_size=10 * 1024 * 1024)
            self._listener_task = asyncio.create_task(self._listen_loop())

            # Enable CDP domains
            await self.ws.send_str(json.dumps({"id": self._next_id(), "method": "Page.enable", "params": {}}))
            await self.ws.send_str(json.dumps({"id": self._next_id(), "method": "Runtime.enable", "params": {}}))
            await self.ws.send_str(json.dumps({"id": self._next_id(), "method": "DOM.enable", "params": {}}))
            print("[CDP] 🔄 Reconnected WebSocket successfully.")
            return True
        except Exception as e:
            print(f"[CDP] Reconnection failed: {e}")
            return False

    async def send(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 20.0, retry_count: int = 2) -> Dict[str, Any]:
        """
        Sends a CDP JSON-RPC command with automatic reconnection and retry.
        """
        for attempt in range(retry_count + 1):
            if not self.ws or self.ws.closed:
                print(f"[CDP] WebSocket closed. Attempting auto-reconnect (attempt {attempt + 1}/{retry_count + 1})...")
                ok = await self.reconnect()
                if not ok:
                    if attempt == retry_count:
                        raise ConnectionError("CDP WebSocket is not connected.")
                    await asyncio.sleep(1.0)
                    continue

            req_id = self._next_id()
            payload = {"id": req_id, "method": method, "params": params or {}}
            future = asyncio.get_running_loop().create_future()
            self._pending_requests[req_id] = future

            try:
                await self.ws.send_str(json.dumps(payload))
                return await asyncio.wait_for(future, timeout=timeout)
            except (ConnectionError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                self._pending_requests.pop(req_id, None)
                if attempt < retry_count:
                    print(f"[CDP] Command '{method}' encountered {type(e).__name__}. Auto-reconnecting...")
                    await self.reconnect()
                    await asyncio.sleep(1.0)
                else:
                    raise

    async def navigate(self, url: str, wait_seconds: float = 3.0) -> Dict[str, Any]:
        """
        Navigates the tab to the specified URL and waits.
        """
        print(f"[CDP] Navigating to: {url}")
        res = await self.send("Page.navigate", {"url": url})
        await asyncio.sleep(wait_seconds)
        return res

    async def evaluate(self, expression: str) -> Any:
        """
        Executes JavaScript in the page context and returns the result value.
        """
        res = await self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True
        })
        val_obj = res.get("result", {})
        return val_obj.get("value")

    async def wait_for_selector(self, selector: str, timeout: float = 10.0, check_interval: float = 0.4) -> bool:
        """
        Polls for a DOM element matching selector until timeout.
        """
        start = time.time()
        js = f"Boolean(document.querySelector('{selector}'))"
        while time.time() - start < timeout:
            try:
                found = await self.evaluate(js)
                if found:
                    return True
            except Exception:
                pass
            await asyncio.sleep(check_interval)
        return False

    async def click(self, selector: str) -> bool:
        """
        Clicks an element using JavaScript dispatch.
        """
        js = f"""
        (() => {{
            const el = document.querySelector('{selector}');
            if (el) {{
                el.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                el.focus();
                el.click();
                return true;
            }}
            return false;
        }})()
        """
        return bool(await self.evaluate(js))

    async def type_text(self, selector: str, text: str) -> bool:
        """
        Types text into an input or textarea element and fires input events.
        """
        escaped_text = json.dumps(text)
        js = f"""
        (() => {{
            const el = document.querySelector('{selector}');
            if (el) {{
                el.focus();
                el.value = {escaped_text};
                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return true;
            }}
            return false;
        }})()
        """
        return bool(await self.evaluate(js))

    async def upload_file(self, selector: str, file_path: str) -> bool:
        """
        Sets files on an input[type="file"] element via CDP DOM.setFileInputFiles.
        """
        abs_path = os.path.abspath(file_path)
        if not os.path.exists(abs_path):
            print(f"[CDP] Upload failed: File not found at {abs_path}")
            return False

        try:
            # Find DOM Node ID
            doc = await self.send("DOM.getDocument")
            node_res = await self.send("DOM.querySelector", {
                "nodeId": doc["root"]["nodeId"],
                "selector": selector
            })
            node_id = node_res.get("nodeId")
            if not node_id:
                print(f"[CDP] File input selector not found: {selector}")
                return False

            await self.send("DOM.setFileInputFiles", {
                "files": [abs_path],
                "nodeId": node_id
            })
            print(f"[CDP] Successfully attached file: {abs_path}")
            return True
        except Exception as e:
            print(f"[CDP] Error in upload_file: {e}")
            return False

    async def check_for_security_challenge(self) -> Dict[str, Any]:
        """
        Inspects page DOM for real, visible blocking challenges:
        - Cloudflare verification screens (#challenge-stage, 'Just a moment...')
        - Visible reCAPTCHA / hCaptcha / Arkose puzzle popups (bframe, challenge iframe with real dimensions)
        - Visible OTP / 2FA verification inputs
        - Visible login gates (when session is expired)
        """
        js = """
        (() => {
            // 1. Cloudflare Turnstile / Bot challenge (blocking page)
            const cf = (() => {
                const stage = document.querySelector('#challenge-stage, #challenge-running, .cf-turnstile, #cf-wrapper');
                if (stage && stage.offsetParent !== null) return true;
                const title = (document.title || '').toLowerCase();
                const text = (document.body ? document.body.innerText : '').toLowerCase();
                if ((title.includes('just a moment') || title.includes('attention required') || title.includes('security check')) &&
                    (text.includes('verifying you are human') || text.includes('enable javascript and cookies') || text.includes('cf-chl-widget'))) {
                    return true;
                }
                return false;
            })();

            // 2. Visible reCAPTCHA / hCaptcha / Arkose Challenge Popup/Iframe
            const recaptcha = (() => {
                const iframes = Array.from(document.querySelectorAll('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], iframe[src*="arkose"], iframe[title*="recaptcha"], iframe[title*="challenge"]'));
                for (const f of iframes) {
                    // Must be visible and have real dimensions (not 0x0 or invisible token badge)
                    const rect = f.getBoundingClientRect();
                    const isVisible = f.offsetParent !== null && rect.width > 50 && rect.height > 50 && window.getComputedStyle(f).visibility !== 'hidden' && window.getComputedStyle(f).display !== 'none';
                    const isChallengeFrame = (f.src || '').includes('/bframe') || (f.src || '').includes('challenge') || (f.title || '').toLowerCase().includes('challenge');
                    if (isVisible && isChallengeFrame) return true;
                }
                
                const puzzle = document.querySelector('#arkose, [class*="captcha-modal"], [class*="geetest"], [class*="slider-captcha"]');
                if (puzzle && puzzle.offsetParent !== null && puzzle.clientHeight > 50) return true;
                return false;
            })();

            // 3. OTP / Verification required
            const otp = (() => {
                const otpInput = document.querySelector('input[name*="otp"], input[id*="otp"], input[autocomplete="one-time-code"]');
                if (otpInput && otpInput.offsetParent !== null) return true;
                const bodyText = (document.body ? document.body.innerText : '').toLowerCase();
                if ((bodyText.includes('enter the 6-digit') || bodyText.includes('enter otp') || bodyText.includes('one time password')) &&
                    document.querySelector('input[type="number"], input[type="text"][maxlength="6"], input[type="text"][maxlength="4"]')) {
                    return true;
                }
                return false;
            })();

            // 4. Login Required Gate (Only if user is actually logged out)
            const login_required = (() => {
                const hasLoginModal = document.querySelector('[class*="login-modal"], form[action*="login"], div[class*="login-layer"]');
                if (hasLoginModal && hasLoginModal.offsetParent !== null) return true;

                if (window.location.href.includes('/nlogin') || (window.location.href.includes('/login') && !document.querySelector('header, nav, [class*="profile"]'))) {
                    return true;
                }
                return false;
            })();

            return {
                has_challenge: Boolean(cf || recaptcha || otp || login_required),
                cloudflare: Boolean(cf),
                recaptcha: Boolean(recaptcha),
                otp: Boolean(otp),
                login_required: Boolean(login_required)
            };
        })()
        """
        try:
            res = await self.evaluate(js)
            return res or {"has_challenge": False}
        except Exception:
            return {"has_challenge": False}

    async def pause_and_wait_for_human(self, reason: str, check_interval: float = 2.0, timeout: float = 300.0) -> bool:
        """
        Pauses automation when a security challenge or manual action is needed.
        Waits for the user to resolve it in the visible Chrome window, then resumes.
        """
        from src.notifier import notify_attention_required, send_desktop_notification

        print("\n" + "=" * 70)
        print("🚨 [HUMAN ACTION REQUIRED] AUTOMATION PAUSED")
        print(f"Reason: {reason}")
        print("👉 Please switch to your visible Chrome browser window and complete the action.")
        print("   The automation is monitoring and will resume immediately once resolved.")
        print("=" * 70 + "\n")

        # Sound chime & desktop popup notification
        notify_attention_required(reason)

        start = time.time()
        while time.time() - start < timeout:
            await asyncio.sleep(check_interval)
            status = await self.check_for_security_challenge()
            if not status.get("has_challenge", False):
                print("✅ [CDP] Verification cleared! Resuming automation workflow...\n")
                send_desktop_notification("✅ Verification Cleared", "Resuming Naukri automation workflow...", sound=None)
                await asyncio.sleep(1.5)
                return True

        print("⚠️ [CDP] Timed out waiting for human action (5 minutes).")
        return False

    async def close(self):
        """
        Cleanly closes WebSocket connection while leaving Chrome running.
        """
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
        if self.ws and not self.ws.closed:
            await self.ws.close()
        if self.session and not self.session.closed:
            await self.session.close()
        print("[CDP] Client disconnected.")
