"""Smoke tests for DuckHunt Bot core modules.

Run with:  python3 -m unittest discover tests

IMPORTANT: These tests must never touch the real duckhunt.json (or any
duckhunt_<server>.json). Every DuckDB instance created here is given an
absolute path inside a TemporaryDirectory.
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from duckhunt import load_connection_configs
from src.db import DuckDB, server_db_filename
from src.duckhuntbot import DuckHuntBot
from src.error_handling import sanitize_user_input
from src.game import ACHIEVEMENTS, DuckGame
from src.levels import LevelManager
from src.shop import ShopManager
from src.utils import MessageManager, parse_irc_message

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fake_sasl(authenticated=False):
    """Minimal SASLHandler stand-in (tests only need the .authenticated flag)."""
    return type("FakeSASL", (), {"authenticated": authenticated})()


class TestMultiServerConfig(unittest.TestCase):
    def test_connections_array_builds_isolated_configs(self):
        root = {
            "connections": [
                {
                    "server": "irc.a.example",
                    "nick": "BotA",
                    "channels": ["#general"],
                    "sasl": {"enabled": True, "password": "secret-a"},
                },
                {
                    "server": "irc.b.example",
                    "nick": "BotB",
                    "channels": ["#general", "#games"],
                },
            ],
            "sasl": {"enabled": False, "password": ""},
            "admins": ["colby"],
            "commands": {"prefix": "!"},
        }
        configs = load_connection_configs(root)
        self.assertEqual(len(configs), 2)
        self.assertEqual(configs[0]["connection"]["server"], "irc.a.example")
        self.assertEqual(configs[0]["connection"]["channels"], ["#general"])
        self.assertEqual(configs[0]["sasl"]["password"], "secret-a")
        self.assertNotIn("sasl", configs[0]["connection"])
        self.assertEqual(configs[1]["connection"]["server"], "irc.b.example")
        self.assertEqual(configs[1]["sasl"]["enabled"], False)
        self.assertEqual(configs[1]["admins"], ["colby"])
        # Shared settings must not be aliased across bots
        configs[0]["admins"].append("other")
        self.assertEqual(configs[1]["admins"], ["colby"])

    def test_nickserv_override_is_hoisted(self):
        root = {
            "connections": [
                {
                    "server": "irc.dal.example",
                    "nick": "Bot",
                    "nickserv": {"enabled": True, "password": "pw"},
                }
            ],
            "nickserv": {"enabled": False},
        }
        configs = load_connection_configs(root)
        self.assertEqual(configs[0]["nickserv"]["enabled"], True)
        self.assertEqual(configs[0]["nickserv"]["password"], "pw")
        self.assertNotIn("nickserv", configs[0]["connection"])

    def test_quakenet_auth_command(self):
        bot = object.__new__(DuckHuntBot)
        bot.config = {
            "connection": {"nick": "DuckHunt2026"},
            "nickserv": {
                "enabled": True,
                "service": "Q@CServe.quakenet.org",
                "command": "auth",
                "username": "DuckHunt2026",
                "password": "secret",
                "identify_delay": 0,
            },
        }
        bot.current_nick = "DuckHunt2026"
        bot.logger = logging.getLogger("test")
        bot.sasl_handler = _fake_sasl(False)
        bot.writer = None
        sent = []
        bot.send_raw = lambda msg: sent.append(msg) or True
        self.assertTrue(asyncio.run(bot._nickserv_identify()))
        self.assertEqual(
            sent[0],
            "PRIVMSG Q@CServe.quakenet.org :auth DuckHunt2026 secret",
        )

    def test_nickserv_identify_skipped_when_sasl_authenticated(self):
        bot = object.__new__(DuckHuntBot)
        bot.config = {
            "connection": {"nick": "Bot"},
            "nickserv": {
                "enabled": True,
                "service": "NickServ",
                "password": "pw",
                "identify_delay": 0,
            },
        }
        bot.current_nick = "Bot"
        bot.logger = logging.getLogger("test")
        bot.sasl_handler = _fake_sasl(True)
        bot.writer = None
        sent = []
        bot.send_raw = lambda msg: sent.append(msg) or True
        self.assertFalse(asyncio.run(bot._nickserv_identify()))
        self.assertEqual(sent, [], "NickServ password was sent despite SASL login")

    def test_legacy_single_connection(self):
        root = {
            "connection": {"server": "irc.legacy.example", "nick": "Legacy"},
            "admins": ["colby"],
        }
        configs = load_connection_configs(root)
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["connection"]["server"], "irc.legacy.example")

    def test_missing_connections_raises(self):
        with self.assertRaises(ValueError):
            load_connection_configs({})


class TestParseIrcMessage(unittest.TestCase):
    def test_privmsg(self):
        prefix, command, params, trailing = parse_irc_message(
            ":nick!user@host PRIVMSG #chan :!bang extra"
        )
        self.assertEqual(prefix, "nick!user@host")
        self.assertEqual(command, "PRIVMSG")
        self.assertEqual(params, ["#chan"])
        self.assertEqual(trailing, "!bang extra")

    def test_ping(self):
        prefix, command, params, trailing = parse_irc_message("PING :server.example")
        self.assertEqual(command, "PING")
        self.assertEqual(trailing, "server.example")

    def test_garbage_does_not_raise(self):
        parse_irc_message("")
        parse_irc_message(":::::")
        parse_irc_message(":only-prefix")


class TestSanitize(unittest.TestCase):
    def test_strips_newlines(self):
        self.assertNotIn("\n", sanitize_user_input("a\r\nb"))

    def test_allowed_chars(self):
        out = sanitize_user_input("ni<k$", allowed_chars="abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(out, "nik")


class TestMessages(unittest.TestCase):
    def test_repo_messages_json_has_required_keys(self):
        with open(os.path.join(PROJECT_ROOT, "messages.json")) as f:
            msgs = json.load(f)
        for key in (
            "duck_drop_normal",
            "duck_drop_fast",
            "duck_drop_golden",
            "duck_drop_ninja",
            "hunting_dog_retrieves",
            "duck_flock",
            "duck_flock_flies_away",
            "rate_limited",
            "bang_cooldown",
        ):
            self.assertIn(key, msgs, f"messages.json missing {key}")

    def test_missing_key_fallback(self):
        mm = MessageManager("/nonexistent/messages.json")
        self.assertTrue(mm.get("no_such_key").startswith("[Missing"))

    def test_prefix_substitution(self):
        mm = MessageManager("/nonexistent/messages.json", command_prefix="@")
        self.assertIn("@reload", mm.get("bang_no_ammo", nick="x"))


class TestLevels(unittest.TestCase):
    def test_no_free_ammo_on_level_recalc(self):
        lm = LevelManager("/nonexistent/levels.json")
        player = {"xp": 0, "magazines": 1, "current_ammo": 2, "bullets_per_magazine": 6}
        lm.update_player_magazines(player)
        total = player["current_ammo"] + max(0, player["magazines"] - 1) * player[
            "bullets_per_magazine"
        ]
        self.assertLessEqual(total, 2, "level recalc granted free ammo")

    def test_full_reload(self):
        lm = LevelManager("/nonexistent/levels.json")
        player = {"xp": 0, "magazines": 0, "current_ammo": 0, "bullets_per_magazine": 6}
        lm.update_player_magazines(player, full_reload=True)
        self.assertGreater(player["current_ammo"], 0)


class TestShop(unittest.TestCase):
    def setUp(self):
        self.shop = ShopManager("/nonexistent/shop.json", LevelManager("/nonexistent/levels.json"))

    def test_purchase_insufficient_xp(self):
        player = {"xp": 0, "inventory": {}}
        result = self.shop.purchase_item(player, 1, store_in_inventory=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "insufficient_xp")

    def test_purchase_into_inventory(self):
        player = {"xp": 100, "inventory": {}}
        result = self.shop.purchase_item(player, 1, store_in_inventory=True)
        self.assertTrue(result["success"])
        self.assertEqual(player["xp"], 95)
        self.assertEqual(player["inventory"]["1"], 1)
        self.assertEqual(player["total_xp_spent"], 5)

    def test_use_trap_without_target_rejected(self):
        # trap must never apply to the user themselves via !use with no target
        self.shop.items[99] = {"name": "Trap", "price": 1, "type": "trap"}
        player = {"xp": 0, "inventory": {"99": 1}}
        result = self.shop.use_inventory_item(player, 99, target_player=None)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "target_required")
        self.assertEqual(player["inventory"]["99"], 1, "item was consumed on failure")

    def test_steal_ammo_credits_buyer(self):
        self.shop.items[98] = {"name": "Sticky Fingers", "price": 1, "type": "steal_ammo", "amount": 2}
        buyer = {"xp": 0, "inventory": {"98": 1}, "current_ammo": 0, "bullets_per_magazine": 6}
        target = {"current_ammo": 5, "bullets_per_magazine": 6}
        result = self.shop.use_inventory_item(buyer, 98, target_player=target)
        self.assertTrue(result["success"])
        self.assertEqual(target["current_ammo"], 3)
        self.assertEqual(buyer["current_ammo"], 2, "stolen ammo was not credited to the user")


class TestDatabasePersistence(unittest.TestCase):
    """Round-trip test proving player data survives save + reload intact."""

    def test_save_and_reload_preserves_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test-duckhunt.json")

            db = DuckDB(db_file=db_path)
            player = db.get_player("hunter", "#ducks")
            player["xp"] = 1234
            player["ducks_shot"] = 56
            player["luck_bonus"] = 7
            player["critical_chance"] = 3
            player["inventory"] = {"1": 2}
            self.assertTrue(db.save_database())
            db.flush_pending_saves(timeout=10.0)

            db2 = DuckDB(db_file=db_path)
            reloaded = db2.get_player_if_exists("hunter", "#ducks")
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded["xp"], 1234)
            self.assertEqual(reloaded["ducks_shot"], 56)
            self.assertEqual(reloaded["inventory"], {"1": 2})
            # Shop effect fields must survive the sanitize pass (regression test)
            self.assertEqual(reloaded["luck_bonus"], 7)
            self.assertEqual(reloaded["critical_chance"], 3)
            # Loading can trigger the one-time accuracy migration, which saves in the
            # background - wait for it, or the temp file is still open when the
            # TemporaryDirectory is removed (WinError 32 on Windows).
            db2.flush_pending_saves(timeout=10.0)

    def test_sanitize_does_not_wipe_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DuckDB(db_file=os.path.join(tmp, "t.json"))
            sanitized = db._sanitize_player_data({"xp": "999", "ducks_shot": 12})
            self.assertEqual(sanitized["xp"], 999)
            self.assertEqual(sanitized["ducks_shot"], 12)


class TestDatabaseMetadataAndIsolation(unittest.TestCase):
    """Regression tests for the metadata and per-server DB bugs."""

    def test_one_time_accuracy_migrations_are_persisted(self):
        # BUG: metadata was never written, so the accuracy soft-cap/floor re-ran on
        # every single load and kept overwriting the accuracy players had earned.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "meta.json")

            db = DuckDB(db_file=db_path)
            player = db.get_player("hunter", "#ducks")
            player["accuracy"] = 40  # below the one-time floor
            db.save_database()
            db.flush_pending_saves(timeout=10.0)

            db2 = DuckDB(db_file=db_path)  # applies the floor once
            self.assertEqual(
                db2.get_player_if_exists("hunter", "#ducks")["accuracy"], 65
            )
            db2.flush_pending_saves(timeout=10.0)

            with open(db_path, encoding="utf-8") as f:
                on_disk = json.load(f)
            self.assertIn("metadata", on_disk)
            self.assertTrue(on_disk["metadata"]["accuracy_floor_applied"])
            self.assertTrue(on_disk["metadata"]["accuracy_soft_cap_applied"])

            # ...and must never run again for the same file.
            db3 = DuckDB(db_file=db_path)
            player3 = db3.get_player_if_exists("hunter", "#ducks")
            player3["accuracy"] = 40
            db3.save_database()
            db3.flush_pending_saves(timeout=10.0)

            db4 = DuckDB(db_file=db_path)
            self.assertEqual(
                db4.get_player_if_exists("hunter", "#ducks")["accuracy"],
                40,
                "one-time accuracy floor re-applied on a later load",
            )
            db4.flush_pending_saves(timeout=10.0)

    def test_filename_derived_from_server(self):
        self.assertEqual(
            server_db_filename("irc.rizon.net"), "duckhunt_irc.rizon.net.json"
        )
        self.assertEqual(
            server_db_filename("IRC.IPv6.QuakeNet.ORG"),
            "duckhunt_irc.ipv6.quakenet.org.json",
        )
        self.assertEqual(server_db_filename("irc.dal.net"), "duckhunt_irc.dal.net.json")

    def test_filename_fallback_without_server(self):
        self.assertEqual(server_db_filename(""), "duckhunt.json")
        self.assertEqual(server_db_filename(None), "duckhunt.json")

    def test_filename_unsafe_characters_are_replaced(self):
        self.assertEqual(
            server_db_filename("irc.example.net:6697"),
            "duckhunt_irc.example.net_6697.json",
        )
        self.assertNotIn("/", server_db_filename("weird/host name"))

    def test_bot_resolves_its_own_db_file(self):
        bot = object.__new__(DuckHuntBot)
        bot.logger = logging.getLogger("test")

        bot.config = {"connection": {"server": "irc.rizon.net"}}
        self.assertEqual(bot._resolve_db_file(), "duckhunt_irc.rizon.net.json")

        # Explicit override wins (e.g. a custom layout).
        bot.config = {
            "connection": {"server": "irc.rizon.net", "db_file": "custom.db.json"}
        }
        self.assertEqual(bot._resolve_db_file(), "custom.db.json")

        # No server configured -> legacy shared file (with a warning).
        bot.config = {"connection": {}}
        self.assertEqual(bot._resolve_db_file(), "duckhunt.json")

    def test_two_servers_keep_separate_players(self):
        with tempfile.TemporaryDirectory() as tmp:
            rizon_file = os.path.join(tmp, server_db_filename("irc.rizon.net"))
            dalnet_file = os.path.join(tmp, server_db_filename("irc.dal.net"))
            self.assertNotEqual(rizon_file, dalnet_file)

            db_a = DuckDB(db_file=rizon_file)
            db_a.get_player("rizer", "#online")["xp"] = 100
            db_a.save_database()
            db_a.flush_pending_saves(timeout=10.0)

            db_b = DuckDB(db_file=dalnet_file)
            db_b.get_player("daler", "#online")["xp"] = 5
            db_b.save_database()
            db_b.flush_pending_saves(timeout=10.0)

            # Same channel name, different server files: no shared players at all.
            self.assertIsNotNone(db_a.get_player_if_exists("rizer", "#online"))
            self.assertIsNone(db_a.get_player_if_exists("daler", "#online"))
            self.assertIsNotNone(db_b.get_player_if_exists("daler", "#online"))
            self.assertIsNone(db_b.get_player_if_exists("rizer", "#online"))


class TestCommandTable(unittest.TestCase):
    def test_table_covers_all_commands(self):
        # Build the table without running __init__ (which would open the real DB)
        bot = object.__new__(DuckHuntBot)
        table = DuckHuntBot._build_command_table(bot)
        expected = {
            "bang", "bef", "befriend", "reload", "shop", "duckstats", "topduck",
            "globaltop", "use", "give", "duckhelp", "help", "daily", "effects",
            "achievements", "inv", "profile", "rearm", "disarm", "ignore",
            "unignore", "ducklaunch", "join", "part",
        }
        self.assertEqual(set(table.keys()), expected)
        admin_cmds = {c for c, (admin, _h) in table.items() if admin}
        self.assertEqual(
            admin_cmds,
            {"disarm", "ignore", "unignore", "ducklaunch", "join", "part"},
        )
        # !rearm deliberately stays public: handle_rearm enforces the admin-only
        # targets (!rearm <other nick> and !rearm all) itself.
        self.assertFalse(table["rearm"][0])


class TestRearmPermissions(unittest.TestCase):
    """!rearm: any player re-arms themselves, only admins target others/all."""

    def _bot(self):
        # No __init__: classification is pure logic, so no DB/IRC is needed.
        return object.__new__(DuckHuntBot)

    def test_no_args_is_self_rearm(self):
        bot = self._bot()
        self.assertEqual(bot._classify_rearm_request("Offline", []), ("self", "Offline"))

    def test_own_nick_is_self_rearm(self):
        bot = self._bot()
        self.assertEqual(
            bot._classify_rearm_request("Offline", ["offline"]), ("self", "offline")
        )

    def test_all_keyword_is_admin_kind(self):
        bot = self._bot()
        self.assertEqual(bot._classify_rearm_request("Offline", ["all"]), ("all", None))
        self.assertEqual(bot._classify_rearm_request("Offline", ["ALL"]), ("all", None))

    def test_other_nick_is_admin_kind(self):
        bot = self._bot()
        self.assertEqual(
            bot._classify_rearm_request("Offline", ["Multiverse"]),
            ("other", "Multiverse"),
        )

    def test_too_many_args_is_invalid(self):
        bot = self._bot()
        self.assertEqual(
            bot._classify_rearm_request("Offline", ["Multiverse", "#ducks"]),
            ("invalid", None),
        )


class _FakeDB:
    """Tiny in-memory DuckDB stand-in for handler tests."""

    def __init__(self):
        self.players = {}
        self.saved = 0

    def _new_player(self, nick):
        return {
            "nick": nick,
            "gun_confiscated": False,
            "current_ammo": 0,
            "magazines": 0,
            "bullets_per_magazine": 6,
            "xp": 0,
        }

    def get_player(self, nick, channel):
        key = (str(channel).lower(), str(nick).lower())
        return self.players.setdefault(key, self._new_player(nick))

    def get_player_if_exists(self, nick, channel):
        return self.players.get((str(channel).lower(), str(nick).lower()))

    def get_players_for_channel(self, channel):
        channel = str(channel).lower()
        return {nick: p for (ch, nick), p in self.players.items() if ch == channel}

    def iter_all_players(self):
        for (channel, nick), player in sorted(self.players.items()):
            yield channel, nick, player

    def save_database(self):
        self.saved += 1
        return True


class TestRearmHandler(unittest.TestCase):
    """End-to-end checks of handle_rearm's permission model (fake DB, real messages)."""

    def _bot(self, auto_rearm=True):
        bot = object.__new__(DuckHuntBot)
        bot.config = {
            "commands": {"prefix": "!"},
            "features": {"auto_rearm_enabled": auto_rearm},
            "admins": ["Boss"],
        }
        bot.logger = logging.getLogger("test")
        bot.command_prefix = "!"
        bot.messages = MessageManager(os.path.join(PROJECT_ROOT, "messages.json"))
        bot.db = _FakeDB()
        bot.levels = LevelManager("/nonexistent/levels.json")
        bot.sent = []
        bot.send_message = lambda target, message: bot.sent.append((target, message)) or True
        return bot

    def test_player_self_rearm_is_allowed(self):
        bot = self._bot()
        bot.db.get_player("Bob", "#ducks")["gun_confiscated"] = True

        asyncio.run(bot.handle_rearm("Bob", "#ducks", [], user="Bob!user@host"))

        self.assertFalse(bot.db.players[("#ducks", "bob")]["gun_confiscated"])
        self.assertEqual(bot.db.players[("#ducks", "bob")]["current_ammo"], 6)
        self.assertEqual(bot.sent[0][0], "#ducks")
        self.assertEqual(bot.sent[0][1], bot.messages.get("player_rearm_self", nick="Bob"))
        self.assertEqual(bot.db.saved, 1)

    def test_player_can_name_themselves(self):
        bot = self._bot()
        bot.db.get_player("Bob", "#ducks")["gun_confiscated"] = True

        asyncio.run(bot.handle_rearm("Bob", "#ducks", ["bob"], user="Bob!user@host"))

        self.assertFalse(bot.db.players[("#ducks", "bob")]["gun_confiscated"])

    def test_non_admin_cannot_rearm_someone_else(self):
        bot = self._bot()

        asyncio.run(
            bot.handle_rearm("Bob", "#ducks", ["Alice"], user="Bob!user@host")
        )

        self.assertEqual(bot.db.saved, 0, "non-admin rearm was executed")
        self.assertIn("Only admins", bot.sent[0][1])

    def test_admin_can_rearm_someone_else(self):
        bot = self._bot()
        bot.db.get_player("Alice", "#ducks")["gun_confiscated"] = True

        asyncio.run(bot.handle_rearm("Boss", "#ducks", ["Alice"], user="Boss!user@host"))

        self.assertFalse(bot.db.players[("#ducks", "alice")]["gun_confiscated"])
        self.assertEqual(bot.db.saved, 1)
        self.assertIn("Alice", bot.sent[0][1])

    def test_admin_rearm_unknown_player_is_reported(self):
        bot = self._bot()

        asyncio.run(bot.handle_rearm("Boss", "#ducks", ["Ghost"], user="Boss!user@host"))

        self.assertEqual(bot.db.saved, 0)
        self.assertIn("Ghost", bot.sent[0][1])

    def test_non_admin_cannot_rearm_all(self):
        bot = self._bot()
        bot.db.get_player("Alice", "#ducks")["gun_confiscated"] = True

        asyncio.run(bot.handle_rearm("Bob", "#ducks", ["all"], user="Bob!user@host"))

        self.assertTrue(bot.db.players[("#ducks", "alice")]["gun_confiscated"])
        self.assertEqual(bot.db.saved, 0)
        self.assertIn("Only admins", bot.sent[0][1])

    def test_auto_rearm_disabled_blocks_self_service_only(self):
        bot = self._bot(auto_rearm=False)

        asyncio.run(bot.handle_rearm("Bob", "#ducks", [], user="Bob!user@host"))
        self.assertEqual(bot.db.saved, 0)
        self.assertIn("disabled", bot.sent[0][1])

        asyncio.run(bot.handle_rearm("Boss", "#ducks", [], user="Boss!user@host"))
        self.assertEqual(bot.db.saved, 1, "admin self-rearm should still work")


