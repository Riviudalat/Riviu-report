"""Partner reports read from Riviu Data (RIVIU_STORAGE=postgres only; 404 otherwise).

GET  /riviu-data/months?platform=tiktok
GET  /riviu-data/rows?platform=tiktok&thang=2026-10
GET  /riviu-data/report-partners?platform=tiktok&thang=2026-10&apply_min_views=true&min_views=100
GET  /riviu-data/report-posters?platform=tiktok&thang=2026-10&apply_min_views=true&min_views=100
POST /riviu-data/export-report {"platform", "thang", "partners": [...], "applyMinViews", "minViews"}
POST /riviu-data/export-report {"platform", "thang", "by": "poster", "posters": [...], ...}
GET  /riviu-data/scan-partners?platform=tiktok&months=latest,previous   (admin: "Quét riêng đối tác")

The three report routes also take a date range in place of `thang`: `tu_ngay` / `den_ngay`
(GET) or `tuNgay` / `denNgay` (POST), `YYYY-MM-DD`, both days included. Such a report holds the
posts whose NGÀY AIR falls in the range, taken from the published snapshots of the month sheets
around it (`range_report_rows`); posts without a NGÀY AIR are left out and counted (`undated`).

`months` lists the month sheets (newest first) and `rows` the posts of one month with their
metrics and scan status, for the web app's table. The report routes give the same rows,
partner list, filters and Excel files as /report-partners and /export-report on a workbook,
read from the soft-delete view v_<platform> for one `thang_nam` month, as its last finished scan
left it (`riviu.store.published`: links added since that scan started, and a running scan's
numbers, appear only when a scan of the month ends). `report-posters` and
`by: "poster"` do the same per Người đăng (nguoi_air; a blank one is "Chưa rõ người đăng"), one
report per poster with an extra ĐỐI TÁC column. All of them are reads the public viewer makes (`security.VIEWER_GET_PATHS` / `VIEWER_POST_PATHS`).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from riviu import internal_scan
from riviu.reports import build_export_payload_from_rows, build_poster_export_payload_from_rows, poster_link_counts
from riviu.source_files import content_disposition
from riviu.store import config as store_config
from riviu.store import published
from riviu.store.rules import ALL_REPORT_METRICS, REPORT_METRIC_COLUMNS, MonthError, current_thang, parse_thang
from riviu.workbook.constants import LAST_UPDATE_COLUMN
from riviu.workbook.partners import partner_dedup_key
from riviu.workbook.report_rows import SheetReportRows, partner_link_counts

router = APIRouter()


def _check(platform, thang):
    if not store_config.ACTIVE.postgres:
        return JSONResponse({"error": "Not Found"}, status_code=404)
    if platform not in internal_scan.PLATFORMS:
        return JSONResponse({"error": "Nền tảng không hợp lệ"}, status_code=400)
    try:
        parse_thang(thang)
    except MonthError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    return None


def _unavailable(error):
    return JSONResponse({"error": f"Không đọc được Riviu Data: {type(error).__name__}"}, status_code=502)


def month_label(thang):
    year, month = thang.split("-")
    return f"Tháng {int(month)}/{year}"


def default_month(months, today=None):
    """The newest month sheet that is not in the future (staff may create next month early); else the newest."""
    today = today or current_thang()
    return next((thang for thang in months if thang <= today), months[0] if months else "")


# ---- Date-range reports ----------------------------------------------------------------

MAX_RANGE_DAYS = 400


class RangeError(ValueError):
    pass


def parse_day(value, label):
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        raise RangeError(f"{label} phải có dạng YYYY-MM-DD") from None


def parse_range(start, end):
    """(first day, last day) of a report range, both included."""
    first, last = parse_day(start, "Từ ngày"), parse_day(end, "Đến ngày")
    if first > last:
        raise RangeError("Từ ngày phải trước hoặc bằng đến ngày")
    if (last - first).days >= MAX_RANGE_DAYS:
        raise RangeError(f"Khoảng ngày tối đa {MAX_RANGE_DAYS} ngày")
    return first, last


def _month_index(day):
    return day.year * 12 + day.month - 1


def range_months(months, first, last):
    """The month sheets a range report reads, oldest first: the range's months and one on each side,
    since staff sometimes file a post in the sheet of the month before or after its NGÀY AIR."""
    low, high = _month_index(first) - 1, _month_index(last) + 1
    chosen = []
    for thang in months:
        start, _ = parse_thang(thang)
        if low <= _month_index(start) <= high:
            chosen.append(thang)
    return sorted(chosen)


def range_label(first, last):
    """The report files' period tag, e.g. "01-11-2026_den_15-11-2026"."""
    return f"{first:%d-%m-%Y}_den_{last:%d-%m-%Y}"


