import pytest

from repository_catalog import CATALOG, default_repository, prose_aliases_in, resolve_repository


EXPECTED_REPOSITORIES = {
    "BSLAgenticQuantDevLoop": ("bankingscience/BSLAgenticQuantDevLoop", "main"),
    "ATPConnectorsRepo": ("bankingscience/ATPConnectorsRepo", "develop"),
    "ATPDataHandlersRepo": ("bankingscience/ATPDataHandlersRepo", "develop"),
    "ATPSiftingAnalyticsRepo": (
        "bankingscience/ATPSiftingAnalyticsRepo",
        "develop",
    ),
    "ATPSiftingPreTradeRepo": (
        "bankingscience/ATPSiftingPreTradeRepo",
        "develop",
    ),
}


def test_catalog_contains_exactly_the_supported_repositories():
    assert {
        entry.alias: (entry.repo_full_name, entry.default_source_branch)
        for entry in CATALOG
    } == EXPECTED_REPOSITORIES
    assert default_repository().alias == "BSLAgenticQuantDevLoop"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" atpconnectorsrepo ", "ATPConnectorsRepo"),
        ("BANKINGSCIENCE/ATPDATAHANDLERSREPO", "ATPDataHandlersRepo"),
        (
            " HTTPS://GITHUB.COM/BANKINGSCIENCE/ATPSIFTINGANALYTICSREPO.GIT ",
            "ATPSiftingAnalyticsRepo",
        ),
        (
            "https://github.com/bankingscience/ATPSiftingPreTradeRepo",
            "ATPSiftingPreTradeRepo",
        ),
    ],
)
def test_catalog_normalizes_names_full_names_and_urls(value, expected):
    assert resolve_repository(value).alias == expected


def test_catalog_does_not_resolve_removed_subsystem_aliases():
    assert resolve_repository("hedge") is None
    assert resolve_repository("commons") is None
    assert resolve_repository("atrade_sifting_model") is None


def test_prose_selection_requires_a_catalog_repository_name():
    assert [
        entry.alias
        for entry in prose_aliases_in("Please inspect ATPConnectorsRepo changes")
    ] == ["ATPConnectorsRepo"]
    assert prose_aliases_in("Please inspect the hedge subsystem") == []
