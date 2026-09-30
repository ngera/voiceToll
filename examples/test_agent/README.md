# G3 test agents

The same small voice receptionist built twice, once on **LiveKit Agents** (`agent.py`) and once on **Pipecat** (`pipecat_agent.py`), each with voiceToll attached, plus a daily script that compares voiceToll's numbers with what the providers report. Running them for a week is gate **G3**: do voiceToll's estimates match the real bills, whichever framework produced them?

Stack (both agents): Deepgram `nova-3` (STT) · OpenAI `gpt-4o-mini` (LLM, with one tool call) · ElevenLabs `eleven_flash_v2_5` or Cartesia `sonic-3` (TTS) · Silero VAD. Same prompt, same tool, same providers, so a call on one can be compared with a call on the other.

| | LiveKit agent | Pipecat agent |
| --- | --- | --- |
| File | `agent.py` | `pipecat_agent.py` |
| Framework | LiveKit Agents 1.x | Pipecat 1.x (1.12 or later) |
| voiceToll hook | `voicetoll.livekit.attach(...)` + `metrics_collected` | `voicetoll.pipecat.Observer(...)` on the `PipelineWorker` |
| Talk to it | `console` (laptop) or `dev` (LiveKit Cloud playground) | browser at http://localhost:7860 (no account needed) or `local` (laptop) |
| Dependency group | `test-agent` | `test-agent-pipecat` |
| Events show as | source `livekit`, agent version `g3-v1` | source `pipecat`, agent version `g3-pipecat-v1` |

## 1. One-time setup

**Accounts and keys** (set a monthly spend cap on each; the week should cost a few dollars):

| Provider | What to create | Environment variable |
| --- | --- | --- |
| Deepgram | API key; a **separate project** for this agent | `DEEPGRAM_API_KEY` |
| OpenAI | API key in a **separate project** (e.g. `voicetoll-g3`) | `OPENAI_API_KEY` |
| ElevenLabs or Cartesia | API key | `ELEVEN_API_KEY` or `CARTESIA_API_KEY` |
| LiveKit Cloud (LiveKit agent `dev` mode only) | Free project: URL, API key, secret | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` |

The Pipecat agent reads the same keys (`ELEVEN_API_KEY` or `ELEVENLABS_API_KEY` both work). It needs no LiveKit account: its browser mode connects straight to your machine over WebRTC. Optional: `VT_ELEVEN_VOICE` / `VT_CARTESIA_VOICE` to pick a voice (stock voices by default).

Both agents can share the same provider projects and the same voiceToll project: reconciliation compares whole provider accounts, and the admin UI and report separate the two by source and agent version.

Separate provider projects matter: reconciliation compares voiceToll's estimate with the provider's bill for an account or project, so that account must carry only this agent's traffic.

**Reconciliation keys** (optional but recommended; separate from the agent's keys):

| Provider | Key | Compared |
| --- | --- | --- |
| OpenAI | Organization **admin** key (Costs API) plus the project id from step above | Dollars |
| ElevenLabs | API key with usage access | Characters |
| Deepgram | API key plus the project id | Audio seconds |

Copy `config/reconcile.example.yaml` to `config/reconcile.yaml`, fill in the project ids, and put the keys in `.env` as `VOICETOLL_RECON_OPENAI_KEY`, `VOICETOLL_RECON_ELEVENLABS_KEY`, `VOICETOLL_RECON_DEEPGRAM_KEY`. If you share an ElevenLabs account with other work, set that entry to `scope: shared`; its drift is then recorded but never alerts.

**Rate cards:** several voice-prices rates for these models are marked stale. Add entries to `config/rate_cards.yaml` with the rates on your plan (see `config/rate_cards.example.yaml`).

**Install** (from the repo root, PowerShell):

```powershell
uv sync --group test-agent            # LiveKit agent
uv sync --group test-agent-pipecat    # Pipecat agent (one group at a time: see below)
Copy-Item .env.example .env   # then fill in the keys above
```

Each agent's packages live in its own dependency group, so pass `--group test-agent` or `--group test-agent-pipecat` to `uv run` for the agent (shown below). The two groups are declared as conflicting in `pyproject.toml`, so uv resolves them separately and you switch between them rather than installing both: `uv run --group test-agent-pipecat ...` swaps the environment to the Pipecat stack and back again on the next `--group test-agent` run. A plain `uv sync` without a flag removes them, because it makes the environment match the lockfile exactly (it also removes anything added with `uv pip install`). The first sync after pulling this change updates `uv.lock`.

The Pipecat group includes PyAudio for `local` mode. If it fails to install on your machine, remove `local` from the extras in `pyproject.toml` and use the browser mode.

## 2. Every session

```powershell
# terminal 1: collector (SQLite is fine for G3). --env-file loads .env; the collector does not read it on its own
uv run --env-file .env voicetoll-collector serve

# terminal 2: talk to the agent
uv run --group test-agent python examples/test_agent/agent.py console   # laptop mic and speakers
# or: uv run --group test-agent python examples/test_agent/agent.py dev, then open the LiveKit Agents Playground and connect

