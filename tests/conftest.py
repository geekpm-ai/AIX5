import os
import tempfile

# Importing config creates its data directory. Keep all test data outside the repo.
_test_data = tempfile.TemporaryDirectory(prefix="aix5-tests-")
os.environ["AIX5_DATA_DIR"] = _test_data.name


def pytest_sessionfinish(session, exitstatus):
    _test_data.cleanup()
