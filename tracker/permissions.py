"""Who may do what. Pure functions so every rule is unit-tested.

Owner          global control: server access, recorded sales, scans, valuation display, eBay
Server admin   one server: its channels, roles, mention rules, Check now, test alerts, cleanup
Member         home, watches, stock, radar, about (+ Flips and Profit where valuations show)

The slash commands are only the two ways in; the screens behind them check roles per button
(tracker.bot.screens).
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import IntEnum

from tracker.guilds import GuildConfig


class Role(IntEnum):
    MEMBER = 1
    SERVER_ADMIN = 2
    OWNER = 3


# Minimum role per slash command (qualified name). Anything missing defaults to OWNER.
COMMAND_ROLES: dict[str, Role] = {
    "flipscout": Role.MEMBER,
    "stock": Role.MEMBER,
}
# Commands whose output contains eBay-derived numbers (none: the screens check per button).
VALUATION_COMMANDS: frozenset[str] = frozenset()


def resolve_role(
    user_id: str | int,
    *,
    owner_ids: Iterable[str],
    global_admin_ids: Iterable[str] = (),
    manage_guild: bool = False,
    role_ids: Iterable[str | int] = (),
    guild: GuildConfig | None = None,
) -> Role:
    user = str(user_id)
    if user in {str(o) for o in owner_ids}:
        return Role.OWNER
    if guild is not None and guild.approved:
        roles = {str(r) for r in role_ids}
        if (
            user in {str(a) for a in global_admin_ids}
            or manage_guild
            or (guild.admin_role_id is not None and str(guild.admin_role_id) in roles)
        ):
            return Role.SERVER_ADMIN
    return Role.MEMBER


def required_role(command_name: str) -> Role:
    return COMMAND_ROLES.get(command_name, Role.OWNER)


def check_access(command_name: str, role: Role, guild: GuildConfig | None) -> str | None:
    """None when allowed; otherwise a user-facing reason."""
    if guild is None or not guild.approved:
        return "This server isn't approved to use the tracker."
    needed = required_role(command_name)
    if role < needed:
        label = "the owner" if needed is Role.OWNER else "server admins"
        return f"Only {label} can use `/{command_name}`."
    if command_name in VALUATION_COMMANDS and not guild.show_valuations and role < Role.OWNER:
        return "Resale valuations aren't enabled in this server."
    return None
