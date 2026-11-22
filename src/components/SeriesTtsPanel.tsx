import React, { useEffect, useMemo, useState } from 'react';
import { ChevronDown, Loader2, Save, Volume2 } from 'lucide-react';
import type {
  SeriesTtsConfig,
  SeriesTtsConfigResponse,
  VoiceOption,
  VoiceSpec,
} from '../types/studio';
import {
  VoicePreviewButton,
  VoiceSelect,
  findVoiceOption,
  useVoicePreview,
} from './VoicePicker';

/**
 * 作品级配音设置面板（Patch v3.2 Patch 13；Patch v3.3 Patch 15 升级 34 音色；
 * Patch v3.4 Patch 16 接入 MiniMax 28 音色；Patch v4.15 音色只保留 MiniMax）。
 *
 * 四块设置：讲解者 / 通用男声 / 通用女声 / 已识别角色专属音色。
 * 每块 = 音色选择（Patch v4.15 起只列 MiniMax 音色，可手填目录外音色 ID）
 * + 语速滑块 + 试听样例按钮。
 *
 * 试听**只播放后端预生成的静态样例**，用 ``audio.playbackRate`` 实时变速，
 * 点击试听或拖动滑块都不会调用 TTS 合成接口。
 */

/** 语速滑块范围（规格 C3：0.75 ~ 1.50；后端 VoiceSpec 允许 0.6 ~ 1.8）。 */
const SPEED_MIN = 0.75;
const SPEED_MAX = 1.5;
const SPEED_STEP = 0.01;
const DEFAULT_SPEED = 1.18;

interface SeriesTtsPanelProps {
  seriesId: string;
}

type SlotKey = 'narrator' | 'default_male' | 'default_female';

/** 一块「音色 + 语速 + 试听」卡片。 */
interface VoiceCardProps {
  title: string;
  subtitle?: string;
  spec: VoiceSpec;
  voices: VoiceOption[];
  playing: boolean;
  onChange: (next: VoiceSpec) => void;
  onPreview: (spec: VoiceSpec) => void;
  onStop: () => void;
}

const VoiceCard: React.FC<VoiceCardProps> = ({
  title,
  subtitle,
  spec,
  voices,
  playing,
  onChange,
  onPreview,
  onStop,
}) => {
  const option = findVoiceOption(voices, spec.voice);
  const sampleReady = option?.sample_ready ?? false;
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 space-y-3">
      <div className="flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-slate-900">{title}</div>
          {subtitle && <div className="text-xs text-slate-500 mt-0.5 truncate">{subtitle}</div>}
        </div>
        <span className="text-[11px] text-slate-400 whitespace-nowrap">
          {option ? `${option.name} · ${option.gender === 'male' ? '男' : '女'}` : spec.voice}
        </span>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-[minmax(0,1fr)_220px] gap-3">
        <label className="block">
          <span className="block text-xs text-slate-500 mb-1">音色</span>
          <VoiceSelect
            value={spec.voice}
            voices={voices}
            ariaLabel={`${title}音色`}
            onChange={(voice) => onChange({ ...spec, voice })}
          />
          <span className="block text-[11px] text-slate-400 mt-1 truncate">
            {option
              ? `${option.description}`
              : `目录外音色 ID：${spec.voice}（无静态样例，无法试听）`}
          </span>
        </label>

        <label className="block">
          <span className="flex items-center justify-between text-xs text-slate-500 mb-1">
            <span>语速</span>
            <span className="font-mono-tabular text-slate-700">{spec.speed.toFixed(2)}x</span>
          </span>
          <input
            type="range"
            min={SPEED_MIN}
            max={SPEED_MAX}
            step={SPEED_STEP}
            value={spec.speed}
            onChange={(e) => onChange({ ...spec, speed: Number(e.target.value) })}
            className="w-full accent-blue-600 cursor-pointer"
            aria-label={`${title}语速`}
          />
        </label>
      </div>

      <VoicePreviewButton
        playing={playing}
        sampleReady={sampleReady}
        onPreview={() => onPreview(spec)}
        onStop={onStop}
      />
    </div>
  );
};

