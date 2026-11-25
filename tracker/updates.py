"""Notice a newer FlipScout on GitHub and hand "Update now" to the host.

The bot never rebuilds itself (its container has no Docker access, by design). It knows the
commit it was built from (FLIPSCOUT_VERSION, baked in by scripts/update.sh) and asks GitHub
whether the branch is ahead of it. When it is, the owner gets a DM listing what changed, with an
"Update now" button that only writes ``<data>/update-request`` (a commit id).

On the host, ``scripts/update.sh --if-requested`` runs every minute from cron. It checks that
the commit is on GitHub's branch, fast-forwards to it, rebuilds and restarts the bot, and leaves
``<data>/update-result``. The restarted bot reads that and tells the owner how it went.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from tracker.settings import UpdateSettings

logger = logging.getLogger(__name__)

SHA_RE = re.compile(r"[0-9a-f]{40}")
REQUEST_FILE = "update-request"
RESULT_FILE = "update-result"
STATE_FILE = "update-state.json"
# A request nobody picked up after this long means the host updater isn't installed.
STALE_REQUEST = timedelta(minutes=10)


class UpdateError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class Available:
    """GitHub's branch is ahead of the running build."""

    head: str
    ahead_by: int
    commits: list[Commit]  # oldest first, at most GitHub's 250
    compare_url: str


@dataclass(frozen=True, slots=True)
class Result:
    """What scripts/update.sh reported."""

    ok: bool
    sha: str
    finished: str
    detail: str


@dataclass(slots=True)
class Notice:
    """Something to DM the owner: one of these is set."""

    available: Available | None = None
    result: Result | None = None
    stale_request: str | None = None
    running: str = ""  # the running commit, when reporting a result


def short(sha: str) -> str:
    return sha[:7]


async def compare(client: httpx.AsyncClient, repo: str, base: str, branch: str) -> Available | None:
    """The commits on ``branch`` after ``base``, or None when there's nothing newer."""
    response = await client.get(f"https://api.github.com/repos/{repo}/compare/{base}...{branch}")
    if response.status_code == 404:
        # The running commit isn't on GitHub (deployed before it was pushed) or the repo is wrong.
        raise UpdateError(f"GitHub doesn't know {short(base)} on {repo}")
    if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
        raise UpdateError("GitHub rate limit reached")
    response.raise_for_status()
    data = response.json()
    if data.get("status") != "ahead" or not data.get("commits"):
        return None
    commits = [
        Commit(
            sha=c["sha"],
            title=(c.get("commit", {}).get("message") or "").splitlines()[0][:200] or c["sha"],
            url=c.get("html_url", ""),
        )
        for c in data["commits"]
    ]
    return Available(
        head=commits[-1].sha,
        ahead_by=int(data.get("ahead_by") or len(commits)),
        commits=commits,
        compare_url=data.get("html_url", ""),
    )


def parse_result(text: str) -> Result | None:
    values = dict(
        line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#")
    )
    if "status" not in values:
        return None
    return Result(
        ok=values["status"].strip() == "ok",
        sha=values.get("sha", "").strip(),
        finished=values.get("finished", "").strip(),
        detail=values.get("detail", "").strip()[:900],
    )


class Updater:
    def __init__(
        self,
        settings: UpdateSettings,
        data_dir: Path,
        notify: Callable[[Notice], Awaitable[None]],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.data_dir = data_dir
        self._notify = notify
        self._transport = transport
        self._clock = clock

    @property
    def version(self) -> str:
        """The running commit, or "" when the image wasn't built by scripts/update.sh."""
        version = self.settings.version.strip().lower()
        return version if SHA_RE.fullmatch(version) else ""

    # ── state files ──────────────────────────────────────────────────────

    def _path(self, name: str) -> Path:
        return self.data_dir / name

    def _state(self) -> dict[str, str]:
        try:
            data = json.loads(self._path(STATE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self, state: dict[str, str]) -> None:
        path = self._path(STATE_FILE)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(path)

    def pending_request(self) -> str | None:
        try:
            return self._path(REQUEST_FILE).read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def request(self, sha: str) -> str:
        """Ask the host to update to ``sha``. Returns what to tell the owner."""
        if not SHA_RE.fullmatch(sha):
            raise UpdateError("That isn't a commit id.")
        if sha == self.version:
            return f"Already running `{short(sha)}`."
        queued = self.pending_request()
        if queued:
            return f"An update to `{short(queued)}` is already waiting to be picked up."
        path = self._path(REQUEST_FILE)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(sha + "\n", encoding="utf-8")
        tmp.replace(path)
        state = self._state()
        state["requested"] = sha
        state["requested_at"] = self._clock().isoformat()
        self._save_state(state)
        logger.info("Update to %s requested", short(sha))
        return (
            f"⏳ Updating to `{short(sha)}`. FlipScout restarts within a couple of minutes and "
            "messages you when it's back."
        )

    # ── the periodic check ───────────────────────────────────────────────

    async def check(self) -> None:
        await self._report_result()
        await self._report_stale_request()
        if not self.settings.configured:
            return
        if not self.version:
            logger.debug("Update check skipped: this build doesn't know its commit")
            return
        async with httpx.AsyncClient(
            timeout=20,
            transport=self._transport,
            headers={
                "User-Agent": "FlipScout-update-check",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        ) as client:
            try:
                available = await compare(
                    client, self.settings.repo.strip(), self.version, self.settings.branch
                )
            except UpdateError as exc:
                logger.info("Update check: %s", exc)
                return
        state = self._state()
        if available is None or state.get("notified") == available.head:
            return
        await self._notify(Notice(available=available))
        state["notified"] = available.head
        self._save_state(state)

    async def _report_result(self) -> None:
        path = self._path(RESULT_FILE)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return
        result = parse_result(text)
        if result is not None:
            await self._notify(Notice(result=result, running=self.version))
        path.unlink(missing_ok=True)

    async def _report_stale_request(self) -> None:
        sha = self.pending_request()
        if sha is None:
            return
        try:
            asked = datetime.fromtimestamp(self._path(REQUEST_FILE).stat().st_mtime, UTC)
        except OSError:
            return
        if self._clock() - asked < STALE_REQUEST:
            return
        self._path(REQUEST_FILE).unlink(missing_ok=True)
        await self._notify(Notice(stale_request=sha))


__all__ = [
    "Available",
    "Commit",
    "Notice",
    "Result",
    "UpdateError",
    "Updater",
    "compare",
    "parse_result",
    "short",
]
