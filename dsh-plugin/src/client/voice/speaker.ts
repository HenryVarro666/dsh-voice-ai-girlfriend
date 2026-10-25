/**
 * ReplySpeaker: plays reply audio through one AudioContext as a FIFO queue
 * (sentence streaming). A clip is either a whole WAV (`speak()`) or a PCM16
 * stream (`speakStream()`, the bridge's /api/tts/stream) that starts playing
 * with its first chunk while the rest of the sentence is still synthesized.
 * Clips play back to back, so sentence N+1 starts the moment N finishes.
 * `speaking` stays true while anything is queued or playing, so the companion
 * window and the mic echo-guard follow the whole reply, not individual clips.
 *
 * Interruption: `stop()` halts what is playing, cancels the stream being read
 * and clears the queue; a generation counter discards clips that were queued
 * but not yet started.
 */

/** Scheduling margin for streamed chunks, and the restart point after a late one. */
const STREAM_LEAD_SEC = 0.05

/** The PCM16 chunks of one streamed clip, filled by the reader as they arrive. */
class PcmClip {
  done = false
  private parts: Float32Array<ArrayBuffer>[] = []
  private carry: number | null = null // low byte left over from an odd-length read
  private wake: (() => void) | null = null

  push(bytes: Uint8Array): void {
    let start = 0
    const total = bytes.byteLength + (this.carry === null ? 0 : 1)
    const out = new Float32Array(total >> 1)
    let i = 0
    if (this.carry !== null && bytes.byteLength > 0) {
      out[i++] = (((bytes[0]! << 8) | this.carry) << 16 >> 16) / 32768
      start = 1
      this.carry = null
    }
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
    for (let p = start; p + 1 < bytes.byteLength; p += 2) out[i++] = view.getInt16(p, true) / 32768
    if ((bytes.byteLength - start) % 2 === 1) this.carry = bytes[bytes.byteLength - 1]!
    if (i > 0) this.parts.push(out.subarray(0, i).slice())
    this.notify()
  }

  end(): void {
    this.done = true
    this.notify()
  }

  /** Everything received since the last call as one block, or null if nothing new. */
  take(): Float32Array<ArrayBuffer> | null {
    if (this.parts.length === 0) return null
    if (this.parts.length === 1) return this.parts.pop()!
    const out = new Float32Array(this.parts.reduce((n, p) => n + p.length, 0))
    let at = 0
    for (const p of this.parts) {
      out.set(p, at)
      at += p.length
    }
    this.parts = []
    return out
  }

  /** Resolves when more audio arrives or the stream ends. */
  wait(): Promise<void> {
    return new Promise((resolve) => {
      this.wake = resolve
    })
  }

  private notify(): void {
    const wake = this.wake
    this.wake = null
    wake?.()
  }
}

export type PlaybackEvent = {
  type: 'scheduled'; epoch: number; utteranceId: string; chunkIndex: number;
  sampleRate: number; sampleOffset: number; contextStartTime: number;
  pcm: Float32Array; context: AudioContext; source: AudioBufferSourceNode;
} | { type: 'reset'; epoch: number }

type Clip = ({ wav: ArrayBuffer } | { pcm: PcmClip; sampleRate: number }) & { id: string }

export class ReplySpeaker {
  private ctx: AudioContext | null = null
  private queue: Clip[] = []
  private current: Clip | null = null
  /** Started or scheduled sources, each with the callback that settles its playback. */
  private sources = new Map<AudioBufferSourceNode, () => void>()
  private readers = new Set<ReadableStreamDefaultReader<Uint8Array>>()
  private drainRunning = false
  private gen = 0
  private nextUtterance = 0
  private playbackObservers = new Set<(event: PlaybackEvent) => void>()
  private disposed = false
  private listeners = new Set<() => void>()

  /** True while a reply is being read (playing or waiting in the queue). */
  get speaking(): boolean {
    return this.current !== null || this.queue.length > 0
  }

  /** Subscribe to speaking-state changes; returns the unsubscribe function. */
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  /** Analysis observers must not play audio or mutate the source; PCM is copied. */
  observePlayback(observer: (event: PlaybackEvent) => void): () => void {
    this.playbackObservers.add(observer)
    return () => this.playbackObservers.delete(observer)
  }

  private emitPlayback(event: PlaybackEvent): void {
    for (const observer of this.playbackObservers) {
      try { observer(event) } catch { /* Visual failures must never block speech. */ }
    }
  }

  private emit(): void {
    for (const listener of this.listeners) listener()
  }

  /** Enqueue one clip for playback (order preserved; plays after earlier clips). */
  speak(wav: ArrayBuffer): void {
    if (this.disposed) return
    this.queue.push({ wav, id: `${this.gen}:${++this.nextUtterance}` })
    this.emit()
    void this.drain()
  }

