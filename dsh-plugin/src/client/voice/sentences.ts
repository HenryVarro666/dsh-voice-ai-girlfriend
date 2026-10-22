/**
 * Sentence splitting for streaming TTS (mirrors the original backend's
 * LMOutputProcessor: the reply is spoken sentence-by-sentence, so synthesis
 * pipelines with playback instead of waiting for the whole text).
 *
 * Chinese/English terminal punctuation: 。！？!?…. The trailing unterminated
 * chunk is returned separately as `partial` — it is not spoken until a
 * terminator completes it.
 *
 * 2026-10-19 加了三条边界（都只看已收到的前缀，流式增长时切法不变——
 * reply-listener 按下标记「已读到第几句」，切法一变就会重读或漏读）：
 * - 换行：列表项、标题没有句末标点，以前整段列表被当成一句（实测 276 字
 *   一次送 TTS，合成出 41.7 s 音频）。需要 cleanReplyText(…, keepLines)。
 * - 第一句在逗号处提前切（够 FIRST_CLAUSE_MIN 字时），第一声早出来。
 * - 每段最长 MAX_CHUNK 字：优先在后半段的次级标点处切，没有就硬切。
 */

const TERMINATORS = new Set(['。', '！', '？', '!', '?', '…'])
/** Where the FIRST sentence may end early (full-width only: "1,000" "10:30"). */
const FIRST_CLAUSE_BREAKS = new Set(['，', '；', '：'])
/** Where an over-long chunk prefers to be cut. */
const SOFT_BREAKS = new Set(['，', '、', '；', '：', ',', ';', ':', ' '])
const FIRST_CLAUSE_MIN = 5
const MAX_CHUNK = 60

/** True when the sentence is trivial noise (only punctuation/whitespace, or
 *  a single character) — split fragments from JSON/fence content during
 *  streaming ("！" "？" "…" standalone) must never reach the TTS. */
function isTrivial(sentence: string): boolean {
  if (sentence.length <= 1) return true
  // No CJK, no ASCII alphanumerics => punctuation/whitespace only.
  return !/[一-鿿0-9a-zA-Z]/.test(sentence)
}

export interface SentenceSplit {
  /** Complete sentences, each ending with a terminator (trimmed). */
  sentences: string[]
  /** The trailing chunk without a terminator, or null when the text ends on a boundary. */
  partial: string | null
}

/** Split text into complete sentences + a trailing partial chunk. */
export function splitSentences(text: string): SentenceSplit {
  const sentences: string[] = []
  let buf: string[] = []
  // 第一段（哪怕是被丢弃的噪声）切出去之后，逗号就不再提前切。
  let firstDone = false
  const emit = (): void => {
    const trimmed = buf.join('').trim()
    if (trimmed !== '' && !isTrivial(trimmed)) sentences.push(trimmed)
    if (trimmed !== '') firstDone = true
    buf = []
  }
  for (const ch of text) {
    if (ch === '\n') {
      emit()
      continue
    }
    buf.push(ch)
    if (TERMINATORS.has(ch)) {
      emit()
    } else if (!firstDone && FIRST_CLAUSE_BREAKS.has(ch) && buf.join('').trim().length >= FIRST_CLAUSE_MIN) {
      emit()
    } else if (buf.length >= MAX_CHUNK) {
      let cut = buf.length
      for (let j = buf.length - 1; j >= MAX_CHUNK / 2; j--) {
        if (SOFT_BREAKS.has(buf[j]!)) {
          cut = j + 1
          break
        }
      }
      const rest = buf.slice(cut)
      buf = buf.slice(0, cut)
      emit()
      buf = rest
    }
  }
  const partial = buf.join('').trim()
  return { sentences, partial: partial === '' || isTrivial(partial) ? null : partial }
}
