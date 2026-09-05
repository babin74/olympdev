import pytest

from olympdev.isolate import IsolateMeta


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("OK", False),
        ("RE", False),
        ("SG", False),
        ("TO", False),
        ("XX", True),
    ],
)
def test_isolate_meta_is_sandbox_error(status: str, expected: bool):
    assert IsolateMeta(status=status).is_sandbox_error() is expected


@pytest.mark.parametrize(
    ("status", "exitcode", "expected"),
    [
        ("OK", 0, True),
        ("OK", 1, False),
        ("RE", 1, False),
        ("SG", 0, False),
        ("TO", 0, False),
        ("XX", 0, False),
    ],
)
def test_isolate_meta_is_successful_termination(
    status: str,
    exitcode: int,
    expected: bool,
):
    assert (
        IsolateMeta(status=status, exitcode=exitcode).is_successful_termination()
        is expected
    )