class TestAchievements(unittest.TestCase):
    def _game(self):
        game = object.__new__(DuckGame)
        import logging

        game.logger = logging.getLogger("test")
        return game

    def test_high_roller_awarded_on_xp_spent(self):
        game = self._game()
        player = {"total_xp_spent": 500, "achievements": []}
        awarded = game._check_achievements(player, "xp_spent")
        self.assertEqual([a["id"] for a in awarded], ["high_roller"])

    def test_mystery_lover_awarded(self):
        game = self._game()
        player = {"achievements": []}
        awarded = game._check_achievements(player, "mystery_box")
        self.assertEqual([a["id"] for a in awarded], ["mystery_lover"])

    def test_no_duplicate_awards(self):
        game = self._game()
        player = {"total_xp_spent": 999, "achievements": []}
        game._check_achievements(player, "xp_spent")
        again = game._check_achievements(player, "xp_spent")
        self.assertEqual(again, [])

    def test_all_achievements_have_a_firing_event(self):
        # Every defined achievement id must be reachable from some event branch
        # in _check_achievements (guards against re-introducing dead achievements).
        import inspect

        source = inspect.getsource(DuckGame._check_achievements)
        for ach_id in ACHIEVEMENTS:
            self.assertIn(f'_award("{ach_id}")', source, f"{ach_id} is never awarded")


if __name__ == "__main__":
    unittest.main()
