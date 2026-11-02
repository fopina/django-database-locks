=====================
django-database-locks
=====================

.. image:: https://github.com/fopina/django-database-locks/workflows/tests/badge.svg
    :target: https://github.com/fopina/django-database-locks/actions?query=workflow%3Atests
    :alt: tests

.. image:: https://codecov.io/gh/fopina/django-database-locks/branch/master/graph/badge.svg
   :target: https://codecov.io/gh/fopina/django-database-locks
   :alt: Test coverage status

.. image:: https://img.shields.io/pypi/v/django-database-locks
    :target: https://pypi.org/project/django-database-locks/
    :alt: Current version on PyPi

.. image:: https://img.shields.io/pypi/dm/django-database-locks
    :target: https://pypi.org/project/django-database-locks/
    :alt: monthly downloads

.. image:: https://img.shields.io/pypi/pyversions/django-database-locks
    :alt: PyPI - Python Version

.. image:: https://img.shields.io/pypi/djversions/django-database-locks
    :alt: PyPI - Django Version

Distributed locks for Django using DB (MySQL/Postgres)

Given the limitation that Percona Cluster does not support MySQL locks, this app implements locks using `select_for_update()` (row locks).

Installation
------------

    pip install django-database-locks


Usage
-----

`django-database-locks` exposes one single the `lock` contextmanager and the `locked` decorator.

The `locked` decorator will wrap a django management command (subclasses of `django.core.management.base.BaseCommand`) or any function with the `lock` contextmanager:


.. code-block:: python

    from django.core.management.base import BaseCommand

    from database_locks import locked

    @locked
    class Command(BaseCommand):
        ...
        def handle(self, *args, **options):
            self.stdout.write('Got the lock')


.. code-block:: python

    from database_locks import locked
    
    class SomeClass:
      def non_locked(self):
        pass
      
      @locked
      def locked(self):
        print('got lock')

.. code-block:: python

    from database_locks import lock
    
    class SomeClass:
      def non_locked(self):
        pass
      
      def locked(self):
        with lock():
            print('got lock')

Docs
----

Both `lock` and `locked` have the same optional args:

.. code-block:: python

    :param lock_name: unique name in DB for this function
    :param timeout: numbers of seconds to wait to acquire lock
    :param lock_ttl: expiration timer of the lock, in seconds (set to None to infinite)
    :param locked_by: owner id for the lock (if lock is active but owner is the same, returns acquired)
    :param auto_renew: if set to True will re-acquire lock (for `lock_ttl` seconds) before `lock_ttl` is over.
                       auto_renew thread will raise LockException (via SIGUSR1) on the main thread in case
                       re-acquiring keeps failing (see `max_failures`)
    :param retry: retry every `retry` seconds acquiring until successful. set to None or 0 to disable.
    :param lost_lock_cb: callback function when lock is lost (when re-acquiring). defaults to raising LockException
    :param max_failures: number of consecutive renew failures (DB unreachable) tolerated before giving up the
                          lock (default `1`, i.e. no tolerance - same as before this option existed). A renewal
                          that fails because someone else genuinely owns the lock always gives up immediately,
                          regardless of this value.

There are also the following options you can specify in the project `settings.py`

- *DATABASE_LOCKS_STATUS_FILE*: file that will be updated with the lock status (default `None`). Useful when you have multiple shared-lock processes, to quickly inspect which one has the lock.
- *DATABASE_LOCKS_ENABLED*: set to `False` to globally disable locks (default `True`)
- *DATABASE_LOCKS_DEFAULT_TTL*: global lock TTL value (default `60`) which is ignored when `lock_ttl` is specified
- *DATABASE_LOCKS_DEFAULT_TTL_RENEW*: number of seconds to renew lock before TTL expires (default `45`)

`max_failures` invariant
------------------------

If you use `max_failures` > 1, keep:

.. code-block:: text

    max_failures * (lock_ttl - lock_ttl_renew) < lock_ttl

i.e. the total tolerated outage must fit inside the lease, with margin - a warning is logged at
lock-acquisition time if it doesn't. Example: `lock_ttl=120, lock_ttl_renew=100` -> renew is attempted
every 20s, so `max_failures=3` tolerates roughly 60s of DB unavailability before giving up the lock.

Clock requirements
------------------

Lock expiry is evaluated in SQL against the *database's* clock (``NOW()``), not the application
server's clock, so contending processes share a single time source regardless of NTP drift between
them. The `Lock.active` property still uses the local clock and is for admin/debug convenience only -
it is not used to decide acquisition.

Galera / multi-master caveats
------------------------------

``SELECT ... FOR UPDATE`` row locks are **node-local** on a synchronous multi-master cluster such as
Galera/Percona XtraDB Cluster: a transaction on one node does not block a conflicting transaction on
another node. Galera is optimistic - conflicting writes are only caught at commit time, surfacing as a
certification failure reported to the client as a regular MySQL deadlock (error 1213). Two processes on
two different nodes can both believe they hold the row lock; one of them loses at commit and gets 1213
(handled internally by retrying acquisition, see `timeout`/`retry`).

Also, with the default ``wsrep_sync_wait=0``, a node may serve a read from a snapshot that has not yet
applied a write already committed elsewhere in the cluster - so a process can observe an expired lease
that another node just renewed, and (incorrectly) treat the lock as free.

Before relying on this library on a Galera-like cluster, determine whether your setup is:

- **Single-writer** (e.g. all writes routed to one node via ProxySQL): everything serialises there, row
  locks behave as this library assumes, no extra work needed.
- **Multi-writer** (each node accepts writes locally): set ``wsrep_sync_wait=1`` for the lock queries (at
  least), and expect MySQL error 1213 on acquire as normal operation - this library retries the acquire
  loop on ``db.Error`` for exactly this reason.


Testing
-------

Tox is used by the Github Action to test several python and django versions, with both MySQL and Postgres.

To quickly test locally, kick off a MySQL and/or Postgres docker container:


    docker run -d --name locks-test \
               -p 8877:3306 \
               -e MYSQL_ROOT_PASSWORD=root \
               mysql:5.7

    docker run -d --name locks-test-psql \
               -p 8878:5432 \
               -e POSTGRES_PASSWORD=postgres \
               postgres:10


List available environments with `tox -l` and then run the one you want/have:

    tox -e py39-dj32-mysql
    # or
    tox -e py39-dj32-postgresql -e py39-dj32-mysql
