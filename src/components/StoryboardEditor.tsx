import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Film,
  FileText,
  Loader2,
  Lock,
  Merge,
  Plus,
  RotateCcw,
  Save,
  Scissors,
  X,
} from 'lucide-react';
import type {
  EpisodeItem,
  EpisodeStoryboard,
  StoryboardGenerateResponse,
  StoryboardScene,
} from '../types/studio';

interface StoryboardEditorProps {
  episode: EpisodeItem;
  /** 后台任务已提交（S4 重生成或 S4→S7 一键制片）。 */
  onJobSubmitted: (job: StoryboardGenerateResponse['job']) => void;
  /** 切换到「解说与多音字审核」Tab 查看任务进度。 */
  onSwitchToScriptTab: () => void;
  /** 首屏镜头组数加载完成后回传，用于父组件 Tab 标题显示计数。 */
  onScenesLoaded?: (count: number) => void;
}

type BusyKind = '' | 'loading' | 'saving' | 'reset' | 'phase1' | 'full';

/** 拆分 / 合并 / 移动后重排 scene_index 与 scene_id，保持其他编辑字段不丢。 */
function renumber(scenes: StoryboardScene[]): StoryboardScene[] {
  // 与后端 s3b_scenes 约定对齐：scene_index 为 1-based，scene_id 前缀 sc_。
  return scenes.map((scene, index) => ({
    ...scene,
    scene_index: index + 1,
    scene_id: `sc_${String(index + 1).padStart(3, '0')}`,
  }));
}

/** 在 sceneIdx 幕的第 panelLocalIdx 格之前拆出新幕（该格成为新幕首格）。 */
function splitSceneAt(
  scenes: StoryboardScene[],
  sceneIdx: number,
  panelLocalIdx: number
): StoryboardScene[] {
  const target = scenes[sceneIdx];
  if (!target || panelLocalIdx <= 0 || panelLocalIdx >= target.panel_ids.length) {
    return scenes;
  }
  const leftIds = target.panel_ids.slice(0, panelLocalIdx);
  const rightIds = target.panel_ids.slice(panelLocalIdx);
  const next = [...scenes];
  next[sceneIdx] = { ...target, panel_ids: leftIds };
  next.splice(sceneIdx + 1, 0, {
    scene_id: '',
    scene_index: 0, // 占位，renumber 会按 1-based 重排
    title: '',
    location: target.location,
    panel_ids: rightIds,
    enabled: true,
    must_cover: true,
    user_note: '',
    locked_quotes: [],
    visual_recap: '',
  });
  return renumber(next);
}

/** 把 sceneIdx 幕整体并入上一幕。 */
function mergeIntoPrevious(scenes: StoryboardScene[], sceneIdx: number): StoryboardScene[] {
  if (sceneIdx <= 0) return scenes;
  const prev = scenes[sceneIdx - 1];
  const target = scenes[sceneIdx];
  const merged: StoryboardScene = {
    ...prev,
    panel_ids: [...prev.panel_ids, ...target.panel_ids],
    // 合并后必保台词取并集，避免人工锁定被静默丢弃。
    locked_quotes: Array.from(new Set([...prev.locked_quotes, ...target.locked_quotes])),
    user_note: [prev.user_note, target.user_note].filter(Boolean).join('\n'),
  };
  const next = [...scenes];
  next[sceneIdx - 1] = merged;
  next.splice(sceneIdx, 1);
  return renumber(next);
}

