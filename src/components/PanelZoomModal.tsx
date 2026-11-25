import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  Check,
  ChevronLeft,
  ChevronRight,
  Loader2,
  Square,
  Volume2,
  X,
} from 'lucide-react';
import type {
  CameraMotion,
  PanelItem,
  PronunciationRule,
  ScriptBeat,
  VoiceOption,
} from '../types/studio';
import { VoicePreviewButton, findVoiceOption, groupVoices, useVoicePreview } from './VoicePicker';

/** 字幕原文自动保存的状态机。 */
export type SaveStatus = 'idle' | 'dirty' | 'saving' | 'saved' | 'error';

interface PanelZoomModalProps {
  open: boolean;
  beats: ScriptBeat[];
  panels: PanelItem[];
  activeBeatId: string | null;
  saveStatus: SaveStatus;
  saveMessage?: string;
  /** 可选说话人（与审核台表格同一份候选：不指定 / 讲解者 / 通用男声 / 通用女声 / 各角色名）。 */
  roleOptions: { value: string; label: string }[];
  /** 本句配音预览（角色 → 音色 → 语速），由审核台统一计算。 */
  voiceInfo: {
    label: string;
    voice: string;
    hint: string;
    /** Patch v4.4：当前生效语速（微调后等于 speed_override，否则等于角色默认）。 */
    speed: number;
    /** Patch v4.4：按角色解析的默认语速。 */
    roleSpeed: number;
    /** Patch v4.4：本句是否做了语速微调。 */
    speedOverridden: boolean;
  };
  /** 全部可选音色（仅用于试听当前生效音色的样例）。 */
  voiceOptions: VoiceOption[];
  onClose: () => void;
  onSelectBeat: (beatId: string) => void;
  onChangeNarration: (beatId: string, text: string) => void;
  /** 改本句说话人；音色由角色自动带出，不直接挑音色。 */
  onChangeRole: (beatId: string, role: string) => void;
  /**
   * Patch v4.27：逐句指定 MiniMax 音色（voice_override）。
   *
   * 传空串表示清除单句指定，恢复按角色解析；缺省时本弹窗不显示该控件。
   */
  onChangeVoiceOverride?: (beatId: string, voiceId: string) => void;
  /** Patch v4.4：改本句语速（传 null = 清空微调，恢复角色默认语速）。 */
  onChangeSpeed: (beatId: string, speed: number | null) => void;
  /**
   * Patch v4.25：弹窗内的「立即保存」回调。
   *
   * 返回 true 表示保存成功。缺省时快捷键不生效（编辑仍然会在切格 / 关闭时自动保存）。
   */
  onQuickSave?: () => Promise<boolean>;
  /**
   * Patch v4.35：试听本句「真实配音」（后端 Stage 5 生成的 WAV，与表格行试听同源；
   * 区别于音色静态样例 VoicePreviewButton）。
   *
   * 缺省时本弹窗不显示该按钮。
   */
  onPreviewAudio?: (beatId: string) => void;
  /** Patch v4.35：当前正在播放真实配音的 beat_id（用于按钮态高亮）。 */
  playingBeatId?: string | null;
  /**
   * Patch v4.35：编辑送音文本（整句，字幕不受影响）。
   *
   * 铁律：只改 tts_text，绝不碰 narration_text（字幕与送音必须分离）。
   * 缺省时本弹窗不显示送音文本编辑块。
   */
  onChangeTtsText?: (beatId: string, text: string) => void;
  /** Patch v4.35：改本句运镜；缺省时不渲染运镜下拉。 */
  onChangeMotion?: (beatId: string, motion: CameraMotion) => void;
  /** Patch v4.35：改本句句间停顿（毫秒）；缺省时不渲染停顿输入框。 */
  onChangePause?: (beatId: string, ms: number) => void;
  /** Patch v4.35：多音字词典（用于显示命中预览）。 */
  rules?: PronunciationRule[];
  /** Patch v4.35：送音标签（MiniMax 语气词 / 停顿）插入回调。 */
  onPickTag?: (beatId: string, snippet: string) => void;
}

