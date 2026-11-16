import type { PronunciationRule, ScriptBeat } from '../types/studio';

export function resolveSpokenText(
  beat: ScriptBeat,
  rules: PronunciationRule[]
): { spokenText: string; matchedRules: PronunciationRule[] } {
  const base = beat.tts_text.trim() ? beat.tts_text : beat.narration_text;
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
