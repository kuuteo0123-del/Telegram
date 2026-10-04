from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from dashboard import update_dashboard
from logger_setup import logger
# take_result_screenshot dùng bản dùng chung trong media_helpers.py (module
# trung lập, không phụ thuộc main_script.py — tránh import vòng).
from media_helpers import take_result_screenshot

SUCCESS_KW = [
    "THÀNH CÔNG", "THANH CONG", "SUCCESS", "COMPLETED",
    "ĐÃ NHẬN", "DA NHAN", "RECEIVED", "ADDED", "AWARDED",
    "CONGRATULATIONS", "APPROVED", "ACCEPTED",
]

FAILED_KW = [
    "SAI", "LỖI", "LOI",
    "ĐÃ SỬ", "DA SU", "ĐÃ DÙNG",
    "FAILED", "ERROR", "INVALID",
    "KHÔNG ĐÚNG", "KHÔNG TỒN TẠI", "KHÔNG HỢP LỆ",
    "HẾT HẠN", "ĐÃ HẾT", "EXPIRED",
    "CODE ĐÃ SỬ DỤNG HẾT", "CODE DA SU DUNG HET",
    "CODE ĐÃ HẾT HẠN", "CODE DA HET HAN",
    "MÃ ĐÃ ĐƯỢC SỬ DỤNG", "MA DA DUOC SU DUNG",
    "ĐÃ ĐƯỢC SỬ DỤNG", "DA DUOC SU DUNG",
    "KHÔNG CÒN HỢP LỆ", "KHONG CON HOP LE",
    "MÃ ĐÃ HẾT HẠN", "MA DA HET HAN",
    "CODE USED UP", "CODE HAS EXPIRED",
    "NOT FOUND", "NOT EXIST", "KHÔNG TÌM THẤY",
    "CODE NOT USED", "CODE_NOT_USED",
    "THAT BAI", "THẤT BẠI",
    "REJECTED", "DECLINED",
]

TOO_MANY_KW = [
    "TOO MANY",
    "RATE LIMIT",
    "QUÁ NHIỀU",
    "THÊM SAU",
    "THỬ LẠI SAU",
]

# "429" chỉ tính là rate limit khi đứng riêng, KHÔNG nằm trong số khác
# (vd "1429 điểm", "25,429 điểm" trước đây bị đọc nhầm là RATE_LIMITED).
_HTTP_429_RE = re.compile(r"(?<![\d.,])429(?![\d.,]?\d)(?!\s*(?:ĐIỂM|DIEM|XU|COIN|POINT))")

NEGATIVE_SUCCESS_KW = (
    "NOT ACCEPTED", "NOT ADDED", "NOT APPROVED", "UNSUCCESSFUL",
    "KHÔNG THÀNH CÔNG", "KHONG THANH CONG",
)

POINT_KW = ["ĐIỂM", "XU", "COIN", "POINT"]

_SHORT_KW = {"SAI", "LOI", "LỖI", "XU", "ĐIỂM", "DIEM"}
_SHORT_KW_PATTERNS = {
    kw: re.compile(rf"(?:^|[^\w]){re.escape(kw)}(?:[^\w]|$)")
    for kw in _SHORT_KW
}


def _kw_matches(text_upper: str, keyword: str) -> bool:
    pattern = _SHORT_KW_PATTERNS.get(keyword)
    if pattern is not None:
        return bool(pattern.search(text_upper))
    return keyword in text_upper


class ResultStatus(str, Enum):
    SUCCESS_POINTS = "SUCCESS_POINTS"
    SUCCESS_NO_POINTS = "SUCCESS_NO_POINTS"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"
    NO_RESULT = "NO_RESULT"
    RATE_LIMITED = "RATE_LIMITED"


@dataclass(frozen=True)
class Outcome:
    status: ResultStatus
    result: dict


def classify_result(raw_text: str) -> ResultStatus:
    text = raw_text or ""
    stripped = text.strip()
    upper = text.upper()

    is_rate_limited = any(kw in upper for kw in TOO_MANY_KW) or bool(_HTTP_429_RE.search(upper))
    if is_rate_limited:
        return ResultStatus.RATE_LIMITED

    if any(marker in upper for marker in NEGATIVE_SUCCESS_KW):
        return ResultStatus.FAILED

    is_success = any(_kw_matches(upper, kw) for kw in SUCCESS_KW)
    is_failed = any(_kw_matches(upper, kw) for kw in FAILED_KW)

    if is_success and not is_failed:
        has_points = any(_kw_matches(upper, kw) for kw in POINT_KW)
        return ResultStatus.SUCCESS_POINTS if has_points else ResultStatus.SUCCESS_NO_POINTS

    if len(stripped) < 3:
        return ResultStatus.NO_RESULT

    if is_failed:
        return ResultStatus.FAILED

    return ResultStatus.AMBIGUOUS


