import React, { useState } from 'react';
import {
  BookMarked,
  CheckCircle2,
  GitBranch,
  History,
  Plus,
  Users,
} from 'lucide-react';
import type {
  CharacterProfile,
  PlotThread,
  SeriesItem,
} from '../types/studio';

interface MemoryViewProps {
  series: SeriesItem;
  onAddCharacter: (seriesId: string, char: CharacterProfile) => void;
  onAddPlotThread: (seriesId: string, thread: Omit<PlotThread, 'thread_id'>) => void;
  onToggleThreadStatus: (seriesId: string, threadId: string) => void;
  onUpdateStyle: (seriesId: string, style: string) => void;
}

export const MemoryView: React.FC<MemoryViewProps> = ({
  series,
  onAddCharacter,
  onAddPlotThread,
  onToggleThreadStatus,
  onUpdateStyle,
}) => {
  const { story_bible } = series;

  const [styleDraft, setStyleDraft] = useState(story_bible.narration_style);
  const [charName, setCharName] = useState('');
  const [charAliases, setCharAliases] = useState('');
  const [charTraits, setCharTraits] = useState('');
  const [charStatus, setCharStatus] = useState('');

  const [threadTitle, setThreadTitle] = useState('');
  const [threadDesc, setThreadDesc] = useState('');
  const [threadEp, setThreadEp] = useState(1);

  const handleAddChar = (e: React.FormEvent) => {
    e.preventDefault();
    if (!charName.trim()) return;
    onAddCharacter(series.series_id, {
      name: charName.trim(),
      aliases: charAliases
        .split(/[,，、]/)
        .map((s) => s.trim())
        .filter(Boolean),
      visual_traits: charTraits.trim() || '待百炼视觉抽取补充',
      current_status: charStatus.trim() || '初次登场',
      first_seen_ep: threadEp,
      last_seen_ep: threadEp,
    });
    setCharName('');
    setCharAliases('');
    setCharTraits('');
    setCharStatus('');
  };

  const handleAddThread = (e: React.FormEvent) => {
    e.preventDefault();
    if (!threadTitle.trim()) return;
    onAddPlotThread(series.series_id, {
      title: threadTitle.trim(),
      description: threadDesc.trim() || threadTitle.trim(),
      introduced_ep: threadEp,
      status: 'open',
    });
    setThreadTitle('');
    setThreadDesc('');
  };

  const openThreads = story_bible.plot_threads.filter((t) => t.status === 'open');
  const resolvedThreads = story_bible.plot_threads.filter((t) => t.status === 'resolved');

  return (
    <div className="space-y-6">
      {/* Architecture Overview & Snapshot Time-Travel Banner */}
      <section className="bg-white border border-slate-200 rounded-xl p-5 flex flex-col lg:flex-row lg:items-center justify-between gap-4">
        <div className="space-y-1">
          <div className="flex items-center gap-2 text-sm font-semibold text-slate-900">
            <History className="w-4 h-4 text-blue-600" />
            四层增强跨话记忆库（StoryBible v2 · 隔离工作区 {series.series_id}）
          </div>
          <p className="text-xs text-slate-600 leading-relaxed max-w-3xl">
            针对 50~200 话长篇连载，记忆库按「人物实体图谱 + 分层摘要（近 3 话详摘 + 篇章归档） + 未回收伏笔追踪 + 按话只读快照」组织。重跑第 N 话时自动加载第 N-1 话快照，彻底杜绝跨话剧透污染。
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-4 shrink-0 text-xs">
          <div>
            <div className="text-slate-500">已归档话次快照</div>
            <div className="font-mono-tabular font-semibold text-slate-900 mt-0.5">
              {story_bible.snapshots_ep_nos.map((n) => `ep_${String(n).padStart(3, '0')}`).join(', ') || '暂无'}
            </div>
          </div>
          <div className="pl-4 border-l border-slate-200">
            <div className="text-slate-500">活跃未回收伏笔</div>
            <div className="font-mono-tabular font-semibold text-amber-700 mt-0.5">
              {openThreads.length} 条 open
            </div>
          </div>
        </div>
      </section>

      {/* Series Narration Persona */}
      <section className="bg-white border border-slate-200 rounded-xl p-5">
        <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-3">
          <div className="flex-1">
            <label className="block text-xs font-semibold text-slate-800 mb-1.5">
              作品专属解说人设与叙事基调（仅作用于《{series.series_title}》）
            </label>
            <input
              type="text"
              value={styleDraft}
              onChange={(e) => setStyleDraft(e.target.value)}
              className="w-full px-3.5 py-2 text-xs bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
            />
          </div>
          <button
            type="button"
            onClick={() => onUpdateStyle(series.series_id, styleDraft)}
            className="px-4 py-2 text-xs font-medium text-white bg-slate-900 rounded-lg hover:bg-slate-800 whitespace-nowrap shrink-0 cursor-pointer"
          >
            保存本作品叙事风格
          </button>
        </div>
      </section>

      {/* Layer 1 & Layer 3 Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-6 items-start">
        {/* Layer 1: Character Entity Graph */}
        <section className="lg:col-span-7 bg-white border border-slate-200 rounded-xl p-5 space-y-4">
          <div className="flex items-center justify-between border-b border-slate-100 pb-3">
            <div className="flex items-center gap-2">
              <Users className="w-4 h-4 text-slate-600" />
              <h3 className="text-sm font-semibold text-slate-900">
                Layer 1 · 人物实体图谱（同时注入 Stage 3 识图防脸盲）
              </h3>
            </div>
            <span className="text-xs font-mono-tabular text-slate-400">
              {story_bible.characters.length} 名角色
            </span>
          </div>

          <div className="space-y-3">
            {story_bible.characters.map((ch) => (
              <div
                key={ch.name}
                className="p-3.5 border border-slate-200 rounded-lg bg-slate-50/50 space-y-1.5"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-semibold text-slate-900">{ch.name}</span>
                    {ch.aliases.length > 0 && (
                      <span className="text-xs text-slate-500">
                        别名：{ch.aliases.join(' / ')}
                      </span>
                    )}
                  </div>
                  <span className="text-[11px] font-mono-tabular text-slate-500">
                    EP.{ch.first_seen_ep} – EP.{ch.last_seen_ep}
                  </span>
                </div>
                <p className="text-xs text-slate-600">
                  <strong className="font-medium text-slate-700">外貌特征：</strong>
                  {ch.visual_traits}
                </p>
                <p className="text-xs text-slate-700">
                  <strong className="font-medium text-slate-800">最新状态：</strong>
                  {ch.current_status}
                </p>
              </div>
            ))}
          </div>

          <form onSubmit={handleAddChar} className="pt-3 border-t border-slate-100 grid grid-cols-1 sm:grid-cols-12 gap-2">
            <input
              type="text"
              value={charName}
              onChange={(e) => setCharName(e.target.value)}
              placeholder="角色主名"
              className="sm:col-span-3 px-2.5 py-1.5 text-xs bg-slate-50 border border-slate-200 rounded-lg"
            />
            <input
              type="text"
              value={charAliases}
              onChange={(e) => setCharAliases(e.target.value)}
              placeholder="别名(逗号分隔)"
              className="sm:col-span-3 px-2.5 py-1.5 text-xs bg-slate-50 border border-slate-200 rounded-lg"
            />
            <input
              type="text"
              value={charTraits}
              onChange={(e) => setCharTraits(e.target.value)}
              placeholder="外貌特征（注入识图）"
              className="sm:col-span-4 px-2.5 py-1.5 text-xs bg-slate-50 border border-slate-200 rounded-lg"
            />
            <button
              type="submit"
              className="sm:col-span-2 inline-flex items-center justify-center gap-1 px-3 py-1.5 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 whitespace-nowrap cursor-pointer"
            >
              <Plus className="w-3.5 h-3.5" />
              收录
            </button>
            <input
              type="text"
              value={charStatus}
              onChange={(e) => setCharStatus(e.target.value)}
              placeholder="当前状态（如：第3话加入主角小队）"
              className="sm:col-span-12 px-2.5 py-1.5 text-xs bg-slate-50 border border-slate-200 rounded-lg"
            />
          </form>
        </section>

        {/* Layer 3: PlotThread Foreshadowing Tracker */}
        <section className="lg:col-span-5 bg-white border border-slate-200 rounded-xl p-5 space-y-4">
          <div className="flex items-center justify-between border-b border-slate-100 pb-3">
            <div className="flex items-center gap-2">
              <GitBranch className="w-4 h-4 text-slate-600" />
              <h3 className="text-sm font-semibold text-slate-900">
                Layer 3 · 跨话伏笔追踪器
              </h3>
            </div>
            <span className="text-xs font-mono-tabular text-slate-400">
              {openThreads.length} 未回收 · {resolvedThreads.length} 已回收
            </span>
          </div>

          <div className="space-y-2.5">
            {story_bible.plot_threads.map((t) => (
              <div
                key={t.thread_id}
                className={`p-3 rounded-lg border text-xs space-y-1.5 ${
                  t.status === 'open'
                    ? 'border-amber-200 bg-amber-50/40'
                    : 'border-slate-200 bg-slate-50 opacity-65'
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <span className="font-semibold text-slate-900">{t.title}</span>
                  <button
                    type="button"
                    onClick={() => onToggleThreadStatus(series.series_id, t.thread_id)}
                    className="inline-flex items-center gap-1 text-[11px] font-medium text-blue-600 hover:underline whitespace-nowrap shrink-0 cursor-pointer"
                  >
                    <CheckCircle2 className="w-3 h-3" />
                    {t.status === 'open' ? '标为已回收' : '重新激活'}
                  </button>
                </div>
                <p className="text-slate-600 leading-relaxed">{t.description}</p>
                <div className="text-[11px] font-mono-tabular text-slate-400">
                  埋下于 EP.{t.introduced_ep} · 状态：{t.status}
                  {t.resolved_ep ? ` (EP.${t.resolved_ep} 回收)` : ''}
                </div>
              </div>
            ))}
          </div>

          <form onSubmit={handleAddThread} className="pt-3 border-t border-slate-100 space-y-2">
            <div className="flex gap-2">
              <input
                type="text"
                value={threadTitle}
                onChange={(e) => setThreadTitle(e.target.value)}
                placeholder="新增伏笔标题（如：黑钥匙用途）"
                className="flex-1 px-2.5 py-1.5 text-xs bg-slate-50 border border-slate-200 rounded-lg"
              />
              <input
                type="number"
                min={1}
                value={threadEp}
                onChange={(e) => setThreadEp(Number(e.target.value) || 1)}
                className="w-16 px-2 py-1.5 text-xs font-mono-tabular bg-slate-50 border border-slate-200 rounded-lg"
                aria-label="伏笔出现话次"
              />
            </div>
            <div className="flex gap-2">
              <input
                type="text"
                value={threadDesc}
                onChange={(e) => setThreadDesc(e.target.value)}
                placeholder="伏笔上下文描述（Stage 4 会自动在相关话次呼应）"
                className="flex-1 px-2.5 py-1.5 text-xs bg-slate-50 border border-slate-200 rounded-lg"
              />
              <button
                type="submit"
                className="px-3 py-1.5 text-xs font-medium text-white bg-slate-900 rounded-lg hover:bg-slate-800 whitespace-nowrap cursor-pointer"
              >
                添加伏笔
              </button>
            </div>
          </form>
        </section>
      </div>

      {/* Layer 2: Arc Summaries & Recent Sliding Window Digests */}
      <section className="bg-white border border-slate-200 rounded-xl p-5 space-y-4">
        <div className="flex items-center gap-2 border-b border-slate-100 pb-3">
          <BookMarked className="w-4 h-4 text-slate-600" />
          <h3 className="text-sm font-semibold text-slate-900">
            Layer 2 · 分层剧情记忆（长期篇章归档 + 近 3 话滑动窗口详摘）
          </h3>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div className="space-y-2.5">
            <div className="text-xs font-medium text-slate-500">
              篇章级长期归档（Arc Summaries · 不被后续话次覆盖）
            </div>
            {story_bible.arc_summaries.length > 0 ? (
              story_bible.arc_summaries.map((arc, i) => (
                <div key={i} className="p-3 border border-slate-200 rounded-lg bg-slate-50 text-xs space-y-1">
                  <div className="flex items-center justify-between font-semibold text-slate-900">
                    <span>{arc.arc_name}</span>
                    <span className="font-mono-tabular text-slate-500">
                      EP.{arc.ep_range[0]}–{arc.ep_range[1]}
                    </span>
                  </div>
                  <p className="text-slate-600 leading-relaxed">{arc.summary}</p>
                </div>
              ))
            ) : (
              <div className="p-4 border border-dashed border-slate-200 rounded-lg text-xs text-slate-400">
                连载满一个篇章（或每 10 话）后由 qwen-plus 自动触发压缩归档
              </div>
            )}
          </div>

          <div className="space-y-2.5">
            <div className="text-xs font-medium text-slate-500">
              近话高保真摘要与关键词索引（Recent Digests · 按需召回）
            </div>
            {story_bible.recent_digests.map((d) => (
              <div key={d.episode_no} className="p-3 border border-slate-200 rounded-lg text-xs space-y-1">
                <div className="flex items-center justify-between">
                  <span className="font-mono-tabular font-semibold text-slate-900">
                    EP.{String(d.episode_no).padStart(3, '0')} · {d.one_line_hook}
                  </span>
                </div>
                <p className="text-slate-600">{d.summary}</p>
                <div className="text-[11px] text-slate-400 font-mono-tabular">
                  出场：{d.characters_present.join(' / ')} · 关键词：{d.keywords.join(', ')}
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>
    </div>
  );
};