  /**
   * Enqueue a streamed clip (raw mono PCM16 little-endian) and read it to the
   * end. Playback starts once earlier clips are done and the first chunk is
   * in. Resolves when the stream has been fully read (or was cancelled), not
   * when it has finished playing, so the caller can fetch the next sentence.
   */
  async speakStream(body: ReadableStream<Uint8Array>, sampleRate = 16000): Promise<void> {
    const reader = body.getReader()
    if (this.disposed) {
      void reader.cancel().catch(() => {})
      return
    }
    const gen = this.gen
    const pcm = new PcmClip()
    this.queue.push({ pcm, sampleRate, id: `${this.gen}:${++this.nextUtterance}` })
    this.emit()
    void this.drain()
    this.readers.add(reader)
    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done || gen !== this.gen) break
        pcm.push(value)
      }
    } catch {
      // aborted or the connection dropped: play what arrived
    } finally {
      this.readers.delete(reader)
      pcm.end()
      if (gen !== this.gen) void reader.cancel().catch(() => {})
    }
  }

  private context(): AudioContext {
    if (this.ctx !== null) return this.ctx
    // 16 kHz like the bridge output: WAVs need no resampling and streamed
    // chunks butt together sample-exact; the browser resamples once at the output.
    try {
      this.ctx = new AudioContext({ sampleRate: 16000 })
    } catch {
      this.ctx = new AudioContext()
    }
    return this.ctx
  }

  private async drain(): Promise<void> {
    if (this.drainRunning) return
    this.drainRunning = true
    try {
      const ctx = this.context()
      if (ctx.state === 'suspended') {
        try {
          await ctx.resume()
        } catch {
          // best-effort
        }
      }
      while (this.queue.length > 0 && !this.disposed) {
        const clip = this.queue.shift()!
        this.current = clip
        if ('wav' in clip) await this.playWav(ctx, clip.wav, clip.id)
        else await this.playPcm(ctx, clip.pcm, clip.sampleRate, clip.id)
        this.current = null
      }
    } finally {
      this.current = null
      this.drainRunning = false
      this.emit()
    }
  }

  private async playWav(ctx: AudioContext, wav: ArrayBuffer, utteranceId: string): Promise<void> {
    const gen = this.gen
    let buffer: AudioBuffer
    try {
      buffer = await ctx.decodeAudioData(wav)
    } catch {
      return
    }
    if (this.disposed || gen !== this.gen) return
    await this.schedule(ctx, buffer, 0, utteranceId, 0, 0)
  }

  /** Schedule chunks back to back as they arrive; resolves when the last one has played. */
  private async playPcm(ctx: AudioContext, pcm: PcmClip, sampleRate: number, utteranceId: string): Promise<void> {
    const gen = this.gen
    let at = 0
    let sampleOffset = 0
    let chunkIndex = 0
    let last: Promise<void> | null = null
    for (;;) {
      if (this.disposed || gen !== this.gen) return
      const samples = pcm.take()
      if (samples === null) {
        if (pcm.done) break
        await pcm.wait()
        continue
      }
      const buffer = ctx.createBuffer(1, samples.length, sampleRate)
      buffer.copyToChannel(samples, 0)
      // A chunk that arrives after the previous one ran out restarts just
      // ahead of now instead of being scheduled in the past.
      at = Math.max(at, ctx.currentTime + STREAM_LEAD_SEC)
      last = this.schedule(ctx, buffer, at, utteranceId, chunkIndex++, sampleOffset)
      sampleOffset += samples.length
      at += buffer.duration
    }
    if (last !== null) await last
  }

  private schedule(ctx: AudioContext, buffer: AudioBuffer, when: number, utteranceId: string, chunkIndex: number, sampleOffset: number): Promise<void> {
    const source = ctx.createBufferSource()
    source.buffer = buffer
    source.connect(ctx.destination)
    return new Promise((resolve) => {
      const finish = (): void => {
        this.sources.delete(source)
        resolve()
      }
      this.sources.set(source, finish)
      source.onended = finish
      const contextStartTime = Math.max(when, ctx.currentTime)
      source.start(contextStartTime)
      if (this.playbackObservers.size > 0) this.emitPlayback({
        type: 'scheduled', epoch: this.gen, utteranceId, chunkIndex, sampleOffset,
        sampleRate: buffer.sampleRate, contextStartTime,
        pcm: buffer.getChannelData(0).slice(), context: ctx, source,
      })
    })
  }

  /** Stop playback immediately, cancel the stream being read and clear the queue. */
  stop(): void {
    this.gen += 1
    this.emitPlayback({ type: 'reset', epoch: this.gen })
    for (const [source, finish] of this.sources) {
      try {
        source.stop()
      } catch {
        // already stopped
      }
      finish()
    }
    for (const reader of this.readers) void reader.cancel().catch(() => {})
    this.readers.clear()
    for (const clip of [this.current, ...this.queue]) {
      if (clip !== null && 'pcm' in clip) clip.pcm.end()
    }
    this.current = null
    this.queue = []
    this.emit()
  }

  /** Release the AudioContext (plugin teardown). */
  dispose(): void {
    this.disposed = true
    this.stop()
    if (this.ctx !== null) {
      void this.ctx.close().catch(() => {})
      this.ctx = null
    }
    this.listeners.clear()
    this.playbackObservers.clear()
  }
}
