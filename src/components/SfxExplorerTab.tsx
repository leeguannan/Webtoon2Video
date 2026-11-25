import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeft,
  CircleAlert,
  Loader2,
  RotateCcw,
  Save,
  Search,
  Volume2,
} from 'lucide-react';
import type { ScriptBeat, SfxGroupCategory, SfxLibraryItem } from '../types/studio';

/**
 * Patch v4.42：音效资源管理器（Studio 的独立标签页）。
 *
 * 解决的是 v4.40 之后冒出来的一个使用断层：句中音效标签 `[sfx:key]` 只能在
 * 「送音编辑浮层」里逐句点出来，而曲库本身散落在浮层里的一堆卡片上，
 * 用户既看不清「这个音效到底是什么、多长、哪来的」，也没法一眼比较同类音效。
 *
 * 这个标签页把三件事放到一起：
 * 1. **插入目标与光标预览条**——先选定哪一句、插在哪个字符位置；
 * 2. **分类筛选与关键词检索**——按一级大类缩小范围，再按关键字定位；
 * 3. **CC0 预处理音效资源表**——中文名 / 标签名 / 素材文件 / 分类 / 时长 /
 *    CC0 溯源，一行一条，点「应用」直接把 `[sfx:key]` 插到光标处。
 *
 * 设计上刻意**不碰字幕原文**：`[sfx:key]` 只写进 `tts_text`（送音文本），
 * 它在 Stage 5 决定「这一声落在哪」，但绝不会印到画面字幕上。
 */
export interface SfxExplorerTabProps {
  /** 当前话次的解说句（草稿态）。 */
  beats: ScriptBeat[];
  /** 当前目标句 id（解说台里最后激活光标的那一句）；为空时退回第一句。 */
  activeBeatId: string | null;
  /** 切换目标句。 */
  onChangeActiveBeatId: (beatId: string) => void;
  /** 目标句当前的光标位置（字符下标）。 */
  caret: number;
  /** 用户在资源管理器里挪动了光标。 */
  onChangeCaret: (caret: number) => void;
  /** 音效曲库（24 个标准 key）。 */
  sfxLibrary: SfxLibraryItem[];
  /** 试听某个音效。 */
  onPreviewSfx: (sfxKey: string) => void;
  /** 把 `[sfx:key]` 应用到目标句的光标处。 */
  onApplySfx: (sfxKey: string, targetBeatId: string) => void;
  /** 回到解说台并定位到该句。 */
  onBackToScript: (beatId: string) => void;
  /** 保存脚本并从 Stage 5 重跑配音。 */
  onSaveAndRerunTts: () => void;
  /** 用户在资源管理器里直接改了目标句的送音文本。 */
  onChangeTargetText?: (beatId: string, text: string) => void;
  /** 正在保存 / 重跑（禁用按钮用）。 */
  busy?: boolean;
}

/** 一级大类分段按钮的顺序（全部 + 四大类）。 */
const GROUP_ORDER: Array<'全部' | SfxGroupCategory> = ['全部', '动作', '环境', '转场', '道具'];

/** 锚点预览里最多展示的字符数（再多就没法一眼看了）。 */
const ANCHOR_PREVIEW_CHARS = 18;

/** 把毫秒格式化成「1.24s」这种一眼能读的长度。 */
function formatDuration(durationMs: number | null | undefined): string {
  if (durationMs == null || !Number.isFinite(durationMs) || durationMs <= 0) return '—';
  return `${(durationMs / 1000).toFixed(2)}s`;
}

/**
 * 音效是否走了兜底回源。
 *
 * 盘面没有 `<key>.<ext>` 时会按 catalog 的 `fallback_key` 借别的素材
 * （例如 `slap` 借 `door_slam`）——这时文件名不以 key 开头。
 */
function usesFallback(item: SfxLibraryItem): boolean {
  return (
    item.resolved_filename !== '' && !item.resolved_filename.startsWith(item.key)
  );
}

