"""Exercise submission flows without starting the bot or connecting to Firebase."""

import ast
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit

import discord
from discord import app_commands
from discord.ext import commands


class Increment:
    def __init__(self, amount):
        self.amount = amount


class Document:
    def __init__(self, store, key):
        self.store = store
        self.key = key

    def get(self):
        row = self.store.get(self.key)
        return SimpleNamespace(
            exists=row is not None,
            to_dict=lambda: dict(row) if row is not None else None,
        )

    def set(self, data, merge=False):
        if not merge or self.key not in self.store:
            self.store[self.key] = {}
        self.update(data)

    def update(self, data):
        row = self.store[self.key]
        for key, value in data.items():
            if isinstance(value, Increment):
                row[key] = row.get(key, 0) + value.amount
            else:
                row[key] = value


class Database:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return SimpleNamespace(
            document=lambda key: Document(self.store, (name, key)),
            get=lambda: [SimpleNamespace(id=key, to_dict=lambda row=row: dict(row))
                         for (collection, key), row in self.store.items() if collection == name],
        )


class SubmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database()
        self.config = {
            'admin1': 42, 'admin2': 43, 'task_forum_id': 100,
            'franco_channel_id': 101, 'arabic_channel_id': 102,
            'speaking_channel_id': 103, 'dictation_channel_id': 104,
            'vip_questions_channel_id': 105,
        }
        self.tags = dict(worksheet=1, reactivation=2, vocab=3, retell=4, connect=5)
        self.bot = SimpleNamespace(
            user=SimpleNamespace(id=999), get_channel=Mock(),
            fetch_channel=AsyncMock(), process_commands=AsyncMock(),
        )
        self.env = dict(
            discord=discord, Choice=app_commands.Choice, date=date, urlsplit=urlsplit,
            db=self.db, firestore=SimpleNamespace(Increment=Increment), bot=self.bot,
            config=self.config, task_forum_ids=self.tags, users_collection='users',
            threads_collection='threads', worksheet_points_emojis=['20', '22', '24', '26', '28'],
            text_points_emoji='text', voice_points_emoji='voice', vip_question_emoji='vip',
            today=lambda: date(2026, 10, 3), log=AsyncMock(),
            update_leaderboard=AsyncMock(return_value=[]), send_points_message=AsyncMock(),
        )
        # main.py initializes Firebase and runs Discord at import time. Load only
        # the actual constants and functions under test, leaving those services out.
        tree = ast.parse(Path(__file__).resolve().parents[1].joinpath('main.py').read_text(encoding='utf-8'))
        constants = {
            'text_points', 'voice_points', 'worksheet_points', 'vip_question_points',
            'weekly_bonuspercent', 'min_worksheet_length', 'min_dictation_length',
            'min_dictation_voice_length', 'min_written_length', 'min_speaking_length',
        }
        functions = {
            'check_user', 'missed_last_week', 'has_voice_message', 'fetch_submission_message',
            'is_thread_claimed', 'claim_thread', 'update_task_streak', 'handle_writing',
            'handle_speaking', 'handle_worksheets', 'handle_dictation', 'handle_vip_question',
            'handle_message', 'handle_forum_task', 'award_submission', 'on_message',
        }
        selected = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in constants for target in node.targets
            ):
                selected.append(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions:
                node.decorator_list = []
                selected.append(node)
        exec(compile(ast.Module(body=selected, type_ignores=[]), '<submission-functions>', 'exec'), self.env)

    def message(self, content='', attachments=None, tag='worksheet', forum=True, author_id=123):
        channel = Mock(spec=discord.Thread if forum else discord.TextChannel)
        channel.id = 456
        channel.name = 'submission'
        channel.mention = '#submission'
        if forum:
            channel.parent_id = 100
            channel.applied_tags = [] if tag is None else [SimpleNamespace(id=self.tags[tag], name=tag)]
        message = SimpleNamespace(
            content=content, attachments=attachments or [], channel=channel,
            author=SimpleNamespace(id=author_id, name='user', mention=f'<@{author_id}>'),
            guild=SimpleNamespace(id=789), jump_url='https://discord.com/channels/789/456/777',
            add_reaction=AsyncMock(),
        )
        channel.fetch_message = AsyncMock(return_value=message)
        self.bot.get_channel.return_value = channel
        self.bot.fetch_channel.return_value = channel
        return message

    @staticmethod
    def voice(duration=1):
        return SimpleNamespace(content_type='audio/ogg', duration=duration, is_voice_message=lambda: True)

    @staticmethod
    def image():
        return SimpleNamespace(content_type='image/png', is_voice_message=lambda: False)

    async def user(self, message, **changes):
        data = await self.env['check_user'](message)
        self.db.collection('users').document(str(message.author.id)).update(changes)
        data.update(changes)
        return data

    def points(self, author_id=123):
        return self.db.store.get(('users', str(author_id)), {}).get('points', 0)

    def load_function(self, name):
        source = Path(__file__).resolve().parents[1] / 'main.py'
        tree = ast.parse(source.read_text(encoding='utf-8'))
        node = next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == name)
        node.decorator_list = []
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<supporting-function>', 'exec'), self.env)
        return self.env[name]

    async def command(self, message, task_type):
        self.bot.get_channel.return_value = message.channel
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=789), user=SimpleNamespace(mention='@admin'),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await self.env['award_submission'](
            interaction, message.jump_url, app_commands.Choice(name=task_type, value=task_type),
        )
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        return interaction.followup.send.await_args.args[0]

    async def test_all_forum_tags_share_routing_and_bonus(self):
        for tag in self.tags:
            with self.subTest(tag=tag):
                self.db.store.clear()
                message = self.message('short', tag=tag)
                await self.user(message, streak=3)
                reply = await self.command(message, 'forum_task')
                self.assertEqual(self.points(), 26)
                self.assertIn('26', reply)
                self.assertEqual(self.db.store['users', '123']['streak'], 4)
                self.assertIn(('threads', '456'), self.db.store)

    async def test_automatic_worksheet_image(self):
        message = self.message(attachments=[self.image()])
        await self.env['on_message'](message)
        self.assertEqual(self.points(), 20)
        self.bot.process_commands.assert_awaited_once_with(message)

    async def test_automatic_short_worksheet_still_rejected(self):
        message = self.message('short')
        await self.env['on_message'](message)
        self.assertEqual(self.points(), 0)
        self.assertNotIn(('threads', '456'), self.db.store)

    async def test_approved_forum_voice_with_caption_only_gets_voice(self):
        message = self.message('caption ' * 20, [self.voice()], tag='retell')
        await self.command(message, 'forum_task')
        self.assertEqual(self.points(), 20)
        message.add_reaction.assert_awaited_once()

    async def test_claimed_thread_command_reports_skip(self):
        message = self.message('short')
        self.db.store['threads', '456'] = {'tag': 'worksheet'}
        reply = await self.command(message, 'forum_task')
        self.assertEqual(self.points(), 0)
        self.assertIn('already been rewarded', reply)

    async def test_writing_command_keeps_daily_limit(self):
        message = self.message('short', forum=False)
        await self.user(message, last_writing_date='2026-10-03')
        reply = await self.command(message, 'writing')
        self.assertEqual(self.points(), 0)
        self.assertIn('already received writing points today', reply)

    async def test_speaking_command_keeps_daily_limit(self):
        message = self.message(attachments=[self.voice()], forum=False)
        await self.user(message, last_speaking_date='2026-10-03')
        reply = await self.command(message, 'speaking')
        self.assertEqual(self.points(), 0)
        self.assertIn('already received speaking points today', reply)

    async def test_non_forum_command_choices(self):
        cases = [('writing', [], 10), ('speaking', [self.voice()], 15),
                 ('dictation', [self.voice()], 15), ('dictation', [self.image()], 10),
                 ('VIP_question', [], 5)]
        for task_type, attachments, expected in cases:
            with self.subTest(task_type=task_type, expected=expected):
                self.db.store.clear()
                message = self.message('short', attachments, forum=False)
                reply = await self.command(message, task_type)
                self.assertEqual(self.points(), expected)
                self.assertIn(f'**{expected}**', reply)

    async def test_speaking_without_voice_reports_skip(self):
        reply = await self.command(self.message('short', forum=False), 'speaking')
        self.assertEqual(self.points(), 0)
        self.assertIn('no Discord voice message', reply)

    async def test_multiple_voice_attachments_reward_once(self):
        message = self.message(attachments=[self.voice(), self.voice()], tag='retell')
        await self.command(message, 'forum_task')
        self.assertEqual(self.points(), 20)
        message.add_reaction.assert_awaited_once()

    async def test_voice_after_image_in_dictation(self):
        message = self.message(attachments=[self.image(), self.voice()], forum=False)
        await self.command(message, 'dictation')
        self.assertEqual(self.points(), 15)

    async def test_automatic_dictation_mixed_submission_keeps_both_rewards(self):
        message = self.message('text ' * 3, [self.voice(3)], forum=False)
        await self.user(message)
        result = await self.env['handle_dictation'](message)
        self.assertEqual(result, 25)
        self.assertEqual(self.points(), 25)

    async def test_custom_minimum_is_forwarded(self):
        message = self.message('123456', forum=False)
        data = await self.user(message)
        result = await self.env['handle_message'](message, data, min_length=5)
        self.assertEqual(result, 10)

    async def test_invalid_forum_channel_and_missing_tag(self):
        for message in [self.message('short', forum=False), self.message('short', tag=None)]:
            with self.subTest(channel=type(message.channel)):
                self.db.store.clear()
                reply = await self.command(message, 'forum_task')
                self.assertEqual(self.points(), 0)
                self.assertNotIn('Awarded **', reply)

    async def test_excluded_author_does_not_crash(self):
        reply = await self.command(self.message('short', author_id=42), 'forum_task')
        self.assertIn('excluded', reply)
        self.assertEqual(self.points(42), 0)

    async def test_other_server_is_rejected(self):
        message = self.message('short')
        message.guild.id = 987
        reply = await self.command(message, 'forum_task')
        self.assertIn('server where you ran', reply)
        self.assertEqual(self.points(), 0)

    async def test_link_query_trailing_slash_and_angle_brackets(self):
        message = self.message()
        for link in [message.jump_url, message.jump_url + '/', message.jump_url + '?jump=1',
                     '<' + message.jump_url + '>']:
            with self.subTest(link=link):
                result = await self.env['fetch_submission_message'](link)
                self.assertIs(result, message)
                message.channel.fetch_message.assert_awaited_with(777)

    async def test_fetch_channel_when_not_cached(self):
        message = self.message()
        self.bot.get_channel.return_value = None
        result = await self.env['fetch_submission_message'](message.jump_url)
        self.assertIs(result, message)
        self.bot.fetch_channel.assert_awaited_once_with(456)

    async def test_malformed_link_gets_response(self):
        for link in ['bad-link', 'https://example.com/channels/789/456/777',
                     'https://discord.com/channels/@me/456/777']:
            with self.subTest(link=link):
                message = self.message()
                message.jump_url = link
                reply = await self.command(message, 'writing')
                self.assertEqual(self.points(), 0)
                self.assertNotIn('Awarded **', reply)
        self.bot.fetch_channel.assert_not_awaited()

    async def test_fetch_errors_get_responses(self):
        for error_type, status, expected in [
            (discord.NotFound, 404, 'could not be found'),
            (discord.Forbidden, 403, "permission to read"),
        ]:
            with self.subTest(error=error_type):
                message = self.message()
                message.channel.fetch_message.side_effect = error_type(
                    SimpleNamespace(status=status, reason='test'), 'test failure',
                )
                reply = await self.command(message, 'writing')
                self.assertIn(expected, reply)
                self.assertEqual(self.points(), 0)

    async def test_database_failure_never_replays_original_commands(self):
        message = self.message('.some_command')
        self.env['check_user'] = AsyncMock(side_effect=RuntimeError('database failure'))
        reply = await self.command(message, 'forum_task')
        self.assertIn('Could not process', reply)
        self.bot.process_commands.assert_not_awaited()

    async def test_partial_award_failure_does_not_claim_success(self):
        for tag in ['worksheet', 'retell']:
            with self.subTest(tag=tag):
                self.db.store.clear()
                message = self.message('short', tag=tag)
                message.add_reaction.side_effect = discord.Forbidden(
                    SimpleNamespace(status=403, reason='test'), 'reaction failed',
                )
                reply = await self.command(message, 'forum_task')
                self.assertEqual(self.points(), 20)
                self.assertIn(('threads', '456'), self.db.store)
                self.assertIn('Check the user', reply)
                self.assertNotIn('Awarded **', reply)
                message.add_reaction.side_effect = None
                retry_reply = await self.command(message, 'forum_task')
                self.assertEqual(self.points(), 20)
                self.assertIn('already been rewarded', retry_reply)

    async def test_existing_user_document_is_healed(self):
        message = self.message('short')
        self.db.store['users', '123'] = {'points': 7}
        data = await self.env['check_user'](message)
        self.assertEqual(data['points'], 7)
        self.assertEqual(data['streak'], 0)
        self.assertEqual(data['last_writing_date'], '2000-01-01')
        self.assertEqual(self.db.store['users', '123'], data)

    async def test_reward_dm_has_correct_rank_points_and_streak(self):
        sender = self.load_function('send_points_message')
        user = SimpleNamespace(id=123, display_name='User', mention='@user', send=AsyncMock())
        rows = [{'id': '999', 'points': 100}, {'id': '123', 'points': 30, 'streak': 2}]
        await sender(user, 20, 'Worksheet Completed', rows, show_streak=True)
        embed = user.send.await_args.kwargs['embed']
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields['🔥 Current Streak'], '**2** weeks')
        self.assertEqual(fields['🪙 Points Earned'], '**+20**')
        self.assertEqual(fields['📊 Leaderboard Rank'], '**#2**')
        self.assertEqual(fields['🪙 Total Points'], '**30**')

    async def test_disabled_dms_do_not_fail_the_reward_message(self):
        sender = self.load_function('send_points_message')
        user = SimpleNamespace(id=123, display_name='User', mention='@user', send=AsyncMock())
        user.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='test'), 'DMs disabled')
        await sender(user, 10, 'Writing Completed', [{'id': '123', 'points': 10}])
        self.env['log'].assert_awaited()

    async def test_leaderboard_sorts_and_edits_existing_message(self):
        updater = self.load_function('update_leaderboard')
        self.config.update(leaderboard_channel_id=106, leaderboard_message_id=555)
        self.db.store['users', '123'] = {'points': 10, 'streak': 1}
        self.db.store['users', '124'] = {'points': 30, 'streak': 2}
        leaderboard_message = SimpleNamespace(edit=AsyncMock())
        channel = SimpleNamespace(get_partial_message=Mock(return_value=leaderboard_message), send=AsyncMock())
        self.bot.get_channel.return_value = channel
        self.bot.get_user = Mock(return_value=SimpleNamespace(display_name='User'))
        rows = await updater()
        self.assertEqual([row['id'] for row in rows], ['124', '123'])
        embed = leaderboard_message.edit.await_args.kwargs['embed']
        self.assertIn('Points: 30', embed.fields[0].value)
        channel.send.assert_not_awaited()

    async def test_deleted_leaderboard_is_recreated_and_saved(self):
        updater = self.load_function('update_leaderboard')
        self.env['config_collection'] = 'config'
        self.config.update(leaderboard_channel_id=106, leaderboard_message_id=555)
        self.db.store['users', '123'] = {'points': 10, 'streak': 1}
        old_message = SimpleNamespace(edit=AsyncMock(side_effect=discord.NotFound(
            SimpleNamespace(status=404, reason='test'), 'message deleted',
        )))
        channel = SimpleNamespace(get_partial_message=Mock(return_value=old_message),
                                  send=AsyncMock(return_value=SimpleNamespace(id=556)))
        self.bot.get_channel.return_value = channel
        self.bot.get_user = Mock(return_value=SimpleNamespace(display_name='User'))
        await updater()
        self.assertEqual(self.config['leaderboard_message_id'], 556)
        self.assertEqual(self.db.store['config', 'settings']['leaderboard_message_id'], '556')
        channel.send.assert_awaited_once()

    def test_actual_command_registration(self):
        tree = ast.parse(Path(__file__).resolve().parents[1].joinpath('main.py').read_text(encoding='utf-8'))
        node = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == 'award_submission')
        command_bot = commands.Bot(command_prefix='.', intents=discord.Intents.none())
        registration_env = dict(self.env, bot=command_bot, app_commands=app_commands,
                                get_guild=lambda: discord.Object(id=789))
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<command-registration>', 'exec'), registration_env)
        command = command_bot.tree.get_command('award_submission', guild=discord.Object(id=789))
        self.assertIsNotNone(command)
        self.assertEqual(len(command.parameters[1].choices), 5)
        self.assertEqual(len(command.checks), 1)


if __name__ == '__main__':
    unittest.main()
