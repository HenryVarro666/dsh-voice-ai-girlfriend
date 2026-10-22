// Isolated ICL/vector comparison. Shared hooks and the actual ReplySpeaker.
import { ReplySpeaker } from '../dsh-plugin/src/client/voice/speaker.ts'
import { experiment, originalFetch, now, sleep, trial, waitUntil, summarize, save, say } from './player_validation_client.js'

experiment.interruption = []
experiment.ttsHeaders = current => ({
  'X-Experiment-Trial': current.key,
  'X-Experiment-Segment': String(current.segment ?? 0),
  'X-Experiment-Seed': String(current.seedBase + (current.ttsCount++)),
})
const longText = '我们先把今天的事情慢慢理清楚。第一件事是保存已经完成的记录，第二件事是检查明天需要准备的材料。如果你暂时还没有想好，也可以先休息一下，等精神好一点的时候再继续。最后记得把桌上的小盒子收好，我们明天再一起看看吧。'
const numberText = '编号是一零零二零零三零零。'

function begin(kind, key, profile, seed) {
  const record = trial(kind, key, profile)
  record.seedBase = seed
  record.ttsCount = 0
  return record
}
async function state() {
  return originalFetch('/__voice_test__/state').then(r => r.json())
}
async function idle() {
  const until = now() + 15000
  while ((await state()).pending) {
    if (now() > until) throw new Error('private producer did not exit within 15 s')
    await sleep(100)
  }
}
async function response(text, signal) {
  const res = await window.fetch('/api/tts/stream', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }), signal,
  })
  if (!res.ok) throw new Error('private TTS ' + res.status + ': ' + await res.text())
  return res
}

async function cancellation(kind, repeat, profile) {
  const record = begin('interruption', `cancel-${kind}-r${repeat}`, profile, kind === 'before' ? 4409 : 5501)
  const speaker = new ReplySpeaker(), second = new ReplySpeaker()
  const controller = new AbortController(), firstController = new AbortController()
  try {
    record.segment = 1
    if (kind === 'queued') {
      const first = await response(longText, firstController.signal)
      const readingFirst = speaker.speakStream(first.body)
      await waitUntil(() => record.sources.some(s => s.segment === 1), 15)
      record.first_context_id = record.sources[0].context_id
      record.segment = 2
      record.seedBase = 4409; record.ttsCount = 0
      const queued = await response(numberText, controller.signal)
      const reading = second.speakStream(queued.body)
      record.stopped_segment = 2
      record.stop_requested_at = now(); second.stop(); controller.abort()
      await reading
      await sleep(200)
      record.first_stop_at = now(); speaker.stop(); firstController.abort()
      await readingFirst
      record.segment = 3
      record.seedBase = 6501; record.ttsCount = 0
      record.followup_requested_at = now()
      const followup = await response('好的，我们明天再聊吧。')
      await second.speakStream(followup.body)
      await waitUntil(() => !second.speaking, 30)
    } else {
      const res = await response(kind === 'before' ? numberText : longText, controller.signal)
      const reading = speaker.speakStream(res.body)
      if (kind === 'after') {
        await waitUntil(() => record.sources.length > 0, 15)
        await sleep(250)
      }
      record.stopped_segment = 1
      record.sources_before_stop = record.sources.length
      record.stop_requested_at = now(); speaker.stop(); controller.abort()
      await reading
      await sleep(500)
      record.speaking_after_stop = speaker.speaking
    }
    await idle()
    record.server = (await state()).streams.filter(s => s.trial === record.key && s.profile === profile)
    record.late_sources_in_stopped_segment = record.sources.filter(s =>
      s.called_at > record.stop_requested_at && (kind !== 'queued' ||
      (s.context_id !== record.first_context_id && s.called_at < record.followup_requested_at))
    ).length
  } finally { speaker.stop(); second.stop() }
  experiment.interruption.push(summarize(record)); await save('interruption')
  return record
}

async function queue(group, repeat, profile) {
  const record = begin('player', `queue-${group}-r${repeat}`, profile, group === 'tail' ? 4409 : 7101 + repeat)
  const speaker = new ReplySpeaker()
  const texts = group === 'tail' ? [numberText, '好的，我们明天再聊吧。'] : ['我在这里陪着你呢。', '不着急，你慢慢说就好。']
  try {
    for (let index = 0; index < texts.length; index++) {
      record.segment = index + 1
      const res = await response(texts[index])
      await speaker.speakStream(res.body)
    }
    await waitUntil(() => !speaker.speaking, 45)
    await idle()
    record.server = (await state()).streams.filter(s => s.trial === record.key && s.profile === profile)
    experiment.player.push(summarize(record)); await save('player')
    return record
  } finally { speaker.stop() }
}

