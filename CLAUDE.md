# CLAUDE.md

The current project state, reconciled across Claude and Codex, is in ../PROJECT_STATUS.md — local-only artifact: `../PROJECT_STATUS.md`. Read it before starting work. Update it after each round and append implementation details to ../EVOLUTION.md. Keep measurement scope explicit: STT-ready time is not voice-to-voice latency.

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A voice layer for DeepSeek Harness (DSH). A browser plugin adds a mic, spoken replies and an animated companion window to the DSH web GUI (:3080); a local FastAPI service, `bridge/voice_bridge.py` (:8765), does the speech work. DSH owns the conversational LLM; the bridge may run a small local language model solely for end-of-turn scoring. README.md is English and README.zh-CN.md is Chinese; detailed guides and most comments are Chinese. The original author's setup was Windows + WSL2 (OmniVoice) + Docker Desktop (DUIX); the current reproduction uses Linux, as recorded in docs/SETUP.md.

The next-stage design and acceptance criteria are in docs/DESIGN_ROADMAP.md. Preserve its distinction between proposals and measured results. Update both README languages together when changing behavior, defaults or published measurements.

The two halves ship separately:

- `bridge/`: Python. One large module (`voice_bridge.py`, ~2000 lines) plus `qq_bridge.py` (NapCat/OneBot push helper). Runs standalone.
- `dsh-plugin/`: TypeScript/React DSH client plugin. This copy is laid out as the monorepo package `@deepseek-ai/dsh-client-ui-voice` (`workspace:^` deps, tsconfig extends `../../../tsconfig.base.client.json`, tsdown config imports `../tsdown.client.ts`), so it only builds inside a deepseek-harness checkout at `packages/client/ui-voice`. What users normally install, `@beiyege-01/dsh-voice-ai-girlfriend` via `dsh plugin --profile web add github:beiyege-01/dsh-voice-ai-girlfriend-plugin`, lives in a separate repo built with `node build.mjs`. Edits here do not reach that package.

## Commands

CPU contracts are in bridge/tests; run `python -m unittest discover -s bridge/tests -v` with requirements-dev.txt. The standalone plugin has `npm run check` (11 tests, core typecheck and build). Both repositories have CI definitions; remote CI has not run until pushed. See CONTRIBUTING.md for exact check scope.

Bridge (tested with Python 3.12 and an NVIDIA GPU). `speech-to-speech==0.2.10` is HuggingFace's package; it brings torch and provides the Whisper STT and Qwen3-TTS handlers the bridge imports. Current installation steps and version snapshot are in docs/SETUP.md.

```bash
pip install -r bridge/requirements.txt
cp bridge/bridge-config.example.json bridge/bridge-config.json   # gitignored: local paths, tokens
# run from the REPO ROOT, see "Path resolution" below
python -m uvicorn voice_bridge:app --app-dir bridge --host 127.0.0.1 --port 8765
curl http://127.0.0.1:8765/api/health        # readiness may be false while background warmup runs
python bridge/smoke_tts.py --text "你好，我是小雅。" --out tts_out.wav
python bridge/smoke_stt.py --file some.wav --format wav
```

The STT smoke script requires `--file`; TTS defaults to a local `tts_out.wav`. The legacy launchers in `bridge/` (`start-bridge.cmd`, `start-all.cmd`, `start-full-stack.ps1`, `start-omnivoice-wsl.ps1`) hardcode the author's machine (`D:\speech-to-speech`, `E:\DSH\...`, a WSL2 IP, `venv-speech` beside the script) and are not portable.

OmniVoice runs as its own Gradio server that the bridge reaches through `gradio_client` at `omnivoice.base`, started with `omnivoice-demo --model k2-fsa/OmniVoice --port 9877 --no-asr`. FlashInfer acceleration (`apply_flashinfer`) needs OmniVoice from git master and, on Blackwell GPUs, `CUDA_HOME` pointing at a CUDA 13 nvcc.

The current standalone build is in `../dsh-voice-ai-girlfriend-plugin`: run `npm ci` and `npm run check`, then follow docs/SETUP.md to deploy its bundle. Keep `dsh-plugin/src` synchronized with `python scripts/sync_plugin.py --check`.

The alternative monorepo integration requires a matching deepseek-harness checkout: copy `dsh-plugin/*` to `<HARNESS>/packages/client/ui-voice`, register it in `tsconfig.client.json`, `packages/bundle/web-app/cordis.patch.yml` and `packages/bundle/web-app/package.json`, then:

```bash
pnpm install
pnpm exec tsc -b packages/client/ui-voice/tsconfig.json
pnpm --filter @deepseek-ai/dsh-client-ui-voice bundle
```

Restart `dsh web` after adding the plugin (the plugin list is fixed at startup; client-only changes need just a hard refresh). A loaded build logs `[ui-voice] loaded, bridge = http://127.0.0.1:8765` in the browser console.

## Architecture

One voice turn, end to end:

1. `voice/recorder.ts` captures audio through an AudioWorklet (`worklets/mic-capture.ts`) with a noise gate and ends an utterance after `minSilenceMs` of RMS silence (1800 ms, set in `MicButton.tsx`). Before speech starts it keeps only 0.5 s of pre-roll. When the bridge has `turn.enabled`, a 500 ms pause instead posts the utterance so far (at most 30 s) to `POST /api/turn`: "complete" ends the utterance at once, "incomplete" waits for 1200 ms of silence (`incompleteSilenceMs`). In the default text mode the bridge runs STT and returns the transcript with a "complete" answer, and `MicButton.tsx` skips `/api/stt` for that utterance. A 404 or error from `/api/turn` switches that recorder to plain silence timing. Otherwise PCM16 goes to `POST /api/stt`.
2. `sendText` in `client/index.ts` submits it with `session.prompt(...)`: mode `steer` (interrupt the running turn) or `queue`, per the ⚡ toggle.
3. `voice/reply-listener.tsx`, a hidden slot component, subscribes to the DSH chat view (`useChat`, read through `voice/chat-nodes.ts` because the node store shape changed between DSH rc.8 and 0.1.3). On every snapshot it cleans assistant text (`voice/clean.ts`), splits complete sentences (`voice/sentences.ts`) and chains one request per new sentence, serially: `POST /api/tts/stream`, or `POST /api/tts` when the bridge answers 404. A chain step ends when the sentence has fully arrived, not when it has played, so synthesis of sentence N+1 overlaps playback of N.
4. `voice/speaker.ts` plays clips as a FIFO on one 16 kHz AudioContext: whole WAVs (`speak`) or PCM streams (`speakStream`), whose chunks are scheduled back to back on the context clock as they arrive. Its `speaking` state drives the companion window and barge-in; `stop()` also cancels the stream being read, which makes the bridge stop synthesizing.
5. During playback the recorder streams PCM to `WS /api/vad` (server-side silero). `speech_start` calls `interruptReply()`: stop playback, abort the in-flight TTS fetch, skip the rest of that reply. The bridge stops synthesizing when the client disconnects.

Digital-human (DH) mode replaces steps 3 and 4 when the bridge switch, the local DH toggle and the companion window are all on and the bridge reports DUIX as `running`: the settled reply text (per user turn only the first and last assistant texts, the last after a 4 s debounce) goes to `POST /api/dh/speak`. The bridge splits it into `segment_chars` segments, synthesizes each, submits them to DUIX (`digital_human.duix_base`; code default :8383, example config :9000) and the companion window polls `/api/dh/status` to play the videos in order. No sentence TTS is spoken in this mode.

Plugin wiring (`dsh-plugin/src/client/index.ts`): everything registers through DSH slots. `conversation.input.dock` holds `VoiceToolbar` (mic, toggles, bridge status, voice/avatar/idle pickers); `conversation.composer.dock` the DeepSeek balance badge; `conversation.input.left` three mounts (reply listener, companion window, QQ bridge). They share one `ReplySpeaker`, one `CompanionController` and the injected `VoiceInjected` face (`contract.ts`). `src/index.ts`, the node half, is intentionally empty. All client state is browser localStorage under `s2s.voice.*`, including the bridge URL override `s2s.voice.bridge`.

Bridge internals (`bridge/voice_bridge.py`):