# or the Pipecat agent (terminal 2)
uv run --group test-agent-pipecat python examples/test_agent/pipecat_agent.py         # then open http://localhost:7860 and click Connect
uv run --group test-agent-pipecat python examples/test_agent/pipecat_agent.py local   # laptop mic and speakers; use headphones
```

With the Pipecat agent in browser mode, each Connect starts a new call and closing the tab (or Disconnect) ends it; in `local` mode, Ctrl+C ends it. Either way the last log line is `voiceToll flush ok=...`, and the call id is printed as `voiceToll call id: pipecat-...`.

Have a realistic conversation: book, move and cancel an appointment, interrupt the agent now and then, and vary the length (a 1-minute call, a 5-minute call). Set `VT_TTS=cartesia` on some days to cover a second TTS provider. Alternate agents across the week (for example LiveKit in the morning, Pipecat in the afternoon) so both frameworks are covered by the same reconciliation.

End a call with Ctrl+C in the agent terminal (in console mode, the call lasts until you stop the program), then check the last lines of the agent log for `voiceToll flush ok=True`. Then check what voiceToll recorded; the call id is printed near the start of the call as `voiceToll call id: ...`:

```powershell
curl.exe -H "X-Voicetoll-Key: dev-key" http://localhost:4319/v1/sessions/<call-id>
```

Or open the built-in report at http://localhost:4319/report (enter `dev-key` once): the day's cost by provider and stage, every call, and each call's turns and latency. After the daily check, the same page shows estimate vs provider bill. Print it or save as PDF for the G3 write-up.

## 3. Check one call against the providers (no need to wait for the next day)

Daily reconciliation compares whole days. To check a single call, audit it against each provider's own request log:

```powershell
# a fixed-size test first: 30.0 s of audio to Deepgram, 500 characters to ElevenLabs, one prompt to OpenAI
uv run --env-file .env python examples/test_agent/known_input_test.py

# 5-10 minutes later (provider logs lag), with the call id it printed or any call id from the report
uv run --env-file .env voicetoll-collector audit-call known-input-20260930-101500
```

Or open the call in the report and press **Audit** (next to each call, or "Audit against providers" in the call view). Each provider shows voiceToll's figure, the provider's, the gap, and the provider's individual requests. `MATCHES` means within 5%. It uses the reconciliation keys. Anything else using the same provider accounts inside the call's window is counted too, so leave a few minutes between test calls. For ElevenLabs it shows both the characters of text and how far your quota moved; if those differ, the dashboard figure is in credits, not characters.

The Health page in `/admin` also shows, per app process, **Sent** (events the client saw acknowledged) next to **Arrived** (events the collector counted). They should match.

## 4. Every day (after midnight UTC)

```powershell
uv run --group test-agent python examples/test_agent/g3_daily.py   # checks yesterday
```

It prints voiceToll's estimate per provider, runs reconciliation, and appends the rows to `examples/test_agent/g3_log.csv`.

**After the first day with real provider numbers**, capture the providers' actual responses so the connectors are pinned to them:

```powershell
uv run --env-file .env voicetoll-collector recon-capture --day 2026-09-30
```

It saves one file per provider under `tests/fixtures/provider_usage/live/` (ids redacted) and prints what it read. If those numbers match each provider's dashboard for that day, commit the files; `uv run pytest tests/test_provider_contracts.py` then fails if a later change reads them differently. If they don't match, send me the printed line and the dashboard figure. Where the status is not `ok` (no key, no API data), copy the day's figure from the provider's dashboard into the `dashboard_usd` or `dashboard_units` column by hand.

## 5. G3 passes when

- 7 consecutive days of real use are logged.
- Every provider is within **5%** of the provider's own number for the week (dollars or units), or each gap is explained (for example a missing rate-card entry, then fixed and repriced).
- No events were dropped (`voiceToll flush ok=True` in the agent log at the end of each call).
- The p50/p99 overhead from `uv run pytest tests/test_overhead.py -s` on your machine is recorded next to the results.

The G3 write-up (estimate vs bill per provider, overhead numbers, what broke) is also the strongest public artifact for voiceToll.

## Notes

- Both agents pass provider and model names to voiceToll explicitly, so pricing does not depend on plugin attribute or class names.
- **Where the two frameworks' numbers come from** (worth recording in the G3 write-up): on Pipecat, STT seconds come from the STT service's own usage metrics (the audio streamed to it); on LiveKit, from the audio duration in LiveKit's STT metrics. Whether the two measure the same thing for streaming STT is exactly what G3 should show, so compare each against the Deepgram bill. TTS characters and LLM tokens should agree for similar conversations.
- To compare them in the admin UI: Cost view, stack by agent version (`g3-v1` vs `g3-pipecat-v1`); the Health view lists each source separately.
- Pipecat changes its API often; `pipecat_agent.py` follows Pipecat 1.12 (`PipelineWorker`, `WorkerRunner`, settings objects). If an import has moved in your installed version, adjust it and note it in the G3 log.
- The voiceToll client reads `VOICETOLL_*` settings from `.env` in each LiveKit job process; see `.env.example`.
- Plugin options follow LiveKit Agents 1.x. If an import or argument has changed in your installed version, adjust `agent.py` and note it in the G3 log.
