/**
 * MicRecorder: mic capture with silence-based utterance endpointing.
 *
 * Reuses the embedded mic-capture AudioWorklet (16 kHz PCM16 chunks at ~40 ms
 * plus per-chunk RMS). The main thread accumulates chunks and, once speech has
 * started, ends the utterance after `minSilenceMs` of RMS below threshold
 * (or `maxUtteranceMs` hard cap), then hands the concatenated PCM16 to
 * `onUtterance`, then resets for the next utterance. MicButton keeps capture
 * active until the user switches listening off.
 *
 * Semantic endpointing (2026-10-19): with `turnCheck` set, a short pause
 * (`turnSilenceMs`) sends the utterance so far to the bridge's end-of-turn
 * check (/api/turn: STT + small LLM, or Smart Turn). "Complete" ends the
 * utterance right away, passing on the transcript when the check made one;
 * "incomplete" keeps listening until speech resumes or `incompleteSilenceMs`
 * of silence. If the check is unavailable the recorder falls back to
 * `minSilenceMs`.
 */
import { MIC_CAPTURE_WORKLET_SOURCE } from '../worklets/mic-capture.ts'
import type { VadStream } from '../bridge.ts'

const DEFAULT_MIN_SILENCE_MS = 1800
const DEFAULT_TURN_SILENCE_MS = 500
const DEFAULT_INCOMPLETE_SILENCE_MS = 1800
const DEFAULT_MAX_UTTERANCE_MS = 30000
/** Linear amplitude threshold (~ -40 dBFS). */
const DEFAULT_RMS_THRESHOLD = 0.01
/** At most the last 30 s go to the check, the bridge's STT limit (16 kHz
 *  PCM16). Smart Turn keeps only the last 8 s of that itself. */
const TURN_WINDOW_BYTES = 30 * 16000 * 2
/** Audio kept from before speech starts (0.5 s), so the first syllable is not clipped. */
const PRE_ROLL_BYTES = 16000
/** Audio kept while listening for a barge-in (1.0 s). The bridge VAD sends
 *  speech_start only after ~0.4 s of voice; those words open the next utterance. */
const BARGE_IN_KEEP_BYTES = 32000

export interface MicRecorderOptions {
  /** Silence that ends an utterance when no semantic check is available. */
  minSilenceMs?: number
  /** Semantic end-of-turn check; null result = unavailable (see class doc).
   *  `text` is the transcript of `pcm16` when the check ran STT. */
  turnCheck?: (pcm16: ArrayBuffer) => Promise<{ complete: boolean; text?: string } | null>
  /** Pause before asking `turnCheck`. */
  turnSilenceMs?: number
  /** Longest silence after `turnCheck` said "incomplete" (or has not answered). */
  incompleteSilenceMs?: number
  maxUtteranceMs?: number
  rmsThreshold?: number
  /** Streamed to the bridge's silero VAD while a reply is playing; its
   *  `speech_start` is the barge-in trigger. When absent, the recorder falls
   *  back to the RMS heuristics below (interruptThreshold / hold / confirm). */
  vad?: VadStream
  /** RMS threshold for barge-in detection during playback (fallback path). */
  interruptThreshold?: number
  /** Sustained above-threshold time (ms) before a barge-in fires (fallback). */
  interruptHoldMs?: number
  /** After a barge-in fires, keep requiring the signal for this long before
   *  accumulating — a false alarm never becomes an utterance (fallback). */
  interruptConfirmMs?: number
  /** Noise gate threshold in dBFS (0 or undefined = disabled). Quiet ambient
   *  audio below this level is faded out of the SENT mic stream — mirrors the
   *  original project's worklet gate (the attack/hold/release envelope
   *  already lives in the embedded worklet; this just arms it). */
  noiseGateDb?: number
  /** Called once when speech is detected while a reply is playing (barge-in);
   *  the recorder then switches back to normal accumulation so the user's
   *  ongoing speech becomes the next utterance. */
  onSpeechInterrupt?: () => void
  /** Called when the "currently capturing speech" flag flips (true = voice
   *  level above threshold detected, false = utterance ended). The UI uses it
   *  to show the recording state without per-chunk re-renders. */
  onSpeakingChange?: (speaking: boolean) => void
  /** Called once with the complete endpointed utterance (PCM16). `text` is
   *  set when the end-of-turn check already transcribed it (skip STT). */
  onUtterance: (pcm16: ArrayBuffer, text?: string) => void
}