def rows_in_range(parts, first, last):
    """(SheetReportRows of the posts aired in [first, last], posts without a NGÀY AIR) from month snapshots."""
    rows, undated = [], 0
    for part in parts:
        for row in part.rows:
            aired = row["NGÀY AIR"]
            if not isinstance(aired, datetime):
                undated += 1
                continue
            if first <= aired.date() <= last:
                rows.append(row)
    rows.sort(key=lambda row: (row["NGÀY AIR"], row.get("_id") or 0))
    names = {}
    for row in rows:
        for partner in row["partners"]:
            names.setdefault(partner_dedup_key(partner), partner)
    return SheetReportRows(rows=tuple(rows), partners=tuple(names.values())), undated


async def range_report_rows(platform, first, last):
    """The published rows of every month sheet around the range, cut to the posts aired in it."""
    store = internal_scan.make_store(platform)
    months = range_months([thang for thang, _ in await store.months()], first, last)
    parts = [await published.report_rows(store, thang) for thang in months]
    return rows_in_range(parts, first, last)


async def report_source(platform, thang, start, end):
    """(rows, period label, extra response fields) of a month report or, with `start`/`end`, a range
    report; (None, error response, None) when the request is invalid."""
    if start or end:
        denied = _check(platform, current_thang())
        if denied is not None:
            return None, denied, None
        try:
            first, last = parse_range(start, end)
        except RangeError as error:
            return None, JSONResponse({"error": str(error)}, status_code=400), None
        try:
            sheet_rows, undated = await range_report_rows(platform, first, last)
        except Exception as error:  # noqa: BLE001
            return None, _unavailable(error), None
        return sheet_rows, range_label(first, last), {"tuNgay": first.isoformat(), "denNgay": last.isoformat(), "undated": undated}
    denied = _check(platform, thang if isinstance(thang, str) else "")
    if denied is not None:
        return None, denied, None
    try:
        sheet_rows = await published.report_rows(internal_scan.make_store(platform), thang)
    except Exception as error:  # noqa: BLE001
        return None, _unavailable(error), None
    return sheet_rows, thang, {"thang": thang}


@router.get("/riviu-data/months")
async def riviu_data_months(platform: str = "tiktok"):
    denied = _check(platform, current_thang())
    if denied is not None:
        return denied
    try:
        months = await internal_scan.make_store(platform).months()
    except Exception as error:  # noqa: BLE001
        return _unavailable(error)
    return {
        "platform": platform,
        # A month's count follows its published snapshot once one is kept (store/published.py).
        "months": [
            {"thang": thang, "label": month_label(thang), "rows": published.kept_count(platform, thang) if published.kept_count(platform, thang) is not None else rows}
            for thang, rows in months
        ],
        "defaultThang": default_month([thang for thang, _ in months]),
    }


@router.get("/riviu-data/scan-partners")
async def riviu_data_scan_partners(platform: str = "tiktok", months: str = "latest,previous"):
    """Admin only (not a viewer path): the partners "Quét riêng đối tác" offers for the months a scan covers."""
    from riviu import scan_service

    denied = _check(platform, current_thang())
    if denied is not None:
        return denied
    tokens = [value.strip() for value in str(months or "").split(",") if value.strip()]
    try:
        resolved = await scan_service.resolve_store_months(platform, tokens)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    except Exception as error:  # noqa: BLE001
        return _unavailable(error)
    try:
        partners = await internal_scan.make_store(platform).scan_partner_counts(resolved)
    except Exception as error:  # noqa: BLE001
        return _unavailable(error)
    return {"platform": platform, "months": resolved, "partners": [{"name": name, "links": links} for name, links in partners]}


