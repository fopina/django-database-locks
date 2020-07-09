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

.. image:: https://img.shields.io/pypi/pyversions/django-database-locks
    :alt: PyPI - Python Version

.. image:: https://img.shields.io/pypi/djversions/django-database-locks
    :alt: PyPI - Django Version

Distributed locks for Django using DB (MySQL/Postgres)

Given the limitation that Percona Cluster does not support MySQL locks, this app implements locks using `select_for_update()` (row locks).