/** Default barge-in level (~ -24 dBFS) and hold time.
 *  0.06 is well above TTS echo residue that slips past browser AEC, so a
 *  reply playing by itself rarely trips the interrupt; real speech is
 *  typically much louder than this. */
const DEFAULT_INTERRUPT_THRESHOLD = 0.06
const DEFAULT_INTERRUPT_HOLD_MS = 250
/** After a barge-in fires, the signal must STILL be above threshold for this
 *  long before the recorder starts accumulating — otherwise the "interrupt"
 *  was ambient noise / TTS echo and nothing is ever spoken. */
const DEFAULT_INTERRUPT_CONFIRM_MS = 180

export class MicRecorder {
  private ctx: AudioContext | null = null
  private stream: MediaStream | null = null
  private source: MediaStreamAudioSourceNode | null = null
  private node: AudioWorkletNode | null = null
  private chunks: ArrayBuffer[] = []
  private chunkBytes = 0
  private speaking = false
  private lastVoiceAt = 0
  private maxTimer: ReturnType<typeof setTimeout> | null = null
  private released = false
  private paused = false
  private interruptMode = false
  private interruptArmed = false
  private interruptHoldStart = 0
  private confirmArmed = false
  private confirmStart = 0
  // Semantic endpointing for the current pause: 'idle' = not asked yet,
  // 'pending' = waiting for turnCheck, 'incomplete' = keep listening.
  // turnSeq invalidates an answer that arrives after speech resumed.
  private turnState: 'idle' | 'pending' | 'incomplete' = 'idle'
  private turnSeq = 0
  private turnUnavailable = false

  constructor(private readonly opts: MicRecorderOptions) {}

  get active(): boolean {
    return !this.released && this.ctx !== null
  }

  /**
   * Pause/resume capture. While paused, incoming chunks and levels are
   * dropped (nothing accumulates, no endpointing fires). Used sparingly —
   * during playback we use {@link setInterruptMode} instead so barge-in
   * detection keeps running.
   */
  setPaused(paused: boolean): void {
    if (this.paused === paused) return
    this.paused = paused
    if (paused) this.resetBuffers()
  }

  /**
   * Barge-in listening mode (during reply playback): chunks are streamed to
   * the bridge's silero VAD (only the last `BARGE_IN_KEEP_BYTES` are kept),
   * which fires `speech_start`
   * only for a REAL human voice — TTS echo / music / ambient noise cannot
   * trip it. On `speech_start` the recorder leaves interrupt mode and the
   * user's ongoing speech accumulates normally, starting with the kept audio.
   */
  setInterruptMode(enabled: boolean): void {
    if (this.interruptMode === enabled) return
    this.interruptMode = enabled
    this.interruptArmed = false
    this.confirmArmed = false
    if (enabled) {
      this.opts.vad?.open(() => this.onVadSpeechStart())
    } else {
      this.opts.vad?.close()
      // Reply over: the last 0.5 s is the pre-roll, in case the user started
      // talking just before it ended.
      this.resetBuffers(PRE_ROLL_BYTES)
    }
  }

  /** Bridge VAD heard a real voice: stop the reply and accumulate the user's
   *  ongoing speech. (silero's speech_start is already the confirmation — no
   *  RMS hold/confirm heuristics needed on this path.) */
  private onVadSpeechStart(): void {
    if (!this.interruptMode) return
    this.interruptMode = false
    this.interruptArmed = false
    this.confirmArmed = false
    // 打断时接上 speech_start 之前的音频（2026-10-19）：以前这里清空缓冲，
    // silero 要听到约 0.4 s 人声才报，"我想换个话题聊聊"就只剩"换个话题聊聊"。
    this.resetBuffers(BARGE_IN_KEEP_BYTES)
    this.heardVoice()
    this.opts.onSpeechInterrupt?.()
  }

