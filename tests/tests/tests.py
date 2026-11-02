import io
import tempfile
from unittest import mock

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.test import TestCase, override_settings, skipUnlessDBFeature

import database_locks


@skipUnlessDBFeature('has_select_for_update')
@override_settings(DATABASE_LOCKS_ENABLED=True)
class Test(TestCase):
    def setUp(self):
        self._thread_patch = mock.patch('database_locks.locks.RenewThread.start')
        self._thread_mock = self._thread_patch.start()
        self._lock_patch = mock.patch('database_locks.locks.DBLock')
        self._lock_mock = self._lock_patch.start()
        self._sleep_patch = mock.patch('time.sleep')
        self._sleep_mock = self._sleep_patch.start()

    def tearDown(self):
        self._thread_patch.stop()
        self._lock_patch.stop()
        self._sleep_patch.stop()

    @mock.patch('django.db.connection.features')
    def test_lock_not_supported(self, feat_mock):
        feat_mock.has_select_for_update = False
        with self.assertLogs(logger='database_locks', level='ERROR') as cm:
            with database_locks.lock('not_locked', timeout=2):
                self._lock_mock.assert_not_called()
        self.assertEqual(
            cm.output,
            [
                'ERROR:database_locks.locks:database_locks cannot be used with the current database engine as it does not '
                'support SELECT .. FOR UPDATE, proceed at your own risk'
            ],
        )

    def test_lock(self):
        with self.assertLogs(logger='database_locks', level='INFO') as cm:
            with database_locks.lock('testing'):
                self._lock_mock.assert_called_once_with('testing', locked_by=None)
                self._lock_mock.return_value.acquire.assert_called_once_with(
                    lock_ttl=10
                )
                self._lock_mock.return_value.release.assert_not_called()
        self._lock_mock.return_value.release.assert_called_once_with()
        self.assertEqual(
            cm.output, ['INFO:database_locks.locks:acquiring lock testing']
        )
        self._thread_mock.assert_called_with()

    def test_lock_fail_and_retry(self):
        self._lock_mock.return_value.acquire.side_effect = [False, True]
        with database_locks.lock('testing'):
            self._lock_mock.assert_called_once_with('testing', locked_by=None)
            self.assertEqual(
                self._lock_mock.return_value.acquire.mock_calls,
                [mock.call(lock_ttl=10), mock.call(lock_ttl=10)],
            )
            self._lock_mock.return_value.release.assert_not_called()
        self._lock_mock.return_value.release.assert_called_once_with()

    def test_lock_fail_but_no_retry(self):
        self._lock_mock.return_value.acquire.return_value = False
        with self.assertRaisesMessage(
            database_locks.locks.LockException, 'failed to acquire lock'
        ):
            with database_locks.lock('testing', retry=False):
                pass
        self._lock_mock.assert_called_once_with('testing', locked_by=None)
        self._lock_mock.return_value.acquire.assert_called_once_with(lock_ttl=10)
        self._lock_mock.return_value.release.assert_not_called()

    def test_lock_acquire_db_error_retried(self):
        # a transient db.Error (e.g. Galera certification failure / deadlock 1213) must not crash
        # the process, it should be treated like a failed acquire and retried
        self._lock_mock.return_value.acquire.side_effect = [
            database_locks.locks.db.Error('deadlock'),
            True,
        ]
        with mock.patch('database_locks.locks.db.close_old_connections') as close_mock:
            with database_locks.lock('testing'):
                pass
        close_mock.assert_called_once_with()
        self._lock_mock.return_value.release.assert_called_once_with()

    def test_lock_releases_on_exception(self):
        # bug 3: an exception inside the `with lock(...)` body must still release the lock
        with self.assertRaises(ValueError):
            with database_locks.lock('testing'):
                raise ValueError('boom')
        self._lock_mock.return_value.release.assert_called_once_with()

    def test_lock_decorator(self):
        @database_locks.locked
        def locked_func():
            self._lock_mock.assert_called_once_with(
                'tests.tests.locked_func', locked_by=None
            )
            self._lock_mock.return_value.acquire.assert_called_once_with(lock_ttl=10)
            self._lock_mock.return_value.release.assert_not_called()
            self._thread_mock.assert_called_with()

        self._lock_mock.assert_not_called()
        locked_func()
        self._lock_mock.return_value.release.assert_called_once_with()

    def test_lock_decorator_empty_parameters(self):
        @database_locks.locked()
        def locked_func():
            self._lock_mock.assert_called_once_with(
                'tests.tests.locked_func', locked_by=None
            )
            self._lock_mock.return_value.acquire.assert_called_once_with(lock_ttl=10)
            self._lock_mock.return_value.release.assert_not_called()
            self._thread_mock.assert_called_with()

        self._lock_mock.assert_not_called()
        locked_func()
        self._lock_mock.return_value.release.assert_called_once_with()

    def test_lock_decorator_with_parameters(self):
        @database_locks.locked(
            'decorator_test', timeout=2, auto_renew=False, lock_ttl=1
        )
        def locked_func():
            self._lock_mock.assert_called_once_with('decorator_test', locked_by=None)
            self._lock_mock.return_value.acquire.assert_called_once_with(lock_ttl=1)
            self._lock_mock.return_value.release.assert_not_called()
            # auto_renew disabled, no thread
            self._thread_mock.assert_not_called()

        self._lock_mock.assert_not_called()
        locked_func()
        self._lock_mock.return_value.release.assert_called_once_with()

    def test_command_decorator(self):
        class BasicCommand(BaseCommand):
            def handle(inner_self, *a, **b):
                return inner_self._handle(*a, **b)

        @database_locks.locked
        class TestCommand(BasicCommand):
            def handle(inner_self, arg):
                self._lock_mock.assert_called_once_with(
                    'tests.tests.TestCommand', locked_by=None
                )
                self._lock_mock.return_value.acquire.assert_called_once_with(
                    lock_ttl=10
                )
                self._lock_mock.return_value.release.assert_not_called()
                # auto_renew disabled, no thread
                self._thread_mock.assert_called()
                return arg + 1

        self._lock_mock.assert_not_called()
        self.assertEqual(TestCommand().handle(1), 2)
        self._lock_mock.return_value.release.assert_called_with()

    def test_command_decorator_fail(self):
        with self.assertRaisesMessage(
            NotImplementedError,
            'only django BaseCommand subclasses are supported for now',
        ):

            @database_locks.locked
            class TestCommand(object):
                def handle(inner_self, arg):
                    self.fail('nothing will get here')

    def test_status_file(self):
        _, f = tempfile.mkstemp()
        with override_settings(DATABASE_LOCKS_STATUS_FILE=f):
            database_locks.locks._status_file('1')
        with open(f) as fd:
            self.assertEqual(fd.read(), '1')

        # feed a directory for error
        f = tempfile.mkdtemp()
        with self.assertLogs('database_locks', level='ERROR') as log:
            with override_settings(DATABASE_LOCKS_STATUS_FILE=f):
                database_locks.locks._status_file('1')
        self.assertIn(
            'ERROR:database_locks.locks:failed to update lock status file',
            log.output[0],
        )

    def test_test_command(self):
        out = io.StringIO()
        call_command('db_lock_it', 'xxx', duration=3456, owner='oona', stdout=out)
        self._lock_mock.assert_called_once_with('xxx', locked_by='oona')
        self._sleep_mock.assert_called_once_with(3456)
        self.assertEqual(
            out.getvalue(), 'Got the lock, sleeping 3456 seconds\nReleasing lock\n'
        )


