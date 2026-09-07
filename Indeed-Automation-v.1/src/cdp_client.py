import asyncio
import json
import logging
import os
import subprocess
import time
from typing import Any, Dict, List, Optional
import aiohttp

logger = logging.getLogger("CDPClient")


def resolve_profile_directory(user_data_dir: str, profile_name: str, fallback: str = "Profile 4") -> str:
    """
    Scans Local State in user_data_dir to find the folder name matching profile_name.
    """
    expanded_dir = os.path.abspath(os.path.expanduser(user_data_dir))
    local_state_path = os.path.join(expanded_dir, "Local State")
    if os.path.exists(local_state_path):
        try:
            with open(local_state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            cache = data.get("profile", {}).get("info_cache", {})
            for p_dir, p_info in cache.items():
                name = p_info.get("name", "").strip()
                if name.lower() == profile_name.strip().lower():
                    return p_dir
        except Exception as e:
            logger.warning(f"Could not parse Local State: {e}")
    return fallback


class ChromeCDPClient:
    """
    Direct Chrome DevTools Protocol (CDP) WebSocket client.
    Connects to real Google Chrome with the dedicated 'Automation' profile.
    Preserves all cookies, sessions, and local storage.
    """

    def __init__(
        self,
        port: int = 9222,
        user_data_dir: str = "~/.config/google-chrome-automation",
        profile_directory: str = "Profile 4",
        profile_name: str = "Sakshi-Automation"
    ):
        self.port = port
        self.user_data_dir = os.path.abspath(os.path.expanduser(user_data_dir))
        self.profile_name = profile_name
        self.profile_directory = resolve_profile_directory(self.user_data_dir, profile_name, fallback=profile_directory)
        self.session: Optional[aiohttp.ClientSession] = None
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.current_target_id: Optional[str] = None
        self._msg_id = 0
        self._pending_requests: Dict[int, asyncio.Future] = {}
        self._listener_task: Optional[asyncio.Task] = None

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
            "/bin/google-chrome",
            "/bin/google-chrome-stable",
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

    async def get_all_targets(self) -> List[Dict[str, Any]]:
        """
        Fetches all available targets from Chrome debugging port.
        """
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers={"Host": f"localhost:{self.port}"})
        try:
            async with self.session.get(f"http://127.0.0.1:{self.port}/json/list") as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            logger.warning(f"[CDP] get_all_targets error: {e}")
        return []

    async def switch_to_target(self, target_id: str) -> bool:
        """
        Switches WebSocket attachment to the given target_id.
        """
        try:
            targets = await self.get_all_targets()
            target = next((t for t in targets if t.get("id") == target_id), None)
            if not target:
                print(f"[CDP] switch_to_target: Target {target_id} not found.")
                return False

            if self._listener_task and not self._listener_task.done():
                self._listener_task.cancel()
            if self.ws and not self.ws.closed:
                await self.ws.close()

            ws_url = target.get("webSocketDebuggerUrl")
            self.current_target_id = target_id
            print(f"[CDP] 🔄 Switching active tab to: {target.get('title', 'Tab')} ({target.get('url', '')})")

            self.ws = await self.session.ws_connect(ws_url, max_msg_size=10 * 1024 * 1024)
            self._listener_task = asyncio.create_task(self._listen_loop())

            await self.send("Page.enable")
            await self.send("Runtime.enable")
            await self.send("DOM.enable")
            return True
        except Exception as e:
            print(f"[CDP] Failed to switch to target {target_id}: {e}")
            return False

    async def close_target(self, target_id: str) -> bool:
        """
        Closes a specific tab / target by ID.
        """
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers={"Host": f"localhost:{self.port}"})
        try:
            async with self.session.put(f"http://127.0.0.1:{self.port}/json/close/{target_id}") as resp:
                return resp.status == 200
        except Exception:
            return False

    async def find_application_target(self, original_target_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Checks for any open tab or iframe target that matches an Indeed application flow:
        - smartapply.indeed.com
        - apply.indeed.com
        - indeed.com/apply
        - indeed.com/beta/indeedapply
        - or newly opened tab distinct from original_target_id
        """
        targets = await self.get_all_targets()
        app_patterns = [
            "smartapply.indeed.com",
            "apply.indeed.com",
            "/apply/",
            "/beta/indeedapply",
            "smartapply"
        ]

        # 1. Check for specific application URLs across page and iframe targets
        for t in targets:
            url = t.get("url", "").lower()
            if any(p in url for p in app_patterns):
                return t

        # 2. Check for any newly opened page target
        if original_target_id:
            for t in targets:
                if t.get("type") == "page" and t.get("id") != original_target_id:
                    url = t.get("url", "").lower()
                    if "indeed.com" in url or "about:blank" in url:
                        return t

        return None

    async def connect(self, target_url_match: Optional[str] = None, create_new: bool = True) -> bool:
        """
        Connects to Chrome CDP endpoint and attaches to an active tab or creates one.
        """
        if not await self.is_chrome_running():
            print(f"[CDP] Chrome is not responding on port {self.port}. Attempting launch...")
            self.launch_chrome_process()
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

        protected_hosts = [
            "localhost:8000", "127.0.0.1:8000",
            "localhost:8001", "127.0.0.1:8001",
            "localhost:8002", "127.0.0.1:8002",
            "chrome://", "devtools://"
        ]

        page_target = None
        if target_url_match:
            for t in targets:
                if t.get("type") == "page":
                    url = t.get("url", "")
                    if target_url_match in url and not any(ph in url for ph in protected_hosts):
                        page_target = t
                        break

        if not page_target:
            if create_new:
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

    async def click_at_point(self, x: float, y: float) -> bool:
        """
        Sends authentic native trusted CDP hardware-level mouse click events at (x, y) coordinates.
        """
        try:
            await self.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": float(x),
                "y": float(y)
            })
            await asyncio.sleep(0.05)
            await self.send("Input.dispatchMouseEvent", {
                "type": "mousePressed",
                "x": float(x),
                "y": float(y),
                "button": "left",
                "clickCount": 1
            })
            await asyncio.sleep(0.08)
            await self.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased",
                "x": float(x),
                "y": float(y),
                "button": "left",
                "clickCount": 1
            })
            return True
        except Exception as e:
            logger.warning(f"[CDP] click_at_point error: {e}")
            return False

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
        Sets files on an input[type="file"] element via CDP DOM.setFileInputFiles and fires React/DOM change events.
        """
        abs_path = os.path.abspath(file_path)
        if not os.path.exists(abs_path):
            print(f"[CDP] Upload failed: File not found at {abs_path}")
            return False

        try:
            node_id = None
            # 1. Preferred method: Query objectId via Runtime.evaluate, then convert to DOM nodeId
            try:
                eval_res = await self.send("Runtime.evaluate", {
                    "expression": f"document.querySelector('{selector}')",
                    "returnByValue": False
                })
                obj_id = eval_res.get("result", {}).get("objectId")
                if obj_id:
                    node_res = await self.send("DOM.requestNode", {"objectId": obj_id})
                    node_id = node_res.get("nodeId")
            except Exception as eval_err:
                logger.debug(f"[CDP] requestNode failed: {eval_err}")

            # 2. Fallback method: DOM.getDocument with deep pierce
            if not node_id:
                doc = await self.send("DOM.getDocument", {"depth": -1, "pierce": True})
                node_res = await self.send("DOM.querySelector", {
                    "nodeId": doc["root"]["nodeId"],
                    "selector": selector
                })
                node_id = node_res.get("nodeId")

            if not node_id:
                print(f"[CDP] File input selector not found: {selector}")
                return False

            # 3. Set files on DOM node
            await self.send("DOM.setFileInputFiles", {
                "files": [abs_path],
                "nodeId": node_id
            })
            print(f"[CDP] Successfully attached file to '{selector}': {abs_path}")

            # 4. CRITICAL FOR REACT: Dispatch input and change events so Indeed's React state registers the file
            await self.evaluate(f"""
            (() => {{
                const el = document.querySelector('{selector}');
                if (el) {{
                    el.dispatchEvent(new Event('input', {{ bubbles: true, cancelable: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true, cancelable: true }}));
                    return true;
                }}
                return false;
            }})()
            """)

            return True
        except Exception as e:
            print(f"[CDP] Error in upload_file: {e}")
            return False

    async def check_for_security_challenge(self) -> Dict[str, Any]:
        """
        Inspects page DOM for blocking challenges:
        - Cloudflare verification screens (#challenge-stage, 'Just a moment...', turnstile)
        - Visible reCAPTCHA / hCaptcha / Arkose / DataDome puzzle popups
        - Indeed Bot challenge / verification pages (/challenge, /captcha, etc.)
        - Visible OTP / 2FA verification inputs
        - Visible login gates
        """
        js = """
        (() => {
            const url = window.location.href.toLowerCase();
            const title = (document.title || '').toLowerCase();
            const text = (document.body ? document.body.innerText : '').toLowerCase();

            // 1. Cloudflare Turnstile / Bot challenge
            const cf = (() => {
                const stage = document.querySelector('#challenge-stage, #challenge-running, .cf-turnstile, #cf-wrapper, #challenge-form, iframe[src*="challenges.cloudflare.com"]');
                if (stage && stage.offsetParent !== null) return true;
                if ((title.includes('just a moment') || title.includes('attention required') || title.includes('security check') || title.includes('verify you are human')) &&
                    (text.includes('verifying you are human') || text.includes('enable javascript and cookies') || text.includes('cf-chl-widget') || text.includes('cloudflare'))) {
                    return true;
                }
                return false;
            })();

            // 2. Visible reCAPTCHA / hCaptcha / Arkose / DataDome Challenge Popup
            const recaptcha = (() => {
                const iframes = Array.from(document.querySelectorAll('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], iframe[src*="arkose"], iframe[src*="datadome"], iframe[src*="captcha-delivery"], iframe[title*="recaptcha"], iframe[title*="challenge"]'));
                for (const f of iframes) {
                    const rect = f.getBoundingClientRect();
                    const isVisible = f.offsetParent !== null && rect.width > 50 && rect.height > 50 && window.getComputedStyle(f).visibility !== 'hidden' && window.getComputedStyle(f).display !== 'none';
                    const isChallengeFrame = (f.src || '').includes('/bframe') || (f.src || '').includes('challenge') || (f.src || '').includes('captcha') || (f.title || '').toLowerCase().includes('challenge');
                    if (isVisible && isChallengeFrame) return true;
                }
                
                const puzzle = document.querySelector('#arkose, [class*="captcha-modal"], [class*="geetest"], [class*="slider-captcha"], div.g-recaptcha, div.h-captcha');
                if (puzzle && puzzle.offsetParent !== null && puzzle.clientHeight > 50) return true;
                return false;
            })();

            // 3. Indeed Bot Verification / Captcha Page
            const indeedChallenge = (() => {
                if (url.includes('/challenge') || url.includes('/captcha') || url.includes('captcha-delivery') || url.includes('secure.indeed.com/auth/verify')) return true;
                if ((text.includes("let us know you're not a robot") || text.includes("enter the characters you see below") || text.includes("please solve this puzzle") || text.includes("security verification required")) &&
                    document.querySelector('form, input[type="text"], iframe')) {
                    return true;
                }
                return false;
            })();

            // 4. OTP / Verification required
            const otp = (() => {
                const otpInput = document.querySelector('input[name*="otp"], input[id*="otp"], input[autocomplete="one-time-code"]');
                if (otpInput && otpInput.offsetParent !== null) return true;
                if ((text.includes('enter the 6-digit') || text.includes('enter otp') || text.includes('one time password') || text.includes('verification code')) &&
                    document.querySelector('input[type="number"], input[type="text"][maxlength="6"], input[type="text"][maxlength="4"]')) {
                    return true;
                }
                return false;
            })();

            // 5. Login Required Gate
            const login_required = (() => {
                const hasLoginModal = document.querySelector('[class*="login-modal"], form[action*="login"], div[class*="login-layer"]');
                if (hasLoginModal && hasLoginModal.offsetParent !== null) return true;

                if (url.includes('/account/login') || url.includes('secure.indeed.com/auth') || (url.includes('/login') && !document.querySelector('header, nav, [class*="profile"]'))) {
                    return true;
                }
                return false;
            })();

            const isBlocked = Boolean(cf || recaptcha || indeedChallenge || otp || login_required);

            return {
                has_challenge: isBlocked,
                cloudflare: Boolean(cf),
                recaptcha: Boolean(recaptcha),
                indeed_challenge: Boolean(indeedChallenge),
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

    async def pause_and_wait_for_human(self, reason: str, check_interval: float = 1.5, timeout: float = 300.0) -> bool:
        """
        Pauses automation when a security challenge or CAPTCHA appears.
        Plays warning sound alert, sends desktop notification, actively monitors the DOM,
        and WAITS FOR THE CAPTCHA TO COMPLETELY DISAPPEAR before resuming continuously.
        """
        from src.notifier import notify_attention_required, notify_captcha_cleared

        print("\n" + "=" * 70)
        print("🚨 [HUMAN ACTION REQUIRED] CAPTCHA / SECURITY CHALLENGE DETECTED")
        print(f"Reason: {reason}")
        print("👉 Please switch to your visible Chrome browser window and solve the CAPTCHA.")
        print("   The automation is paused and waiting for the challenge to disappear...")
        print("=" * 70 + "\n")

        # Sound chime & desktop popup notification
        notify_attention_required(reason)

        start = time.time()
        while time.time() - start < timeout:
            await asyncio.sleep(check_interval)
            status = await self.check_for_security_challenge()

            # If challenge has disappeared
            if not status.get("has_challenge", False):
                # Stabilization confirmation check: wait 1.5s and verify it didn't flash/reappear
                await asyncio.sleep(1.5)
                recheck = await self.check_for_security_challenge()
                if not recheck.get("has_challenge", False):
                    print("✅ [CDP] CAPTCHA / Verification completely cleared! Resuming Indeed automation workflow...\n")
                    notify_captcha_cleared()
                    await asyncio.sleep(1.5)
                    return True

        print("⚠️ [CDP] Timed out waiting for CAPTCHA resolution (5 minutes).")
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
