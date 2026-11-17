import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  CheckCircle2,
  Clock,
  Download,
  Eye,
  FileText,
  Film,
  FolderOutput,
  Loader2,
  Pause,
  Play,
  Plus,
  RotateCcw,
  Terminal,
  Trash2,
  Volume2,
  AlertCircle,
} from 'lucide-react';
import type {
  CameraMotion,
  EpisodeItem,
  PronunciationRule,
  ScriptBeat,
  StageId,
  StudioConfig,
} from '../types/studio';
import { formatMs, playPreviewTone, resolveSpokenText } from '../utils/pronunciation';
import { PanelZoomModal, type SaveStatus } from './PanelZoomModal';

interface EpisodeStudioViewProps {
  episode: EpisodeItem;
  seriesId: string;
  config: StudioConfig;
  rules: PronunciationRule[];
  onRerunFromStage: (episodeId: string, stageId: StageId) => void;
  onTogglePanelBlank: (episodeId: string, panelId: string) => void;
  onUpdateBeats: (episodeId: string, beats: ScriptBeat[], triggerFromStage5: boolean) => void;
  /** 分镜大图弹窗里的字幕原文自动保存（不触发重跑配音）。缺省时退化为本地草稿保存。 */
  onAutoSaveBeats?: (episodeId: string, beats: ScriptBeat[]) => Promise<void>;
  onExportEpisode: (episodeId: string, format: 'mp4' | 'ass' | 'script') => void;
}

type StudioTab = 'panels' | 'script' | 'timeline' | 'preview';

const MOTION_LABELS: Record<CameraMotion, string> = {
  zoom_in: 'ZOOM_IN 缓推',
  zoom_out: 'ZOOM_OUT 缓拉',
  pan_down: 'PAN_DOWN 竖扫',
  pan_up: 'PAN_UP 上移',
  shake: 'SHAKE 震动',
  static: 'STATIC 静止',
};