/** 把某格移到上一幕（作为上一幕末格）或下一幕（作为下一幕首格）。 */
function movePanel(
  scenes: StoryboardScene[],
  sceneIdx: number,
  panelId: string,
  dir: 'prev' | 'next'
): StoryboardScene[] {
  const target = scenes[sceneIdx];
  const neighbourIdx = dir === 'prev' ? sceneIdx - 1 : sceneIdx + 1;
  if (!target || !scenes[neighbourIdx]) return scenes;
  if (!target.panel_ids.includes(panelId)) return scenes;
  const next = scenes.map((scene) => ({ ...scene, panel_ids: [...scene.panel_ids] }));
  next[sceneIdx].panel_ids = next[sceneIdx].panel_ids.filter((id) => id !== panelId);
  if (dir === 'prev') {
    next[neighbourIdx].panel_ids.push(panelId);
  } else {
    next[neighbourIdx].panel_ids.unshift(panelId);
  }
  // 源幕被搬空时删除空幕，避免出现零分镜镜头组。
  const compact = next.filter((scene) => scene.panel_ids.length > 0);
  return renumber(compact);
}

export const StoryboardEditor: React.FC<StoryboardEditorProps> = ({
  episode,
  onJobSubmitted,
  onSwitchToScriptTab,
  onScenesLoaded,
}) => {
  const [data, setData] = useState<EpisodeStoryboard | null>(null);
  const [scenes, setScenes] = useState<StoryboardScene[]>([]);
  const [busy, setBusy] = useState<BusyKind>('loading');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [confirmReset, setConfirmReset] = useState(false);
  const [openRecap, setOpenRecap] = useState<Set<number>>(new Set());
  const [quoteDraft, setQuoteDraft] = useState<Record<string, string>>({});

  const basePath = `/api/episodes/${encodeURIComponent(episode.episode_id)}/storyboard`;

  const request = useCallback(
    async (suffix: string, init?: RequestInit): Promise<EpisodeStoryboard | StoryboardGenerateResponse> => {
      const res = await fetch(`${basePath}${suffix}`, {
        headers: { 'Content-Type': 'application/json' },
        ...init,
      });
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const body = (await res.json()) as { detail?: string };
          if (body?.detail) detail = body.detail;
        } catch {
          // 非 JSON 错误响应时保留 HTTP 状态码。
        }
        throw new Error(detail);
      }
      return (await res.json()) as EpisodeStoryboard | StoryboardGenerateResponse;
    },
    [basePath]
  );

  const load = useCallback(async () => {
    setBusy('loading');
    setError('');
    try {
      const payload = (await request('')) as EpisodeStoryboard;
      setData(payload);
      setScenes(payload.scenes);
      onScenesLoaded?.(payload.scenes.length);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy('');
    }
  }, [request, onScenesLoaded]);

  useEffect(() => {
    void load();
    // 切换话次时重新拉取；onScenesLoaded 引用稳定即可。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [episode.episode_id]);

  const dirty = useMemo(() => {
    if (!data) return false;
    return JSON.stringify(data.scenes) !== JSON.stringify(scenes);
  }, [data, scenes]);

  const totalPanels = useMemo(
    () => scenes.reduce((sum, scene) => sum + scene.panel_ids.length, 0),
    [scenes]
  );

  const patchScene = useCallback((sceneId: string, patch: Partial<StoryboardScene>) => {
    setScenes((prev) => prev.map((scene) => (scene.scene_id === sceneId ? { ...scene, ...patch } : scene)));
  }, []);

  const toggleQuote = useCallback((sceneId: string, quote: string) => {
    const text = quote.trim();
    if (!text) return;
    setScenes((prev) =>
      prev.map((scene) => {
        if (scene.scene_id !== sceneId) return scene;
        const locked = scene.locked_quotes.includes(text)
          ? scene.locked_quotes.filter((q) => q !== text)
          : [...scene.locked_quotes, text];
        return { ...scene, locked_quotes: locked };
      })
    );
  }, []);

  const addManualQuote = useCallback((sceneId: string) => {
    const text = (quoteDraft[sceneId] ?? '').trim();
    if (!text) return;
    setScenes((prev) =>
      prev.map((scene) =>
        scene.scene_id === sceneId && !scene.locked_quotes.includes(text)
          ? { ...scene, locked_quotes: [...scene.locked_quotes, text] }
          : scene
      )
    );
    setQuoteDraft((prev) => ({ ...prev, [sceneId]: '' }));
  }, [quoteDraft]);

  const handleSave = useCallback(async () => {
    setBusy('saving');
    setError('');
    setNotice('');
    try {
      const payload = (await request('', {
        method: 'PUT',
        body: JSON.stringify({ scenes }),
      })) as EpisodeStoryboard;
      setData(payload);
      setScenes(payload.scenes);
      setNotice('镜头组已保存为人工编排（source = human），后续自动重建不会覆盖。');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy('');
    }
  }, [request, scenes]);

  const handleReset = useCallback(async () => {
    if (!confirmReset) {
      setConfirmReset(true);
      return;
    }
    setConfirmReset(false);
    setBusy('reset');
    setError('');
    setNotice('');
    try {
      const payload = (await request('/reset', { method: 'POST' })) as EpisodeStoryboard;
      setData(payload);
      setScenes(payload.scenes);
      onScenesLoaded?.(payload.scenes.length);
      setNotice('已按视觉抽取结果重新自动分组（source = auto）。');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy('');
    }
  }, [confirmReset, request, onScenesLoaded]);

  const handleGenerate = useCallback(
    async (mode: 'phase1_only' | 'full_video') => {
      setBusy(mode === 'phase1_only' ? 'phase1' : 'full');
      setError('');
      setNotice('');
      try {
        // 有未保存的人工编排先落盘，保证后台任务读到的就是当前版本。
        if (dirty) {
          const saved = (await request('', {
            method: 'PUT',
            body: JSON.stringify({ scenes }),
          })) as EpisodeStoryboard;
          setData(saved);
          setScenes(saved.scenes);
        }
        const payload = (await request('/generate', {
          method: 'POST',
          body: JSON.stringify({ mode }),
        })) as StoryboardGenerateResponse;
        setData(payload.storyboard);
        setScenes(payload.storyboard.scenes);
        onScenesLoaded?.(payload.storyboard.scenes.length);
        setNotice(
          mode === 'phase1_only'
            ? `后台任务已提交（job_id=${payload.job.job_id}），正在按镜头组重生成 Stage 4 剧本。`
            : `后台任务已提交（job_id=${payload.job.job_id}），正在从 Stage 4 一路制片到 Stage 7。`
        );
        onJobSubmitted(payload.job);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy('');
      }
    },
    [dirty, request, scenes, onJobSubmitted, onScenesLoaded]
  );

  const toggleRecap = useCallback((sceneIndex: number) => {
    setOpenRecap((prev) => {
      const next = new Set(prev);
      if (next.has(sceneIndex)) next.delete(sceneIndex);
      else next.add(sceneIndex);
      return next;
    });
  }, []);

  if (busy === 'loading') {
    return (
      <div className="p-12 flex items-center justify-center text-slate-500 text-sm">
        <Loader2 className="w-4 h-4 animate-spin mr-2" />
        正在加载镜头编排…
      </div>
    );
  }

  return (
    <div className="p-5 space-y-4">
      {/* 顶部操作栏 */}
      <div className="flex flex-wrap items-center justify-between gap-3 p-4 bg-white border border-slate-200 rounded-xl">
        <div className="flex items-center gap-2.5 text-xs">
          <span
            className={`inline-flex items-center px-2 py-0.5 rounded-full font-medium ${
              data?.source === 'human'
                ? 'bg-amber-100 text-amber-800'
                : 'bg-blue-50 text-blue-700'
            }`}
          >
            {data?.source === 'human' ? '人工编排' : '自动分组'}
          </span>
          <span className="text-slate-600">
            {scenes.length} 幕 · {totalPanels} 格
          </span>
          {dirty && <span className="text-amber-600 font-medium">有未保存的改动</span>}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={handleReset}
            disabled={busy !== ''}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-slate-700 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 disabled:opacity-50 cursor-pointer disabled:cursor-not-allowed"
          >
            {busy === 'reset' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RotateCcw className="w-3.5 h-3.5" />}
            {confirmReset ? '确认恢复？未保存编排将丢失' : '恢复自动分组'}
          </button>
          {confirmReset && (
            <button
              type="button"
              onClick={() => setConfirmReset(false)}
              className="px-2.5 py-1.5 text-xs text-slate-500 hover:text-slate-800 cursor-pointer"
            >
              取消
            </button>
          )}
          <button
            type="button"
            onClick={handleSave}
            disabled={busy !== '' || !dirty}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50 cursor-pointer disabled:cursor-not-allowed"
          >
            {busy === 'saving' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
            保存镜头组
          </button>
          <button
            type="button"
            onClick={() => handleGenerate('phase1_only')}
            disabled={busy !== ''}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-slate-700 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 disabled:opacity-50 cursor-pointer disabled:cursor-not-allowed"
          >
            {busy === 'phase1' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <FileText className="w-3.5 h-3.5" />}
            按镜头组重生成剧本 (S4)
          </button>
          <button
            type="button"
            onClick={() => handleGenerate('full_video')}
            disabled={busy !== ''}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-slate-900 rounded-lg hover:bg-slate-800 disabled:opacity-50 cursor-pointer disabled:cursor-not-allowed"
          >
            {busy === 'full' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Film className="w-3.5 h-3.5" />}
            按镜头组一键制片 (S4→S7)
          </button>
        </div>
      </div>

      {error && (
        <div className="flex items-start gap-2 p-3 text-xs text-red-700 bg-red-50 border border-red-200 rounded-lg">
          <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
          <div className="flex-1">
            <p className="font-medium">操作失败：{error}</p>
            <button type="button" onClick={() => void load()} className="mt-1 underline cursor-pointer">
              重新加载
            </button>
          </div>
        </div>
      )}
      {notice && !error && (
        <div className="flex items-start gap-2 p-3 text-xs text-emerald-800 bg-emerald-50 border border-emerald-200 rounded-lg">
          <CheckCircle2 className="w-4 h-4 shrink-0 mt-0.5" />
          <div className="flex-1">
            <p>{notice}</p>
            <button
              type="button"
              onClick={onSwitchToScriptTab}
              className="mt-1 font-medium underline cursor-pointer"
            >
              前往「解说与多音字审核」查看进度
            </button>
          </div>
        </div>
      )}

      {/* 场景卡片流 */}
      <div className="space-y-4">
        {scenes.map((scene, sceneIdx) => {
          const panelIndex = data?.panels_index ?? {};
          const recapOpen = openRecap.has(scene.scene_index);
          return (
            <section
              key={scene.scene_id}
              className={`bg-white border rounded-xl overflow-hidden ${
                scene.enabled ? 'border-slate-200' : 'border-slate-200 opacity-60'
              }`}
            >
              {/* 幕头部 */}
              <div className="px-4 py-3 border-b border-slate-100 flex flex-wrap items-center gap-3">
                <span className="inline-flex items-center justify-center min-w-[3.5rem] px-2 py-1 rounded-md bg-slate-900 text-white text-xs font-semibold">
                  第 {scene.scene_index} 幕
                </span>
                <input
                  value={scene.title}
                  onChange={(e) => patchScene(scene.scene_id, { title: e.target.value })}
                  placeholder="幕标题（如：病房决裂）"
                  className="flex-1 min-w-[10rem] px-2.5 py-1.5 text-sm font-medium border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500/30 focus:border-blue-500"
                />
                <input
                  value={scene.location}
                  onChange={(e) => patchScene(scene.scene_id, { location: e.target.value })}
                  placeholder="地点"
                  className="w-32 px-2.5 py-1.5 text-xs border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500/30 focus:border-blue-500"
                />
                <label className="inline-flex items-center gap-1.5 text-xs text-slate-600 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={scene.enabled}
                    onChange={(e) => patchScene(scene.scene_id, { enabled: e.target.checked })}
                    className="rounded border-slate-300"
                  />
                  参与讲述
                </label>
                <label className="inline-flex items-center gap-1.5 text-xs text-slate-600 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={scene.must_cover}
                    onChange={(e) => patchScene(scene.scene_id, { must_cover: e.target.checked })}
                    className="rounded border-slate-300"
                  />
                  必讲不可漏
                </label>
                {sceneIdx > 0 && (
                  <button
                    type="button"
                    onClick={() => setScenes((prev) => mergeIntoPrevious(prev, sceneIdx))}
                    className="inline-flex items-center gap-1 px-2.5 py-1.5 text-xs text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50 cursor-pointer"
                    title="把本幕所有分镜并入上一幕"
                  >
                    <Merge className="w-3.5 h-3.5" />
                    合并到上一幕
                  </button>
                )}
              </div>

              {/* 缩略图网格 */}
              <div className="p-4 flex gap-3 overflow-x-auto">
                {scene.panel_ids.map((panelId, localIdx) => {
                  const info = panelIndex[panelId];
                  return (
                    <div key={panelId} className="shrink-0 w-44 space-y-1.5">
                      <div className="relative group">
                        <img
                          src={info?.image_url ?? ''}
                          alt={panelId}
                          referrerPolicy="no-referrer"
                          className="w-44 h-56 object-cover rounded-lg border border-slate-200 bg-slate-100"
                        />
                        <span className="absolute top-1.5 left-1.5 px-1.5 py-0.5 rounded bg-slate-900/80 text-white text-[10px] font-mono-tabular">
                          {panelId}
                        </span>
                        <div className="absolute inset-x-1.5 bottom-1.5 flex flex-wrap gap-1">
                          {localIdx === 0 && sceneIdx > 0 && (
                            <button
                              type="button"
                              onClick={() => setScenes((prev) => movePanel(prev, sceneIdx, panelId, 'prev'))}
                              className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded bg-white/95 text-slate-700 text-[10px] font-medium shadow-xs hover:bg-white cursor-pointer"
                              title="移到上一幕末尾"
                            >
                              <ArrowLeft className="w-3 h-3" />
                              上一幕
                            </button>
                          )}
                          {localIdx > 0 && (
                            <button
                              type="button"
                              onClick={() => setScenes((prev) => splitSceneAt(prev, sceneIdx, localIdx))}
                              className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded bg-blue-600/95 text-white text-[10px] font-medium shadow-xs hover:bg-blue-600 cursor-pointer"
                              title="从这一格之前拆出新幕"
                            >
                              <Scissors className="w-3 h-3" />
                              拆新幕
                            </button>
                          )}
                          {localIdx === scene.panel_ids.length - 1 && sceneIdx < scenes.length - 1 && (
                            <button
                              type="button"
                              onClick={() => setScenes((prev) => movePanel(prev, sceneIdx, panelId, 'next'))}
                              className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded bg-white/95 text-slate-700 text-[10px] font-medium shadow-xs hover:bg-white cursor-pointer"
                              title="移到下一幕开头"
                            >
                              下一幕
                              <ArrowRight className="w-3 h-3" />
                            </button>
                          )}
                        </div>
                      </div>
                      <p className="text-[11px] text-slate-500 line-clamp-2 leading-snug">
                        {info?.scene_summary || '（无画面描述）'}
                      </p>
                      {/* 气泡：点击加入 / 移出必保台词 */}
                      {info?.dialogues?.length ? (
                        <div className="space-y-1">
                          {info.dialogues.map((d, dIdx) => {
                            const locked = scene.locked_quotes.includes(d.text);
                            return (
                              <button
                                key={dIdx}
                                type="button"
                                onClick={() => toggleQuote(scene.scene_id, d.text)}
                                className={`w-full text-left px-2 py-1 rounded-md border text-[11px] leading-snug transition-colors cursor-pointer ${
                                  locked
                                    ? 'border-amber-300 bg-amber-50 text-amber-900'
                                    : 'border-slate-200 bg-slate-50 text-slate-600 hover:border-slate-300'
                                }`}
                                title={locked ? '点击移出必保台词' : '点击锁定为必保台词'}
                              >
                                <span className="flex items-start gap-1">
                                  {locked ? <Lock className="w-3 h-3 mt-0.5 shrink-0" /> : <Plus className="w-3 h-3 mt-0.5 shrink-0" />}
                                  <span>
                                    <span className="font-medium">{d.speaker_guess}：</span>
                                    {d.text}
                                  </span>
                                </span>
                              </button>
                            );
                          })}
                        </div>
                      ) : null}
                    </div>
                  );
                })}
              </div>

              {/* 必保台词 + 必讲要点 */}
              <div className="px-4 pb-4 grid grid-cols-1 lg:grid-cols-2 gap-4">
                <div>
                  <div className="text-xs font-medium text-slate-700 mb-1.5">必保台词（locked_quotes）</div>
                  <div className="flex flex-wrap gap-1.5 mb-2">
                    {scene.locked_quotes.map((quote) => (
                      <span
                        key={quote}
                        className="inline-flex items-center gap-1 pl-2 pr-1 py-0.5 rounded-full bg-amber-50 border border-amber-200 text-amber-900 text-[11px]"
                      >
                        {quote}
                        <button
                          type="button"
                          onClick={() => toggleQuote(scene.scene_id, quote)}
                          className="p-0.5 rounded-full hover:bg-amber-100 cursor-pointer"
                          title="移出必保台词"
                        >
                          <X className="w-3 h-3" />
                        </button>
                      </span>
                    ))}
                    {scene.locked_quotes.length === 0 && (
                      <span className="text-[11px] text-slate-400">暂无，点击上方气泡或手动输入</span>
                    )}
                  </div>
                  <input
                    value={quoteDraft[scene.scene_id] ?? ''}
                    onChange={(e) =>
                      setQuoteDraft((prev) => ({ ...prev, [scene.scene_id]: e.target.value }))
                    }
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        e.preventDefault();
                        addManualQuote(scene.scene_id);
                      }
                    }}
                    placeholder="手动输入必保台词，回车添加"
                    className="w-full px-2.5 py-1.5 text-xs border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500/30 focus:border-blue-500"
                  />
                </div>
                <div>
                  <div className="text-xs font-medium text-slate-700 mb-1.5">本幕必讲要点（user_note）</div>
                  <textarea
                    value={scene.user_note}
                    onChange={(e) => patchScene(scene.scene_id, { user_note: e.target.value })}
                    rows={3}
                    placeholder="纠正识图误差、补充因果关系或编导特别要求…"
                    className="w-full px-2.5 py-1.5 text-xs border border-slate-200 rounded-lg resize-y focus:outline-none focus:ring-2 focus:ring-blue-500/30 focus:border-blue-500"
                  />
                </div>
              </div>

              {/* 整幕视觉联看摘要（折叠） */}
              {scene.visual_recap && (
                <div className="px-4 pb-4">
                  <button
                    type="button"
                    onClick={() => toggleRecap(scene.scene_index)}
                    className="inline-flex items-center gap-1 text-xs font-medium text-slate-500 hover:text-slate-800 cursor-pointer"
                  >
                    {recapOpen ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
                    整幕视觉联看摘要（visual_recap）
                  </button>
                  {recapOpen && (
                    <p className="mt-2 p-3 bg-slate-50 border border-slate-100 rounded-lg text-xs text-slate-600 leading-relaxed whitespace-pre-wrap">
                      {scene.visual_recap}
                    </p>
                  )}
                </div>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
};
