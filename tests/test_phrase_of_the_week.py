"""Exercise submission paths without starting the bot or connecting to Firebase."""
import ast
from datetime import date
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord
from discord.app_commands import Choice
from streak_test_helpers import normalize_streak


SOURCE = Path(__file__).resolve().parents[1] / "main.py"


def load_functions(namespace):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names = {
        "load_config", "check_user", "initialize_user", "missing_user_defaults", "configure", "handle_phrase_of_the_week",
        "on_message", "award_submission",
    }
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)


class PhraseOfTheWeekTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = dict.fromkeys([
            "server_id", "admin1", "admin2", "franco_channel_id", "arabic_channel_id",
            "speaking_channel_id", "dictation_channel_id", "vip_questions_channel_id",
            "task_forum_id", "phrase_of_the_week_channel_id",
        ])
        self.config.update(server_id=1, admin1=10, admin2=11,
                           phrase_of_the_week_channel_id=20, franco_channel_id=21)
        self.db = MagicMock()
        self.doc = self.db.collection.return_value.document.return_value
        self.doc.get.return_value.exists = True
        self.doc.get.return_value.to_dict.return_value = {
            "points": 0, "streak": 0, "last_worksheet_date": "2000-01-01",
            "first_worksheet_thisWeek_date": "2000-01-01",
            "last_writing_date": "2000-01-01", "last_speaking_date": "2000-01-01",
        }
        self.bot = SimpleNamespace(user=object(), process_commands=AsyncMock())
        self.ns = {
            "discord": discord, "Choice": Choice, "db": self.db,
            "config": self.config, "config_collection": "config", "users_collection": "users",
            "environment": "dev", "bot": self.bot,
            "normalize_streak": normalize_streak, "today": lambda: date(2026, 10, 1),
            "firestore": SimpleNamespace(Increment=lambda amount: ("increment", amount)),
            "phrase_of_the_week_points": 5, "phrase_of_the_week_emoji": "⭐",
            "update_leaderboard": AsyncMock(), "log": AsyncMock(),
            "handle_writing": AsyncMock(), "handle_speaking": AsyncMock(),
            "handle_forum_task": AsyncMock(), "handle_dictation": AsyncMock(),
            "handle_vip_question": AsyncMock(), "fetch_submission_message": AsyncMock(),
        }
        load_functions(self.ns)
        self.message = SimpleNamespace(
            author=SimpleNamespace(id=42, mention="<@42>"), channel=SimpleNamespace(id=20, mention="<#20>"),
            guild=SimpleNamespace(id=1), attachments=[], add_reaction=AsyncMock(),
            jump_url="https://discord.com/channels/1/20/30",
        )

    async def test_automatic_submission_awards_five_points(self):
        await self.ns["on_message"](self.message)
        self.db.collection.assert_called_with("users")
        self.db.collection.return_value.document.assert_called_with("42")
        self.doc.update.assert_called_once_with({"points": ("increment", 5)})
        self.message.add_reaction.assert_awaited_once_with("⭐")
        self.ns["update_leaderboard"].assert_awaited_once()
        self.bot.process_commands.assert_awaited_once_with(self.message)

    async def test_existing_writing_channel_still_processes_commands(self):
        self.message.channel.id = 21
        await self.ns["on_message"](self.message)
        self.ns["handle_writing"].assert_awaited_once()
        self.doc.update.assert_not_called()
        self.bot.process_commands.assert_awaited_once()

    async def test_unconfigured_phrase_channel_does_not_break_existing_channels(self):
        self.config["phrase_of_the_week_channel_id"] = None
        self.message.channel.id = 21
        await self.ns["on_message"](self.message)
        self.ns["handle_writing"].assert_awaited_once()
        self.bot.process_commands.assert_awaited_once()

    async def test_untracked_channel_does_not_award_points(self):
        self.message.channel.id = 99
        await self.ns["on_message"](self.message)
        self.doc.update.assert_not_called()
        self.bot.process_commands.assert_awaited_once()

    async def test_admin_and_bot_messages_are_excluded(self):
        for admin_id in (10, 11):
            self.message.author.id = admin_id
            await self.ns["on_message"](self.message)
        self.message.author = self.bot.user
        await self.ns["on_message"](self.message)
        self.doc.update.assert_not_called()
        self.ns["update_leaderboard"].assert_not_awaited()

    async def test_first_submission_creates_user_before_reward(self):
        self.doc.get.return_value.exists = False
        self.doc.get.return_value.to_dict.return_value = None
        await self.ns["on_message"](self.message)
        transaction = self.db.transaction.return_value
        transaction.set.assert_called_once()
        self.assertEqual(transaction.set.call_args.args[1]["points"], 0)
        self.doc.update.assert_called_once_with({"points": ("increment", 5)})

    async def test_admin_manual_submission_awards_five_points(self):
        self.ns["fetch_submission_message"].return_value = self.message
        interaction = SimpleNamespace(
            guild=self.message.guild, user=SimpleNamespace(mention="<@10>"),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
        )
        await self.ns["award_submission"](
            interaction, self.message.jump_url,
            Choice(name="Phrase Of The Week", value="phrase_of_the_week"),
        )
        self.doc.update.assert_called_once_with({"points": ("increment", 5)})
        self.assertIn("Awarded **5** points", interaction.followup.send.call_args.args[0])

    async def test_configuration_saves_and_loads_phrase_channel(self):
        settings = {
            "text_points_emoji": "⭐", "voice_points_emoji": "⭐", "vip_question_emoji": "⭐",
            "worksheet_points_emojis": ["⭐"] * 5, "winner_roles": ["1", "2", "3"],
            "task_forum_ids": dict.fromkeys(["worksheet", "reactivation", "vocab", "retell", "connect"], "50"),
        }
        self.doc.get.return_value.to_dict.side_effect = lambda: dict(settings)
        self.doc.set.side_effect = lambda values, **kwargs: settings.update(values)
        self.ns["start_background_tasks"] = MagicMock()
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock()),
            user=SimpleNamespace(mention="<@10>"),
        )
        channel = SimpleNamespace(id=20)
        await self.ns["configure"](interaction, *([channel] * 12))
        self.assertEqual(settings["phrase_of_the_week_channel_id"], "20")
        self.assertEqual(self.config["phrase_of_the_week_channel_id"], 20)
        self.ns["start_background_tasks"].assert_called_once()
        interaction.response.send_message.assert_awaited_once_with(
            "Config updated successfully!", ephemeral=True,
        )

    def test_configure_registers_with_discord(self):
        command = discord.app_commands.Command(
            name="configure", description="Configure channels", callback=self.ns["configure"],
        )
        self.assertIn("phrase_of_the_week_channel", [p.name for p in command.parameters])


if __name__ == "__main__":
    unittest.main()
