"""Exercise the real bot functions with an in-memory Firestore substitute.

Only function definitions are loaded: no credentials, bot startup or network.
The substitute stages writes and can simulate a conflicting transaction retry.
"""

import ast
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord
from discord.app_commands import Choice
import streak_test_helpers as streaks


class FakeDocument:
    def __init__(self, database, key):
        self.database = database
        self.key = key
        self.id = key.split('/')[-1]

    def get(self, transaction=None):
        if transaction and transaction.writes:
            raise AssertionError('All transaction reads must happen before writes.')
        data = deepcopy(self.database.records.get(self.key))
        return SimpleNamespace(exists=data is not None, to_dict=lambda: data, id=self.id)

    def update(self, fields):
        transaction = self.database.transaction()
        transaction.set(self, fields, merge=True)
        transaction.commit()


class FakeTransaction:
    def __init__(self, database):
        self.database = database
        self.writes = []

    def set(self, reference, fields, merge=False):
        self.writes.append((reference.key, deepcopy(fields), merge))

    def create(self, reference, fields):
        self.writes.append((reference.key, deepcopy(fields), False))

    def commit(self):
        for key, fields, merge in self.writes:
            record = {}
            if merge:
                record.update(self.database.records.get(key, {}))
            for field, value in fields.items():
                if isinstance(value, tuple) and value[0] == 'increment':
                    record[field] = record.get(field, 0) + value[1]
                else:
                    record[field] = value
            self.database.records[key] = record
        self.writes = []


class FakeDatabase:
    def __init__(self):
        self.records = {}
        self.retry_once = False
        self.before_retry = None

    def collection(self, name):
        def document(identifier):
            return FakeDocument(self, f'{name}/{identifier}')

        def get():
            documents = []
            for key in self.records:
                if key.startswith(name + '/'):
                    documents.append(FakeDocument(self, key).get())
            return documents

        return SimpleNamespace(document=document, get=get)

    def transaction(self):
        return FakeTransaction(self)

    def transactional(self, function):
        def run(transaction, *args, **kwargs):
            result = function(transaction, *args, **kwargs)
            if self.retry_once:
                self.retry_once = False
                transaction.writes = []
                if self.before_retry:
                    self.before_retry()
                result = function(transaction, *args, **kwargs)
            transaction.commit()
            return result
        return run


def load_bot_functions(database):
    namespace = dict(vars(streaks))
    namespace.update({
        'discord': discord, 'Choice': Choice, 'date': date, 'datetime': datetime,
        'time': time, 'timedelta': timedelta, 'timezone': timezone,
        'db': database, 'users_collection': 'users', 'threads_collection': 'thread_submissions',
        'config': {'task_forum_id': 100, 'admin1': 10, 'admin2': 11},
        'text_points': 10, 'voice_points': 15, 'worksheet_points': 20,
        'text_points_emoji': 'text', 'voice_points_emoji': 'voice', 'weekly_bonuspercent': 10,
        'min_written_length': 20, 'min_speaking_length': 5, 'min_worksheet_length': 100,
        'worksheet_points_emojis': ['0', '1', '2', '3', '4'],
        'task_forum_ids': {'worksheet': 1, 'vocab': 2, 'retell': 3, 'reactivation': 4, 'connect': 5},
        'firestore': SimpleNamespace(Increment=lambda amount: ('increment', amount)),
    })
    source = Path(__file__).resolve().parents[1] / 'main.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    functions = []
    transactions = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Attribute) and decorator.attr == 'transactional':
                    transactions.append(node.name)
            node.decorator_list = []
            functions.append(node)
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), 'exec'), namespace)
    for name in transactions:
        namespace[name] = database.transactional(namespace[name])
    namespace['today'] = lambda: date(2026, 10, 1)
    namespace['update_leaderboard'] = AsyncMock(return_value=[])
    namespace['send_points_message'] = AsyncMock()
    namespace['log'] = AsyncMock()
    namespace['bot'] = SimpleNamespace(user=object(), get_user=MagicMock(), fetch_user=AsyncMock())
    return namespace


class BotStreakTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = FakeDatabase()
        self.bot = load_bot_functions(self.db)
        self.user_ref = self.db.collection('users').document('42')

    def message(self, identifier=200, worksheet=True, text=None, voice=False, image=False):
        channel = MagicMock(spec=discord.Thread)
        channel.id = identifier
        channel.parent_id = 100
        channel.name = 'Task'
        channel.mention = f'<#{identifier}>'
        tag_id = 2
        if worksheet:
            tag_id = 1
        channel.applied_tags = [SimpleNamespace(id=tag_id, name='Task')]
        attachments = []
        if voice or image:
            content_type = 'audio/ogg'
            if image:
                content_type = 'image/png'
            attachments.append(SimpleNamespace(content_type=content_type, duration=8,
                                               is_voice_message=lambda: voice))
        if text is None:
            text = 'x' * 100
        return SimpleNamespace(channel=channel, content=text, attachments=attachments,
                               author=SimpleNamespace(id=42, name='Learner', mention='<@42>'),
                               add_reaction=AsyncMock())

    async def award(self, number, identifier, **kwargs):
        self.bot['today'] = lambda: date(2026, 10, number)
        return await self.bot['handle_forum_task'](self.message(identifier, **kwargs), {})

    def refresh(self, number):
        return self.bot['refresh_streak'](self.db.transaction(), self.user_ref, date(2026, 10, number))

    async def test_points_streak_and_claim_save_together(self):
        self.assertEqual(await self.award(1, 200), 20)
        self.assertEqual(await self.award(5, 201), 22)
        self.assertEqual(await self.award(8, 202), 22)
        state = self.db.records['users/42']
        self.assertEqual(state['points'], 64)
        self.assertEqual(state['streak'], 2)
        self.assertEqual(state['streak_shields'], 1)
        self.assertIn('thread_submissions/202', self.db.records)

    async def test_duplicate_thread_cannot_award_points_or_shield(self):
        await self.award(1, 200)
        self.assertEqual(await self.award(5, 200), 0)
        self.assertEqual(self.db.records['users/42']['points'], 20)
        self.assertEqual(self.db.records['users/42']['streak_shields'], 0)

    async def test_text_and_voice_points_but_only_one_streak_reward(self):
        self.assertEqual(await self.award(1, 200, worksheet=False, voice=True), 40)
        state = self.db.records['users/42']
        self.assertEqual(state['streak'], 1)
        self.assertEqual(state['streak_shields'], 0)
        self.bot['send_points_message'].assert_awaited_once()

    async def test_invalid_submission_does_not_claim_thread(self):
        self.assertEqual(await self.award(1, 200, text='short'), 0)
        self.assertEqual(self.db.records, {})
        self.assertEqual(await self.award(1, 200, text='', image=True), 20)

    async def test_expired_streak_does_not_keep_bonus(self):
        await self.award(1, 200)
        self.db.records['users/42']['streak'] = 8
        self.assertEqual(await self.award(15, 201), 20)
        self.assertEqual(self.db.records['users/42']['streak'], 1)

    async def test_transaction_retry_does_not_double_reward(self):
        self.db.retry_once = True
        await self.award(1, 200)
        self.assertEqual(self.db.records['users/42']['points'], 20)
        self.assertEqual(self.db.records['users/42']['streak'], 1)

    async def test_retry_sees_another_reward_and_earns_shield(self):
        await self.award(1, 200)
        self.bot['today'] = lambda: date(2026, 10, 8)
        def another_task_commits():
            state, events, bonus = streaks.record_task(self.db.records['users/42'], date(2026, 10, 8))
            self.db.records['users/42'].update(state)
            self.db.records['users/42']['points'] += 22
        self.db.retry_once = True
        self.db.before_retry = another_task_commits
        await self.bot['handle_forum_task'](self.message(201), {})
        self.assertEqual(self.db.records['users/42']['streak'], 2)
        self.assertEqual(self.db.records['users/42']['streak_shields'], 1)
        self.assertEqual(self.db.records['users/42']['points'], 66)

    async def test_retry_sees_same_thread_claimed_by_another_request(self):
        await self.award(1, 200)
        def another_request_claims_thread():
            self.db.records['thread_submissions/201'] = {'user_id': 99}
        self.db.retry_once = True
        self.db.before_retry = another_request_claims_thread
        self.assertEqual(await self.award(5, 201), 0)
        self.assertEqual(self.db.records['users/42']['points'], 20)
        self.assertEqual(self.db.records['users/42']['streak_shields'], 0)

    async def test_manual_voice_approval_does_not_invent_text_points(self):
        result = await self.bot['handle_forum_task'](
            self.message(200, worksheet=False, text='', voice=True), {}, admin_approved=True,
        )
        self.assertEqual(result, 20)

    async def test_reminder_reservation_survives_restart(self):
        await self.award(1, 200)
        self.assertIsNotNone(self.refresh(13)['reminder'])
        self.bot = load_bot_functions(self.db)
        self.assertIsNone(self.refresh(13)['reminder'])
        self.assertIsNotNone(self.refresh(14)['reminder'])
        await self.award(14, 201)
        self.assertIsNone(self.refresh(14)['reminder'])

    async def test_repeated_check_does_not_spend_shield_twice(self):
        await self.award(1, 200)
        await self.award(5, 201)
        self.assertEqual(self.refresh(15)['state']['streak'], 1)
        self.assertEqual(self.refresh(16)['events'], [])
        self.assertEqual(self.refresh(22)['state']['streak'], 0)

    async def test_new_user_fields_and_legacy_migration_do_not_overwrite_points(self):
        message = self.message()
        state = await self.bot['check_user'](message)
        self.assertEqual(state['streak_shields'], 0)
        self.db.records['users/42'] = {'points': 99, 'streak': 5, streaks.LAST_TASK: '2026-10-01'}
        state = await self.bot['check_user'](message)
        self.assertEqual(state['points'], 99)
        self.assertEqual(state['streak'], 5)
        self.assertEqual(state[streaks.ANCHOR], '2026-10-01')

    async def test_daily_loop_continues_after_malformed_record(self):
        self.db.records['users/99'] = {'streak': 3, streaks.LAST_TASK: 'invalid'}
        await self.award(1, 200)
        self.bot['today'] = lambda: date(2026, 10, 13)
        member = SimpleNamespace(send=AsyncMock())
        self.bot['bot'].get_user.return_value = member
        await self.bot['run_streak_checks']()
        member.send.assert_awaited_once()
        self.assertIn('<t:', member.send.call_args.args[0])
        self.assertIn('/my_streak', member.send.call_args.args[0])
        await self.bot['run_streak_checks']()
        member.send.assert_awaited_once()

    async def test_writing_and_speaking_stay_daily_and_do_not_earn_shields(self):
        message = self.message(200, worksheet=False, voice=True)
        state = await self.bot['check_user'](message)
        self.assertEqual(await self.bot['handle_writing'](message, state), 10)
        self.assertEqual(await self.bot['handle_speaking'](message, state), 15)
        state = self.db.records['users/42']
        self.assertEqual(state['streak'], 0)
        self.assertEqual(state['streak_shields'], 0)
        self.assertEqual(state['points'], 25)
        self.assertEqual(await self.bot['handle_writing'](message, state), 0)
        self.assertEqual(await self.bot['handle_speaking'](message, state), 0)

    async def test_admin_commands_register_and_validate(self):
        for name in ('streak_fields', 'set_streak_field', 'set_streak'):
            command = discord.app_commands.Command(name=name, description='Test', callback=self.bot[name])
            self.assertIn('user', [parameter.name for parameter in command.parameters])
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
            user=SimpleNamespace(mention='<@10>'),
        )
        user = SimpleNamespace(id=42, mention='<@42>', name='Learner')
        await self.bot['set_streak'](interaction, user, 4)
        self.assertEqual(self.db.records['users/42']['streak'], 4)
        await self.bot['set_streak_field'](interaction, user, Choice(name='Shield', value='streak_shields'), '2')
        self.assertEqual(self.db.records['users/42']['streak_shields'], 0)
        self.assertIn('Cannot set', interaction.followup.send.call_args.args[0])
        await self.bot['set_streak_field'](interaction, user, Choice(name='Shield', value='streak_shields'), '1')
        self.assertEqual(self.db.records['users/42']['streak_shields'], 1)
        await self.bot['set_streak'](interaction, user, 0)
        self.assertEqual(self.db.records['users/42']['streak_shields'], 0)

    def streak_interaction(self):
        return SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            user=SimpleNamespace(id=42, mention='<@42>'),
        )

    async def test_my_streak_command(self):
        interaction = self.streak_interaction()
        await self.bot['my_streak'](interaction)
        embed = interaction.followup.send.call_args.kwargs['embed']
        self.assertEqual(embed.title, '🔥 Task Streak Status')
        self.assertIn('0', embed.fields[0].value)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertTrue(interaction.followup.send.call_args.kwargs['ephemeral'])
        await self.award(1, 200)
        await self.bot['my_streak'](interaction)
        embed = interaction.followup.send.call_args.kwargs['embed']
        self.assertIn('1', embed.fields[0].value)
        self.assertIn('Completed for this week', embed.fields[3].name)

    async def test_my_streak_explains_reset_and_refreshes_leaderboard_once(self):
        await self.award(1, 200)
        self.bot['today'] = lambda: date(2026, 10, 15)
        self.bot['update_leaderboard'].reset_mock()
        interaction = self.streak_interaction()

        await self.bot['my_streak'](interaction)

        self.assertEqual(self.db.records['users/42']['streak'], 0)
        self.assertEqual(self.db.records['users/42']['points'], 20)
        embed = interaction.followup.send.call_args.kwargs['embed']
        self.assertEqual(embed.fields[-1].name, 'Streak update')
        self.assertIn('missed a week without a shield', embed.fields[-1].value)
        self.bot['update_leaderboard'].assert_awaited_once()

        await self.bot['my_streak'](interaction)
        embed = interaction.followup.send.call_args.kwargs['embed']
        self.assertNotIn('Streak update', [field.name for field in embed.fields])
        self.bot['update_leaderboard'].assert_awaited_once()

    async def test_my_streak_explains_consumed_shield(self):
        await self.award(1, 200)
        await self.award(5, 201)
        self.bot['today'] = lambda: date(2026, 10, 15)
        self.bot['update_leaderboard'].reset_mock()
        interaction = self.streak_interaction()

        await self.bot['my_streak'](interaction)

        state = self.db.records['users/42']
        self.assertEqual((state['streak'], state['streak_shields']), (1, 0))
        embed = interaction.followup.send.call_args.kwargs['embed']
        self.assertIn('shield protected one missed week', embed.fields[-1].value)
        self.assertIn('Pending Submission', embed.fields[3].name)
        self.bot['update_leaderboard'].assert_awaited_once()

    async def test_my_streak_displays_all_changes_after_multiple_missed_weeks(self):
        await self.award(1, 200)
        await self.award(5, 201)
        self.bot['today'] = lambda: date(2026, 10, 29)
        interaction = self.streak_interaction()

        await self.bot['my_streak'](interaction)

        self.assertEqual(self.db.records['users/42']['streak'], 0)
        embed = interaction.followup.send.call_args.kwargs['embed']
        self.assertIn('shield protected one missed week', embed.fields[-1].value)
        self.assertIn('missed a week without a shield', embed.fields[-1].value)

    async def test_my_streak_uses_one_date_across_utc_midnight(self):
        await self.award(1, 200)
        self.bot['today'] = MagicMock(side_effect=[date(2026, 10, 14), date(2026, 10, 15)])
        self.bot['update_leaderboard'].reset_mock()
        interaction = self.streak_interaction()

        await self.bot['my_streak'](interaction)

        embed = interaction.followup.send.call_args.kwargs['embed']
        deadline = int(datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp())
        self.assertIn(f'<t:{deadline}:R>', embed.fields[3].value)
        self.assertEqual(self.db.records['users/42']['streak'], 1)
        self.bot['today'].assert_called_once()
        self.bot['update_leaderboard'].assert_not_awaited()

    async def test_my_streak_does_not_claim_daily_reminder(self):
        await self.award(1, 200)
        self.bot['today'] = lambda: date(2026, 10, 13)
        await self.bot['my_streak'](self.streak_interaction())
        self.assertIsNone(self.db.records['users/42']['streak_last_reminder_key'])
        self.assertIsNotNone(self.refresh(13)['reminder'])

    async def test_leaderboard_failure_does_not_undo_private_status(self):
        await self.award(1, 200)
        self.bot['today'] = lambda: date(2026, 10, 15)
        self.bot['update_leaderboard'].side_effect = RuntimeError('Leaderboard unavailable')
        interaction = self.streak_interaction()

        await self.bot['my_streak'](interaction)

        interaction.followup.send.assert_awaited_once()
        self.assertIn('embed', interaction.followup.send.call_args.kwargs)
        self.assertEqual(self.db.records['users/42']['streak'], 0)
        self.assertIn('Could not refresh leaderboard after /my_streak', self.bot['log'].call_args.args[0])

    async def test_shield_message_matches_two_task_replacement_rule(self):
        await self.award(1, 200)
        await self.award(5, 201)
        self.refresh(15)
        message = self.bot['streak_event_text'](['shield_used'])
        self.assertIn('second qualifying task within the same active week', message)
        await self.award(15, 202)
        self.assertEqual(self.db.records['users/42']['streak_shields'], 0)
        await self.award(16, 203)
        self.assertEqual(self.db.records['users/42']['streak_shields'], 1)

    def test_my_streak_registers_without_arguments(self):
        command = discord.app_commands.Command(
            name='my_streak', description='Check streak', callback=self.bot['my_streak'],
        )
        self.assertEqual(command.parameters, [])


if __name__ == '__main__':
    unittest.main()
