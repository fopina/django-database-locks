import inspect
import logging
import threading
import platform
import os
import time
import signal
from contextlib import contextmanager
from functools import wraps

from django.conf import settings
from django import db
from django.apps import apps
from django.utils import timezone
from django.db.models import BooleanField, Case, Value, When
from django.db.models.functions import Now
from django.core.management.base import BaseCommand


logger = logging.getLogger(__name__)
NOTSET = object()


@contextmanager
def lock(
    lock_name,
    timeout=0,
    lock_ttl=NOTSET,
    locked_by=None,
    auto_renew=True,
    retry=0.5,
    lost_lock_cb=None,
    lock_ttl_renew=NOTSET,
    max_failures=1,
):
    """
    :param lock_name: unique name in DB for this function
    :param timeout: numbers of seconds to wait to acquire lock
    :param lock_ttl: expiration timer of the lock, in seconds (set to None to infinite)
    :param locked_by: owner id for the lock (if lock is active but owner is the same, returns acquired)
    :param auto_renew: if set to True will re-acquire lock (for `lock_ttl` seconds) before `lock_ttl` is over.
                       auto_renew thread will raise LockException (via SIGUSR1) on the main thread in case
                       re-acquiring keeps failing (see `max_failures`)
    :param retry: retry every `retry` seconds acquiring until successful. set to None or 0 to disable.
    :param lost_lock_cb: callback function when lock is lost (when re-acquiring). defaults to raising LockException
    :param lock_ttl_renew: number of seconds to renew before lock_ttl expires
    :param max_failures: number of consecutive renew failures (DB unreachable) tolerated before giving up the
                          lock. A renewal that fails because someone else genuinely owns the lock always gives
                          up immediately, regardless of this value. Keep
                          ``max_failures * (lock_ttl - lock_ttl_renew) < lock_ttl`` (with margin) so the total
                          tolerated outage still fits inside the lease.
    :return:
    """
    # TODO migrate to contextlib.ContextDecorator once only py3 is used
    _status_file('0')

    if not settings.DATABASE_LOCKS_ENABLED:
        logger.warning(
            'database_locks currently disabled in settings, adjust DATABASE_LOCKS_ENABLED if not intended'
        )
        yield
        return

    if not db.connection.features.has_select_for_update:
        logger.error(
            'database_locks cannot be used with the current database engine as it does not support SELECT .. FOR UPDATE, '
            'proceed at your own risk'
        )
        yield
        return

    if lock_ttl is NOTSET:
        lock_ttl = settings.DATABASE_LOCKS_DEFAULT_TTL
    if lock_ttl_renew is NOTSET:
        lock_ttl_renew = settings.DATABASE_LOCKS_DEFAULT_TTL_RENEW

    if lock_ttl is not None and max_failures * (lock_ttl - lock_ttl_renew) >= lock_ttl:
        logger.warning(
            'max_failures * (lock_ttl - lock_ttl_renew) should be < lock_ttl (with margin) so the '
            'tolerated outage fits inside the lease - got max_failures=%s, lock_ttl=%s, lock_ttl_renew=%s',
            max_failures,
            lock_ttl,
            lock_ttl_renew,
        )

    logger.info('acquiring lock %s' % lock_name)
    lock = DBLock(lock_name, locked_by=locked_by)

    _status_file('1')

    time_started = time.time()
    while True:
        try:
            if lock.acquire(lock_ttl=lock_ttl):
                break
        except db.Error:
            # DB unreachable/certification failure (e.g. Galera 1213) - retry like any other failed acquire
            logger.exception('error acquiring lock %s, will retry', lock_name)
            db.close_old_connections()
        if not retry:
            raise LockException('failed to acquire lock')
        if 0 < timeout < time.time() - time_started:
            raise LockException('failed to acquire lock within timeout', timeout)
        time.sleep(retry)

    # set SIGUSR1 handler for lost lock exception
    signal.signal(signal.SIGUSR1, lost_lock_cb or __default_lost_lock_cb)

    renew_thread = None
    if auto_renew:
        renew_thread = RenewThread(lock, lock_ttl, lock_ttl_renew, max_failures=max_failures)
        renew_thread.start()

    _status_file('2')
    try:
        yield
    finally:
        if renew_thread:
            renew_thread.stop()
        lock.release()


def locked(func_or_name=None, **lock_kwargs):
    """
    Decorator to apply the `lock()` context manager to a function or class

    :param func_or_name: decorated function/class - used as lock name
    :param lock_kwargs: passed directly to `lock()`, refer to its documentation
    :return: decorated function/class
    """

    def decorator(func):
        if func_or_name and func_or_name != func:
            name = func_or_name
        else:
            # TODO classes inside the same module with same function names will get same default name...
            name = '{}.{}'.format(func.__module__, func.__name__)

        if inspect.isclass(func):
            if not issubclass(func, BaseCommand):
                raise NotImplementedError(
                    'only django BaseCommand subclasses are supported for now'
                )

            orig_handle = func.handle

            @wraps(func.handle)
            def new_handle(self, *args, **kwargs):
                with lock(name, **lock_kwargs):
                    return orig_handle(self, *args, **kwargs)

            func.handle = new_handle

            return func
        else:

            @wraps(func)
            def wrapper(*args, **kwds):
                with lock(name, **lock_kwargs):
                    return func(*args, **kwds)

            return wrapper

    if func_or_name and callable(func_or_name):
        return decorator(func_or_name)
    return decorator


