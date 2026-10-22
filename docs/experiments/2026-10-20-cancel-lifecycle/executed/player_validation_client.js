// Temporary browser test panel. Bundled with the unchanged production speaker.
import { ReplySpeaker } from '../dsh-plugin/src/client/voice/speaker.ts'

const originalFetch = window.fetch.bind(window)
const fake = { ctx: null, destination: null }
const experiment = { current: null, player: [], e2e: [], activeTts: 0, policy: 'baseline' }
const sourceRecords = new WeakMap()
const contextIds = new WeakMap()
let contextCounter = 0
const now = () => performance.now()
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))

navigator.mediaDevices.getUserMedia = async () => {
  if (!fake.ctx) {
    fake.ctx = new AudioContext({ sampleRate: 16000 })
    fake.destination = fake.ctx.createMediaStreamDestination()
  }
  await fake.ctx.resume()
  return fake.destination.stream
}

const start = AudioBufferSourceNode.prototype.start
AudioBufferSourceNode.prototype.start = function (when, ...rest) {
  const current = experiment.current
  if (current && this.context !== fake.ctx && this.buffer) {
    const values = this.buffer.getChannelData(0)
    let first = 0
    while (first < values.length && Math.abs(values[first]) < .01) first++
    const at = now() + Math.max(0, (when || 0) - this.context.currentTime) * 1000
    const record = {
      called_at: now(), scheduled_start: at,
      scheduled_end: at + this.buffer.duration * 1000,
      first_voiced_at: first < values.length ? at + first / this.buffer.sampleRate * 1000 : null,
      frames: values.length, sample_rate: this.buffer.sampleRate,
      context_state: this.context.state,
      segment: current.segment ?? null,
      context_id: contextIds.get(this.context) ?? (() => {
        const id = ++contextCounter; contextIds.set(this.context, id); return id
      })(),
    }
    sourceRecords.set(this, record)
    current.sources.push(record)
    this.addEventListener('ended', () => { record.ended_at = now() }, { once: true })
  }
  return start.call(this, when, ...rest)
}
const stop = AudioBufferSourceNode.prototype.stop
AudioBufferSourceNode.prototype.stop = function (...args) {
  const record = sourceRecords.get(this)
  if (record) record.stopped_at = now()
  return stop.apply(this, args)
}

window.fetch = async (input, init) => {
  let url = typeof input === 'string' ? input : input?.url || String(input)
  const match = url.match(/\/api\/(turn|stt|tts(?:\/stream)?)\b/)
  const current = experiment.current
  if (!match || !current) return originalFetch(input, init)
  const record = { api: match[1], at: now() }
  current.events.push(record)
  if (match[1] === 'tts/stream') {
    try { record.text = JSON.parse(init.body).text } catch {}
    url = '/__voice_test__/tts/stream?policy=' + experiment.policy
    if (experiment.ttsHeaders) {
      init = { ...init, headers: { ...init.headers, ...experiment.ttsHeaders(current) } }
    }
    experiment.activeTts++
  }
  try {
    const response = await originalFetch(url, init)
    record.status = response.status
    record.headers_at = now()
    record.stream_id = response.headers.get('X-Test-Stream')
    if (match[1] !== 'tts/stream') {
      response.clone().json().then(value => { record.result = value; record.done = now() }).catch(() => {})
      return response
    }
    const reader = response.body.getReader()
    let finished = false
    const finish = () => {
      if (!finished) { finished = true; experiment.activeTts--; record.done = now() }
    }
    return new Response(new ReadableStream({
      async pull(controller) {
        try {
          const { done, value } = await reader.read()
          if (done) { finish(); controller.close() } else controller.enqueue(value)
        } catch (error) { finish(); controller.error(error) }
      },
      async cancel(reason) { record.cancelled_at = now(); finish(); await reader.cancel(reason) },
    }), { status: response.status, headers: response.headers })
  } catch (error) {
    record.error = String(error)
    if (match[1] === 'tts/stream') experiment.activeTts--
    throw error
  }
}

