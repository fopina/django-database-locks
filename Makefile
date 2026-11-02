.PHONY: lint test coverage style style_check

style:
	ruff format database_locks tests
	ruff check database_locks tests

style_check:
	ruff format --check database_locks tests
	ruff check database_locks tests

lint:
	ruff check database_locks tests

startmysql:
	docker inspect django-database-locks-mysql | grep '"Running": true' || \
		docker run --name django-database-locks-mysql \
		           -e MYSQL_ROOT_PASSWORD=root \
		           --rm -p 8877:3306 -d \
				   mysql:8.4

startpg:
	docker inspect django-database-locks-pg | grep '"Running": true' || \
		docker run --name django-database-locks-pg \
		           -e POSTGRES_USER=postgres \
          		   -e POSTGRES_PASSWORD=postgres \
				   -e POSTGRES_DB=postgres \
		           --rm -p 8878:5432 -d \
				   postgres:15

test: startmysql
	DJANGO_SETTINGS_MODULE=settings_mysql \
		tests/manage.py test $${TEST_ARGS:-tests}

testpg: startpg
	DJANGO_SETTINGS_MODULE=settings_postgresql \
		tests/manage.py test $${TEST_ARGS:-tests}

testcover: export DJANGO_SETTINGS_MODULE=settings_mysql
testcover: startmysql
testcover: coverage

coverage:
	python --version
	coverage erase
	PYTHONPATH="tests" \
		python -b -W always -m coverage run tests/manage.py test $${TEST_ARGS:-tests}
	coverage report
