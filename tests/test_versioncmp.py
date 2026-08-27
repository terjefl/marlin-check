import pytest

from app.versioncmp import compare_versions, meets_minimum, parse_version


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("2.1.0", "2.1.0", 0),
        ("2.1", "2.1.0", 0),
        ("2.1.1", "2.1.0", 1),
        ("2.0.9", "2.1.0", -1),
        ("1.14", "1.9", 1),          # tallmessig, ikke leksikografisk
        ("2.1.0-b123", "2.1.0", 1),  # byggsuffiks teller som ekstra segment
        ("10.0", "9.9", 1),
    ],
)
def test_compare_versions(a, b, expected):
    assert compare_versions(a, b) == expected


def test_meets_minimum():
    assert meets_minimum("2.1.4", "2.1.0")
    assert meets_minimum("2.1.0", "2.1.0")
    assert not meets_minimum("1.9.9", "2.1.0")


def test_unparseable_version_raises():
    with pytest.raises(ValueError):
        parse_version("unknown")