@skipUnlessDBFeature('has_select_for_update')
@override_settings(DATABASE_LOCKS_ENABLED=True)
class TestRenewThread(TestCase):
    def setUp(self):
        self._sleep_patch = mock.patch('time.sleep')
        self._sleep_mock = self._sleep_patch.start()

    def tearDown(self):
        self._sleep_patch.stop()

    @mock.patch('database_locks.locks.DBLock')
    def test_renew_thread(self, lock_mock):
        ml = lock_mock.return_value
        with mock.patch('database_locks.locks.RenewThread') as rt_mock:
            with database_locks.lock('testing'):
                rt_mock.assert_called_once_with(ml, 10, 2, max_failures=1)
                rt_mock.return_value.start.assert_called_once_with()
        ml.release.assert_called_with()

        # same __init__ call as asserted before
        rt = database_locks.locks.RenewThread(ml, 10, 2)
        self.assertFalse(rt._RenewThread__stopped.is_set())
        self.assertTrue(rt.stop())
        self.assertTrue(rt._RenewThread__stopped.is_set())

        ml.reset_mock()
        rt.run()
        ml.acquire.assert_called_once_with(lock_ttl=10)

    @override_settings(
        DATABASE_LOCKS_DEFAULT_TTL=20, DATABASE_LOCKS_DEFAULT_TTL_RENEW=5
    )
    @mock.patch('database_locks.locks.DBLock')
    def test_renew_thread_change_defaults(self, lock_mock):
        ml = lock_mock.return_value
        with mock.patch('database_locks.locks.RenewThread') as rt_mock:
            with database_locks.lock('testing'):
                rt_mock.assert_called_once_with(ml, 20, 5, max_failures=1)
                rt_mock.return_value.start.assert_called_once_with()
        ml.release.assert_called_with()

        # same __init__ call as asserted before
        rt = database_locks.locks.RenewThread(ml, 20, 5)
        self.assertFalse(rt._RenewThread__stopped.is_set())
        self.assertTrue(rt.stop())
        self.assertTrue(rt._RenewThread__stopped.is_set())

        ml.reset_mock()
        rt.run()
        ml.acquire.assert_called_once_with(lock_ttl=20)

    def test_renew_thread_fail(self):
        ml = mock.MagicMock()
        # "name" property is a MagicMock constructor, so it has to go like this...
        type(ml).name = 'test_lock'
        ml.acquire.return_value = False
        rt = database_locks.locks.RenewThread(ml, 10, 2)

        # in case test fails, no need for infinite loop
        self.assertFalse(rt._RenewThread__stopped.is_set())
        self.assertTrue(rt.stop())
        self.assertTrue(rt._RenewThread__stopped.is_set())

        with self.assertLogs('database_locks', level='ERROR') as log:
            with mock.patch('os.kill') as kill_mock:
                rt.run()
        kill_mock.assert_called_once()
        self.assertEqual(
            log.output,
            ['ERROR:database_locks.locks:lock test_lock taken by someone else'],
        )

    def test_renew_thread_exception(self):
        ml = mock.MagicMock()
        # "name" property is a MagicMock constructor, so it has to go like this...
        type(ml).name = 'test_lock'
        ml.acquire.side_effect = Exception('wtv happens happens')
        rt = database_locks.locks.RenewThread(ml, 10, 1)

        # in case test fails, no need for infinite loop
        self.assertFalse(rt._RenewThread__stopped.is_set())
        self.assertTrue(rt.stop())
        self.assertTrue(rt._RenewThread__stopped.is_set())

        with self.assertLogs('database_locks', level='ERROR') as log:
            with mock.patch('os.kill') as kill_mock:
                rt.run()
        kill_mock.assert_called_once()
        self.assertIn(
            'ERROR:database_locks.locks:error re-acquiring lock test_lock',
            log.output[0],
        )

    def test_renew_thread_tolerates_failures_below_max(self):
        # bug 1: an exception re-acquiring should be tolerated up to max_failures, not kill
        # the process on the first failure
        ml = mock.MagicMock()
        type(ml).name = 'test_lock'
        rt = database_locks.locks.RenewThread(ml, 10, 2, max_failures=2)

        with mock.patch('os.kill') as kill_mock, mock.patch(
            'database_locks.locks.db.close_old_connections'
        ) as close_mock:
            ml.acquire.side_effect = Exception('blip')
            rt.renew()
            kill_mock.assert_not_called()
            close_mock.assert_called_once_with()
            self.assertEqual(rt._RenewThread__failures, 1)

            ml.acquire.side_effect = None
            ml.acquire.return_value = True
            rt.renew()
            kill_mock.assert_not_called()
            self.assertEqual(rt._RenewThread__failures, 0)

    def test_renew_thread_gives_up_after_max_failures(self):
        ml = mock.MagicMock()
        type(ml).name = 'test_lock'
        ml.acquire.side_effect = Exception('still down')
        rt = database_locks.locks.RenewThread(ml, 10, 2, max_failures=2)

        with mock.patch('os.kill') as kill_mock:
            rt.renew()
            kill_mock.assert_not_called()
            rt.renew()
            kill_mock.assert_called_once()

    def test_renew_thread_lost_to_other_owner_gives_up_immediately(self):
        # losing the lock to a genuine new owner must never be tolerated, regardless of max_failures
        ml = mock.MagicMock()
        type(ml).name = 'test_lock'
        ml.acquire.return_value = False
        rt = database_locks.locks.RenewThread(ml, 10, 2, max_failures=5)

        with mock.patch('os.kill') as kill_mock:
            rt.renew()
            kill_mock.assert_called_once()