class DBLock:
    def __init__(self, name, locked_by=None):
        self._name = name
        if locked_by is None:
            self._locked_by = f'{platform.node()}.{os.getpid()}'
        else:
            self._locked_by = locked_by
        # delay import model as class decorator runs before apps are ready
        self._model = apps.get_model('database_locks', 'Lock')
        self.__last_owner = None

    @property
    def name(self):
        # read-only (wrapper ofc, it's python...)
        return self._name

    def acquire(self, lock_ttl=10):
        with db.transaction.atomic():
            # evaluate expiry against the DB clock (Now()), not the acquiring VM's clock, so all
            # contenders share one time source - a fast/slow app server clock must not affect who
            # holds the lock
            dblock = (
                self._model.objects.select_for_update()
                .annotate(
                    is_active=Case(
                        When(expires_at__gt=Now(), then=Value(True)),
                        default=Value(False),
                        output_field=BooleanField(),
                    )
                )
                .filter(name=self._name)
                .first()
            )
            if dblock is None:
                logger.debug(
                    'lock %s not yet created, trying to create (and acquire)',
                    self._name,
                )
                # not protected by select_for_update so let's use just `.create`
                # so it blows up with IntegrityError in case of race (as name is unique)
                try:
                    self._model.objects.create(
                        name=self._name,
                        locked_by=self._locked_by,
                        expires_at=timezone.now()
                        + timezone.timedelta(seconds=lock_ttl),
                    )
                    logger.debug('lock %s (created and) acquired', self._name)
                    return True
                except db.IntegrityError:
                    logger.debug('could not create lock %s, try next time', self._name)
                    return False
            if dblock.is_active and dblock.locked_by != self._locked_by:
                # it's DEBUG level but no need to spam...
                if dblock.locked_by != self.__last_owner:
                    logger.debug(
                        'lock %s active and owned by %s, try later',
                        self._name,
                        dblock.locked_by,
                    )
                    self.__last_owner = dblock.locked_by
                return False

            # fencing: only overwrite rows still owned by us (or unowned/expired, matched above under
            # the row lock) - cheap defence in depth on top of select_for_update, in case that ever
            # gets bypassed (e.g. node-local row locks on a multi-writer Galera cluster)
            updated = self._model.objects.filter(
                pk=dblock.pk, locked_by=dblock.locked_by
            ).update(
                locked_by=self._locked_by,
                expires_at=timezone.now() + timezone.timedelta(seconds=lock_ttl),
            )
            if not updated:
                logger.debug('lock %s stolen from under us, try next time', self._name)
                return False
            logger.debug('lock %s acquired/renewed', self._name)
            return True

    def release(self):
        logger.debug('releasing lock %s', self._name)
        with db.transaction.atomic():
            dblock = (
                self._model.objects.select_for_update().filter(name=self._name).first()
            )
            if dblock and dblock.active and dblock.locked_by == self._locked_by:
                dblock.expires_at = None
                dblock.save()
                logger.debug('released lock %s', self._name)


class RenewThread(threading.Thread):
    def __init__(self, lock_obj, ttl, early_tick, max_failures=1):
        super(RenewThread, self).__init__()

        self.__lock = lock_obj
        self.__ttl = ttl
        # renew EARLY_TICK seconds before TTL
        self.__wait = max(ttl - early_tick, 1)
        self.__max_failures = max_failures
        self.__failures = 0

        self.__stopped = threading.Event()
        self.daemon = True

    def renew(self):
        try:
            if self.__lock.acquire(lock_ttl=self.__ttl):
                self.__failures = 0
                return
            # someone else genuinely owns it now - give up immediately, no tolerance
            logger.error('lock %s taken by someone else', self.__lock.name)
            self.__failures = self.__max_failures
        except Exception:
            # couldn't reach the DB (or similar) - tolerate up to max_failures, retrying next tick
            logger.exception('error re-acquiring lock %s', self.__lock.name)
            # don't let a retry reuse a broken connection
            db.close_old_connections()
            self.__failures += 1

        if self.__failures >= self.__max_failures:
            # is there any other way to notify main thread?
            self.__stopped.set()
            os.kill(os.getpid(), signal.SIGUSR1)

    def run(self):
        while True:
            self.renew()
            if self.__stopped.wait(self.__wait):
                break

    def stop(self):
        self.__stopped.set()
        return self.__stopped.wait()


class LockException(Exception):
    pass


def __default_lost_lock_cb(*_):
    raise LockException('lost lock, terminating')


def _status_file(message):
    if settings.DATABASE_LOCKS_STATUS_FILE is None:
        return
    try:
        with open(settings.DATABASE_LOCKS_STATUS_FILE, 'w') as _f:
            _f.write(message)
    except Exception:
        # log but don't break anything
        logger.exception(
            'failed to update lock status file %s', settings.DATABASE_LOCKS_STATUS_FILE
        )
