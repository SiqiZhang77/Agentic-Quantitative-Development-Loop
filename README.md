# Agentic Quantitative Development Loop

**UCL MSc Financial Risk Management dissertation · 2026**  
**Siqi Zhang**

Project memory, manager–architect–developer coordination, and controlled evaluation for quantitative software-development tasks.

This repository contains the complete dissertation **code handover package**, including six frozen source snapshots, tests, configuration and dependency files, documentation, selected evaluation tables, and offline verification and analysis tools. The source snapshots remain separate so that each reported experiment can be traced to the code version used.

## Download the complete project

[**Download the complete code package**](2026_SiqiZhang_ProjectMemoryAgentCoordination_CODE_HANDOVER.zip)

The archive contains actual implementation source, not just a project summary. Start with the `e4v3_runtime` snapshot for the main portfolio-analysis experiment. Earlier snapshots and separately versioned experiment controls are included for traceability.

## What I worked on

- Role-specific instructions and structured JSON hand-offs for a manager–architect–developer workflow, with explicit tool permissions and shared budgets.
- BM25 retrieval over frozen Jira-derived project memory, supplied as bounded context.
- Controlled comparisons of retrieval and agent coordination, plus diagnostics distinguishing incorrect answers, missing submissions and workflow failures.
- Reproducible analysis and packaging of the frozen implementation and results.

The underlying quantitative-development platform was developed collaboratively. My dissertation contributions build on that shared foundation; this repository does not claim sole authorship of the whole platform.

## Selected evaluation result

The main portfolio-analysis task required **25 outputs per run**. The experiment included **60 runs**: 15 in each combination of single-agent / multi-agent and retrieval off / on.

| Measure | Single agent | Multi-agent |
|---|---:|---:|
| Runs, across both retrieval settings | 30 | 30 |
| Mean correct outputs per run | 12.2 / 25 | 15.6 / 25 |
| Correct / required outputs | 367 / 750 | 469 / 750 |
| Submitted / required outputs | 473 / 750 | 567 / 750 |
| Submission coverage | 63.1% | 75.6% |
| Correct / submitted outputs | 77.6% | 82.7% |

These are descriptive results for one benchmark. Retrieval did not improve mean correctness in this task. A submitted or structurally accepted output is not necessarily mathematically correct.

## Quick start — no model calls

Download and unzip the package, then open a terminal in its `code` directory:

```bash
unzip 2026_SiqiZhang_ProjectMemoryAgentCoordination_CODE_HANDOVER.zip
cd 2026_SiqiZhang_ProjectMemoryAgentCoordination/code

# Verify source archives, source files and packaged analytical inputs.
python3 tools/verify_and_extract.py

# Recalculate descriptive results from the included tables.
python3 analysis/reproduce_summaries.py --output reproduced_summaries.json

# Extract the main frozen runtime to inspect its implementation.
python3 tools/verify_and_extract.py --snapshot e4v3_runtime --destination extracted
```

These steps use Python 3.11+ and the standard library. They do not require credentials or call a model. The package's `code/README.md` explains the optional offline test environment and the additional requirements for an operational deployment.

## Package structure

```text
2026_SiqiZhang_ProjectMemoryAgentCoordination/
├── README.md
└── code/
    ├── README.md
    ├── SOURCE_MANIFEST.json
    ├── PACKAGE_SHA256.json
    ├── source_archives/       # Six separately identified frozen source snapshots
    ├── tools/                 # Verification, extraction and offline tests
    ├── analysis/              # Descriptive analysis and selected evaluation tables
    └── docs/                  # Packaging decisions and validation record
```

Main runtime entry points after extraction:

- `rae_runtime/proxy/exp3/`: manager-star coordination, role policies and budgets.
- `jira-chatops-gateway/dags/jira_rag_retriever.py`: frozen project-memory retrieval.
- `rae_runtime/proxy/pipeline.py` and `pipeline_mcp.py`: developer workflow.
- `rae_runtime/proxy/quant_calculator.py`: bounded quantitative calculations.
- `rae_runtime/proxy/t3_quant_item_submission.py`: item submission contracts.

## Scope and provenance

The original handover files are preserved byte-for-byte inside the downloadable package. Historical documentation describes the original company handover destination. This repository presents the same code package and preserves the original source attribution and licensing notices.

The package excludes populated credentials, private evaluator assets, original company Jira records and raw model traces. Running new agents requires separately configured services and credentials. The thesis PDF and final presentation are separate deliverables and are not included in this code package. Consult the packaged documentation for evaluation limitations and source-version mappings.
