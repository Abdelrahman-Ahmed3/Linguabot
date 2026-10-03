"""Load the complete application with real dependencies and external services disabled."""

import logging
import os
import runpy
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import discord
import webserver
from discord import app_commands


class StartupTests(unittest.IsolatedAsyncioTestCase):
    def load_bot(self, environment='dev'):
        settings = {
            'server_id': '789', 'admin1': '42', 'admin2': '43',
            'task_forum_id': '100', 'text_points_emoji': 'text',
            'voice_points_emoji': 'voice', 'vip_question_emoji': 'vip',
            'worksheet_points_emojis': ['20', '22', '24', '26', '28'],
            'task_forum_ids': {
                'worksheet': '1', 'reactivation': '2', 'vocab': '3',
                'retell': '4', 'connect': '5',
            },
            'winner_roles': ['201', '202', '203'],
        }
        database = Mock()
        database.collection.return_value.document.return_value.get.return_value = Mock(
            exists=True, to_dict=lambda: dict(settings),
        )
        source = Path(__file__).resolve().parents[1] / 'main.py'
        with (
            patch.dict(os.environ, {
                'BOT_ENV': environment, 'DISCORD_TOKEN': 'offline-test-token',
                'FIREBASE_CREDS': '{}',
            }),
            patch('dotenv.load_dotenv'),
            patch('firebase_admin.credentials.Certificate'),
            patch('firebase_admin.initialize_app'),
            patch('firebase_admin.firestore.client', return_value=database),
            patch('logging.FileHandler', return_value=logging.NullHandler()),
            patch('webserver.keep_alive') as keep_alive,
            patch('discord.ext.commands.Bot.run') as run_bot,
        ):
            namespace = runpy.run_path(str(source))
            keep_alive.assert_called_once()
            run_bot.assert_called_once()
        self.addAsyncCleanup(namespace['bot'].close)
        return namespace

    async def test_complete_dev_startup_and_command_registration(self):
        namespace = self.load_bot()
        bot = namespace['bot']
        commands = bot.tree.get_commands(guild=discord.Object(id=789))
        self.assertIn('award_submission', [command.name for command in commands])
        self.assertEqual(namespace['users_collection'], 'users')
        self.assertEqual(namespace['threads_collection'], 'thread_submissions')
        self.assertFalse(namespace['monthly_leaderboard'].is_running())
        self.assertFalse(namespace['weekly_leaderboard'].is_running())
        self.assertFalse(namespace['check_streaks'].is_running())

    async def test_production_uses_separate_collections(self):
        namespace = self.load_bot('prod')
        self.assertEqual(namespace['users_collection'], 'prod_users')
        self.assertEqual(namespace['config_collection'], 'prod_config')
        self.assertEqual(namespace['threads_collection'], 'prod_thread_submissions')

    async def test_award_command_requires_administrator(self):
        namespace = self.load_bot()
        command = namespace['bot'].tree.get_command('award_submission', guild=discord.Object(id=789))
        interaction = Mock(spec=discord.Interaction)
        interaction.permissions = discord.Permissions.none()
        with self.assertRaises(app_commands.MissingPermissions):
            command.checks[0](interaction)
        interaction.permissions = discord.Permissions(administrator=True)
        self.assertTrue(command.checks[0](interaction))

    def test_healthcheck_route(self):
        with webserver.app.test_client() as client:
            response = client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), 'discord bot good!')


if __name__ == '__main__':
    unittest.main()
