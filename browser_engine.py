"""
🌐 BROWSER ENGINE — trình duyệt (Playwright/Edge CDP) cho các domain KHÔNG có
API client riêng: MM88, RR88, XX88, O8, GG88.

Đây là bản khôi phục/thu gọn từ bot đời trước (trước khi migrate sang
browser-only — giữ lại phần cần cho các domain cấu hình:
  - Kết nối Edge đang chạy sẵn qua CDP (KHÔNG launch Chromium riêng)
  - TabPool: mỗi domain giữ (các) tab riêng, không tự mở tràn lan
  - Tìm ô nhập tài khoản/code, bấm nút submit, đọc kết quả (nhiều tầng
    fallback: selector riêng domain → selector chung SweetAlert/toast →
    quét từ khoá toàn trang → diff text trước/sau khi bấm)
  - Chụp màn hình + lưu HTML khi kết quả không rõ ràng (SCREENSHOT_ON_UNKNOWN)
    để dễ debug khi giao diện site đã đổi so với lúc code cũ chạy.

Module này CỐ TÌNH không import main_script.py ở cấp module (tránh import
vòng, vì main_script.py phải import module này để gọi). Vài hàm dùng
DEFERRED IMPORT (import main_script bên trong thân hàm) để gọi ngược lại
các tiện ích đã có sẵn ở đó (append_code_history, client Telegram) — an
toàn vì lúc các hàm này thực sự được GỌI, main_script đã import xong.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes
import gc
import re as _re
import time
import weakref
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from config import Config
from logger_setup import logger
from media_helpers import take_result_screenshot
from submission_outcomes import record_outcome, classify_result, ResultStatus
from dashboard import update_latency
from browser_site_profiles import browser_domains, get_site_profile

# ============================================================
# DOMAIN SCOPE
# Browser automation remains on one asyncio event loop. Do not call
# Playwright Page/Browser objects from raw threading.Thread workers; the
# TabPool locks and per-domain async workers provide safe parallelism instead.
# ============================================================
BROWSER_DOMAINS = browser_domains()


def _normalize_domain(url: str) -> str:
    p = urlparse(url or "")
    return (p.netloc or p.path).lower().replace("www.", "").strip("/")


def _page_matches_target(page_url: str, target_url: str, domain: str = "") -> bool:
    """Require both the host and the configured path for warm-tab reuse.

    Checking only ``domain in page.url`` incorrectly treated GG88's homepage
    as the code form, so a warm tab at ``/`` skipped navigation to
    ``/nhap-code``.
    """
    try:
        current = urlparse(page_url or "")
        target = urlparse(target_url or "")
        current_host = (current.hostname or "").lower().removeprefix("www.")
        target_host = (target.hostname or domain or "").lower().removeprefix("www.")
        if not current_host or current_host != target_host:
            return False
        target_path = (target.path or "/").rstrip("/") or "/"
        current_path = (current.path or "/").rstrip("/") or "/"
        return target_path == "/" or current_path == target_path
    except Exception:
        return False


def _append_code_history_safe(**kwargs):
    """Deferred import wrapper — gọi append_code_history() thật của
    main_script.py mà không cần import nó ở cấp module."""
    try:
        import sys
        _ms = sys.modules.get("__main__")
        if _ms is None or not hasattr(_ms, "append_code_history"):
            import main_script as _ms
        _ms.append_code_history(**kwargs)
    except Exception as e:
        logger.debug(f"⚠️ [Browser] append_history lỗi: {e}")


# ============================================================
# STATE
# ============================================================
class BrowserState:
    def __init__(self):
        self.account_pages: dict = {}       # key "domain|user" -> Page
        self.context_locks: dict = {}
        self.cf_verified: dict = {}
        self.submission_count: dict = {}
        self._input_cache: dict = {}
        # Tab nóng giữ cùng form trong nhiều lượt; nếu React thay DOM thì
        # REACT_FILL_VERIFY_JS sẽ phát hiện handle stale và tự invalidate.
        self._input_cache_ttl: float = 20.0
        self._submits_since_full_reload: dict = {}
        self.is_running = True


bot_state = BrowserState()


def shutdown():
    bot_state.is_running = False


# ============================================================
# SELECTORS
# ============================================================
def _get_domain_username_selectors(domain: str) -> list:
    profile = get_site_profile(domain)
    return list(profile.username_selectors) if profile else []


def _get_domain_result_selectors(domain: str) -> list:
    profile = get_site_profile(domain)
    return list(profile.result_selectors) if profile else []


def _site_profile_value(domain: str, name: str, default):
    profile = get_site_profile(domain)
    return getattr(profile, name, default) if profile else default


CF_SELECTORS = [
    "iframe[src*='turnstile']",
    "iframe[src*='challenges.cloudflare.com']",
    ".cf-turnstile",
    "[data-sitekey]",
]

_CF_STATE_JS = """
() => {
    const text = (document.body.innerText || '').toLowerCase();
    const token = [...document.querySelectorAll(
        'textarea[name="g-recaptcha-response"], textarea[name*="recaptcha"], '
        + 'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], '
        + '[data-cf-turnstile-response]'
    )].some((el) => String(
        el.value || el.textContent || el.getAttribute('data-cf-turnstile-response') || ''
    ).trim().length > 20);
    if (token || ['xác thực thành công', 'xac thuc thanh cong', 'verification successful']
        .some((marker) => text.includes(marker))) {
        return false;
    }
    const visible = (el) => {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden';
    };
    if ([...document.querySelectorAll('button')].some((button) => {
        const label = (button.innerText || button.textContent || '').trim();
        return /^(xác thực|xac thuc|verify)$/i.test(label) && visible(button);
    })) return true;
    const loadFailed = ['không tải được captcha', 'khong tai duoc captcha', 'thử lại', 'thu lai']
        .some((marker) => text.includes(marker))
        && ['captcha', 'turnstile', 'cloudflare'].some((marker) => text.includes(marker));
    if (loadFailed) return true;
    return [...document.querySelectorAll(
        "iframe[src*='turnstile'], iframe[src*='challenges.cloudflare.com'], .cf-turnstile, [data-sitekey]"
    )].some(visible);
}
"""

REACT_FILL_JS = """
    ([el, val]) => {
        const proto = el.tagName === 'TEXTAREA'
            ? window.HTMLTextAreaElement.prototype
            : window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        el.focus();
        setter.call(el, '');
        setter.call(el, val);
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
    }
"""

REACT_FILL_VERIFY_JS = """
    ([userEl, codeEl, userVal, codeVal, fillUser]) => {
        const setVal = (el, val) => {
            const proto = el.tagName === 'TEXTAREA'
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            el.focus();
            setter.call(el, '');
            setter.call(el, val);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        };
        if (userEl && fillUser) setVal(userEl, userVal);
        setVal(codeEl, codeVal);
        return {
            actualUser: userEl ? userEl.value : null,
            actualCode: codeEl.value,
        };
    }
