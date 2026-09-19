# Project Memory and Agent Coordination in Quantitative Software Development

**Siqi Zhang | MSc Financial Risk Management, University College London | 2026**

This directory provides the code handover for *Project Memory and Agent Coordination in Quantitative Software Development: An Empirical Evaluation*. The project evaluates frozen Jira-derived project memory and a manager-star coordination protocol within an auditable quantitative software-development workflow.

## Start here

- [Code README](code/README.md): quick start, version selection, implementation map and execution requirements.
- [Code structure and packaging decisions](code/docs/PACKAGING_DECISIONS.md): what was included, why versions are separate, and material retained outside this handover.
- [Validation record](code/docs/VALIDATION.md): checks performed on the submitted files and the scope of those checks.
- [Analysis data dictionary](code/analysis/README.md): how to reproduce descriptive results without calling a model.

The code folder is self-contained for source verification, source extraction and descriptive result recalculation. Selected runtime and retrieval tests require the two packages listed in `code/tools/requirements-offline.txt`. Running the operational system additionally requires the company infrastructure and credentials described in the code README.

The thesis PDF and final presentation are separate submission materials. Their presence or final approval is not implied by this code handover. Upload the author's approved versions alongside this README when ready.

## Research design

| Task | Required work | Reported comparisons |
|---|---|---|
| T1 | Explain existing result-emission code with source-grounded claims | 5 primary retrieval pairs; 3 supplementary architecture pairs |
| T2 | Introduce a clock interface, preserve budget behaviour and deliver deterministic tests | 5 primary retrieval pairs; 3 supplementary architecture pairs |
| T3 | Produce 25 required portfolio-analysis outputs from synthetic returns across three designed complexity levels | 2 x 2 architecture/retrieval comparison; 15 observations per condition |

T1/T2 primary conditions are C0 (no memory) and C1 (project memory). Their supplementary architecture comparison uses M0 and M1 with memory disabled. T3 crosses M0 (individual developer) / M1 (manager-star) with R0 (no memory) / R1 (project memory). These labels identify different factors and must not be collapsed into a single baseline.

## Contribution and access

The Jira/Airflow/runtime development loop is a collaborative company foundation. The dissertation's individual contributions concern the frozen project-memory pipeline, controlled retrieval comparisons, manager-star coordination and the associated evaluation and diagnostics. Shared infrastructure is retained because these extensions depend on it; this does not imply sole authorship of that infrastructure.

The original development repository is the private `bankingscience/BSLAgenticQuantDevLoop`. This handover is prepared for `bankingscience/ATPSiftingAnalyticsRepo`, on branch `2026_SiqiZhang_ProjectMemoryAgentCoordination`, inside the identically named directory. Original company access and ownership arrangements continue to apply; no new public licence is granted by this README.
