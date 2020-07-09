.PHONY: flake8 test coverage style style_check

style:
	black --target-version=py36 db_locks tests setup.py
	flake8 db_locks tests

style_check:
	black --target-version=py36 --check db_locks tests setup.py

flake8:
	flake8 db_locks tests

startmysql:
	docker inspect django-db-locks-mysql | grep '"Running": true' || \
		docker run --name django-db-locks-mysql \
		           -e MYSQL_ROOT_PASSWORD=root \
		           --rm -p 8877:3306 -d \
				   mysql:5

startpg:
	docker inspect django-db-locks-pg | grep '"Running": true' || \
		docker run --name django-db-locks-pg \
		           -e POSTGRES_USER=postgres \
          		   -e POSTGRES_PASSWORD=postgres \
				   -e POSTGRES_DB=postgres \
		           --rm -p 8878:5432 -d \
				   postgres:10

test: startmysql
	DJANGO_SETTINGS_MODULE=settings_mysql \
		tests/manage.py test $${TEST_ARGS:-tests}

testpg: startpg
	DJANGO_SETTINGS_MODULE=settings_pg \
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