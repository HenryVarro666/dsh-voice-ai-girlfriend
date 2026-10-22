/**
 * Clean assistant reply text for TTS reading.
 *
 * The reply body is markdown (headings, emphasis, lists, inline code, fenced
 * code blocks — including ```dsh-ui fences with JSON) plus URLs. Reading it
 * verbatim is noisy and can generate minutes of audio. This strips the
 * decorations, collapses whitespace, and caps the length at a sentence
 * boundary so a reading stays short and audible.
 */

const FENCED_BLOCK = /```[\s\S]*?```/g
// A fence still streaming (not closed yet): hide everything after it, or its
// code lines would be read one by one before the closing fence arrives.
const OPEN_FENCE = /```[\s\S]*$/
const INLINE_CODE = /`[^`\n]*`/g
const URL = /https?:\/\/[^\s)\]]+/g
const HEADING = /^#{1,6}\s+/gm
const QUOTE = /^>\s?/gm
// Bullets and ordered-list numbers ("1." "2)" "3、"), indented ones included.
const LIST_MARK = /^[ \t]*(?:[-*+]|\d{1,3}[.)、])[ \t]+/gm
const EMPHASIS = /[*_~]{1,3}/g
const WHITESPACE = /\s+/g
const LINE_BREAKS = /[ \t]*\n\s*/g
const SPACES = /[^\S\n]+/g

/**
 * @param text - raw assistant markdown text.
 * @param maxChars - soft cap; truncation lands on a Chinese/English sentence
 *   boundary when one exists past the half-way point.
 * @param keepLines - keep one '\n' between lines (sentence TTS splits on it:
 *   list items and headings carry no terminal punctuation). Default false:
 *   single line, as the DH and QQ paths expect.
 * @returns clean, capped text ('' when nothing remains).
 */
export function cleanReplyText(text: string, maxChars = 400, keepLines = false): string {
  const stripped = text
    .replace(FENCED_BLOCK, ' ')
    .replace(OPEN_FENCE, ' ')
    .replace(INLINE_CODE, ' ')
    .replace(URL, ' ')
    .replace(HEADING, '')
    .replace(QUOTE, '')
    .replace(LIST_MARK, '')
    .replace(EMPHASIS, '')
  // A trailing newline is a committed sentence boundary. Removing it while
  // the next list marker streams in can retract a sentence already spoken.
  const out = keepLines
    ? stripped.replace(LINE_BREAKS, '\n').replace(SPACES, ' ').trimStart().replace(/[ \t]+$/, '')
    : stripped.replace(WHITESPACE, ' ').trim()
  if (out.length <= maxChars) return out
  const cut = out.slice(0, maxChars)
  const boundary = Math.max(
    cut.lastIndexOf('。'),
    cut.lastIndexOf('！'),
    cut.lastIndexOf('？'),
    cut.lastIndexOf('.'),
    cut.lastIndexOf('!'),
    cut.lastIndexOf('?'),
  )
  return boundary > maxChars * 0.5
    ? `${cut.slice(0, boundary + 1)}…`
    : `${cut}…`
}