@skipUnlessDBFeature('has_select_for_update')
@override_settings(DATABASE_LOCKS_ENABLED=True)
class TestLock(TestCase):
    def test_multiple_locks(self):
        l1 = database_locks.locks.DBLock('x1')
        l2 = database_locks.locks.DBLock('x2')
        with self.assertLogs('database_locks', level='DEBUG') as logs:
            self.assertTrue(l1.acquire())
        self.assertEqual(
            logs.output,
            [
                'DEBUG:database_locks.locks:lock x1 not yet created, trying to create (and acquire)',
                'DEBUG:database_locks.locks:lock x1 (created and) acquired',
            ],
        )

        with self.assertLogs('database_locks', level='DEBUG') as logs:
            self.assertTrue(l2.acquire())
        self.assertEqual(
            logs.output,
            [
                'DEBUG:database_locks.locks:lock x2 not yet created, trying to create (and acquire)',
                'DEBUG:database_locks.locks:lock x2 (created and) acquired',
            ],
        )

    def test_locked_lock_same_owner(self):
        l1 = database_locks.locks.DBLock('x1')
        l2 = database_locks.locks.DBLock('x1')
        with self.assertLogs('database_locks', level='DEBUG') as logs:
            self.assertTrue(l1.acquire())
        self.assertEqual(
            logs.output,
            [
                'DEBUG:database_locks.locks:lock x1 not yet created, trying to create (and acquire)',
                'DEBUG:database_locks.locks:lock x1 (created and) acquired',
            ],
        )

        with self.assertLogs('database_locks', level='DEBUG') as logs:
            self.assertTrue(l2.acquire())
        self.assertEqual(
            logs.output, ['DEBUG:database_locks.locks:lock x1 acquired/renewed']
        )

    def test_locked_lock_diff_owner(self):
        l1 = database_locks.locks.DBLock('x1', locked_by='dibs')
        l2 = database_locks.locks.DBLock('x1')
        with self.assertLogs('database_locks', level='DEBUG') as logs:
            self.assertTrue(l1.acquire())
        self.assertEqual(
            logs.output,
            [
                'DEBUG:database_locks.locks:lock x1 not yet created, trying to create (and acquire)',
                'DEBUG:database_locks.locks:lock x1 (created and) acquired',
            ],
        )

        with self.assertLogs('database_locks', level='DEBUG') as logs:
            self.assertFalse(l2.acquire())
        self.assertEqual(
            logs.output,
            ['DEBUG:database_locks.locks:lock x1 active and owned by dibs, try later'],
        )

    def test_locked_lock_diff_owner_with_release(self):
        l1 = database_locks.locks.DBLock('x1', locked_by='dibs')
        l2 = database_locks.locks.DBLock('x1')
        with self.assertLogs('database_locks', level='DEBUG') as logs:
            self.assertTrue(l1.acquire())
            l1.release()
        self.assertEqual(
            logs.output,
            [
                'DEBUG:database_locks.locks:lock x1 not yet created, trying to create (and acquire)',
                'DEBUG:database_locks.locks:lock x1 (created and) acquired',
                'DEBUG:database_locks.locks:releasing lock x1',
                'DEBUG:database_locks.locks:released lock x1',
            ],
        )

        with self.assertLogs('database_locks', level='DEBUG') as logs:
            self.assertTrue(l2.acquire())
        self.assertEqual(
            logs.output, ['DEBUG:database_locks.locks:lock x1 acquired/renewed']
        )

    def test_expired_lock(self):
        l1 = database_locks.locks.DBLock('x1', locked_by='dibs')
        l2 = database_locks.locks.DBLock('x1')
        with self.assertLogs('database_locks', level='DEBUG') as logs:
            # negative TTL to expire immediately :smart-thinking:
            self.assertTrue(l1.acquire(lock_ttl=-1))
        self.assertEqual(
            logs.output,
            [
                'DEBUG:database_locks.locks:lock x1 not yet created, trying to create (and acquire)',
                'DEBUG:database_locks.locks:lock x1 (created and) acquired',
            ],
        )

        with self.assertLogs('database_locks', level='DEBUG') as logs:
            self.assertTrue(l2.acquire())
        self.assertEqual(
            logs.output, ['DEBUG:database_locks.locks:lock x1 acquired/renewed']
        )

    def test_fencing_stolen_lock_rejected(self):
        # bug 5: a race between reading the row (past its expiry, so eligible to steal) and
        # writing our own ownership, where another process wins that race in between - the
        # fenced UPDATE (WHERE locked_by=<owner we read>) must reject our write instead of
        # silently overwriting the new owner. Simulated here by racing the update itself, since
        # select_for_update already prevents this within a single DB/single-writer setup.
        l1 = database_locks.locks.DBLock('x1', locked_by='dibs')
        self.assertTrue(l1.acquire(lock_ttl=-1))  # expire immediately, eligible to steal

        Lock = database_locks.locks.apps.get_model('database_locks', 'Lock')
        real_filter = Lock.objects.filter

        def racing_filter(*args, **kwargs):
            # right before our own fenced UPDATE runs, someone else grabs the row first
            if kwargs.get('locked_by') == 'dibs':
                Lock.objects.filter(name='x1').update(locked_by='thief')
            return real_filter(*args, **kwargs)

        with mock.patch.object(Lock.objects, 'filter', side_effect=racing_filter):
            with self.assertLogs('database_locks', level='DEBUG') as logs:
                self.assertFalse(l1.acquire())
        self.assertIn(
            'DEBUG:database_locks.locks:lock x1 stolen from under us, try next time',
            logs.output,
        )