"""

_MANUAL_VERIFY_KEYWORDS = [
    "mã xác thực", "ma xac thuc",
    "nhập đúng mã trong ảnh", "nhap dung ma trong anh",
    "hoàn tất xác minh", "hoan tat xac minh",
    "nhập mã xác nhận", "nhap ma xac nhan",
    "kéo thanh trượt", "keo thanh truot",
    "hoàn thành ghép", "hoan thanh ghep",
]


def _needs_manual_verify(text: str) -> bool:
    if not text:
        return False
    low = text.strip().lower()
    return any(k in low for k in _MANUAL_VERIFY_KEYWORDS)


# ============================================================
# EDGE CDP CONNECT / LAUNCH
# ============================================================
_pw_instance = None
_edge_browser = None
_shared_context = None
_browser_lock = None
_last_launch_time: float = 0.0
_LAUNCH_COOLDOWN = 15.0


def _get_launch_lock():
    global _browser_lock
    if _browser_lock is None:
        _browser_lock = asyncio.Lock()
    return _browser_lock




def _get_edge_bot_profile_dir() -> str:
    custom_dir = getattr(Config, "EDGE_PROFILE_DIR", "") or ""
    if custom_dir.strip():
        return custom_dir.strip()
    return str(Path(__file__).resolve().parent / "edge_bot_profile")


def _launch_edge_debug(cdp_port: int) -> bool:
    exe = getattr(Config, "EDGE_EXECUTABLE_PATH", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

    if not Path(exe).exists():
        alt = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
        if Path(alt).exists():
            logger.warning(f"⚠️ [Edge-CDP] EDGE_EXECUTABLE_PATH không tồn tại ({exe}) — dùng: {alt}")
            exe = alt
        else:
            logger.critical(
                f"❌ [Edge-CDP] Không tìm thấy msedge.exe ở cả 2 vị trí:\n"
                f"   - {exe}\n   - {alt}\n👉 Kiểm tra lại EDGE_EXECUTABLE_PATH trong .env"
            )
            return False

    profile_dir = _get_edge_bot_profile_dir()
    profile_name = getattr(Config, "EDGE_PROFILE_NAME", "") or "Default"

    try:
        import subprocess
        subprocess.Popen(
            [
                exe,
                f"--remote-debugging-port={cdp_port}",
                "--remote-debugging-address=127.0.0.1",
                f"--remote-allow-origins=http://localhost:{cdp_port},http://127.0.0.1:{cdp_port}",
                "--disable-blink-features=AutomationControlled",
                f"--user-data-dir={profile_dir}",
                f"--profile-directory={profile_name}",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--disable-component-update",
                "--disable-domain-reliability",
                "--disable-client-side-phishing-detection",
                "--disable-default-apps",
                "--no-first-run",
                "--no-default-browser-check",
                "--mute-audio",
                # Tab nền bị Chromium throttle timer (~1s/lần) → polling chờ kết
                # quả bị trễ. Tắt throttle cho mọi tab.
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--disable-features=Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider,AutofillServerCommunication,CalculateNativeWinOcclusion",
            ],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        return True
    except Exception as e:
        logger.error(f"❌ [Edge-CDP] Không tự mở lại được Edge ({exe}): {e}")
        return False


async def get_or_launch_browser_context(user: str = "shared", force_reconnect: bool = False):
    global _pw_instance, _edge_browser, _shared_context, _last_launch_time

    if not force_reconnect and _shared_context is not None:
        try:
            _ = _shared_context.pages
            return _shared_context
        except Exception:
            _shared_context = None

    async with _get_launch_lock():
        if not force_reconnect and _shared_context is not None:
            try:
                _ = _shared_context.pages
                return _shared_context
            except Exception:
                _shared_context = None

        if _pw_instance is None:
            _pw_instance = await async_playwright().start()

        cdp_host = getattr(Config, "EDGE_CDP_HOST", "127.0.0.1")
        cdp_port = getattr(Config, "EDGE_CDP_PORT", 9222)
        cdp_url = f"http://{cdp_host}:{cdp_port}"

        logger.info(f"[Edge-CDP] Đang kết nối vào Edge đang chạy tại {cdp_url}...")
        _edge_browser = None
        last_err = None

        now = time.time()
        just_launched = (now - _last_launch_time) < _LAUNCH_COOLDOWN
        max_attempts = 1 if just_launched else 2

        for attempt in range(1, max_attempts + 1):
            try:
                _edge_browser = await _pw_instance.chromium.connect_over_cdp(cdp_url, timeout=15000)
                break
            except Exception as e:
                last_err = e
                if just_launched:
                    wait_left = max(1.0, _LAUNCH_COOLDOWN - (time.time() - _last_launch_time))
                    logger.warning(
                        f"⚠️ [Edge-CDP] Kết nối thất bại nhưng Edge vừa được mở lại gần đây "
                        f"({wait_left:.0f}s trước) — ĐỢI THÊM thay vì kill lại: {e}"
                    )
                    await asyncio.sleep(min(wait_left + 3.0, 15.0))
                    just_launched = False
                    continue

                logger.warning(
                    f"⚠️ [Edge-CDP] Kết nối thất bại (lần {attempt}/{max_attempts}): {e} — "
                    f"không đóng các tiến trình Edge hiện có; thử mở Edge debug riêng..."
                )
                await asyncio.sleep(1)
                if _launch_edge_debug(cdp_port):
                    _last_launch_time = time.time()
                    logger.info("[Edge-CDP] Đã gửi lệnh mở lại Edge — chờ khởi động...")
                    await asyncio.sleep(10)

        if _edge_browser is None:
            logger.critical(
                f"❌ [Edge-CDP] Không kết nối được tới Edge ({cdp_url}) sau khi đã thử tự mở lại: {last_err}\n"
                f"👉 Có thể do: Edge bị chặn bởi firewall/antivirus trên cổng {getattr(Config, 'EDGE_CDP_PORT', 9222)}, "
                f"hoặc \"Tiếp tục chạy ứng dụng nền\" đang bật trong edge://settings/system."
            )
            raise last_err if last_err else RuntimeError("Edge CDP connect failed")

        if not _edge_browser.contexts:
            logger.error(
                "❌ [Edge-CDP] Kết nối được nhưng Edge không có context/tab nào đang mở — "
                "mở ít nhất 1 tab trong Edge trước khi chạy bot."
            )
            raise RuntimeError("Edge CDP: no existing browser context")

        _shared_context = _edge_browser.contexts[0]
        logger.info(f"[Edge-CDP] ✅ Đã kết nối — dùng context hiện có ({len(_shared_context.pages)} tab đang mở)")
        return _shared_context


_browser_hwnd: int = 0
_last_restore_at: float = 0.0


def _find_browser_hwnd() -> int:
    global _browser_hwnd
    if _browser_hwnd:
        if ctypes.windll.user32.IsWindow(_browser_hwnd):
            return _browser_hwnd
        _browser_hwnd = 0

    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    def _cb(hwnd, _):
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.lower()
        if any(k in title for k in ("edge", "microsoft edge", "chrome")):
            rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w > 100 and h > 50:
                found.append((w * h, hwnd))
        return True

    ctypes.windll.user32.EnumWindows(WNDENUMPROC(_cb), 0)
    if not found:
        return 0
    found.sort(key=lambda x: x[0], reverse=True)
    _browser_hwnd = found[0][1]
    return _browser_hwnd


def edge_restore():
    global _last_restore_at
    try:
        now = time.monotonic()
        if now - _last_restore_at < 2.0:
            return
        _last_restore_at = now
        hwnd = _find_browser_hwnd()
        if hwnd:
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            # Chỉ bỏ trạng thái minimize. Không gọi BringWindowToTop hoặc
            # SetForegroundWindow: người dùng vẫn có thể làm việc ở ứng dụng
            # khác; page.bring_to_front() sẽ chọn đúng tab bên trong Edge.
            logger.debug("🔼 Edge restored; active tab selected by Playwright")
    except Exception as e:
        logger.debug(f"browser_restore error: {e}")


# ============================================================
# CLOUDFLARE DETECTION
# ============================================================
async def _cf_already_passed(page, domain: str = "") -> bool:
    """Return True only when the page exposes a verified signal.

    Turnstile/reCAPTCHA runs in a cross-origin iframe, so the reliable signal
    available to the host page is either a non-empty response token or an
    explicit verified/success marker. This function only observes state; it
    never clicks a challenge or tries to solve it.
    """
    try:
        state = await page.evaluate(
            """
            () => {
                const text = (document.body.innerText || '').toLowerCase();
                const successMarkers = [
                    'xác thực thành công', 'xac thuc thanh cong',
                    'verification successful', 'verified',
                ];
                const token = [...document.querySelectorAll(
                    'textarea[name="g-recaptcha-response"], textarea[name*="recaptcha"], '
                    + 'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], '
                    + '[data-cf-turnstile-response]'
                )].some((el) => String(
                    el.value || el.textContent || el.getAttribute('data-cf-turnstile-response') || ''
                ).trim().length > 20);
                const marker = successMarkers.some((value) => text.includes(value));
                return {token, marker};
            }
            """
        )
        return bool(state and (state.get("token") or state.get("marker")))
    except Exception:
        return False


async def _wait_for_cloudflare_passed_and_cleanup(page, domain: str = "", timeout_seconds: float | None = None) -> bool:
    """Wait for a verified token/marker, then close only non-verification popups."""
    wait_seconds = float(
        timeout_seconds
        if timeout_seconds is not None
        else getattr(Config, "VERIFICATION_BUTTON_WAIT_SECONDS", 3.0)
    )
    try:
        # Keep the polling inside the page. The previous Python loop made up
        # to 20 CDP round-trips per second while Turnstile was completing.
        handle = await page.wait_for_function(
            """
            () => {
                const text = (document.body.innerText || '').toLowerCase();
                const marker = [
                    'xác thực thành công', 'xac thuc thanh cong',
                    'verification successful', 'verified',
                ].some((value) => text.includes(value));
                const token = [...document.querySelectorAll(
                    'textarea[name="g-recaptcha-response"], textarea[name*="recaptcha"], '
                    + 'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], '
                    + '[data-cf-turnstile-response]'
                )].some((el) => String(
                    el.value || el.textContent || el.getAttribute('data-cf-turnstile-response') || ''
                ).trim().length > 20);
                return token || marker;
            }
            """,
            timeout=min(8000.0, max(500.0, wait_seconds * 1000.0)),
            polling=100,
        )
        try:
            await handle.dispose()
        except Exception:
            pass
    except Exception:
        return False
    await _close_unwanted_popups(page)
    return True


async def is_cloudflare_present(page, domain: str = "") -> bool:
    try:
        return bool(await page.evaluate(_CF_STATE_JS))
    except Exception:
        return False


async def safe_is_visible(element) -> bool:
    try:
        return await element.is_visible()
    except Exception:
        return False


def safe_is_closed(page) -> bool:
    try:
        if page is None:
            return True
        return page.is_closed()
    except Exception:
        return True


# ============================================================
# INPUT FIELDS
# ============================================================
def _invalidate_input_cache(key: str):
    bot_state._input_cache.pop(key, None)


def _invalidate_page_input_cache(page) -> None:
    suffix = f"|{id(page)}"
    for cache_key in [key for key in list(bot_state._input_cache) if key.endswith(suffix)]:
        bot_state._input_cache.pop(cache_key, None)


async def find_input_fields(page, cache_key: str = None, domain: str = ""):
    now = time.time()

    if cache_key:
        cached = bot_state._input_cache.get(cache_key)
        if cached:
            username_input, code_input, cache_time = cached
            if now - cache_time < bot_state._input_cache_ttl:
                # Page/DOM rerender được xử lý ở lớp fill: nếu JS handle đã
                # detached, REACT_FILL_VERIFY_JS sẽ throw và submit path sẽ
                # invalidate rồi tìm selector lại. Không gọi is_visible()
                # ở đây nữa vì đó là thêm một round-trip CDP trên mọi submit.
                if code_input:
                    return username_input, code_input
                _invalidate_input_cache(cache_key)

    username_input = None
    code_input = None
    domain_username_selectors = _get_domain_username_selectors(domain)

    username_selectors = domain_username_selectors + [
        "#account-code", "#username-input", "#ten_tai_khoan",
        "input#username", "input[name='username']",
        "input[placeholder*='người dùng' i]", "input[placeholder*='tên' i]",
        "input[placeholder*='tài' i]", "input[placeholder*='tài khoản' i]",
        "input[placeholder*='user' i]", "input[placeholder*='đăng nhập' i]",
        "input[name='ten_tai_khoan']", "input[id='username']", "input[type='text']",
    ]

    profile = get_site_profile(domain)
    domain_code_selectors = list(profile.code_selectors) if profile else []
    code_selectors = domain_code_selectors + [
        "#enter-code-code", "#promo-code", "#giftcode-input", "input[placeholder='Nhập mã']", "input[autocomplete='one-time-code']",
        "input#code", "input[name='code']", "input[placeholder*='mã code' i]",
        "input[placeholder*='code' i]", "input[placeholder*='mã' i]",
        "input[name='giftcode']", "input[id='code']", "input[id*='code' i]", "input[id*='promo' i]",
    ]

    try:
        selector_result = await page.evaluate(
            """
            ({usernameSelectors, codeSelectors}) => {
                const visible = (el) => {
                    if (!el || el.disabled) return false;
                    const s = getComputedStyle(el), r = el.getBoundingClientRect();
                    return s.display !== 'none' && s.visibility !== 'hidden' &&
                           r.width > 0 && r.height > 0;
                };
                const first = (selectors) => {
                    for (const sel of selectors) {
                        try {
                            const el = document.querySelector(sel);
                            if (visible(el)) return sel;
                        } catch (_) {}
                    }
                    return null;
                };
                return {username: first(usernameSelectors), code: first(codeSelectors)};
            }
            """,
            {"usernameSelectors": username_selectors, "codeSelectors": code_selectors},
        )
        if selector_result:
            if selector_result.get("username"):
                username_input = await page.query_selector(selector_result["username"])
            if selector_result.get("code"):
                code_input = await page.query_selector(selector_result["code"])

        if not username_input or not code_input:
            inputs = await page.query_selector_all(
                "input:not([type='hidden']):not([type='checkbox']):not([type='radio']):not([type='submit'])"
            )
            visible_inputs = []
            for inp in inputs:
                if await safe_is_visible(inp):
                    visible_inputs.append(inp)
            if len(visible_inputs) >= 2:
                if not username_input:
                    username_input = visible_inputs[0]
                if not code_input:
                    code_input = visible_inputs[1]
            elif len(visible_inputs) == 1 and not code_input:
                code_input = visible_inputs[0]

    except Exception as e:
        logger.debug(f"⚠️ Error finding input fields: {e}")

    if cache_key and code_input:
        bot_state._input_cache[cache_key] = (username_input, code_input, now)

    return username_input, code_input


async def scroll_to_input_fields(page):
    try:
        found = await page.evaluate(
            """
            () => {
                const inputs = document.querySelectorAll('input[type="text"], input:not([type="hidden"])');
                if (inputs.length > 0) {
                    const firstInput = inputs[0];
                    firstInput.scrollIntoView({behavior: 'auto', block: 'center'});
                    firstInput.focus();
                    return true;
                }
                return false;
            }
            """
        )
        return found
    except Exception as e:
        logger.debug(f"⚠️ Scroll error: {e}")
        return False


async def open_mm88_code_form(page) -> bool:
    """MM88 may land on its home shell before exposing the code form."""
    try:
        clicked = await page.evaluate(
            """
            () => {
                const nodes = [...document.querySelectorAll('a,button,[role="button"],span')];
                const target = nodes.find((el) => {
                    const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                    return text === 'nhập code' || text === 'nhap code';
                });
                if (!target) return false;
                const clickable = target.closest('a,button,[role="button"]') || target;
                clickable.click();
                return true;
            }
            """
        )
        if not clicked:
            return False
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        await asyncio.sleep(0.05)
        return True
    except Exception as e:
        logger.debug(f"⚠️ MM88 Nhập Code navigation lỗi: {e}")
        return False


# ============================================================
# SUBMIT BUTTON CLICKING
# ============================================================
async def _response_to_result_text(response) -> str:
    try:
        payload = await response.json()
    except Exception:
        try:
            return (await response.text()).strip()
        except Exception:
            return ""

    # Các site không thống nhất tên trường response: có site dùng message,
    # site khác dùng msg/detail/result/content hoặc lồng trong data/payload.
    # Thu thập các giá trị nguyên thủy trong JSON giúp bộ phân loại đọc được
    # kết quả ngay cả khi popup DOM không xuất hiện.
    preferred_keys = (
        "message",
        "msg",
        "error",
        "detail",
        "result",
        "status",
        "content",
        "description",
    )
    parts: list[str] = []

    def collect(value, depth: int = 0) -> None:
        if value is None or depth > 3:
            return
        if isinstance(value, dict):
            # Ưu tiên các trường có khả năng chứa thông báo kết quả.
            for key in preferred_keys:
                if key in value:
                    collect(value[key], depth + 1)
            # Vẫn duyệt các trường còn lại để hỗ trợ data/payload lồng nhau.
            for key, item in value.items():
                if key not in preferred_keys:
                    collect(item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value[:20]:
                collect(item, depth + 1)
        elif isinstance(value, (str, int, float, bool)):
            text = str(value).strip()
            if text and text not in parts:
                parts.append(text)

    collect(payload)
    return " ".join(parts)[:4000]


# ============================================================
# TURNSTILE SOLVED DETECTION + PENDING-VERIFICATION WATCHER
# ============================================================
_TURNSTILE_HOST_STATE_JS = """
() => {
    const visible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
    };
    const hasWidget = [...document.querySelectorAll(
        "iframe[src*='turnstile'], iframe[src*='challenges.cloudflare.com'], .cf-turnstile, [data-sitekey]"
    )].some(visible);
    const token = [...document.querySelectorAll(
        'textarea[name="g-recaptcha-response"], textarea[name*="recaptcha"], '
        + 'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], '
        + '[data-cf-turnstile-response]'
    )].some((el) => String(
        el.value || el.textContent || el.getAttribute('data-cf-turnstile-response') || ''
    ).trim().length > 20);
    return {hasWidget, token};
}
"""

_PENDING_VERIFY_TASKS: dict = {}


async def _turnstile_solved(page) -> bool:
    """True khi Turnstile đã xong (hoặc không có widget nào để chờ)."""
    try:
        state = await page.evaluate(_TURNSTILE_HOST_STATE_JS)
    except Exception:
        return False
    if not state:
        return False
    if state.get("token") or not state.get("hasWidget"):
        return True
    # Widget nằm trong iframe cross-origin: host DOM không đọc được chữ
    # "Thành công!", nhưng Playwright đọc được qua page.frames.
    try:
        for frame in page.frames:
            if "challenges.cloudflare.com" not in (frame.url or ""):
                continue
            text = await frame.evaluate(
                "() => ((document.body && document.body.innerText) || '')"
            )
            low = str(text or "").lower()
            if any(m in low for m in ("thành công", "thanh cong", "success", "verified")):
                return True
    except Exception:
        pass
    return False


async def _wait_turnstile_solved(page, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        if safe_is_closed(page):
            return False
        if await _turnstile_solved(page):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.15)


async def _click_verify_button_now(page) -> bool:
    try:
        btn = page.get_by_role(
            "button", name=_re.compile(r"^\s*(xác thực|xac thuc|verify)\s*$", _re.I)
        ).last
        if await btn.count() and await btn.is_visible() and await btn.is_enabled():
            await btn.click(timeout=2000, no_wait_after=True)
            return True
    except Exception:
        pass
    try:
        return bool(await page.evaluate(
            """() => {
                const el = [...document.querySelectorAll('button,[role="button"]')].find((b) => {
                    const t = (b.innerText || b.textContent || '').trim().toLowerCase();
                    const r = b.getBoundingClientRect();
                    return /^(xác thực|xac thuc|verify)$/.test(t) && r.width > 0 && !b.disabled;
                });
                if (!el) return false;
                el.click();
                return true;
            }"""
        ))
    except Exception:
        return False


def arm_verification_watcher(page, domain: str = "", code: str = "", user: str = "") -> None:
    """Chạy nền: khi Turnstile của page đang giữ xong thì tự bấm "Xác thực".

    Item đã kết thúc để nhả worker, nên không ai còn bấm nút này nữa nếu
    Turnstile chỉ hoàn tất SAU khi bot bỏ cuộc. Watcher không giữ worker.
    """
    if page is None or safe_is_closed(page):
        return
    key = id(page)
    old = _PENDING_VERIFY_TASKS.get(key)
    if old is not None and not old.done():
        return

    async def _run():
        tag = f"[Verify-Watch|{domain}|{user}|{code}]"
        try:
            watch_s = float(getattr(Config, "PENDING_VERIFY_WATCH_SECONDS", 90.0))
            if not await _wait_turnstile_solved(page, watch_s):
                logger.warning(f"⌛ {tag} Turnstile chưa xong sau {watch_s:.0f}s — bỏ theo dõi")
                return
            if not await _click_verify_button_now(page):
                logger.warning(f"⚠️ {tag} Turnstile xong nhưng không thấy nút Xác thực")
                return
            logger.info(f"✅ {tag} Turnstile xong → đã bấm Xác thực")
            await asyncio.sleep(2.5)
            try:
                body = await page.evaluate(
                    "() => ((document.querySelector('[role=dialog],.modal,main,#app,body')"
                    " || document.body).innerText || '').slice(0, 600)"
                )
            except Exception:
                body = ""
            logger.info(f"📋 {tag} kết quả hiển thị: {str(body).strip()[:200]!r}")
            await _close_unwanted_popups(page)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"⚠️ {tag} watcher lỗi: {exc}")
        finally:
            if _PENDING_VERIFY_TASKS.get(key) is asyncio.current_task():
                _PENDING_VERIFY_TASKS.pop(key, None)

    _PENDING_VERIFY_TASKS[key] = asyncio.create_task(_run())


async def click_verification_button_if_present(page, domain: str = "") -> bool:
    if domain not in {"tangquaqq88.com", "hi88-freecode.pages.dev"}:
        logger.debug(f"ℹ️ [{domain or 'unknown'}] Bỏ qua nút Xác thực: domain không yêu cầu")
        return False
    try:
        wait_seconds = min(
            8.0,
            max(1.0, float(getattr(Config, "VERIFICATION_BUTTON_WAIT_SECONDS", 8.0))),
        )
        # The challenge/modal is created asynchronously after the site's
        # primary submit button is clicked. Do not return on the first DOM
        # snapshot: that race was why QQ88/HI88 could show a visible "Xác
        # thực" button while the bot had already abandoned the click path.
        try:
            hint = await page.wait_for_function(
                """
                () => {
                    const challenge = document.querySelector(
                        '.cf-turnstile, [data-sitekey], iframe[src*="turnstile"], '
                        + 'iframe[src*="challenges.cloudflare.com"], iframe[src*="recaptcha"]'
                    );
                    if (challenge) return true;
                    return [...document.querySelectorAll('button,[role="button"]')].some((el) => {
                        const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                        return /^(xác thực|xac thuc|verify)$/.test(text);
                    });
                }
                """,
                timeout=wait_seconds * 1000.0,
                polling=100,
            )
            try:
                await hint.dispose()
            except Exception:
                pass
        except Exception:
            return False

        # QUAN TRỌNG: chỉ bấm "Xác thực" khi Turnstile đã xong. Bấm sớm khiến site
        # trả "Hoàn tất xác minh bên dưới rồi bấm Xác thực" và bot bỏ cuộc,
        # trong khi widget chạy xong sau đó (modal "Thành công!" nằm chờ).
        solve_wait = float(getattr(Config, "TURNSTILE_SOLVE_WAIT_SECONDS", 8.0))
        if not await _wait_turnstile_solved(page, solve_wait):
            logger.debug(f"ℹ️ [{domain}] Turnstile chưa xong sau {solve_wait:.1f}s — chưa bấm Xác thực")
            return False

        # HI88 renders a regular React button in the modal. Prefer a real
        # Playwright click for this path so React receives the trusted click
        # event; the old evaluate-only path could be ignored by the site's
        # event delegation even when the button was visibly present. Do not
        # click merely because the button appeared: QQ88/HI88 can render it
        # while Turnstile is still loading.
        try:
            verify_button = page.locator("button").filter(has_text="Xác thực").last
            ready = await page.evaluate(
                """
                () => {
                    const visible = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0
                            && s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    const scopes = [...document.querySelectorAll(
                        '[role="dialog"], [role="alertdialog"], .modal, '
                        '[class*="modal" i], [class*="dialog" i], '
                        '[class*="captcha" i], [class*="turnstile" i]'
                    )].filter(visible);
                    const challengeText = scopes
                        .map((el) => el.innerText || el.textContent || '').join(' ')
                        .toLowerCase();
                    const loading = /đang (xác minh|kiểm tra|tải)|dang (xac minh|kiem tra|tai)|verifying|checking|loading/.test(challengeText);
                    const button = [...document.querySelectorAll(
                        'button, [role="button"], input[type="button"], input[type="submit"], '
                        '[tabindex="0"], img[alt*="xác thực" i], img[alt*="xac thuc" i], '
                        'img[alt*="verify" i]'
                    )].find((el) => {
                        const label = (el.innerText || el.textContent || el.value || el.alt || '')
                            .trim().toLowerCase();
                        return /^(xác thực|xac thuc|verify)$/.test(label)
                            && visible(el) && !el.disabled;
                    });
                    return Boolean(button && !loading);
                }
                """
            )
            if ready and await verify_button.count() and await verify_button.is_visible() and await verify_button.is_enabled():
                await verify_button.click(timeout=1500, no_wait_after=True)
                passed = await _wait_for_cloudflare_passed_and_cleanup(page, domain=domain)
                logger.info(
                    f"✅ [Browser|{domain}] Playwright đã click nút Xác thực; "
                    f"Cloudflare={'passed' if passed else 'pending'}"
                )
                return passed
        except Exception as exc:
            logger.debug(f"⚠️ [{domain}] Playwright click Xác thực chưa thành công: {exc}")

        clicked = bool(await page.evaluate(
            """async function (waitSeconds) {
                var deadline = Date.now() + (waitSeconds * 1000);
                var verifyTexts = ['x\\u00e1c th\\u1ef1c', 'xac thuc', 'verify'];

                function isVisible(el) {
                    if (!el) return false;
                    var rect = el.getBoundingClientRect();
                    var style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0
                        && style.display !== 'none'
                        && style.visibility !== 'hidden';
                }

                function cloudLoading() {
                    var scopes = document.querySelectorAll(
                        '[role="dialog"], [role="alertdialog"], .modal, ' +
                        '[class*="modal" i], [class*="dialog" i], ' +
                        '[class*="captcha" i], [class*="turnstile" i]'
                    );
                    var text = '';
                    for (var i = 0; i < scopes.length; i++) {
                        if (!isVisible(scopes[i])) continue;
                        text += ' ' + (scopes[i].innerText || scopes[i].textContent || '');
                    }
                    if (/thành công|success|verified|verification complete/i.test(text)) {
                        return false;
                    }
                    return /đang (xác minh|kiểm tra|tải)|verifying|checking|loading/i.test(text);
                }

                while (Date.now() < deadline) {
                    var buttons = document.querySelectorAll(
                        'button, [role="button"], input[type="button"], input[type="submit"], '
                        + '[tabindex="0"], img[alt*="xác thực" i], img[alt*="xac thuc" i], '
                        + 'img[alt*="verify" i]'
                    );
                    for (var j = 0; j < buttons.length; j++) {
                        var el = buttons[j];
                        var label = (el.innerText || el.textContent || el.value || el.alt || '').trim().toLowerCase();
                        if (verifyTexts.indexOf(label) === -1) continue;
                        if (!isVisible(el) || el.disabled || cloudLoading()) continue;
                        var target = el.closest('button,[role="button"],a,[tabindex="0"]') || el;
                        target.click();
                        return true;
                    }
                    await new Promise(function (resolve) { setTimeout(resolve, 100); });
                }
                return false;
            }""",
            wait_seconds,
        ))
        if not clicked:
            logger.debug(f"ℹ️ [{domain}] Không thấy nút Xác thực trong {wait_seconds}s")
            return False
        passed = await _wait_for_cloudflare_passed_and_cleanup(page, domain=domain)
        logger.info(
            f"✅ [Browser|{domain}] JavaScript đã click nút Xác thực; "
            f"Cloudflare={'passed' if passed else 'pending'}"
        )
        return passed
    except Exception as exc:
        logger.warning(f"⚠️ [{domain}] verification button check lỗi: {exc}")
        return False


async def click_submit_fast(page, domain: str = "") -> bool:
    profile = get_site_profile(domain)
    domain_sel = profile.submit_selector if profile else None
    if domain_sel:
        try:
            locator = page.locator(domain_sel).first
            # locator.click() already auto-waits for visibility/enabled state;
            # a separate wait_for() added one needless CDP round trip per submit.
            await locator.click(timeout=700)
            logger.debug(f"✅ Playwright-clicked domain-specific button: {domain}")
            return True
        except Exception:
            pass
        try:
            clicked = await page.evaluate(
                """
                async (sel) => {
                    const deadline = Date.now() + 300;
                    while (Date.now() < deadline) {
                        const btn = document.querySelector(sel);
                        if (btn && !btn.disabled) {
                            const rect = btn.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                btn.click();
                                return true;
                            }
                        }
                        await new Promise(r => setTimeout(r, 50));
                    }
                    const btn = document.querySelector(sel);
                    if (btn) { btn.click(); return true; }
                    return false;
                }
                """,
                domain_sel,
            )
            if clicked:
                logger.debug(f"✅ Clicked domain-specific button: {domain}")
                return True
        except Exception:
            pass

    try:
        clicked = await page.evaluate(
            """
            () => {
                const keywords = [
                    'kiểm tra ngay', 'kiem tra ngay', 'kiểm tra', 'kiem tra',
                    'nhận code', 'nhan code', 'nhận ngay', 'nhan ngay',
                    'áp dụng', 'ap dung', 'đổi code', 'doi code',
                    'nạp code', 'nap code', 'gửi', 'gui', 'submit', 'apply'
                ];
                const EXCLUDE = /menu|nav|home|close|cancel|toggle|hamburger|back|trở về|huỷ|hủy|đóng|xác thực|xac thuc|verify|check/i;
                const els = [...document.querySelectorAll(
                    'button, a[role="button"], div[role="button"], span[role="button"], input[type="button"], input[type="submit"]'
                )];
                for (const kw of keywords) {
                    for (const el of els) {
                        if (el.disabled) continue;
                        const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                        const img = el.querySelector('img[alt]');
                        const imgAlt = img ? (img.getAttribute('alt') || '').toLowerCase() : '';
                        const txt = (el.innerText || el.textContent || el.value || '').toLowerCase().trim();
                        if (EXCLUDE.test(aria + txt)) continue;
                        if ([txt, aria, imgAlt].some(s => s && s.includes(kw))) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                el.click();
                                return true;
                            }
                        }
                    }
                }
                return false;
            }
            """
        )
        if clicked:
            return True
    except Exception:
        pass

    generic_selectors = [
        "button[type='submit']", "input[type='submit']", ".btn-submit",
        ".apply-btn", ".submit-btn", "[class*='submit' i]", "[class*='apply' i]",
    ]
    for sel in generic_selectors:
        try:
            el = await page.query_selector(sel)
            if el and await safe_is_visible(el):
                await page.evaluate("el => el.click()", el)
                return True
        except Exception:
            pass

    try:
        await page.keyboard.press("Enter")
        return True
    except Exception:
        return False


# ============================================================
# RESULT DETECTION
# ============================================================
def _filter_nextjs_noise(text: str) -> str:
    if not text:
        return ""
    noise_markers = [
        "__next_f", "__NEXT", "self.__next", 'push([1,"', '"stylesheet"',
        '"link"', "webpack", "hydrat", '"rel":', '"href":', ':[[[\"$\"',
    ]
    t = text.strip()
    for marker in noise_markers:
        if marker in t:
            return ""
    if t.startswith(('{"', '[["', '[[["', "self.")):
        return ""
    return t


_TRANSIENT_CF_PATTERNS = [
    "captcha", "turnstile", "xác thực người dùng",
    "đang xử lý", "dang xu ly", "đang tải", "dang tai",
    "đang kiểm tra", "dang kiem tra", "checking",
    "vui lòng đợi", "vui long doi", "please wait",
    "processing", "verifying", "đang xác thực", "dang xac thuc",
    # Static HI88 page copy, not a submit response. Without filtering it,
    # the domain selector can return this text immediately after the form
    # submit and classify the attempt as AMBIGUOUS before the real result.
    "theo dõi nhận code", "theo doi nhan code",
]


def _is_transient_captcha_text(text: str) -> bool:
    if not text:
        return False
    low = text.strip().lower()
    if any(p in low for p in _TRANSIENT_CF_PATTERNS):
        return True
    BUTTON_ONLY_WORDS = {"hủy", "huy", "xác thực", "xac thuc", "đóng", "dong", "cancel", "verify", "ok", "close"}
    tokens = [t.strip() for t in _re.split(r"[\n/|,]+", low) if t.strip()]
    if tokens and len(low) <= 40 and all(t in BUTTON_ONLY_WORDS for t in tokens):
        return True
    return False




_DETECT_RESULT_JS = r"""
(args) => {
    const { orderedSelectors, combinedSelectors, includeGlobal } = args;
    const readSelector = (sel) => {
        try {
            const els = document.querySelectorAll(sel);
            const texts = [];
            for (const el of els) {
                if (!el.getClientRects().length) continue;
                const t = (el.innerText || el.textContent || '').trim();
                if (t) texts.push(t);
            }
            return texts.join(' ');
        } catch (e) {
            return '';
        }
    };
    const ordered = orderedSelectors.map(readSelector);
    const combinedParts = [];
    if (includeGlobal) {
        for (const sel of combinedSelectors) {
            const t = readSelector(sel);
            if (t) combinedParts.push(t);
        }
    }
    if (!includeGlobal) {
        return {ordered, combinedText: '', keywordText: '', bodyText: ''};
    }
    const keywords = [
        'thành công', 'thanh cong', 'thất bại', 'that bai', 'sai', 'lỗi', 'loi',
        'đã sử dụng', 'da su dung', 'success', 'failed', 'error', 'invalid', 'used',
        'không hợp lệ', 'khong hop le', 'hết hạn', 'het han', 'không đúng', 'không tồn tại',
    ];
    const noisePatterns = ['__next_f', '__NEXT', 'self.__next', 'push([', 'webpack'];
    let keywordText = '';
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
    let node;
    while (node = walker.nextNode()) {
        const parent = node.parentElement;
        if (!parent || ['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(parent.tagName)) continue;
        const txt = (node.textContent || '').trim();
        if (txt.length < 3 || noisePatterns.some(p => txt.includes(p))) continue;
        if (keywords.some(k => txt.toLowerCase().includes(k))) {
            keywordText = txt;
            break;
        }
    }
    return {
        ordered,
        combinedText: combinedParts.join(' '),
        keywordText,
        bodyText: (document.body.innerText || '').slice(0, 30000),
    };
}
"""


_WS_RE = _re.compile(r"\s+")


_PRIORITY_RESULT_SELECTORS = [
    ".swal2-container", ".swal2-popup", "[role='alertdialog']",
    "#toast-container", ".iziToast-wrapper", ".notyf", ".p-toast",
    ".p-toast-message-content", "[class*='snackbar' i]",
    ".swal2-html-container", ".swal2-title", ".swal2-popup",
    "div[class*='popup'] p", "div[class*='modal'] p", "div[class*='dialog'] p",
    "div[class*='alert'] p", "div[class*='notice'] p", "div[class*='message'] p",
    ".text-red-600", ".text-green-600", ".text-yellow-600",
    ".text-red-500", ".text-green-500", "p.mt-1.text-sm",
    "div[class*='rounded-2xl'] p", "div[class*='rounded-xl'] p", "div[class*='rounded-lg'] p",
    "[role='alert']", "[role='status']", "[role='dialog']",
    "div[style*='position: fixed'] p", "div[style*='position:fixed'] p",
    "[data-sonner-toast] [data-description]", "[data-sonner-toast]",
    ".Toastify__toast-body", ".ant-message-notice-content", ".ant-notification-notice-message",
    "[data-toast]", "[data-radix-toast-viewport] *", "[aria-live]", "output",
    ".van-toast", ".van-dialog", ".el-message", ".el-notification", ".ant-message",
    ".toast", ".modal", "[class*='message']", "[class*='result']",
    "[class*='success']", "[class*='error']",
]

_RESULT_WAIT_JS = r"""
([domSel, genSel, beforeRaw, transient, fastMs, startMs, keywords]) => {
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const before = norm(beforeRaw);
    const buttonOnly = new Set(['hủy', 'huy', 'xác thực', 'xac thuc', 'đóng', 'dong', 'cancel', 'verify', 'ok', 'close']);
    const okText = (raw) => {
        const n = norm(raw);
        if (n.length < 5) return false;
        if (before && before.includes(n)) return false;
        if (transient.some((p) => n.includes(p))) return false;
        if (n.length <= 40) {
            const toks = raw.toLowerCase().split(/[\n\/|,]+/).map((x) => x.trim()).filter(Boolean);
            if (toks.length && toks.every((x) => buttonOnly.has(x))) return false;
        }
        if (/__next_f|self\.__next|webpack|push\(\[/.test(raw)) return false;
        return true;
    };
    const scan = (sels) => {
        for (const sel of sels) {
            let els;
            try { els = document.querySelectorAll(sel); } catch (_) { continue; }
            for (const el of els) {
                if (!el.getClientRects().length) continue;
                let t = (el.innerText || el.textContent || '').trim();
                // XX88's aquarium popup exposes the result text in a sibling
                // container while the stable marker is the btn-accept image.
                if (!t && el.matches("img[src$='/images/ui-aquarium/btn-accept.png']")) {
                    let parent = el.parentElement;
                    for (let i = 0; parent && i < 5 && !t; i++, parent = parent.parentElement) {
                        t = (parent.innerText || parent.textContent || '').trim();
                    }
                }
                if (okText(t)) return t;
            }
        }
        return null;
    };
    let r = scan(domSel);
    if (r) return r;
    if (Date.now() - startMs >= fastMs) {
        r = scan(genSel);
        if (r) return r;
        const beforeLines = new Set((beforeRaw || '').split('\n').map((l) => l.trim()).filter(Boolean));
        const body = (document.body && document.body.innerText) || '';
        for (const line of body.split('\n')) {
            const l = line.trim();
            if (l.length < 3 || beforeLines.has(l)) continue;
            const low = l.toLowerCase();
            if (keywords.some((k) => low.includes(k)) && okText(l)) return l;
        }
    }
    return null;
}
"""

_RESULT_KEYWORDS = (
    "thành công", "thanh cong", "thất bại", "that bai", "sai", "lỗi", "loi",
    "đã sử dụng", "da su dung", "success", "failed", "error", "invalid", "used",
    "không hợp lệ", "khong hop le", "hết hạn", "het han", "không đúng", "không tồn tại",
)


def _normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", (text or "")).strip().lower()


def _is_stale_static_text(candidate: str, before_text: str) -> bool:
    """True nếu 'candidate' đã xuất hiện y hệt trên trang TRƯỚC khi bấm
    submit (before_text chụp lúc đó). Một số site (vd tangquaqq88.com) có
    banner cảnh báo tĩnh (vd "QQ88 LINK CHÍNH THỨC...") luôn nằm sẵn trong
    DOM và tình cờ khớp 1 trong các selector chung (PRIORITY_SELECTORS) —
    nếu không lọc, banner này bị đọc nhầm thành kết quả submit ngay ở lần
    poll ĐẦU TIÊN (trước khi popup thật kịp hiện ra), khiến vòng lặp thoát
    sớm với nội dung sai (AMBIGUOUS/NO_RESULT giả) dù site chưa trả lời gì.
    So khớp theo substring sau khi chuẩn hoá khoảng trắng — nội dung kết
    quả thật (thành công/sai/hết hạn...) gần như không bao giờ trùng khớp
    y hệt với text tĩnh đã có sẵn trước đó."""
    if not candidate or not before_text:
        return False
    norm_candidate = _normalize_ws(candidate)
    if len(norm_candidate) < 3:
        return False
    return norm_candidate in _normalize_ws(before_text)


async def detect_result_text(
    page,
    domain: str = "",
    before_text: str = "",
    *,
    selector_only: bool = False,
) -> str:
    domain_selectors = _get_domain_result_selectors(domain)

    PRIORITY_SELECTORS = _PRIORITY_RESULT_SELECTORS

    result_selectors = [
        ".swal2-container", "[role='alertdialog']", "#toast-container",
        ".iziToast-wrapper", ".notyf", ".p-toast", ".p-toast-message-content",
        "[class*='snackbar' i]",
        ".text-red-600", ".text-green-600", "p.mt-1.text-sm",
        "div[class*='rounded-2xl'] p", "div[class*='rounded-xl'] p", "div[class*='rounded-lg'] p",
        "[role='dialog']", "[role='alert']", "[role='status']",
        ".modal-body", ".modal-content", ".popup-content", ".alert",
        "[class*='success']", "[class*='error']", "[class*='toast']",
        "[class*='result']", "[class*='notify']", "[class*='modal']",
        "[class*='popup']", "[class*='notification']", "div[style*='position: fixed']",
    ]

    ordered_selectors = domain_selectors + PRIORITY_SELECTORS

    try:
        data = await page.evaluate(
            _DETECT_RESULT_JS,
            {
                "orderedSelectors": domain_selectors if selector_only else ordered_selectors,
                "combinedSelectors": result_selectors,
                "includeGlobal": not selector_only,
            },
        )
    except Exception:
        data = None

    if data:
        for txt in data.get("ordered", []):
            if txt and len(txt.strip()) >= 3:
                clean = _filter_nextjs_noise(txt.strip())
                if not clean or _is_transient_captcha_text(clean):
                    continue
                if _is_stale_static_text(clean, before_text):
                    continue
                return clean

        combined = (data.get("combinedText") or "").strip()
        if len(combined) >= 3 and not _is_transient_captcha_text(combined):
            filtered = _filter_nextjs_noise(combined)
            if filtered and not _is_stale_static_text(filtered, before_text):
                return filtered

    if data:
        page_text = (data.get("keywordText") or "").strip()
        if page_text:
            clean = _filter_nextjs_noise(page_text)
            if clean and not _is_transient_captcha_text(clean) and not _is_stale_static_text(clean, before_text):
                return clean

        after_text = data.get("bodyText") or ""
        if before_text and after_text:
            before_lines = {line.strip() for line in before_text.splitlines() if line.strip()}
            new_lines = []
            for line in after_text.splitlines():
                line = line.strip()
                if not line or line in before_lines or len(line) < 3:
                    continue
                clean = _filter_nextjs_noise(line)
                if clean and not _is_transient_captcha_text(clean):
                    new_lines.append(clean)
            if new_lines:
                return " ".join(new_lines[:6])

    return ""


# ============================================================
# PAGE PERFORMANCE / STEALTH
# ============================================================
_perf_ready_pages = weakref.WeakSet()


async def _setup_page_performance(page, label: str = ""):
    try:
        if page in _perf_ready_pages:
            return
    except TypeError:
        # Some Playwright proxy objects may not support weak references.
        # In that case retain the old behavior rather than failing setup.
        pass

    STEALTH_JS = """
        () => {
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
            if (!window.chrome) { window.chrome = {}; }
            window.chrome.runtime = {};
            Object.defineProperty(navigator, 'plugins', {
                get: () => ([
                    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                ]),
                configurable: true,
            });
            Object.defineProperty(navigator, 'languages', { get: () => ['vi-VN', 'vi', 'en-US', 'en'], configurable: true });
            if (window.$cdc_asdjflasutopfhvcZLmcfl_) { delete window.$cdc_asdjflasutopfhvcZLmcfl_; }
            if (window.$wdc_) { delete window.$wdc_; }
            if (navigator.permissions && navigator.permissions.query) {
                const origQuery = navigator.permissions.query;
                navigator.permissions.query = (parameters) =>
                    parameters.name === 'notifications'
                        ? Promise.resolve({ state: Notification.permission })
                        : origQuery(parameters);
            }
            Object.defineProperty(navigator, 'headless', { get: () => false, configurable: true });
            Object.defineProperty(screen, 'width', { get: () => 1920, configurable: true });
            Object.defineProperty(screen, 'height', { get: () => 1080, configurable: true });
            Object.defineProperty(screen, 'availWidth', { get: () => 1920, configurable: true });
            Object.defineProperty(screen, 'availHeight', { get: () => 1040, configurable: true });
            try {
                const getParam = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) return 'Intel Inc.';
                    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                    return getParam.call(this, parameter);
                };
                const getParam2 = WebGL2RenderingContext.prototype.getParameter;
                WebGL2RenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) return 'Intel Inc.';
                    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                    return getParam2.call(this, parameter);
                };
            } catch(e) {}
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8, configurable: true });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8, configurable: true });
            Object.defineProperty(navigator, 'connection', {
                get: () => ({ rtt: 50, downlink: 10, effectiveType: '4g', saveData: false }),
                configurable: true,
            });
        }
    """
    try:
        # Edge thật + profile thật thường không cần giả lập; giá trị giả
        # (WebGL "Intel", màn hình 1920x1080...) lệch phần cứng thật có thể
        # làm Turnstile nghi ngờ hơn. Chỉ bật khi cần A/B test.
        if getattr(Config, "STEALTH_JS_ENABLED", False):
            await page.add_init_script(STEALTH_JS)
    except Exception as e:
        logger.debug(f"⚠️ [{label}] add_init_script error: {e}")

    _BLOCK_DOMAINS = (
        "google-analytics", "googletagmanager", "doubleclick", "facebook.net",
        "fbcdn.net", "hotjar", "googlesyndication", "adsystem", "criteo",
        "taboola", "outbrain", "clarity.ms", "sentry.io", "crisp.chat", "tawk.to",
    )
    # Không abort font/media/API: các site mới dùng chúng trong layout hoặc
    # submit. Chỉ intercept đúng các host telemetry/quảng cáo không cần thiết;
    # như vậy request chính không phải đi qua Python handler.
    _BLOCK_RE = _re.compile(
        r"(?:" + "|".join(_re.escape(d) for d in _BLOCK_DOMAINS) + r")",
        _re.IGNORECASE,
    )

    async def _abort_tracking(route):
        await route.abort()

    try:
        await page.route(_BLOCK_RE, _abort_tracking)
    except Exception as e:
        logger.debug(f"⚠️ [{label}] Cannot setup route: {e}")

    try:
        _perf_ready_pages.add(page)
    except TypeError:
        pass


async def _close_unwanted_popups(page) -> dict:
    """Đóng popup/overlay không mong muốn và xác minh kết quả.

    Trả về {"closed": số nút đã click, "stuck": còn overlay hiển thị sau khi
    thử đóng hay không}. "stuck" cho phép _quick_clean_page() phát hiện popup
    cứng đầu (animation chưa xong lúc click, nút nằm ngoài các selector/từ
    khoá đã biết...) và escalate sang full reload ngay lập tức thay vì chờ
    đủ FULL_RELOAD_EVERY_N lần submit mới dọn được DOM.
    """
    try:
        result = await page.evaluate(
            """
            () => {
                const OVERLAY_SEL = [
                    '.modal', '[class*="modal" i]', '[class*="popup" i]',
                    '[class*="overlay" i]', '[class*="dialog" i]',
                    '[class*="notification" i]', '[class*="toast" i]',
                    '[class*="alert" i]:not(.alert-success):not(.alert-info)',
                    '[class*="banner" i]', '[class*="announcement" i]',
                ];
                const isVisible = (el) => {
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                // Container có ô nhập là FORM của trang (vd o8: .modal-code-anchor),
                // không phải popup thông báo -> không đóng và không tính là "kẹt".
                const hasInput = (el) => !!el.querySelector(
                    'input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]), textarea'
                );
                const hasOverlay = [...document.querySelectorAll(
                    '.modal, [class*="modal" i], [class*="popup" i], [role="dialog"]'
                )].some((el) => isVisible(el) && !hasInput(el));
                if (!hasOverlay) return {closed: 0, stuck: false};
                const CLOSE_KEYWORDS = ['đóng', 'close', 'x', 'cancel', 'hủy', 'dismiss', 'got it', 'ok', 'thoát'];
                const SKIP_TEXT = ['xác thực', 'xac thuc', 'submit', 'kiểm tra', 'áp dụng', 'nhận'];
                const ICON_CLOSE_PATHS = ['M6 18L18 6M6 6l12 12'];
                let count = 0;
                for (const sel of OVERLAY_SEL) {
                    const els = [...document.querySelectorAll(sel)];
                    for (const el of els) {
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        if (hasInput(el)) continue;
                        const btns = [...el.querySelectorAll('button, [role="button"], a, span')];
                        for (const btn of btns) {
                            const txt = (btn.innerText || btn.textContent || btn.getAttribute('aria-label') || '').trim().toLowerCase();
                            if (SKIP_TEXT.some(s => txt.includes(s))) continue;
                            if (CLOSE_KEYWORDS.some(k => txt === k || txt.startsWith(k + ' '))) {
                                btn.click();
                                count++;
                                break;
                            }
                        }
                    }
                }
                const paths = [...document.querySelectorAll('svg path')];
                for (const p of paths) {
                    const d = (p.getAttribute('d') || '').trim();
                    if (!ICON_CLOSE_PATHS.includes(d)) continue;
                    const clickable = p.closest('button, [role="button"], a');
                    if (!clickable) continue;
                    const rect = clickable.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    clickable.click();
                    count++;
                }
                // Sau khi click, kiểm tra lại: overlay có thực sự biến mất
                // chưa. Một click "thành công" (đúng selector, đúng từ khoá)
                // vẫn có thể không đóng được overlay nếu nó cần animation,
                // một sự kiện khác ngoài click, hoặc nút đóng thật nằm ngoài
                // CLOSE_KEYWORDS/ICON_CLOSE_PATHS đã biết.
                const stuck = OVERLAY_SEL.some(
                    (sel) => [...document.querySelectorAll(sel)].some((el) => isVisible(el) && !hasInput(el))
                );
                return {closed: count, stuck};
            }
            """
        )
        closed = int((result or {}).get("closed", 0)) if isinstance(result, dict) else 0
        stuck = bool((result or {}).get("stuck", False)) if isinstance(result, dict) else False
        if closed > 0:
            logger.debug(f"🧹 Đóng {closed} popup không mong muốn")
        if stuck:
            logger.debug("⚠️ Popup/overlay vẫn còn sau khi thử đóng")
        return {"closed": closed, "stuck": stuck}
    except Exception:
        return {"closed": 0, "stuck": False}


_DISMISS_RESULT_POPUP_JS = r"""
async ([selectors, texts]) => {
    const isVisible = (el) => {
        if (!el || !el.isConnected) return false;
        const st = getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    // Nút phải nằm trong overlay (fixed/dialog): tránh bấm nhầm nút cùng chữ
    // "Tiếp tục"/"Đồng ý" của chính trang.
    const OVERLAY_CLASS = /modal|popup|overlay|dialog|backdrop|alert/i;
    const inOverlay = (el) => {
        for (let n = el; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
            const role = n.getAttribute && n.getAttribute('role');
            if (role === 'dialog' || role === 'alertdialog') return true;
            if (n.getAttribute && n.getAttribute('aria-modal') === 'true') return true;
            const st = getComputedStyle(n);
            if (st.position === 'fixed') return true;
            // absolute + z-index > 0 (kiểu popup căn giữa không dùng fixed)
            if (st.position === 'absolute' && parseInt(st.zIndex, 10) > 0) return true;
            if (typeof n.className === 'string' && OVERLAY_CLASS.test(n.className)) return true;
        }
        return false;
    };
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const find = () => {
        const found = [];
        for (const sel of selectors) {
            const before = found.length;
            try { for (const el of document.querySelectorAll(sel)) if (isVisible(el)) found.push(el); } catch (e) {}
            // Selector đứng trước thắng: chỉ bấm 1 nút đóng (vd "Thử lại" rồi
            // thôi, không bấm thêm dấu X).
            if (found.length > before) break;
        }
        if (texts.length) {
            for (const el of document.querySelectorAll('button, [role="button"]')) {
                if (!isVisible(el) || !texts.includes(norm(el.innerText || el.textContent))) continue;
                if (inOverlay(el)) found.push(el);
            }
        }
        return found;
    };
    const targets = find();
    if (!targets.length) return {clicked: 0, remaining: 0};
    for (const el of targets) { try { el.click(); } catch (e) {} }
    // Chờ animation đóng (tối đa ~450ms) để bước kiểm tra "popup còn kẹt"
    // phía sau không thấy overlay đang mờ dần rồi kết luận nhầm là kẹt.
    const t0 = Date.now();
    let remaining = find().length;
    while (remaining && Date.now() - t0 < 450) {
        await new Promise((r) => setTimeout(r, 30));
        remaining = find().length;
    }
    return {clicked: targets.length, remaining};
}
"""


async def _dismiss_result_popup(page, domain: str) -> dict:
    """Bấm nút đóng popup thông báo kết quả theo profile của site.

    CHỈ gọi sau khi kết quả đã được đọc (bước dọn trang). Không có cấu hình cho
    domain → không làm gì. Lỗi/timeout → trả mặc định, caller rơi về logic cũ.
    """
    profile = get_site_profile(domain)
    selectors = list(getattr(profile, "popup_dismiss_selectors", ()) or ()) if profile else []
    texts = [t.strip().lower() for t in (getattr(profile, "popup_dismiss_texts", ()) or ())] if profile else []
    if not selectors and not texts:
        return {"clicked": 0, "remaining": 0}
    try:
        res = await page.evaluate(_DISMISS_RESULT_POPUP_JS, [selectors, texts])
        if isinstance(res, dict):
            if res.get("clicked"):
                logger.debug(f"🧹 [{domain}] đóng popup kết quả: clicked={res.get('clicked')} còn={res.get('remaining')}")
            return {"clicked": int(res.get("clicked", 0)), "remaining": int(res.get("remaining", 0))}
    except Exception as e:
        logger.debug(f"⚠️ [{domain}] dismiss popup kết quả lỗi: {e}")
    return {"clicked": 0, "remaining": 0}


async def _wake_tab_for_submit(page):
    try:
        await page.bring_to_front()
        await page.evaluate(
            "Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });"
        )
        await _close_unwanted_popups(page)
    except Exception:
        pass


# ============================================================
# TAB POOL
# ============================================================
class TabPool:
    def __init__(self, max_per_domain: int = 3, per_domain_overrides: dict | None = None):
        self.max_per_domain = max(1, int(max_per_domain))
        self._per_domain_max: dict = dict(per_domain_overrides or {})
        self._domains: dict = {}
        self._account_tabs: dict[str, dict[str, dict]] = {}
        self._rr_idx: dict = {}
        self._setup_lock = asyncio.Lock()

    @staticmethod
    def _new_entry(page):
        now = time.monotonic()
        return {
            "page": page,
            "lock": asyncio.Lock(),
            "created_at": now,
            "last_used": now,
            "last_memory_maintenance": now,
            "last_memory_reload": now,
            "reserved": False,
        }

    @staticmethod
    def _touch(entry):
        entry["last_used"] = time.monotonic()

    def _max_for(self, domain: str) -> int:
        return max(1, int(self._per_domain_max.get(domain, self.max_per_domain)))

    def _bind_accounts(self, domain: str, accounts: list[str] | None = None):
        """Bind configured accounts to dedicated entries in stable order."""
        if not accounts:
            return
        entries = self._domains.get(domain, [])
        mapping = self._account_tabs.setdefault(domain, {})
        for index, account in enumerate(accounts):
            if account and index < len(entries):
                mapping[str(account)] = entries[index]

    async def init(self, domain_url_map: dict | None = None, domain_accounts: dict | None = None):
        context = await get_or_launch_browser_context("shared")
        existing = list(context.pages)
        claimed_ids = set()
        domain_url_map = domain_url_map or {}

        pending_nav = []
        pending_setup = []
        for domain, target_url in domain_url_map.items():
            if not domain or domain in self._domains:
                continue

            # Warm one tab per submit slot at startup. Previously only one tab
            # was preloaded, so the second concurrent task had to open/navigate
            # a cold tab during the live submit path (and could fail fast while
            # the first tab was busy). The pool cap still bounds every domain.
            warm_count = min(self._max_for(domain), self.max_per_domain)
            for slot in range(max(1, warm_count)):
                page = None
                for p in existing:
                    if id(p) in claimed_ids:
                        continue
                    try:
                        url = (p.url or "").lower()
                    except Exception:
                        continue
                    if domain in url:
                        page = p
                        claimed_ids.add(id(p))
                        break

                opened_new = False
                if page is not None:
                    pending_setup.append((domain, page, False, target_url, slot + 1, warm_count))
                else:
                    opened_new = True
                    pending_setup.append((domain, None, opened_new, target_url, slot + 1, warm_count))

        # CDP setup is independent for different pages. Do it concurrently so
        # warming several domains does not spend one full round-trip per tab.
        # Navigation remains separately throttled below because it is the
        # expensive part for the target websites and must not overload Edge.
        setup_semaphore = asyncio.Semaphore(8)

        async def _setup_one(domain, page, opened_new, target_url, slot, warm_count):
            async with setup_semaphore:
                if page is None:
                    page = await context.new_page()
                    logger.info(f"  🌐 [Startup] '{domain}' → mở tab nóng ({slot}/{warm_count})")
                else:
                    logger.info(f"  🗂️ [Startup] '{domain}' → dùng tab có sẵn ({slot}/{warm_count})")
                await _setup_page_performance(page, f"dedicated-{domain}-{slot}")
                return domain, page, opened_new, target_url

        prepared_tabs = await asyncio.gather(*[
            _setup_one(domain, page, opened_new, target_url, slot, warm_count)
            for domain, page, opened_new, target_url, slot, warm_count in pending_setup
        ])
        for domain, page, opened_new, target_url in prepared_tabs:
            self._domains.setdefault(domain, []).append(self._new_entry(page))
            pending_nav.append((domain, page, opened_new, target_url))

        for domain, accounts in (domain_accounts or {}).items():
            self._bind_accounts(domain, [str(account) for account in accounts if account])

        nav_semaphore = asyncio.Semaphore(5)

        async def _goto_one(domain, page, opened_new, target_url):
            async with nav_semaphore:
                try:
                    need_goto = opened_new
                    if not need_goto:
                        try:
                            need_goto = not _page_matches_target(page.url, target_url, domain)
                        except Exception:
                            need_goto = True
                    if need_goto:
                        await page.goto(
                            target_url,
                            wait_until="domcontentloaded",
                            timeout=int(float(_site_profile_value(domain, "navigation_timeout_seconds", 12.0)) * 1000),
                        )
                        await scroll_to_input_fields(page)
                    await _close_unwanted_popups(page)
                    await page.bring_to_front()
                except Exception as e:
                    logger.warning(f"⚠️ [Startup] '{domain}' lỗi điều hướng: {e}")

        await asyncio.gather(*[
            _goto_one(domain, page, opened_new, target_url)
            for domain, page, opened_new, target_url in pending_nav
        ])

        logger.info(
            f"✅ TabPool: {len(self._domains)} domain đã sẵn sàng. Nếu 1 tab bị Cloudflare "
            f"chặn ngay lúc này, lượt submit tới đó sẽ tự fallback sang browser — "
            f"không cần xác minh tay."
        )

    async def _claim_existing_or_new(self, domain: str) -> dict | None:
        context = await get_or_launch_browser_context("shared")
        claimed_ids = {id(e["page"]) for entries in self._domains.values() for e in entries}
        page = None
        for p in context.pages:
            if id(p) in claimed_ids:
                continue
            try:
                url = (p.url or "").lower()
            except Exception:
                continue
            if domain in url:
                page = p
                break

        if page is not None:
            await _setup_page_performance(page, f"dedicated-{domain}")
            entry = self._new_entry(page)
            self._domains.setdefault(domain, []).append(entry)
            return entry

        if not getattr(Config, "AUTO_OPEN_MISSING_TABS", True):
            logger.info(f"🗂️ [Dedicated] '{domain}' → AUTO_OPEN_MISSING_TABS=False: không mở tab mới")
            return None

        page = await context.new_page()
        await _setup_page_performance(page, f"dedicated-{domain}")
        logger.info(f"🌐 [Dedicated] '{domain}' → chưa có tab sẵn (lazy) → mở tab mới")
        entry = self._new_entry(page)
        self._domains.setdefault(domain, []).append(entry)
        return entry

    async def _new_tab_for_domain(self, domain: str) -> dict:
        context = await get_or_launch_browser_context("shared")
        claimed_ids = {id(e["page"]) for e in self._domains.get(domain, [])}
        for p in context.pages:
            if id(p) in claimed_ids:
                continue
            try:
                url = (p.url or "").lower()
            except Exception:
                continue
            if domain in url:
                await _setup_page_performance(p, f"dedicated-{domain}")
                entry = self._new_entry(p)
                self._domains.setdefault(domain, []).append(entry)
                return entry

        if not getattr(Config, "AUTO_OPEN_MISSING_TABS", True):
            raise RuntimeError("Auto-open missing tabs disabled by config")

        page = await context.new_page()
        await _setup_page_performance(page, f"dedicated-{domain}")
        entry = self._new_entry(page)
        self._domains.setdefault(domain, []).append(entry)
        logger.info(
            f"🗂️ [Dedicated] '{domain}' cần xử lý song song → mở thêm tab phụ "
            f"({len(self._domains[domain])}/{self._max_for(domain)})"
        )
        return entry


    async def acquire(self, domain: str = "", account: str | None = None):
        """Acquire a usable tab, waiting briefly through transient contention."""
        wait_limit = max(0.0, float(getattr(Config, "TAB_ACQUIRE_WAIT_SECONDS", 2.0)))
        deadline = time.monotonic() + wait_limit
        while True:
            try:
                return await self._try_acquire(domain, account=account)
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.05)

    async def _try_acquire(self, domain: str = "", account: str | None = None):
        domain = domain or "unknown"

        entries = self._domains.get(domain)
        if not entries:
            async with self._setup_lock:
                entries = self._domains.get(domain)
                if not entries:
                    entry = await self._claim_existing_or_new(domain)
                    if entry is None:
                        context = await get_or_launch_browser_context("shared")
                        wait_timeout = 10.0
                        poll = 0.5
                        waited = 0.0
                        while waited < wait_timeout:
                            for p in context.pages:
                                try:
                                    if domain in (p.url or "").lower():
                                        entry = self._new_entry(p)
                                        self._domains.setdefault(domain, []).append(entry)
                                        entries = self._domains[domain]
                                        break
                                except Exception:
                                    pass
                            if entries:
                                break
                            await asyncio.sleep(poll)
                            waited += poll
                        if not entries:
                            raise RuntimeError(f"No tab available for domain '{domain}'")

        if account:
            bound = self._account_tabs.setdefault(domain, {}).get(str(account))
            if bound is None:
                # A late-created account gets the first unbound slot, without
                # ever stealing a slot already pinned to another account.
                bound_accounts = {id(item) for item in self._account_tabs[domain].values()}
                for candidate in entries:
                    if id(candidate) not in bound_accounts:
                        self._account_tabs[domain][str(account)] = candidate
                        bound = candidate
                        break
            if bound is None and len(entries) >= self._max_for(domain):
                raise RuntimeError(
                    f"No dedicated tab slot for account '{account}' on domain '{domain}'"
                )
            entries = [bound] if bound is not None else []

        for entry in entries:
            page = entry["page"]
            if entry["lock"].locked() or entry.get("reserved") or safe_is_closed(page):
                continue
            # Đặt chỗ NGAY (trước mọi await): nếu không, 2 coroutine cùng thấy
            # tab #1 rảnh, cùng chọn nó và xếp hàng trên 1 tab trong khi tab #2
            # ngồi không.
            entry["reserved"] = True
            try:
                # RR88 luôn render Turnstile trong form; widget hiện diện không
                # đồng nghĩa tab bị chặn. Chỉ kiểm tra challenge tự động ở các
                # domain có modal xác minh sau khi bấm submit.
                cf_blocked = (
                    domain != "liverr88.net"
                    and await is_cloudflare_present(page, domain=domain)
                )
            except Exception:
                cf_blocked = False
            if cf_blocked:
                entry["reserved"] = False
                continue
            self._touch(entry)
            return entry, page, entry["lock"]

        domain_cap = self._max_for(domain)
        entries = self._domains.get(domain, [])
        if len(entries) < domain_cap:
            try:
                entry = await self._new_tab_for_domain(domain)
                if account:
                    self._account_tabs.setdefault(domain, {})[str(account)] = entry
                entry["reserved"] = True
                self._touch(entry)
                return entry, entry["page"], entry["lock"]
            except Exception:
                pass

        fallback_entries = entries
        if account:
            bound = self._account_tabs.get(domain, {}).get(str(account))
            if bound is None:
                raise RuntimeError(
                    f"No dedicated tab slot for account '{account}' on domain '{domain}'"
                )
            fallback_entries = [bound]
        for candidate in fallback_entries:
            if candidate is None:
                continue
            if candidate["lock"].locked() or candidate.get("reserved") or safe_is_closed(candidate["page"]):
                continue
            candidate["reserved"] = True
            try:
                if domain != "liverr88.net" and await is_cloudflare_present(candidate["page"], domain=domain):
                    candidate["reserved"] = False
                    continue
            except Exception:
                pass
            self._touch(candidate)
            return candidate, candidate["page"], candidate["lock"]

        # Tất cả tab đều bị coi là "đang xác minh": reload tab rảnh lâu nhất
        # (không đụng tab vừa dùng <20s để khỏi phá xác minh đang làm dở).
        now_m = time.monotonic()
        for candidate in sorted(entries, key=lambda e: e.get("last_used", 0.0)):
            if candidate["lock"].locked() or candidate.get("reserved") or safe_is_closed(candidate["page"]):
                continue
            if now_m - float(candidate.get("last_used", now_m)) < 20.0:
                continue
            candidate["reserved"] = True
            try:
                await candidate["page"].reload(
                    wait_until="domcontentloaded",
                    timeout=int(getattr(Config, "PAGE_NAVIGATION_TIMEOUT", 10000)),
                )
                self._touch(candidate)
                return candidate, candidate["page"], candidate["lock"]
            except Exception:
                candidate["reserved"] = False
                continue

        raise RuntimeError(
            f"No available non-blocked tab for domain '{domain}' "
            f"({len(entries)} entries are busy, closed, or under verification)"
        )

    async def collect_garbage(self, *, idle_ttl: float = 900.0, min_tabs_per_domain: int = 1) -> dict:
        """Remove closed/stale spare tabs without interrupting submissions."""
        idle_ttl = max(30.0, float(idle_ttl))
        keep_min = max(1, int(min_tabs_per_domain))
        now = time.monotonic()
        removed = 0
        closed = 0

        async with self._setup_lock:
            for domain, entries in list(self._domains.items()):
                if not entries:
                    self._domains.pop(domain, None)
                    self._rr_idx.pop(domain, None)
                    continue

                survivors = []
                ordered = sorted(entries, key=lambda e: e.get("created_at", now))
                for index, entry in enumerate(ordered):
                    page = entry.get("page")
                    lock = entry.get("lock")
                    is_closed = safe_is_closed(page)
                    is_idle_spare = (
                        index >= keep_min
                        and not lock.locked()
                        and now - float(entry.get("last_used", now)) >= idle_ttl
                    )
                    if entry.get("reserved") or lock.locked() or (not is_closed and not is_idle_spare):
                        survivors.append(entry)
                        continue

                    if not is_closed:
                        try:
                            await page.close()
                            closed += 1
                        except Exception:
                            survivors.append(entry)
                            continue
                    removed += 1

                    for key, cached_page in list(bot_state.account_pages.items()):
                        if cached_page is page:
                            bot_state.account_pages.pop(key, None)
                            bot_state._input_cache.pop(key, None)
                            bot_state.context_locks.pop(key, None)
                    for account, bound_entry in list(self._account_tabs.get(domain, {}).items()):
                        if bound_entry is entry:
                            self._account_tabs[domain].pop(account, None)

                if survivors:
                    self._domains[domain] = survivors
                    self._rr_idx[domain] = self._rr_idx.get(domain, 0) % len(survivors)
                else:
                    self._domains.pop(domain, None)
                    self._rr_idx.pop(domain, None)
                    self._account_tabs.pop(domain, None)

        if removed:
            gc.collect()
            logger.info(
                "🧹 [TabPool-GC] removed=%s closed=%s remaining=%s",
                removed,
                closed,
                sum(len(items) for items in self._domains.values()),
            )
            return {
                "removed": removed,
                "closed": closed,
                "remaining": sum(len(items) for items in self._domains.values()),
            }

    async def cleanup_idle_memory(
        self, *, compact_idle_seconds: float = 300.0, reload_idle_seconds: float = 900.0
    ) -> dict:
        """Release accumulated page memory without touching active submits.

        Only unlocked, non-Cloudflare tabs are considered. A lightweight DOM
        compact runs first; a long-idle tab is reloaded to release the page's
        JS/resource graph while retaining the warm tab slot. At most one tab
        per domain is reloaded per watchdog pass to avoid a synchronized cold
        start across all sites.
        """
        now = time.monotonic()
        compacted = 0
        reloaded = 0
        skipped = 0
        for domain, entries in list(self._domains.items()):
            reloaded_this_domain = False
            for entry in entries:
                page = entry.get("page")
                lock = entry.get("lock")
                if lock is None or lock.locked() or entry.get("reserved") or safe_is_closed(page):
                    skipped += 1
                    continue
                idle = now - float(entry.get("last_used", now))
                compact_interval = max(30.0, float(compact_idle_seconds))
                last_maintenance = float(entry.get("last_memory_maintenance", entry.get("last_used", now)))
                if idle < compact_interval or now - last_maintenance < compact_interval:
                    continue
                try:
                    if await is_cloudflare_present(page, domain=domain):
                        skipped += 1
                        continue
                except Exception:
                    skipped += 1
                    continue

                reload_threshold = max(compact_interval, float(reload_idle_seconds))
                last_reload = float(entry.get("last_memory_reload", entry.get("last_used", now)))
                if not reloaded_this_domain and idle >= reload_threshold and now - last_reload >= reload_threshold:
                    try:
                        await page.reload(
                            wait_until="domcontentloaded",
                            timeout=int(getattr(Config, "PAGE_NAVIGATION_TIMEOUT", 10000)),
                        )
                        await _close_unwanted_popups(page)
                        maintenance_at = time.monotonic()
                        entry["last_memory_maintenance"] = maintenance_at
                        entry["last_memory_reload"] = maintenance_at
                        reloaded += 1
                        reloaded_this_domain = True
                        continue
                    except Exception as exc:
                        logger.debug("⚠️ [TabPool-Memory] reload %s lỗi: %s", domain, exc)

                try:
                    await page.evaluate(
                        """
                        () => {
                            const transient = [
                                '.toast', '[class*="toast" i]', '[class*="snackbar" i]',
                                '[role="alert"]', '[role="status"]'
                            ];
                            for (const selector of transient) {
                                for (const el of document.querySelectorAll(selector)) {
                                    if (el && !el.matches('input,textarea,form')) el.remove();
                                }
                            }
                            try { performance.clearResourceTimings(); } catch (_) {}
                            return true;
                        }
                        """
                    )
                    # Maintenance is not a user submit: don't reset last_used,
                    # otherwise periodic compaction starves the reload threshold.
                    entry["last_memory_maintenance"] = time.monotonic()
                    compacted += 1
                except Exception as exc:
                    entry["last_memory_maintenance"] = time.monotonic()
                    logger.debug("⚠️ [TabPool-Memory] compact %s lỗi: %s", domain, exc)
        return {"compacted": compacted, "reloaded": reloaded, "skipped": skipped}


_tab_pool: TabPool | None = None


def _check_edge_cdp_port_reachable(port: int, timeout: float = 1.5) -> bool:
    import socket
    try:
        host = getattr(Config, "EDGE_CDP_HOST", "127.0.0.1")
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def preload_browsers_and_accounts(account_targets: list):
    """account_targets: danh sách item {"key","domain","target_url","accounts"}
    ĐÃ ĐƯỢC LỌC SẴN chỉ gồm domain thuộc BROWSER_DOMAINS — main_script.py
    tính toán và truyền vào (dùng chung build_unique_account_targets())."""
    global _tab_pool

    if not account_targets:
        logger.info("ℹ️ [Browser] Không có kênh nào thuộc 7 domain trình duyệt — bỏ qua preload")
        return

    pool_size = max(1, min(10, int(getattr(Config, "TAB_POOL_SIZE", 2) or 2)))

    domain_channel_count: dict = {}
    for item in account_targets:
        d = item["domain"]
        domain_channel_count[d] = domain_channel_count.get(d, 0) + 1

    domain_tab_cap = max(1, int(getattr(Config, "MAX_TAB_PER_DOMAIN_CAP", 5)))
    per_domain_overrides = {}
    for d, count in domain_channel_count.items():
        # Apply the cap to every configured domain. Without this override,
        # TAB_POOL_SIZE could accidentally allow many tabs for domains that
        # have fewer configured account targets than the global pool size.
        profile_slots = int(_site_profile_value(d, "tab_slots", domain_tab_cap))
        per_domain_overrides[d] = min(
            domain_tab_cap,
            max(1, profile_slots),
            max(1, count),
        )

    _tab_pool = TabPool(max_per_domain=pool_size, per_domain_overrides=per_domain_overrides)

    domain_url_map: dict = {}
    domain_accounts: dict[str, list[str]] = {}
    for item in account_targets:
        d = item["domain"]
        if d not in domain_url_map:
            domain_url_map[d] = item["target_url"]
        for account in item.get("accounts", []) or []:
            username = str(account.get("username", "")).strip() if isinstance(account, dict) else str(account).strip()
            if username and username not in domain_accounts.setdefault(d, []):
                domain_accounts[d].append(username)

    for item in account_targets:
        key = item.get("key", item["domain"])
        bot_state.context_locks[key] = asyncio.Lock()
        bot_state.cf_verified[key] = True
        bot_state.submission_count[key] = 0

    site_count = len({item.get("domain") for item in account_targets if item.get("domain")})
    logger.info(
        "✅ [Browser] %s target domain+tài khoản (%s site trình duyệt) đăng ký xong",
        len(account_targets),
        site_count,
    )

    cdp_port = getattr(Config, "EDGE_CDP_PORT", 9222)
    cdp_ready = _check_edge_cdp_port_reachable(cdp_port)
    if not cdp_ready:
        max_retries = 7
        for attempt in range(1, max_retries + 1):
            logger.info(f"⏳ [Edge-CDP] Cổng {cdp_port} chưa phản hồi — thử lại ({attempt}/{max_retries}, mỗi 2s)...")
            await asyncio.sleep(2.0)
            if _check_edge_cdp_port_reachable(cdp_port):
                cdp_ready = True
                break

    if cdp_ready:
        logger.info(f"✅ [Edge-CDP] Cổng {cdp_port} đang mở — Edge sẵn sàng nhận kết nối")
        try:
            await _tab_pool.init(domain_url_map=domain_url_map, domain_accounts=domain_accounts)
            logger.info(f"✅ [TabPool] Đã gán tab riêng cho {len(domain_url_map)} domain")
        except Exception as e:
            logger.warning(f"⚠️ [TabPool] Không init được ngay lúc preload ({e}) — sẽ tự thử lại kiểu lazy")
    else:
        # Không để một pool chưa khởi tạo tiếp tục chạy lazy rồi thử kết nối
        # lại trong từng submit, gây chậm khoảng thời gian timeout CDP.
        _tab_pool = None
        logger.error(
            f"❌ [Edge-CDP] Cổng {cdp_port} KHÔNG phản hồi — Edge CHƯA chạy ở chế độ debug! "
            f"Các submit sẽ fail-fast cho tới khi Edge được khởi động đúng chế độ debug."
        )


# ============================================================
# PAGE CLEAN-UP AFTER SUBMIT
# ============================================================
async def _reload_page_and_refill(page, domain: str, target_url: str, key: str):
    try:
        edge_restore()
        await page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=int(float(_site_profile_value(
                domain,
                "navigation_timeout_seconds",
                float(getattr(Config, "PAGE_NAVIGATION_TIMEOUT", 10000)) / 1000.0,
            )) * 1000),
        )
        if domain in ("livemm88.net", "liverr88.net"):
            await open_mm88_code_form(page)
        await scroll_to_input_fields(page)
        await _close_unwanted_popups(page)
        settle = float(_site_profile_value(
            domain,
            "form_settle_seconds",
            getattr(Config, "MM88_FORM_SETTLE_SECONDS", 0.05)
            if domain == "livemm88.net"
            else getattr(Config, "FORM_SETTLE_SECONDS", 0.03),
        ))
        await asyncio.sleep(max(0.02, settle))
        _invalidate_input_cache(key)
        return True
    except Exception as e:
        logger.warning(f"⚠️ [{domain}] Lỗi reload trang sau submit: {e}")
        return False


async def _quick_clean_page(page, key: str, domain: str = "") -> bool:
    try:
        # Đóng popup kết quả bằng nút đã biết TRƯỚC (Đồng ý / Tiếp tục / Xác
        # nhận). Trước đây các nút này không nằm trong danh sách từ khoá nên
        # popup bị coi là "kẹt" và MỖI LẦN submit đều bị reload cả trang.
        dismissed = {"clicked": 0, "remaining": 0}
        if domain:
            dismissed = await _dismiss_result_popup(page, domain)
        popup_state = await _close_unwanted_popups(page)
        # Nút đóng đã biết mà popup vẫn còn (vd xx88: popup không có class
        # modal/popup nên _close_unwanted_popups không nhìn thấy) -> escalate.
        if dismissed.get("remaining"):
            logger.debug(f"⚠️ [{key}] Popup kết quả còn sau khi bấm nút đóng — escalate reload")
            return False
        if popup_state.get("stuck"):
            # Overlay vẫn hiển thị sau khi thử đóng bằng JS. Trả về False để
            # _clean_page_after_submit() escalate sang full reload ngay cho
            # lần submit này, thay vì để lại popup che input cho tới khi đủ
            # FULL_RELOAD_EVERY_N lần mới được dọn.
            logger.debug(f"⚠️ [{key}] Popup còn cứng đầu sau quick-clean — escalate reload")
            return False
        await page.evaluate(
            """
            () => {
                const inputs = document.querySelectorAll('input:not([type="hidden"])');
                for (const inp of inputs) {
                    try {
                        const placeholder = (inp.placeholder || '').toLowerCase();
                        const isUsername = placeholder.includes('tài khoản')
                            || placeholder.includes('tên người dùng')
                            || placeholder.includes('tai khoan')
                            || placeholder.includes('ten nguoi dung')
                            || inp.id === 'account-code'
                            || inp.name === 'username';
                        if (isUsername && (inp.value || '').trim()) continue;
                        const proto = inp.tagName === 'TEXTAREA'
                            ? window.HTMLTextAreaElement.prototype
                            : window.HTMLInputElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                        setter.call(inp, '');
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                    } catch (e) {}
                }
            }
            """
        )
        return True
    except Exception as e:
        logger.debug(f"⚠️ [{key}] Quick-clean lỗi: {e}")
        return False


async def _clean_page_after_submit(page, domain: str, target_url: str, key: str, force_full: bool = False):
    count = bot_state._submits_since_full_reload.get(key, 0) + 1
    threshold = int(_site_profile_value(
        domain,
        "full_reload_every",
        getattr(Config, "FULL_RELOAD_EVERY_N", 100),
    ))

    if not force_full and count < threshold:
        ok = await _quick_clean_page(page, key, domain)
        if ok:
            bot_state._submits_since_full_reload[key] = count
            return True

    bot_state._submits_since_full_reload[key] = 0
    return await _reload_page_and_refill(page, domain, target_url, key)


# ============================================================
# SUBMIT (bản trình duyệt) — điểm vào chính của module này
# ============================================================
async def submit_code_browser(user: str, code: str, target_url: str, systems: dict) -> dict:
    """Serialize form mutations for one ``(domain, account)`` pair.

    A page keeps the username and code in shared DOM state. Without this
    outer lock, two fanout tasks for the same account can overwrite each
    other's inputs between fill and click, producing validation errors.
    """
    domain = _normalize_domain(target_url)
    key = f"{domain}|{user}"
    lock = bot_state.context_locks.setdefault(key, asyncio.Lock())
    async with lock:
        return await _submit_code_browser_locked(user, code, target_url, systems)


def _is_decisive_result(text: str) -> bool:
    """Text đủ rõ để kết luận (thành công / thất bại / rate-limit). Text mơ hồ
    hoặc rỗng → vẫn phải đối chiếu DOM, không được coi là kết quả."""
    if not text:
        return False
    return classify_result(text) in (
        ResultStatus.SUCCESS_POINTS,
        ResultStatus.SUCCESS_NO_POINTS,
        ResultStatus.FAILED,
        ResultStatus.RATE_LIMITED,
    )


async def _resolve_result(api_fut, dom_coro) -> str:
    """Chạy đua response API (api_fut, có thể None) với quét DOM (dom_coro).

    - API có kết quả RÕ RÀNG trước → dùng ngay, huỷ quét DOM (tiết kiệm thời gian).
    - DOM xong trước → dùng DOM; nếu DOM chỉ ra text rác/mơ hồ ("Close"…) thì
      cho API thêm tối đa 1s để cứu.
    - API không bao giờ tới → không chặn: DOM vẫn chạy hết nhịp của nó.
    """
    if api_fut is None:
        return await dom_coro
    dom_task = asyncio.ensure_future(dom_coro)
    try:
        await asyncio.wait({api_fut, dom_task}, return_when=asyncio.FIRST_COMPLETED)
        if api_fut.done() and _is_decisive_result(api_fut.result()):
            return api_fut.result()
        text = await dom_task
        if not _is_decisive_result(text) and not api_fut.done():
            try:
                await asyncio.wait_for(asyncio.shield(api_fut), timeout=1.0)
            except Exception:
                pass
        if api_fut.done() and _is_decisive_result(api_fut.result()):
            return api_fut.result()
        return text
    finally:
        if not dom_task.done():
            dom_task.cancel()


class _ApiTimer:
    """Quan sát request do CHÍNH TRANG phát ra (không tự gọi API, không bypass
    Turnstile) để:
      * đo RTT API thật: từ lúc trang gửi POST tới lúc nhận response;
      * (nếu profile có public_api_endpoint) đọc sẵn nội dung response vào
        `text_future`, để bot không phải chờ popup DOM hiện ra.
    KHÔNG chặn: nếu response không bao giờ tới thì DOM observer vẫn chạy bình
    thường, không mất thêm giây nào."""

    _SKIP = ("challenges.cloudflare.com", "turnstile", "cdn-cgi")

    def __init__(self, page, endpoint: str | None = None):
        self._page = page
        self._endpoint = endpoint or None
        self._t0: dict = {}
        self._tasks: set = set()
        self.api_ms: float | None = None
        self.status: int | None = None
        self.click_t: float | None = None
        self.pre_api_ms: float | None = None  # click -> trang phát POST
        self.text_future = asyncio.get_running_loop().create_future() if self._endpoint else None

    def _wanted(self, url: str, method: str) -> bool:
        if method.upper() not in ("POST", "PUT", "PATCH"):
            return False
        low = url.lower()
        if any(s in low for s in self._SKIP):
            return False
        return self._endpoint in url if self._endpoint else True

    def _on_request(self, request):
        try:
            if request.resource_type in ("fetch", "xhr") and self._wanted(request.url, request.method):
                now = time.perf_counter()
                self._t0[id(request)] = now
                if self.click_t is not None and self.pre_api_ms is None:
                    self.pre_api_ms = (now - self.click_t) * 1000.0
        except Exception:
            pass

    def _on_response(self, response):
        try:
            req = response.request
            t0 = self._t0.pop(id(req), None)
            if t0 is not None and self.api_ms is None:
                self.api_ms = (time.perf_counter() - t0) * 1000.0
                self.status = response.status
            if (
                self.text_future is not None
                and not self.text_future.done()
                and self._wanted(response.url, req.method)
            ):
                task = asyncio.ensure_future(self._read(response))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except Exception:
            pass

    async def _read(self, response):
        try:
            text = await _response_to_result_text(response)
        except Exception:
            text = ""
        if self.text_future is not None and not self.text_future.done():
            self.text_future.set_result(text)

    def mark_click(self):
        self.click_t = time.perf_counter()

    def attach(self):
        try:
            self._page.on("request", self._on_request)
            self._page.on("response", self._on_response)
        except Exception:
            pass

    def detach(self):
        for evt, fn in (("request", self._on_request), ("response", self._on_response)):
            try:
                self._page.remove_listener(evt, fn)
            except Exception:
                pass
        for t in list(self._tasks):
            t.cancel()


async def _submit_code_browser_locked(user: str, code: str, target_url: str, systems: dict) -> dict:
    """Trả về kết quả submit chuẩn hóa gồm {"success", "message",
    "has_points", "is_wrong_code", ...}. Khi gặp lỗi HẠ TẦNG (mất tab, mất
    CDP, không tìm thấy input, Cloudflare/captcha chặn...) trả thêm khoá
    "_infra_failure": True — main_script.py.submit_code_safe() dựa vào cờ
    này để ghi nhận lỗi sang browser ngay, không đứng chờ."""
    start_time = time.time()
    phase_start = time.perf_counter()
    submit_perf_start = phase_start
    timings: dict[str, float] = {}
    domain = _normalize_domain(target_url)
    key = f"{domain}|{user}"

    def mark_phase(name: str) -> None:
        nonlocal phase_start
        now = time.perf_counter()
        timings[name] = round((now - phase_start) * 1000.0, 2)
        phase_start = now

    def timed_result(payload: dict) -> dict:
        timings["total_submit_ms"] = round((time.perf_counter() - submit_perf_start) * 1000.0, 2)
        payload["latency_ms"] = dict(timings)
        return payload

    if _tab_pool is None:
        return {"success": False, "message": "Browser chưa sẵn sàng (no TabPool)", "_infra_failure": True}

    if key not in bot_state.context_locks:
        bot_state.context_locks[key] = asyncio.Lock()
        bot_state.cf_verified[key] = True
        bot_state.submission_count.setdefault(key, 0)

    try:
        tab_entry, page, tab_lock = await _tab_pool.acquire(domain=domain, account=user)
    except Exception as e:
        logger.warning(f"⚠️ [Browser|{domain}] Không lấy được tab: {e}")
        return {"success": False, "message": f"No tab: {e}", "_infra_failure": True}

    api_timer = None
    try:
        async with tab_lock:
            tab_entry["reserved"] = False
            mark_phase("tab_wait_ms")
            bot_state.account_pages[key] = page

            if page.is_closed():
                context = await get_or_launch_browser_context("shared")
                page = await context.new_page()
                await _setup_page_performance(page, domain)
                tab_entry["page"] = page
                bot_state.account_pages[key] = page
                _invalidate_page_input_cache(page)

            input_key = f"{key}|{id(page)}"

            try:
                page_url = page.url
            except Exception:
                page_url = ""

            if not _page_matches_target(page_url, target_url, domain):
                logger.info(f"🌐 [{domain}] Điều hướng tới {target_url}")
                edge_restore()
                try:
                    await page.goto(
                        target_url,
                        wait_until="domcontentloaded",
                        timeout=int(float(_site_profile_value(
                            domain,
                            "navigation_timeout_seconds",
                            float(getattr(Config, "PAGE_NAVIGATION_TIMEOUT", 10000)) / 1000.0,
                        )) * 1000),
                    )
                    if domain in ("livemm88.net", "liverr88.net"):
                        await open_mm88_code_form(page)
                    await scroll_to_input_fields(page)
                    settle = float(_site_profile_value(
                        domain,
                        "form_settle_seconds",
                        getattr(Config, "MM88_FORM_SETTLE_SECONDS", 0.05)
                        if domain == "livemm88.net"
                        else getattr(Config, "FORM_SETTLE_SECONDS", 0.03),
                    ))
                    await asyncio.sleep(max(0.02, settle))
                    _invalidate_input_cache(key)
                except Exception as e:
                    return {"success": False, "message": f"Goto failed: {e}", "_infra_failure": True}

                # Không kiểm tra widget ngay sau goto. Nhiều site render
                # Turnstile thường trực trong form, vì vậy chỉ thấy iframe/
                # .cf-turnstile chưa đủ để kết luận trang bị chặn. Kiểm tra
                # challenge sau khi thử tìm form; nếu không có ô code thì mới
                # phân loại là Cloudflare interstitial.

            # Luôn đưa đúng tab/account lên trước, kể cả khi tab đã ở đúng URL
            # và không đi qua nhánh navigation ở trên. Trước đây lượt đầu có
            # thể hiện trên Edge nhưng các lượt sau vẫn nhập âm thầm ở tab nền.
            await _wake_tab_for_submit(page)
            edge_restore()
            mark_phase("navigation_ms")
            cached_entry = bot_state._input_cache.get(input_key)
            cache_was_fresh = bool(cached_entry and (time.time() - cached_entry[2]) < bot_state._input_cache_ttl)
            username_input, code_input = await find_input_fields(page, cache_key=input_key, domain=domain)

            if not code_input:
                await asyncio.sleep(0.05)
                _invalidate_input_cache(input_key)
                cache_was_fresh = False
                username_input, code_input = await find_input_fields(page, cache_key=input_key, domain=domain)

            if not code_input and domain == "liverr88.net":
                # RR88 can leave a stale shell/tab without the form. Reload
                # once and rediscover inputs before classifying the issue as a
                # UI change; do not spend repeated retries on the same DOM.
                try:
                    await page.goto(
                        target_url,
                        wait_until="domcontentloaded",
                        timeout=int(float(_site_profile_value(domain, "navigation_timeout_seconds", 12.0)) * 1000),
                    )
                    await _wake_tab_for_submit(page)
                    await scroll_to_input_fields(page)
                    _invalidate_input_cache(input_key)
                    username_input, code_input = await find_input_fields(
                        page, cache_key=input_key, domain=domain,
                    )
                except Exception:
                    pass

            if not code_input:
                if domain in ("livemm88.net", "liverr88.net") and await open_mm88_code_form(page):
                    _invalidate_input_cache(input_key)
                    username_input, code_input = await find_input_fields(page, cache_key=input_key, domain=domain)

            if not code_input:
                if await is_cloudflare_present(page, domain=domain):
                    logger.warning(f"⚠️ [{domain}] Cloudflare challenge không có form — cần xác minh thủ công")
                    return timed_result({
                        "success": False,
                        "message": "Cloudflare challenge",
                        "failure_kind": "CLOUDFLARE_CHALLENGE",
                        "_infra_failure": True,
                        "keep_page": True,
                    })
                return timed_result({
                    "success": False,
                    "message": "Không tìm thấy ô nhập code sau reload (site có thể đã đổi UI)",
                    "failure_kind": "SITE_UI_CHANGED",
                    "_infra_failure": True,
                })

            if not cache_was_fresh:
                await scroll_to_input_fields(page)

            preserve_prefilled = False
            try:
                if username_input and getattr(Config, "PRESERVE_PREFILLED_USERNAME", True):
                    try:
                        current_username = (await username_input.input_value()).strip()
                        preserve_prefilled = bool(current_username) and current_username == str(user).strip()
                        if preserve_prefilled:
                            logger.debug(f"✅ [{domain}|{user}] giữ tài khoản đã điền sẵn, chỉ nhập code")
                    except Exception:
                        preserve_prefilled = False
                # Gộp fill + đọc xác minh vào một round-trip CDP thay vì
                # fill username, fill code, đọc username, đọc code riêng lẻ.
                verify = await page.evaluate(
                    REACT_FILL_VERIFY_JS,
                    [username_input, code_input, user, code, not preserve_prefilled],
                )
                mark_phase("input_fill_ms")
                actual_user = (verify.get("actualUser") or "").strip()
                actual_code = (verify.get("actualCode") or "").strip()
                if username_input and not preserve_prefilled and actual_user != str(user).strip():
                    raise RuntimeError(
                        f"Account input mismatch: expected={user!r} actual={actual_user!r}"
                    )
                if actual_code.upper() != str(code).strip().upper():
                    raise RuntimeError(
                        f"Code input mismatch: expected={code!r} actual={actual_code!r}"
                    )
            except Exception as e:
                _invalidate_input_cache(input_key)
                username_input, code_input = await find_input_fields(page, cache_key=input_key, domain=domain)
                if code_input:
                    try:
                        verify = await page.evaluate(
                            REACT_FILL_VERIFY_JS,
                            [username_input, code_input, user, code, not preserve_prefilled],
                        )
                        actual_user = (verify.get("actualUser") or "").strip()
                        actual_code = (verify.get("actualCode") or "").strip()
                        if username_input and not preserve_prefilled and actual_user != str(user).strip():
                            return {
                                "success": False,
                                "message": f"Account input mismatch after retry: {actual_user!r}",
                                "_infra_failure": True,
                            }
                        if actual_code.upper() != str(code).strip().upper():
                            return {
                                "success": False,
                                "message": f"Code input mismatch after retry: {actual_code!r}",
                                "_infra_failure": True,
                            }
                    except Exception as e2:
                        return {"success": False, "message": f"Fill error: {e2}", "_infra_failure": True}
                else:
                    return {"success": False, "message": f"Fill error: {e}", "_infra_failure": True}

            # RR88 dùng Turnstile ngay trong form; API sẽ trả HTTP 400 nếu
            # gửi khi chưa có captchaToken. Không submit mù: giữ nguyên tab
            # để người dùng hoàn tất xác minh trong Edge.
            if domain == "liverr88.net":
                try:
                    rr88_challenge_pending = await is_cloudflare_present(page, domain=domain)
                except Exception:
                    rr88_challenge_pending = True
                if rr88_challenge_pending:
                    logger.warning(
                        "⏸️ [RR88] Chưa xác minh Turnstile — giữ tab, không gửi request lỗi 400"
                    )
                    mark_phase("challenge_wait_ms")
                    return timed_result({
                        "success": False,
                        "message": "RR88 Turnstile verification pending",
                        "status": "PENDING_VERIFICATION",
                        "failure_kind": "PENDING_VERIFICATION",
                        "keep_page": True,
                    })

            # QQ88/HI88 expose the Cloudflare verification button only after
            # the user clicks "Kiểm tra ngay". Their required order is:
            # fill code -> click check -> wait for Xác thực -> click Xác thực.
            if domain not in {"tangquaqq88.com", "hi88-freecode.pages.dev", "liverr88.net"}:
                cf_wait_deadline = time.time() + float(getattr(Config, "CF_WAIT_SECONDS", 1.0))
                while time.time() < cf_wait_deadline:
                    try:
                        cf_state = await page.evaluate(
                            """
                            () => {
                                const hasWidget = !!document.querySelector(
                                    '.cf-turnstile, [data-sitekey], iframe[src*="turnstile"], '
                                    + 'iframe[src*="challenges.cloudflare.com"]'
                                );
                                if (!hasWidget) return {hasWidget: false, passed: false};
                                const text = (document.body.innerText || '').toLowerCase();
                                const passed = ['thành công', 'thanh cong', 'verified', 'success']
                                    .some((marker) => text.includes(marker));
                                return {hasWidget: true, passed};
                            }
                            """
                        )
                    except Exception:
                        cf_state = {"hasWidget": False, "passed": False}
                    if not cf_state.get("hasWidget") or cf_state.get("passed"):
                        break
                    await asyncio.sleep(max(0.05, float(getattr(Config, "CF_POLL_INTERVAL", 0.10))))

            mark_phase("cf_wait_ms")
            try:
                # HI88/QQ88 chỉ cần snapshot form/modal để loại stale result.
                # Không gửi toàn bộ main/#app (thường chứa banner/link dài) qua
                # CDP trước mỗi submit; các site khác vẫn giữ snapshot rộng để
                # tương thích với layout cũ.
                pre_click_scopes = (
                    'form, [role="dialog"], .modal, [role="alert"], [role="status"]'
                    if domain in {"tangquaqq88.com", "hi88-freecode.pages.dev"}
                    else 'form, [role="dialog"], .modal, main, #app, #root'
                )
                pre_click_text = await page.evaluate(
                    """
                    (scopeSelector) => {
                        const scopes = document.querySelectorAll(scopeSelector);
                        let text = '';
                        for (const scope of scopes) {
                            text += (scope.innerText || '') + '\n';
                            if (text.length >= 30000) break;
                        }
                        return text.slice(0, 30000);
                    }
                    """,
                    pre_click_scopes,
                )
            except Exception:
                pre_click_text = ""

            mark_phase("snapshot_ms")

            # Đăng ký listener TRƯỚC khi bấm (bắt được cả POST do nút Xác thực
            # của QQ88/HI88 phát ra). Không chặn: chỉ quan sát.
            _profile = get_site_profile(domain)
            api_timer = _ApiTimer(page, getattr(_profile, "public_api_endpoint", None))
            api_timer.attach()
            api_timer.mark_click()
            clicked = await click_submit_fast(page, domain=domain)
            verified_clicked = await click_verification_button_if_present(page, domain=domain)
            if verified_clicked:
                logger.info(f"✅ [Browser|{domain}] đã bấm nút Xác thực và Cloudflare đã xác minh")
            elif domain in {"tangquaqq88.com", "hi88-freecode.pages.dev"}:
                # Trang có thể đã xác minh từ trước và không còn render lại
                # nút Xác thực. Khi đó chỉ dọn popup không cần thiết rồi đọc
                # kết quả; không click lại một nút đã biến mất.
                if await _cf_already_passed(page, domain=domain):
                    await _close_unwanted_popups(page)
                    logger.info(f"✅ [Browser|{domain}] Cloudflare đã xác minh từ trước")
                    verified_clicked = True
                else:
                # Turnstile may still be verifying after the check button was
                # clicked. Stop this attempt before result recording: that
                # callback can clean/reload the page and restart the widget.
                # Keep the current page alive so a human can finish the
                # verification in the same Edge profile.
                    try:
                        challenge_pending = await is_cloudflare_present(page, domain=domain)
                    except Exception:
                        challenge_pending = False
                    if challenge_pending:
                        logger.warning(
                            f"⚠️ [{domain}] Turnstile chưa hoàn tất — giữ nguyên trang, "
                            "không reload và chờ xác minh thủ công"
                        )
                        mark_phase("challenge_wait_ms")
                        arm_verification_watcher(page, domain=domain, code=code, user=user)
                        return timed_result({
                            "success": False,
                            "message": "Turnstile verification pending",
                            "status": "PENDING_VERIFICATION",
                            "failure_kind": "PENDING_VERIFICATION",
                            "keep_page": True,
                        })
            mark_phase("challenge_wait_ms")
            click_elapsed = time.time() - start_time
            logger.info(f"🚀 [Browser|{user}] SUBMIT {code} ({click_elapsed:.2f}s)")

            result_text = ""
            timeout_by_domain = getattr(Config, "RESULT_DETECTION_TIMEOUT_BY_DOMAIN", {})
            default_timeout_ms = getattr(Config, "RESULT_DETECTION_TIMEOUT", 5000)
            profile_timeout_ms = _site_profile_value(
                domain,
                "result_timeout_ms",
                default_timeout_ms,
            )
            result_timeout_ms = timeout_by_domain.get(domain, default_timeout_ms)
            if domain not in timeout_by_domain:
                result_timeout_ms = profile_timeout_ms
            result_timeout_s = float(result_timeout_ms) / 1000.0
            fast_window_ms = int(max(0.0, float(_site_profile_value(
                domain,
                "selector_fast_window_seconds",
                getattr(Config, "RESULT_SELECTOR_FAST_WINDOW_SECONDS", 0.80),
            ))) * 1000)

            async def _dom_wait() -> str:
                # Chờ ngay trong page để giảm round-trip CDP; selector theo
                # domain được ưu tiên, sau fast window mới dùng selector chung.
                try:
                    handle = await page.wait_for_function(
                        _RESULT_WAIT_JS,
                        arg=[
                            _get_domain_result_selectors(domain),
                            _PRIORITY_RESULT_SELECTORS,
                            pre_click_text or "",
                            list(_TRANSIENT_CF_PATTERNS),
                            fast_window_ms,
                            int(time.time() * 1000),
                            list(_RESULT_KEYWORDS),
                        ],
                        timeout=max(300.0, result_timeout_s * 1000.0),
                        polling=max(20.0, float(_site_profile_value(
                            domain,
                            "result_poll_interval",
                            0.10,
                        )) * 1000.0),
                    )
                    try:
                        return str(await handle.json_value() or "").strip()
                    finally:
                        try:
                            await handle.dispose()
                        except Exception:
                            pass
                except Exception:
                    # Timeout hoặc page navigation: quét DOM rộng đúng một lần.
                    try:
                        return await detect_result_text(
                            page, domain=domain, before_text=pre_click_text, selector_only=False
                        )
                    except Exception:
                        return ""

            result_text = await _resolve_result(
                api_timer.text_future if api_timer is not None else None,
                _dom_wait(),
            )

            elapsed = time.time() - start_time
            mark_phase("result_wait_ms")
            if api_timer is not None and api_timer.api_ms is not None:
                timings["api_ms"] = round(api_timer.api_ms, 1)
                timings["api_status"] = api_timer.status
            if api_timer is not None and api_timer.pre_api_ms is not None:
                timings["pre_api_ms"] = round(api_timer.pre_api_ms, 1)

            status = classify_result(result_text)
            if status.value in ("NO_RESULT", "AMBIGUOUS") and _needs_manual_verify(result_text):
                logger.warning(
                    f"⚠️ [{domain}] Site yêu cầu xác thực riêng (captcha ảnh) — "
                    f"result_text={result_text[:200]!r}"
                )
                arm_verification_watcher(page, domain=domain, code=code, user=user)
                return timed_result({
                    "success": False,
                    "message": "Cần xác thực thủ công",
                    "status": "PENDING_VERIFICATION",
                    "failure_kind": "PENDING_VERIFICATION",
                    "keep_page": True,
                })

            callbacks = {
                "take_screenshot": take_result_screenshot,
                "clean_page": _clean_page_after_submit,
                "append_history": _append_code_history_safe,
            }
            debug_info = None
            if status.value == "NO_RESULT":
                debug_info = {
                    "code": code, "user": user, "domain": domain,
                    "clicked_submit_button": clicked,
                    "pre_click_text_len": len(pre_click_text or ""),
                    "result_timeout_s": result_timeout_s,
                    "page_url": page.url if page else None,
                }

            outcome = await record_outcome(
                status=status, page=page, user=user, code=code,
                target_url=target_url, domain=domain, key=key, elapsed=elapsed,
                result_text=result_text, systems=systems, callbacks=callbacks,
                debug_info=debug_info,
            )
            result = dict(outcome.result)
            mark_phase("cleanup_ms")
            timings["total_submit_ms"] = round((time.perf_counter() - submit_perf_start) * 1000.0, 2)
            result["latency_ms"] = dict(timings)
            # record_outcome() ghi dòng dashboard TRƯỚC khi các pha được chốt
            # → bổ sung lại cột Latency ở đây.
            update_latency(domain, user, code, timings)
            return result

    except Exception as e:
        elapsed = time.time() - start_time
        err_str = str(e)
        if "Target page, context or browser has been closed" in err_str or "TargetClosedError" in type(e).__name__:
            try:
                context = await get_or_launch_browser_context("shared", force_reconnect=True)
                new_page = await context.new_page()
                bot_state.account_pages[key] = new_page
                await _setup_page_performance(new_page, domain)
                await new_page.goto(target_url, wait_until="domcontentloaded", timeout=10000)
                _invalidate_input_cache(key)
            except Exception:
                pass
        try:
            systems["performance_monitor"].record_task("submit_code", elapsed, False)
        except Exception:
            pass
        logger.error(f"❌ [Browser|{domain}] {e}")
        return {"success": False, "message": str(e), "_infra_failure": True}
    finally:
        try:
            tab_entry["reserved"] = False
        except Exception:
            pass
        if api_timer is not None:
            api_timer.detach()


# ============================================================
# WATCHDOGS — chỉ giữ phần KHÔNG liên quan captcha (giữ Edge/tab sống)
# ============================================================
_edge_cdp_was_down = False


async def browser_watchdog():
    """Single ordered watchdog: CDP reachability/reconnect, then stale tabs."""
    interval = max(5.0, float(getattr(Config, "CDP_PING_INTERVAL", 60.0)))
    port = getattr(Config, "EDGE_CDP_PORT", 9222)
    global _edge_cdp_was_down
    while bot_state.is_running:
        try:
            await asyncio.sleep(interval)
            reachable = _check_edge_cdp_port_reachable(port, timeout=2.0)
            if not reachable:
                if not _edge_cdp_was_down:
                    logger.critical(f"❌ [Browser-Watchdog] Mất kết nối CDP {port}; đang reconnect")
                    _edge_cdp_was_down = True
                try:
                    await get_or_launch_browser_context("shared", force_reconnect=True)
                    _edge_cdp_was_down = False
                    logger.info(f"✅ [Browser-Watchdog] CDP {port} đã kết nối lại")
                except Exception as exc:
                    logger.warning(f"⚠️ [Browser-Watchdog] Reconnect thất bại: {exc}")
                continue
            if _edge_cdp_was_down:
                logger.info(f"✅ [Browser-Watchdog] CDP {port} phản hồi trở lại")
                _edge_cdp_was_down = False
            stale_keys = [key for key, page in list(bot_state.account_pages.items()) if safe_is_closed(page)]
            for key in stale_keys:
                # Không tự ctx.new_page() ở đây: page mới không được đăng ký
                # vào TabPool._domains, nên acquire() lần sau có thể mở thêm
                # một page khác trong khi page watchdog vừa tạo vẫn bị bỏ rơi
                # (tăng dần số renderer/RAM sau mỗi lần CDP chập chờn).
                # Xoá mapping stale để collect_garbage() loại entry đóng khỏi
                # pool; acquire() sẽ tạo lại đúng slot/domain khi có việc mới.
                bot_state.account_pages.pop(key, None)
                bot_state._input_cache.pop(key, None)
                logger.info(
                    "♻️ [Browser-Watchdog] Tab stale %s đã được gỡ; "
                    "TabPool sẽ tạo lại đúng slot khi cần",
                    key,
                )
            if _tab_pool is not None:
                try:
                    await _tab_pool.collect_garbage(
                        idle_ttl=getattr(Config, "TAB_POOL_IDLE_TTL", 900.0),
                        min_tabs_per_domain=getattr(Config, "TAB_POOL_MIN_TABS_PER_DOMAIN", 2),
                    )
                    memory_stats = await _tab_pool.cleanup_idle_memory(
                        compact_idle_seconds=getattr(Config, "TAB_POOL_MEMORY_COMPACT_IDLE_SECONDS", 300.0),
                        reload_idle_seconds=getattr(Config, "TAB_POOL_MEMORY_RELOAD_IDLE_SECONDS", 1800.0),
                    )
                    if memory_stats.get("compacted") or memory_stats.get("reloaded"):
                        logger.info("🧹 [TabPool-Memory] %s", memory_stats)
                except Exception as exc:
                    logger.debug(f"⚠️ [TabPool-GC] cleanup lỗi: {exc}")
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug(f"⚠️ browser_watchdog error: {exc}")


async def cleanup_browsers():
    global _shared_context, _edge_browser, _pw_instance

    _shared_context = None

    if _edge_browser is not None:
        try:
            await _edge_browser.close()
        except Exception:
            pass
        _edge_browser = None

    if _pw_instance is not None:
        try:
            await _pw_instance.stop()
        except Exception:
            pass
        _pw_instance = None
