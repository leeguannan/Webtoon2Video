import React, { useCallback, useMemo, useRef, useState } from 'react';
import { Play, Square, Volume2 } from 'lucide-react';
import type { VoiceOption, VoiceSpec } from '../types/studio';

/**
 * 音色选择 / 试听共享组件（Patch v3.4 Patch 16）。
 *
 * 两处复用：
 * - 作品配音面板（`SeriesTtsPanel`）：讲解者 / 通用男女声 / 角色专属音色
 * - 全局配置页（`ConfigDictView`）：Stage 5 的默认音色
 *
 * Patch v4.15：音色下拉**只保留 MiniMax**（百炼 / 阿里音色一律不在候选里），
 * 分组仍按「供应商 → 分类」组织，避免不同 provider 的音色混在一起
 * （百炼音色发给 MiniMax 会直接 2013 invalid voice_id）。
 *
 * 试听**只播放后端预生成的静态样例**，用 `audio.playbackRate` 实时变速，
 * 不会调用 TTS 合成接口。
 */

/** 音色分类 → 中文标签（下拉分组标题）。 */
export const CATEGORY_LABELS: Record<string, string> = {
  narrator: '旁白',
  character: '角色',
  dialect: '方言',
  drama: '戏剧',
  male: '男角色',
  female: '女角色',
  other: '其他',
};

/** 供应商 → 中文标签（Patch v3.4 Patch 16；Patch v4.15 起仅保留 MiniMax）。 */
export const PROVIDER_LABELS: Record<string, string> = {
  minimax: 'MiniMax Speech',
};

/** 分组展示顺序（未列出的分类排最后）。 */
const CATEGORY_ORDER = ['narrator', 'character', 'dialect', 'drama', 'male', 'female', 'other'];
const PROVIDER_ORDER = ['minimax'];

/** 「自定义音色 ID」占位值（不是合法音色 ID）。 */
export const CUSTOM_VOICE = '__custom__';

export interface VoiceGroup {
  provider: string;
  category: string;
  items: VoiceOption[];
  /** optgroup 标题：`供应商 · 分类（数量）`。 */
  label: string;
}

/** 把音色按「供应商 → 分类」分组（Patch v4.15：只保留 MiniMax 音色）。 */
export function groupVoices(voices: VoiceOption[]): VoiceGroup[] {
  const map = new Map<string, VoiceOption[]>();
  for (const item of voices) {
    // Patch v4.15：剔除百炼 / 阿里等非 MiniMax 音色，下拉只展示 MiniMax 分组。
    if ((item.provider ?? 'qwen') !== 'minimax') continue;
    const provider = item.provider ?? 'minimax';
    const category = item.category || 'other';
    const key = `${provider}::${category}`;
    const bucket = map.get(key);
    if (bucket) bucket.push(item);
    else map.set(key, [item]);
  }
  const rank = (list: string[], value: string) => {
    const index = list.indexOf(value);
    return index === -1 ? list.length : index;
  };
  return Array.from(map.entries())
    .map(([key, items]) => {
      const [provider, category] = key.split('::');
      return { provider, category, items, label: '' };
    })
    .sort(
      (a, b) =>
        rank(PROVIDER_ORDER, a.provider) - rank(PROVIDER_ORDER, b.provider) ||
        rank(CATEGORY_ORDER, a.category) - rank(CATEGORY_ORDER, b.category),
    )
    .map((group) => ({
      ...group,
      label: `${PROVIDER_LABELS[group.provider] ?? group.provider} · ${
        CATEGORY_LABELS[group.category] ?? group.category
      }（${group.items.length}）`,
    }));
}

/** 在两套清单里按 ID 找音色（兼容 voice_id / id 两个字段）。 */
export function findVoiceOption(
  voices: VoiceOption[],
  voiceId: string,
): VoiceOption | undefined {
  return voices.find((item) => item.voice_id === voiceId || item.id === voiceId);
}

/**
 * 音色选择：原生 `<select>` 按「供应商 · 分类」分组 +「自定义音色 ID」手填。
 *
 * 不用 `<input list=datalist>`：浏览器原生 datalist 会按输入框里的现有值做前缀过滤，
 * 框内已有 "Ethan" 时候选只剩一条，用户无法浏览其余音色。
 */
