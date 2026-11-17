import type { PronunciationRule, ScriptBeat } from '../types/studio';

/**
 * `tts_text` 相对 `narration_text` 的长度占比下限。
 *
 * `tts_text` 的契约是**整句送音文本**（与字幕等量级，仅个别脱敏字被换成标准汉字
 * 同音词）。若它明显短于字幕原句，说明里面只写了「被替换的那一个词」（如「混蛋」），
 * 直接拿来试听/合成会让整条解说只剩两三个字——此时一律回退字幕原句。
 * 与后端 `backend/src/stages/s5_tts.py::build_spoken_text` 保持同一判定。
 */
const TTS_FRAGMENT_RATIO = 0.6;

export function resolveSpokenText(
  beat: ScriptBeat,
  rules: PronunciationRule[]
): { spokenText: string; matchedRules: PronunciationRule[] } {
  const narration = beat.narration_text;
  const ttsText = (beat.tts_text ?? '').trim();
  let base = narration;
  if (
    ttsText &&
    !narration.includes(ttsText) &&
    ttsText.length >= narration.length * TTS_FRAGMENT_RATIO
  ) {
    base = ttsText;
  }
  let result = base;
  const matchedRules: PronunciationRule[] = [];

  for (const rule of rules) {
    if (rule.source && result.includes(rule.source)) {
      result = result.split(rule.source).join(rule.replacement);
      matchedRules.push(rule);
    }
  }

  return { spokenText: result, matchedRules };
}

export function parseEpisodeRangeExpression(expr: string, maxEp: number): number[] {
  const result = new Set<number>();
  const parts = expr
    .split(/[,，]/)
    .map((s) => s.trim())
    .filter(Boolean);

  for (const part of parts) {
    if (part.includes('-')) {
      const [aStr, bStr] = part.split('-').map((s) => s.trim());
      const a = Number(aStr);
      const b = Number(bStr);
      if (!Number.isNaN(a) && !Number.isNaN(b) && a <= b) {
        for (let i = Math.max(1, a); i <= Math.min(maxEp, b); i++) {
          result.add(i);
        }
      }
    } else {
      const n = Number(part);
      if (!Number.isNaN(n) && n >= 1 && n <= maxEp) {
        result.add(n);
      }
    }
  }
  return Array.from(result).sort((a, b) => a - b);
}

export function formatMs(ms: number): string {
  const totalSec = Math.max(0, ms / 1000);
  const mins = Math.floor(totalSec / 60);
  const secs = Math.floor(totalSec % 60);
  const centis = Math.floor((ms % 1000) / 10);
  return `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}.${String(centis).padStart(2, '0')}`;
}

export function playPreviewTone(): void {
  try {
    const AudioCtx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
    if (!AudioCtx) return;
    const ctx = new AudioCtx();
    const now = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(523.25, now);
    osc.frequency.exponentialRampToValueAtTime(659.25, now + 0.14);
    gain.gain.setValueAtTime(0.08, now);
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.28);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(now);
    osc.stop(now + 0.29);
  } catch {
    // Ignore audio context restriction if browser blocks autoplay
  }
}
