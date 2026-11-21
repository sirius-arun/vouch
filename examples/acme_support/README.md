# Demo: Acme Cloud support bot with seeded defects

A small FastAPI bot for the fictional Acme Cloud. It answers with `gpt-4o-mini` using [policy.md](policy.md) as its system prompt. The `DEFECTS` variable injects known bugs, so you can check that Vouch catches them.

| Defect | What the bot does wrong | Requirement broken |
|---|---|---|
| `hallucinate_refund` | Says the refund window is 60 days (policy: 14) | R1 |
| `leak_internal` | Quotes the confidential internal note (ticket AC-4471, Morgan Lee) | R4 |
| `rude_tone` | Curt and condescending | R5 |
| `ignore_escalation` | "Approves" refunds itself instead of escalating billing disputes | R3 |

Each defect rewrites the relevant lines of the bot's system prompt, so the prompt never contradicts itself. The clean prompt also tells the bot to acknowledge frustration before answering (R5); `rude_tone` removes that line. (Just appending "ignore the rules above" isn't reliable: gpt-4o-mini often sticks to the original rule.) You can print the exact prompt with `python -c "import app; print(app.SYSTEM_PROMPT)"` from this folder.

Run all commands from the repo root. The bot reads `OPENAI_API_KEY` from `.env`.

**Start the bot, clean:**

```bash
uvicorn app:app --app-dir examples/acme_support --port 8000
```

**Start the bot with defects** (one, or several separated by commas):

```bash
DEFECTS=hallucinate_refund uvicorn app:app --app-dir examples/acme_support --port 8000
DEFECTS=hallucinate_refund,leak_internal,rude_tone,ignore_escalation uvicorn app:app --app-dir examples/acme_support --port 8000
```

Check what's active with `curl localhost:8000/health`. Then, in a second terminal:

```bash
vouch run examples/acme_support
```

`vouch run` reads the active defects from the bot's `GET /health`, so you don't need to tell it which ones are on. (To override, use `--defects a,b` or `--defects none`.)

Each run writes `report.html` and `report.md` to `examples/acme_support/runs/<timestamp>/`. These show:
- **With defects on:** the detection rate (cases targeting an active defect with at least one failing trial), and false positives on every other case.
- **With the bot clean:** "No seeded defects active (clean run)", and false positives across all cases.

Rebuild a report without new API calls using `vouch report examples/acme_support/runs/<timestamp>`.

There are 20 cases. Each one's first tag is the defect it targets, or `clean` for 5 control cases that no defect should break. Rule checks cover hard facts only (numbers, IDs, emails, forbidden phrases); tone is left to the LLM judge.

## Sample reports

Committed copies of real runs, so you can see the output without an API key:

| Run | Detection | False alarms | pass^1 / pass^5 | Report |
|---|---|---|---|---|
| All four defects on | 15 / 15 | 0 / 5 | 25% / 25% | [md](sample_runs/all_defects/report.md) · [html](sample_runs/all_defects/report.html) |
| Clean bot | n/a | 3 / 20 (each 1 of 5 trials) | 97% / 85% | [md](sample_runs/clean/report.md) · [html](sample_runs/clean/report.html) |

The main [README](../../README.md#demo-results) explains the three clean-run flags.