export const EpisodeStudioView: React.FC<EpisodeStudioViewProps> = ({
  episode,
  seriesId,
  config,
  rules,
  onRerunFromStage,
  onTogglePanelBlank,
  onUpdateBeats,
  onAutoSaveBeats,
  onExportEpisode,
}) => {
  const [activeTab, setActiveTab] = useState<StudioTab>('script');
  const [selectedPanelId, setSelectedPanelId] = useState<string>(
    episode.panels[0]?.panel_id ?? ''
  );
  const [showFocusOverlay, setShowFocusOverlay] = useState(true);
  const [draftBeats, setDraftBeats] = useState<ScriptBeat[]>(episode.beats);
  const [playingBeatId, setPlayingBeatId] = useState<string | null>(null);

  // 分镜大图弹窗 + 字幕原文保存（切换分镜 / 关闭弹窗时落盘，编辑过程中不写库）
  const [zoomBeatId, setZoomBeatId] = useState<string | null>(null);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle');
  const [saveMessage, setSaveMessage] = useState('');
  const draftBeatsRef = useRef<ScriptBeat[]>(episode.beats);
  /** 待保存的草稿：{ episode_id, beats }；只在弹窗里改字时写入，落盘后清空 */
  const pendingRef = useRef<{ episodeId: string; beats: ScriptBeat[] } | null>(null);

  // Video simulator state (9:16 standard)
  const [isPlayingVideo, setIsPlayingVideo] = useState(false);
  const [videoTimeMs, setVideoTimeMs] = useState(0);
  const isVideoReady = episode.stages.find((s) => s.id === 's7_renderer')?.status === 'done';
  const epFolderName = `ep_${String(episode.episode_no).padStart(3, '0')}`;
  const localOutputPath = `workspace/${seriesId}/episodes/${epFolderName}/07_out/${episode.episode_id}.mp4`;

  useEffect(() => {
    // 后台会定时刷新剧集数据并回填 episode.beats；只要本话还有未保存的改动，
    // 就保留本地草稿，别让用户正在改的字被服务端数据冲掉。
    const pending = pendingRef.current;
    if (!pending || pending.episodeId !== episode.episode_id) {
      setDraftBeats(episode.beats);
      draftBeatsRef.current = episode.beats;
    }
    setSelectedPanelId(episode.panels[0]?.panel_id ?? '');
    setVideoTimeMs(0);
    setIsPlayingVideo(false);
  }, [episode.episode_id, episode.beats, episode.panels]);

  const activePanel = useMemo(
    () => episode.panels.find((p) => p.panel_id === selectedPanelId) ?? episode.panels[0],
    [episode.panels, selectedPanelId]
  );

  // Compute simulated Audio-First timeline from draftBeats
  const timelineSegments = useMemo(() => {
    let cursor = 0;
    return draftBeats.map((beat) => {
      const startMs = cursor;
      const speechEndMs = startMs + beat.audio_duration_ms;
      const totalBeatMs = beat.audio_duration_ms + beat.pause_after_ms;
      const endMs = startMs + totalBeatMs;
      cursor = endMs;
      const panel =
        episode.panels.find((p) => p.panel_id === beat.target_panel_ids[0]) ??
        episode.panels[0];
      return {
        beat,
        panel,
        startMs,
        speechEndMs,
        endMs,
        totalBeatMs,
      };
    });
  }, [draftBeats, episode.panels]);

  const totalSimDurationMs = useMemo(
    () =>
      timelineSegments.length > 0
        ? timelineSegments[timelineSegments.length - 1].endMs
        : 0,
    [timelineSegments]
  );

  useEffect(() => {
    if (!isPlayingVideo || totalSimDurationMs === 0) return;
    let rafId = 0;
    let last = performance.now();
    const tick = (now: number) => {
      const dt = now - last;
      last = now;
      setVideoTimeMs((prev) => {
        const next = prev + dt;
        if (next >= totalSimDurationMs) {
          setIsPlayingVideo(false);
          return totalSimDurationMs;
        }
        return next;
      });
      rafId = requestAnimationFrame(tick);
    };
    rafId = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafId);
  }, [isPlayingVideo, totalSimDurationMs]);

  const currentVideoSegment = useMemo(() => {
    return (
      timelineSegments.find(
        (seg) => videoTimeMs >= seg.startMs && videoTimeMs <= seg.endMs
      ) ?? timelineSegments[0]
    );
  }, [timelineSegments, videoTimeMs]);

  const activeSubtitle = useMemo(() => {
    if (!currentVideoSegment) return '';
    if (videoTimeMs <= currentVideoSegment.speechEndMs) {
      return currentVideoSegment.beat.narration_text;
    }
    return '';
  }, [currentVideoSegment, videoTimeMs]);

  const updateBeatField = <K extends keyof ScriptBeat>(
    beatId: string,
    key: K,
    value: ScriptBeat[K]
  ) => {
    setDraftBeats((prev) =>
      prev.map((b) => (b.beat_id === beatId ? { ...b, [key]: value } : b))
    );
  };

  // ── 分镜大图弹窗：字幕原文改完，在切换分镜 / 关闭弹窗时落盘 ────────────
  /**
   * 把待保存的草稿写回后端；没有待保存内容就不发请求。
   * 返回是否保存成功（失败时保留弹窗，让用户看到错误并可重试）。
   */
  const flushSave = useCallback(async (): Promise<boolean> => {
    const pending = pendingRef.current;
    if (!pending) return true;
    const { episodeId, beats } = pending;
    setSaveStatus('saving');
    try {
      if (!onAutoSaveBeats) {
        // 没有独立通道时退化为普通保存（不触发重跑配音）
        onUpdateBeats(episodeId, beats, false);
      } else {
        await onAutoSaveBeats(episodeId, beats);
      }
      pendingRef.current = null;
      setSaveStatus('saved');
      setSaveMessage(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
      return true;
    } catch (err) {
      setSaveStatus('error');
      setSaveMessage(err instanceof Error ? err.message : String(err));
      return false;
    }
  }, [onAutoSaveBeats, onUpdateBeats]);

  /** 弹窗内改字：只改本地草稿，落盘交给切格 / 关闭时统一做 */
  const handleZoomNarrationChange = (beatId: string, text: string) => {
    const next = draftBeatsRef.current.map((b) =>
      b.beat_id === beatId ? { ...b, narration_text: text } : b
    );
    draftBeatsRef.current = next;
    setDraftBeats(next);
    pendingRef.current = { episodeId: episode.episode_id, beats: next };
    setSaveStatus('dirty');
    setSaveMessage('切换分镜或关闭弹窗时自动保存');
  };

  /** 上一格 / 下一格：先把当前格的改动存下来，再翻页 */
  const handleZoomSelectBeat = useCallback(
    async (nextBeatId: string) => {
      if (nextBeatId === zoomBeatId) return;
      const ok = await flushSave();
      if (!ok) return; // 保存失败就别翻页，避免看起来改好了其实没存上
      setZoomBeatId(nextBeatId);
    },
    [flushSave, zoomBeatId]
  );

  /** 关闭弹窗：先存盘，成功才关 */
  const handleZoomClose = useCallback(async () => {
    const ok = await flushSave();
    if (!ok) {
      setSaveMessage('保存失败，请检查后端服务后再次点击关闭重试');
      return;
    }
    setZoomBeatId(null);
  }, [flushSave]);

  // 兜底：换话或卸载时把还挂着的草稿写回它所属的那一话。
  // 依赖只能是 episode_id —— 若依赖 flushSave，App 的定时刷新会让回调身份变化，
  // 清理函数被反复触发，等于又在编辑过程中偷偷保存。
  const flushSaveRef = useRef(flushSave);
  flushSaveRef.current = flushSave;
  useEffect(
    () => () => {
      void flushSaveRef.current();
    },
    [episode.episode_id]
  );

  const handleAddBeat = () => {
    const idx = draftBeats.length + 1;
    const firstPanel = episode.panels[0]?.panel_id ?? 'p_001';
    const newBeat: ScriptBeat = {
      beat_id: `b_${String(idx).padStart(3, '0')}`,
      narration_text: '请在此输入新的短句解说文案。',
      tts_text: '',
      emotion_instruction: '用自然平稳的漫评解说语气说出',
      target_panel_ids: [firstPanel],
      motion: 'zoom_in',
      pause_after_ms: 250,
      audio_duration_ms: 2800,
    };
    setDraftBeats((prev) => [...prev, newBeat]);
  };

  const handleDeleteBeat = (beatId: string) => {
    setDraftBeats((prev) => prev.filter((b) => b.beat_id !== beatId));
  };

  const handlePreviewAudio = (beatId: string) => {
    playPreviewTone();
    setPlayingBeatId(beatId);
    setTimeout(() => {
      setPlayingBeatId((cur) => (cur === beatId ? null : cur));
    }, 1200);
  };

  const stageProgressRatio =
    totalSimDurationMs > 0 && currentVideoSegment
      ? Math.min(
          1,
          Math.max(
            0,
            (videoTimeMs - currentVideoSegment.startMs) /
              Math.max(1, currentVideoSegment.totalBeatMs)
          )
        )
      : 0;

  const getMotionStyle = (motion: CameraMotion, progress: number): React.CSSProperties => {
    if (motion === 'zoom_in') {
      const s = 1 + 0.14 * progress;
      return { transform: `scale(${s.toFixed(3)})` };
    }
    if (motion === 'zoom_out') {
      const s = 1.14 - 0.14 * progress;
      return { transform: `scale(${s.toFixed(3)})` };
    }
    if (motion === 'pan_down') {
      const y = 16 - 32 * progress;
      return { transform: `scale(1.12) translateY(${y.toFixed(1)}px)` };
    }
    if (motion === 'pan_up') {
      const y = -16 + 32 * progress;
      return { transform: `scale(1.12) translateY(${y.toFixed(1)}px)` };
    }
    return { transform: 'scale(1)' };
  };

  return (
    <div className="space-y-6">
      {/* 8-Step Interactive Pipeline Stepper */}
      <section className="bg-white border border-slate-200 rounded-xl p-5">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">
              流水线执行状态与断点重跑（manifest.json）
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              点击任意步骤右侧的「从此步重跑」，系统会自动跳过上游已完成产物
            </p>
          </div>
          <div className="text-xs font-mono-tabular text-slate-500">
            {episode.episode_id} · 总时长 {totalSimDurationMs > 0 ? formatMs(totalSimDurationMs) : '未合成'}
          </div>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-2.5">
          {episode.stages.map((st) => {
            const isDone = st.status === 'done';
            const isRunning = st.status === 'running';
            const isFailed = st.status === 'failed';
            return (
              <div
                key={st.id}
                className={`p-3 rounded-lg border transition-colors flex flex-col justify-between gap-2 ${
                  isRunning
                    ? 'border-blue-500 bg-blue-50/40'
                    : isDone
                    ? 'border-slate-200 bg-slate-50/60'
                    : isFailed
                    ? 'border-red-300 bg-red-50/40'
                    : 'border-slate-200 bg-white opacity-70'
                }`}
              >
                <div className="flex items-center justify-between gap-1">
                  <span className="text-xs font-mono-tabular font-semibold text-slate-700">
                    {st.code}
                  </span>
                  {isRunning ? (
                    <Loader2 className="w-3.5 h-3.5 text-blue-600 animate-spin" />
                  ) : isDone ? (
                    <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600" />
                  ) : isFailed ? (
                    <AlertCircle className="w-3.5 h-3.5 text-red-600" />
                  ) : (
                    <Clock className="w-3.5 h-3.5 text-slate-400" />
                  )}
                </div>

                <div>
                  <div className="text-xs font-medium text-slate-900 truncate">{st.label}</div>
                  <div className="text-[11px] font-mono-tabular text-slate-500 mt-0.5">
                    {st.durationSec !== undefined
                      ? `${st.durationSec.toFixed(1)}s`
                      : isDone
                        ? '已完成'
                        : isFailed
                          ? '失败'
                          : isRunning
                            ? '执行中'
                            : '待执行'}
                  </div>
                </div>

                <button
                  type="button"
                  onClick={() => onRerunFromStage(episode.episode_id, st.id)}
                  className="w-full py-1 px-2 text-[11px] font-medium text-slate-700 bg-white border border-slate-200 rounded hover:border-blue-400 hover:text-blue-600 transition-colors whitespace-nowrap shrink-0 cursor-pointer"
                >
                  从此步重跑
                </button>
              </div>
            );
          })}
        </div>
      </section>

      {/* Main Studio Workspace Tabs */}
      <section className="bg-white border border-slate-200 rounded-xl overflow-hidden">
        <div className="px-5 py-3.5 border-b border-slate-200 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-1 p-1 bg-slate-100 rounded-lg overflow-x-auto">
            {(
              [
                { id: 'panels', label: `分镜与视觉抽取 (${episode.panels.length})` },
                { id: 'script', label: `解说与多音字审核 (${draftBeats.length})` },
                { id: 'timeline', label: '时间轴与执行日志' },
                { id: 'preview', label: '成片动态预览' },
              ] as const
            ).map((tab) => (
              <button
                key={tab.id}
                type="button"
                onClick={() => setActiveTab(tab.id)}
                className={`px-3.5 py-1.5 text-xs font-medium rounded-md transition-colors whitespace-nowrap shrink-0 cursor-pointer ${
                  activeTab === tab.id
                    ? 'bg-white text-slate-900 shadow-xs'
                    : 'text-slate-600 hover:text-slate-900'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {activeTab === 'script' && (
            <div className="flex items-center gap-2.5">
              <button
                type="button"
                onClick={handleAddBeat}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-slate-700 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 whitespace-nowrap shrink-0 cursor-pointer"
              >
                <Plus className="w-3.5 h-3.5" />
                新增解说句
              </button>
              <button
                type="button"
                onClick={() => onUpdateBeats(episode.episode_id, draftBeats, true)}
                className="inline-flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 whitespace-nowrap shrink-0 cursor-pointer"
              >
                <RotateCcw className="w-3.5 h-3.5" />
                保存并从 Stage 5 重跑配音
              </button>
            </div>
          )}
        </div>

        {/* Empty State if Episode not crawled yet */}
        {episode.panels.length === 0 ? (
          <div className="p-12 text-center">
            <p className="text-sm font-medium text-slate-900">
              本话尚未完成 Stage 1–3 下载与分镜抽取
            </p>
            <p className="text-xs text-slate-500 mt-1 mb-4">
              请点击上方步骤条中的「S1 从此步重跑」，模拟本地下载切片并调用百炼 qwen3-vl-plus 解析分镜。
            </p>
            <button
              type="button"
              onClick={() => onRerunFromStage(episode.episode_id, 's1_crawler')}
              className="inline-flex items-center gap-2 px-4 py-2 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 cursor-pointer"
            >
              <Play className="w-3.5 h-3.5" />
              立即启动 S1–S4 流水线
            </button>
          </div>
        ) : (
          <>
            {/* TAB 1: Panels & Vision Inspector */}
            {activeTab === 'panels' && activePanel && (
              <div className="p-5 grid grid-cols-1 lg:grid-cols-12 gap-6">
                {/* Panel Strip */}
                <div className="lg:col-span-4 space-y-2.5 max-h-[620px] overflow-y-auto pr-1">
                  {episode.panels.map((p) => {
                    const isSelected = p.panel_id === activePanel.panel_id;
                    return (
                      <button
                        key={p.panel_id}
                        type="button"
                        onClick={() => setSelectedPanelId(p.panel_id)}
                        className={`w-full text-left p-2.5 rounded-lg border flex items-start gap-3 transition-colors cursor-pointer ${
                          isSelected
                            ? 'border-blue-600 bg-blue-50/40'
                            : 'border-slate-200 hover:bg-slate-50'
                        } ${p.is_transition_blank ? 'opacity-50' : ''}`}
                      >
                        <img
                          src={p.image_url}
                          alt={p.panel_id}
                          referrerPolicy="no-referrer"
                          className="w-16 h-20 object-cover rounded border border-slate-200 shrink-0"
                        />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center justify-between gap-2">
                            <span className="text-xs font-mono-tabular font-semibold text-slate-900">
                              {p.panel_id}
                            </span>
                            <span className="text-[11px] font-mono-tabular text-slate-500">
                              切片 {p.source_slice_range[0]}–{p.source_slice_range[1]}
                            </span>
                          </div>
                          <p className="text-xs text-slate-600 line-clamp-2 mt-1">
                            {p.scene_summary}
                          </p>
                          <div className="text-[11px] text-slate-400 font-mono-tabular mt-1.5">
                            {p.width}×{p.height} · {p.dialogues.length} 条气泡
                            {p.is_transition_blank ? ' · 已标记跳过' : ''}
                          </div>
                        </div>
                      </button>
                    );
                  })}
                </div>

                {/* Selected Panel Detail + Focus Region Overlay */}
                <div className="lg:col-span-8 grid grid-cols-1 md:grid-cols-2 gap-6 items-start">
                  <div className="bg-slate-900 rounded-xl p-3 flex flex-col items-center">
                    <div className="relative w-full max-w-[300px] aspect-[3/4] overflow-hidden rounded-lg">
                      <img
                        src={activePanel.image_url}
                        alt={activePanel.panel_id}
                        referrerPolicy="no-referrer"
                        className="w-full h-full object-cover"
                      />
                      {showFocusOverlay && (
                        <div
                          className="absolute border-2 border-amber-400 bg-amber-400/10 pointer-events-none"
                          style={{
                            left: `${activePanel.focus_region[0] * 100}%`,
                            top: `${activePanel.focus_region[1] * 100}%`,
                            width: `${(activePanel.focus_region[2] - activePanel.focus_region[0]) * 100}%`,
                            height: `${(activePanel.focus_region[3] - activePanel.focus_region[1]) * 100}%`,
                          }}
                        >
                          <span className="inline-block bg-amber-400 text-slate-950 text-[10px] font-mono-tabular font-semibold px-1">
                            focus_region
                          </span>
                        </div>
                      )}
                    </div>

                    <div className="w-full flex items-center justify-between mt-3 pt-3 border-t border-slate-800 text-xs text-slate-300">
                      <label className="inline-flex items-center gap-2 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={showFocusOverlay}
                          onChange={(e) => setShowFocusOverlay(e.target.checked)}
                          className="rounded border-slate-600"
                        />
                        显示视觉焦点框
                      </label>
                      <button
                        type="button"
                        onClick={() => onTogglePanelBlank(episode.episode_id, activePanel.panel_id)}
                        className="text-xs text-slate-300 hover:text-white underline cursor-pointer"
                      >
                        {activePanel.is_transition_blank ? '恢复参与剪辑' : '标记为过渡废格(跳过)'}
                      </button>
                    </div>
                  </div>

                  <div className="space-y-4">
                    <div>
                      <div className="text-xs font-medium text-slate-500">
                        {config.visionModel} 画面描述 (scene_summary)
                      </div>
                      <p className="text-sm text-slate-900 mt-1 leading-relaxed">
                        {activePanel.scene_summary}
                      </p>
                    </div>

                    <div className="pt-3 border-t border-slate-100">
                      <div className="text-xs font-medium text-slate-500 mb-2">
                        气泡 OCR 与说话人归因 (dialogues)
                      </div>
                      <div className="space-y-2">
                        {activePanel.dialogues.map((d, idx) => (
                          <div
                            key={idx}
                            className="p-3 bg-slate-50 border border-slate-200 rounded-lg text-xs"
                          >
                            <div className="flex items-center justify-between text-slate-500 mb-1">
                              <span className="font-semibold text-slate-800">{d.speaker_guess}</span>
                              <span className="font-mono-tabular">emotion: {d.emotion}</span>
                            </div>
                            <p className="text-slate-800">{d.text}</p>
                          </div>
                        ))}
                      </div>
                    </div>

                    <div className="pt-3 border-t border-slate-100 flex items-center justify-between text-xs text-slate-600">
                      <span>拟声词 (sfx_list)：{activePanel.sfx_list.join(' / ') || '无'}</span>
                      <span className="font-mono-tabular text-slate-400">
                        [{activePanel.focus_region.map((n) => n.toFixed(2)).join(', ')}]
                      </span>
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* TAB 2: Script & Polyphone Editor */}
            {activeTab === 'script' && (
              <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse">
                  <thead>
                    <tr className="bg-slate-50 border-b border-slate-200 text-xs font-medium text-slate-500">
                      <th className="py-3 pl-5 pr-2 w-20">分镜</th>
                      <th className="py-3 px-3 min-w-[260px]">
                        字幕原文 (narration_text · 屏幕显示)
                      </th>
                      <th className="py-3 px-3 min-w-[220px]">
                        TTS 送音文本（整句 · 不改字幕）
                      </th>
                      <th className="py-3 px-3 min-w-[210px]">
                        Qwen TTS 情绪指令 (instruction)
                      </th>
                      <th className="py-3 px-3 w-36">运镜与停顿</th>
                      <th className="py-3 pl-3 pr-5 w-28 text-right">试听 / 删</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 text-xs align-top">
                    {draftBeats.map((beat) => {
                      const panel = episode.panels.find(
                        (p) => p.panel_id === beat.target_panel_ids[0]
                      );
                      const { spokenText, matchedRules } = resolveSpokenText(beat, rules);
                      const charLen = beat.narration_text.trim().length;
                      const isLengthWarn = charLen < 8 || charLen > 30;
                      return (
                        <tr key={beat.beat_id} className="hover:bg-slate-50/50">
                          <td className="py-3.5 pl-5 pr-2">
                            <div className="space-y-1.5">
                              <div className="font-mono-tabular font-semibold text-slate-700">
                                {beat.beat_id}
                              </div>
                              {panel && (
                                <button
                                  type="button"
                                  onClick={() => setZoomBeatId(beat.beat_id)}
                                  title="放大查看分镜并编辑字幕原文"
                                  aria-label={`放大查看 ${panel.panel_id} 并编辑字幕原文`}
                                  className="block w-12 h-16 rounded border border-slate-200 overflow-hidden hover:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/40 cursor-pointer transition-colors"
                                >
                                  <img
                                    src={panel.image_url}
                                    alt={panel.panel_id}
                                    referrerPolicy="no-referrer"
                                    className="w-12 h-16 object-cover"
                                  />
                                </button>
                              )}
                              <select
                                value={beat.target_panel_ids[0]}
                                onChange={(e) =>
                                  updateBeatField(beat.beat_id, 'target_panel_ids', [
                                    e.target.value,
                                  ])
                                }
                                className="w-16 px-1 py-0.5 text-[11px] font-mono-tabular bg-white border border-slate-200 rounded"
                              >
                                {episode.panels.map((p) => (
                                  <option key={p.panel_id} value={p.panel_id}>
                                    {p.panel_id}
                                  </option>
                                ))}
                              </select>
                            </div>
                          </td>

                          <td className="py-3.5 px-3">
                            <textarea
                              rows={2}
                              value={beat.narration_text}
                              onChange={(e) =>
                                updateBeatField(beat.beat_id, 'narration_text', e.target.value)
                              }
                              className="w-full p-2 text-xs text-slate-900 bg-white border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600 resize-none"
                            />
                            <div className="flex items-center justify-between mt-1 text-[11px] font-mono-tabular">
                              <span className={isLengthWarn ? 'text-amber-600' : 'text-slate-400'}>
                                {charLen} 字（建议 10–28 字）
                              </span>
                              <span className="text-slate-400">
                                时长 {(beat.audio_duration_ms / 1000).toFixed(2)}s
                              </span>
                            </div>
                          </td>

                          <td className="py-3.5 px-3">
                            <textarea
                              rows={2}
                              value={beat.tts_text}
                              onChange={(e) =>
                                updateBeatField(beat.beat_id, 'tts_text', e.target.value)
                              }
                              placeholder="留空则自动继承字幕 + 词典替换；填写时必须是整句送音文本..."
                              className="w-full p-2 text-xs text-slate-800 bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600 resize-none"
                            />
                            <div className="mt-1 text-[11px] text-slate-500 leading-snug">
                              {matchedRules.length > 0 ? (
                                <span className="text-blue-700">
                                  词典命中：{matchedRules.map((r) => `${r.source}→${r.replacement}`).join('、')}
                                  {' · '}实际送音：{spokenText}
                                </span>
                              ) : (
                                <span className="text-slate-400">无词典替换 · 直接按字幕语义合成</span>
                              )}
                            </div>
                          </td>

                          <td className="py-3.5 px-3">
                            <textarea
                              rows={2}
                              value={beat.emotion_instruction}
                              onChange={(e) =>
                                updateBeatField(
                                  beat.beat_id,
                                  'emotion_instruction',
                                  e.target.value
                                )
                              }
                              className="w-full p-2 text-xs text-slate-800 bg-white border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600 resize-none"
                            />
                            <div className="mt-1 text-[11px] font-mono-tabular text-slate-400">
                              {config.ttsModel} · {config.ttsVoice}
                            </div>
                          </td>

                          <td className="py-3.5 px-3 space-y-2">
                            <select
                              value={beat.motion}
                              onChange={(e) =>
                                updateBeatField(
                                  beat.beat_id,
                                  'motion',
                                  e.target.value as CameraMotion
                                )
                              }
                              className="w-full px-2 py-1.5 text-[11px] font-mono-tabular bg-white border border-slate-200 rounded-lg"
                            >
                              {Object.entries(MOTION_LABELS).map(([val, label]) => (
                                <option key={val} value={val}>
                                  {label}
                                </option>
                              ))}
                            </select>

                            <div className="flex items-center gap-1.5">
                              <span className="text-[11px] text-slate-500 whitespace-nowrap">
                                停顿ms
                              </span>
                              <input
                                type="number"
                                step={50}
                                min={0}
                                max={2000}
                                value={beat.pause_after_ms}
                                onChange={(e) =>
                                  updateBeatField(
                                    beat.beat_id,
                                    'pause_after_ms',
                                    Number(e.target.value) || 0
                                  )
                                }
                                className="w-full px-2 py-1 text-[11px] font-mono-tabular bg-white border border-slate-200 rounded"
                              />
                            </div>
                          </td>

                          <td className="py-3.5 pl-3 pr-5 text-right">
                            <div className="flex items-center justify-end gap-1.5">
                              <button
                                type="button"
                                onClick={() => handlePreviewAudio(beat.beat_id)}
                                className={`inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border text-[11px] font-medium whitespace-nowrap shrink-0 cursor-pointer transition-colors ${
                                  playingBeatId === beat.beat_id
                                    ? 'border-blue-600 bg-blue-50 text-blue-700'
                                    : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
                                }`}
                              >
                                <Volume2 className="w-3.5 h-3.5" />
                                {playingBeatId === beat.beat_id ? '试听中' : '试听'}
                              </button>
                              <button
                                type="button"
                                onClick={() => handleDeleteBeat(beat.beat_id)}
                                aria-label={`删除 ${beat.beat_id}`}
                                className="p-1.5 text-slate-400 hover:text-red-600 cursor-pointer"
                              >
                                <Trash2 className="w-3.5 h-3.5" />
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

            {/* TAB 3: Audio-First Timeline & SSE Logs */}
            {activeTab === 'timeline' && (
              <div className="p-5 space-y-6">
                <div>
                  <div className="flex items-center justify-between mb-3">
                    <h3 className="text-sm font-semibold text-slate-900">
                      Audio-First 音画同步时间轴（30fps 帧对齐）
                    </h3>
                    <span className="text-xs font-mono-tabular text-slate-500">
                      总时长 {formatMs(totalSimDurationMs)} · 0 帧漂移
                    </span>
                  </div>

                  <div className="border border-slate-200 rounded-lg p-4 bg-slate-50 space-y-3 overflow-x-auto">
                    {/* Track 1: Panels */}
                    <div className="flex items-center gap-3">
                      <span className="w-20 text-xs font-medium text-slate-600 shrink-0">
                        画面分镜轨
                      </span>
                      <div className="flex-1 flex gap-1 min-w-[540px]">
                        {timelineSegments.map((seg) => {
                          const pct =
                            totalSimDurationMs > 0
                              ? (seg.totalBeatMs / totalSimDurationMs) * 100
                              : 25;
                          return (
                            <div
                              key={`v-${seg.beat.beat_id}`}
                              style={{ width: `${pct}%` }}
                              className="h-10 bg-white border border-slate-300 rounded px-2 flex items-center justify-between text-[11px] font-mono-tabular text-slate-700 overflow-hidden"
                            >
                              <span className="truncate font-semibold">
                                {seg.beat.target_panel_ids.join('+')}
                              </span>
                              <span className="text-slate-400 ml-1 shrink-0">
                                {seg.beat.motion}
                              </span>
                            </div>
                          );
                        })}
                      </div>
                    </div>

                    {/* Track 2: Qwen TTS Audio */}
                    <div className="flex items-center gap-3">
                      <span className="w-20 text-xs font-medium text-slate-600 shrink-0">
                        Qwen配音轨
                      </span>
                      <div className="flex-1 flex gap-1 min-w-[540px]">
                        {timelineSegments.map((seg) => {
                          const pct =
                            totalSimDurationMs > 0
                              ? (seg.totalBeatMs / totalSimDurationMs) * 100
                              : 25;
                          const speechRatio =
                            seg.totalBeatMs > 0
                              ? (seg.beat.audio_duration_ms / seg.totalBeatMs) * 100
                              : 90;
                          return (
                            <div
                              key={`a-${seg.beat.beat_id}`}
                              style={{ width: `${pct}%` }}
                              className="h-8 bg-slate-200/70 rounded overflow-hidden flex"
                            >
                              <div
                                style={{ width: `${speechRatio}%` }}
                                className="h-full bg-blue-600/85 text-white px-2 flex items-center text-[11px] font-mono-tabular truncate"
                              >
                                {seg.beat.beat_id}.wav ({seg.beat.audio_duration_ms}ms)
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>

                    {/* Track 3: ASS Subtitles */}
                    <div className="flex items-center gap-3">
                      <span className="w-20 text-xs font-medium text-slate-600 shrink-0">
                        ASS 字幕轨
                      </span>
                      <div className="flex-1 flex gap-1 min-w-[540px]">
                        {timelineSegments.map((seg) => {
                          const pct =
                            totalSimDurationMs > 0
                              ? (seg.totalBeatMs / totalSimDurationMs) * 100
                              : 25;
                          return (
                            <div
                              key={`s-${seg.beat.beat_id}`}
                              style={{ width: `${pct}%` }}
                              className="h-8 bg-emerald-50 border border-emerald-200 rounded px-2 flex items-center text-[11px] text-emerald-900 truncate"
                            >
                              {seg.beat.narration_text}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  </div>
                </div>

                {/* Live SSE Log Output */}
                <div>
                  <div className="flex items-center gap-2 mb-2.5">
                    <Terminal className="w-4 h-4 text-slate-600" />
                    <h3 className="text-sm font-semibold text-slate-900">
                      流水线实时日志（SSE Stream 模拟）
                    </h3>
                  </div>
                  <div className="bg-slate-950 text-slate-200 rounded-xl p-4 font-mono-tabular text-xs leading-6 max-h-64 overflow-y-auto">
                    {episode.logs.map((line, i) => (
                      <div key={i} className="whitespace-pre-wrap">
                        {line}
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {/* TAB 4: Video Dynamic Previewer */}
            {activeTab === 'preview' && (
              <div className="p-5 grid grid-cols-1 lg:grid-cols-12 gap-6 items-start">
                <div className="lg:col-span-7 flex flex-col items-center bg-slate-900 rounded-xl p-6">
                  <div className="w-full flex items-center justify-between mb-4 text-xs text-slate-300">
                    <span className="inline-flex items-center gap-1.5 font-medium">
                      <Film className="w-4 h-4 text-blue-400" />
                      成片运镜与 ASS 字幕实时模拟播放器
                    </span>
                    <span className="px-2.5 py-1 text-[11px] font-mono-tabular bg-slate-800 text-slate-200 rounded-md">
                      9:16 竖屏 · 1080×1920 · 30fps
                    </span>
                  </div>

                  {/* Viewport Frame (Locked 9:16 Vertical Standard) */}
                  <div className="relative overflow-hidden rounded-xl border border-slate-700 bg-black flex items-center justify-center w-[280px] aspect-[9/16]">
                    {currentVideoSegment?.panel ? (
                      <>
                        {/* Blurred Ambient Background */}
                        <img
                          src={currentVideoSegment.panel.image_url}
                          alt=""
                          aria-hidden="true"
                          referrerPolicy="no-referrer"
                          className="absolute inset-0 w-full h-full object-cover scale-125 blur-xl brightness-50"
                        />
                        {/* Active Motion Foreground Panel */}
                        <div className="relative z-10 w-[92%] max-h-[82%] overflow-hidden rounded-md shadow-lg">
                          <img
                            src={currentVideoSegment.panel.image_url}
                            alt={currentVideoSegment.panel.panel_id}
                            referrerPolicy="no-referrer"
                            style={getMotionStyle(
                              currentVideoSegment.beat.motion,
                              stageProgressRatio
                            )}
                            className={`w-full h-full object-cover ${
                              currentVideoSegment.beat.motion === 'shake' && isPlayingVideo
                                ? 'animate-panel-shake'
                                : ''
                            }`}
                          />
                        </div>

                        {/* ASS Bottom Safe-Zone Subtitle */}
                        {activeSubtitle && (
                          <div className="absolute z-20 inset-x-4 bottom-8 text-center pointer-events-none">
                            <span
                              className="inline-block px-3 py-1.5 rounded bg-black/75 text-white font-semibold leading-snug shadow"
                              style={{
                                fontSize: '13px',
                                textShadow: '0 1px 3px rgba(0,0,0,0.9)',
                              }}
                            >
                              {activeSubtitle}
                            </span>
                          </div>
                        )}
                      </>
                    ) : (
                      <div className="text-xs text-slate-400">暂无可预览分镜</div>
                    )}
                  </div>

                  {/* Scrubber & Play Controls */}
                  <div className="w-full max-w-[460px] mt-4 space-y-2">
                    <input
                      type="range"
                      min={0}
                      max={Math.max(1, totalSimDurationMs)}
                      value={videoTimeMs}
                      onChange={(e) => setVideoTimeMs(Number(e.target.value))}
                      className="w-full accent-blue-500 cursor-pointer"
                    />
                    <div className="flex items-center justify-between text-xs text-slate-300 font-mono-tabular">
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          onClick={() => {
                            if (videoTimeMs >= totalSimDurationMs) setVideoTimeMs(0);
                            setIsPlayingVideo((p) => !p);
                          }}
                          className="inline-flex items-center gap-1.5 px-3 py-1.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 cursor-pointer"
                        >
                          {isPlayingVideo ? (
                            <>
                              <Pause className="w-3.5 h-3.5" /> 暂停
                            </>
                          ) : (
                            <>
                              <Play className="w-3.5 h-3.5" /> 播放模拟成片
                            </>
                          )}
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            setVideoTimeMs(0);
                            setIsPlayingVideo(true);
                          }}
                          className="px-2.5 py-1.5 bg-slate-800 text-slate-200 rounded-lg hover:bg-slate-700 cursor-pointer"
                        >
                          重头播放
                        </button>
                      </div>
                      <span>
                        {formatMs(videoTimeMs)} / {formatMs(totalSimDurationMs)}
                      </span>
                    </div>
                  </div>
                </div>

                {/* Right Side: Export & Current Beat Telemetry */}
                <div className="lg:col-span-5 space-y-4">
                  {/* Export & Delivery Box */}
                  <div className="p-4 border border-slate-200 rounded-xl space-y-3 bg-white">
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2 text-sm font-semibold text-slate-900">
                        <FolderOutput className="w-4 h-4 text-blue-600" />
                        成片交付与导出
                      </div>
                      <span
                        className={`text-[11px] font-mono-tabular ${
                          isVideoReady ? 'text-emerald-700 font-medium' : 'text-amber-600'
                        }`}
                      >
                        {isVideoReady ? 'Stage 7 已出片' : '待运行 S5–S7 合成'}
                      </span>
                    </div>

                    <div className="p-2.5 bg-slate-50 border border-slate-200 rounded-lg text-[11px] font-mono-tabular text-slate-600 break-all">
                      本地落盘：{localOutputPath}
                    </div>

                    <div className="flex flex-wrap gap-2">
                      <button
                        type="button"
                        disabled={!isVideoReady}
                        onClick={() => onExportEpisode(episode.episode_id, 'mp4')}
                        className="inline-flex items-center gap-1.5 px-3.5 py-2 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap shrink-0 cursor-pointer"
                      >
                        <Download className="w-3.5 h-3.5" />
                        下载成片 (.mp4)
                      </button>

                      <button
                        type="button"
                        disabled={draftBeats.length === 0}
                        onClick={() => onExportEpisode(episode.episode_id, 'ass')}
                        className="inline-flex items-center gap-1.5 px-3 py-2 text-xs font-medium text-slate-700 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap shrink-0 cursor-pointer"
                      >
                        <FileText className="w-3.5 h-3.5" />
                        外挂字幕 (.ass)
                      </button>

                      <button
                        type="button"
                        disabled={draftBeats.length === 0}
                        onClick={() => onExportEpisode(episode.episode_id, 'script')}
                        className="inline-flex items-center gap-1.5 px-3 py-2 text-xs font-medium text-slate-700 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed transition-colors whitespace-nowrap shrink-0 cursor-pointer"
                      >
                        <FileText className="w-3.5 h-3.5" />
                        解说稿 (.md)
                      </button>
                    </div>
                  </div>

                  <div className="p-4 border border-slate-200 rounded-xl space-y-3">
                    <div className="flex items-center gap-2 text-sm font-semibold text-slate-900">
                      <Eye className="w-4 h-4 text-slate-500" />
                      当前播放分镜参数
                    </div>
                    {currentVideoSegment ? (
                      <dl className="grid grid-cols-2 gap-y-2.5 text-xs">
                        <dt className="text-slate-500">当前 Beat</dt>
                        <dd className="font-mono-tabular font-medium text-slate-900">
                          {currentVideoSegment.beat.beat_id}
                        </dd>
                        <dt className="text-slate-500">绑定分镜</dt>
                        <dd className="font-mono-tabular font-medium text-slate-900">
                          {currentVideoSegment.beat.target_panel_ids.join(', ')}
                        </dd>
                        <dt className="text-slate-500">FFmpeg 运镜</dt>
                        <dd className="font-mono-tabular font-medium text-blue-700">
                          {currentVideoSegment.beat.motion}
                        </dd>
                        <dt className="text-slate-500">TTS 情绪指令</dt>
                        <dd className="text-slate-800">
                          {currentVideoSegment.beat.emotion_instruction}
                        </dd>
                        <dt className="text-slate-500">实际送音文本</dt>
                        <dd className="text-slate-800">
                          {resolveSpokenText(currentVideoSegment.beat, rules).spokenText}
                        </dd>
                      </dl>
                    ) : (
                      <p className="text-xs text-slate-500">请先生成解说脚本</p>
                    )}
                  </div>

                  <div className="p-4 bg-slate-50 border border-slate-200 rounded-xl text-xs text-slate-600 leading-relaxed">
                    <strong className="text-slate-900 block mb-1">
                      9:16 竖屏渲染与导出说明：
                    </strong>
                    系统统一锁定 9:16（1080×1920）竖屏标准以完整呈现条漫构图。成品默认保存在{' '}
                    <code className="font-mono-tabular text-slate-800">07_out/*.mp4</code>，并支持浏览器一键下载 MP4、外挂 ASS 字幕或解说文稿。
                  </div>
                </div>
              </div>
            )}
          </>
        )}
      </section>

      {/* 分镜大图 + 字幕原文编辑（改完自动保存） */}
      <PanelZoomModal
        open={zoomBeatId !== null}
        beats={draftBeats}
        panels={episode.panels}
        activeBeatId={zoomBeatId}
        saveStatus={saveStatus}
        saveMessage={saveMessage}
        onClose={handleZoomClose}
        onSelectBeat={handleZoomSelectBeat}
        onChangeNarration={handleZoomNarrationChange}
      />
    </div>
  );
};
