from database import enableTestDatabaseGuard


# This changes only the current Python test process. Normal application launchers do
# not import the tests package and therefore never enable this guard.
enableTestDatabaseGuard()
