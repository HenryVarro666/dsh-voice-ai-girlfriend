// Fake mic for the DSH voice plugin, v3 (2026-10-19).
// v2: logs upload length (sec) and the bridge VAD's speech_start on /api/vad.
// v3: speech start/end come from the audio itself (first/last sample above 0.01),
//     not the clip edges, because each TTS engine pads differently; `play` is
//     the moment the sound is due (start(when) may be in the future), and the
//     report folds a run of back-to-back plays (streamed chunks) into one event.
(() => {
  const t = () => Math.round(performance.now())
  const log = (window.__e2e = { events: [], speech: [] })
  const fake = (window.__fakeMic = { ctx: null, dest: null })
  const origFetch = (window.__origFetch = window.__origFetch || window.fetch)

  navigator.mediaDevices.getUserMedia = async () => {
    fake.ctx = new AudioContext()
    if (fake.ctx.state !== 'running') await fake.ctx.resume()
    fake.dest = fake.ctx.createMediaStreamDestination()
    log.events.push({ api: 'getUserMedia', t: t() })
    return fake.dest.stream
  }

  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || String(input)
    const m = url.match(/\/api\/(stt|turn|tts(?:\/stream)?)\b/)
    if (!m) return origFetch.apply(this, arguments)
    const rec = { api: m[1], t: t() }
    if (m[1].startsWith('tts')) { try { rec.text = JSON.parse(init.body).text } catch {} }
    else if (init && init.body instanceof ArrayBuffer) rec.sec = +(init.body.byteLength / 32000).toFixed(2)
    log.events.push(rec)
    return origFetch.apply(this, arguments).then(async (r) => {
      rec.done = t()
      if (!m[1].startsWith('tts')) { try { rec.res = await r.clone().json() } catch {} }
      rec.server_ms = rec.res?.ms; rec.stt_ms = rec.res?.stt_ms
      return r
    })
  }

  const OrigWS = window.WebSocket
  window.WebSocket = class extends OrigWS {
    constructor(url, protocols) {
      super(url, protocols)
      if (/\/api\/vad/.test(String(url))) {
        this.addEventListener('message', (e) => {
          if (typeof e.data === 'string' && e.data.includes('speech_start')) log.events.push({ api: 'vad_start', t: t() })
        })
      }
    }
  }

  const start = AudioBufferSourceNode.prototype.start
  AudioBufferSourceNode.prototype.start = function (when, ...rest) {
    if (this.context !== fake.ctx) {
      const ahead = Math.max(0, (when || 0) - this.context.currentTime)
      const a = this.buffer?.getChannelData(0)
      let first = 0
      if (a) while (first < a.length && Math.abs(a[first]) < 0.01) first++
      if (a && first < a.length) log.events.push({ api: 'play', t: Math.round(performance.now() + ahead * 1000 + first / this.buffer.sampleRate * 1000), dur: this.buffer.duration })
    }
    return start.call(this, when, ...rest)
  }

  const voiced = (b) => {
    const d = b.getChannelData(0)
    let first = 0, last = d.length - 1
    while (first < d.length && Math.abs(d[first]) < 0.01) first++
    while (last > first && Math.abs(d[last]) < 0.01) last--
    return [first / b.sampleRate, (last + 1) / b.sampleRate]
  }

  // parts: [{ text } | { silenceMs }] played back to back into the fake mic.
  // silenceMs is added to whatever silence the clips carry at their edges.
  fake.say = async (parts) => {
    const bufs = []
    for (const p of parts) {
      if (p.text === undefined) { bufs.push(p.silenceMs); continue }
      const wav = await origFetch('http://127.0.0.1:8765/api/tts', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: p.text }),
      }).then(r => r.arrayBuffer())
      bufs.push(await fake.ctx.decodeAudioData(wav))
    }
    let at = fake.ctx.currentTime + 0.2
    const marks = []
    for (const b of bufs) {
      if (typeof b === 'number') { at += b / 1000; continue }
      const src = fake.ctx.createBufferSource()
      src.buffer = b
      src.connect(fake.dest)
      src.start(at)
      const [v0, v1] = voiced(b)
      marks.push([at + v0, at + v1])
      at += b.duration
    }
    const c0 = fake.ctx.currentTime, p0 = performance.now()
    const toPerf = c => Math.round(p0 + (c - c0) * 1000)
    log.speech = marks.map(([s, e]) => ({ start: toPerf(s), end: toPerf(e) }))
    log.events = log.events.filter(e => e.api === 'getUserMedia')
    return log.speech
  }

  // Timeline relative to the last voiced sample of the last spoken part.
  fake.report = () => {
    const end = log.speech.length ? log.speech[log.speech.length - 1].end : 0
    const out = []
    for (const e of log.events.filter(e => e.t >= (log.speech[0]?.start ?? 0)).sort((a, b) => a.t - b.t)) {
      const prev = out[out.length - 1]
      if (e.api === 'play' && prev?.api === 'play') { prev.n += 1; prev.sec = +(prev.sec + (e.dur || 0)).toFixed(2); continue }
      out.push({
        api: e.api, at: e.t - end, took: e.done !== undefined ? e.done - e.t : undefined,
        sec: e.api === 'play' ? +(e.dur || 0).toFixed(2) : e.sec, n: e.api === 'play' ? 1 : undefined,
        p: e.res?.probability, complete: e.res?.complete, text: e.res?.text ?? e.text, server_ms: e.server_ms, stt_ms: e.stt_ms,
      })
    }
    return { speech: log.speech.map(s => [s.start - end, s.end - end]), events: out }
  }
  return 'harness v3 ok'
})()

