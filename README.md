# django-db-locks

Distributed locks for Django using DB (MySQL/Postgres)

Given the limitation that Percona Cluster does not support MySQL locks, this app implements locks using `select_for_update()` (row locks).
