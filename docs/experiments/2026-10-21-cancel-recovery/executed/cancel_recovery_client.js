// Actual ReplySpeaker cancellation bursts; no microphone or DSH prompt injection.
import { ReplySpeaker } from '../dsh-plugin/src/client/voice/speaker.ts'
import { experiment, originalFetch, now, sleep, trial, waitUntil, summarize, save } from './player_validation_client.js'

experiment.interruption = []
experiment.ttsHeaders = r => ({
  'X-Experiment-Trial': r.key,
  'X-Experiment-Segment': String(r.segment),
  'X-Experiment-Seed': String(r.seed),
})
const number = '编号是一零零二零零三零零。'
const long = '我们先把今天的事情慢慢理清楚。第一件事是保存已经完成的记录，第二件事是检查明天需要准备的材料。如果你暂时还没有想好，也可以先休息一下，等精神好一点的时候再继续。最后记得把桌上的小盒子收好，我们明天再一起看看吧。'
async function response(text, signal) {
  const res = await window.fetch('/api/tts/stream', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }), signal,
  })
  if (!res.ok) throw new Error('Private TTS ' + res.status)
  return res
}
async function burst(arm, repeat) {
  const r = trial('interruption', `browser-burst-${repeat}-${arm}`, arm)
  const speaker = new ReplySpeaker()
  r.stops = []
  try {
    for (const segment of [1, 2]) {
      r.segment = segment; r.seed = segment === 1 ? 4409 : 5501
      const control = new AbortController()
      const previous = r.sources.length
      const res = await response(segment === 1 ? number : long, control.signal)
      const reading = speaker.speakStream(res.body)
      if (segment === 1) await sleep(20)
      else { await waitUntil(() => r.sources.length > previous, 10); await sleep(80) }
      const stopped = now()
      speaker.stop(); control.abort(); await reading
      await sleep(60)
      r.stops.push({ segment, at: stopped, gap_end: now(), sources_before: previous,
        late_sources: r.sources.filter(s => s.called_at > stopped).length })
    }
    r.segment = 3; r.seed = 6501
    r.followup_requested_at = now()
    const res = await response('好的，我们明天再聊吧。')
    await speaker.speakStream(res.body)
    await waitUntil(() => !speaker.speaking, 20)
    let state
    const deadline = now() + 15000
    do {
      state = await originalFetch('/__voice_test__/state').then(x => x.json())
      if (now() > deadline) throw new Error('Private worker did not drain')
      if (state.pending) await sleep(50)
    } while (state.pending)
    r.server = state.streams.filter(s => s.trial === r.key)
    r.max_active = state.max_active
    const final = r.sources.filter(s => s.called_at >= r.followup_requested_at)
    r.followup_samples = final.reduce((n, s) => n + s.frames, 0)
    r.followup_received = r.server.find(s => s.segment === '3').sent_samples
    r.followup_to_voice_ms = Math.min(...final.map(s => s.first_voiced_at).filter(x => x !== null)) - r.followup_requested_at
    r.late_sources = r.stops.reduce((n, s) => n + s.late_sources, 0)
    if (r.late_sources || r.followup_samples !== r.followup_received || r.max_active !== 1) throw new Error('Burst invariant failed')
    experiment.interruption.push(summarize(r)); await save('interruption')
    return r
  } finally { speaker.stop() }
}
function install() {
  const panel = document.createElement('section')
  panel.id = 'recovery-panel'
  panel.style.cssText = 'position:fixed;right:12px;top:12px;z-index:2147483647;width:450px;padding:16px;background:#102b3a;color:white;border:2px solid #57d9a3;font:13px system-ui;max-height:40vh;overflow:auto;border-radius:10px'
  panel.innerHTML = '<b>取消恢复 · 真实播放器验证</b><p>默认ICL；生产与裁剪不变；测试麦克风关闭。</p><button>运行4组连续取消</button><pre style="white-space:pre-wrap"></pre>'
  document.body.append(panel)
  const button = panel.querySelector('button'), log = panel.querySelector('pre')
  const note = text => { log.textContent += text + '\n'; panel.scrollTop = panel.scrollHeight }
  button.onclick = async () => {
    button.disabled = true
    try {
      for (const repeat of [0, 1]) for (const arm of repeat ? ['fixed', 'v1'] : ['v1', 'fixed']) {
        const r = await burst(arm, repeat)
        note(r.key + ': 晚source=' + r.late_sources + '，后继采样=' + r.followup_samples + '/' + r.followup_received + '，首声=' + Math.round(r.followup_to_voice_ms) + 'ms')
      }
      note('4组连续取消完成；8次stop；后台与采样核验通过')
    } catch (error) {
      note('失败已保留：' + error)
      experiment.interruption.push({ error: String(error), partial: experiment.current })
      await save('interruption')
    } finally { experiment.current = null; button.disabled = false }
  }
}
if (document.body) install()
else document.addEventListener('DOMContentLoaded', install, { once: true })
