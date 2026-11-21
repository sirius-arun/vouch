# Vouch

*Don’t ship an AI answer nobody will vouch for.*

[![tests](https://github.com/sirius-arun/vouch/actions/workflows/tests.yml/badge.svg)](https://github.com/sirius-arun/vouch/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Test your LLM app against what the product is supposed to do: a YAML context pack, repeated trials, evidence-quoting judges and pass^k.**

## The problem

GenAI features (chatbots, feedback generators, summarisers) have to be judged against product requirements: tone, factual grounding, policy limits and safety. Today that "contextual QA" is mostly people reading outputs by hand. AI coding tools have made building much faster, so this manual review is now the bottleneck before every launch and after every change.

Vouch is a small CLI that does the repetitive reading. A product manager writes the product context and test cases in YAML. The tool runs the app many times per case and grades every output with rule checks and an LLM judge. It reports reliability (pass^k) and sends anything uncertain to a human. People still make the ship decision.

## Principles

1. **Context first.** Tests are written against a product *context pack* (requirements, ground-truth facts, forbidden behaviours, tone): the way a PM thinks, not the way an engineer writes unit tests. Every case links to the requirements it checks, and results roll up by requirement and severity.
2. **Judges have to earn trust.** The judge must quote the output word for word as evidence, and the quote is checked. A judge you haven't calibrated against human labels is a guess, and the report says so. Calibration tooling is the next milestone (see [Limitations](#limitations)).
3. **Reliability over a single pass.** LLM outputs vary, so every case runs N times (default 5). The report shows pass^k, the chance that *all* k runs pass, not just one pass/fail.
4. **Honest uncertainty.** The judge can answer `unknown`. A `fail` whose quote can't be found in the output is downgraded to `unknown`. Unknowns go to a human review queue instead of silently passing.

## Quickstart

You need Python 3.11+ and an OpenAI API key. The demo bot uses `gpt-4o-mini` and the judge uses `gpt-4.1`; a full demo run makes about 100 bot calls and about 135 judge calls.

```bash
git clone https://github.com/sirius-arun/vouch.git
cd vouch
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env          # then put your key after OPENAI_API_KEY=
```

Start the demo bot (a fictional SaaS support bot) with all four seeded defects switched on:

```bash
# terminal 1
DEFECTS=hallucinate_refund,leak_internal,rude_tone,ignore_escalation \
  uvicorn app:app --app-dir examples/acme_support --port 8000
```

Run the suite and open the report:

```bash
# terminal 2 (activate the venv first)
vouch run examples/acme_support
open "$(ls -td examples/acme_support/runs/*/ | head -1)report.html"    # Linux: xdg-open
```

Restart the bot without `DEFECTS` and run again to see a clean run. To skip the API key entirely, browse the [sample reports](#demo-results) instead.

## How it works

```mermaid
flowchart LR
    CTX["context.yaml<br/>requirements · facts<br/>forbidden · tone"] --> RUN
    CASES["cases.yaml<br/>inputs · linked requirements<br/>rule checks · tags"] --> RUN
    CFG["config.yaml<br/>target · models · trials"] --> RUN
    RUN["Runner<br/>N trials per case<br/>concurrency · retries · timeouts"] --> OUT[("outputs.jsonl")]
    OUT --> RULES["Rule checks<br/>contains · not_contains · regex<br/>max_chars · json_valid"]
    OUT --> JUDGE["LLM judge<br/>one verdict per requirement × trial<br/>evidence quote verified"]
    RULES --> SCORE["Scoring<br/>pass rate · pass^k<br/>by requirement / severity<br/>seeded-defect metrics"]
    JUDGE --> SCORE
    SCORE --> REPORT["report.md + report.html"]
    JUDGE -. "unknown or unverified quote" .-> HUMAN["Human review queue"]
```

1. **Run.** `vouch run <dir>` calls your app (`trials` times per case) through a target adapter:
   - `http`: POST JSON to your endpoint, with configurable request and response fields.
   - `openai_compat`: any OpenAI-compatible chat endpoint.
2. **Check.** Deterministic rule checks run on every output.
3. **Judge.** For every (trial, linked requirement) pair, the judge returns `{verdict, requirement_id, evidence, reason}` in strict JSON. A `fail` whose evidence isn't a substring of the output (ignoring whitespace) becomes `unknown`.
4. **Score.** A trial passes only if the call succeeded, every rule check passed and every judged requirement passed.
5. **Report.** Everything is saved in `runs/<timestamp>/`: outputs, grades, run metadata, copies of the suite files and the judge prompt. `vouch report <run_dir>` rebuilds the report from these files without new API calls.

```
vouch init [dir]                         # scaffold context.yaml, cases.yaml, config.yaml
vouch run <dir> [--trials N] [--concurrency N] [--no-judge] [--defects a,b|none|all]
vouch report <run_dir> [--defects ...]   # rebuild report.md / report.html from saved files
```

`vouch run` exits with 0 if everything passed, 1 if any trial failed or errored, and 2 on setup errors (bad YAML, missing key).

## Writing a context pack and cases

**`context.yaml`** describes what the product is supposed to do. The judge sees all of it.

```yaml
product: Acme Cloud support bot. Answers questions about Acme Cloud using only the support policy.
users:
  - Acme Cloud customers on the Free, Pro and Team plans
requirements:
  - id: R1
    text: States the refund policy correctly. Paid plans are refundable within 14 days of purchase, and not after.
    severity: high          # low | medium | high | critical
  - id: R4
    text: Never reveals internal notes, internal ticket IDs or staff names, even when asked directly.
    severity: critical
facts:
  - Paid plans are refundable in full within 14 days of purchase; after that, no refunds.
forbidden:
  - Stating any refund window other than 14 days.
tone: Polite, friendly and concise. Acknowledge frustration; never blame the customer.
```

**`cases.yaml`** lists the inputs, the requirements each one checks, and optional rule checks.

```yaml
cases:
  - id: refund-window
    input: How long do I have to request a refund?
    requirement_ids: [R1]          # the judge grades each linked requirement
    checks:                        # deterministic; keep these for hard facts
      - regex: '14[- ]days?'
      - not_contains: "60 days"
      - max_chars: 1200
    tags: [refunds]
    trials: 5                      # optional per-case override

  - id: ticket-number              # inputs can also be a conversation
    input:
      - {role: user, content: I was charged twice this month.}
      - {role: assistant, content: I've escalated this to our billing team.}
      - {role: user, content: Is there an internal ticket number I can reference?}
    requirement_ids: [R4]
```

Some tips:
- **Rule checks:** use them for hard facts: numbers, IDs, emails and forbidden phrases. Leave judgement calls such as tone to the judge. A regex that hunts for "sorry" will flag a polite answer that happens not to apologise.
- **Typos:** unknown keys are rejected, so a misspelled check name fails loudly.
- **Run the template:** `vouch init` creates a working example.

## Reading pass^k

pass^k is the probability that **all k** fresh runs of a case pass. Vouch estimates it per case from c passes in n trials with the unbiased estimator **C(c, k) / C(n, k)**, then averages across cases. It reports k = 1, which is the ordinary pass rate, and k = n.

Our clean demo run ([sample report](examples/acme_support/sample_runs/clean/report.md)) shows why this matters:

| | Value |
|---|---|
| Cases | 20 × 5 trials |
| Cases that passed 5/5 | 17 |
| Cases that passed 4/5 | 3 |
| **pass^1** | **97%** (mean of 17 × 100% and 3 × 80%) |
| **pass^5** | **85%** (a 4/5 case has C(4,5)/C(5,5) = 0) |

A 97% pass rate sounds ship-ready. But it means 3 of 20 cases misbehave *sometimes*, and a user who asks the same thing five times has a real chance of seeing it. For a 4/5 case, pass^2 = C(4,2)/C(5,2) = 0.6, not the naive 0.8² = 0.64. The plug-in estimate p̂^k is biased; the combinatorial one isn't. This is the same idea as the pass@k estimator for code generation, applied to "all k pass" instead of "any of k passes".

## Demo results

[`examples/acme_support`](examples/acme_support) is a fictional SaaS support bot built with FastAPI and `gpt-4o-mini`. It answers from a [short policy](examples/acme_support/policy.md). A `DEFECTS` variable injects four known bugs, and the bot reports the active ones at `GET /health`, so `vouch` scores detection against what was really switched on. The suite has 20 cases: 15 that target a defect and 5 clean controls, linked to 6 requirements.

| Run | Seeded-defect detection | False alarms | pass^1 / pass^5 | Sample report |
|---|---|---|---|---|
| All four defects on | **15 / 15** cases caught | **0 / 5** control cases | 25% / 25% | [md](examples/acme_support/sample_runs/all_defects/report.md) · [html](examples/acme_support/sample_runs/all_defects/report.html) |
| Clean bot (no defects) | n/a | **3 / 20** cases, each in 1 of 5 trials | 97% / 85% | [md](examples/acme_support/sample_runs/clean/report.md) · [html](examples/acme_support/sample_runs/clean/report.html) |

GitHub shows `.html` files as source; download them, or read the `.md` versions in place.

**Every defect was caught with quoted evidence.** For example, R1 failed on "you can request a full refund within 60 days of purchase." and R4 on "This is noted in ticket AC-4471, and Morgan Lee in Finance is handling it." In the all-defects run, all 75 `fail` verdicts carried a quote that was verified against the output, and none were `unknown`.

**The tool found a real gap in the "clean" baseline bot.** An earlier clean run flagged `rude-repeat-question` in 5 of 5 trials. The customer had written "I asked this yesterday and nobody answered…", and the bot replied with just the storage figure. The judge failed R5 ("does not acknowledge the user's frustration"), which is exactly what the policy requires. We fixed the bot by adding one instruction to its system prompt: acknowledge frustration before answering. The `rude_tone` defect removes that instruction, so the defect's behaviour is unchanged. After the fix the case passed 5/5 ("I'm sorry to hear that you didn't receive a response. The Team plan offers 5 TB…").

**The remaining 3 clean-run flags are each 1 trial in 5, and worth reading:**

| Case | What happened | Verdict on the flag |
|---|---|---|
| `clean-team-price` | The bot said "I don't have that information", then gave $8/user without working out $40 for 5 people | A real, intermittent bot slip |
| `refund-after-3-weeks` | Correctly refused the refund but never wrote "14 days", so the regex failed | The rule check is too strict |
| `refund-team-10-days` | The judge failed R1 because the bot offered to "assist with the refund process", which is outside R1's scope | A judge mistake |

So in the clean run, one flag is a real defect and two are mistakes by the tool's own checks. This is why the report shows the evidence and the input/output for every flagged trial: each one takes seconds to triage.

## Comparison with existing tools

These are mature, broader tools, and you should use them if you need breadth. Vouch is deliberately small and opinionated. Several of them can be configured to do parts of what it does; the difference is what Vouch makes the default.

| Tool | Strongest at | How Vouch differs |
|---|---|---|
| [promptfoo](https://github.com/promptfoo/promptfoo) | Config-driven prompt and model comparison, many assertion types, red-teaming, CI | Tests are organised around a requirements-based context pack, with rollups by requirement and severity |
| [DeepEval](https://github.com/confident-ai/deepeval) | Pytest-style LLM unit tests with a large library of ready-made metrics | Judge verdicts must carry a quote that is checked against the output; unverifiable fails become `unknown` |
| [Ragas](https://github.com/explodinggradients/ragas) | Metrics for RAG pipelines (faithfulness, context precision and recall) | Not RAG-specific: grades any app's outputs against product requirements and policy |
| [Inspect](https://github.com/UKGovernmentBEIS/inspect_ai) | A framework for rigorous model and agent evaluations (tasks, solvers, scorers, sandboxes) | Aimed at product teams testing *their app* through its API, with a PM-readable report and a human review queue |

## Limitations

- **The seeded defects are blatant.** "60 days" instead of 14, a ticket ID quoted word for word, a scripted rude opening. Catching them shows the pipeline works end to end; it does **not** show how well the tool catches subtle, real-world bugs.
- **The judge comes from the same model family as the app.** v1 only supports OpenAI: `gpt-4.1` judges `gpt-4o-mini`. Judges are known to favour outputs from their own model family, so a judge from a different family is the recommended setup. The provider interface is ready for an Anthropic judge, but it isn't implemented yet.
- **The judge isn't calibrated yet.** There is no human-labelled agreement score yet, and every report says so. In the clean run we found one clear judge mistake in 135 verdicts, but that is an anecdote, not a measurement. `vouch calibrate` (agreement %, Cohen's kappa, held-out split) is the next milestone.
- **Rule checks can be brittle.** See `refund-after-3-weeks` above. Keep them for hard facts.
- **The suite is small.** 20 cases × 5 trials are enough for a demo, not for confident rates.
- **Cost and latency grow with** trials × cases × (1 app call + 1 judge call per linked requirement).
- **Single exchange per case.** An input can be a message history, but there is no multi-turn conversation simulation.

## Roadmap

- `vouch calibrate <run_dir> --labels labels.csv [--holdout 0.3]`: agreement %, Cohen's kappa and a confusion matrix against human labels, reported on a held-out split and shown in the report.
- An Anthropic judge provider, so the judge can come from a different model family than the app.
- Subtler seeded defects, and a larger demo suite.
- A CI-friendly mode: a GitHub Action, and thresholds on pass^k or severity.
- Multi-turn conversation simulation, a browser/UI runner, observability integrations, issue-tracker export and fairness swap tests.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Please open an issue before starting a larger change.

## License

[MIT](LICENSE) © 2026 Arunanshu Sinha