const SAVE_STATUS_TEXT: Record<SaveStatus, string> = {
  idle: '尚未修改',
  dirty: '待保存',
  saving: '保存中…',
  saved: '已保存',
  error: '保存失败',
};

/** 说话人下拉的候选项（value 落库到 speaker_role，label 只用于显示）。 */
export interface RoleOption {
  value: string;
  label: string;
}

/**
 * Patch v4.35：运镜下拉的候选项。
 *
 * 中文标签便于在弹窗里一眼看懂；value 落库到 beat.motion（CameraMotion 字符串）。
 */
const MOTION_OPTIONS: { value: CameraMotion; label: string }[] = [
  { value: 'static', label: '静止' },
  { value: 'zoom_in', label: '推近' },
  { value: 'zoom_out', label: '拉远' },
  { value: 'pan_left', label: '左扫' },
  { value: 'pan_right', label: '右扫' },
  { value: 'pan_up', label: '上扫' },
  { value: 'pan_down', label: '下扫' },
  { value: 'shake', label: '震动' },
];

/**
 * Patch v4.35：本弹窗的送音文本口径（与审核台表格一致）。
 *
 * - 命中判定：source 出现在 narration_text 中即视为命中（replacement 为对应读音）；
 * - 实际送音：优先用 tts_text（人工自定义），否则用 narration_text；
 *   再把命中的词典项按 source→replacement 替换一遍。
 *
 * 铁律：这里只读 narration_text / tts_text，**绝不改字幕**。
 */
function resolveZoomSpoken(
  beat: ScriptBeat,
  rules: PronunciationRule[]
): { spokenText: string; matched: PronunciationRule[] } {
  const narration = beat.narration_text ?? '';
  const custom = (beat.tts_text ?? '').trim();
  const matched = rules.filter(
    (rule) => !!rule.source && narration.includes(rule.source)
  );
  const base = custom || narration;
  // 用 split/join 做全量替换（与 src/utils/pronunciation.ts 同一写法，避免 replaceAll 的 lib 依赖）。
  const spokenText = matched.reduce(
    (acc, rule) => acc.split(rule.source).join(rule.replacement),
    base
  );
  return { spokenText, matched };
}

/**
 * 分镜大图 + 字幕原文编辑弹窗。
 *
 * 存在的理由：审核台表格里的分镜只有 48×64 的小图，字与画面对不上号，
 * 改字幕只能盲改。这里把面板原图放大到可看清的尺寸，右侧直接编辑该条解说的字幕原文，
 * 并支持上一格 / 下一格连续翻看，编辑后自动落盘。
 */
