import pytest

from vk import phi


def codes(text):
    return [h.code for h in phi.scan(text)]


@pytest.mark.parametrize("text,expected", [
    ("My SSN is 219-09-9999", "VK201"),
    ("card 4111 1111 1111 1111 on file", "VK202"),
    ("MRN: 00482913", "VK206"),
    ("member id A1B2C3D4E5", "VK206"),
    ("born 03/14/1962", "VK203"),
    ("call me at (312) 867-5309", "VK204"),
    ("email jane.doe@gmail.com", "VK205"),
])
def test_flags_real_looking_data(text, expected):
    assert expected in codes(text)


@pytest.mark.parametrize("text", [
    "Use 123-45-6789 as the example SSN",          # canonical fake
    "Call 555-0123 for the demo",                  # reserved fictional range
    "write to test.user@example.com",              # example domain
    "card 4111 1111 1111 1112",                    # fails Luhn
    "Ask for the caller's date of birth.",         # instruction, no value
    "order 1234567890123",                         # not Luhn-valid
    "",
])
def test_ignores_fake_or_instructional_text(text):
    assert not [c for c in codes(text) if c in ("VK201", "VK202", "VK204", "VK205")]


def test_errors_vs_warnings():
    severities = {h.code: h.severity for h in phi.scan("219-09-9999 and (312) 867-5309")}
    assert severities["VK201"] == "error"
    assert severities["VK204"] == "warning"


@pytest.mark.parametrize("text,expected", [
    ("MRN #00482913", "VK206"),
    ("Patient ID   :   ZX-998877", "VK206"),
    ("policy no. 77881234", "VK206"),
    ("reach me at sam.lee+work@mail.co.uk", "VK205"),
])
def test_still_flags_after_the_speedup(text, expected):
    assert expected in codes(text)


@pytest.mark.parametrize("text", [
    "MRN" + " " * 200_000,                          # spaces with no ID after them
    "a." * 100_000,                                 # dots and letters, no @
    "x@" + "a." * 100_000,                          # a very long domain that never ends in a TLD
])
def test_long_crafted_text_scans_quickly(text):
    """These used to take time growing with the square of their length (CodeQL py/polynomial-redos)."""
    import time
    start = time.perf_counter()
    phi.scan(text)
    assert time.perf_counter() - start < 1.0