export const VoiceSelect: React.FC<{
  value: string;
  voices: VoiceOption[];
  onChange: (voice: string) => void;
  ariaLabel: string;
  placeholder?: string;
  className?: string;
}> = ({ value, voices, onChange, ariaLabel, placeholder, className }) => {
  const [custom, setCustom] = useState(false);
  const [draft, setDraft] = useState(value);

  const groups = useMemo(() => groupVoices(voices), [voices]);
  const known = voices.some((item) => item.voice_id === value || item.id === value);

  if (custom) {
    return (
      <div className="space-y-1.5">
        <input
          type="text"
          value={draft}
          aria-label={ariaLabel}
          placeholder={placeholder ?? '输入音色 ID，例如 Chinese (Mandarin)_Radio_Host'}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => {
            const next = draft.trim();
            if (next && next !== value) onChange(next);
          }}
          className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none"
        />
        <button
          type="button"
          onClick={() => {
            setCustom(false);
            setDraft(value);
          }}
          className="text-[11px] text-blue-600 hover:underline cursor-pointer"
        >
          ← 返回下拉选择
        </button>
      </div>
    );
  }

  return (
    <select
      value={known ? value : CUSTOM_VOICE}
      aria-label={ariaLabel}
      onChange={(e) => {
        if (e.target.value === CUSTOM_VOICE) {
          setDraft(value);
          setCustom(true);
          return;
        }
        onChange(e.target.value);
      }}
      className={
        className ??
        'w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 outline-none cursor-pointer'
      }
    >
      {groups.map((group) => (
        <optgroup key={`${group.provider}:${group.category}`} label={group.label}>
          {group.items.map((item) => (
            <option key={item.voice_id} value={item.voice_id}>
              {item.display_name} · {item.gender === 'male' ? '男' : item.gender === 'female' ? '女' : item.gender}
            </option>
          ))}
        </optgroup>
      ))}
      <option value={CUSTOM_VOICE}>✎ 自定义音色 ID…</option>
    </select>
  );
};

/** 试听静态样例（播放后端预生成的 wav，按当前语速变速）。 */
export function useVoicePreview(onError?: (message: string) => void) {
  const [playingKey, setPlayingKey] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const stop = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
    }
    setPlayingKey(null);
  }, []);

  const preview = useCallback(
    (key: string, spec: VoiceSpec, voices: VoiceOption[]) => {
      const option = findVoiceOption(voices, spec.voice);
      if (!option?.sample_ready) return;
      stop();
      const audio = new Audio(option.sample_url);
      audio.playbackRate = spec.speed;
      audio.onended = () => setPlayingKey(null);
      audio.onerror = () => {
        setPlayingKey(null);
        onError?.('试听样例播放失败');
      };
      audioRef.current = audio;
      setPlayingKey(key);
      void audio.play().catch((err) => {
        setPlayingKey(null);
        onError?.(err instanceof Error ? err.message : String(err));
      });
    },
    [onError, stop],
  );

  return { playingKey, preview, stop };
}

/** 试听 / 停止试听按钮（两处页面共用）。 */
export const VoicePreviewButton: React.FC<{
  playing: boolean;
  sampleReady: boolean;
  onPreview: () => void;
  onStop: () => void;
}> = ({ playing, sampleReady, onPreview, onStop }) => (
  <div className="flex items-center gap-2">
    {playing ? (
      <button
        type="button"
        onClick={onStop}
        className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-red-700 bg-red-50 border border-red-200 rounded-lg hover:bg-red-100 transition-colors cursor-pointer"
      >
        <Square className="w-3.5 h-3.5" />
        停止试听
      </button>
    ) : (
      <button
        type="button"
        disabled={!sampleReady}
        title={
          sampleReady
            ? '播放静态试听样例（按当前语速变速，不调用 TTS）'
            : '样例未生成，请先运行 init_voice_samples.py'
        }
        onClick={onPreview}
        className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-blue-700 bg-blue-50 border border-blue-200 rounded-lg hover:bg-blue-100 transition-colors disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
      >
        <Play className="w-3.5 h-3.5" />
        试听样例
      </button>
    )}
    {!sampleReady && (
      <span className="text-[11px] text-amber-600 inline-flex items-center gap-1">
        <Volume2 className="w-3 h-3" />
        样例未生成
      </span>
    )}
  </div>
);
