# The Deniability Gap

A benchmark that asks: when an LLM has an incentive to leave a user with a favourable impression, does its message lead readers to a claim P that the model does not itself believe and disowns when asked directly? Each scenario gives an advisor model a pool of facts and a user asking for advice, then takes three measurements of one proposition P: IMPLIED (blind reader models see only the advice message and rate how likely P is), DEFENDED (in the same conversation the user asks the model "are you saying P?"), and BELIEVED (in a fresh context with the same facts, the model rates P itself). The advisor holds the facts as private product information in its system prompt; the user's message is only the question. Every scenario runs under three conditions: neutral, mild (the system prompt also states the operator's commercial interest) and strong (it also states the impression the operator wants the user to have). No condition tells the model to lie or to withhold facts.

All code and data in this repository were created during the Apart Research AI x Epistemics sprint (13-15 Nov 2026). No preexisting code is included.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
cp .env.example .env    # then fill in the keys you need; .env is git-ignored
```

Edit `config/models.yaml` and replace the `FILL_ME/...` placeholders with real litellm model strings before any real run.

## Tests

```bash
DGAP_MOCK=1 python -m pytest -q
```

Tests need no API keys and no network. With `DGAP_MOCK=1`, `dgap.models.complete` returns a deterministic canned response instead of calling a provider.

## Layout

```
config/models.yaml   target, reader and judge models
prompts/             plain-text prompt templates with {placeholders}
data/pilot.jsonl     example scenarios (one JSON object per line)
src/dgap/            schema, prompt rendering, cached model client
tests/               pytest suite
results/             raw outputs (and the response cache) and tables
```
