import React, { useState } from 'react';
import {
  Archive,
  CheckCircle2,
  Clock,
  Download,
  FolderSearch,
  Layers,
  Loader2,
  Lock,
  Play,
  ArrowRight,
  AlertCircle,
} from 'lucide-react';
import type {
  EpisodeItem,
  EpisodeRenderOptions,
  OutroMode,
  RunMode,
  SeriesItem,
} from '../types/studio';
import { formatMs, parseEpisodeRangeExpression } from '../utils/pronunciation';
import { formatBytes } from '../utils/format';
import { SeriesBrandingPanel } from './SeriesBrandingPanel';
import { SeriesTtsPanel } from './SeriesTtsPanel';

interface CatalogViewProps {
  series: SeriesItem;
  runMode: RunMode;
  onChangeRunMode: (mode: RunMode) => void;
  onParseCatalogUrl: (url: string) => void;
  onToggleEpisode: (episodeId: string) => void;
  onApplyRangeSelection: (epNos: number[]) => void;
  onStartBatch: () => void;
  onOpenEpisode: (episodeId: string) => void;
  onExportEpisode: (episodeId: string, format: 'mp4' | 'ass' | 'script') => void;
  onExportSeriesZip: (seriesId: string) => void;
  onUpdateEpisodeRenderOptions: (episodeId: string, options: EpisodeRenderOptions) => void;
  /** Patch v4.11：作品级批量清理已出片话次的过程文件，返回是否实际清理了话次。 */
  onCleanupSeriesIntermediates?: (seriesId: string) => Promise<boolean>;
}

const OUTRO_MODE_OPTIONS: Array<{ value: OutroMode; label: string }> = [
  { value: 'inherit', label: '结尾：跟随默认' },
  { value: 'continue', label: '结尾：待续' },
  { value: 'final', label: '结尾：完结' },
  { value: 'none', label: '结尾：不加' },
];

type StatusFilter = 'all' | 'done' | 'pending' | 'locked';

