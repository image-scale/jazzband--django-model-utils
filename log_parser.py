import re


def parse_log(log: str) -> dict[str, str]:
    """Parse pytest verbose output into per-test results.

    Args:
        log: Full stdout+stderr output of `bash run_test.sh 2>&1`.

    Returns:
        Dict mapping test_id to status.
        - test_id: pytest native format (e.g. "tests/foo.py::TestClass::test_func")
        - status: one of "PASSED", "FAILED", "SKIPPED", "ERROR"
    """
    results: dict[str, str] = {}

    # Match pytest verbose output lines like:
    # tests/test_choices.py::ChoicesTests::test_composability PASSED  [  0%]
    # tests/test_managers/test_join_manager.py::JoinManagerTest::test_self_join FAILED [ 84%]
    # Also handles SKIPPED, XFAIL, XPASS, ERROR
    line_pattern = re.compile(
        r'^(\S+::\S+)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\s+\[\s*\d+%\]'
    )

    for line in log.splitlines():
        line = line.strip()
        m = line_pattern.match(line)
        if m:
            test_id = m.group(1)
            status = m.group(2)
            # Normalize statuses
            if status == "XFAIL":
                status = "SKIPPED"
            elif status == "XPASS":
                status = "PASSED"
            results[test_id] = status

    # Handle collection errors: lines like "ERROR tests/foo.py" in the short summary
    error_pattern = re.compile(r'^ERROR\s+(tests/\S+\.py)\s*-')
    for line in log.splitlines():
        line = line.strip()
        m = error_pattern.match(line)
        if m:
            module = m.group(1)
            results[module] = "ERROR"

    # Also parse FAILED lines from the short test summary section
    # e.g.: FAILED tests/test_managers/test_join_manager.py::JoinManagerTest::test_self_join - ...
    failed_pattern = re.compile(r'^FAILED\s+(\S+::\S+)')
    for line in log.splitlines():
        line = line.strip()
        m = failed_pattern.match(line)
        if m:
            test_id = m.group(1)
            # Only set if not already captured (verbose line takes precedence)
            if test_id not in results:
                results[test_id] = "FAILED"

    return results

