from datetime import datetime, timedelta
from urllib.parse import urlencode
import unittest

from features.checkin.client import CheckinError, ENTRY
from features.checkin.scheduler import CheckinScheduler, CheckinTaskError
from features.checkin.teams import CheckinTeams, _parse_team
from recharge.cmcc import SHANGHAI
from tests.test_checkin_scheduler import QueueExecutor


def official_state(token, *, status=0, signed=()):
    link = ENTRY + '?' + urlencode({'groupMarkCode': token})
    share = 'https://h.app.coc.10086.cn/activity/transit/transferDownload.html?' + urlencode({'targetUrl': link})
    return {'status': status, 'baseConfig': {'minMemberNum': 2, 'maxMemberNum': 5, 'totalMarkTimes': 35},
            'shareUrl': share, 'myGroup': {'leader': f'0000-{signed[0]}',
            'members': '|'.join(f'{i:04}-{n}' for i, n in enumerate(signed[1:], 1))} if signed else None}


class CheckinTeamTests(unittest.TestCase):
    def setUp(self):
        self.accounts = [{'id': str(i), 'name': str(i), 'phone_masked': f'138****{i:04}', 'status': 'active'}
                         for i in range(13)]
        self.states = {account['id']: official_state(account['id']) for account in self.accounts}
        self.calls = []
        self.uncertain = False
        self.awards = []
        self.executor = QueueExecutor()
        self.now = datetime(2026, 11, 11, 22, tzinfo=SHANGHAI)
        outer = self

        class Client:
            def __init__(self, provider):
                self.login_id = provider()

            def team_status(self):
                return outer.states[self.login_id]

            def join_team(self, token, *, before_submit):
                outer.assertNotEqual(self.login_id, token)
                before_submit()
                outer.assertTrue(outer.scheduler.busy_login(self.login_id))
                outer.assertEqual(outer.scheduler.data['team_joins'][self.login_id]['state'], 'submitted')
                outer.calls.append((self.login_id, token))
                if outer.uncertain:
                    raise CheckinError('结果待核实', uncertain=True)
                outer.states[self.login_id] = official_state('', status=1, signed=(11, 11))

            def team_award(self, *, before_submit, check_only):
                status = outer.states[self.login_id]['status']
                if status == 3 and not check_only:
                    before_submit()
                    outer.assertTrue(outer.scheduler.data['team_awards'][self.login_id]['submitted'])
                    outer.awards.append(self.login_id)
                    if outer.uncertain:
                        raise CheckinError('结果待核实', uncertain=True)
                    status = 4
                    outer.states[self.login_id] = official_state('', status=4, signed=(18, 17))
                return status, '5元话费券' if status == 4 else ''

        self.scheduler = CheckinScheduler(lambda login_id: login_id, lambda: self.accounts,
                                          client_factory=Client, now=lambda: self.now, executor=self.executor)
        self.addCleanup(self.scheduler.shutdown)
        self.scheduler.data['observations'] = {row['id']: {'date': '20261111', 'month_count': 11}
                                               for row in self.accounts}
        self.teams = CheckinTeams(self.scheduler)

    def test_preview_uses_all_eligible_accounts_and_avoids_early_completion(self):
        plan = self.teams.preview()
        self.assertEqual(sorted(len(team['accounts']) for team in plan['groups']), [3, 3, 3, 4])
        self.assertEqual(self.calls, [])
        self.assertNotIn('groupMarkCode', str(plan))
        accounts = [row['id'] for team in plan['groups'] for row in team['accounts']]
        self.assertCountEqual(accounts, [row['id'] for row in self.accounts])
        result = self.teams.apply(plan['plan_id'])
        self.assertEqual(len(self.calls), 9)
        self.assertTrue(all(member['joined'] for group in result['groups'] for member in group['members']))
        with self.assertRaises(CheckinTaskError):
            self.teams.apply(plan['plan_id'])

    def test_existing_teams_are_excluded_and_uncertain_join_is_not_replayed(self):
        self.accounts = self.accounts[:4]
        self.states['0'] = official_state('', status=3, signed=(18, 17))
        self.uncertain = True
        preview = self.teams.preview()
        self.assertEqual(preview['accounts'][0]['signed'], 35)
        result = self.teams.apply(preview['plan_id'])
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(result['groups'][0]['members'][0]['joined'])
        retried_id = self.calls[0][0]
        self.teams = CheckinTeams(self.scheduler)
        refreshed = self.teams.preview()
        self.assertNotIn(retried_id, [row['id'] for team in refreshed['groups'] for row in team['accounts']])
        self.assertIn('等待官方', next(row['message'] for row in refreshed['accounts'] if row['id'] == retried_id))

    def test_official_invite_wrapper_cannot_point_to_another_origin(self):
        state = official_state('valid')
        self.assertEqual(_parse_team(state)[1], 'valid')
        state['shareUrl'] = state['shareUrl'].replace('wx.10086.cn', 'wrong.example')
        self.assertEqual(_parse_team(state)[1], '')

    def test_lone_account_can_join_an_open_team_through_its_leader(self):
        self.accounts = self.accounts[:3]
        self.states.update({
            '0': official_state('leader-invite', status=1, signed=(11, 11)),
            '1': official_state('member-invite', status=1, signed=(11, 11)),
            '2': official_state(''),
        })
        self.assertEqual(_parse_team(self.states['1'], self.accounts[1]['phone_masked'])[1], '')
        plan = self.teams.preview()
        group, = plan['groups']
        self.assertTrue(group['existing'])
        self.assertEqual([row['id'] for row in group['accounts']], ['0', '2'])
        self.assertNotIn('leader-invite', str(plan))
        self.assertEqual(self.calls, [])
        self.teams.apply(plan['plan_id'])
        self.assertEqual(self.calls, [('2', 'leader-invite')])

    def test_team_that_reaches_goal_before_confirmation_is_skipped(self):
        self.accounts = self.accounts[:2]
        self.states['0'] = official_state('leader-invite', status=1, signed=(8, 8, 8, 8))
        plan = self.teams.preview()
        self.assertTrue(plan['groups'][0]['existing'])
        self.states['0'] = official_state('leader-invite', status=4, signed=(9, 9, 9, 9))
        group, = self.teams.apply(plan['plan_id'])['groups']
        self.assertIn('已达标', group['message'])
        self.assertEqual(self.calls, [])
        plan = self.teams.preview()
        self.assertIsNone(plan['plan_id'])
        self.assertIn('暂无可加入的团队', plan['accounts'][1]['message'])

    def test_daily_awards_only_submit_for_official_ready_accounts(self):
        self.accounts = self.accounts[:3]
        self.states['0'] = official_state('', status=3, signed=(18, 17))
        self.states['1'] = official_state('', status=1, signed=(10, 10))
        self.states['2'] = official_state('', status=4, signed=(18, 17))
        self.assertEqual(self.teams.queue_awards(), 3)
        self.assertEqual(self.teams.queue_awards(), 0)
        self.executor.drain()
        self.assertEqual(self.awards, ['0'])
        self.assertEqual(self.teams.award_status(), {'checking': 0, 'claimed': 2, 'unconfirmed': 0})
        self.assertEqual(self.teams.queue_awards(), 0)

        self.states['1']['status'] = 3
        self.scheduler.data['daily']['1:signin:20261111'] = {
            'state': 'signed', 'submitted': True, 'time': self.now.replace(minute=2).isoformat()}
        self.now = self.now.replace(minute=3)
        self.assertEqual(self.teams.queue_awards(), 1)
        self.executor.drain()
        self.assertEqual(self.awards, ['0', '1'])

    def test_uncertain_award_is_only_read_back_after_restart(self):
        self.accounts = self.accounts[:1]
        self.states['0'] = official_state('', status=3, signed=(18, 17))
        self.uncertain = True
        self.teams.queue_awards()
        self.executor.drain()
        self.assertEqual(self.teams.award_status()['unconfirmed'], 1)
        self.teams = CheckinTeams(self.scheduler)
        self.teams.queue_awards(immediate=True)
        self.executor.drain()
        self.assertEqual(self.awards, ['0'])
        self.now += timedelta(days=1)
        self.states['0']['status'] = 4
        self.teams.queue_awards()
        self.executor.drain()
        self.assertEqual(self.teams.award_status(), {'checking': 0, 'claimed': 1, 'unconfirmed': 0})
        self.assertEqual(self.awards, ['0'])