export const SeriesTtsPanel: React.FC<SeriesTtsPanelProps> = ({ seriesId }) => {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<SeriesTtsConfigResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const { playingKey, preview: previewSample, stop: stopPreview } = useVoicePreview(setError);

  /** Patch v4.15：音色选择仅保留 MiniMax（剔除百炼 / 阿里音色）。 */
  const voiceOptions: VoiceOption[] = useMemo(
    () => [...(data?.minimax_voices ?? [])],
    [data],
  );

  /** 试听静态样例：只设置 playbackRate，绝不调用 TTS 合成接口。 */
  const preview = (key: string, spec: VoiceSpec) => {
    if (!data) return;
    previewSample(key, spec, voiceOptions);
  };

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/series/${encodeURIComponent(seriesId)}/tts-config`);
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const payload = (await res.json()) as SeriesTtsConfigResponse;
      setData(payload);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (open && data === null && !loading) {
      void load();
    }
    // 折叠/卸载时停止播放
    return () => stopPreview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const patchSlot = (key: SlotKey, next: VoiceSpec) => {
    if (!data) return;
    setData({ ...data, [key]: next });
    setDirty(true);
  };

  const patchCharacter = (name: string, next: VoiceSpec) => {
    if (!data) return;
    setData({
      ...data,
      character_voices: { ...data.character_voices, [name]: next },
    });
    setDirty(true);
  };

  /** 角色未配置专属音色时的占位默认（按角色性别取通用音色）。 */
  const characterFallback = (gender: string): VoiceSpec => {
    if (!data) return { voice: 'Elias', speed: DEFAULT_SPEED };
    if (gender === 'male') return data.default_male;
    if (gender === 'female') return data.default_female;
    return data.narrator;
  };

  const save = async () => {
    if (!data) return;
    setSaving(true);
    setError(null);
    try {
      const body: Pick<SeriesTtsConfig, 'narrator' | 'default_male' | 'default_female' | 'character_voices'> = {
        narrator: data.narrator,
        default_male: data.default_male,
        default_female: data.default_female,
        character_voices: data.character_voices,
      };
      const res = await fetch(`/api/series/${encodeURIComponent(seriesId)}/tts-config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`;
        try {
          const payload = await res.json();
          if (typeof payload?.detail === 'string') detail = payload.detail;
        } catch {
          /* 非 JSON 响应保留状态码 */
        }
        throw new Error(detail);
      }
      const payload = (await res.json()) as SeriesTtsConfigResponse;
      setData(payload);
      setDirty(false);
      setSavedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="rounded-xl border border-slate-200 bg-white overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="w-full flex items-center justify-between gap-3 px-5 py-3.5 text-left hover:bg-slate-50 transition-colors cursor-pointer"
        aria-expanded={open}
      >
        <div className="flex items-center gap-2.5 min-w-0">
          <Volume2 className="w-4 h-4 text-blue-600 shrink-0" />
          <div className="min-w-0">
            <div className="text-sm font-semibold text-slate-900">作品配音设置（音色与语速）</div>
            <div className="text-xs text-slate-500 mt-0.5">
              讲解者 / 通用男声 / 通用女声 / 角色专属音色 · 试听只播放静态样例，不触发 TTS 合成
            </div>
          </div>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          {savedAt && !dirty && (
            <span className="text-xs text-emerald-600">已保存 {savedAt}</span>
          )}
          {dirty && <span className="text-xs text-amber-600">有未保存修改</span>}
          <ChevronDown
            className={`w-4 h-4 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`}
          />
        </div>
      </button>

      {open && (
        <div className="border-t border-slate-200 px-5 py-4 space-y-4">
          {loading && (
            <div className="flex items-center gap-2 text-sm text-slate-500 py-6 justify-center">
              <Loader2 className="w-4 h-4 animate-spin" />
              正在加载配音设置…
            </div>
          )}

          {error && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              {error}
            </div>
          )}

          {data && !loading && (
            <>
              <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
                <VoiceCard
                  title="讲解者（旁白）"
                  subtitle="所有解说句的默认音色"
                  spec={data.narrator}
                  voices={voiceOptions}
                  playing={playingKey === 'narrator'}
                  onChange={(next) => patchSlot('narrator', next)}
                  onPreview={(spec) => preview('narrator', spec)}
                  onStop={stopPreview}
                />
                <VoiceCard
                  title="通用男声"
                  subtitle="未单独配置的男性角色"
                  spec={data.default_male}
                  voices={voiceOptions}
                  playing={playingKey === 'default_male'}
                  onChange={(next) => patchSlot('default_male', next)}
                  onPreview={(spec) => preview('default_male', spec)}
                  onStop={stopPreview}
                />
                <VoiceCard
                  title="通用女声"
                  subtitle="未单独配置的女性角色"
                  spec={data.default_female}
                  voices={voiceOptions}
                  playing={playingKey === 'default_female'}
                  onChange={(next) => patchSlot('default_female', next)}
                  onPreview={(spec) => preview('default_female', spec)}
                  onStop={stopPreview}
                />
              </div>

              <div>
                <div className="flex items-center justify-between mb-2">
                  <h4 className="text-sm font-semibold text-slate-900">已识别角色专属音色</h4>
                  <span className="text-xs text-slate-400">
                    {data.characters.length} 个角色 · 已配置 {Object.keys(data.character_voices).length} 个
                  </span>
                </div>
                {data.characters.length === 0 ? (
                  <p className="text-xs text-slate-500 rounded-lg border border-dashed border-slate-300 px-4 py-6 text-center">
                    StoryBible 里还没有识别到角色，跑完 Stage 3/4 后会自动出现在这里。
                  </p>
                ) : (
                  <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                    {data.characters.map((character) => {
                      const key = `char:${character.name}`;
                      const spec = data.character_voices[character.name] ?? characterFallback(character.gender);
                      return (
                        <VoiceCard
                          key={character.name}
                          title={character.name}
                          subtitle={[
                            character.gender === 'unknown' ? '性别未知' : character.gender === 'male' ? '男' : '女',
                            character.aliases?.length ? `别名：${character.aliases.join('、')}` : null,
                            character.traits,
                          ]
                            .filter(Boolean)
                            .join(' · ')}
                          spec={spec}
                          voices={voiceOptions}
                          playing={playingKey === key}
                          onChange={(next) => patchCharacter(character.name, next)}
                          onPreview={(next) => preview(key, next)}
                          onStop={stopPreview}
                        />
                      );
                    })}
                  </div>
                )}
              </div>

              <div className="flex items-center justify-between gap-3 pt-1">
                <p className="text-[11px] text-slate-400">
                  语速范围 {SPEED_MIN.toFixed(2)}x ~ {SPEED_MAX.toFixed(2)}x，默认 {DEFAULT_SPEED.toFixed(2)}x；保存后下一次 Stage 5 配音生效。
                </p>
                <button
                  type="button"
                  disabled={!dirty || saving}
                  onClick={() => void save()}
                  className="inline-flex items-center gap-1.5 px-4 py-2 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
                >
                  {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
                  保存配音设置
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </section>
  );
};