export const CatalogView: React.FC<CatalogViewProps> = ({
  series,
  runMode,
  onChangeRunMode,
  onParseCatalogUrl,
  onToggleEpisode,
  onApplyRangeSelection,
  onStartBatch,
  onOpenEpisode,
  onExportEpisode,
  onExportSeriesZip,
  onUpdateEpisodeRenderOptions,
  onCleanupSeriesIntermediates,
}) => {
  const [urlInput, setUrlInput] = useState(series.list_url);
  const [rangeInput, setRangeInput] = useState('1-3');
  const [filter, setFilter] = useState<StatusFilter>('all');
  /** Patch v4.11：批量清理的内联二次确认条与忙碌态。 */
  const [cleanupConfirm, setCleanupConfirm] = useState(false);
  const [cleanupBusy, setCleanupBusy] = useState(false);

  const filteredEpisodes = series.episodes.filter((ep) => {
    if (filter === 'done') return ep.status === 'done' && !ep.is_locked;
    if (filter === 'pending') return ep.status !== 'done' && !ep.is_locked;
    if (filter === 'locked') return ep.is_locked;
    return true;
  });

  const selectedCount = series.episodes.filter((e) => e.selected && !e.is_locked).length;
  const doneCount = series.episodes.filter((e) => e.status === 'done').length;
  const renderedCount = series.episodes.filter(
    (e) => e.stages.find((s) => s.id === 's7_renderer')?.status === 'done'
  ).length;

  // Patch v4.11：可一键清理的话次 = 已出片、未归档、非运行中；汇总可回收空间。
  const cleanableEpisodes = series.episodes.filter(
    (e) =>
      e.stages.find((s) => s.id === 's7_renderer')?.status === 'done' &&
      !e.archived &&
      e.status !== 'running' &&
      e.status !== 'queued'
  );
  const reclaimableTotal = cleanableEpisodes.reduce(
    (sum, ep) => sum + (ep.reclaimable_bytes ?? 0),
    0
  );

  const handleConfirmSeriesCleanup = async () => {
    if (!onCleanupSeriesIntermediates) return;
    setCleanupBusy(true);
    try {
      const ok = await onCleanupSeriesIntermediates(series.series_id);
      if (ok) setCleanupConfirm(false);
    } finally {
      setCleanupBusy(false);
    }
  };

  const handleApplyRange = (e: React.FormEvent) => {
    e.preventDefault();
    const maxEp = Math.max(...series.episodes.map((ep) => ep.episode_no), 1);
    const nos = parseEpisodeRangeExpression(rangeInput, maxEp);
    onApplyRangeSelection(nos);
  };

  const renderStatusText = (ep: EpisodeItem) => {
    if (ep.is_locked) {
      return (
        <span className="inline-flex items-center gap-1.5 text-xs text-slate-400">
          <Lock className="w-3.5 h-3.5" />
          已锁定跳过
        </span>
      );
    }
    if (ep.status === 'running') {
      return (
        <span className="inline-flex items-center gap-1.5 text-xs font-medium text-blue-600">
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
          运行中
        </span>
      );
    }
    if (ep.status === 'queued') {
      return (
        <span className="inline-flex items-center gap-1.5 text-xs text-amber-600">
          <Clock className="w-3.5 h-3.5" />
          排队中
        </span>
      );
    }
    if (ep.status === 'failed') {
      return (
        <span className="inline-flex items-center gap-1.5 text-xs font-medium text-red-600">
          <AlertCircle className="w-3.5 h-3.5" />
          失败
        </span>
      );
    }
    if (ep.status === 'done') {
      const isPhase1Only = ep.stages.find((s) => s.id === 's7_renderer')?.status !== 'done';
      return (
        <span className="inline-flex items-center gap-1.5 text-xs font-medium text-emerald-700">
          <CheckCircle2 className="w-3.5 h-3.5" />
          {isPhase1Only ? 'Phase 1 已出稿' : '成片已完成'}
        </span>
      );
    }
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-slate-500">
        <Clock className="w-3.5 h-3.5" />
        待制作
      </span>
    );
  };

  return (
    <div className="space-y-6">
      {/* Patch v3.2 Patch 13：作品级配音设置（音色 / 语速 / 静态试听） */}
      <SeriesTtsPanel seriesId={series.series_id} />

      {/* Patch v4.5：作品级封页写字与双结尾图片包 */}
      <SeriesBrandingPanel seriesId={series.series_id} />

      {/* Stage 0 URL Capture Bar */}
      <section className="bg-white border border-slate-200 rounded-xl p-5">
        <div className="flex flex-col lg:flex-row lg:items-end justify-between gap-4">
          <div className="flex-1 min-w-0">
            <label
              htmlFor="webtoon-url"
              className="block text-sm font-semibold text-slate-900 mb-1.5"
            >
              Stage 0 作品目录索引（支持 WEBTOON 列表页或 local:// 本地目录）
            </label>
            <div className="flex flex-col sm:flex-row gap-2.5">
              <input
                id="webtoon-url"
                type="url"
                value={urlInput}
                onChange={(e) => setUrlInput(e.target.value)}
                placeholder="https://www.webtoons.com/en/action/.../list?title_no=..."
                className="flex-1 min-w-0 px-3.5 py-2 text-sm font-mono-tabular bg-slate-50 border border-slate-200 rounded-lg text-slate-900 focus:outline-none focus:ring-2 focus:ring-blue-600/20 focus:border-blue-600"
              />
              <button
                type="button"
                onClick={() => onParseCatalogUrl(urlInput)}
                className="inline-flex items-center justify-center gap-2 px-4 py-2 text-sm font-medium text-slate-900 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors whitespace-nowrap shrink-0 cursor-pointer"
              >
                <FolderSearch className="w-4 h-4 text-slate-600" />
                拉取漫画目录
              </button>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-6 pt-2 lg:pt-0 border-t lg:border-t-0 border-slate-100">
            <div>
              <div className="text-xs text-slate-500">已索引话次</div>
              <div className="text-lg font-semibold text-slate-900 font-mono-tabular">
                {series.episodes.length} 话
              </div>
            </div>
            <div>
              <div className="text-xs text-slate-500">已产出</div>
              <div className="text-lg font-semibold text-slate-900 font-mono-tabular">
                {doneCount} 话
              </div>
            </div>
            <div>
              <div className="text-xs text-slate-500">记忆体角色 / 伏笔</div>
              <div className="text-lg font-semibold text-slate-900 font-mono-tabular">
                {series.story_bible.characters.length} 名 / {series.story_bible.plot_threads.filter((t) => t.status === 'open').length} 伏笔
              </div>
            </div>
          </div>
        </div>

        {/* Rolling StoryBible Summary */}
        <div className="mt-4 pt-4 border-t border-slate-100 flex flex-col md:flex-row md:items-center justify-between gap-2 text-xs text-slate-600">
          <div className="flex items-start gap-2">
            <Layers className="w-4 h-4 text-slate-400 shrink-0 mt-0.5" />
            <span>
              <strong className="font-medium text-slate-800">最新话次记忆接续：</strong>
              {series.story_bible.recent_digests[series.story_bible.recent_digests.length - 1]?.summary ?? '暂无历史摘要'}
            </span>
          </div>
          <span className="text-slate-400 font-mono-tabular whitespace-nowrap shrink-0">
            series_id: {series.series_id}
          </span>
        </div>
      </section>

      {/* Batch Toolbar & Episode Table */}
      <section className="bg-white border border-slate-200 rounded-xl overflow-hidden">
        <div className="px-5 py-4 border-b border-slate-200 flex flex-col xl:flex-row xl:items-center justify-between gap-4">
          <div className="flex flex-wrap items-center gap-3">
            {/* Segmented Filter */}
            <div className="flex items-center gap-1 p-1 bg-slate-100 rounded-lg">
              {(
                [
                  { id: 'all', label: '全部话次' },
                  { id: 'done', label: '已处理' },
                  { id: 'pending', label: '待制作' },
                  { id: 'locked', label: '已锁定' },
                ] as const
              ).map((tab) => (
                <button
                  key={tab.id}
                  type="button"
                  onClick={() => setFilter(tab.id)}
                  className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors whitespace-nowrap shrink-0 cursor-pointer ${
                    filter === tab.id
                      ? 'bg-white text-slate-900 shadow-xs'
                      : 'text-slate-600 hover:text-slate-900'
                  }`}
                >
                  {tab.label}
                </button>
              ))}
            </div>

            {/* Range Selector */}
            <form onSubmit={handleApplyRange} className="flex items-center gap-2">
              <input
                type="text"
                value={rangeInput}
                onChange={(e) => setRangeInput(e.target.value)}
                aria-label="话次区间"
                placeholder="1-3 或 1,3,5"
                className="w-32 px-2.5 py-1.5 text-xs font-mono-tabular bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
              />
              <button
                type="submit"
                className="px-3 py-1.5 text-xs font-medium text-slate-700 bg-slate-50 border border-slate-200 rounded-lg hover:bg-slate-100 transition-colors whitespace-nowrap shrink-0 cursor-pointer"
              >
                按区间勾选
              </button>
            </form>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            {/* Mode Switch —— Patch v4.36 三档 */}
            <div className="flex items-center gap-1 p-1 bg-slate-100 rounded-lg">
              <button
                type="button"
                onClick={() => onChangeRunMode('storyboard_prep')}
                title="只跑下载→切图→视觉抽取→语义切幕，然后停下等你调镜头编排；确认后再单独跑一次 S4 写稿（S4 只跑一遍）"
                className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors whitespace-nowrap shrink-0 cursor-pointer ${
                  runMode === 'storyboard_prep'
                    ? 'bg-white text-slate-900 shadow-xs'
                    : 'text-slate-600 hover:text-slate-900'
                }`}
              >
                准备分镜+切幕 (S1–S3b)
              </button>
              <button
                type="button"
                onClick={() => onChangeRunMode('phase1_only')}
                title="跑到 S4 写稿为止，停下来给你审解说稿"
                className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors whitespace-nowrap shrink-0 cursor-pointer ${
                  runMode === 'phase1_only'
                    ? 'bg-white text-slate-900 shadow-xs'
                    : 'text-slate-600 hover:text-slate-900'
                }`}
              >
                出稿止步 (S1–S4)
              </button>
              <button
                type="button"
                onClick={() => onChangeRunMode('full_video')}
                title="一路跑到成片（S1–S7）"
                className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors whitespace-nowrap shrink-0 cursor-pointer ${
                  runMode === 'full_video'
                    ? 'bg-white text-slate-900 shadow-xs'
                    : 'text-slate-600 hover:text-slate-900'
                }`}
              >
                全流程制片 (S1–S7)
              </button>
            </div>
            {runMode === 'storyboard_prep' && (
              <span className="text-[11px] text-slate-500 whitespace-nowrap shrink-0">
                推荐：先调幕再写稿，S4 只跑一次
              </span>
            )}

            <button
              type="button"
              disabled={selectedCount === 0}
              onClick={onStartBatch}
              className="inline-flex items-center gap-2 px-4 py-2 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap shrink-0 cursor-pointer"
            >
              <Play className="w-3.5 h-3.5" />
              开始制作已选 ({selectedCount} 话)
            </button>

            <button
              type="button"
              disabled={renderedCount === 0}
              onClick={() => onExportSeriesZip(series.series_id)}
              className="inline-flex items-center gap-1.5 px-3.5 py-2 text-xs font-medium text-slate-700 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap shrink-0 cursor-pointer"
            >
              <Download className="w-3.5 h-3.5" />
              打包导出成片 ({renderedCount})
            </button>

            <button
              type="button"
              disabled={cleanableEpisodes.length === 0 || cleanupBusy || !onCleanupSeriesIntermediates}
              onClick={() => setCleanupConfirm(true)}
              title="清理已出片话次的过程文件，只保留最终成片"
              className="inline-flex items-center gap-1.5 px-3.5 py-2 text-xs font-medium text-slate-700 bg-white border border-slate-200 rounded-lg hover:border-amber-400 hover:text-amber-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap shrink-0 cursor-pointer"
            >
              <Archive className="w-3.5 h-3.5" />
              一键清理已出片过程文件 ({cleanableEpisodes.length})
            </button>
          </div>
        </div>

        {/* Patch v4.11：作品级批量清理的内联二次确认（不用 window.confirm） */}
        {cleanupConfirm && (
          <div className="mx-5 mb-4 p-3.5 rounded-lg border border-amber-200 bg-amber-50/70 text-xs text-amber-900">
            <div className="flex items-start gap-2">
              <AlertCircle className="w-4 h-4 shrink-0 mt-0.5 text-amber-600" />
              <div className="flex-1 space-y-2">
                <p className="font-medium">
                  确认批量清理 {cleanableEpisodes.length} 话的过程文件、只保留最终成片？
                  {reclaimableTotal > 0 && (
                    <>
                      预计共释放约{' '}
                      <span className="font-mono-tabular">{formatBytes(reclaimableTotal)}</span>
                      。
                    </>
                  )}
                </p>
                <ul className="list-disc pl-4 space-y-0.5 text-amber-800/90">
                  <li>删除各话原始截图、切图、识图、分镜、剧本、配音与时间轴等过程目录，9:16 成片完整保留，试看与导出不受影响。</li>
                  <li>作品级剧情记忆（story_bible.json）保留，不影响后续集数的记忆连贯。</li>
                  <li>运行中与已归档的话次自动跳过；07_out 与 exports 两份成片合并为硬链接，同盘只占一份体积。</li>
                  <li>日后需要修改某一话时，得从 Stage 1 重新跑完整流水线。</li>
                </ul>
                <div className="flex items-center gap-2 pt-0.5">
                  <button
                    type="button"
                    disabled={cleanupBusy}
                    onClick={() => void handleConfirmSeriesCleanup()}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-amber-600 rounded hover:bg-amber-700 transition-colors cursor-pointer disabled:opacity-50"
                  >
                    {cleanupBusy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                    确认清理 {cleanableEpisodes.length} 话
                  </button>
                  <button
                    type="button"
                    disabled={cleanupBusy}
                    onClick={() => setCleanupConfirm(false)}
                    className="px-3 py-1.5 text-xs font-medium text-slate-600 bg-white border border-slate-200 rounded hover:bg-slate-50 transition-colors cursor-pointer disabled:opacity-50"
                  >
                    取消
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Scheduling Topology Hint */}
        <div className="px-5 py-2.5 bg-slate-50 border-b border-slate-200 text-xs text-slate-600 flex flex-wrap items-center justify-between gap-2">
          <span>
            调度拓扑：Stage 1–3（下载 / 切图 / 视觉抽取）多话并发执行 · Stage 4（百炼撰稿）按话次升序加锁串行更新 StoryBible
          </span>
          <span className="font-mono-tabular text-slate-500">
            concurrency=3 · stage4_serial_lock=on
          </span>
        </div>

        {/* Table */}
        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse">
            <thead>
              <tr className="border-b border-slate-200 text-xs font-medium text-slate-500">
                <th className="py-3 pl-5 pr-2 w-10">选</th>
                <th className="py-3 px-3 w-20">话次</th>
                <th className="py-3 px-3">标题与分镜</th>
                <th className="py-3 px-3 w-36">当前进度</th>
                <th className="py-3 px-3 w-36">状态</th>
                <th className="py-3 px-3 w-28 text-right">成片时长</th>
                <th className="py-3 pl-3 pr-5 w-64 text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 text-sm">
              {filteredEpisodes.map((ep) => {
                const doneStages = ep.stages.filter((s) => s.status === 'done').length;
                const isVideoReady =
                  ep.stages.find((s) => s.id === 's7_renderer')?.status === 'done';
                return (
                  <tr
                    key={ep.episode_id}
                    className={`hover:bg-slate-50/80 transition-colors ${
                      ep.is_locked ? 'opacity-60' : ''
                    }`}
                  >
                    <td className="py-3 pl-5 pr-2">
                      <input
                        type="checkbox"
                        disabled={ep.is_locked}
                        checked={ep.selected && !ep.is_locked}
                        onChange={() => onToggleEpisode(ep.episode_id)}
                        className="w-4 h-4 rounded border-slate-300 text-blue-600 focus:ring-blue-600 cursor-pointer"
                      />
                    </td>
                    <td className="py-3 px-3 font-mono-tabular text-xs font-medium text-slate-700">
                      EP.{String(ep.episode_no).padStart(3, '0')}
                    </td>
                    <td className="py-3 px-3">
                      <div className="font-medium text-slate-900">{ep.title}</div>
                      <div className="text-xs text-slate-500 flex items-center gap-2 mt-0.5 font-mono-tabular">
                        <span>{ep.publish_date}</span>
                        <span aria-hidden="true">·</span>
                        <span>{ep.panels.length} 格分镜</span>
                        <span aria-hidden="true">·</span>
                        <span>{ep.beats.length} 句解说</span>
                      </div>
                    </td>
                    <td className="py-3 px-3">
                      <div className="flex items-center gap-2">
                        <div className="w-20 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                          <div
                            className="h-full bg-blue-600 transition-all duration-300"
                            style={{ width: `${(doneStages / ep.stages.length) * 100}%` }}
                          />
                        </div>
                        <span className="text-xs font-mono-tabular text-slate-600">
                          {doneStages}/{ep.stages.length}
                        </span>
                      </div>
                    </td>
                    <td className="py-3 px-3">{renderStatusText(ep)}</td>
                    <td className="py-3 px-3 text-right font-mono-tabular text-xs text-slate-700">
                      {ep.video_duration_ms > 0 ? formatMs(ep.video_duration_ms) : '—'}
                    </td>
                    <td className="py-3 pl-3 pr-5 text-right">
                      <div className="inline-flex items-center justify-end gap-3">
                        {/* Patch v4.5：单话结尾图模式，切换即自动保存，仅重跑 S7 生效 */}
                        <select
                          aria-label={`第 ${ep.episode_no} 话结尾图模式`}
                          value={ep.render_options?.outro_mode ?? 'inherit'}
                          disabled={ep.is_locked}
                          onChange={(e) => {
                            const current: EpisodeRenderOptions = ep.render_options ?? {
                              cover_enabled: true,
                              cover_text_override: null,
                              outro_mode: 'inherit',
                            };
                            onUpdateEpisodeRenderOptions(ep.episode_id, {
                              ...current,
                              outro_mode: e.target.value as OutroMode,
                            });
                          }}
                          className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700 focus:outline-none focus:ring-2 focus:ring-blue-600/20 focus:border-blue-600 disabled:opacity-50 cursor-pointer whitespace-nowrap"
                        >
                          {OUTRO_MODE_OPTIONS.map((option) => (
                            <option key={option.value} value={option.value}>
                              {option.label}
                            </option>
                          ))}
                        </select>
                        {isVideoReady && (
                          <button
                            type="button"
                            onClick={() => onExportEpisode(ep.episode_id, 'mp4')}
                            className="inline-flex items-center gap-1 text-xs font-medium text-emerald-700 hover:text-emerald-800 whitespace-nowrap shrink-0 cursor-pointer"
                          >
                            <Download className="w-3.5 h-3.5" />
                            导出 MP4
                          </button>
                        )}
                        <button
                          type="button"
                          disabled={ep.is_locked}
                          onClick={() => onOpenEpisode(ep.episode_id)}
                          className="inline-flex items-center gap-1 text-xs font-medium text-blue-600 hover:text-blue-700 disabled:text-slate-400 disabled:cursor-not-allowed whitespace-nowrap shrink-0 cursor-pointer"
                        >
                          进入审核台
                          <ArrowRight className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
};
