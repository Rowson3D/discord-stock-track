"""Update DMs: GitHub compare, one DM per new version, the request file, and the results."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from tracker.bot.updates import UpdateButton, notice_message
from tracker.settings import UpdateSettings
from tracker.updates import (
    REQUEST_FILE,
    RESULT_FILE,
    Notice,
    Result,
    UpdateError,
    Updater,
    parse_result,
)

OLD = "a" * 40
NEW = "b" * 40
MID = "c" * 40
NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)


def settings(version: str = OLD, repo: str = "someone/flipscout") -> UpdateSettings:
    return UpdateSettings(_env_file=None, repo=repo, FLIPSCOUT_VERSION=version)  # type: ignore[call-arg]


def github(status: str = "ahead", code: int = 200) -> tuple[httpx.MockTransport, list[str]]:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if code != 200:
            return httpx.Response(code, json={"message": "Not Found"})
        commits = [
            {
                "sha": MID,
                "html_url": "https://github.com/c",
                "commit": {"message": "First\n\nbody"},
            },
            {"sha": NEW, "html_url": "https://github.com/b", "commit": {"message": "Second"}},
        ]
        return httpx.Response(
            200,
            json={
                "status": status,
                "ahead_by": 2 if status == "ahead" else 0,
                "commits": commits if status == "ahead" else [],
                "html_url": "https://github.com/compare",
            },
        )

    return httpx.MockTransport(handler), seen


def updater(
    tmp_path: Path, transport: httpx.MockTransport | None = None, **kwargs: Any
) -> tuple[Updater, list[Notice]]:
    notices: list[Notice] = []

    async def notify(notice: Notice) -> None:
        notices.append(notice)

    return (
        Updater(settings(**kwargs), tmp_path, notify, transport=transport, clock=lambda: NOW),
        notices,
    )


async def test_new_commits_are_announced_once(tmp_path: Path) -> None:
    transport, seen = github()
    up, notices = updater(tmp_path, transport)
    await up.check()
    await up.check()
    assert seen[0] == f"https://api.github.com/repos/someone/flipscout/compare/{OLD}...main"
    assert len(notices) == 1
    available = notices[0].available
    assert available is not None
    assert available.head == NEW
    assert [c.title for c in available.commits] == ["First", "Second"]


async def test_nothing_newer_or_unknown_commit_stays_quiet(tmp_path: Path) -> None:
    for transport, _ in (github("identical"), github("behind"), github(code=404)):
        up, notices = updater(tmp_path, transport)
        await up.check()
        assert notices == []


async def test_no_version_or_no_repo_skips_github(tmp_path: Path) -> None:
    transport, seen = github()
    for kwargs in ({"version": ""}, {"version": "dev"}, {"repo": ""}):
        up, notices = updater(tmp_path, transport, **kwargs)
        await up.check()
        assert notices == []
    assert seen == []


def test_update_now_writes_a_request_once(tmp_path: Path) -> None:
    up, _ = updater(tmp_path)
    assert "Updating to `bbbbbbb`" in up.request(NEW)
    assert (tmp_path / REQUEST_FILE).read_text() == NEW + "\n"
    assert "already waiting" in up.request(MID)
    assert (tmp_path / REQUEST_FILE).read_text() == NEW + "\n"
    assert "Already running" in up.request(OLD)
    with pytest.raises(UpdateError):
        up.request("main; rm -rf /")


async def test_the_result_is_reported_after_the_restart(tmp_path: Path) -> None:
    (tmp_path / RESULT_FILE).write_text(
        "status=ok\nsha="
        + NEW
        + "\nfinished=2026-11-25T12:00:00Z\ndetail=bbbbbbb Second|ccccccc First\n"
    )
    up, notices = updater(tmp_path, version=NEW)
    await up.check_local()
    assert notices[0].result == Result(
        True, NEW, "2026-11-25T12:00:00Z", "bbbbbbb Second|ccccccc First"
    )
    assert notices[0].running == NEW
    assert not (tmp_path / RESULT_FILE).exists()
    embed, view = notice_message(notices[0])
    assert embed.title == "✅ FlipScout updated to bbbbbbb"
    assert embed.description == "• bbbbbbb Second\n• ccccccc First"
    assert view is None


async def test_a_rolled_back_update_offers_try_again(tmp_path: Path) -> None:
    (tmp_path / RESULT_FILE).write_text(
        f"status=failed\nsha={OLD}\ntarget={NEW}\ndetail=bbbbbbb didn't come up healthy\n"
    )
    up, notices = updater(tmp_path)
    await up.check_local()
    assert notices[0].result is not None
    assert notices[0].result.target == NEW
    embed, view = notice_message(notices[0])
    assert "Still running `aaaaaaa`" in (embed.description or "")
    assert view is not None
    retry = getattr(view.children[0], "item", view.children[0])
    assert (retry.label, retry.custom_id) == ("Try again", f"flipscout:update:{NEW}")
    # Nothing to retry when the commit asked for is the one running, or none was asked for.
    for target in (OLD, "", "main"):
        result = parse_result(f"status=failed\ntarget={target}\n")
        assert result is not None
        assert notice_message(Notice(result=result, running=OLD))[1] is None


async def test_the_github_check_remembers_what_it_found(tmp_path: Path) -> None:
    transport, _ = github()
    up, _ = updater(tmp_path, transport)
    await up.check()
    assert up.available is not None
    assert up.available.head == NEW
    up._transport = github("identical")[0]
    await up.check()
    assert up.available is None

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network", request=request)

    up._transport = httpx.MockTransport(offline)
    await up.check()  # logged, not raised


def test_a_failed_update_says_what_is_still_running() -> None:
    result = parse_result("status=failed\nsha=x\ndetail=error: build failed\n")
    assert result is not None
    assert not result.ok
    embed, _ = notice_message(Notice(result=result, running=OLD))
    assert embed.title == "⚠️ FlipScout update failed"
    assert "Still running `aaaaaaa`" in (embed.description or "")
    assert "error: build failed" in (embed.description or "")


async def test_a_request_nobody_picks_up_is_reported(tmp_path: Path) -> None:
    up, notices = updater(tmp_path)
    up.request(NEW)
    recent = (NOW - timedelta(minutes=1)).timestamp()
    os.utime(tmp_path / REQUEST_FILE, (recent, recent))
    await up.check_local()
    assert notices == []
    old = (NOW - timedelta(minutes=11)).timestamp()
    os.utime(tmp_path / REQUEST_FILE, (old, old))
    await up.check_local()
    assert notices[0].stale_request == NEW
    assert not (tmp_path / REQUEST_FILE).exists()
    embed, _ = notice_message(notices[0])
    assert "scripts/update.sh --install" in (embed.description or "")


async def test_the_dm_has_update_now_and_the_changes(tmp_path: Path) -> None:
    transport, _ = github()
    up, notices = updater(tmp_path, transport)
    await up.check()
    embed, view = notice_message(notices[0])
    assert embed.title == "FlipScout update: 2 new commits"
    assert (embed.description or "").splitlines()[0].endswith("Second")  # newest first
    assert view is not None
    buttons = [getattr(c, "item", c) for c in view.children]
    assert buttons[0].custom_id == f"flipscout:update:{NEW}"
    assert len(buttons[0].custom_id) <= 100
    assert buttons[1].url == "https://github.com/compare"


def interaction(user_id: int, bot: Any) -> Any:
    response = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
    return SimpleNamespace(user=SimpleNamespace(id=user_id), client=bot, response=response)


async def test_only_the_owner_can_press_update_now(tmp_path: Path) -> None:
    up, _ = updater(tmp_path)
    bot = SimpleNamespace(owner_ids={"1"}, updater=up, audit_owner=AsyncMock())
    stranger = interaction(2, bot)
    await UpdateButton(NEW).callback(stranger)
    stranger.response.send_message.assert_awaited_once()
    assert not (tmp_path / REQUEST_FILE).exists()

    owner = interaction(1, bot)
    await UpdateButton(NEW).callback(owner)
    assert (tmp_path / REQUEST_FILE).read_text() == NEW + "\n"
    bot.audit_owner.assert_awaited_once()
    kwargs = owner.response.edit_message.await_args.kwargs
    assert kwargs["view"] is None
    assert "Updating to `bbbbbbb`" in kwargs["content"]
