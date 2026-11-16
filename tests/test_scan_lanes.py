"""Riviu Data storage: one scan lane per platform, so TikTok and Threads scan side by side."""

import asyncio

import pytest

from riviu import scan_journal, scan_service, scan_state
from riviu import server_context as context
from riviu.store import config as store_config


@pytest.fixture
def lanes(monkeypatch, tmp_path):
    monkeypatch.setattr(store_config, "ACTIVE", store_config.StoreConfig(backend="postgres", database_url="postgresql://riviu_scanner@fake/db"))
    monkeypatch.setattr(context, "EXCEL_DIR", str(tmp_path))
    return tmp_path


def test_each_platform_has_its_own_lane_for_start_skip_and_cancel(lanes):
    async def scenario():
        tiktok = scan_state.lane("tiktok")
        tiktok.task = asyncio.create_task(asyncio.sleep(10))
        threads_wait = asyncio.create_task(scan_service.wait_retry_cooldown(10, "threads"))
        await asyncio.sleep(0)
        assert scan_state.start_blocked("tiktok") and not scan_state.start_blocked("threads")
        assert scan_state.source_mutation_blocked()  # a cookie or source change still waits for every lane
        assert scan_service.skip_retry_wait("tiktok") is False  # TikTok is not in a cooldown
        assert scan_service.skip_retry_wait("threads") is True
        assert await threads_wait is False
        assert scan_service.user_cancel_scan("threads") is False
        assert scan_service.user_cancel_scan("tiktok") is True
        await asyncio.gather(tiktok.task, return_exceptions=True)
        assert tiktok.task.cancelled() and not scan_state.scan_running()

    asyncio.run(scenario())


def test_workbook_storage_keeps_one_lane_for_every_platform(monkeypatch):
    monkeypatch.setattr(store_config, "ACTIVE", store_config.WORKBOOK)

    async def scenario():
        monkeypatch.setattr(scan_state, "SCRAPE_TASK", asyncio.create_task(asyncio.sleep(10)))
        assert scan_state.lane("threads") is scan_state.lane("tiktok") is scan_state.SINGLE_LANE
        assert scan_state.start_blocked("threads")
        scan_state.SCRAPE_TASK.cancel()
        await asyncio.gather(scan_state.SCRAPE_TASK, return_exceptions=True)

    asyncio.run(scenario())


def test_interrupted_scans_of_both_lanes_resume_side_by_side(lanes, monkeypatch):
    for platform in ("tiktok", "threads"):
        options = scan_service.scan_options(platform=platform, file_id=scan_service.RIVIU_DATA_FILE_ID, sheet_name="2026-10")
        journal = scan_journal.RunJournal.begin(str(lanes), options, scan_service.journal_payload(options), lane=platform)
        asyncio.run(journal.save())
        assert (lanes / "data" / f"scan_journal.{platform}.json").exists()
    started = []

    async def prepare(payload):
        return None, scan_service.scan_options(platform=payload["platform"], file_id=payload["file_id"], sheet_name=payload["sheet_name"])

    async def runner(target_path, options, run_context=None):
        started.append((options["platform"], options["resume"]["lane"], scan_state.scan_running("tiktok"), scan_state.scan_running("threads")))
        await asyncio.sleep(0.02)
        await scan_state.lane(options["platform"]).manager.broadcast_status({"done": True, "phase": "completed"})

    monkeypatch.setattr(scan_service, "prepare_scan_start", prepare)
    monkeypatch.setattr(scan_service, "run_scraper_safely", runner)

    async def restart():
        tasks = await scan_service.resume_all_interrupted()
        await asyncio.gather(*tasks)

    asyncio.run(restart())
    assert sorted(item[:2] for item in started) == [("threads", "threads"), ("tiktok", "tiktok")]
    assert started[-1][2:] == (True, True)  # the second resume started while the first still ran


def _quick_runs(monkeypatch, started, seconds=0.05):
    """Fake prepare/run: each scan records its month, waits `seconds` and completes."""
    async def prepare(payload):
        return None, scan_service.scan_options(
            platform=payload.get("platform", "tiktok"), file_id=scan_service.RIVIU_DATA_FILE_ID, sheet_name=payload["sheet_name"],
            partners=payload.get("partners") or [])

    async def runner(_target, options, run_context=None):
        started.append((options["sheet_name"], tuple(options["partners"])))
        await asyncio.sleep(seconds)
        await scan_state.lane(options["platform"]).manager.broadcast_status({"done": True, "phase": "completed"})

    monkeypatch.setattr(scan_service, "prepare_scan_start", prepare)
    monkeypatch.setattr(scan_service, "run_scraper_safely", runner)


def test_quet_with_the_recent_scope_scans_the_newest_month_then_the_one_before_it(lanes, monkeypatch):
    # The admin page's "Quét 2 tháng gần nhất" (owner, 16/11/2026), here limited to one partner.
    import time

    from fastapi.testclient import TestClient
    from riviu import app as backend
    from riviu.api import riviu_data

    async def months(_platform):
        return [("2026-12", 1), ("2026-11", 9), ("2026-10", 30)]  # December created early

    monkeypatch.setattr(scan_service, "store_months", months)
    monkeypatch.setattr(riviu_data, "current_thang", lambda: "2026-11")
    started = []
    _quick_runs(monkeypatch, started)
    with TestClient(backend.app, base_url="http://127.0.0.1:1231") as client:
        assert client.get("/").status_code == 200
        with client.websocket_connect("ws://127.0.0.1:1231/ws", headers={"Origin": "http://127.0.0.1:1231"}) as ws:
            ws.send_json({"action": "start", "platform": "tiktok", "sheet_name": "", "months": ["latest", "previous"], "partners": ["Cafe A"]})
            deadline = time.monotonic() + 5
            while len(started) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
    assert started == [("2026-11", ("Cafe A",)), ("2026-10", ("Cafe A",))]


def test_a_cancel_ends_a_multi_month_scan(lanes, monkeypatch):
    started = []
    _quick_runs(monkeypatch, started)

    async def scenario():
        task, context = scan_service.start_scan_task(None, (await scan_service.prepare_scan_start({"sheet_name": "2026-11"}))[1])
        sequence = scan_service.continue_with_months(task, context, {"platform": "tiktok"}, ["2026-10"])
        await asyncio.sleep(0.01)
        scan_service.user_cancel_scan("tiktok")
        await sequence
        await asyncio.gather(scan_state.lane("tiktok").task, return_exceptions=True)

    asyncio.run(scenario())
    assert started == [("2026-11", ())]
