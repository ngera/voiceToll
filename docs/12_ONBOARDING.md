# Getting started with voiceToll

voiceToll shows what each call to your voice agent costs, broken down by speech-to-text, the LLM and text-to-speech, and by customer. It runs next to your app and never slows a call down.

Setup has two parts. The first takes about five minutes and needs no provider keys. The second is optional: it checks voiceToll's numbers against your provider bills.

## What you need

- Python 3.11 or later and [uv](https://docs.astral.sh/uv/).
- A voice agent built with LiveKit Agents or Pipecat, or any app that can report its own usage.

## Part 1: see the cost of every call

### 1. Install and configure

In the voiceToll folder:

```powershell
uv sync
Copy-Item .env.example .env
```

The defaults in `.env` work on your own computer. Before sharing voiceToll with anyone, change `VOICETOLL_INGEST_KEY` and `VOICETOLL_HMAC_KEY` to long random values.

### 2. Start the collector

```powershell
uv run --env-file .env voicetoll-collector serve
```

Leave this running. It receives usage from your agent and works out the cost.

### 3. Connect your agent

Add a few lines to your agent. It reads the `VOICETOLL_*` settings from your environment.

LiveKit Agents:

```python
import voicetoll

voicetoll.configure()
meter = voicetoll.livekit.attach(session, tenant="your-customer-id", call_id=ctx.room.name)
session.on("metrics_collected", lambda ev: meter.observe(ev.metrics))
```

Pipecat:

```python
import voicetoll

voicetoll.configure()
observer = voicetoll.pipecat.Observer(tenant="your-customer-id", session_id=room_name)
worker = PipelineWorker(pipeline, params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
                        observers=[observer])
```

Customer ids are scrambled before they leave your app, so voiceToll never stores the real ones.

### 4. Make a call and look at the cost

Make a test call, then open the report at http://localhost:4319/report and enter your ingest key (`dev-key` by default). You'll see the day's cost by provider, every call, and for each call its cost per turn and how long each step took.

That's all you need. voiceToll uses published list prices for about 1,500 models from 50 providers.

### Check which models are priced

Set `VOICETOLL_ADMIN_KEY` in `.env`, restart the collector and open http://localhost:4319/admin. Under **Prices**:

- **In use** shows the prices behind your costs, and flags any that need a look.
- **All available** lists every provider and model voiceToll can price, with its list price. Use the provider and model names shown there, and calls are priced automatically.

If you pay a different rate than the list price (a contract or a discount), press **Override** next to the model. It gives you a rate card entry to paste into `config/rate_cards.yaml`.

If a model you use isn't listed, its calls show as **unpriced** rather than free. Add a rate card entry for it the same way.

## Part 2 (optional): check against your provider bills

voiceToll can compare its numbers with each provider's own usage records, once a day and for single calls. To do that it needs permission to read your usage from each provider.

### 1. Check your keys

```powershell
uv run --env-file .env voicetoll-collector doctor
```

This tests the keys you already have and tells you, in plain terms, what works and what to fix. For example, it might say which permission a key is missing, with a link to where you change it.

What each provider needs:

| Provider | Key | Notes |
| --- | --- | --- |
| Deepgram | Your agent's key works | Or a separate key with read access to usage |
| ElevenLabs | Your agent's key works if it's unrestricted | A restricted key needs read access to usage, and to Speech History for single-call checks |
| OpenAI | An organization admin key | Your agent's key can't read usage. Create one at [platform.openai.com/settings/organization/admin-keys](https://platform.openai.com/settings/organization/admin-keys) |

A separate read-only key for each provider is safer than reusing your agent's key. Put them in `.env` as `VOICETOLL_RECON_DEEPGRAM_KEY`, `VOICETOLL_RECON_ELEVENLABS_KEY` and `VOICETOLL_RECON_OPENAI_KEY`; voiceToll uses them whenever they're set.

### 2. Save the settings

When `doctor` says a provider is ready:

```powershell
uv run --env-file .env voicetoll-collector doctor --write
```

This saves the working settings, including which project each key belongs to, to `config/reconcile.yaml`. It never saves the keys themselves. Restart the collector.

### 3. See the comparison

- **Every day:** after midnight UTC, the collector compares the previous day with each provider. Results appear at the bottom of the report and in Admin → Cost.
- **For one call:** open the call in the report and press **Audit against providers**. Wait 5 to 10 minutes after the call ends, because providers take a while to record usage.

If the numbers differ by more than 5%, voiceToll flags it. Some providers take hours to update their records, so a new gap is worth re-checking the next day before you act on it.

## If something isn't working

- **No calls in the report:** check that the collector is running and that `VOICETOLL_ENDPOINT` and `VOICETOLL_INGEST_KEY` match in the agent's environment. At the end of each call the agent logs `voiceToll flush ok=True` when everything was sent.
- **Admin page says it's off:** set `VOICETOLL_ADMIN_KEY` in `.env` and restart the collector.
- **Anything else:** run `voicetoll-collector doctor`. It checks the database, your events, your rate cards and your provider keys.
