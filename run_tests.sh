#!/usr/bin/env bash
# Run the test suite.
#   HERMES_PYTHON  Python interpreter to use (default: python). Integration tests need the Python managed by a
#                  Hermes test profile.
#   PYTEST_PKGS    optional directory holding pytest (pure Python) if it is not installed in that interpreter.
#   HERMES_SRC     Hermes Agent sources (added to PYTHONPATH) - integration tests only.
#   HERMES_TEST_HOME  isolated Hermes test profile - integration tests only (NEVER your real profile).
# Examples:
#   bash run_tests.sh tests --ignore=tests/integration -q
#   bash run_tests.sh tests/integration -q
set -e
cd "$(dirname "$0")"
PY="${HERMES_PYTHON:-python}"
if [ -n "$PYTEST_PKGS" ]; then export PYTHONPATH="$PYTEST_PKGS${PYTHONPATH:+:$PYTHONPATH}"; fi
if [ -n "$HERMES_SRC" ]; then export PYTHONPATH="$HERMES_SRC${PYTHONPATH:+:$PYTHONPATH}"; fi
exec "$PY" -m pytest -c tests/pytest.ini "$@"