;(() => {
 const build=()=>{
  const panel=document.createElement('section');panel.id='voice-e2e-panel';panel.style.cssText='position:fixed;right:10px;top:10px;z-index:2147483647;background:#152638;color:#fff;padding:14px;border:2px solid #70d5ab;width:420px;max-height:70vh;overflow:auto;font:13px system-ui';
  panel.innerHTML='<b>apex 全链路计时（假麦克风）</b><p>先在 DSH 工具栏开启麦克风，再点击运行。</p><button id="voice-e2e-run">运行四轮语音计时</button><pre id="voice-e2e-log" style="white-space:pre-wrap"></pre>';
  document.body.append(panel)
  const output=panel.querySelector('pre');const note=t=>output.textContent+=t+'\n';window.__e2eResults=[]
  panel.querySelector('button').onclick=async()=>{
   panel.querySelector('button').disabled=true
   if(!window.__fakeMic.ctx){note('请先开启DSH麦克风');panel.querySelector('button').disabled=false;return}
   const cases=[
    {name:'首次会话完整问句',parts:[{text:'你好，今天天气怎么样？'}]},
    {name:'热运行完整问句',parts:[{text:'你吃饭了吗？'}]},
    {name:'热运行简短请求',parts:[{text:'给我讲个简短的笑话吧。'}]},
    {name:'句中停顿后继续说',parts:[{text:'我想问一下那个'},{silenceMs:600},{text:'明天出门要不要带伞？'}]}
   ];
   try{for(const c of cases){
    note('运行：'+c.name)
    await window.__fakeMic.say(c.parts)
    await new Promise(r=>setTimeout(r,18000))
    const report=window.__fakeMic.report();window.__e2eResults.push({name:c.name,...report});note(JSON.stringify(window.__e2eResults.at(-1)))
    // Do not start the next test while a scheduled reply clip is still playing.
    const last=window.__e2e.events.filter(e=>e.api==='play').reduce((v,e)=>Math.max(v,e.t+(e.dur||0)*1000),0)
    if(last>performance.now())await new Promise(r=>setTimeout(r,last-performance.now()+1000))
   }note('全部完成')}catch(e){note(String(e))}
  }
 }
 if(document.body)build();else document.addEventListener('DOMContentLoaded',build,{once:true})
})()
