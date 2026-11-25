import React, { useEffect, useMemo, useState } from 'react';
import { Clock, Volume2, X } from 'lucide-react';
import {
  INTERJECTION_TAG_GROUPS,
  PAUSE_QUICK_SECONDS,
  PAUSE_TAG_MAX_SECONDS,
  PAUSE_TAG_MIN_SECONDS,
  formatPauseTag,
  isSpeech28Model,
} from '../constants/ttsTags';
import type { SfxLibraryItem } from '../types/studio';

/**
 * 送音标签浮层面板（语气词 + 句中停顿 + 句中音效）。
 *
 * 本组件是**受控浮层的内容部分**：开合与位置由父组件负责挂载，
 * 这里只渲染内容并通过 `onPick` / `onClose` 回调把意图抛回去。
 *
 * 三类标签的插入位置要求不同，所以提示分处给：
 * - 语气词：任何位置都可插入；
 * - 停顿标签：必须夹在句中两段文字之间，因此只在「句中停顿」Tab 内给出位置类提醒；
 * - 音效标签（Patch v4.40）：写在句中就落在那句话说完的那一刻，
 *   与 MiniMax 的语气词/停顿标签不是一套能力，因此**不**提示 speech-2.8 兼容性。
 */
export interface TtsTagPickerProps {
  /** 当前该句的送音文本（tts_text），用于判断「空文本 / 选区」并给出提示。 */
  value: string;
  /** 最近一次已知选区；null 或 start===end 视为「无选区（插入点在光标处）」。 */
  selection: { start: number; end: number } | null;
  /** 用户选中一个标签/停顿后回调。snippet 是要写入的原文（如 "(laughs)" 或 "<#0.5#>"）。 */
  onPick: (snippet: string) => void;
  /** 点「关闭」或按 Esc 时回调。 */
  onClose: () => void;
  /** 当前 MiniMax 模型名；非 speech-2.8-* 时在顶部显示温和提醒。 */
  ttsModel?: string;
  /** Patch v4.40：24 个标准音效（用于「句中音效」Tab 的卡片网格）。 */
  sfxLibrary?: SfxLibraryItem[];
  /** Patch v4.40：试听某个音效（由父组件负责播放与复用单一 Audio 实例）。 */
  onPreviewSfx?: (sfxKey: string) => void;
}

type PickerTab = 'interjection' | 'pause' | 'sfx';

/** 面板底部固定提示（文案为约定原文，不要改写）。 */
const PAUSE_FOOTER_HINT = '停顿标签仅用于句中两段文字之间，不可连用；句尾停顿请用右侧停顿ms';

/** Patch v4.40：句中音效 Tab 的底部提示。 */
const SFX_FOOTER_HINT = '只作用于配音句内落点、不印字幕，保存后重跑 S5 生效';

/**
 * Patch v4.41：句中音效 Tab 的补充提示——内联音效渲染时单独降一档增益。
 *
 * 为什么：内联音效落在解说正文**中间**，与这一句的人声同段共存；
 * 整句级音效落在句首/句末，前后是干净空隙，可以给足音量。
 */
const SFX_GAIN_HINT = '句中音效比整句音效轻一档（默认 -6dB），避免压住正在念的词';

/** 停顿秒数输入的合法形状：整数或最多两位小数（允许 ".5" 写法）。 */
const PAUSE_INPUT_PATTERN = /^(\d+(\.\d{1,2})?|\.\d{1,2})$/;

/** 停顿标签形状：`<#0.5#>`（允许中间有空白）。 */
const PAUSE_TAG_AT_END_PATTERN = /<#\s*[^#]*#>\s*$/;
const PAUSE_TAG_AT_START_PATTERN = /^\s*<#\s*[^#]*#>/;