  /** Clear the endpointing state; keep only the last `keepBytes` of audio. */
  private resetBuffers(keepBytes = 0): void {
    if (this.maxTimer !== null) {
      clearTimeout(this.maxTimer)
      this.maxTimer = null
    }
    this.trimChunks(keepBytes)
    this.speaking = false
    this.interruptArmed = false
    this.confirmArmed = false
    this.resetTurn()
  }

  private resetTurn(): void {
    this.turnState = 'idle'
    this.turnSeq++
  }

  /** Acquire the mic and start the capture worklet. */
  async start(): Promise<void> {
    this.released = false
    const ctx = new AudioContext({ latencyHint: 'interactive' })
    this.ctx = ctx
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    })
    this.stream = stream
    const workletUrl = URL.createObjectURL(
      new Blob([MIC_CAPTURE_WORKLET_SOURCE], { type: 'text/javascript' }),
    )
    try {
      await ctx.audioWorklet.addModule(workletUrl)
    } finally {
      URL.revokeObjectURL(workletUrl)
    }
    const source = ctx.createMediaStreamSource(stream)
    const node = new AudioWorkletNode(ctx, 'mic-capture', {
      numberOfInputs: 1,
      numberOfOutputs: 0,
      processorOptions: { chunkMs: 40 },
    })
    // Arm the noise gate (mirrors the original project): quiet ambient audio
    // below `noiseGateDb` is faded out of the SENT stream. The worklet already
    // carries the full attack/hold/release envelope — this message enables it.
    const gateDb = this.opts.noiseGateDb
    // dBFS values are NEGATIVE (e.g. -35); 0 or undefined means disabled —
    // a `> 0` guard would silently never arm the gate and ambient noise
    // would reach STT untouched (phantom messages).
    if (gateDb !== undefined && gateDb !== 0) {
      node.port.postMessage({ kind: 'gate', enabled: true, thresholdDb: gateDb })
    }
    node.port.onmessage = (e) => {
      if (this.released) return
      const data = e.data
      if (data instanceof ArrayBuffer) this.onChunk(data)
      else if (data !== null && typeof data === 'object' && data.kind === 'level') this.onLevel(data.rms)
    }
    source.connect(node)
    this.source = source
    this.node = node
  }

  /** Stop capture and release the mic / AudioContext. */
  stop(): void {
    if (this.released) return
    this.released = true
    if (this.maxTimer !== null) {
      clearTimeout(this.maxTimer)
      this.maxTimer = null
    }
    this.opts.vad?.close()
    this.node?.port.close()
    this.source?.disconnect()
    this.stream?.getTracks().forEach(track => track.stop())
    void this.ctx?.close().catch(() => {})
    this.node = null
    this.source = null
    this.stream = null
    this.ctx = null
    this.chunks = []
    this.chunkBytes = 0
    this.speaking = false
    this.interruptMode = false
    this.interruptArmed = false
    this.confirmArmed = false
  }

  private onLevel(rms: number): void {
    if (this.released || this.paused) return

    // Barge-in mode: stream to the bridge VAD (never accumulate). Without a
    // connected VadStream (bridge without the endpoint), fall back to RMS.
    if (this.interruptMode) {
      if (this.opts.vad === undefined || !this.opts.vad.available) {
        const threshold = this.opts.interruptThreshold ?? DEFAULT_INTERRUPT_THRESHOLD
        const now = performance.now()
        if (rms >= threshold) {
          if (!this.interruptArmed) {
            this.interruptArmed = true
            this.interruptHoldStart = now
          } else if (!this.confirmArmed && now - this.interruptHoldStart >= (this.opts.interruptHoldMs ?? DEFAULT_INTERRUPT_HOLD_MS)) {
            this.opts.onSpeechInterrupt?.()
            this.confirmArmed = true
            this.confirmStart = now
          } else if (this.confirmArmed && now - this.confirmStart >= (this.opts.interruptConfirmMs ?? DEFAULT_INTERRUPT_CONFIRM_MS)) {
            this.confirmArmed = false
            this.interruptArmed = false
            this.interruptMode = false
            this.resetBuffers(BARGE_IN_KEEP_BYTES)
            this.heardVoice()
          }
        } else {
          this.interruptArmed = false
          this.confirmArmed = false
        }
      }
      return
    }

    const threshold = this.opts.rmsThreshold ?? DEFAULT_RMS_THRESHOLD
    if (rms >= threshold) this.heardVoice()
  }

  /** Voice now: restart the silence clock and start the utterance if needed. */
  private heardVoice(): void {
    this.lastVoiceAt = performance.now()
    // Speech resumed: the pause is over, a pending answer is stale.
    if (this.turnState !== 'idle') this.resetTurn()
    if (!this.speaking) {
      this.speaking = true
      this.armMaxTimer()
      this.opts.onSpeakingChange?.(true)
    }
  }

  private onChunk(buffer: ArrayBuffer): void {
    if (this.released || this.paused) return
    this.chunks.push(buffer)
    this.chunkBytes += buffer.byteLength
    if (this.interruptMode) {
      // Barge-in mode: stream to the bridge VAD, keep only the last second.
      this.opts.vad?.send(buffer)
      this.trimChunks(BARGE_IN_KEEP_BYTES)
      return
    }
    if (!this.speaking) {
      // 开口前只留一小段预录（2026-10-19）：以前上一句结束后的静音全攒在这里，
      // 和下一句一起发给 STT / /api/turn，等半分钟再说话就超过 30 s 上限。
      this.trimChunks(PRE_ROLL_BYTES)
      return
    }
    const silenceMs = performance.now() - this.lastVoiceAt
    if (this.opts.turnCheck === undefined || this.turnUnavailable) {
      if (silenceMs >= (this.opts.minSilenceMs ?? DEFAULT_MIN_SILENCE_MS)) this.flush()
      return
    }
    if (this.turnState === 'idle') {
      if (silenceMs >= (this.opts.turnSilenceMs ?? DEFAULT_TURN_SILENCE_MS)) this.askTurn()
      return
    }
    // 'pending' or 'incomplete': give the speaker room to go on.
    if (silenceMs >= (this.opts.incompleteSilenceMs ?? DEFAULT_INCOMPLETE_SILENCE_MS)) this.flush()
  }

  /** Ask the semantic check whether the utterance so far is a finished turn. */
  private askTurn(): void {
    this.turnState = 'pending'
    const seq = this.turnSeq
    const all = this.concatChunks()
    const pcm = all.byteLength > TURN_WINDOW_BYTES ? all.slice(all.byteLength - TURN_WINDOW_BYTES) : all
    void this.opts.turnCheck!(pcm).then((verdict) => {
      if (this.released || seq !== this.turnSeq || this.turnState !== 'pending') return
      if (verdict === null) {
        // Bridge without /api/turn (or it failed): plain silence timing from now on.
        this.turnUnavailable = true
        this.turnState = 'idle'
      } else if (verdict.complete) {
        this.flush(verdict.text)
      } else {
        this.turnState = 'incomplete'
      }
    })
  }

  private armMaxTimer(): void {
    if (this.maxTimer !== null) clearTimeout(this.maxTimer)
    this.maxTimer = setTimeout(
      () => this.flush(),
      this.opts.maxUtteranceMs ?? DEFAULT_MAX_UTTERANCE_MS,
    )
  }

  private flush(text?: string): void {
    if (this.released) return
    if (this.maxTimer !== null) {
      clearTimeout(this.maxTimer)
      this.maxTimer = null
    }
    const pcm = this.chunkBytes > 0 ? this.concatChunks() : null
    this.chunks = []
    this.chunkBytes = 0
    this.resetTurn()
    if (this.speaking) {
      this.speaking = false
      this.opts.onSpeakingChange?.(false)
    }
    if (pcm !== null) this.opts.onUtterance(pcm, text)
  }

  /** Drop the oldest chunks while at least `bytes` would remain (0 = drop all). */
  private trimChunks(bytes: number): void {
    while (this.chunks.length > 0 && this.chunkBytes - this.chunks[0]!.byteLength >= bytes) {
      this.chunkBytes -= this.chunks.shift()!.byteLength
    }
  }

  private concatChunks(): ArrayBuffer {
    const out = new Uint8Array(this.chunkBytes)
    let offset = 0
    for (const chunk of this.chunks) {
      out.set(new Uint8Array(chunk), offset)
      offset += chunk.byteLength
    }
    return out.buffer
  }
}
