# Validation record

Validated on 8 September 2026, before upload. No model run, deployment, Git push or Pull Request was performed while preparing this package.

| Check | Result |
|---|---|
| Exact Git exports | 6 identified snapshots; 3,929 regular source-file entries preserved byte-for-byte |
| Archive and file integrity | Archive SHA-256 and individual source-file SHA-256 checks passed |
| Python source parsing | 1,297 Python entries across the six snapshots parsed successfully |
| Excluded local artefacts | No `.git`, populated `.env`, virtual-environment, cache, node_modules or key-file paths in source archives |
| Targeted credential-pattern review | No unresolved high-confidence token/key pattern findings; six URL matches were the same synthetic `example.com` test fixture, retained unchanged |
| Selected offline tests | **195 passed**: calculator/audit, role policy, budgets, hand-off contracts, T3 public item contract and frozen retrieval |
| Descriptive data validation | 60 T3 runs, 1,500 T3 items, 32 T1/T2 rows and 15 relevance labels passed alignment/count checks |
| Descriptive results | T3 condition means/coverage, T1/T2 final/content means and pairs, and reported retrieval metrics recomputed successfully |

The selected tests were executed with Python 3.11.6, pytest 8.3.4 and jsonschema 4.26.0. The full production sandbox has a separate, retained dependency lock generated with Python 3.12; the two-package offline test requirement file is not a substitute for the full runtime dependencies.

An initial broader test selection could not collect a test importing the production GitHub client because the local test environment lacked `python-dotenv`. The final portable subset intentionally avoids those production-client imports. The full schema/client integration tests remain in the source archives and were not certified by this handover. No frozen source was changed to make a test pass.

These checks establish integrity and selected offline behaviour. They do not establish that external company services are configured or available, that all historical tests pass in a new environment, or that new model outputs will reproduce the historical observations.
