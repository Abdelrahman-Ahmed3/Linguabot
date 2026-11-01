"""Check recommendation rewards without connecting to Discord or Firebase."""
import ast
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord
from discord.app_commands import Choice
import test_phrase_of_the_week as phrase_tests

SOURCE = phrase_tests.SOURCE


class RecommendationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        phrase_tests.PhraseOfTheWeekTests.setUp(self)
        self.config["recommendations_channel_id"] = 30
        self.message.channel.id = 30
        self.ns["recommendation_emoji"] = "⭐"
        nodes = []
        for node in ast.parse(SOURCE.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_recommendation":
                nodes.append(node)
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "recommendation_points"
                for target in node.targets
            ):
                nodes.append(node)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), self.ns)

    async def test_each_post_earns_five_points(self):
        for _ in range(2):
            await self.ns["on_message"](self.message)
        self.assertEqual(self.doc.update.call_count, 2)
        self.doc.update.assert_called_with({"points": ("increment", 5)})
        self.message.add_reaction.assert_awaited_with("⭐")
        self.assertEqual(self.ns["update_leaderboard"].await_count, 2)
        self.ns["handle_writing"].assert_not_awaited()

    async def test_unconfigured_channel_does_not_award_points(self):
        self.config.pop("recommendations_channel_id")
        await self.ns["on_message"](self.message)
        self.doc.update.assert_not_called()
        self.bot.process_commands.assert_awaited_once_with(self.message)

    async def test_admins_and_bot_are_excluded(self):
        for admin_id in (10, 11):
            self.message.author.id = admin_id
            await self.ns["on_message"](self.message)
        self.message.author = self.bot.user
        await self.ns["on_message"](self.message)
        self.doc.update.assert_not_called()

    async def test_new_user_initialized_before_reward(self):
        self.doc.get.return_value.to_dict.return_value = None
        await self.ns["on_message"](self.message)
        self.db.transaction.return_value.set.assert_called_once()
        self.doc.update.assert_called_once_with({"points": ("increment", 5)})

    async def test_manual_award(self):
        self.ns["fetch_submission_message"].return_value = self.message
        interaction = SimpleNamespace(
            guild=self.message.guild, user=SimpleNamespace(mention="<@10>"),
            response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
        )
        await self.ns["award_submission"](
            interaction, self.message.jump_url, Choice(name="Recommendation", value="recommendation"),
        )
        self.doc.update.assert_called_once_with({"points": ("increment", 5)})
        self.assertIn("Awarded **5** points", interaction.followup.send.call_args.args[0])

    async def test_configure_saves_channel_and_preserves_it_when_omitted(self):
        settings = {
            "text_points_emoji": "⭐", "voice_points_emoji": "⭐", "vip_question_emoji": "⭐",
            "worksheet_points_emojis": ["⭐"] * 5, "winner_roles": ["1", "2", "3"],
            "task_forum_ids": dict.fromkeys(["worksheet", "reactivation", "vocab", "retell", "connect"], "50"),
        }
        self.doc.get.return_value.to_dict.side_effect = lambda: dict(settings)
        self.doc.set.side_effect = lambda values, **kwargs: settings.update(values)
        self.ns["start_background_tasks"] = MagicMock()
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock()), user=SimpleNamespace(mention="<@10>"),
        )
        channel = SimpleNamespace(id=20)
        await self.ns["configure"](
            interaction, *([channel] * 12), recommendations_channel=SimpleNamespace(id=30),
        )
        self.assertEqual(settings["recommendations_channel_id"], "30")
        self.assertEqual(self.config["recommendations_channel_id"], 30)
        await self.ns["configure"](interaction, *([channel] * 12))
        self.assertEqual(self.config["recommendations_channel_id"], 30)

    def test_discord_registers_optional_channel(self):
        command = discord.app_commands.Command(
            name="configure", description="Configure channels", callback=self.ns["configure"],
        )
        parameter = next(p for p in command.parameters if p.name == "recommendations_channel")
        self.assertFalse(parameter.required)


if __name__ == "__main__":
    unittest.main()