- `bridge-config.json` is read once at import; most module-level constants derive from it.
- `ModelManager` lazy-loads STT (FunASR Paraformer when `stt.backend` is `funasr`, otherwise speech-to-speech's `WhisperSTTHandler`) and Qwen3-TTS. A single `infer_lock` serializes all model work on the GPU; the OmniVoice `gradio_client` has its own lock.
- The TTS engine is picked per voice folder under `VOICES_DIR`: `ref_audio.wav` + `ref_text.txt` is a Qwen3-TTS clone (loads the heavy Qwen3 model), adding an empty `omnivoice.txt` makes it an OmniVoice clone, a `design.json` makes it an OmniVoice designed voice. All TTS output is resampled to 16 kHz mono int16. `/api/tts` returns a whole WAV; `/api/tts/stream` pushes raw PCM16 (`audio/pcm;format=s16le`) as the Qwen3 handler yields it (32 ms blocks; OmniVoice voices arrive in one piece). Qwen3 loads with the current voice's reference audio, and when the default voice is a Qwen3 voice the bridge loads and warms it at startup.
- DH is an asyncio queue worker (`_dh_worker` / `_dh_run`) that idles while the runtime switch is off. `POST /api/dh/enable` flips the switch, starts or stops the DUIX container through the docker CLI, and writes `digital_human.enabled` back into `bridge-config.json`.
- QQ: NapCat connects to `WS /api/qq/onebot`. The plugin's hidden `voice/qq-bridge.tsx` holds `WS /api/qq/ws`, injects inbound private messages with `sendText`, and sends settled replies back (`{type: 'reply'}`); the bridge pushes text plus a silk voice note (`pilk`) to QQ via `qq_bridge.py`.
- `/api/balance` needs `deepseek.api_key` or `DEEPSEEK_API_KEY`, otherwise it returns 503.
- `/api/turn` has two judges, picked by `turn.mode`, both warmed at startup when `turn.enabled` is true. `text` (default): STT under `infer_lock`, then `TextTurnJudge` from turn_detection.py scores the transcript with Qwen3-0.6B (`turn.text_model`, default `bridge/models/qwen3-0.6b`, bf16 on `turn.text_device`, about 30 ms on CPU) outside the lock. The score is P(end-of-turn token or 。？！) right after the text as an open user turn; `turn.text_threshold` is 0.2. `audio`: `TurnDetector` from turn_detection.py runs Smart Turn v3.2 with onnxruntime on the CPU (`turn.model`, default `bridge/models/smart-turn/smart-turn-v3.2-gpu.onnx`; onnxruntime is not in requirements.txt).

## Gotchas

- Path resolution in the bridge is mixed. `bridge-config.json`, `logs/bridge.log` and the silero model (`bridge/models/silero-vad/silero_vad_v4.jit`) resolve against `bridge/`. `stt.model_name` and the `media.*` dirs are relative to the process cwd, so run the bridge from the repo root. `voices_dir` defaults to the repository voices/ directory; relative voice paths resolve against the repo root. `VOICE_BRIDGE_CONFIG` can select an external JSON configuration.
- `reply-listener.tsx` freezes its history baseline at the first non-empty chat snapshot. Do not key it on assistant node status: in a brand-new session the first reply never shows up as `running`, it arrives already `settled`, while history arrives as one batch of user and assistant nodes.
- The DH toggle follows the bridge on mount (`/api/dh/status`) and only POSTs `/api/dh/enable` on a user click. Sentence TTS stays on unless the bridge switch is on and `duix.state` is `running`.
- `splitSentences` must stay prefix-stable: the listener tracks spoken sentences by index while the text streams, so a split that changes as more text arrives re-reads or drops sentences. Sentence TTS uses `cleanReplyText(text, max, true)` to keep line breaks as boundaries; the DH and QQ paths use the single-line form.
- End-of-turn detection is off by default. Text mode at threshold 0.2 lets about half of finished turns through at 0.5 s on chat-style Chinese text and lets 1.8% of unfinished ones through. On human read speech (FLEURS cmn) mid-sentence pauses mostly fall at commas, where the text alone cannot tell. With the plugin's 1200 ms fallback, utterances cut off mid-sentence rise from 1% (fixed 1.8 s) to about 9.5% at an average wait of 0.89 s; a plain 1.0 s timeout gives 6.6%, so on that corpus the check does about as well as a fixed timeout. Smart Turn (`audio` mode) treats nearly every pause as finished on human Mandarin (95% of mid-sentence pauses); Pipecat's 929 Chinese test clips are all Google TTS.
- The recorder used to keep all silence since the last utterance and send it with the next one. After about 30 s of silence `/api/stt` answered 422 and the utterance was dropped without any message. The 0.5 s pre-roll limit fixes this.
- Barge-in used to lose the first words: silero sends `speech_start` only after 384 ms of voice (about 0.45 s after onset in tests), and `onVadSpeechStart` cleared the buffer, so "我想换个话题聊聊" said over a reply came out as "换个话题聊聊". While a reply plays the recorder now keeps the last 1.0 s (`BARGE_IN_KEEP_BYTES`) and starts the utterance from it; when the reply ends on its own it keeps 0.5 s as pre-roll. The kept second can include TTS echo that got past AEC and the noise gate (not tested with a real mic).
- DH playback in `companion.tsx`: the window polls `/api/dh/status` every 1 s while the task state is `tts` or `generating` and every 4 s otherwise, and polls once more as soon as its video queue runs dry; with a fixed 4 s poll each finished segment waited up to 4 s before playing. The bridge keeps the last task's state until the next one starts, so the first poll after a page load records an already finished task (`done`, `error`, `discarded`) as handled instead of playing it again. DUIX runs one task at a time and a submitted segment cannot be cancelled, so a new reply waits for the segment in flight.
- `qwen_tts` registers a "default" RoPE init that reads `config.rope_theta`; with transformers 5.20 the Mimi speech tokenizer keeps it in `config.rope_parameters`, and `FasterQwen3TTS.from_pretrained` fails with `'MimiConfig' object has no attribute 'rope_theta'`. `_patch_qwen_tts_rope()` in the bridge wraps that init before the handler loads; on transformers 4.x it does nothing.
- Streaming Qwen3-TTS does not make the first word of a reply faster than OmniVoice on one GPU: the first sentence is synthesized while the local LLM is still generating the rest of the reply, and the two processes time-slice the card (first audible chunk 0.23 to 0.44 s, later sentences 0.10 to 0.27 s). The model also starts sentences with a random stretch of silence that the handler trims but must generate first, so first-chunk times fall into 0.1 / 0.18 / 0.26 s steps and occasionally exceed 1 s. Passing `append_silence=False` made no measurable difference. Numbers in `../EVOLUTION.md` section 8.
- `stream_tail.py` is an experimental PCM wrapper, not imported by the running bridge. Its bounded buffer and end-only crop passed replay contracts, but low-amplitude final words can still be cut; do not enable it from the ASR checks alone. The 104-output 0.6B/1.7B comparison retained 0.6B. See docs/TTS_MODELS.md and docs/experiments/2026-10-20-stream-guard/RESULTS.md for timing scope, resource costs and remaining playback checks.
- The 2026-10-19 fixes and the end-of-turn hook above are uncommitted, here and in a local clone of the plugin repo (`../dsh-voice-ai-girlfriend-plugin`). The published plugin has none of them.
- Every open DSH tab mounts its own listener, so two tabs read each reply twice.
- The deployed stream endpoint still starts cancelled queued jobs and checks cancellation after the handler emits audible PCM. The isolated candidate in scripts/tts_cancel_lifecycle.py cancels waiting tasks and checks each raw model chunk before leading-silence filtering. Running workers keep the GPU lock until thread exit. It is not imported by production. V2 adds an eight-element queue, waits through repeated task cancellation for the thread to exit, forwards cleanup errors and explicitly closes disconnected responses (ASGI 2.3/2.4 CPU contracts). Bounded queues can keep the inference lock while a sender stalls; no send-stall deadline exists yet. See docs/experiments/2026-10-21-cancel-recovery/RESULTS.md and resolve the deadline/lock budget before deployment.
- On Windows the venv launcher makes `python -m uvicorn` appear as two python processes; check the single listener on :8765 instead of counting processes.

Our own change history (reproduction on apex, every fix since, with debugging steps, root causes and measurements) is in `../EVOLUTION.md`, outside this repo. Append a dated section there after each round of work.

`docs/INTEGRATION_LOG.md` is the author's dev log (milestones T1 to T8, bug root causes) and explains most of the defensive code paths. Comments in the code carry the same dated reasoning, often in Chinese; follow that style when editing nearby code.

Published-documentation copies of the latest full DSH latency data are in docs/experiments, with methods in docs/EXPERIMENTS.md. The local detailed record is `../samples/2026-10-20-e2e/RESULTS.md`; use `../PROJECT_STATUS.md` as the current progress entry point. Historical measurements used the deployed bundle, not the subsequently refactored local candidate.