export const TtsTagPicker: React.FC<TtsTagPickerProps> = ({
  value,
  selection,
  onPick,
  onClose,
  ttsModel,
  sfxLibrary,
  onPreviewSfx,
}) => {
  const [tab, setTab] = useState<PickerTab>('interjection');
  const [pauseInput, setPauseInput] = useState('');

  // Patch v4.40：音效按分类分组展示（顺序沿用后端 catalog 的分类顺序）。
  const sfxGroups = useMemo(() => {
    const groups: { category: string; items: SfxLibraryItem[] }[] = [];
    const index = new Map<string, { category: string; items: SfxLibraryItem[] }>();
    (sfxLibrary ?? []).forEach((item) => {
      const category = item.category || '其它';
      let group = index.get(category);
      if (!group) {
        group = { category, items: [] };
        index.set(category, group);
        groups.push(group);
      }
      group.items.push(item);
    });
    return groups;
  }, [sfxLibrary]);

  // Esc 关闭：浮层由父组件挂载，这里只挂一个文档级监听，卸载时清理。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.stopPropagation();
      onClose();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  // 选区越界时收敛到文本长度内，避免父组件传来脏数据导致切片异常。
  const range = useMemo(() => {
    const length = value.length;
    const rawStart = selection ? selection.start : length;
    const rawEnd = selection ? selection.end : rawStart;
    const start = Math.max(0, Math.min(rawStart, length));
    const end = Math.max(start, Math.min(rawEnd, length));
    return { start, end };
  }, [selection, value.length]);

  /** 选区两侧紧挨着（允许中间只有空白）是否已有另一个停顿标签。 */
  const hasAdjacentPauseTag = useMemo(() => {
    const before = value.slice(0, range.start);
    const after = value.slice(range.end);
    return PAUSE_TAG_AT_END_PATTERN.test(before) || PAUSE_TAG_AT_START_PATTERN.test(after);
  }, [value, range.start, range.end]);

  /** 选区落在句首或句尾 → 停顿标签没有「两段文字之间」可放。 */
  const atTextEdge = range.start === 0 || range.end >= value.length;

  const trimmedPauseInput = pauseInput.trim();
  const pauseSeconds = Number(trimmedPauseInput);
  const pauseInputValid =
    trimmedPauseInput !== '' &&
    PAUSE_INPUT_PATTERN.test(trimmedPauseInput) &&
    Number.isFinite(pauseSeconds) &&
    pauseSeconds >= PAUSE_TAG_MIN_SECONDS &&
    pauseSeconds <= PAUSE_TAG_MAX_SECONDS;
  const pauseInputInvalid = trimmedPauseInput !== '' && !pauseInputValid;

  const modelNotice =
    ttsModel && !isSpeech28Model(ttsModel)
      ? `当前模型 ${ttsModel} 不是 speech-2.8 系列，语气词与停顿标签可能不生效；推荐 speech-2.8-hd / speech-2.8-turbo`
      : '';
  const emptyTextNotice = value.trim() === '' ? '送音文本为空，插入后会先铺底字幕原文' : '';
  const tabButtonClass = (active: boolean) =>
    active
      ? 'px-2.5 py-1 rounded-lg border border-blue-200 bg-blue-50 text-blue-700 font-medium transition-colors cursor-pointer'
      : 'px-2.5 py-1 rounded-lg border border-transparent text-slate-500 hover:bg-slate-50 hover:text-slate-700 transition-colors cursor-pointer';

  return (
    <div className="w-[22rem] rounded-xl border border-slate-200 bg-white shadow-lg overflow-hidden">
      {/* 顶部：标题 + 关闭 */}
      <div className="flex items-center justify-between border-b border-slate-100 px-3 py-2">
        <div className="flex items-center gap-1.5">
          <Volume2 className="h-4 w-4 text-blue-600" />
          <h2 className="text-xs font-semibold text-slate-800">送音标签</h2>
        </div>
        <button
          type="button"
          onClick={onClose}
          title="关闭"
          aria-label="关闭标签面板"
          className="rounded-md p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700 transition-colors cursor-pointer"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* 温和提醒：不阻塞操作，只提示风险 */}
      <div className="px-3 pt-2 space-y-1">
        {modelNotice && tab !== 'sfx' && (
          <p className="rounded-lg border border-amber-200 bg-amber-50 px-2 py-1 text-[11px] leading-4 text-amber-700">
            {modelNotice}
          </p>
        )}
        {emptyTextNotice && (
          <p className="rounded-lg border border-slate-200 bg-slate-50 px-2 py-1 text-[11px] leading-4 text-slate-500">
            {emptyTextNotice}
          </p>
        )}
      </div>

      {/* Tab 切换 */}
      <div className="flex items-center gap-1 px-3 pt-2">
        <button type="button" onClick={() => setTab('interjection')} className={tabButtonClass(tab === 'interjection')}>
          语气词
        </button>
        <button type="button" onClick={() => setTab('pause')} className={tabButtonClass(tab === 'pause')}>
          句中停顿
        </button>
        <button type="button" onClick={() => setTab('sfx')} className={tabButtonClass(tab === 'sfx')}>
          句中音效
        </button>
      </div>

      {/* 内容区：可滚动 */}
      <div className="max-h-[22rem] overflow-y-auto px-3 py-2.5">
        {tab === 'interjection' ? (
          <div className="space-y-3">
            {INTERJECTION_TAG_GROUPS.map((group) => (
              <div key={group.group} className="space-y-1.5">
                <p className="text-[11px] font-medium text-slate-500">{group.group}</p>
                <div className="grid grid-cols-3 gap-1.5">
                  {group.tags.map((def) => (
                    <button
                      key={def.tag}
                      type="button"
                      title={def.hint ?? def.tag}
                      onClick={() => onPick(def.tag)}
                      className="flex flex-col items-start gap-0.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-left transition-colors hover:border-blue-300 hover:bg-blue-50 cursor-pointer"
                    >
                      <span className="text-xs font-medium text-slate-800">{def.label}</span>
                      <span className="font-mono text-[10px] text-slate-400">{def.tag}</span>
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        ) : tab === 'pause' ? (
          <div className="space-y-3">
            {/* 快捷秒数 */}
            <div className="space-y-1.5">
              <p className="flex items-center gap-1 text-[11px] font-medium text-slate-500">
                <Clock className="h-3 w-3" />
                快捷停顿
              </p>
              <div className="flex flex-wrap gap-1.5">
                {PAUSE_QUICK_SECONDS.map((seconds) => (
                  <button
                    key={seconds}
                    type="button"
                    onClick={() => onPick(formatPauseTag(seconds))}
                    className="rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-xs text-slate-700 transition-colors hover:border-blue-300 hover:bg-blue-50 cursor-pointer"
                  >
                    {seconds.toFixed(1)}s
                  </button>
                ))}
              </div>
            </div>

            {/* 自定义秒数 */}
            <div className="space-y-1.5">
              <p className="text-[11px] font-medium text-slate-500">自定义停顿</p>
              <div className="flex items-center gap-1.5">
                <input
                  type="number"
                  inputMode="decimal"
                  step="0.01"
                  min={PAUSE_TAG_MIN_SECONDS}
                  max={PAUSE_TAG_MAX_SECONDS}
                  value={pauseInput}
                  onChange={(e) => setPauseInput(e.target.value)}
                  placeholder="秒数"
                  aria-label="自定义停顿秒数"
                  className="w-20 rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs text-slate-900 focus:border-blue-500 focus:outline-none"
                />
                <button
                  type="button"
                  disabled={!pauseInputValid}
                  onClick={() => {
                    if (!pauseInputValid) return;
                    onPick(formatPauseTag(pauseSeconds));
                  }}
                  className="rounded-lg border border-blue-200 bg-blue-50 px-2.5 py-1 text-xs font-medium text-blue-700 transition-colors hover:bg-blue-100 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
                >
                  插入停顿
                </button>
              </div>
              {pauseInputInvalid && (
                <p className="text-[11px] leading-4 text-amber-600">
                  停顿秒数需在 {PAUSE_TAG_MIN_SECONDS} ~ {PAUSE_TAG_MAX_SECONDS} 之间，最多两位小数
                </p>
              )}
            </div>

            {/* 停顿位置类提醒：只在停顿 Tab 给（此处插入的才是停顿标签） */}
            {hasAdjacentPauseTag && (
              <p className="rounded-lg border border-slate-200 bg-slate-50 px-2 py-1 text-[11px] leading-4 text-slate-500">
                相邻停顿标签会被合并成一个，避免连用
              </p>
            )}
            {atTextEdge && (
              <p className="rounded-lg border border-slate-200 bg-slate-50 px-2 py-1 text-[11px] leading-4 text-slate-500">
                停顿标签只能放在句中两段文字之间
              </p>
            )}
          </div>
        ) : (
          /* Patch v4.40：句中音效 —— 24 个标准音效的网格卡片 */
          <div className="space-y-3">
            {sfxGroups.length === 0 && (
              <p className="rounded-lg border border-slate-200 bg-slate-50 px-2 py-1 text-[11px] leading-4 text-slate-500">
                音效曲库加载中或为空，稍后再打开本面板即可看到 24 个标准音效
              </p>
            )}
            {sfxGroups.map((group) => (
              <div key={group.category} className="space-y-1.5">
                <p className="text-[11px] font-medium text-slate-500">{group.category}</p>
                <div className="grid grid-cols-2 gap-1.5">
                  {group.items.map((item) => (
                    <div
                      key={item.key}
                      className="flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2 py-1 transition-colors hover:border-blue-300 hover:bg-blue-50"
                    >
                      <button
                        type="button"
                        title={`在光标处插入 [sfx:${item.key}]`}
                        onClick={() => onPick(`[sfx:${item.key}]`)}
                        className="flex min-w-0 flex-1 flex-col items-start gap-0.5 text-left transition-colors cursor-pointer"
                      >
                        <span className="truncate text-xs font-medium text-slate-800">{item.label}</span>
                        <span className="font-mono text-[10px] text-slate-400">[{`sfx:${item.key}`}]</span>
                      </button>
                      {onPreviewSfx && item.available && (
                        <button
                          type="button"
                          title={`试听 ${item.label}`}
                          aria-label={`试听 ${item.label}`}
                          onClick={() => onPreviewSfx(item.key)}
                          className="shrink-0 rounded-md p-1 text-slate-400 transition-colors hover:bg-white hover:text-blue-600 cursor-pointer"
                        >
                          <Volume2 className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 底部固定提示 */}
      <div className="border-t border-slate-100 bg-slate-50 px-3 py-2">
        <p className="text-[11px] leading-4 text-slate-500">
          {tab === 'sfx' ? SFX_FOOTER_HINT : PAUSE_FOOTER_HINT}
        </p>
        {tab === 'sfx' ? (
          <p className="mt-0.5 text-[11px] leading-4 text-slate-400">{SFX_GAIN_HINT}</p>
        ) : null}
      </div>
    </div>
  );
};

export default TtsTagPicker;
