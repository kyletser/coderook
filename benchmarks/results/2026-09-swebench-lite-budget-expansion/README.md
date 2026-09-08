# SWE-bench development subset: budget expansion

Date: 2026-09-07. This is a three-case development experiment, **not a full or held-out SWE-bench Lite score**.

## Setup

- Same frozen CodeRook image, `qwen3.8-flash`, OpenAI Chat wire format, temperature 0, single/direct strategy.
- Previous limits: 40 steps / 900 seconds. New limits: 80 steps / 1800 seconds.
- One fresh attempt per case per condition, not continuation of the earlier patch. No manual hints or patch edits during execution; official evaluation occurs afterward.
- Candidate base commit: `7cda2be6bc07b45845f9fd9bf196db2b1593e25f`, with uncommitted changes. The commit alone does not reproduce the candidate.
- Frozen source archive SHA-256: `97bd65a85e16089f9e1ff7fb5241e08c987d3514bd24ea057fe5f680ad01c2f3`.
- Runtime image digest: `sha256:b4ca8693500111465a2655417d5f5ba1b7e0605f823a94e53a1583ea20bca351`.
- Official instance images and test environments were reused without changes. The original environment control was Django-specific, not proof that all three environments were healthy.
- No additional full repository test suite or paid retries were run by the experiment operator. Commands selected by the evaluated Agent remain part of its measured execution.

## Results

| Case | Previous official result | Expanded official result | Expanded runtime end | Steps | Seconds | Input tokens | Output tokens |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| django__django-15320 | Pass | Pass | Time limit | 57 | 1800.18 | 1,233,388 | 51,565 |
| psf__requests-3362 | Fail | Fail | Normal | 32 | 648.84 | 453,605 | 48,289 |
| pallets__flask-5063 | Pass | Fail | Normal | 35 | 680.96 | 613,914 | 58,189 |

| Metric | 40 steps / 900 seconds | 80 steps / 1800 seconds |
| --- | ---: | ---: |
| Official resolved | 2/3 | 1/3 |
| Normal runtime ending | 1/3 | 2/3 |
| Normal ending AND official resolved | 0/3 | 0/3 |
| Input tokens | 1,611,819 | 2,300,907 |
| Output tokens | 132,081 | 158,043 |
| Sum of Agent elapsed time | 1571.96 s | 3129.97 s |
| Tool errors | 7 | 14 |

Usage is reported provider usage; interrupted requests may omit usage. USD cost is unavailable because route pricing is not configured. Token counts do not establish dollar cost.

## Findings and limitations

- Django produced a passing patch but spent substantial time waiting for tests, including explicit sleep-and-poll commands, and still reached the 30-minute limit. More budget did not establish autonomous completion.
- Requests still fails `test_response_decode_unicode`. Its official output also contains 108 fixture errors involving `httpbin`, so the environment is not clean despite the report's `infra_failure: false`. The target assertion itself fails; the result is retained, not discarded as infrastructure-only.
- Flask ends normally but fails both target tests, `TestRoutes::test_subdomain` and `TestRoutes::test_host`; reported PASS_TO_PASS tests pass. Runtime success is not code correctness.
- Requests and Flask ended below both earlier limits. Their different trajectories cannot be attributed solely to a larger limit. One attempt per condition cannot separate run variability from budget effects, even at temperature 0.
- No network retries or repeat reminders occurred in either condition. Expanded Django contained two consecutive-identical-call pairs, but no reminder event. These runs do not measure retry/reminder efficacy.
- Do not merge best patches across attempts or advertise the resulting fraction as overall Lite accuracy. This small selected subset shows no observed benefit from budget expansion; it does not prove larger budgets generally reduce accuracy.

Before expanding the paid benchmark, investigate targeted verification coverage and stopping after useful verification. Do not inject official hidden tests or answers into the Agent prompt.

## Evidence

- [Expanded summary](summary.json)
- [Per-case before/after comparison](comparison.json)
- [Execution contract](execution-contract.json)
- [Candidate metadata](candidate.json)
- [Submitted predictions](predictions.jsonl)
- Official raw reports: [Django](official/django__django-15320.json), [Requests](official/psf__requests-3362.json), [Flask](official/pallets__flask-5063.json).

Full local evidence is retained under `.benchmark-results/swebench-lite-expanded-20260907/`; the source archive and previous raw run remain under `.benchmark-results/swebench-lite-retry-20260907/`. These ignored local files are not included in this public summary. Published files exclude credentials, private prompt traces and official reference patches.