# Row keys of the table; metric keys are the platform's own (REPOST in place of LƯỢT LƯU on Threads).
METRIC_KEYS = {"LƯỢT XEM": "views", "TIM": "likes", "BÌNH LUẬN": "comments", "LƯỢT LƯU": "saves", "REPOST": "reposts", "CHIA SẺ": "shares"}


def table_row(row, platform):
    ngay_air = row["NGÀY AIR"]
    return {
        "id": row["_id"],
        "ngayAir": ngay_air.strftime("%d/%m/%Y") if ngay_air else "",
        "nguoiAir": row.get("_nguoiAir", ""),
        "channel": row["TÊN KÊNH"],
        "link": row["LINK AIR"],
        # TikTok's full post link a short vt./vm. link led to on its last successful scan, or "".
        "resolvedLink": row.get("_resolvedUrl") or "",
        "partners": list(row["partners"]),
        # None = unknown (never scanned or failed), 0 = a confirmed zero.
        **{METRIC_KEYS[metric]: row[metric] for metric in ALL_REPORT_METRICS if metric in REPORT_METRIC_COLUMNS[platform]},
        "updatedAt": row[LAST_UPDATE_COLUMN],
        "status": row["_scanStatus"],
    }


@router.get("/riviu-data/rows")
async def riviu_data_rows(platform: str = "tiktok", thang: str = ""):
    denied = _check(platform, thang)
    if denied is not None:
        return denied
    try:
        sheet_rows = await published.report_rows(internal_scan.make_store(platform), thang)
    except Exception as error:  # noqa: BLE001
        return _unavailable(error)
    return {
        "platform": platform, "thang": thang, "partners": list(sheet_rows.partners),
        "rows": [table_row(row, platform) for row in sheet_rows.rows],
    }


@router.get("/riviu-data/report-partners")
async def riviu_data_report_partners(platform: str = "tiktok", thang: str = "", apply_min_views: bool = True, min_views: int = 100,
                                     tu_ngay: str = "", den_ngay: str = ""):
    sheet_rows, period, extra = await report_source(platform, thang, tu_ngay, den_ngay)
    if sheet_rows is None:
        return period
    partners = partner_link_counts(
        sheet_rows.partners, sheet_rows.rows, apply_min_views=apply_min_views, min_views=max(int(min_views or 0), 0),
        platform=platform,
    )
    return {"partners": partners, "total": len(partners), "platform": platform, **extra, "rows": len(sheet_rows.rows)}


@router.get("/riviu-data/report-posters")
async def riviu_data_report_posters(platform: str = "tiktok", thang: str = "", apply_min_views: bool = True, min_views: int = 100,
                                    tu_ngay: str = "", den_ngay: str = ""):
    sheet_rows, period, extra = await report_source(platform, thang, tu_ngay, den_ngay)
    if sheet_rows is None:
        return period
    posters = poster_link_counts(sheet_rows.rows, apply_min_views=apply_min_views, min_views=max(int(min_views or 0), 0))
    return {"posters": posters, "total": len(posters), "platform": platform, **extra, "rows": len(sheet_rows.rows)}


@router.post("/riviu-data/export-report")
async def riviu_data_export_report(data: dict):
    platform = data.get("platform", "tiktok")
    by_poster = data.get("by") == "poster"
    selected = data.get("posters" if by_poster else "partners") or []
    try:
        min_views = max(int(data.get("minViews", 100)), 0)
    except (TypeError, ValueError):
        min_views = 100
    sheet_rows, period, _extra = await report_source(platform, data.get("thang", ""), data.get("tuNgay"), data.get("denNgay"))
    if sheet_rows is None:
        return period
    if not isinstance(selected, list) or not selected:
        return JSONResponse({"error": f"Vui lòng chọn ít nhất một {'người đăng' if by_poster else 'đối tác'}"}, status_code=400)
    build = build_poster_export_payload_from_rows if by_poster else build_export_payload_from_rows
    try:
        payload = await asyncio.to_thread(
            build, sheet_rows, selected, bool(data.get("applyMinViews", True)), min_views, period, platform,
        )
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=404 if "Không tìm thấy" in str(error) else 400)
    return Response(
        content=payload["content"], media_type=payload["media_type"],
        headers={"Content-Disposition": content_disposition(payload["filename"])},
    )