async def _build_no_result_debug_info(
    page, code: str, user: str, domain: str, clicked: bool,
    pre_click_text: str, result_timeout_s: float, is_hi88: bool,
) -> dict:
    """Debug info CHI TIẾT (snippet trang + capture MutationObserver HI88)
    — chỉ dùng khi caller KHÔNG tự truyền sẵn debug_info vào record_outcome
    (xem nhánh NO_RESULT bên dưới). main_script.py hiện tự build 1 bản
    debug_info đơn giản hơn và truyền thẳng vào — nếu có, dùng bản đó,
    không gọi lại hàm này (tránh tính 2 lần, tốn round-trip page.evaluate)."""
    debug_info: dict[str, Any] = {
        "code": code,
        "user": user,
        "domain": domain,
        "clicked_submit_button": clicked,
        "pre_click_text_len": len(pre_click_text or ""),
        "result_timeout_s": result_timeout_s,
        "page_url": None,
        "post_click_text_snippet": "",
    }
    try:
        debug_info["page_url"] = page.url
    except Exception:
        pass
    try:
        post_text = await page.evaluate("() => document.body.innerText || ''")
        debug_info["post_click_text_snippet"] = (post_text or "")[:800]
    except Exception:
        pass
    if is_hi88:
        try:
            debug_info["hi88_watcher_captures"] = await page.evaluate(
                "() => window.__hi88Captures || []"
            )
        except Exception:
            debug_info["hi88_watcher_captures"] = []
    return debug_info


def _safe_append_history(callbacks: Optional[dict], **kwargs) -> None:
    """Gọi callbacks['append_history'] nếu có — đây là hàm SYNC
    (append_code_history trong main_script.py không phải coroutine, chỉ
    put_nowait vào queue hoặc ghi file), nên KHÔNG await ở đây. An toàn bỏ
    qua nếu không có callback hoặc callback tự ném lỗi — ghi lịch sử không
    được phép làm sập luồng submit chính."""
    if not callbacks:
        return
    fn = callbacks.get("append_history")
    if not fn:
        return
    try:
        fn(**kwargs)
    except Exception as e:
        logger.debug(f"⚠️ append_history callback error: {e}")