export const PanelZoomModal: React.FC<PanelZoomModalProps> = ({
  open,
  beats,
  panels,
  activeBeatId,
  saveStatus,
  saveMessage,
  roleOptions,
  voiceInfo,
  voiceOptions,
  onClose,
  onSelectBeat,
  onChangeNarration,
  onChangeRole,
  onChangeVoiceOverride,
  onChangeSpeed,
  onQuickSave,
  onPreviewAudio,
  playingBeatId,
  onChangeTtsText,
  onChangeMotion,
  onChangePause,
  rules,
  onPickTag,
}) => {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const { playingKey, preview, stop } = useVoicePreview();
  // Patch v4.27：MiniMax 音色按「供应商 · 分类」分组，整幕共用一份。
  const voiceGroups = useMemo(() => groupVoices(voiceOptions), [voiceOptions]);

  const index = beats.findIndex((b) => b.beat_id === activeBeatId);
  const beat = index >= 0 ? beats[index] : undefined;
  // Patch v4.35：送音文本摘要（词典命中 + 实际送音），与审核台表格同一口径。
  const { spokenText, matched: matchedRules } = useMemo(
    () =>
      beat
        ? resolveZoomSpoken(beat, rules ?? [])
        : { spokenText: '', matched: [] as PronunciationRule[] },
    [beat, rules]
  );
  // Patch v4.35：送音文本默认折叠（先看摘要，需要时再展开整句编辑）。
  const [ttsExpanded, setTtsExpanded] = useState(false);
  useEffect(() => {
    setTtsExpanded(false);
  }, [activeBeatId]);
  // Patch v4.32：这一句实际会播出的全部分镜（Stage 6 按格数均分时长轮播）。
  // 以前只看第 1 格，成片里出现的第 2..N 格在审核台/弹窗里都看不见。
  const targetPanelIds = beat?.target_panel_ids ?? [];
  const [activePanelIndex, setActivePanelIndex] = useState(0);
  useEffect(() => {
    setActivePanelIndex(0);
  }, [activeBeatId]);
  const safePanelIndex =
    targetPanelIds.length === 0
      ? 0
      : Math.min(activePanelIndex, targetPanelIds.length - 1);
  const panelId = targetPanelIds[safePanelIndex] ?? '';
  const panel = panels.find((p) => p.panel_id === panelId);

  // 打开时把光标放进字幕框，直接就能改字。
  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => {
      textareaRef.current?.focus();
      // 光标落在文末（不整段选中，避免翻格后一打字就把整句覆盖掉）
      const len = textareaRef.current?.value.length ?? 0;
      textareaRef.current?.setSelectionRange(len, len);
    }, 50);
    return () => window.clearTimeout(timer);
  }, [open, activeBeatId]);

  // Esc 关闭；左右方向键翻页（焦点在文本框里时让给光标）。
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === 'TEXTAREA' || tag === 'INPUT' || target?.isContentEditable) return;
      if (index < 0) return;
      const nextIndex = event.key === 'ArrowLeft' ? index - 1 : index + 1;
      if (nextIndex < 0 || nextIndex >= beats.length) return;
      event.preventDefault();
      onSelectBeat(beats[nextIndex].beat_id);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open, index, beats, onClose, onSelectBeat]);

  /**
   * Patch v4.25：弹窗内快捷键。
   *
   * - Cmd/Ctrl+S      → 原地立即保存
   * - Cmd/Ctrl+Enter  → 保存当前格并跳到下一句（最后一句只保存，不跳）
   *
   * Cmd/Ctrl+Enter 在 textarea 里本来是「换行」，这里必须拦掉；
   * 保存期间不允许重复触发。
   */
  const quickSavingRef = useRef(false);
  useEffect(() => {
    if (!open || !onQuickSave) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (!event.metaKey && !event.ctrlKey) return;
      if (event.altKey) return;
      const key = event.key.toLowerCase();
      if (key !== 's' && key !== 'enter') return;
      event.preventDefault();
      if (quickSavingRef.current) return;
      quickSavingRef.current = true;
      void (async () => {
        try {
          const ok = await onQuickSave();
          if (!ok) return;
          if (key === 'enter' && index >= 0 && index < beats.length - 1) {
            onSelectBeat(beats[index + 1].beat_id);
          }
        } finally {
          quickSavingRef.current = false;
        }
      })();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open, onQuickSave, index, beats, onSelectBeat]);

  if (!open || !beat) return null;

  const charLen = beat.narration_text.trim().length;

  // Patch v4.2：说话人由「角色」决定，音色自动带出，不再让用户直接挑音色。
  // voiceOverride 只用于提示历史遗留的强制指定（改角色时会被清掉）。
  const voiceOverride = beat.voice_override ?? '';
  const previewOption = findVoiceOption(voiceOptions, voiceInfo.voice);
  // Patch v4.27：按角色解析时用的音色（单句未指定时的兜底说明）。
  const roleVoiceId = beat.resolved_voice?.voice ?? '';
  const roleVoiceName =
    findVoiceOption(voiceOptions, roleVoiceId)?.display_name || roleVoiceId || '按角色解析';
  const isLengthWarn = charLen < 8 || charLen > 30;
  const canPrev = index > 0;
  const canNext = index < beats.length - 1;

  // Patch v4.35：本句是否已有真实配音（Stage 5 合成后才会写回 audio_duration_ms）。
  // 没有真实时长 = 这一句还没有 WAV，按钮置灰并提示「尚未生成配音」。
  const hasRealAudio = (beat.audio_duration_ms ?? 0) > 0;
  const isRealAudioPlaying = playingBeatId === beat.beat_id;
  // Patch v4.35：送音文本是否被人工改过（tts_text 非空即视为已自定义）。
  const hasCustomTts = (beat.tts_text ?? '').trim().length > 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/80 backdrop-blur-sm"
      onClick={onClose}
      role="presentation"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="分镜大图与字幕原文编辑"
        className="w-full max-w-6xl max-h-[92vh] bg-white rounded-xl shadow-2xl overflow-hidden flex flex-col"
        onClick={(event) => event.stopPropagation()}
      >
        {/* 顶部：定位信息与关闭 */}
        <div className="flex items-center justify-between gap-3 px-5 py-3 border-b border-slate-200 shrink-0">
          <div className="flex items-center gap-3 min-w-0">
            <span className="font-mono-tabular text-sm font-semibold text-slate-900">
              {beat.beat_id}
            </span>
            <span className="font-mono-tabular text-xs text-slate-500">{panelId}</span>
            <span className="text-xs text-slate-400 font-mono-tabular">
              第 {index + 1} / {beats.length} 条
            </span>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭弹窗"
            className="p-1.5 text-slate-400 hover:text-slate-700 cursor-pointer"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_360px]">
          {/* 左：大图 */}
          <div className="min-h-0 bg-slate-900 flex flex-col items-center justify-center p-4 gap-3 overflow-hidden">
            {panel ? (
              <img
                src={panel.image_url}
                alt={`${panel.panel_id} 分镜大图`}
                referrerPolicy="no-referrer"
                className="max-h-full max-w-full object-contain rounded"
              />
            ) : (
              <p className="text-xs text-slate-400">该条解说未绑定分镜</p>
            )}
            {/* Patch v4.32：一句多格时把每一格都列出来（成片会按格数均分时长轮播）。 */}
            {targetPanelIds.length > 1 && (
              <>
                {/* Patch v4.35：标出当前看的是第几格 / 一共几格。 */}
                <p className="text-[11px] font-mono-tabular text-slate-400">
                  第 {safePanelIndex + 1} / {targetPanelIds.length} 格
                </p>
                <div className="flex flex-wrap gap-2 justify-center">
                  {targetPanelIds.map((pid, i) => {
                    const item = panels.find((p) => p.panel_id === pid);
                    const active = i === safePanelIndex;
                    return (
                      <button
                        key={`${pid}-${i}`}
                        type="button"
                        onClick={() => setActivePanelIndex(i)}
                        aria-label={`第 ${i + 1} 格分镜 ${pid}`}
                        title={`第 ${i + 1} 格：${pid}（成片会按格数均分时长轮播）`}
                        className={
                          active
                            ? 'w-12 h-16 rounded border-2 border-blue-500 overflow-hidden'
                            : 'w-12 h-16 rounded border border-white/30 overflow-hidden opacity-70 hover:opacity-100'
                        }
                      >
                        {item ? (
                          <img
                            src={item.image_url}
                            alt={pid}
                            referrerPolicy="no-referrer"
                            loading="lazy"
                            className="w-12 h-16 object-cover"
                          />
                        ) : (
                          <span className="text-[10px] text-slate-300 font-mono-tabular">
                            {pid}
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>
              </>
            )}
          </div>

          {/* 右：字幕原文编辑 */}
          <div className="min-h-0 overflow-y-auto p-5 space-y-4 bg-white">
            {panel?.scene_summary && (
              <div>
                <div className="text-[11px] font-medium text-slate-400 mb-1">画面摘要</div>
                <p className="text-xs text-slate-600 leading-relaxed">{panel.scene_summary}</p>
              </div>
            )}

            {/* Patch v4.35：试听本句「真实配音」。
                与角色块里的 VoicePreviewButton 是两回事：那里播的是音色静态样例，
                这里播的是 Stage 5 为这一句合成的实际 WAV（与审核台表格行试听同源）。 */}
            {onPreviewAudio && (
              <div className="rounded-lg border border-slate-200 bg-slate-50/70 px-3 py-2.5">
                <div className="text-[11px] font-medium text-slate-700">
                  试听本句真实配音
                </div>
                <p className="mt-0.5 text-[10px] leading-relaxed text-slate-400">
                  播放 Stage 5 为这一句合成的实际配音（不是音色静态样例）。
                </p>
                <button
                  type="button"
                  onClick={() => onPreviewAudio(beat.beat_id)}
                  disabled={!hasRealAudio}
                  aria-label="试听本句真实配音"
                  title={
                    hasRealAudio
                      ? '播放 Stage 5 生成的这一句实际配音（再次点击可停止）'
                      : '尚未生成配音：请先在审核台跑完 Stage 5 配音'
                  }
                  className={`mt-2 inline-flex w-full items-center justify-center gap-1.5 rounded-lg border px-3 py-2 text-xs font-medium transition-colors ${
                    isRealAudioPlaying
                      ? 'cursor-pointer border-blue-600 bg-blue-50 text-blue-700'
                      : 'cursor-pointer border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
                  } disabled:cursor-not-allowed disabled:opacity-50`}
                >
                  {isRealAudioPlaying ? (
                    <>
                      <Square className="w-3.5 h-3.5" />
                      试听中…
                    </>
                  ) : (
                    <>
                      <Volume2 className="w-3.5 h-3.5" />
                      试听本句真实配音
                    </>
                  )}
                </button>
                {!hasRealAudio && (
                  <p className="mt-1 text-[11px] text-amber-600">尚未生成配音</p>
                )}
              </div>
            )}

            <div>
              <label
                htmlFor="zoom-narration-text"
                className="block text-xs font-medium text-slate-700 mb-1.5"
              >
                字幕原文 (narration_text · 屏幕显示)
              </label>
              <textarea
                id="zoom-narration-text"
                ref={textareaRef}
                rows={6}
                value={beat.narration_text}
                onChange={(event) => onChangeNarration(beat.beat_id, event.target.value)}
                className="w-full p-3 text-sm text-slate-900 bg-white border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600 resize-y"
              />
              <div className="flex items-center justify-between mt-1.5 text-[11px] font-mono-tabular">
                <span className={isLengthWarn ? 'text-amber-600' : 'text-slate-400'}>
                  {charLen} 字（建议 10–28 字）
                </span>
                <span className="text-slate-400">
                  时长 {(beat.audio_duration_ms / 1000).toFixed(2)}s
                </span>
              </div>
            </div>

            {/* Patch v4.35：送音文本（tts_text）——只影响配音，字幕原文不受影响。 */}
            {onChangeTtsText && (
              <div className="border-t border-slate-100 pt-3">
                <div className="mb-1.5 flex items-center justify-between gap-2">
                  <label
                    htmlFor={ttsExpanded ? 'zoom-tts-text' : undefined}
                    className="block text-xs font-medium text-slate-700"
                  >
                    送音文本（tts_text · 只影响配音，不改字幕）
                  </label>
                  {!ttsExpanded && (
                    <button
                      type="button"
                      onClick={() => setTtsExpanded(true)}
                      aria-label="改送音文本或加语气、停顿标签"
                      title="展开编辑送音文本（整句送音，字幕不变）"
                      className="shrink-0 cursor-pointer rounded-md border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] text-slate-600 hover:border-blue-400 hover:text-blue-700"
                    >
                      改送音 / 加标签
                    </button>
                  )}
                </div>
                {!ttsExpanded ? (
                  <p className="text-[11px] leading-relaxed">
                    {/* 已自定义 = tts_text 非空（此时实际送音以 tts_text 为准）。 */}
                    {hasCustomTts && (
                      <span className="text-amber-600">已自定义</span>
                    )}
                    {matchedRules.length > 0 ? (
                      <span className="text-blue-700">
                        {hasCustomTts && ' · '}
                        词典命中：
                        {matchedRules
                          .map((rule) => `${rule.source}→${rule.replacement}`)
                          .join('、')}
                        {' · '}实际送音：{spokenText}
                      </span>
                    ) : hasCustomTts ? (
                      <span className="text-slate-600">
                        {' · '}实际送音：{spokenText}
                      </span>
                    ) : (
                      <span className="text-slate-400">同字幕</span>
                    )}
                  </p>
                ) : (
                  <div className="space-y-2">
                    <textarea
                      id="zoom-tts-text"
                      rows={5}
                      value={beat.tts_text ?? ''}
                      onChange={(event) =>
                        onChangeTtsText(beat.beat_id, event.target.value)
                      }
                      placeholder="留空则自动继承字幕 + 词典替换；填写时必须是整句送音文本"
                      className="w-full resize-y rounded-lg border border-slate-200 bg-slate-50 p-3 text-sm text-slate-900 focus:border-blue-600 focus:outline-none"
                    />
                    {matchedRules.length > 0 && (
                      <p className="text-[11px] leading-relaxed text-blue-700">
                        词典命中：
                        {matchedRules
                          .map((rule) => `${rule.source}→${rule.replacement}`)
                          .join('、')}
                      </p>
                    )}
                    <div className="flex items-center gap-2">
                      {onPickTag && (
                        <button
                          type="button"
                          onClick={() => onPickTag(beat.beat_id, '(laughs)')}
                          aria-label="插入语气或停顿标签"
                          title="插入 MiniMax 语气词（如 (laughs)）或句中停顿（如 <#0.5#>）"
                          className="cursor-pointer rounded-md border border-slate-200 bg-white px-2 py-1 text-[11px] text-slate-600 hover:border-blue-400 hover:text-blue-700"
                        >
                          插入语气 / 停顿标签
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => setTtsExpanded(false)}
                        aria-label="收起送音文本编辑"
                        className="cursor-pointer rounded-md border border-slate-200 bg-white px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-50"
                      >
                        收起
                      </button>
                    </div>
                    <p className="text-[10px] leading-relaxed text-slate-400">
                      这里只改送音文本（tts_text），字幕原文不会被改动；留空表示自动继承字幕 + 词典替换。
                    </p>
                  </div>
                )}
              </div>
            )}

            {/* Patch v4.2：说话人角色（音色自动带出）+ 情绪指令 */}
            <div className="space-y-2 border-t border-slate-100 pt-3">
              <div>
                <label
                  htmlFor="zoom-role-select"
                  className="block text-xs font-medium text-slate-700 mb-1.5"
                >
                  配音角色（speaker_role · 本句说话人）
                </label>
                <select
                  id="zoom-role-select"
                  aria-label="本句配音角色"
                  value={beat.speaker_role ?? ''}
                  onChange={(event) =>
                    onChangeRole(beat.beat_id, event.target.value)
                  }
                  className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 focus:border-blue-600 focus:outline-none cursor-pointer"
                >
                  {roleOptions.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
                <div className="mt-1.5 flex items-start justify-between gap-2">
                  <p className="text-[11px] text-slate-500 leading-relaxed">
                    本句配音：{voiceInfo.label} · {voiceInfo.voice}
                    {voiceOverride && (
                      <span className="text-amber-600">
                        （本句已单独指定音色 {voiceOverride}，改角色后将按角色解析）
                      </span>
                    )}
                  </p>
                  <div className="shrink-0">
                    <VoicePreviewButton
                      playing={playingKey === 'zoomVoice'}
                      sampleReady={!!previewOption?.sample_ready}
                      onPreview={() =>
                        preview(
                          'zoomVoice',
                          {
                            voice: voiceInfo.voice,
                            speed: beat.resolved_voice?.speed ?? 1,
                          },
                          voiceOptions
                        )
                      }
                      onStop={stop}
                    />
                  </div>
                </div>
                {voiceInfo.hint && (
                  <p className="mt-1 text-[11px] text-slate-400 leading-relaxed">
                    {voiceInfo.hint}
                  </p>
                )}
              </div>

              {/* Patch v4.27：单句指定 MiniMax 音色（一次性 / 龙套角色单独配音）。
                  只改这一句的 voice_override，不动角色；选回「跟随角色」即清除指定。 */}
              {onChangeVoiceOverride && (
                <div>
                  <label
                    htmlFor="zoom-voice-override-select"
                    className="block text-xs font-medium text-slate-700 mb-1.5"
                  >
                    单句指定 MiniMax 音色（可选）
                  </label>
                  <select
                    id="zoom-voice-override-select"
                    aria-label="本句指定 MiniMax 音色"
                    value={voiceOverride}
                    onChange={(event) =>
                      onChangeVoiceOverride(beat.beat_id, event.target.value)
                    }
                    className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 focus:border-blue-600 focus:outline-none cursor-pointer"
                  >
                    <option value="">跟随角色（{roleVoiceName}）</option>
                    {voiceOverride && !findVoiceOption(voiceOptions, voiceOverride) && (
                      <option value={voiceOverride}>自定义：{voiceOverride}</option>
                    )}
                    {voiceGroups.map((group) => (
                      <optgroup
                        key={`${group.provider}-${group.category}`}
                        label={group.label}
                      >
                        {group.items.map((item) => {
                          const id = item.voice_id || item.id;
                          return (
                            <option key={id} value={id}>
                              {item.display_name} ·{' '}
                              {item.gender === 'female' ? '女' : '男'}
                            </option>
                          );
                        })}
                      </optgroup>
                    ))}
                  </select>
                  <p className="mt-1 text-[11px] text-slate-400 leading-relaxed">
                    指定后这一句固定用该音色合成（用于一次性 / 龙套角色）；
                    「试听当前音色」会跟着切换。改说话人角色会清除这里的指定。
                  </p>
                </div>
              )}

              {/* Patch v4.4：单句语速微调（只作用于本句 TTS 合成，不碰角色默认配置） */}
              <div className="space-y-1.5">
                <div className="flex items-center justify-between gap-2">
                  <label
                    htmlFor="zoom-speed-range"
                    className="text-xs font-medium text-slate-700"
                  >
                    本句语速（speed_override · 本句TTS合成）
                  </label>
                  <span className="text-xs font-mono-tabular text-slate-600">
                    {voiceInfo.speed.toFixed(2)}x
                  </span>
                </div>
                <div className="flex items-center justify-between gap-2 text-[11px]">
                  {voiceInfo.speedOverridden ? (
                    <>
                      <span className="text-slate-500">
                        角色默认 {voiceInfo.roleSpeed.toFixed(2)}x
                      </span>
                      <button
                        type="button"
                        onClick={() => onChangeSpeed(beat.beat_id, null)}
                        className="shrink-0 text-blue-600 hover:text-blue-700 hover:underline cursor-pointer"
                      >
                        恢复默认
                      </button>
                    </>
                  ) : (
                    <span className="text-slate-400">角色默认</span>
                  )}
                </div>
                <input
                  id="zoom-speed-range"
                  type="range"
                  min={0.75}
                  max={1.5}
                  step={0.01}
                  value={voiceInfo.speed}
                  aria-label="本句语速"
                  onChange={(event) =>
                    onChangeSpeed(beat.beat_id, Number(event.target.value))
                  }
                  className="w-full accent-blue-600 cursor-pointer"
                />
              </div>
            </div>

            {/* Patch v4.35：本句运镜与句间停顿（成片级参数，与审核台表格同源）。 */}
            {(onChangeMotion || onChangePause) && (
              <div className="space-y-2 border-t border-slate-100 pt-3">
                <div className="text-[11px] font-medium text-slate-400">运镜与停顿</div>
                {onChangeMotion && (
                  <div>
                    <label
                      htmlFor="zoom-motion-select"
                      className="mb-1.5 block text-xs font-medium text-slate-700"
                    >
                      本句运镜（motion · 成片镜头运动）
                    </label>
                    <select
                      id="zoom-motion-select"
                      aria-label="本句运镜"
                      value={beat.motion}
                      onChange={(event) =>
                        onChangeMotion(
                          beat.beat_id,
                          event.target.value as CameraMotion
                        )
                      }
                      className="w-full cursor-pointer rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 focus:border-blue-600 focus:outline-none"
                    >
                      {MOTION_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </div>
                )}
                {onChangePause && (
                  <div>
                    <label
                      htmlFor="zoom-pause-input"
                      className="mb-1.5 block text-xs font-medium text-slate-700"
                    >
                      句间停顿（pause_after_ms · 毫秒）
                    </label>
                    <div className="flex items-center gap-1.5">
                      <input
                        id="zoom-pause-input"
                        type="number"
                        step={50}
                        min={0}
                        max={2000}
                        value={beat.pause_after_ms ?? 0}
                        onChange={(event) =>
                          onChangePause(
                            beat.beat_id,
                            // Patch v4.8 同款口径：整数 + 0~2000ms 钳位，避免越界值让时间轴算崩。
                            Math.max(
                              0,
                              Math.min(
                                2000,
                                Math.round(Number(event.target.value) || 0)
                              )
                            )
                          )
                        }
                        // Patch v4.29：数字框太窄时 Chrome 的上下微调箭头会把数字整段挤掉，
                        // 给足最小宽度并用 hide-spinner 隐藏箭头（见 index.css）。
                        className="hide-spinner w-auto min-w-[76px] flex-1 rounded border border-slate-200 bg-white px-2 py-1 font-mono-tabular text-[11px]"
                      />
                      <span className="text-[11px] text-slate-400">ms</span>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* 保存状态 */}
            <div
              className="flex items-center gap-1.5 text-[11px]"
              role="status"
              aria-live="polite"
            >
              {saveStatus === 'saving' && (
                <>
                  <Loader2 className="w-3.5 h-3.5 text-blue-600 animate-spin" />
                  <span className="text-slate-500">保存中…</span>
                </>
              )}
              {saveStatus === 'saved' && (
                <>
                  <Check className="w-3.5 h-3.5 text-emerald-600" />
                  <span className="text-emerald-700">
                    已保存{saveMessage ? ` ${saveMessage}` : ''}
                  </span>
                </>
              )}
              {saveStatus === 'error' && (
                <>
                  <AlertCircle className="w-3.5 h-3.5 text-red-600" />
                  <span className="text-red-600">
                    保存失败{saveMessage ? `：${saveMessage}` : '，请重试'}
                  </span>
                </>
              )}
              {(saveStatus === 'dirty' || saveStatus === 'idle') && (
                <>
                  <span className="text-slate-400">
                    {saveStatus === 'dirty'
                      ? '待保存 · 切换分镜或关闭弹窗时自动保存'
                      : SAVE_STATUS_TEXT.idle}
                  </span>
                </>
              )}
            </div>

            {/* 上下格切换 */}
            <div className="flex items-center gap-2 pt-1">
              <button
                type="button"
                onClick={() => canPrev && onSelectBeat(beats[index - 1].beat_id)}
                disabled={!canPrev}
                className="inline-flex items-center gap-1 px-3 py-2 text-xs font-medium text-slate-700 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
              >
                <ChevronLeft className="w-3.5 h-3.5" />
                上一格
              </button>
              <button
                type="button"
                onClick={() => canNext && onSelectBeat(beats[index + 1].beat_id)}
                disabled={!canNext}
                className="inline-flex items-center gap-1 px-3 py-2 text-xs font-medium text-slate-700 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
              >
                下一格
                <ChevronRight className="w-3.5 h-3.5" />
              </button>
            </div>

            <p className="text-[11px] text-slate-400 leading-relaxed">
              快捷键：Esc 关闭 · ←/→ 翻格（焦点在输入框内时为移动光标）
              <br />
              Cmd/Ctrl+S 立即保存 · Cmd/Ctrl+Enter 保存并跳到下一句（焦点在字幕框内也生效）
              <br />
              修改字幕原文、配音角色、语速后，在切换分镜或关闭弹窗时自动保存，不会触发重新配音；如需重出配音请回审核台点「保存并从 Stage 5 重跑配音」。
            </p>
          </div>
        </div>
      </div>
    </div>
  );
};