const OriginalWebSocket = window.WebSocket
window.WebSocket = class extends OriginalWebSocket {
  constructor(url, protocols) {
    super(url, protocols)
    if (String(url).includes('/api/vad')) this.addEventListener('message', e => {
      if (experiment.current && String(e.data).includes('speech_start')) {
        experiment.current.events.push({ api: 'vad_start', at: now() })
      }
    })
  }
}

function trial(kind, key, policy) {
  experiment.policy = policy
  return (experiment.current = { kind, key, policy, started: now(), events: [], sources: [] })
}
async function waitUntil(check, seconds = 60) {
  const until = now() + seconds * 1000
  while (!check()) {
    if (now() > until) throw new Error('test timeout')
    await sleep(50)
  }
}
function summarize(record) {
  const voiced = record.sources.map(s => s.first_voiced_at).filter(t => t !== null)
  record.first_voice_at = voiced.length ? Math.min(...voiced) : null
  record.last_ended_at = Math.max(0, ...record.sources.map(s => s.ended_at || 0))
  record.finished = now()
  if (record.input_end && record.first_voice_at) record.voice_to_voice_ms = record.first_voice_at - record.input_end
  return record
}
async function save(kind) {
  await originalFetch('/__voice_test__/results/' + kind, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(experiment[kind]),
  })
}

async function say(parts) {
  if (!fake.ctx) throw new Error('请先开启 DSH 测试麦克风')
  let at = fake.ctx.currentTime + .2
  const marks = []
  for (const part of parts) {
    if (typeof part === 'number') { at += part / 1000; continue }
    const raw = await originalFetch('/__voice_test__/audio/' + part + '.wav').then(r => r.arrayBuffer())
    const decoded = await fake.ctx.decodeAudioData(raw)
    const data = decoded.getChannelData(0)
    let first = 0, last = data.length - 1
    while (first < data.length && Math.abs(data[first]) < .01) first++
    while (last > first && Math.abs(data[last]) < .01) last--
    const buffer = fake.ctx.createBuffer(1, last - first + 1, decoded.sampleRate)
    buffer.copyToChannel(data.slice(first, last + 1), 0)
    // Fetch all inputs before scheduling: no network wait can put starts in the past.
    marks.push({ buffer, gap: at })
    at += buffer.duration
  }
  const base = fake.ctx.currentTime + .2
  const initial = marks[0].gap
  const performanceBase = now(), contextBase = fake.ctx.currentTime
  for (const mark of marks) {
    const source = fake.ctx.createBufferSource()
    source.buffer = mark.buffer
    source.connect(fake.destination)
    const startAt = base + mark.gap - initial
    source.start(startAt)
    mark.start = performanceBase + (startAt - contextBase) * 1000
    mark.end = mark.start + mark.buffer.duration * 1000
    delete mark.buffer; delete mark.gap
  }
  experiment.current.input_marks = marks
  experiment.current.input_end = marks.at(-1).end
}