const inputCases = [['input-0'], ['input-1'], ['input-2'], ['input-3', 600, 'input-4']]
async function e2e(item) {
  const record = begin('e2e', item.key, item.profile, item.seed)
  record.warmup = Boolean(item.first)
  record.case_index = item.caseIndex
  await say(inputCases[item.caseIndex])
  await waitUntil(() => {
    const failed = record.events.find(e => e.api === 'tts/stream' && e.status >= 400)
    if (failed) throw new Error('TTS rejected with ' + failed.status)
    const ends = record.sources.map(s => s.ended_at || 0)
    const tts = record.events.filter(e => e.api === 'tts/stream')
    return ends.length && ends.every(Boolean) && experiment.activeTts === 0 &&
      now() > Math.max(...ends) + 900 && tts.every(e => e.done && now() > e.done + 1200)
  }, 90)
  await idle()
  record.server = (await state()).streams.filter(s => s.trial === record.key && s.profile === item.profile)
  experiment.e2e.push(summarize(record)); await save('e2e')
  return record
}

function install() {
  const panel = document.createElement('section')
  panel.id = 'xvector-integration-panel'
  panel.style.cssText = 'position:fixed;right:12px;top:12px;z-index:2147483647;width:470px;padding:15px;background:#102b3a;color:white;border:2px solid #57d9a3;font:13px system-ui;max-height:48vh;overflow:auto;border-radius:10px'
  panel.innerHTML = '<b>仅向量 · 独立集成实验</b><p>生产与裁剪不变。先测播放器；开启DSH测试麦克风后测完整链路。</p><button id="xv-player">运行12取消+8队列</button> <button id="xv-first-baseline">首次默认</button> <button id="xv-first-xvector">首次向量</button> <button id="xv-e2e">运行16轮热链路</button><pre style="white-space:pre-wrap"></pre>'
  document.body.append(panel)
  const output = panel.querySelector('pre')
  const note = text => { output.textContent += text + '\n'; output.scrollTop = output.scrollHeight }
  const action = async (kind, fn) => {
    panel.querySelectorAll('button').forEach(b => { b.disabled = true })
    try { await fn() }
    catch (error) {
      note('失败，已保留：' + error)
      experiment[kind].push({ error: String(error), partial: experiment.current })
      await save(kind)
    } finally {
      experiment.current = null
      panel.querySelectorAll('button').forEach(b => { b.disabled = false })
    }
  }
  panel.querySelector('#xv-player').onclick = () => action('player', async () => {
    for (let repeat = 0; repeat < 2; repeat++) for (const kind of ['before', 'after', 'queued']) {
      for (const profile of repeat ? ['xvector', 'baseline'] : ['baseline', 'xvector']) {
        const r = await cancellation(kind, repeat, profile)
        note(r.key + ' ' + profile + ' 取消后新增source=' + r.late_sources_in_stopped_segment)
      }
    }
    for (let repeat = 0; repeat < 2; repeat++) for (const group of ['tail', 'natural']) {
      for (const profile of repeat ? ['xvector', 'baseline'] : ['baseline', 'xvector']) {
        const r = await queue(group, repeat, profile)
        note(r.key + ' ' + profile + ' 播完=' + Math.round(r.last_ended_at - r.started) + 'ms')
      }
    }
    note('取消12项、队列8项完成')
  })
  for (const profile of ['baseline', 'xvector']) panel.querySelector('#xv-first-' + profile).onclick = () => action('e2e', async () => {
    const r = await e2e({ key: 'first-' + profile, profile, caseIndex: 0, seed: 8101, first: true })
    note('首次 ' + profile + ' 首声=' + Math.round(r.voice_to_voice_ms) + 'ms')
  })
  panel.querySelector('#xv-e2e').onclick = () => action('e2e', async () => {
    for (let repeat = 0; repeat < 2; repeat++) for (let caseIndex = 0; caseIndex < inputCases.length; caseIndex++) {
      for (const profile of (repeat + caseIndex) % 2 ? ['xvector', 'baseline'] : ['baseline', 'xvector']) {
        const r = await e2e({ key: `case-${caseIndex}-r${repeat}`, profile, caseIndex, seed: 8201 + caseIndex * 100 + repeat * 10 })
        note(r.key + ' ' + profile + ' 首声=' + Math.round(r.voice_to_voice_ms) + 'ms')
      }
    }
    note('16轮热链路完成')
  })
}
if (document.body) install()
else document.addEventListener('DOMContentLoaded', install, { once: true })