const SfxExplorerTab: React.FC<SfxExplorerTabProps> = ({
  beats,
  activeBeatId,
  onChangeActiveBeatId,
  caret,
  onChangeCaret,
  sfxLibrary,
  onPreviewSfx,
  onApplySfx,
  onBackToScript,
  onSaveAndRerunTts,
  onChangeTargetText,
  busy = false,
}) => {
  const [groupFilter, setGroupFilter] = useState<'全部' | SfxGroupCategory>('全部');
  const [query, setQuery] = useState('');
  /** 目标句在资源管理器里的本地文本副本（用户可直接编辑）。 */
  const [draftText, setDraftText] = useState('');
  /** 本地文本副本对应的是哪一句（切换目标句时重新铺底）。 */
  const [draftBeatId, setDraftBeatId] = useState<string | null>(null);
  /** 上一次从外部同步进来的 tts_text，用来判断「外部是不是又改了」。 */
  const lastSyncedRef = useRef<string>('');

  /** 目标句：优先用指定的 activeBeatId，否则退回第一句。 */
  const targetBeat = useMemo(() => {
    if (beats.length === 0) return null;
    return beats.find((beat) => beat.beat_id === activeBeatId) ?? beats[0];
  }, [beats, activeBeatId]);

  const targetBeatId = targetBeat?.beat_id ?? '';

  /**
   * 铺底文本：送音文本为空时用字幕原文兜底。
   *
   * 为什么要兜底：`tts_text` 留空时后端会「继承字幕 + 词典替换」，
   * 如果这时候直接往空串里插标签，整句正文就没了 —— 所以先铺一份字幕原文
   * 当底本，再把标签插进去（字幕原文本身不会被改动，字幕永远读 narration_text）。
   */
  const baseText = useMemo(() => {
    if (!targetBeat) return '';
    const tts = (targetBeat.tts_text ?? '').trim();
    return tts !== '' ? (targetBeat.tts_text ?? '') : (targetBeat.narration_text ?? '');
  }, [targetBeat]);

  const isTtsEmpty = (targetBeat?.tts_text ?? '').trim() === '';

  // 目标句变了 → 重新铺底；同一句但外部（解说台/后端）改了 → 以外部为准。
  useEffect(() => {
    if (!targetBeat) {
      setDraftBeatId(null);
      setDraftText('');
      return;
    }
    const external = targetBeat.tts_text ?? '';
    if (draftBeatId !== targetBeat.beat_id) {
      setDraftBeatId(targetBeat.beat_id);
      setDraftText(baseText);
      lastSyncedRef.current = external;
      return;
    }
    if (external !== lastSyncedRef.current) {
      setDraftText(baseText);
      lastSyncedRef.current = external;
    }
  }, [targetBeat, baseText, draftBeatId]);

  /** 光标位置（受父组件控制，保证解说台与资源管理器指向同一个位置）。 */
  const safeCaret = Math.max(0, Math.min(caret, draftText.length));

  const handleDraftChange = useCallback(
    (next: string) => {
      setDraftText(next);
      if (targetBeatId && onChangeTargetText) {
        onChangeTargetText(targetBeatId, next);
      }
      // 编辑会把光标推到末尾，父组件的 caret 也要跟上。
      onChangeCaret(next.length);
    },
    [onChangeCaret, onChangeTargetText, targetBeatId]
  );

  const handleCaretMove = useCallback(
    (event: React.SyntheticEvent<HTMLTextAreaElement>) => {
      onChangeCaret(event.currentTarget.selectionStart ?? 0);
    },
    [onChangeCaret]
  );

  /** 锚点预览：光标前后各截一小段，肉眼能看出「要插在哪儿」。 */
  const anchorPreview = useMemo(() => {
    const head = draftText.slice(Math.max(0, safeCaret - ANCHOR_PREVIEW_CHARS), safeCaret);
    const tail = draftText.slice(safeCaret, safeCaret + ANCHOR_PREVIEW_CHARS);
    return { head, tail };
  }, [draftText, safeCaret]);

  /** 各大类的条数（分段按钮上的角标）。 */
  const groupCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const item of sfxLibrary) {
      const group = item.group ?? '';
      counts.set(group, (counts.get(group) ?? 0) + 1);
    }
    return counts;
  }, [sfxLibrary]);

  /** 按大类 + 关键词过滤。 */
  const filteredItems = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return sfxLibrary.filter((item) => {
      if (groupFilter !== '全部' && (item.group ?? '') !== groupFilter) return false;
      if (keyword === '') return true;
      const haystack = [
        item.key,
        item.label,
        item.category,
        item.resolved_filename,
        item.cc0_source?.name ?? '',
        item.cc0_source?.author ?? '',
      ]
        .join(' ')
        .toLowerCase();
      return haystack.includes(keyword);
    });
  }, [groupFilter, query, sfxLibrary]);

  const resetFilters = useCallback(() => {
    setGroupFilter('全部');
    setQuery('');
  }, []);

  const handleApply = useCallback(
    (sfxKey: string) => {
      if (!targetBeatId) return;
      onApplySfx(sfxKey, targetBeatId);
    },
    [onApplySfx, targetBeatId]
  );

  return (
    <div className="px-5 py-4 space-y-4">
      {/* ---------- ① 插入目标与光标预览条 ---------- */}
      <section className="rounded-xl border border-slate-200 bg-white p-4">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2">
            <label
              htmlFor="sfx-explorer-target"
              className="text-xs font-medium text-slate-600"
            >
              插入目标
            </label>
            <select
              id="sfx-explorer-target"
              value={targetBeatId}
              onChange={(event) => onChangeActiveBeatId(event.target.value)}
              aria-label="音效插入目标句"
              className="min-w-[180px] px-2 py-1.5 text-xs bg-white border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-600/20"
            >
              {beats.map((beat) => (
                <option key={beat.beat_id} value={beat.beat_id}>
                  {beat.beat_id} · {(beat.narration_text ?? '').slice(0, 18)}
                </option>
              ))}
            </select>
          </div>
          <span className="font-mono-tabular text-xs text-slate-500">
            {targetBeatId || '—'} · 第 {safeCaret} 字符处
          </span>
          <div className="ml-auto flex items-center gap-2">
            <button
              type="button"
              onClick={() => targetBeatId && onBackToScript(targetBeatId)}
              disabled={!targetBeatId}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 text-[11px] rounded-lg border border-slate-300 bg-white text-slate-600 hover:border-blue-400 hover:text-blue-700 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <ArrowLeft className="w-3.5 h-3.5" />
              返回解说台查看
            </button>
            <button
              type="button"
              onClick={onSaveAndRerunTts}
              disabled={busy || !targetBeatId}
              className="inline-flex items-center gap-1 px-2.5 py-1.5 text-[11px] rounded-lg bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {busy ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <Save className="w-3.5 h-3.5" />
              )}
              保存脚本并从 Stage 5 重跑配音
            </button>
          </div>
        </div>

        {isTtsEmpty && (
          <p className="mt-2.5 flex items-start gap-1.5 text-[11px] leading-4 text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-2.5 py-1.5">
            <CircleAlert className="w-3.5 h-3.5 mt-px shrink-0" />
            <span>
              当前 `tts_text` 留空 · 应用时将基于字幕原文写入 `[sfx:key]`
              （字幕原文本身不会被改动，画面字幕仍读 narration_text）。
            </span>
          </p>
        )}

        <div className="mt-3">
          <label
            htmlFor="sfx-explorer-text"
            className="block text-[11px] font-medium text-slate-500 mb-1"
          >
            送音文本（可编辑，点击即可调整光标位置）
          </label>
          <textarea
            id="sfx-explorer-text"
            rows={3}
            value={draftText}
            onChange={(event) => handleDraftChange(event.target.value)}
            onSelect={handleCaretMove}
            onClick={handleCaretMove}
            onKeyUp={handleCaretMove}
            placeholder="这一句还没有送音文本..."
            className="w-full p-2 text-xs leading-5 text-slate-800 bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600 resize-none"
          />
          <div className="mt-1.5 flex items-center gap-2 text-[11px] text-slate-500">
            <span className="truncate">
              …{anchorPreview.head}
              <span className="px-0.5 text-blue-600 font-medium">▏光标</span>
              {anchorPreview.tail}…
            </span>
            <span className="ml-auto shrink-0 text-slate-400">
              插入位置＝第 {safeCaret} 字符
            </span>
          </div>
        </div>
      </section>

      {/* ---------- ② 分类筛选与关键词检索 ---------- */}
      <section className="rounded-xl border border-slate-200 bg-white p-4">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-1 p-1 bg-slate-100 rounded-lg" role="group" aria-label="音效一级大类筛选">
            {GROUP_ORDER.map((group) => {
              const count =
                group === '全部'
                  ? sfxLibrary.length
                  : groupCounts.get(group) ?? 0;
              return (
                <button
                  key={group}
                  type="button"
                  onClick={() => setGroupFilter(group)}
                  aria-pressed={groupFilter === group}
                  className={`px-2.5 py-1 text-[11px] font-medium rounded-md transition-colors cursor-pointer ${
                    groupFilter === group
                      ? 'bg-white text-slate-900 shadow-xs'
                      : 'text-slate-600 hover:text-slate-900'
                  }`}
                >
                  {group} ({count})
                </button>
              );
            })}
          </div>
          <div className="relative flex-1 min-w-[200px]">
            <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400 pointer-events-none" />
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索音效键 / 中文名 / 分类 / 文件名 / CC0 作者"
              aria-label="搜索音效"
              className="w-full pl-8 pr-2 py-1.5 text-xs bg-white border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-600/20"
            />
          </div>
          <span className="text-[11px] text-slate-500">
            命中 {filteredItems.length} / {sfxLibrary.length} 条
          </span>
        </div>
      </section>

      {/* ---------- ③ CC0 预处理音效资源表 ---------- */}
      <section className="rounded-xl border border-slate-200 bg-white overflow-hidden">
        {filteredItems.length === 0 ? (
          <div className="px-5 py-10 text-center space-y-3">
            <p className="text-sm text-slate-600">没有匹配的音效。</p>
            <button
              type="button"
              onClick={resetFilters}
              className="inline-flex items-center gap-1 px-3 py-1.5 text-xs rounded-lg border border-slate-300 bg-white text-slate-600 hover:border-blue-400 hover:text-blue-700"
            >
              <RotateCcw className="w-3.5 h-3.5" />
              重置筛选
            </button>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="border-b border-slate-200 bg-slate-50 text-[11px] text-slate-500">
                  <th className="px-3 py-2 font-medium">音效</th>
                  <th className="px-3 py-2 font-medium">标签</th>
                  <th className="px-3 py-2 font-medium">分类</th>
                  <th className="px-3 py-2 font-medium">时长</th>
                  <th className="px-3 py-2 font-medium">CC0 来源</th>
                  <th className="px-3 py-2 font-medium text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {filteredItems.map((item) => {
                  const fallback = usesFallback(item);
                  const cc0 = item.cc0_source;
                  return (
                    <tr
                      key={item.key}
                      className="border-b border-slate-100 last:border-b-0 hover:bg-slate-50/60"
                    >
                      <td className="px-3 py-2 align-top">
                        <div className="text-xs font-medium text-slate-800">
                          {item.label}
                        </div>
                        <div className="mt-0.5 text-[10px] text-slate-400 font-mono">
                          {item.resolved_filename || '（盘面无素材）'}
                        </div>
                        {fallback && (
                          <div className="mt-0.5 text-[10px] text-amber-600">
                            兜底 → {item.fallback_key}
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-2 align-top">
                        <code className="text-[11px] text-blue-700 bg-blue-50 border border-blue-100 rounded px-1.5 py-0.5">
                          [sfx:{item.key}]
                        </code>
                      </td>
                      <td className="px-3 py-2 align-top text-[11px] text-slate-600">
                        <span className="text-slate-800">{item.group || '未归类'}</span>
                        <span className="mx-1 text-slate-300">·</span>
                        {item.category}
                      </td>
                      <td className="px-3 py-2 align-top text-[11px] font-mono-tabular text-slate-600">
                        {formatDuration(item.duration_ms)}
                      </td>
                      <td className="px-3 py-2 align-top text-[11px] text-slate-600">
                        {item.source_type === 'freesound_cc0' && cc0 ? (
                          <div className="space-y-0.5">
                            <div>
                              Freesound #{cc0.sound_id || '—'} · {cc0.author || '未知作者'}
                            </div>
                            {cc0.name && (
                              <div className="text-[10px] text-slate-400 truncate max-w-[220px]">
                                {cc0.name}
                              </div>
                            )}
                            {cc0.freesound_url && (
                              <a
                                href={cc0.freesound_url}
                                target="_blank"
                                rel="noreferrer"
                                className="text-[10px] text-blue-600 hover:underline"
                              >
                                查看原始素材页
                              </a>
                            )}
                          </div>
                        ) : (
                          <span className="text-slate-500">仓库内置 CC0 资产</span>
                        )}
                      </td>
                      <td className="px-3 py-2 align-top">
                        <div className="flex items-center justify-end gap-1.5">
                          <button
                            type="button"
                            onClick={() => onPreviewSfx(item.key)}
                            disabled={!item.available}
                            title={
                              item.available
                                ? `试听 ${item.label}`
                                : '盘面没有可用素材，无法试听'
                            }
                            aria-label={`试听 ${item.label}`}
                            className="rounded-md p-1 text-slate-400 hover:bg-white hover:text-blue-600 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                          >
                            <Volume2 className="w-4 h-4" />
                          </button>
                          <button
                            type="button"
                            onClick={() => handleApply(item.key)}
                            disabled={!targetBeatId}
                            title="把 [sfx:key] 插入目标句的光标处"
                            className="px-2 py-1 text-[11px] rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed"
                          >
                            应用
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <p className="text-[11px] leading-4 text-slate-400">
        `[sfx:key]` 只写进送音文本（tts_text），由 Stage 5 决定这一声落在哪，
        <span className="text-slate-500">不会印入画面字幕</span>；
        改完请点「保存脚本并从 Stage 5 重跑配音」才生效。
      </p>
    </div>
  );
};

export default SfxExplorerTab;
