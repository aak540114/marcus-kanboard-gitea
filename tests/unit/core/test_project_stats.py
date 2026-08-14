"""
Unit tests for src/core/project_stats.py

ProjectStatsManager tracks how many tickets move into the "done" and
"waiting_for_human" Kanboard columns, per hour, per project — fed by the
ticket.status_changed event (see server.py's _track_project_stats).
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.core.project_stats import ProjectStatsManager


@pytest.fixture
def mgr(tmp_path):
    return ProjectStatsManager(data_dir=tmp_path)


def _dt(hour: int, minute: int = 0, day: int = 13) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=timezone.utc)


class TestRecordStatusChange:
    @pytest.mark.asyncio
    async def test_records_a_done_move(self, mgr):
        counted = await mgr.record_status_change(7, "42", "done", _dt(14))
        assert counted is True
        assert mgr.get_hourly_stats(7, "done") == [
            {"hour": "2026-08-13T14:00", "count": 1}
        ]

    @pytest.mark.asyncio
    async def test_records_a_waiting_for_human_move(self, mgr):
        counted = await mgr.record_status_change(7, "42", "waiting_for_human", _dt(15))
        assert counted is True
        assert mgr.get_hourly_stats(7, "waiting_for_human") == [
            {"hour": "2026-08-13T15:00", "count": 1}
        ]

    @pytest.mark.asyncio
    async def test_multiple_tickets_same_hour_increment_the_same_bucket(self, mgr):
        await mgr.record_status_change(7, "42", "done", _dt(14, 5))
        await mgr.record_status_change(7, "43", "done", _dt(14, 40))
        assert mgr.get_hourly_stats(7, "done") == [
            {"hour": "2026-08-13T14:00", "count": 2}
        ]

    @pytest.mark.asyncio
    async def test_different_hours_produce_separate_buckets(self, mgr):
        await mgr.record_status_change(7, "42", "done", _dt(14))
        await mgr.record_status_change(7, "43", "done", _dt(16))
        assert mgr.get_hourly_stats(7, "done") == [
            {"hour": "2026-08-13T14:00", "count": 1},
            {"hour": "2026-08-13T16:00", "count": 1},
        ]

    @pytest.mark.asyncio
    async def test_different_projects_tracked_independently(self, mgr):
        await mgr.record_status_change(7, "42", "done", _dt(14))
        await mgr.record_status_change(8, "99", "done", _dt(14))
        assert mgr.get_hourly_stats(7, "done") == [
            {"hour": "2026-08-13T14:00", "count": 1}
        ]
        assert mgr.get_hourly_stats(8, "done") == [
            {"hour": "2026-08-13T14:00", "count": 1}
        ]

    @pytest.mark.asyncio
    async def test_untracked_status_is_not_counted(self, mgr):
        counted = await mgr.record_status_change(7, "42", "in_progress", _dt(14))
        assert counted is False
        assert mgr.get_hourly_stats(7, "done") == []
        assert mgr.get_hourly_stats(7, "waiting_for_human") == []


class TestPollEchoDedup:
    """Every webhook-signalled move re-fires once on the next BoardWatcher
    poll — without dedup, every real move would be counted twice."""

    @pytest.mark.asyncio
    async def test_same_status_reported_twice_counts_once(self, mgr):
        first = await mgr.record_status_change(7, "42", "done", _dt(14))
        second = await mgr.record_status_change(7, "42", "done", _dt(14, 30))
        assert first is True
        assert second is False
        assert mgr.get_hourly_stats(7, "done") == [
            {"hour": "2026-08-13T14:00", "count": 1}
        ]

    @pytest.mark.asyncio
    async def test_real_reopen_and_redone_counts_twice(self, mgr):
        """A ticket that genuinely moves to done, then away, then back to
        done again must count twice — dedup only guards against repeated
        DELIVERY of the SAME transition, not real re-transitions."""
        await mgr.record_status_change(7, "42", "done", _dt(14))
        await mgr.record_status_change(7, "42", "in_progress", _dt(15))
        counted = await mgr.record_status_change(7, "42", "done", _dt(16))
        assert counted is True
        assert mgr.get_hourly_stats(7, "done") == [
            {"hour": "2026-08-13T14:00", "count": 1},
            {"hour": "2026-08-13T16:00", "count": 1},
        ]

    @pytest.mark.asyncio
    async def test_untracked_status_still_updates_dedup_state(self, mgr):
        """last_status must be updated even for untracked statuses, so a
        later real move to a tracked column is still detected as new."""
        await mgr.record_status_change(7, "42", "ready", _dt(13))
        await mgr.record_status_change(7, "42", "in_progress", _dt(13, 30))
        counted = await mgr.record_status_change(7, "42", "done", _dt(14))
        assert counted is True


class TestGetHourlyStats:
    def test_empty_when_never_recorded(self, mgr):
        assert mgr.get_hourly_stats(7, "done") == []

    @pytest.mark.asyncio
    async def test_skips_empty_hours(self, mgr):
        """No zero-padding between sparse buckets — only hours with an
        actual move appear."""
        await mgr.record_status_change(7, "42", "done", _dt(10))
        await mgr.record_status_change(7, "43", "done", _dt(18))
        result = mgr.get_hourly_stats(7, "done")
        assert result == [
            {"hour": "2026-08-13T10:00", "count": 1},
            {"hour": "2026-08-13T18:00", "count": 1},
        ]

    @pytest.mark.asyncio
    async def test_sorted_chronologically_regardless_of_insertion_order(self, mgr):
        await mgr.record_status_change(7, "42", "done", _dt(18))
        await mgr.record_status_change(7, "43", "done", _dt(10))
        result = mgr.get_hourly_stats(7, "done")
        assert [r["hour"] for r in result] == [
            "2026-08-13T10:00",
            "2026-08-13T18:00",
        ]


class TestGetLastHourCount:
    @pytest.mark.asyncio
    async def test_returns_count_for_the_current_hour_bucket(self, mgr):
        now = _dt(14, 45)
        await mgr.record_status_change(7, "42", "done", _dt(14, 10))
        result = mgr.get_last_hour_count(7, "done", now=now)
        assert result == 1

    def test_returns_zero_when_nothing_moved_this_hour(self, mgr):
        result = mgr.get_last_hour_count(7, "done", now=_dt(14))
        assert result == 0

    @pytest.mark.asyncio
    async def test_does_not_count_a_different_hour(self, mgr):
        await mgr.record_status_change(7, "42", "done", _dt(13, 55))
        result = mgr.get_last_hour_count(7, "done", now=_dt(14, 5))
        assert result == 0

    def test_unknown_project_returns_zero(self, mgr):
        assert mgr.get_last_hour_count(999, "done", now=_dt(14)) == 0


class TestPersistence:
    @pytest.mark.asyncio
    async def test_reloading_from_disk_preserves_counts(self, tmp_path):
        mgr1 = ProjectStatsManager(data_dir=tmp_path)
        await mgr1.record_status_change(7, "42", "done", _dt(14))

        mgr2 = ProjectStatsManager(data_dir=tmp_path)
        assert mgr2.get_hourly_stats(7, "done") == [
            {"hour": "2026-08-13T14:00", "count": 1}
        ]

    @pytest.mark.asyncio
    async def test_writes_valid_json_to_the_expected_path(self, tmp_path):
        m = ProjectStatsManager(data_dir=tmp_path)
        await m.record_status_change(7, "42", "done", _dt(14))

        raw = json.loads((tmp_path / "project_stats.json").read_text())
        assert raw["projects"]["7"]["done"]["2026-08-13T14:00"] == 1

    def test_missing_file_loads_empty(self, tmp_path):
        m = ProjectStatsManager(data_dir=tmp_path)
        assert m.get_hourly_stats(1, "done") == []

    def test_corrupt_file_loads_empty_instead_of_raising(self, tmp_path):
        (tmp_path / "project_stats.json").write_text("{not valid json")
        m = ProjectStatsManager(data_dir=tmp_path)
        assert m.get_hourly_stats(1, "done") == []

    @pytest.mark.asyncio
    async def test_no_leftover_tmp_file_after_successful_save(self, tmp_path):
        m = ProjectStatsManager(data_dir=tmp_path)
        await m.record_status_change(7, "42", "done", _dt(14))
        assert not Path(f"{m._path}.tmp").exists()

    @pytest.mark.asyncio
    async def test_tmp_file_cleaned_up_on_failed_save(self, tmp_path, monkeypatch):
        m = ProjectStatsManager(data_dir=tmp_path)

        import json as json_module

        def broken_dump(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(json_module, "dump", broken_dump)
        await m.record_status_change(7, "42", "done", _dt(14))

        assert not Path(f"{m._path}.tmp").exists()


class TestConcurrentAccessSafety:
    @pytest.mark.asyncio
    async def test_concurrent_calls_do_not_lose_updates(self, tmp_path):
        import asyncio

        m = ProjectStatsManager(data_dir=tmp_path)
        await asyncio.gather(
            *[
                m.record_status_change(7, str(i), "done", _dt(14))
                for i in range(20)
            ]
        )
        assert mgr_count(m, 7, "done") == 20


def mgr_count(mgr: ProjectStatsManager, project_id: int, status: str) -> int:
    stats = mgr.get_hourly_stats(project_id, status)
    return sum(s["count"] for s in stats)


class TestRefreshLocCount:
    """refresh_loc_count computes total lines of code on the repo's main
    branch via `git diff --shortstat <empty-tree> origin/main` — every
    tracked line in the repo shows as an "insertion" relative to the
    empty tree, and git already skips binary files from that count."""

    @pytest.mark.asyncio
    async def test_parses_insertions_from_shortstat_output(self, mgr):
        async def fake_run_git(args, cwd):
            if args[0] == "fetch":
                return (0, "", "")
            return (0, " 12 files changed, 340 insertions(+), 5 deletions(-)\n", "")

        with patch.object(mgr, "_run_git", side_effect=fake_run_git):
            result = await mgr.refresh_loc_count(7, "/repos/my-app")

        assert result == 340
        assert mgr.get_loc_count(7) == 340

    @pytest.mark.asyncio
    async def test_fetches_before_diffing(self, mgr):
        calls = []

        async def fake_run_git(args, cwd):
            calls.append(tuple(args))
            if args[0] == "fetch":
                return (0, "", "")
            return (0, "1 file changed, 3 insertions(+)", "")

        with patch.object(mgr, "_run_git", side_effect=fake_run_git):
            await mgr.refresh_loc_count(7, "/repos/my-app")

        assert calls[0] == ("fetch", "origin", "main")
        assert calls[1] == (
            "diff",
            "--shortstat",
            "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
            "origin/main",
        )

    @pytest.mark.asyncio
    async def test_uses_repo_path_as_cwd(self, mgr):
        seen_cwds = []

        async def fake_run_git(args, cwd):
            seen_cwds.append(cwd)
            return (0, "1 file changed, 3 insertions(+)", "")

        with patch.object(mgr, "_run_git", side_effect=fake_run_git):
            await mgr.refresh_loc_count(7, "/repos/my-app")

        assert seen_cwds == ["/repos/my-app", "/repos/my-app"]

    @pytest.mark.asyncio
    async def test_zero_insertions_when_shortstat_has_no_insertions_line(self, mgr):
        """An empty repo's shortstat output can be blank entirely."""
        async def fake_run_git(args, cwd):
            return (0, "", "")

        with patch.object(mgr, "_run_git", side_effect=fake_run_git):
            result = await mgr.refresh_loc_count(7, "/repos/my-app")

        assert result == 0

    @pytest.mark.asyncio
    async def test_returns_none_and_does_not_store_when_fetch_fails(self, mgr):
        async def fake_run_git(args, cwd):
            return (1, "", "fatal: unable to access repository")

        with patch.object(mgr, "_run_git", side_effect=fake_run_git):
            result = await mgr.refresh_loc_count(7, "/repos/my-app")

        assert result is None
        assert mgr.get_loc_count(7) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_diff_fails(self, mgr):
        async def fake_run_git(args, cwd):
            if args[0] == "fetch":
                return (0, "", "")
            return (1, "", "fatal: bad revision 'origin/main'")

        with patch.object(mgr, "_run_git", side_effect=fake_run_git):
            result = await mgr.refresh_loc_count(7, "/repos/my-app")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_unexpected_exception(self, mgr):
        with patch.object(mgr, "_run_git", side_effect=RuntimeError("boom")):
            result = await mgr.refresh_loc_count(7, "/repos/my-app")

        assert result is None

    @pytest.mark.asyncio
    async def test_persists_across_reload(self, tmp_path):
        mgr1 = ProjectStatsManager(data_dir=tmp_path)

        async def fake_run_git(args, cwd):
            if args[0] == "fetch":
                return (0, "", "")
            return (0, "1 file changed, 42 insertions(+)", "")

        with patch.object(mgr1, "_run_git", side_effect=fake_run_git):
            await mgr1.refresh_loc_count(7, "/repos/my-app")

        mgr2 = ProjectStatsManager(data_dir=tmp_path)
        assert mgr2.get_loc_count(7) == 42


class TestGetLocCount:
    def test_returns_none_when_never_computed(self, mgr):
        assert mgr.get_loc_count(7) is None