function installPanel() {
  const panel = document.createElement('section')
  panel.id = 'voice-validation-panel'
  panel.style.cssText = 'position:fixed;right:12px;top:12px;z-index:2147483647;width:440px;padding:16px;background:#102b3a;color:white;border:2px solid #57d9a3;font:13px system-ui;max-height:55vh;overflow:auto;border-radius:10px'
  panel.innerHTML = '<b>apex 播放器与完整链路实验</b><p>合成输入；使用真实 ReplySpeaker。人工听检另记。</p><button id="run-player">运行31项播放器验证</button> <button id="run-e2e">运行17轮完整链路</button><pre id="validation-status" style="white-space:pre-wrap"></pre>'
  document.body.append(panel)
  const output = panel.querySelector('pre')
  const note = text => { output.textContent += text + '\n'; output.scrollTop = output.scrollHeight }
  const busy = value => panel.querySelectorAll('button').forEach(b => { b.disabled = value })
  panel.querySelector('#run-player').onclick = async () => {
    busy(true)
    const speaker = new ReplySpeaker()
    try {
      const cases = [...Array.from({ length: 6 }, (_, i) => 'soft-' + i), 'long-tail', 'internal-pause', 'late-packet']
      for (const key of cases) for (const policy of ['baseline', 'guard99', 'guard3']) {
        const record = trial('player', key, policy)
        const response = await originalFetch('/__voice_test__/fixture/' + key + '?policy=' + policy)
        record.stream_id = response.headers.get('X-Test-Stream')
        await speaker.speakStream(response.body)
        await waitUntil(() => !speaker.speaking)
        experiment.player.push(summarize(record)); await save('player')
        note(key + ' ' + policy + ' 已结束：' + Math.round(record.last_ended_at - record.started) + 'ms')
      }
      for (const policy of ['baseline', 'guard3']) {
        const record = trial('player', 'queued-two-segments', policy)
        for (const key of ['long-tail', 'normal-0']) {
          const response = await originalFetch('/__voice_test__/fixture/' + key + '?policy=' + policy)
          await speaker.speakStream(response.body)
        }
        await waitUntil(() => !speaker.speaking)
        experiment.player.push(summarize(record)); await save('player')
        note('连续两段 ' + policy + ' 已结束')
      }
      for (const policy of ['baseline', 'guard3']) {
        const record = trial('player', 'cancel-stream', policy)
        const response = await originalFetch('/__voice_test__/fixture/long-tail?policy=' + policy)
        const reading = speaker.speakStream(response.body)
        await waitUntil(() => record.sources.length > 0)
        await sleep(250)
        record.stop_requested_at = now(); speaker.stop()
        await reading; await sleep(500)
        record.speaking_after_stop = speaker.speaking
        experiment.player.push(summarize(record)); await save('player')
        note('取消 ' + policy + ' 完成')
      }
      note('播放器完成：' + experiment.player.length + '/31')
    } catch (error) { note('播放器失败：' + error); experiment.player.push({ error: String(error) }); await save('player') }
    finally { speaker.stop(); experiment.current = null; busy(false) }
  }
  panel.querySelector('#run-e2e').onclick = async () => {
    busy(true)
    try {
      if (!fake.ctx) throw new Error('先点击 DSH 语音输入，测试流将替代麦克风')
      const cases = [['input-0'], ['input-1'], ['input-2'], ['input-3', 600, 'input-4']]
      const sequence = [{ key: 'first-new-session', policy: 'baseline', parts: cases[0], warmup: true }]
      for (let repeat = 0; repeat < 2; repeat++) for (let i = 0; i < cases.length; i++) {
        for (const policy of (i + repeat) % 2 ? ['guard3', 'baseline'] : ['baseline', 'guard3']) {
          sequence.push({ key: 'case-' + i + '-repeat-' + repeat, policy, parts: cases[i] })
        }
      }
      for (const item of sequence) {
        const record = trial('e2e', item.key, item.policy)
        record.warmup = Boolean(item.warmup)
        await say(item.parts)
        await waitUntil(() => {
          const ends = record.sources.map(s => s.ended_at || 0)
          const tts = record.events.filter(e => e.api === 'tts/stream')
          return ends.length > 0 && ends.every(Boolean) && experiment.activeTts === 0 &&
            now() > Math.max(...ends) + 900 && tts.every(e => e.done && now() > e.done + 1200)
        }, 75)
        experiment.e2e.push(summarize(record)); await save('e2e')
        note(item.key + ' ' + item.policy + ' 首声：' + Math.round(record.voice_to_voice_ms) + 'ms')
      }
      note('完整链路完成：' + experiment.e2e.length + '/17')
    } catch (error) { note('完整链路失败：' + error); experiment.e2e.push({ error: String(error), partial: experiment.current }); await save('e2e') }
    finally { experiment.current = null; busy(false) }
  }
}
if (!document.querySelector('script[data-xvector]')) {
  if (document.body) installPanel()
  else document.addEventListener('DOMContentLoaded', installPanel, { once: true })
}

export { experiment, originalFetch, now, sleep, trial, waitUntil, summarize, save, say }
