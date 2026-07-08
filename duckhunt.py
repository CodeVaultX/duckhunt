"""
DuckHunt IRC Bot - Simplified Entry Point
Commands: !bang, !reload, !shop, !rearm, !disarm

Supports one or more IRC servers via config.json:
- Preferred: top-level "connections" array
- Legacy: single top-level "connection" object
"""

import asyncio
import copy
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from src.duckhuntbot import DuckHuntBot

# Keys that may be overridden per connection entry (instead of globally).
_PER_CONNECTION_OVERRIDES = frozenset({"sasl", "nickserv"})
# Per-connection metadata — not passed through to the bot's connection.* config.
_CONNECTION_META_KEYS = frozenset({"enabled", "label"})


def connection_is_enabled(connection_entry) -> bool:
    """Return False only when ``enabled`` is explicitly false."""
    if not isinstance(connection_entry, dict):
        return False
    return connection_entry.get("enabled", True) is not False


def build_bot_config(root_config, connection_entry):
    """Merge shared root settings with one connection entry.

    Each bot still sees a normal config shape with a single ``connection`` object
    so existing ``get_config("connection.*")`` paths keep working. Optional
    per-server ``sasl`` (and similar) may live inside the connection entry.
    """
    if not isinstance(connection_entry, dict):
        raise ValueError("Each connection entry must be a JSON object")

    shared = {
        key: copy.deepcopy(value)
        for key, value in root_config.items()
        if key not in ("connection", "connections")
    }

    conn = copy.deepcopy(connection_entry)
    overrides = {}
    for key in list(conn.keys()):
        if key in _PER_CONNECTION_OVERRIDES:
            overrides[key] = conn.pop(key)
        elif key in _CONNECTION_META_KEYS or (
            isinstance(key, str) and key.startswith("_comment")
        ):
            conn.pop(key)

    bot_config = shared
    bot_config["connection"] = conn
    bot_config.update(overrides)
    return bot_config


def load_connection_configs(root_config):
    """Return a list of per-bot configs from ``connections`` or legacy ``connection``."""
    connections = root_config.get("connections")
    if isinstance(connections, list) and connections:
        active = [entry for entry in connections if connection_is_enabled(entry)]
        if not active:
            raise ValueError(
                "All entries in 'connections' are disabled. Set at least one "
                "'enabled': true (or remove enabled to default to on)."
            )
        return [build_bot_config(root_config, entry) for entry in active]

    legacy = root_config.get("connection")
    if isinstance(legacy, dict) and legacy:
        if not connection_is_enabled(legacy):
            raise ValueError("Legacy 'connection' is disabled (enabled: false)")
        return [build_bot_config(root_config, legacy)]

    raise ValueError(
        "config.json must define a non-empty 'connections' array "
        "or a legacy 'connection' object"
    )


def list_connection_status(root_config):
    """Return (enabled_entries, disabled_labels) for startup logging."""
    connections = root_config.get("connections")
    if not isinstance(connections, list):
        return [], []

    enabled, disabled = [], []
    for entry in connections:
        if not isinstance(entry, dict):
            continue
        label = entry.get("label") or entry.get("server", "unknown")
        if connection_is_enabled(entry):
            enabled.append(label)
        else:
            disabled.append(label)
    return enabled, disabled


async def run_bots(bots):
    """Run one or more bots concurrently until they all stop."""
    if len(bots) == 1:
        await bots[0].run()
        return

    try:
        results = await asyncio.gather(
            *(bot.run() for bot in bots), return_exceptions=True
        )
    except asyncio.CancelledError:
        # Ctrl+C cancels the gather; bots already handle their own cleanup.
        return

    errors = [
        r
        for r in results
        if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError)
    ]
    if errors:
        # Re-raise the first real failure after all bots have finished cleaning up.
        raise errors[0]


def main():
    """Main entry point for DuckHunt Bot"""
    try:
        config_file = "config.json"
        if not os.path.exists(config_file):
            print("❌ config.json not found!")
            sys.exit(1)

        with open(config_file, encoding="utf-8") as f:
            root_config = json.load(f)

        bot_configs = load_connection_configs(root_config)
        bots = [DuckHuntBot(cfg) for cfg in bot_configs]

        servers = [
            bot.get_config("connection.server", "unknown") for bot in bots
        ]
        _, disabled = list_connection_status(root_config)
        if disabled:
            bots[0].logger.info(
                "Skipped disabled server(s): %s", ", ".join(disabled)
            )
        bots[0].logger.info(
            "Starting DuckHunt Bot for %d server(s): %s",
            len(bots),
            ", ".join(servers),
        )

        asyncio.run(run_bots(bots))

    except KeyboardInterrupt:
        print("\n🛑 Shutdown interrupted by user")
    except FileNotFoundError:
        print("❌ config.json not found!")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
    else:
        print("👋 DuckHunt Bot stopped gracefully")


if __name__ == "__main__":
    main()
