(() => {
  // dsh-plugin/src/client/voice/speaker.ts
  var STREAM_LEAD_SEC = 0.05;
  var PcmClip = class {
    done = false;
    parts = [];
    carry = null;
    // low byte left over from an odd-length read
    wake = null;
    push(bytes) {
      let start2 = 0;
      const total = bytes.byteLength + (this.carry === null ? 0 : 1);
      const out = new Float32Array(total >> 1);
      let i = 0;
      if (this.carry !== null && bytes.byteLength > 0) {
        out[i++] = ((bytes[0] << 8 | this.carry) << 16 >> 16) / 32768;
        start2 = 1;
        this.carry = null;
      }
      const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
      for (let p = start2; p + 1 < bytes.byteLength; p += 2) out[i++] = view.getInt16(p, true) / 32768;
      if ((bytes.byteLength - start2) % 2 === 1) this.carry = bytes[bytes.byteLength - 1];
      if (i > 0) this.parts.push(out.subarray(0, i).slice());
      this.notify();
    }
    end() {
      this.done = true;
      this.notify();
    }
    /** Everything received since the last call as one block, or null if nothing new. */
    take() {
      if (this.parts.length === 0) return null;
      if (this.parts.length === 1) return this.parts.pop();
      const out = new Float32Array(this.parts.reduce((n, p) => n + p.length, 0));
      let at = 0;
      for (const p of this.parts) {
        out.set(p, at);
        at += p.length;
      }
      this.parts = [];
      return out;
    }
    /** Resolves when more audio arrives or the stream ends. */
    wait() {
      return new Promise((resolve) => {
        this.wake = resolve;
      });
    }
    notify() {
      const wake = this.wake;
      this.wake = null;
      wake?.();
    }
  };
  var ReplySpeaker = class {
    ctx = null;
    queue = [];
    current = null;
    /** Started or scheduled sources, each with the callback that settles its playback. */
    sources = /* @__PURE__ */ new Map();
    readers = /* @__PURE__ */ new Set();
    drainRunning = false;
    gen = 0;
    disposed = false;
    listeners = /* @__PURE__ */ new Set();
    /** True while a reply is being read (playing or waiting in the queue). */
    get speaking() {
      return this.current !== null || this.queue.length > 0;
    }
    /** Subscribe to speaking-state changes; returns the unsubscribe function. */
    subscribe(listener) {
      this.listeners.add(listener);
      return () => {
        this.listeners.delete(listener);
      };
    }
    emit() {
      for (const listener of this.listeners) listener();
    }
    /** Enqueue one clip for playback (order preserved; plays after earlier clips). */
    speak(wav) {
      if (this.disposed) return;
      this.queue.push({ wav });
      this.emit();
      void this.drain();
    }
    /**
     * Enqueue a streamed clip (raw mono PCM16 little-endian) and read it to the
     * end. Playback starts once earlier clips are done and the first chunk is
     * in. Resolves when the stream has been fully read (or was cancelled), not
     * when it has finished playing, so the caller can fetch the next sentence.
     */
    async speakStream(body, sampleRate = 16e3) {
      const reader = body.getReader();
      if (this.disposed) {
        void reader.cancel().catch(() => {
        });
        return;
      }
      const gen = this.gen;
      const pcm = new PcmClip();
      this.queue.push({ pcm, sampleRate });
      this.emit();
      void this.drain();
      this.readers.add(reader);
      try {
        for (; ; ) {
          const { done, value } = await reader.read();
          if (done || gen !== this.gen) break;
          pcm.push(value);
        }
      } catch {
      } finally {
        this.readers.delete(reader);
        pcm.end();
        if (gen !== this.gen) void reader.cancel().catch(() => {
        });
      }
    }
    context() {
      if (this.ctx !== null) return this.ctx;
      try {
        this.ctx = new AudioContext({ sampleRate: 16e3 });
      } catch {
        this.ctx = new AudioContext();
      }
      return this.ctx;
    }
    async drain() {
      if (this.drainRunning) return;
      this.drainRunning = true;
      try {
        const ctx = this.context();
        if (ctx.state === "suspended") {
          try {
            await ctx.resume();
          } catch {
          }
        }
        while (this.queue.length > 0 && !this.disposed) {
          const clip = this.queue.shift();
          this.current = clip;
          if ("wav" in clip) await this.playWav(ctx, clip.wav);
          else await this.playPcm(ctx, clip.pcm, clip.sampleRate);
          this.current = null;
        }
      } finally {
        this.current = null;
        this.drainRunning = false;
        this.emit();
      }
    }
    async playWav(ctx, wav) {
      const gen = this.gen;
      let buffer;
      try {
        buffer = await ctx.decodeAudioData(wav);
      } catch {
        return;
      }
      if (this.disposed || gen !== this.gen) return;
      await this.schedule(ctx, buffer, 0);
    }
    /** Schedule chunks back to back as they arrive; resolves when the last one has played. */
    async playPcm(ctx, pcm, sampleRate) {
      const gen = this.gen;
      let at = 0;
      let last = null;
      for (; ; ) {
        if (this.disposed || gen !== this.gen) return;
        const samples = pcm.take();
        if (samples === null) {
          if (pcm.done) break;
          await pcm.wait();
          continue;
        }
        const buffer = ctx.createBuffer(1, samples.length, sampleRate);
        buffer.copyToChannel(samples, 0);
        at = Math.max(at, ctx.currentTime + STREAM_LEAD_SEC);
        last = this.schedule(ctx, buffer, at);
        at += buffer.duration;
      }
      if (last !== null) await last;
    }
    schedule(ctx, buffer, when) {
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(ctx.destination);
      return new Promise((resolve) => {
        const finish = () => {
          this.sources.delete(source);
          resolve();
        };
        this.sources.set(source, finish);
        source.onended = finish;
        source.start(when);
      });
    }
    /** Stop playback immediately, cancel the stream being read and clear the queue. */
    stop() {
      this.gen += 1;
      for (const [source, finish] of this.sources) {
        try {
          source.stop();
        } catch {
        }
        finish();
      }
      for (const reader of this.readers) void reader.cancel().catch(() => {
      });
      this.readers.clear();
      for (const clip of [this.current, ...this.queue]) {
        if (clip !== null && "pcm" in clip) clip.pcm.end();
      }
      this.current = null;
      this.queue = [];
      this.emit();
    }
    /** Release the AudioContext (plugin teardown). */
    dispose() {
      this.disposed = true;
      this.stop();
      if (this.ctx !== null) {
        void this.ctx.close().catch(() => {
        });
        this.ctx = null;
      }
      this.listeners.clear();
    }
  };

  // scripts/player_validation_client.js
  var originalFetch = window.fetch.bind(window);
  var fake = { ctx: null, destination: null };
  var experiment = { current: null, player: [], e2e: [], activeTts: 0, policy: "baseline" };
  var sourceRecords = /* @__PURE__ */ new WeakMap();
  var contextIds = /* @__PURE__ */ new WeakMap();
  var contextCounter = 0;
  var now = () => performance.now();
  var sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  navigator.mediaDevices.getUserMedia = async () => {
    if (!fake.ctx) {
      fake.ctx = new AudioContext({ sampleRate: 16e3 });
      fake.destination = fake.ctx.createMediaStreamDestination();
    }
    await fake.ctx.resume();
    return fake.destination.stream;
  };
  var start = AudioBufferSourceNode.prototype.start;
  AudioBufferSourceNode.prototype.start = function(when, ...rest) {
    const current = experiment.current;
    if (current && this.context !== fake.ctx && this.buffer) {
      const values = this.buffer.getChannelData(0);
      let first = 0;
      while (first < values.length && Math.abs(values[first]) < 0.01) first++;
      const at = now() + Math.max(0, (when || 0) - this.context.currentTime) * 1e3;
      const record = {
        called_at: now(),
        scheduled_start: at,
        scheduled_end: at + this.buffer.duration * 1e3,
        first_voiced_at: first < values.length ? at + first / this.buffer.sampleRate * 1e3 : null,
        frames: values.length,
        sample_rate: this.buffer.sampleRate,
        context_state: this.context.state,
        segment: current.segment ?? null,
        context_id: contextIds.get(this.context) ?? (() => {
          const id = ++contextCounter;
          contextIds.set(this.context, id);
          return id;
        })()
      };
      sourceRecords.set(this, record);
      current.sources.push(record);
      this.addEventListener("ended", () => {
        record.ended_at = now();
      }, { once: true });
    }
    return start.call(this, when, ...rest);
  };
  var stop = AudioBufferSourceNode.prototype.stop;
  AudioBufferSourceNode.prototype.stop = function(...args) {
    const record = sourceRecords.get(this);
    if (record) record.stopped_at = now();
    return stop.apply(this, args);
  };
  window.fetch = async (input, init) => {
    let url = typeof input === "string" ? input : input?.url || String(input);
    const match = url.match(/\/api\/(turn|stt|tts(?:\/stream)?)\b/);
    const current = experiment.current;
    if (!match || !current) return originalFetch(input, init);
    const record = { api: match[1], at: now() };
    current.events.push(record);
    if (match[1] === "tts/stream") {
      try {
        record.text = JSON.parse(init.body).text;
      } catch {
      }
      url = "/__voice_test__/tts/stream?policy=" + experiment.policy;
      if (experiment.ttsHeaders) {
        init = { ...init, headers: { ...init.headers, ...experiment.ttsHeaders(current) } };
      }
      experiment.activeTts++;
    }
    try {
      const response2 = await originalFetch(url, init);
      record.status = response2.status;
      record.headers_at = now();
      record.stream_id = response2.headers.get("X-Test-Stream");
      if (match[1] !== "tts/stream") {
        response2.clone().json().then((value) => {
          record.result = value;
          record.done = now();
        }).catch(() => {
        });
        return response2;
      }
      const reader = response2.body.getReader();
      let finished = false;
      const finish = () => {
        if (!finished) {
          finished = true;
          experiment.activeTts--;
          record.done = now();
        }
      };
      return new Response(new ReadableStream({
        async pull(controller) {
          try {
            const { done, value } = await reader.read();
            if (done) {
              finish();
              controller.close();
            } else controller.enqueue(value);
          } catch (error) {
            finish();
            controller.error(error);
          }
        },
        async cancel(reason) {
          record.cancelled_at = now();
          finish();
          await reader.cancel(reason);
        }
      }), { status: response2.status, headers: response2.headers });
    } catch (error) {
      record.error = String(error);
      if (match[1] === "tts/stream") experiment.activeTts--;
      throw error;
    }
  };
  var OriginalWebSocket = window.WebSocket;
  window.WebSocket = class extends OriginalWebSocket {
    constructor(url, protocols) {
      super(url, protocols);
      if (String(url).includes("/api/vad")) this.addEventListener("message", (e) => {
        if (experiment.current && String(e.data).includes("speech_start")) {
          experiment.current.events.push({ api: "vad_start", at: now() });
        }
      });
    }
  };
  function trial(kind, key, policy) {
    experiment.policy = policy;
    return experiment.current = { kind, key, policy, started: now(), events: [], sources: [] };
  }
  async function waitUntil(check, seconds = 60) {
    const until = now() + seconds * 1e3;
    while (!check()) {
      if (now() > until) throw new Error("test timeout");
      await sleep(50);
    }
  }
  function summarize(record) {
    const voiced = record.sources.map((s) => s.first_voiced_at).filter((t) => t !== null);
    record.first_voice_at = voiced.length ? Math.min(...voiced) : null;
    record.last_ended_at = Math.max(0, ...record.sources.map((s) => s.ended_at || 0));
    record.finished = now();
    if (record.input_end && record.first_voice_at) record.voice_to_voice_ms = record.first_voice_at - record.input_end;
    return record;
  }
  async function save(kind) {
    await originalFetch("/__voice_test__/results/" + kind, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(experiment[kind])
    });
  }
  async function say(parts) {
    if (!fake.ctx) throw new Error("\u8BF7\u5148\u5F00\u542F DSH \u6D4B\u8BD5\u9EA6\u514B\u98CE");
    let at = fake.ctx.currentTime + 0.2;
    const marks = [];
    for (const part of parts) {
      if (typeof part === "number") {
        at += part / 1e3;
        continue;
      }
      const raw = await originalFetch("/__voice_test__/audio/" + part + ".wav").then((r) => r.arrayBuffer());
      const decoded = await fake.ctx.decodeAudioData(raw);
      const data = decoded.getChannelData(0);
      let first = 0, last = data.length - 1;
      while (first < data.length && Math.abs(data[first]) < 0.01) first++;
      while (last > first && Math.abs(data[last]) < 0.01) last--;
      const buffer = fake.ctx.createBuffer(1, last - first + 1, decoded.sampleRate);
      buffer.copyToChannel(data.slice(first, last + 1), 0);
      marks.push({ buffer, gap: at });
      at += buffer.duration;
    }
    const base = fake.ctx.currentTime + 0.2;
    const initial = marks[0].gap;
    const performanceBase = now(), contextBase = fake.ctx.currentTime;
    for (const mark of marks) {
      const source = fake.ctx.createBufferSource();
      source.buffer = mark.buffer;
      source.connect(fake.destination);
      const startAt = base + mark.gap - initial;
      source.start(startAt);
      mark.start = performanceBase + (startAt - contextBase) * 1e3;
      mark.end = mark.start + mark.buffer.duration * 1e3;
      delete mark.buffer;
      delete mark.gap;
    }
    experiment.current.input_marks = marks;
    experiment.current.input_end = marks.at(-1).end;
  }
  function installPanel() {
    const panel = document.createElement("section");
    panel.id = "voice-validation-panel";
    panel.style.cssText = "position:fixed;right:12px;top:12px;z-index:2147483647;width:440px;padding:16px;background:#102b3a;color:white;border:2px solid #57d9a3;font:13px system-ui;max-height:55vh;overflow:auto;border-radius:10px";
    panel.innerHTML = '<b>apex \u64AD\u653E\u5668\u4E0E\u5B8C\u6574\u94FE\u8DEF\u5B9E\u9A8C</b><p>\u5408\u6210\u8F93\u5165\uFF1B\u4F7F\u7528\u771F\u5B9E ReplySpeaker\u3002\u4EBA\u5DE5\u542C\u68C0\u53E6\u8BB0\u3002</p><button id="run-player">\u8FD0\u884C31\u9879\u64AD\u653E\u5668\u9A8C\u8BC1</button> <button id="run-e2e">\u8FD0\u884C17\u8F6E\u5B8C\u6574\u94FE\u8DEF</button><pre id="validation-status" style="white-space:pre-wrap"></pre>';
    document.body.append(panel);
    const output = panel.querySelector("pre");
    const note = (text) => {
      output.textContent += text + "\n";
      output.scrollTop = output.scrollHeight;
    };
    const busy = (value) => panel.querySelectorAll("button").forEach((b) => {
      b.disabled = value;
    });
    panel.querySelector("#run-player").onclick = async () => {
      busy(true);
      const speaker = new ReplySpeaker();
      try {
        const cases = [...Array.from({ length: 6 }, (_, i) => "soft-" + i), "long-tail", "internal-pause", "late-packet"];
        for (const key of cases) for (const policy of ["baseline", "guard99", "guard3"]) {
          const record = trial("player", key, policy);
          const response2 = await originalFetch("/__voice_test__/fixture/" + key + "?policy=" + policy);
          record.stream_id = response2.headers.get("X-Test-Stream");
          await speaker.speakStream(response2.body);
          await waitUntil(() => !speaker.speaking);
          experiment.player.push(summarize(record));
          await save("player");
          note(key + " " + policy + " \u5DF2\u7ED3\u675F\uFF1A" + Math.round(record.last_ended_at - record.started) + "ms");
        }
        for (const policy of ["baseline", "guard3"]) {
          const record = trial("player", "queued-two-segments", policy);
          for (const key of ["long-tail", "normal-0"]) {
            const response2 = await originalFetch("/__voice_test__/fixture/" + key + "?policy=" + policy);
            await speaker.speakStream(response2.body);
          }
          await waitUntil(() => !speaker.speaking);
          experiment.player.push(summarize(record));
          await save("player");
          note("\u8FDE\u7EED\u4E24\u6BB5 " + policy + " \u5DF2\u7ED3\u675F");
        }
        for (const policy of ["baseline", "guard3"]) {
          const record = trial("player", "cancel-stream", policy);
          const response2 = await originalFetch("/__voice_test__/fixture/long-tail?policy=" + policy);
          const reading = speaker.speakStream(response2.body);
          await waitUntil(() => record.sources.length > 0);
          await sleep(250);
          record.stop_requested_at = now();
          speaker.stop();
          await reading;
          await sleep(500);
          record.speaking_after_stop = speaker.speaking;
          experiment.player.push(summarize(record));
          await save("player");
          note("\u53D6\u6D88 " + policy + " \u5B8C\u6210");
        }
        note("\u64AD\u653E\u5668\u5B8C\u6210\uFF1A" + experiment.player.length + "/31");
      } catch (error) {
        note("\u64AD\u653E\u5668\u5931\u8D25\uFF1A" + error);
        experiment.player.push({ error: String(error) });
        await save("player");
      } finally {
        speaker.stop();
        experiment.current = null;
        busy(false);
      }
    };
    panel.querySelector("#run-e2e").onclick = async () => {
      busy(true);
      try {
        if (!fake.ctx) throw new Error("\u5148\u70B9\u51FB DSH \u8BED\u97F3\u8F93\u5165\uFF0C\u6D4B\u8BD5\u6D41\u5C06\u66FF\u4EE3\u9EA6\u514B\u98CE");
        const cases = [["input-0"], ["input-1"], ["input-2"], ["input-3", 600, "input-4"]];
        const sequence = [{ key: "first-new-session", policy: "baseline", parts: cases[0], warmup: true }];
        for (let repeat = 0; repeat < 2; repeat++) for (let i = 0; i < cases.length; i++) {
          for (const policy of (i + repeat) % 2 ? ["guard3", "baseline"] : ["baseline", "guard3"]) {
            sequence.push({ key: "case-" + i + "-repeat-" + repeat, policy, parts: cases[i] });
          }
        }
        for (const item of sequence) {
          const record = trial("e2e", item.key, item.policy);
          record.warmup = Boolean(item.warmup);
          await say(item.parts);
          await waitUntil(() => {
            const ends = record.sources.map((s) => s.ended_at || 0);
            const tts = record.events.filter((e) => e.api === "tts/stream");
            return ends.length > 0 && ends.every(Boolean) && experiment.activeTts === 0 && now() > Math.max(...ends) + 900 && tts.every((e) => e.done && now() > e.done + 1200);
          }, 75);
          experiment.e2e.push(summarize(record));
          await save("e2e");
          note(item.key + " " + item.policy + " \u9996\u58F0\uFF1A" + Math.round(record.voice_to_voice_ms) + "ms");
        }
        note("\u5B8C\u6574\u94FE\u8DEF\u5B8C\u6210\uFF1A" + experiment.e2e.length + "/17");
      } catch (error) {
        note("\u5B8C\u6574\u94FE\u8DEF\u5931\u8D25\uFF1A" + error);
        experiment.e2e.push({ error: String(error), partial: experiment.current });
        await save("e2e");
      } finally {
        experiment.current = null;
        busy(false);
      }
    };
  }
  if (!document.querySelector("script[data-xvector]")) {
    if (document.body) installPanel();
    else document.addEventListener("DOMContentLoaded", installPanel, { once: true });
  }

  // scripts/cancel_recovery_client.js
  experiment.interruption = [];
  experiment.ttsHeaders = (r) => ({
    "X-Experiment-Trial": r.key,
    "X-Experiment-Segment": String(r.segment),
    "X-Experiment-Seed": String(r.seed)
  });
  var number = "\u7F16\u53F7\u662F\u4E00\u96F6\u96F6\u4E8C\u96F6\u96F6\u4E09\u96F6\u96F6\u3002";
  var long = "\u6211\u4EEC\u5148\u628A\u4ECA\u5929\u7684\u4E8B\u60C5\u6162\u6162\u7406\u6E05\u695A\u3002\u7B2C\u4E00\u4EF6\u4E8B\u662F\u4FDD\u5B58\u5DF2\u7ECF\u5B8C\u6210\u7684\u8BB0\u5F55\uFF0C\u7B2C\u4E8C\u4EF6\u4E8B\u662F\u68C0\u67E5\u660E\u5929\u9700\u8981\u51C6\u5907\u7684\u6750\u6599\u3002\u5982\u679C\u4F60\u6682\u65F6\u8FD8\u6CA1\u6709\u60F3\u597D\uFF0C\u4E5F\u53EF\u4EE5\u5148\u4F11\u606F\u4E00\u4E0B\uFF0C\u7B49\u7CBE\u795E\u597D\u4E00\u70B9\u7684\u65F6\u5019\u518D\u7EE7\u7EED\u3002\u6700\u540E\u8BB0\u5F97\u628A\u684C\u4E0A\u7684\u5C0F\u76D2\u5B50\u6536\u597D\uFF0C\u6211\u4EEC\u660E\u5929\u518D\u4E00\u8D77\u770B\u770B\u5427\u3002";
  async function response(text, signal) {
    const res = await window.fetch("/api/tts/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
      signal
    });
    if (!res.ok) throw new Error("Private TTS " + res.status);
    return res;
  }
  async function burst(arm, repeat) {
    const r = trial("interruption", `browser-burst-${repeat}-${arm}`, arm);
    const speaker = new ReplySpeaker();
    r.stops = [];
    try {
      for (const segment of [1, 2]) {
        r.segment = segment;
        r.seed = segment === 1 ? 4409 : 5501;
        const control = new AbortController();
        const previous = r.sources.length;
        const res2 = await response(segment === 1 ? number : long, control.signal);
        const reading = speaker.speakStream(res2.body);
        if (segment === 1) await sleep(20);
        else {
          await waitUntil(() => r.sources.length > previous, 10);
          await sleep(80);
        }
        const stopped = now();
        speaker.stop();
        control.abort();
        await reading;
        await sleep(60);
        r.stops.push({
          segment,
          at: stopped,
          gap_end: now(),
          sources_before: previous,
          late_sources: r.sources.filter((s) => s.called_at > stopped).length
        });
      }
      r.segment = 3;
      r.seed = 6501;
      r.followup_requested_at = now();
      const res = await response("\u597D\u7684\uFF0C\u6211\u4EEC\u660E\u5929\u518D\u804A\u5427\u3002");
      await speaker.speakStream(res.body);
      await waitUntil(() => !speaker.speaking, 20);
      let state;
      const deadline = now() + 15e3;
      do {
        state = await originalFetch("/__voice_test__/state").then((x) => x.json());
        if (now() > deadline) throw new Error("Private worker did not drain");
        if (state.pending) await sleep(50);
      } while (state.pending);
      r.server = state.streams.filter((s) => s.trial === r.key);
      r.max_active = state.max_active;
      const final = r.sources.filter((s) => s.called_at >= r.followup_requested_at);
      r.followup_samples = final.reduce((n, s) => n + s.frames, 0);
      r.followup_received = r.server.find((s) => s.segment === "3").sent_samples;
      r.followup_to_voice_ms = Math.min(...final.map((s) => s.first_voiced_at).filter((x) => x !== null)) - r.followup_requested_at;
      r.late_sources = r.stops.reduce((n, s) => n + s.late_sources, 0);
      if (r.late_sources || r.followup_samples !== r.followup_received || r.max_active !== 1) throw new Error("Burst invariant failed");
      experiment.interruption.push(summarize(r));
      await save("interruption");
      return r;
    } finally {
      speaker.stop();
    }
  }
  function install() {
    const panel = document.createElement("section");
    panel.id = "recovery-panel";
    panel.style.cssText = "position:fixed;right:12px;top:12px;z-index:2147483647;width:450px;padding:16px;background:#102b3a;color:white;border:2px solid #57d9a3;font:13px system-ui;max-height:40vh;overflow:auto;border-radius:10px";
    panel.innerHTML = '<b>\u53D6\u6D88\u6062\u590D \xB7 \u771F\u5B9E\u64AD\u653E\u5668\u9A8C\u8BC1</b><p>\u9ED8\u8BA4ICL\uFF1B\u751F\u4EA7\u4E0E\u88C1\u526A\u4E0D\u53D8\uFF1B\u6D4B\u8BD5\u9EA6\u514B\u98CE\u5173\u95ED\u3002</p><button>\u8FD0\u884C4\u7EC4\u8FDE\u7EED\u53D6\u6D88</button><pre style="white-space:pre-wrap"></pre>';
    document.body.append(panel);
    const button = panel.querySelector("button"), log = panel.querySelector("pre");
    const note = (text) => {
      log.textContent += text + "\n";
      panel.scrollTop = panel.scrollHeight;
    };
    button.onclick = async () => {
      button.disabled = true;
      try {
        for (const repeat of [0, 1]) for (const arm of repeat ? ["fixed", "v1"] : ["v1", "fixed"]) {
          const r = await burst(arm, repeat);
          note(r.key + ": \u665Asource=" + r.late_sources + "\uFF0C\u540E\u7EE7\u91C7\u6837=" + r.followup_samples + "/" + r.followup_received + "\uFF0C\u9996\u58F0=" + Math.round(r.followup_to_voice_ms) + "ms");
        }
        note("4\u7EC4\u8FDE\u7EED\u53D6\u6D88\u5B8C\u6210\uFF1B8\u6B21stop\uFF1B\u540E\u53F0\u4E0E\u91C7\u6837\u6838\u9A8C\u901A\u8FC7");
      } catch (error) {
        note("\u5931\u8D25\u5DF2\u4FDD\u7559\uFF1A" + error);
        experiment.interruption.push({ error: String(error), partial: experiment.current });
        await save("interruption");
      } finally {
        experiment.current = null;
        button.disabled = false;
      }
    };
  }
  if (document.body) install();
  else document.addEventListener("DOMContentLoaded", install, { once: true });
})();
