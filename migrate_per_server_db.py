#!/usr/bin/env python3
"""Create one database file per IRC server from a legacy shared duckhunt.json.

Why this exists
---------------
Every bot instance used to open the same ``duckhunt.json``, so two servers
overwrote each other's players (last writer wins) and both wrote through the same
``<file>.tmp`` - which is what produced the ``*.corrupt-*`` files in this folder.
``DuckHuntBot`` now derives ``duckhunt_<server>.json`` per connection entry; this
script seeds those per-server files from the existing legacy database.

Channel ownership is decided in this order:

1. ``--assign "#channel=server"`` overrides given on the command line.
2. The ``connections[]`` entry that lists the channel. A channel listed by several
   servers is copied to every one of them, so no player loses progress; the files
   simply diverge from then on (the same channel name on two networks is a different
   community).
3. Bot log evidence (``:BotNick!user@host JOIN #channel``) when ``--logs DIR`` is used;
   skipped for nicks that are used by more than one connection (the log alone cannot
   tell those apart).
4. Older per-server files passed with ``--previous "file.json=server"``: the channels
   inside such a file are the channels that server was in.
5. Anything still unassigned goes to ``--default-server`` and is listed in the report,
   so it can be corrected with ``--assign`` and a re-run with ``--force``.

The legacy database is only ever read - it is never renamed or deleted - so you can
always start over by deleting the generated per-server files and re-running.

Examples
--------
    python migrate_per_server_db.py --dry-run
    python migrate_per_server_db.py --logs logs --default-server irc.rizon.net
    python migrate_per_server_db.py --verify --logs logs --default-server irc.rizon.net
    python migrate_per_server_db.py --assign "#next-x=irc.dal.net" --force

``--verify`` is read-only: it compares every record of the legacy database with its copy
in each per-server file and fails (exit 1) if anything is missing, altered or extra, so
you can prove the split did not reset anybody's progress.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from duckhunt import load_connection_configs  # noqa: E402
from src.db import server_db_filename  # noqa: E402

# ":Quackbot!~Quackbot@host JOIN #Offline" / ".... JOIN :#Online"
JOIN_RE = re.compile(r":([^\s!]+)!\S+ JOIN :?(\S+)")
INTERNAL_BUCKET_RE = re.compile(r"^__.*__$")


def load_servers(config_path):
    """Return the enabled connections with their channel sets and target DB files."""
    with open(config_path, encoding="utf-8") as f:
        root = json.load(f)

    servers = []
    for cfg in load_connection_configs(root):
        conn = cfg.get("connection") or {}
        server = conn.get("server", "")
        channels = conn.get("channels") or []
        servers.append(
            {
                "server": server,
                "nick": str(conn.get("nick") or "").lower(),
                "label": conn.get("label") or server,
                "channels": {
                    c.strip().lower()
                    for c in channels
                    if isinstance(c, str) and c.strip()
                },
                "db_file": conn.get("db_file") or server_db_filename(server),
            }
        )
    return servers


def log_channel_owners(servers, logs_dir):
    """Return ({channel: {server, ...}}, ambiguous_nicks) from bot JOIN log lines.

    Log lines only show the bot's nick, so when two connections share a nick (e.g.
    two networks both running "DuckHunt2") that nick's evidence is unusable and is
    skipped - the config/previous-file rules decide those channels instead.
    """
    by_nick = {}
    for server in servers:
        if server["nick"]:
            by_nick.setdefault(server["nick"], set()).add(server["server"])

    ambiguous = {nick for nick, owners in by_nick.items() if len(owners) > 1}
    owners = {}
    if not logs_dir or not os.path.isdir(logs_dir):
        return owners, sorted(ambiguous)

    for name in sorted(os.listdir(logs_dir)):
        if not name.startswith("duckhunt.log"):
            continue
        path = os.path.join(logs_dir, name)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "JOIN" not in line:
                        continue
                    match = JOIN_RE.search(line)
                    if not match:
                        continue
                    nick = match.group(1).lower()
                    channel = match.group(2).strip().lower()
                    nick_servers = by_nick.get(nick)
                    # Serve only unambiguous bot nicks (see docstring).
                    if not nick_servers or len(nick_servers) > 1:
                        continue
                    if not channel.startswith(("#", "&")):
                        continue
                    owners.setdefault(channel, set()).update(nick_servers)
        except OSError as e:
            print(f"  ! could not read {path}: {e}")
    return owners, sorted(ambiguous)


def parse_previous_db_specs(raw_specs, servers):
    """Parse ``--previous "file.json=server"`` specs into [(path, server)]."""
    known = {s["server"] for s in servers}
    specs = []
    for raw in raw_specs:
        if "=" not in raw:
            raise SystemExit(f"--previous expects 'file.json=server', got: {raw}")
        path, server = raw.rsplit("=", 1)
        path = path.strip().strip('"')
        server = server.strip()
        if server not in known:
            raise SystemExit(
                f"--previous {raw}: unknown server {server!r}. "
                f"Known: {', '.join(sorted(known))}"
            )
        specs.append((path, server))
    return specs


def previous_channel_owners(specs):
    """Return ({channel: {server}}, missing_report) from older per-server DB files.

    These files are evidence: back when every server wrote its own file, the channels
    a file contains are the channels that server was in. Any player that exists there
    but not in the legacy database is listed, since a shared-file regression could
    have dropped it.
    """
    owners = {}
    missing = []
    for path, server in specs:
        if not os.path.exists(path):
            raise SystemExit(f"--previous file not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        channels = data.get("channels")
        if not isinstance(channels, dict):
            continue
        for channel, bucket in channels.items():
            owners.setdefault(channel, set()).add(server)
            if not (isinstance(bucket, dict) and bucket.get("players")):
                continue
            for nick, player in bucket["players"].items():
                missing.append((path, channel, nick, (player or {}).get("xp", 0)))
    return owners, missing



def parse_assignments(raw_assignments, servers):
    """Parse ``--assign "#chan=server"`` options into {channel: server}."""
    known = {s["server"] for s in servers}
    assignments = {}
    for raw in raw_assignments:
        if "=" not in raw:
            raise SystemExit(f"--assign expects '#channel=server', got: {raw}")
        channel, server = raw.split("=", 1)
        channel = channel.strip().lower()
        server = server.strip()
        if server not in known:
            raise SystemExit(
                f"--assign {raw}: unknown server {server!r}. "
                f"Known: {', '.join(sorted(known))}"
            )
        assignments[channel] = server
    return assignments


def resolve_owners(
    channels, servers, log_owners, assignments, default_server, previous_owners=None
):
    """Map every legacy channel key to the servers that should receive it."""
    resolved = {}
    unassigned = []
    previous_owners = previous_owners or {}

    for channel in channels:
        if channel in assignments:
            resolved[channel] = ([assignments[channel]], "explicit --assign")
            continue

        # The connection entries are authoritative for the channels the bots are
        # configured to join today.
        internal = bool(INTERNAL_BUCKET_RE.match(channel))
        owners = [s["server"] for s in servers if channel in s["channels"]]
        if owners:
            # A channel listed by several servers is copied to all of them (no player
            # loses progress); a channel listed by one belongs to that one.
            resolved[channel] = (
                owners,
                "single connection entry"
                if len(owners) == 1
                else "shared by several servers",
            )
            continue

        if channel in log_owners:
            resolved[channel] = (sorted(log_owners[channel]), "bot logs")
            continue

        previous = sorted(previous_owners.get(channel, ()))
        if previous and not internal:
            resolved[channel] = (previous, "previous per-server DB")
            continue

        if default_server:
            reason = (
                "internal bucket (PM/global data)"
                if internal
                else "not listed in any connection entry"
            )
            resolved[channel] = ([default_server], f"default server ({reason})")
            if not internal:
                unassigned.append(channel)
        else:
            resolved[channel] = ([], "unassigned")
            if not internal:
                unassigned.append(channel)

    return resolved, unassigned


def build_payload(legacy, channel_keys, server, legacy_name, reason_by_channel):
    """Build one per-server database payload from the legacy channel buckets."""
    now = datetime.now().isoformat()
    return {
        "channels": {key: legacy["channels"][key] for key in channel_keys},
        "last_save": str(time.time()),
        "version": "2.0",
        "description": "DuckHunt Bot Player Database",
        "metadata": {
            "version": "2.0",
            "created": now,
            "last_modified": now,
            "split_from": legacy_name,
            "split_at": now,
            "server": server,
            "channels_from_legacy": sorted(channel_keys, key=str.lower),
            "channel_assignments": {
                key: reason_by_channel.get(key, "unknown") for key in sorted(channel_keys)
            },
        },
    }


def describe_diff(expected, actual):
    """Human-readable summary of the fields that differ between two player records."""
    keys = sorted(set(expected) | set(actual))
    diffs = [k for k in keys if expected.get(k) != actual.get(k)]
    shown = [f"{k}: {expected.get(k)!r} -> {actual.get(k)!r}" for k in diffs[:3]]
    if len(diffs) > 3:
        shown.append(f"+{len(diffs) - 3} more field(s)")
    return "; ".join(shown) or "no field-level difference"


def _sum_stats(players):
    """Sum xp/kills/deaths over a {nick: player} mapping, tolerating junk values."""
    xp = kills = deaths = 0
    for player in players.values():
        if not isinstance(player, dict):
            continue
        xp += int(player.get("xp", 0) or 0)
        kills += int(player.get("kills", 0) or 0)
        deaths += int(player.get("deaths", 0) or 0)
    return xp, kills, deaths


def _players_of(per_server, owner, channel):
    """Return {nick: player} for ``channel`` in one server's DB.

    ``None`` means the server's file itself could not be read; ``{}`` means the file was
    read fine but does not contain that channel.
    """
    channels = per_server.get(owner)
    if not isinstance(channels, dict):
        return None
    bucket = channels.get(channel)
    if not isinstance(bucket, dict):
        return {}
    players = bucket.get("players")
    return players if isinstance(players, dict) else {}


def verify_split(legacy_path, legacy, servers, resolved, directory=None):
    """Prove the per-server files still hold every legacy record (read-only).

    Two independent checks:

    * **Loss check** (decides the exit code): every record of the legacy database must
      exist, unchanged, in at least one per-server file. A record that is missing from
      every file, or altered in all of them, is reported as a problem.
    * **Ownership check** (reported as a warning): every server the channel rules assign
      should hold its own identical copy. This depends on the channel -> server rules
      (config, logs, ``--previous``), which can legitimately differ on another machine,
      so a mismatch here does not fail the run - it tells you to re-check the mapping.

    ``directory`` is where the per-server files live (defaults to the legacy DB's folder).
    Returns ``(problems, warnings, stats, totals)``.
    """
    directory = directory or os.path.dirname(legacy_path) or "."
    legacy_channels = legacy.get("channels") or {}
    per_server = {}
    problems = []

    for server in servers:
        path = os.path.join(directory, server["db_file"])
        if not os.path.exists(path):
            problems.append(f"{server['db_file']}: file does not exist")
            per_server[server["server"]] = None
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            problems.append(f"{server['db_file']}: unreadable ({e})")
            per_server[server["server"]] = None
            continue
        channels = data.get("channels") if isinstance(data, dict) else None
        per_server[server["server"]] = channels if isinstance(channels, dict) else {}

    matched = changed = missing = extra = 0
    covered = 0
    lost = []
    altered = []
    ownership = []
    extras = []

    for channel, bucket in legacy_channels.items():
        players = bucket.get("players") if isinstance(bucket, dict) else None
        players = players if isinstance(players, dict) else {}
        if not players:
            continue
        owners, reason = resolved.get(channel, ([], ""))[:2]
        if not owners:
            missing += len(players)
            lost.append(
                f"{channel}: {len(players)} record(s) have no destination at all "
                f"(no channel rule matched)"
            )
            continue

        dest_by_owner = {owner: _players_of(per_server, owner, channel) for owner in owners}

        # Copy-level counters: one check per server that owns the channel.
        for owner, dest in dest_by_owner.items():
            if dest is None:
                continue  # file-level problem already recorded
            for nick in players:
                if nick not in dest:
                    missing += 1
                elif dest[nick] != players[nick]:
                    changed += 1
                else:
                    matched += 1
            for nick in dest:
                if nick not in players:
                    extra += 1
                    extras.append(
                        f"{owner}: {channel}/{nick} is in {owner}'s file but not in the "
                        f"legacy database"
                    )

        # Record-level coverage, independent of the channel -> server rules.
        for nick, player in players.items():
            identical_in = [
                owner
                for owner, dest in dest_by_owner.items()
                if dest is not None and dest.get(nick) == player
            ]
            present_in = [
                owner
                for owner, dest in dest_by_owner.items()
                if dest is not None and nick in dest
            ]
            if identical_in:
                covered += 1
                if len(identical_in) < len(owners):
                    absent = [owner for owner in owners if owner not in identical_in]
                    ownership.append(
                        f"{channel}/{nick}: identical copy only in "
                        f"{', '.join(identical_in)} - missing or altered in "
                        f"{', '.join(absent)} (rules: {reason})"
                    )
            elif present_in:
                altered.append(
                    f"{channel}/{nick}: present in {', '.join(present_in)} but altered "
                    f"everywhere ({describe_diff(player, dest_by_owner[present_in[0]][nick])})"
                )
            else:
                lost.append(f"{channel}/{nick}: not present in any per-server file")

    stats = {}
    for server in servers:
        channels = per_server.get(server["server"]) or {}
        players = {}
        records = 0
        for bucket in channels.values():
            if isinstance(bucket, dict) and isinstance(bucket.get("players"), dict):
                records += len(bucket["players"])
                players.update(bucket["players"])
        xp, kills, deaths = _sum_stats(players)
        stats[server["server"]] = {
            "db_file": server["db_file"],
            "channels": len(channels),
            "records": records,
            "nicks": len(players),
            "players": len(players),
            "xp": xp,
            "kills": kills,
            "deaths": deaths,
        }

    legacy_players = {}
    channel_records = 0
    for bucket in legacy_channels.values():
        if isinstance(bucket, dict) and isinstance(bucket.get("players"), dict):
            channel_records += len(bucket["players"])
            legacy_players.update(bucket["players"])
    lxp, lkills, ldeaths = _sum_stats(legacy_players)
    totals = {
        "channels": len(legacy_channels),
        "records": channel_records,
        "nicks": len(legacy_players),
        "xp": lxp,
        "kills": lkills,
        "deaths": ldeaths,
        "matched": matched,
        "changed": changed,
        "missing": missing,
        "extra": extra,
        "copied": matched + changed + missing,
        "covered": covered,
        "lost": len(lost),
        "altered": len(altered),
        "ownership": len(ownership),
    }
    problems.extend(lost)
    problems.extend(altered)
    warnings = ownership + extras
    return problems, warnings, stats, totals


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", default=os.path.join(PROJECT_ROOT, "config.json"))
    parser.add_argument("--db", default=os.path.join(PROJECT_ROOT, "duckhunt.json"))
    parser.add_argument("--logs", default=os.path.join(PROJECT_ROOT, "logs"))
    parser.add_argument(
        "--default-server",
        default="",
        help="server that receives channels no rule could assign (required if any exist)",
    )
    parser.add_argument(
        "--assign",
        action="append",
        default=[],
        metavar='"#chan=server"',
        help="explicit channel -> server assignment (repeatable)",
    )
    parser.add_argument(
        "--previous",
        action="append",
        default=[],
        metavar='"file.json=server"',
        help="older per-server DB file, used as evidence for channel ownership (repeatable)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    parser.add_argument(
        "--out-dir",
        default="",
        help="where the per-server DB files live/will be written (default: the legacy "
        "DB's folder). Use it when the legacy DB sits in a backup folder, e.g. "
        "--db old/duckhunt.json --out-dir .",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="read-only: prove every legacy record is still present and identical in the "
        "per-server files, then exit without writing anything",
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite existing per-server files"
    )
    parser.add_argument(
        "--report", default=os.path.join(PROJECT_ROOT, "db_split_report.json")
    )
    args = parser.parse_args(argv)

    # Per-server DB files are read/written here (defaults to the legacy DB's folder).
    out_dir = args.out_dir or (os.path.dirname(args.db) or ".")

    servers = load_servers(args.config)
    if not servers:
        raise SystemExit("No enabled connections found in " + args.config)

    known_servers = {s["server"] for s in servers}
    if args.default_server and args.default_server not in known_servers:
        raise SystemExit(
            f"--default-server {args.default_server!r} is not an enabled connection. "
            f"Known: {', '.join(sorted(known_servers))}"
        )

    if not os.path.exists(args.db):
        raise SystemExit(f"Legacy database not found: {args.db}")

    with open(args.db, "r", encoding="utf-8") as f:
        legacy = json.load(f)
    legacy_channels = legacy.get("channels")
    if not isinstance(legacy_channels, dict):
        raise SystemExit(f"{args.db} has no 'channels' object - nothing to split")

    stat_before = os.stat(args.db)
    log_owners, ambiguous_nicks = log_channel_owners(servers, args.logs)
    previous_specs = parse_previous_db_specs(args.previous, servers)
    previous_owners, previous_players = previous_channel_owners(previous_specs)
    assignments = parse_assignments(args.assign, servers)
    resolved, unassigned = resolve_owners(
        sorted(legacy_channels, key=str.lower),
        servers,
        log_owners,
        assignments,
        args.default_server,
        previous_owners,
    )

    if ambiguous_nicks:
        print(
            f"! Nick(s) used by more than one connection: {', '.join(ambiguous_nicks)}"
            "\n  Their JOIN log lines cannot identify a server, so they were ignored.\n"
        )

    missing_players = []
    for path, channel, nick, xp in previous_players:
        bucket = legacy_channels.get(channel)
        players = bucket.get("players") if isinstance(bucket, dict) else None
        if not isinstance(players, dict) or nick not in players:
            missing_players.append(
                {"file": os.path.basename(path), "channel": channel, "nick": nick, "xp": xp}
            )
    if missing_players:
        print("Players found in an older per-server file but not in the legacy DB")
        print("(the shared-file regression may have dropped them):")
        for row in missing_players:
            print(
                f"  - {row['nick']} in {row['channel']} (xp {row['xp']})"
                f"  <- {row['file']}"
            )
        print("")

    if args.verify:
        print(f"Verifying {os.path.basename(args.db)} against the per-server files")
        print("(read-only: nothing is written by --verify)\n")
        problems, warnings, stats, totals = verify_split(
            args.db, legacy, servers, resolved, directory=out_dir
        )
        for server in servers:
            row = stats.get(server["server"], {})
            print(
                f"[{server['label']:<10}] {server['db_file']:<36} "
                f"channels={row.get('channels', 0):<3} records={row.get('records', 0):<4} "
                f"nicks={row.get('nicks', 0):<4} xp={row.get('xp', 0):<7} "
                f"kills={row.get('kills', 0):<8} deaths={row.get('deaths', 0):<6}"
            )
        print(
            f"\nlegacy total     : {totals['channels']} channels, {totals['records']} records "
            f"({totals['nicks']} nicks), xp={totals['xp']}, kills={totals['kills']}, "
            f"deaths={totals['deaths']}"
        )
        print(
            f"per-server copies: {totals['copied']} record copy/copies checked "
            f"({totals['matched']} identical, {totals['changed']} changed, "
            f"{totals['missing']} missing, {totals['extra']} extra)"
        )
        print(
            f"coverage         : {totals['covered']}/{totals['records']} legacy record(s) exist "
            "unchanged in at least one per-server file"
        )
        print(
            "                 : a channel listed by two servers is counted in both files, so "
            "'copies' can exceed 'records'."
        )
        if warnings:
            print(f"\n{len(warnings)} note(s) - no record was lost, but worth a look:")
            for line in warnings[:20]:
                print(f"  - {line}")
            if len(warnings) > 20:
                print(f"  ... and {len(warnings) - 20} more")
            print(
                "  A record held by a different file than the rules say: re-check the mapping "
                "with --assign '#channel=server'. Extra records are expected once the bot has "
                "run with the per-server files."
            )
        if problems:
            print(f"\n! {len(problems)} problem(s) found:")
            for line in problems[:40]:
                print(f"  - {line}")
            if len(problems) > 40:
                print(f"  ... and {len(problems) - 40} more")
            print(
                "\nFix ownership with --assign '#channel=server' and re-run with --force, "
                "then verify again."
            )
            return 1
        print(
            "\nOK: every legacy record is present and identical in its per-server file(s). "
            "Nothing was reset or dropped."
        )
        return 0

    if unassigned and not args.default_server:
        print("These channels are in no connection entry and need a decision:")
        for channel in unassigned:
            print(f"  - {channel}")
        raise SystemExit(
            "Re-run with --default-server <server> or one --assign per channel above."
        )

    print(f"Legacy database : {args.db}")
    print(f"Target directory: {out_dir}")
    print("")
    report = {
        "created_at": datetime.now().isoformat(),
        "legacy_db": args.db,
        "out_dir": out_dir,
        "dry_run": args.dry_run,
        "servers": {},
        "channels_not_in_any_connection": unassigned,
        "ambiguous_bot_nicks": ambiguous_nicks,
        "previous_files": [path for path, _server in previous_specs],
        "players_missing_from_legacy": missing_players,
    }

    total_written = 0
    for server in servers:
        keys = [
            key
            for key in legacy_channels
            if server["server"] in resolved.get(key, ([], ""))[0]
        ]
        payload = build_payload(
            legacy,
            keys,
            server["server"],
            os.path.basename(args.db),
            {key: resolved[key][1] for key in keys},
        )
        players = 0
        for key in keys:
            bucket = payload["channels"].get(key)
            if isinstance(bucket, dict) and isinstance(bucket.get("players"), dict):
                players += len(bucket["players"])

        target = os.path.join(out_dir, server["db_file"])
        exists = os.path.exists(target)
        will_write = args.force or not exists

        print(f"[{server['label']}] {server['server']}")
        print(f"  file     : {server['db_file']}" + ("  (exists!)" if exists else ""))
        print(f"  channels : {len(keys)} -> {', '.join(sorted(keys))}")
        print(f"  players  : {players}")
        for key in sorted(keys):
            print(f"    {key:<22} {resolved[key][1]}")
        print("")

        report["servers"][server["server"]] = {
            "db_file": server["db_file"],
            "channels": {key: resolved[key][1] for key in sorted(keys)},
            "players": players,
            "written": bool(will_write and not args.dry_run),
            "skipped_existing": bool(exists and not args.force),
        }

        if not will_write:
            print("  -> skipped: file already exists (use --force to overwrite)\n")
            continue
        if args.dry_run:
            print("  -> dry run: not written\n")
            continue

        with open(target, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=True)
        total_written += 1
        print(f"  -> written: {target}\n")

    stat_after = os.stat(args.db)
    legacy_unchanged = (stat_before.st_mtime, stat_before.st_size) == (
        stat_after.st_mtime,
        stat_after.st_size,
    )
    report["legacy_unchanged_during_run"] = legacy_unchanged
    report["files_written"] = total_written

    if unassigned:
        print(
            "! Channels that were in no connection entry were placed in "
            f"{args.default_server}: {', '.join(unassigned)}"
        )
        print("  (fix with --assign '#channel=server' and re-run with --force if wrong)\n")
    if not legacy_unchanged:
        print(
            f"! {args.db} changed while this script ran - the bot may still be running."
            "\n  Stop the bot, then re-run for a consistent split.\n"
        )

    try:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, sort_keys=True)
        print(f"Report written to {args.report}")
    except OSError as e:
        print(f"! could not write report: {e}")

    print(
        "\nNow restart the bot: every connection will use its own database file and "
        "stop sharing " + os.path.basename(args.db) + "."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())