async def record_outcome(
    *,
    systems: dict,
    page,
    user: str,
    code: str,
    target_url: str,
    domain: str,
    elapsed: float,
    raw_text: str = "",
    result_text: str = "",  # alias của raw_text — main_script.py gọi bằng tên này
    status: "ResultStatus | str | None" = None,  # nếu có sẵn thì dùng luôn, không tính lại
    key: str | None = None,  # main_script.py truyền "domain|user"
    # callbacks: dict các hàm (append_history, take_screenshot, clean_page,
    # reset_page...) — dùng callback thay vì import ngược main_script.py để
    # tránh import vòng. Hiện chỉ "append_history" và "take_screenshot" được
    # gọi tự động; "clean_page"/"reset_page" nhận nhưng không tự gọi ở đây.
    callbacks: Optional[dict] = None,
    debug_info: Optional[dict] = None,  # nếu main_script.py build sẵn thì dùng luôn
    clicked: bool = True,
    pre_click_text: str = "",
    result_timeout_s: float = 0.0,
    is_hi88: bool = False,
    **_ignored_kwargs: Any,  # nuốt tham số lạ phát sinh sau này thay vì crash
) -> Outcome:
    db = systems["db"]
    perf_mon = systems["performance_monitor"]

    final_raw_text = raw_text or result_text or ""
    key = key or f"{domain}|{user}"

    if status is None:
        status = classify_result(final_raw_text)
    elif not isinstance(status, ResultStatus):
        try:
            status = ResultStatus(status)
        except ValueError:
            # Chuỗi lạ không khớp enum nào (vd lỗi gõ tay) → tự phân loại
            # lại từ text cho an toàn, không để crash vì ValueError.
            logger.debug(f"⚠️ [{key}] status lạ '{status}' — tự phân loại lại từ raw_text")
            status = classify_result(final_raw_text)

    result_text = final_raw_text
    loop = asyncio.get_running_loop()

    # take_screenshot: cho phép caller ghi đè bằng callback riêng, mặc
    # định dùng bản dùng chung trong media_helpers.py.
    take_screenshot_fn = (callbacks or {}).get("take_screenshot") or take_result_screenshot

    # 1 điểm return duy nhất: mỗi nhánh chỉ gán `outcome`, để sau khi xác
    # định kết quả luôn chạy qua callbacks['clean_page'] đúng 1 lần ở cuối
    # (áp dụng cho MỌI trạng thái, kể cả RATE_LIMITED).
    if status == ResultStatus.RATE_LIMITED:
        from config import Config
        backoff_delay = max(
            0.1,
            float(getattr(Config, "RATE_LIMIT_BACKOFF_SECONDS", 5.0)),
        )
        logger.warning(
            f"🚫 [{user}|{domain}] Too Many Requests — backoff {backoff_delay:.1f}s"
        )
        await asyncio.sleep(backoff_delay)
        outcome = Outcome(
            status=status,
            result={"success": False, "message": f"RateLimit:{result_text[:60]}"},
        )

    elif status in (ResultStatus.SUCCESS_POINTS, ResultStatus.SUCCESS_NO_POINTS):
        has_points = status == ResultStatus.SUCCESS_POINTS
        logger.info(f"✅ [{user}] SUCCESS ({elapsed:.2f}s) — {result_text[:60]}")
        update_dashboard(
            domain=domain, account=user, code=code, status="THÀNH CÔNG",
            rtt_ms=elapsed * 1000, raw_response=result_text,
        )
        _safe_append_history(
            callbacks, event_type="RESULT", code=code, target_url=target_url,
            account=user, status="SUCCESS", submit_elapsed=elapsed, message=result_text[:100],
        )
        await loop.run_in_executor(
            None, db.record_submission, code, user, target_url, "SUCCESS", result_text[:100],
        )
        perf_mon.record_task("submit_code", elapsed, True)
        outcome = Outcome(
            status=status,
            result={"success": True, "has_points": has_points, "message": result_text[:100]},
        )

    elif status == ResultStatus.NO_RESULT:
        info = debug_info or await _build_no_result_debug_info(
            page, code, user, domain, clicked, pre_click_text, result_timeout_s, is_hi88,
        )
        screenshot = await take_screenshot_fn(
            page, user, code, target_url, "UNKNOWN", debug_info=info,
        )
        logger.warning(f"⚠️ [{user}] NO RESULT after {elapsed:.2f}s")
        update_dashboard(
            domain=domain, account=user, code=code, status="UNKNOWN",
            rtt_ms=elapsed * 1000, raw_response="No popup",
        )
        _safe_append_history(
            callbacks, event_type="RESULT", code=code, target_url=target_url,
            account=user, status="UNKNOWN", submit_elapsed=elapsed, message="No popup",
            screenshot=screenshot,
        )
        await loop.run_in_executor(
            None, db.record_submission, code, user, target_url, "UNKNOWN", "No popup",
        )
        perf_mon.record_task("submit_code", elapsed, False)
        outcome = Outcome(status=status, result={"success": False, "message": "No popup"})

    elif status == ResultStatus.FAILED:
        # Sai code/hết hạn là kết quả bình thường, không cần chụp screenshot
        # và page.content() trong lúc đang giữ tab lock. Chỉ bật lại khi cần
        # debug qua SCREENSHOT_ON_FAILED=true.
        try:
            from config import Config
            capture_failed = bool(getattr(Config, "SCREENSHOT_ON_FAILED", False))
        except Exception:
            capture_failed = False
        screenshot = (
            await take_screenshot_fn(page, user, code, target_url, "FAILED")
            if capture_failed else ""
        )
        logger.warning(f"❌ [{user}] FAILED ({elapsed:.2f}s) — {result_text[:60]}")
        update_dashboard(
            domain=domain, account=user, code=code, status="THẤT BẠI",
            rtt_ms=elapsed * 1000, raw_response=result_text,
        )
        _safe_append_history(
            callbacks, event_type="RESULT", code=code, target_url=target_url,
            account=user, status="FAILED", submit_elapsed=elapsed, message=result_text[:100],
            screenshot=screenshot,
        )
        await loop.run_in_executor(
            None, db.record_submission, code, user, target_url, "FAILED", result_text[:100],
        )
        perf_mon.record_task("submit_code", elapsed, False)
        outcome = Outcome(
            status=status,
            result={"success": False, "message": result_text[:100], "is_wrong_code": True},
        )

    else:
        # AMBIGUOUS
        screenshot = await take_screenshot_fn(page, user, code, target_url, "AMBIGUOUS")
        logger.warning(
            f"❓ [{user}] Kết quả MƠ HỒ ({elapsed:.2f}s), không rõ đúng/sai — "
            f"KHÔNG huỷ code, để retry: {result_text[:80]}"
        )
        update_dashboard(
            domain=domain, account=user, code=code, status="UNKNOWN",
            rtt_ms=elapsed * 1000, raw_response=result_text,
        )
        _safe_append_history(
            callbacks, event_type="RESULT", code=code, target_url=target_url,
            account=user, status="AMBIGUOUS", submit_elapsed=elapsed, message=result_text[:100],
            screenshot=screenshot,
        )
        await loop.run_in_executor(
            None, db.record_submission, code, user, target_url, "UNKNOWN", result_text[:100],
        )
        perf_mon.record_task("submit_code", elapsed, False)
        outcome = Outcome(
            status=ResultStatus.AMBIGUOUS,
            result={"success": False, "message": result_text[:100], "is_wrong_code": False},
        )

    # Dọn trang nhanh sau MỌI lần submit — bỏ qua an toàn nếu caller không
    # truyền callbacks['clean_page'].
    clean_page_fn = (callbacks or {}).get("clean_page")
    if clean_page_fn is not None:
        try:
            await clean_page_fn(page, domain, target_url, key)
        except Exception as e:
            logger.debug(f"⚠️ [{key}] clean_page callback lỗi (bỏ qua, không ảnh hưởng kết quả): {e}")

    return outcome
