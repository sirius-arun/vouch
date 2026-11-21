# Contributing

Thanks for your interest in Vouch!

## Before you start

Please **open an issue first** for anything beyond a small fix, so we can agree on the approach before you spend time on it. Bug reports are most useful with the command you ran, the relevant YAML, and the error or a snippet of the report.

## Setup

```bash
git clone https://github.com/sirius-arun/vouch.git
cd vouch
python3 -m venv .venv && source .venv/bin/activate   # Python 3.11+
pip install -e .
```

## Run the tests

```bash
pytest -q
```

The tests don't need an API key or network access: the judge and HTTP targets are mocked. CI runs them on Python 3.11 and 3.12 for every push and pull request.

Running the demo end to end (`examples/acme_support`) does need an `OPENAI_API_KEY` in `.env`; see the README quickstart.

## Pull requests

- Keep changes focused, and add or update tests for any behaviour change.
- Keep the code simple and readable, with short docstrings, matching the existing style.
- Don't commit `.env`, run folders (`runs/`) or anything containing real customer or company data. Examples must use fictional domains.

By contributing, you agree that your contributions are licensed under the MIT License.
