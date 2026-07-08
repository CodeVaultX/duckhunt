# DuckHunt IRC Bot (Enhanced Fork)

An asyncio-based IRC bot that runs the classic **Duck Hunt** mini-game in IRC channels.

This repository is an enhanced fork maintained by [CodeVaultX](https://github.com/CodeVaultX). It keeps the core gameplay while adding multi-network support, improved authentication, a friendlier help system, and several reliability fixes for real-world IRC networks.

---

## What Changed in This Fork

Compared to [lord3nd3r/duckhunt](https://github.com/lord3nd3r/duckhunt), this version adds and changes the following:

### Multi-server support

- Connect to **multiple IRC networks at once** from a single process.
- Configure servers in a top-level `connections` array in `config.json`.
- Each server gets its **own database file**: `duckhunt_<server>.json` (e.g. `duckhunt_irc.rizon.net.json`).
- Per-server options: `server`, `port`, `nick`, `channels`, `ssl`, `sasl`, `nickserv`, reconnect settings, etc.
- **`enabled: false`** on a connection entry disables that network without deleting its config.
- Optional **`label`** field for readable log messages (e.g. `"Rizon"`, `"QuakeNet"`).
- Legacy single `connection` object is still supported for one-server setups.

### Command prefix: `!` **and** `.`

- The configured prefix (default `!` via `commands.prefix`) works as before.
- **Additionally**, every command also accepts a **dot prefix** (`.`), regardless of config.
  - Example: if `commands.prefix` is `!`, both `!bang` and `.bang` work.
  - Same for `!help` / `.help`, `!shop` / `.shop`, admin commands, etc.

### Help commands (new behaviour)

The upstream bot documents `!duckhelp` for a full command list via PM. This fork adds:

| Where | Command | What it does |
|---|---|---|
| Channel | `!help` or `.help` | Short public command summary + “PRIVMSG me `help`” |
| PM | `help` (no prefix) | Short command list |
| PM | `help full` (no prefix) | Detailed command list |
| Channel / PM | `!help full` / `.help full` | Same as `help full` |
| Channel / PM | `!duckhelp` / `.duckhelp` | Shortcut for full help (PM) |

Help replies are sent via **PRIVMSG**, not NOTICE.

### Nick / services authentication

Per-server authentication is supported inside each `connections` entry:

- **SASL** — standard PLAIN auth when the network supports it.
- **NickServ IDENTIFY** — for networks like Rizon, DALnet, etc.
  - On the registered nick: `IDENTIFY <password>`
  - On a guest nick / different account: `IDENTIFY <nick> <password>`
- **QuakeNet Q auth** — `command: "auth"` sends `AUTH <username> <password>` to `Q@CServe.quakenet.org`.

SASL and NickServ can be configured **per connection**, overriding global defaults.

### Other improvements

- **`duck_spawning.timeout`** — global duck fly-away timeout (seconds) is now respected; per-type overrides in `duck_types.*.timeout` still work.
- **Unicode channel names** — channels like `#Fenerbahçe` are handled correctly (no broken replies from ASCII-only sanitization).
- **Per-server enable/disable** — run only the networks you need without editing channel lists each time.
- **Smoke tests** — `tests/test_smoke.py` covers multi-server config, NickServ hoisting, and QuakeNet auth.
- **Windows helpers** (optional) — `start_hidden.vbs` / `stop_duckhunt.vbs` for background startup (use a Startup **shortcut**, not a copied VBS).

---

## Features (Gameplay)

- Multi-channel support per IRC server
- Per-channel player stats + global leaderboard
- Duck types: Normal, Golden, Fast, Ninja, Flock
- Shop, inventory, items, temporary effects
- Leveling, XP, daily bonus streaks
- Achievements
- JSON persistence with auto-save
- Admin tools (rearm, disarm, ignore, ducklaunch, join/part)
- Rate limiting, reconnect, auto-rejoin, lag watchdog

---

## Requirements

- **Python 3.9+** (stdlib only — no pip dependencies)
- An IRC network and channel(s) where the bot is allowed

---

## Quick Start

```bash
git clone https://github.com/CodeVaultX/duckhunt.git
cd duckhunt
```
```bash
cp config.json.example config.json

# Create config.json (see Configuration section below)
python duckhunt.py
```

Run tests:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

---

## Configuration

`config.json` is **gitignored** — never commit real passwords.

### Minimal single-server example (legacy format)

```json
{
  "connection": {
    "server": "irc.example.net",
    "port": 6697,
    "nick": "DuckHunt",
    "channels": ["#duckhunt"],
    "ssl": true,
    "ssl_verify": true
  },
  "commands": { "prefix": "!" },
  "admins": ["YourNick"],
  "duck_spawning": {
    "spawn_min": 600,
    "spawn_max": 1200,
    "timeout": 300
  }
}
```

### Multi-server example

```json
{
  "connections": [
    {
      "label": "Rizon",
      "enabled": true,
      "server": "irc.rizon.net",
      "port": 6697,
      "nick": "DuckHunt",
      "channels": ["#games", "#duckhunt"],
      "ssl": true,
      "ssl_verify": true,
      "nickserv": {
        "enabled": true,
        "service": "NickServ",
        "password": "YOUR_NICKSERV_PASSWORD",
        "identify_delay": 1.0
      }
    },
    {
      "label": "QuakeNet",
      "enabled": false,
      "server": "irc.quakenet.org",
      "port": 6667,
      "nick": "DuckHunt",
      "channels": ["#duckhunt"],
      "ssl": false,
      "nickserv": {
        "enabled": true,
        "service": "Q@CServe.quakenet.org",
        "command": "auth",
        "username": "DuckHunt",
        "password": "YOUR_Q_PASSWORD"
      }
    }
  ],
  "commands": { "prefix": "!" },
  "admins": [
    "YourNick",
    { "nick": "TrustedAdmin", "hostmask": "*!user@trusted.host" }
  ],
  "duck_spawning": {
    "spawn_min": 600,
    "spawn_max": 1200,
    "timeout": 300,
    "flock_chance": 0.08
  }
}
```

### Important config keys

| Key | Description |
|---|---|
| `connections[]` | List of IRC servers (preferred) |
| `connection` | Legacy single-server object |
| `enabled` | Per connection; `false` skips that server |
| `label` | Optional name shown in logs |
| `commands.prefix` | Primary trigger (default `!`). Dot `.` always works too |
| `admins` | Nick string and/or `{ "nick", "hostmask" }` objects |
| `sasl` | Global or per-connection SASL (`enabled`, `username`, `password`) |
| `nickserv` | Per-connection services auth (see above) |
| `duck_spawning.spawn_min` / `spawn_max` | Random spawn interval in **seconds** |
| `duck_spawning.timeout` | Seconds before an unshot duck flies away (global default) |
| `duck_types.*.timeout` | Per duck-type timeout override |
| `connection.ssl_verify` | Keep `true` in production; `false` only for self-signed certs |

**Admin security:** plain-string admins (`"colby"`) authenticate by nick only. Use hostmask objects for production.

---

## Command Reference

Throughout this section:

- **`!`** = your configured prefix (`commands.prefix`, default `!`)
- **`.`** = alternate prefix (always accepted)
- Examples use `!`; replace with `.` anytime (e.g. `.bang`, `.help`)

### Player commands (channel)

| Command | Description |
|---|---|
| `!bang` or `.bang` | Shoot at the active duck |
| `!bef` / `!befriend` or `.bef` / `.befriend` | Try to befriend the duck |
| `!reload` or `.reload` | Reload or unjam your weapon |
| `!daily` or `.daily` | Claim daily XP bonus (24h cooldown, streaks) |
| `!duckstats [player]` or `.duckstats [player]` | Hunting stats for this channel |
| `!topduck` or `.topduck` | Channel XP leaderboard |
| `!globaltop` or `.globaltop` | Top players across all channels on **this server** |
| `!shop` or `.shop` | Shop menu (sent via NOTICE; short ack in channel) |
| `!shop buy <id> [target]` or `.shop buy <id> [target]` | Buy an item (optionally for another player) |
| `!use <item_id> [target]` or `.use <item_id> [target]` | Use an inventory item |
| `!give <item_id> <player>` or `.give <item_id> <player>` | Give an inventory item to someone |
| `!inv` or `.inv` | Quick inventory view |
| `!effects` or `.effects` | Active buffs and timers |
| `!profile` or `.profile` | Detailed stat card (PM) |
| `!achievements` or `.achievements` | Earned badges (PM) |
| `!help` / `!duckhelp` or `.help` / `.duckhelp` | Help (see [Help commands](#help-commands-new-behaviour) above) |

### Help usage (summary)

```
# In channel:
!help
.help

# In private message to the bot:
help
help full

# Also works with prefix:
!help full
.duckhelp
```

### Admin commands

Admins must match `admins` in config (hostmask recommended).

| Command | Where | Description |
|---|---|---|
| `!rearm` | Channel | Rearm yourself |
| `!rearm <player>` | Channel | Rearm one player in current channel |
| `!rearm all` | Channel | Rearm all confiscated players in channel |
| `!rearm all` | PM | Rearm all players on this server |
| `!rearm <#channel> <player>` | PM | Rearm a specific player in a channel |
| `!disarm <player>` | Channel / PM | Confiscate a player's gun |
| `!ignore <player>` | Channel / PM | Ignore a player's commands |
| `!unignore <player>` | Channel / PM | Stop ignoring a player |
| `!ducklaunch [duck_type]` | Channel | Force-spawn duck in current channel |
| `!ducklaunch <#channel> [duck_type]` | PM | Force-spawn in another channel |
| `!join <#channel>` | Channel / PM | Bot joins a channel |
| `!part <#channel>` | Channel / PM | Bot leaves a channel |
| `!reload` | **PM only** | **Restart the bot process** (applies code/config changes) |

**Note:** In channels, `!reload` reloads your **gun**. In PM, `!reload` (admin only) **restarts the entire bot process** (all enabled servers in multi-server mode).

### Duck types for `!ducklaunch`

`normal`, `golden`, `fast`, `ninja`, `flock`

---

## Shop Items

| ID | Name | Typical cost | Effect |
|----|------|--------------|--------|
| 1 | Single Bullet | 5 XP | +1 bullet in current magazine |
| 2 | Magazine | 15 XP | +1 spare magazine |
| 4 | Gun Brush | 20 XP | −10% jam chance |
| 5 | Bread | 50 XP | 2× duck spawn rate for 20 minutes |
| 7 | Buy Gun Back | 40 XP | Recover confiscated gun |
| 13 | Scope | 60 XP | +20% accuracy for next 5 shots |
| 14 | Body Armor | 100 XP | Blocks next XP-loss event |

Use `!shop` for live prices and descriptions.

---

## Gameplay

1. A duck spawns randomly in the channel (interval set by `duck_spawning.spawn_min` / `spawn_max`).
2. Players type `!bang` to shoot or `!bef` to befriend.
3. Earn XP, level up, buy shop items, unlock achievements.
4. Unshot ducks fly away after `duck_spawning.timeout` seconds (unless overridden per duck type).

### Stats tracked

XP, level, streaks, ducks shot/befriended, accuracy, inventory, active effects, achievements, shop spending — **per channel**, per IRC server database.

---

## Persistence

| File | Purpose |
|---|---|
| `duckhunt_<server>.json` | Player data per IRC server (multi-server mode) |
| `duckhunt.json` | Used when only a legacy single `connection` is configured |
| `levels.json` | Level definitions |
| `shop.json` | Shop catalog |
| `messages.json` | Bot message templates |

Writes are atomic with retry logic. Relative DB paths resolve to the project root, not the shell CWD.

---

## Repository Layout

```
duckhunt/
├── duckhunt.py              # Entry point (spawns one bot per enabled connection)
├── config.json              # Your config (gitignored — create locally)
├── duckhunt_*.json          # Per-server databases (gitignored)
├── levels.json
├── shop.json
├── messages.json
├── start_hidden.vbs         # Optional Windows background launcher
├── stop_duckhunt.vbs        # Optional Windows stop script
├── tests/
│   └── test_smoke.py
└── src/
    ├── duckhuntbot.py       # IRC bot, commands, auth
    ├── game.py              # Duck spawning & combat
    ├── db.py                # JSON persistence
    ├── shop.py              # Shop & inventory
    ├── levels.py            # Leveling
    ├── sasl.py              # SASL
    ├── error_handling.py
    ├── logging_utils.py
    └── utils.py
```

---

## Multi-server Notes

- All enabled bots run concurrently via `asyncio.gather`.
- Same channel name on different networks has **separate** stats (different DB files).
- Disabling a server: set `"enabled": false` on that `connections` entry and restart.
- PM admin `!reload` restarts the **whole process** (every enabled server).
- Do not run the same nick on two machines against the same network — IRC will kick one connection.

---

## Differences from Upstream (Quick Reference)

| Feature | [lord3nd3r/duckhunt](https://github.com/lord3nd3r/duckhunt) | This fork |
|---|---|---|
| IRC networks | Single `connection` | `connections[]` multi-server |
| Command prefix | Configured prefix only | Configured prefix **+** `.` always |
| Help | `!duckhelp` → full list in PM | `!help` / `.help` in channel + PM `help` / `help full` |
| Nick/password auth | SASL (global) | SASL + per-server NickServ / QuakeNet Q |
| Per-server on/off | — | `enabled: true/false` |
| Database | `duckhunt.json` | `duckhunt_<server>.json` per network |
| Duck timeout config | Per-type only in practice | `duck_spawning.timeout` global fallback |
| Unicode channels | — | Full Unicode channel names supported |

---

## Security

- Keep `config.json` out of git (already in `.gitignore`).
- Prefer admin entries with `hostmask`, not nick-only strings.
- Keep `connection.ssl_verify: true` unless you fully trust the network path.
- Rotate passwords if they were ever shared in logs or chat.

---

## License

Follow the license of the [upstream repository](https://github.com/lord3nd3r/duckhunt). This fork is based on that project.

---

## Credits

- **Fork maintained by** [CodeVaultX](https://github.com/CodeVaultX)
- **Original repository** — [lord3nd3r/duckhunt](https://github.com/lord3nd3r/duckhunt)
- **Originally written by** Computertech
- **Maintained upstream by** End3r

---

**Happy Duck Hunting!**
