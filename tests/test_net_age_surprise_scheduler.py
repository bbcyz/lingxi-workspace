from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Barrier
import unittest
from unittest.mock import Mock, patch

from activities.coc_client import CocError
from login_storage import LoginStorageError
from net_age_surprise_scheduler import END_AT, SCOPE, SurpriseScheduler
from recharge.cmcc import SHANGHAI


class Queue:
    def __init__(self):
        self.jobs = []

    def submit(self, function, *args):
        self.jobs.append((function, args))

    def drain(self):
        while self.jobs:
            function, args = self.jobs.pop(0)
            function(*args)

    def shutdown(self, **_kwargs):
        pass


class Store:
    def __init__(self):
        self.data = {}

    def load_activity_data(self, key):
        return deepcopy(self.data.get(key))

    def save_activity_data(self, key, value):
        self.data[key] = deepcopy(value)


class SurpriseSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 21, 19, 23, tzinfo=SHANGHAI)
        self.pool = [{'id': 'a', 'status': 'active', 'name': 'one'},
                     {'id': 'b', 'status': 'active', 'name': 'two'},
                     {'id': 'c', 'status': 'login_required'}]
        self.storage, self.queue = Store(), Queue()
        self.service = Mock()
        self.service.busy_login.return_value = False
        self.service.surprise_progress.return_value = {}
        self.service.auto_surprise.return_value = {'remaining': 0, 'draws': [], 'prizes': []}
        self.scheduler = self.create()

    def create(self):
        return SurpriseScheduler(self.service, lambda: deepcopy(self.pool), storage=self.storage,
                                 now=lambda: self.now, executor=self.queue)

    def row(self, login_id='a'):
        return next(row for row in self.scheduler.status()['accounts'] if row['id'] == login_id)

    def test_all_active_accounts_are_enabled_without_selection_or_clock_setup(self):
        self.assertEqual(self.scheduler.tick(), 2)
        saved = self.storage.data[SCOPE]
        for login_id in ('a', 'b'):
            self.assertEqual(saved['records'][login_id]['2026-11-21']['state'], 'running')
        self.assertNotIn('c', saved['records'])
        self.queue.drain()
        self.assertEqual({call.args[0] for call in self.service.auto_surprise.call_args_list}, {'a', 'b'})
        self.assertTrue(self.row()['completed_today'])
        self.assertEqual(self.scheduler.status()['start_time'], '00:00')

    def test_zero_quota_and_confirmed_draws_are_completed_and_reused_after_restart(self):
        self.service.auto_surprise.return_value = {
            'remaining': 0, 'draws': [{'name': '流量券', 'confirmed': True}], 'prizes': [{'name': '流量券'}]}
        self.scheduler.tick()
        self.queue.drain()
        self.scheduler = self.create()
        self.assertEqual(self.scheduler.tick(), 0)
        self.queue.drain()
        self.assertEqual(self.service.auto_surprise.call_count, 2)
        self.assertEqual(self.row()['prizes'], [{'name': '流量券'}])
        self.assertTrue(self.row()['completed_today'])

    def test_later_failure_preserves_confirmed_prizes_and_manual_rechecks_keep_them(self):
        progress={'remaining':1,'prizes':[],
                  'draws':[{'record_id':'r1','name':'测试实物','confirmed':True,'needs_address':True}]}
        self.service.surprise_progress.return_value=deepcopy(progress)
        self.service.auto_surprise.side_effect=CocError('后续抽奖频繁','SURPRISE_RATE_LIMITED',429)
        self.scheduler.run('a');self.queue.drain()
        self.assertEqual(self.row()['state'],'failed')
        self.assertEqual(self.row()['draws'],progress['draws'])
        self.assertTrue(self.row()['draws'][0]['needs_address'])
        self.service.auto_surprise.side_effect=None
        self.service.surprise_progress.return_value={**progress,'remaining':0}
        self.scheduler.run('a');self.queue.drain()
        self.assertTrue(self.row()['completed_today'])
        self.assertEqual(self.row()['draws'],progress['draws'])

    def test_new_accounts_and_late_login_catch_up_on_the_same_day(self):
        self.scheduler.tick()
        self.queue.drain()
        self.pool.append({'id': 'd', 'status': 'active'})
        self.pool[2]['status'] = 'active'
        self.now = self.now.replace(hour=23, minute=59)
        self.assertEqual(self.scheduler.tick(), 2)
        self.queue.drain()
        self.assertEqual([call.args[0] for call in self.service.auto_surprise.call_args_list], ['a', 'b', 'c', 'd'])

    def test_busy_verification_defers_without_spending_the_daily_attempt(self):
        self.service.busy_login.side_effect = lambda login_id: login_id == 'a'
        self.scheduler.tick()
        self.queue.drain()
        self.assertNotIn('a', self.scheduler.data['records'])
        self.service.busy_login.side_effect = None
        self.service.busy_login.return_value = False
        self.assertEqual(self.scheduler.tick(), 1)
        self.queue.drain()
        self.assertEqual([call.args[0] for call in self.service.auto_surprise.call_args_list], ['b', 'a'])

    def test_busy_or_disabled_after_queue_never_calls_the_service_and_can_resume(self):
        self.pool = self.pool[:1]
        self.scheduler.tick()
        self.service.busy_login.return_value = True
        self.queue.drain()
        self.service.auto_surprise.assert_not_called()
        self.assertEqual(self.row()['state'], 'deferred')
        self.service.busy_login.return_value = False
        self.scheduler.tick()
        enabled = [False]
        self.scheduler.feature_enabled = lambda: enabled[0]
        self.queue.drain()
        self.service.auto_surprise.assert_not_called()
        self.assertEqual(self.scheduler.tick(), 0)
        enabled[0] = True
        self.scheduler.tick()
        self.queue.drain()
        self.service.auto_surprise.assert_called_once_with('a')

    def test_failed_attempt_is_not_completed_or_automatically_retried_after_restart(self):
        self.service.auto_surprise.side_effect = CocError('官方暂未响应', 'SURPRISE_NETWORK')
        self.scheduler.tick()
        self.queue.drain()
        self.assertFalse(self.row()['completed_today'])
        self.assertEqual(self.row()['state'], 'failed')
        self.scheduler = self.create()
        self.now += timedelta(hours=1)
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(self.service.auto_surprise.call_count, 2)
        self.now += timedelta(days=1)
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(self.service.auto_surprise.call_count, 4)

    def test_busy_error_from_service_defers_without_spending_an_official_attempt(self):
        self.pool = self.pool[:1]
        self.service.auto_surprise.side_effect = CocError('该账号正在验证权益', 'ACCOUNT_BUSY', 409)
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(self.row()['state'], 'deferred')
        self.assertEqual(self.row()['record']['attempt_count'], 0)
        self.assertFalse(self.row()['completed_today'])
        self.scheduler = self.create()
        self.service.auto_surprise.side_effect = None
        self.assertEqual(self.scheduler.tick(), 1)
        self.queue.drain()
        self.assertTrue(self.row()['completed_today'])
        self.assertEqual(self.row()['record']['attempt_count'], 1)

    def test_protected_app_refresh_defers_but_real_refresh_failure_does_not_loop(self):
        self.pool = self.pool[:1]
        self.service.auto_surprise.side_effect = CocError(
            '该账号正在用券或短信验证中，请完成后再恢复 APP 会话。', 'APP_REFRESH_FAILED')
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(self.row()['state'], 'deferred')
        self.assertEqual(self.row()['record']['attempt_count'], 0)
        self.service.auto_surprise.side_effect = CocError('APP 自动续期暂未完成', 'APP_REFRESH_FAILED')
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(self.row()['state'], 'failed')
        self.assertEqual(self.row()['record']['attempt_count'], 1)
        self.assertEqual(self.scheduler.tick(), 0)

    def test_manual_run_allows_existing_login_required_account_with_cached_business_session(self):
        self.pool = [{'id': 'a', 'status': 'login_required'}]
        self.assertEqual(self.scheduler.tick(), 0)
        self.assertTrue(self.scheduler.run('a')['queued'])
        self.scheduler.status()
        self.service.auto_surprise.assert_not_called()
        self.queue.drain()
        self.service.auto_surprise.assert_called_once_with('a')
        self.assertTrue(self.row()['completed_today'])

    def test_uncertain_busy_error_remains_blocked_instead_of_being_deferred(self):
        self.pool = self.pool[:1]
        self.service.auto_surprise.side_effect = CocError('结果不确定', 'ACCOUNT_BUSY', 409, uncertain=True)
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(self.row()['failure_kind'], 'uncertain')
        self.assertEqual(self.row()['record']['attempt_count'], 1)
        self.assertEqual(self.scheduler.tick(), 0)

    def test_restored_login_retries_authentication_once_and_preserves_budget_after_restart(self):
        self.pool = self.pool[:1]
        self.service.auto_surprise.side_effect = CocError('请重新登录', 'APP_LOGIN_REQUIRED', 401)
        self.scheduler.tick()
        self.queue.drain()
        self.scheduler.login_restored('a')
        self.queue.drain()
        self.assertEqual(self.service.auto_surprise.call_count, 2)
        self.assertTrue(self.row()['record']['auth_retry_used'])
        self.scheduler = self.create()
        self.scheduler.login_restored('a')
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(self.service.auto_surprise.call_count, 2)

    def test_active_transition_after_login_failure_is_a_durable_restore_signal(self):
        self.pool = self.pool[:1]
        self.service.auto_surprise.side_effect = CocError('会话已失效', 'SURPRISE_SESSION_EXPIRED', 401)
        self.scheduler.tick()
        self.queue.drain()
        self.pool[0]['status'] = 'login_required'
        self.scheduler.tick()
        self.scheduler = self.create()
        self.pool[0]['status'] = 'active'
        self.service.auto_surprise.side_effect = None
        self.assertEqual(self.scheduler.tick(), 1)
        self.queue.drain()
        self.assertTrue(self.row()['completed_today'])
        self.assertEqual(self.service.auto_surprise.call_count, 2)

    def test_login_notification_while_running_continues_after_auth_failure(self):
        self.pool = self.pool[:1]
        calls = []

        def execute(login_id):
            calls.append(login_id)
            if len(calls) == 1:
                self.assertFalse(self.scheduler.login_restored(login_id))
                raise CocError('原会话已失效', 'SURPRISE_SESSION_EXPIRED', 401)
            return {'remaining': 0, 'draws': [], 'prizes': []}

        self.service.auto_surprise.side_effect = execute
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(calls, ['a', 'a'])
        self.assertTrue(self.row()['completed_today'])

    def test_restored_login_never_overrides_terminal_or_uncertain_failures(self):
        self.pool = self.pool[:1]
        errors = [CocError('不符合活动资格', 'NOT_ELIGIBLE', 401),
                  CocError('请求频繁', '-800', 401),
                  CocError('请完成官方滑块', 'SLIDER_REQUIRED', 401),
                  CocError('结果未知', 'APP_LOGIN_REQUIRED', 401, uncertain=True),
                  CocError('连接暂不可用', 'SURPRISE_NETWORK')]
        for error in errors:
            self.storage, self.queue = Store(), Queue()
            self.scheduler = self.create()
            self.service.auto_surprise.reset_mock()
            self.service.auto_surprise.side_effect = error
            self.scheduler.tick()
            self.queue.drain()
            self.scheduler = self.create()
            self.scheduler.login_restored('a')
            self.pool[0]['credential_revision'] = self.pool[0].get('credential_revision', 0) + 1
            self.scheduler.tick()
            self.queue.drain()
            self.assertEqual(self.service.auto_surprise.call_count, 1)
            self.assertFalse(self.row()['completed_today'])

    def test_interrupted_running_record_restores_as_uncertain_without_replay(self):
        self.pool = self.pool[:1]
        self.scheduler.tick()  # Simulate stopping before the queued worker finishes.
        self.queue = Queue()
        self.scheduler = self.create()
        self.assertEqual(self.row()['failure_kind'], 'uncertain')
        self.assertTrue(self.row()['uncertain'])
        self.scheduler.tick()
        self.scheduler.login_restored('a')
        self.queue.drain()
        self.service.auto_surprise.assert_not_called()

    def test_manual_run_can_request_fresh_check_after_completion_or_failure(self):
        self.pool = self.pool[:1]
        self.scheduler.tick()
        self.queue.drain()
        self.assertTrue(self.scheduler.run('a')['queued'])
        self.queue.drain()
        self.service.auto_surprise.side_effect = CocError('待核对', 'SURPRISE_PENDING', uncertain=True)
        self.scheduler.run('a')
        self.queue.drain()
        self.service.auto_surprise.side_effect = None
        self.scheduler.run('a')
        self.queue.drain()
        self.assertEqual(self.service.auto_surprise.call_count, 4)
        self.assertTrue(self.row()['completed_today'])

    def test_parallel_manual_queues_merge_and_status_never_queues_or_queries(self):
        self.pool = self.pool[:1]
        barrier = Barrier(6)

        def queue_once():
            barrier.wait(5)
            return self.scheduler.run('a')['queued']

        with ThreadPoolExecutor(max_workers=6) as executor:
            self.assertEqual(sum(executor.map(lambda _: queue_once(), range(6))), 1)
        self.assertEqual(len(self.queue.jobs), 1)
        for _ in range(5):
            self.scheduler.status()
        self.assertEqual(len(self.queue.jobs), 1)
        self.service.auto_surprise.assert_not_called()
        self.queue.drain()
        self.service.auto_surprise.assert_called_once_with('a')

    def test_positive_remainder_or_invalid_result_stays_failed_without_retry(self):
        self.pool = self.pool[:1]
        self.service.auto_surprise.return_value = {'remaining': 2, 'draws': [{'confirmed': True}], 'prizes': []}
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(self.row()['failure_kind'], 'remaining')
        self.assertEqual(self.row()['remaining'], 2)
        self.scheduler.tick()
        self.assertEqual(self.service.auto_surprise.call_count, 1)
        self.service.auto_surprise.return_value = {'remaining': None, 'draws': [], 'prizes': []}
        self.scheduler.run('a')
        self.queue.drain()
        self.assertEqual(self.row()['failure_kind'], 'uncertain')
        self.assertFalse(self.row()['completed_today'])

    def test_beijing_day_boundary_and_confirmed_campaign_end(self):
        self.pool = self.pool[:1]
        self.now = datetime(2026, 11, 21, 15, 59, tzinfo=timezone.utc)
        self.scheduler.tick()
        self.queue.drain()
        self.now += timedelta(minutes=2)
        self.scheduler.tick()
        self.queue.drain()
        self.assertEqual(set(self.scheduler.data['records']['a']), {'2026-11-21', '2026-11-22'})
        self.now = END_AT
        self.scheduler.tick()
        self.queue.drain()
        self.now += timedelta(seconds=1)
        self.assertFalse(self.scheduler.status()['active'])
        self.assertEqual(self.scheduler.tick(), 0)
        with self.assertRaises(CocError) as raised:
            self.scheduler.run('a')
        self.assertEqual(raised.exception.code, 'ACTIVITY_CLOSED')

    def test_feature_lease_or_disabled_switch_stops_new_work_without_deleting_records(self):
        self.pool = self.pool[:1]
        self.scheduler.feature_enabled = lambda: False
        self.assertEqual(self.scheduler.tick(), 0)
        self.assertEqual(self.scheduler.status()['accounts'][0]['state'], 'pending')
        with self.assertRaises(CocError):
            self.scheduler.run('a')
        self.scheduler.feature_enabled = lambda: True
        with patch('net_age_surprise_scheduler.feature_running', return_value=False):
            self.assertEqual(self.scheduler.tick(), 0)
        self.scheduler.tick()
        self.queue.drain()
        saved = deepcopy(self.storage.data)
        self.scheduler.feature_enabled = lambda: False
        self.scheduler.tick()
        self.assertEqual(self.storage.data, saved)
        self.assertTrue(self.row()['completed_today'])

    def test_storage_failure_before_queue_prevents_service_execution(self):
        self.storage.save_activity_data = Mock(side_effect=LoginStorageError('不能保存任务记录'))
        with self.assertRaises(LoginStorageError):
            self.scheduler.tick()
        self.assertEqual(self.queue.jobs, [])
        self.service.auto_surprise.assert_not_called()
        self.assertEqual(self.scheduler.tick(), 0)
        self.assertIn('不能保存', self.scheduler.status()['warning'])


if __name__ == '__main__':
    unittest.main()
