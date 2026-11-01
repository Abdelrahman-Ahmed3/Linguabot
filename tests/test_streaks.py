"""Run with: python -m unittest discover -s tests -v"""

from datetime import date
import unittest

from streak_test_helpers import (
    ANCHOR, LAST_TASK, empty_streak, normalize_streak, advance_streak,
    record_task, streak_reminder, edit_streak_fields,
)


def day(number):
    return date(2026, 10, number)


class StreakTests(unittest.TestCase):
    def submit(self, state, number):
        result, events, bonus = record_task(state, day(number))
        return result

    def test_first_task_completes_first_period(self):
        state, events, bonus = record_task(empty_streak(), day(1))
        self.assertEqual(state['streak'], 1)
        self.assertEqual(state['streak_shields'], 0)
        self.assertEqual(state[ANCHOR], '2026-10-01')
        self.assertEqual(bonus, 0)
        for number in range(1, 15):
            state, events = advance_streak(state, day(number))
            self.assertEqual(state['streak'], 1)
        state, events = advance_streak(state, day(15))
        self.assertEqual(state['streak'], 0)

    def test_extra_tasks_fill_one_shield_without_moving_anchor(self):
        state = self.submit(empty_streak(), 1)
        for number in (1, 5, 7):
            state = self.submit(state, number)
            self.assertEqual(state['streak'], 1)
            self.assertEqual(state['streak_shields'], 1)
            self.assertEqual(state[ANCHOR], '2026-10-01')

    def test_every_day_in_next_period_can_earn_increase(self):
        initial = self.submit(empty_streak(), 1)
        for number in range(8, 15):
            with self.subTest(number=number):
                state = self.submit(initial, number)
                self.assertEqual(state['streak'], 2)
                self.assertEqual(state['streak_shields'], 0)
                self.assertEqual(state[ANCHOR], initial[ANCHOR])
                state = self.submit(state, 15)
                self.assertEqual(state['streak'], 3)

    def test_late_completion_does_not_move_next_boundary(self):
        state = self.submit(empty_streak(), 1)
        state = self.submit(state, 14)
        state = self.submit(state, 15)
        self.assertEqual(state['streak'], 3)
        self.assertEqual(state[ANCHOR], '2026-10-01')

    def test_shield_used_once_across_daily_checks(self):
        state = self.submit(empty_streak(), 1)
        state = self.submit(state, 5)
        state, events = advance_streak(state, day(15))
        self.assertEqual(events[0], 'shield_used')
        self.assertEqual(state['streak_last_processed_period'], 1)
        for number in range(15, 22):
            state, events = advance_streak(state, day(number))
            self.assertEqual(events, [])
            self.assertEqual(state['streak'], 1)
            self.assertEqual(state['streak_shields'], 0)
        state, events = advance_streak(state, day(22))
        self.assertEqual(state['streak'], 0)

    def test_return_after_shielded_week_and_replenish(self):
        state = self.submit(empty_streak(), 1)
        state = self.submit(state, 5)
        state, events, bonus = record_task(state, day(20))
        self.assertEqual(state['streak'], 2)
        self.assertEqual(state['streak_shields'], 0)
        self.assertEqual(bonus, 1)
        self.assertEqual(events, ['shield_used', 'streak_increased'])
        state = self.submit(state, 20)
        self.assertEqual(state['streak_shields'], 1)

    def test_downtime_cannot_skip_multiple_missed_periods(self):
        state = self.submit(empty_streak(), 1)
        state = self.submit(state, 5)
        state, events, bonus = record_task(state, day(29))
        self.assertEqual(state['streak'], 1)
        self.assertEqual(state[ANCHOR], '2026-10-29')
        self.assertEqual(bonus, 0)
        self.assertEqual(events, ['shield_used', 'streak_reset', 'streak_started'])

    def test_submission_on_expiry_date_starts_fresh(self):
        state = self.submit(empty_streak(), 1)
        state['streak'] = 10
        state, events, bonus = record_task(state, day(15))
        self.assertEqual(state['streak'], 1)
        self.assertEqual(state[ANCHOR], '2026-10-15')
        self.assertEqual(bonus, 0)

    def test_reminders_only_last_two_dates_and_stop_after_task(self):
        state = self.submit(empty_streak(), 1)
        for number in range(1, 13):
            self.assertIsNone(streak_reminder(state, day(number)))
        reminder = streak_reminder(state, day(13))
        self.assertIn('<t:', reminder['message'])
        self.assertIn('/my_streak', reminder['message'])
        state['streak_last_reminder_key'] = reminder['key']
        self.assertIsNone(streak_reminder(state, day(13)))
        self.assertIsNotNone(streak_reminder(state, day(14)))
        state = self.submit(state, 14)
        self.assertIsNone(streak_reminder(state, day(14)))

    def test_shield_reminder_describes_consumption_not_reset(self):
        state = self.submit(empty_streak(), 1)
        state = self.submit(state, 5)
        message = streak_reminder(state, day(13))['message']
        self.assertIn('shield will be used', message)
        self.assertNotIn('streak will reset', message)

    def test_dates_across_month_year_and_leap_day(self):
        cases = [
            (date(2026, 12, 28), date(2027, 1, 4)),
            (date(2028, 2, 23), date(2028, 3, 1)),
        ]
        for start, next_period in cases:
            state, events, bonus = record_task(empty_streak(), start)
            state, events, bonus = record_task(state, next_period)
            self.assertEqual(state['streak'], 2)

    def test_migration_uses_latest_task_and_runs_once(self):
        legacy = {'streak': 6, ANCHOR: '2026-09-20', LAST_TASK: '2026-10-05'}
        state = normalize_streak(legacy, day(10))
        self.assertEqual(state['streak'], 6)
        self.assertEqual(state[ANCHOR], '2026-10-05')
        self.assertEqual(state['streak_shields'], 0)
        state = self.submit(state, 10)
        self.assertEqual(normalize_streak(state, day(11)), state)

    def test_stale_and_placeholder_legacy_records(self):
        state, events = advance_streak({'streak': 6, LAST_TASK: '2026-09-01'}, day(1))
        self.assertEqual(state['streak'], 0)
        state = normalize_streak({'streak': 6, LAST_TASK: '2000-01-01'}, day(1))
        self.assertEqual(state, empty_streak())

    def test_admin_start_reset_and_date_validation(self):
        state = edit_streak_fields(empty_streak(), day(10), {'streak': 4})
        self.assertEqual(state[ANCHOR], '2026-10-10')
        self.assertEqual(state[LAST_TASK], '2026-10-10')
        state = edit_streak_fields(state, day(10), {'streak_shields': 1})
        self.assertEqual(state['streak_shields'], 1)
        for changes in ({'streak': -1}, {'streak_shields': 2}, {LAST_TASK: '2026-10-11'},
                        {ANCHOR: '2026-10-11'}, {'streak_last_processed_period': 0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                edit_streak_fields(state, day(10), changes)
        self.assertEqual(edit_streak_fields(state, day(10), {'streak': 0}), empty_streak())

    def test_admin_can_repair_invalid_new_record(self):
        state = self.submit(empty_streak(), 1)
        state['streak_shields'] = 5
        repaired = edit_streak_fields(state, day(5), {'streak_shields': 1})
        self.assertEqual(repaired['streak_shields'], 1)

    def test_date_edit_rebases_processing_without_creating_extra_shield(self):
        state = self.submit(empty_streak(), 1)
        state = edit_streak_fields(state, day(20), {'last_worksheet_date': '2026-10-20'})
        self.assertEqual(state['streak_last_processed_period'], 1)
        self.assertEqual(state['streak_shields'], 0)
        state, events = advance_streak(state, day(21))
        self.assertEqual(events, [])


if __name__ == '__main__':
    unittest.main()
