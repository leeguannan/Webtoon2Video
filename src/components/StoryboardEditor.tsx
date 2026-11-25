import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Film,
  FileText,
  Loader2,
  Lock,
  Merge,
  Music,
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
  StoryboardPanelInfo,
  StoryboardScene,
} from '../types/studio';

/**
 * Patch v4.25：缩略图 URL 助手。
 *
 * 后端提供 `GET /api/episodes/{id}/panels/{filename}?w=<像素>` 缩略图接口（返回 JPEG），
 * 原图 URL 没有 `?` 用 `?w=`，有 `?` 用 `&w=`；已有 w 参数时直接替换。
 */
function panelThumbUrl(url: string, width: number): string {
  if (!url) return url;
  if (/[?&]w=\d+/.test(url)) return url.replace(/([?&])w=\d+/, `$1w=${width}`);
  return `${url}${url.includes('?') ? '&' : '?'}w=${width}`;
}

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
  // Patch v4.29：拆幕后左半幕的分镜组合已经变了，旧「画面复核」摘要必然过期，
  // 必须清空，否则 Stage 4 会拿着过期摘要去讲已经拆走的画面。
  next[sceneIdx] = { ...target, panel_ids: leftIds, visual_recap: '' };
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
    // 新幕不带配乐配置，等待 AI 建议或人工选择（后端按自动模式解析）。
    bgm_mood: '',
    bgm_suggestion: '',
    bgm: null,
    bgm_gain_db: null,
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
    // Patch v4.29：合并后分镜组合变了，画面复核摘要过期，必须清空。
    visual_recap: '',
  };
  const next = [...scenes];
  next[sceneIdx - 1] = merged;
  next.splice(sceneIdx, 1);
  return renumber(next);
}

/** 把某幕的第 localIdx 格移到上一幕（作为上一幕末格）或下一幕（作为下一幕首格）。 */
function movePanel(
  scenes: StoryboardScene[],
  sceneIdx: number,
  localIdx: number,
  dir: 'prev' | 'next'
): StoryboardScene[] {
  const target = scenes[sceneIdx];
  const neighbourIdx = dir === 'prev' ? sceneIdx - 1 : sceneIdx + 1;
  if (!target || !scenes[neighbourIdx]) return scenes;
  if (localIdx < 0 || localIdx >= target.panel_ids.length) return scenes;
  const panelId = target.panel_ids[localIdx];
  const next = scenes.map((scene) => ({ ...scene, panel_ids: [...scene.panel_ids] }));
  // 只搬走被点的那一格：同一分镜在本幕出现多次时（分句复用同一画面是合法的），
  // 其余副本必须留在原位，所以这里按索引 splice，不能按 id filter。
  next[sceneIdx].panel_ids.splice(localIdx, 1);
  if (dir === 'prev') {
    next[neighbourIdx].panel_ids.push(panelId);
  } else {
    next[neighbourIdx].panel_ids.unshift(panelId);
  }
  // Patch v4.29：源幕（被移出）与目标幕（被移入）的分镜组合都变了，
  // 两边的画面复核摘要都过期，必须清空。
  next[sceneIdx].visual_recap = '';
  next[neighbourIdx].visual_recap = '';
  // 源幕被搬空时删除空幕，避免出现零分镜镜头组。
  const compact = next.filter((scene) => scene.panel_ids.length > 0);
  return renumber(compact);
}

/** 分镜自然序键（与后端 core/panel_ids.py 的 panel_sort_key 对齐）。 */
function panelOrderKey(panelId: string): [string, number, string] {
  const match = /(\d+)$/.exec(panelId ?? '');
  if (match) {
    return [panelId.slice(0, match.index), Number(match[1]), panelId];
  }
  return [panelId, -1, panelId];
}

/** 比较两个自然序键（后端用 tuple 比较，这里逐项比较保持一致）。 */
function comparePanelOrder(a: [string, number, string], b: [string, number, string]): number {
  if (a[0] !== b[0]) return a[0] < b[0] ? -1 : 1;
  if (a[1] !== b[1]) return a[1] - b[1];
  if (a[2] !== b[2]) return a[2] < b[2] ? -1 : 1;
  return 0;
}

/** 分镜清单按自然序排列（p_2 排在 p_10 前面），选择面板用它排序。 */
function sortPanelIds(panelIds: string[]): string[] {
  return [...panelIds].sort((a, b) => comparePanelOrder(panelOrderKey(a), panelOrderKey(b)));
}

/**
 * 幕内相邻两格能否换序。
 *
 * 后端 `_validate_storyboard_scenes` 要求幕内分镜的 `panel_sort_key` 单调不减，
 * 而幕内本来就得按剧情时间序排列，所以只有「同一张分镜的两个副本」互换才不会
 * 破坏该约束（两者 sort key 相同）；不同分镜互换必然逆序，保存时会被后端 400
 * 拒绝（`第 X 幕的分镜顺序必须单调不减`）。据此隐藏会触发后端报错的换序按钮。
 */
function canSwapWithinScene(
  panelIds: string[],
  localIdx: number,
  dir: 'up' | 'down'
): boolean {
  const neighbourIdx = dir === 'up' ? localIdx - 1 : localIdx + 1;
  if (neighbourIdx < 0 || neighbourIdx >= panelIds.length) return false;
  return panelIds[localIdx] === panelIds[neighbourIdx];
}

/**
 * 在幕内把第 localIdx 格上移 / 下移。
 * 按索引换序而不是按 id 查找：同一分镜在本幕可能重复出现多次，
 * 按 id 换序会连带影响所有副本，与「只动被点的那一格」的语义冲突。
 */
function movePanelWithinScene(
  scenes: StoryboardScene[],
  sceneIdx: number,
  localIdx: number,
  dir: 'up' | 'down'
): StoryboardScene[] {
  const target = scenes[sceneIdx];
  if (!target) return scenes;
  const neighbourIdx = dir === 'up' ? localIdx - 1 : localIdx + 1;
  if (neighbourIdx < 0 || neighbourIdx >= target.panel_ids.length) return scenes;
  // 守卫：只有同一张分镜的副本之间才允许换序，否则会破坏幕内单调不减校验。
  if (!canSwapWithinScene(target.panel_ids, localIdx, dir)) return scenes;
  const next = scenes.map((scene, index) =>
    index === sceneIdx ? { ...scene, panel_ids: [...scene.panel_ids] } : scene
  );
  const ids = next[sceneIdx].panel_ids;
  const current = ids[localIdx];
  ids[localIdx] = ids[neighbourIdx];
  ids[neighbourIdx] = current;
  // Patch v4.29：幕内换序同样改变了 panel_ids 序列（后端比较的是列表），
  // 画面复核摘要按同一规则失效，前后端口径一致。
  next[sceneIdx].visual_recap = '';
  // 幕内换序不改变幕的数量与先后，scene_index / scene_id 保持不变。
  return next;
}

/**
 * 往幕里加一格分镜：允许与本幕已有分镜重复（同一张漫画被多句解说复用）。
 * 插入位置取自然序有序的位置，这样常态下不会破坏幕内「顺序单调不减」的约定。
 */
function addPanelToScene(
  scenes: StoryboardScene[],
  sceneIdx: number,
  panelId: string
): StoryboardScene[] {
  const target = scenes[sceneIdx];
  if (!target || !panelId) return scenes;
  const key = panelOrderKey(panelId);
  const ids = [...target.panel_ids];
  let insertAt = ids.length;
  for (let index = 0; index < ids.length; index += 1) {
    if (comparePanelOrder(panelOrderKey(ids[index]), key) > 0) {
      insertAt = index;
      break;
    }
  }
  ids.splice(insertAt, 0, panelId);
  return scenes.map((scene, index) =>
    // Patch v4.29：加格后分镜组合变了，画面复核摘要过期，必须清空。
    index === sceneIdx ? { ...scene, panel_ids: ids, visual_recap: '' } : scene
  );
}

/** 从幕里移除第 localIdx 格：同一分镜出现多次时只移除被点的那一个。 */
function removePanelFromScene(
  scenes: StoryboardScene[],
  sceneIdx: number,
  localIdx: number
): StoryboardScene[] {
  const target = scenes[sceneIdx];
  // 硬约束：每幕至少保留 1 个分镜，禁止把某一幕清空。
  if (!target || target.panel_ids.length <= 1) return scenes;
  if (localIdx < 0 || localIdx >= target.panel_ids.length) return scenes;
  const ids = target.panel_ids.filter((_, index) => index !== localIdx);
  return scenes.map((scene, index) =>
    // Patch v4.29：删格后分镜组合变了，画面复核摘要过期，必须清空。
    index === sceneIdx ? { ...scene, panel_ids: ids, visual_recap: '' } : scene
  );
}

/**
 * 镜像后端 _validate_storyboard_scenes 的三条顺序校验，给出逐幕的行内提示。
 * 只提示不阻断：后端才是最终裁决方，规则若被放宽这里也不会把人卡住。
 */
function findSceneOrderIssues(scenes: StoryboardScene[]): Map<string, string[]> {
  const issues = new Map<string, string[]>();
  const push = (sceneId: string, text: string) => {
    const list = issues.get(sceneId) ?? [];
    if (!list.includes(text)) list.push(text);
    issues.set(sceneId, list);
  };
  // 同一分镜出现在多个幕：后端不允许跨幕重复引用。
  const owners = new Map<string, number[]>();
  scenes.forEach((scene, sceneIdx) => {
    scene.panel_ids.forEach((panelId) => {
      const list = owners.get(panelId) ?? [];
      if (!list.includes(sceneIdx)) list.push(sceneIdx);
      owners.set(panelId, list);
    });
  });
  owners.forEach((sceneIdxs, panelId) => {
    if (sceneIdxs.length < 2) return;
    const names = sceneIdxs.map((index) => `第 ${index + 1} 幕`).join('、');
    sceneIdxs.forEach((index) => {
      push(scenes[index].scene_id, `分镜 ${panelId} 同时出现在${names}，后端不允许跨幕重复引用`);
    });
  });
  let previousLastKey: [string, number, string] | null = null;
  scenes.forEach((scene) => {
    const keys = scene.panel_ids.map(panelOrderKey);
    keys.forEach((key, index) => {
      if (index > 0 && comparePanelOrder(keys[index - 1], key) > 0) {
        push(scene.scene_id, '幕内分镜顺序必须单调不减（与后端校验一致），保存时可能被拒绝');
      }
    });
    if (previousLastKey !== null && keys.length > 0 && comparePanelOrder(keys[0], previousLastKey) <= 0) {
      push(scene.scene_id, '与上一幕的分镜顺序交叉，后端要求各幕按剧情时间先后排列');
    }
    if (keys.length > 0) previousLastKey = keys[keys.length - 1];
  });
  return issues;
}

/**
 * Patch v4.25：单幕卡片抽成 memo 子组件。
 *
 * 一话 200~300 个分镜时，幕卡片里塞满了缩略图；只要任意一格变了就整片重画太浪费。
 * 这里把卡片隔离成独立组件 + content-visibility，浏览器会跳过屏外卡片的渲染。
 */
interface SceneCardProps {
  scene: StoryboardScene;
  sceneIdx: number;
  sceneCount: number;
  panelIndex: Record<string, StoryboardPanelInfo>;
  recapOpen: boolean;
  issues: string[];
  pickerOpen: boolean;
  allPanelIds: string[];
  panelOwners: Map<string, number[]>;
  quoteDraft: string;
  patchScene: (sceneId: string, patch: Partial<StoryboardScene>) => void;
  togglePicker: (sceneId: string) => void;
  setPickerSceneId: (sceneId: string | null) => void;
  setScenes: React.Dispatch<React.SetStateAction<StoryboardScene[]>>;
  handleRemovePanel: (sceneId: string, localIdx: number) => void;
  handleRemovePanelAll: (sceneId: string, panelId: string) => void;
  handleAddPanel: (sceneId: string, panelId: string) => void;
  handleMoveWithinScene: (sceneId: string, localIdx: number, dir: 'up' | 'down') => void;
  toggleQuote: (sceneId: string, quote: string) => void;
  addManualQuote: (sceneId: string) => void;
  setQuoteDraft: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  toggleRecap: (sceneIndex: number) => void;
}

const SceneCard = React.memo(function SceneCard({
  scene,
  sceneIdx,
  sceneCount,
  panelIndex,
  recapOpen,
  issues,
  pickerOpen,
  allPanelIds,
  panelOwners,
  quoteDraft,
  patchScene,
  togglePicker,
  setPickerSceneId,
  setScenes,
  handleRemovePanel,
  handleRemovePanelAll,
  handleAddPanel,
  handleMoveWithinScene,
  toggleQuote,
  addManualQuote,
  setQuoteDraft,
  toggleRecap,
}: SceneCardProps) {
  return (
    <section
      className={`bg-white border rounded-xl overflow-hidden [content-visibility:auto] [contain-intrinsic-size:auto_600px] ${
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
        <button
          type="button"
          onClick={() => togglePicker(scene.scene_id)}
          className="inline-flex items-center gap-1 px-2.5 py-1.5 text-xs text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50 cursor-pointer"
          title="从本话全部分镜中勾选，添加到本幕"
        >
          {pickerOpen ? (
            <ChevronDown className="w-3.5 h-3.5" />
          ) : (
            <Plus className="w-3.5 h-3.5" />
          )}
          添加分镜
        </button>
        {sceneIdx > 0 && (
          <button
            type="button"
            onClick={() => {
              // 合幕会改变 scene_id 序列，先收起勾选面板避免指向错幕。
              setPickerSceneId(null);
              setScenes((prev) => mergeIntoPrevious(prev, sceneIdx));
            }}
            className="inline-flex items-center gap-1 px-2.5 py-1.5 text-xs text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50 cursor-pointer"
            title="把本幕所有分镜并入上一幕"
          >
            <Merge className="w-3.5 h-3.5" />
            合并到上一幕
          </button>
        )}
        {(scene.bgm_mood || scene.bgm_suggestion) && (
          <span
            className="inline-flex items-center gap-1.5 rounded-full border border-violet-200 bg-violet-50 px-2.5 py-1 text-[11px] text-violet-700"
            title="AI 配乐建议（在解说审核页可为本幕选择具体曲目）"
          >
            <Music className="h-3.5 w-3.5" />
            {scene.bgm_mood && <span className="font-medium">{scene.bgm_mood}</span>}
            {scene.bgm_suggestion && (
              <span className="max-w-[22rem] truncate">{scene.bgm_suggestion}</span>
            )}
          </span>
        )}
      </div>

      {/* 幕内换序规则提示：不同分镜不可互换（后端要求幕内单调不减） */}
      {scene.panel_ids.length > 1 && (
        <p className="px-4 pt-3 text-[11px] text-slate-500">
          同一幕内按剧情时间序排列；跨句复用同一格请通过「添加分镜」插入。
        </p>
      )}

      {/* 缩略图网格 */}
      <div className="p-4 flex gap-3 overflow-x-auto">
        {scene.panel_ids.map((panelId, localIdx) => {
          const info = panelIndex[panelId];
          const isLastPanel = scene.panel_ids.length <= 1;
          return (
            // 同一分镜可以在本幕出现多次（多句解说复用同一画面），
            // 所以 key 必须带索引，否则 React 会报重复 key。
            <div key={`${panelId}-${localIdx}`} className="shrink-0 w-44 space-y-1.5">
              <div className="relative group">
                <img
                  src={panelThumbUrl(info?.image_url ?? '', 360)}
                  alt={panelId}
                  referrerPolicy="no-referrer"
                  loading="lazy"
                  decoding="async"
                  className="w-44 h-56 object-cover rounded-lg border border-slate-200 bg-slate-100"
                />
                <span className="absolute top-1.5 left-1.5 px-1.5 py-0.5 rounded bg-slate-900/80 text-white text-[10px] font-mono-tabular">
                  {panelId}
                </span>
                {/* 移除本幕的这一格：只剩 1 格时禁用，避免把幕清空 */}
                <button
                  type="button"
                  onClick={() => handleRemovePanel(scene.scene_id, localIdx)}
                  disabled={isLastPanel}
                  title={isLastPanel ? '每幕至少保留 1 个分镜' : '从本幕移除这一格'}
                  className="absolute top-1.5 right-1.5 inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded bg-white/95 text-red-600 text-[10px] font-medium shadow-xs hover:bg-red-50 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <X className="w-3 h-3" />
                  移除
                </button>
                <div className="absolute inset-x-1.5 bottom-1.5 flex flex-wrap gap-1">
                  {localIdx === 0 && sceneIdx > 0 && (
                    <button
                      type="button"
                      onClick={() => {
                        setPickerSceneId(null);
                        setScenes((prev) => movePanel(prev, sceneIdx, localIdx, 'prev'));
                      }}
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
                      onClick={() => {
                        // 拆幕会重排 scene_id，先收起勾选面板避免指向错幕。
                        setPickerSceneId(null);
                        setScenes((prev) => splitSceneAt(prev, sceneIdx, localIdx));
                      }}
                      className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded bg-blue-600/95 text-white text-[10px] font-medium shadow-xs hover:bg-blue-600 cursor-pointer"
                      title="从这一格之前拆出新幕"
                    >
                      <Scissors className="w-3 h-3" />
                      拆新幕
                    </button>
                  )}
                  {localIdx === scene.panel_ids.length - 1 && sceneIdx < sceneCount - 1 && (
                    <button
                      type="button"
                      onClick={() => {
                        setPickerSceneId(null);
                        setScenes((prev) => movePanel(prev, sceneIdx, localIdx, 'next'));
                      }}
                      className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded bg-white/95 text-slate-700 text-[10px] font-medium shadow-xs hover:bg-white cursor-pointer"
                      title="移到下一幕开头"
                    >
                      下一幕
                      <ArrowRight className="w-3 h-3" />
                    </button>
                  )}
                  {/* 幕内换序：仅同一分镜的两个副本之间可换（不同分镜互换会破坏后端单调序校验） */}
                  {localIdx > 0 && canSwapWithinScene(scene.panel_ids, localIdx, 'up') && (
                    <button
                      type="button"
                      onClick={() => handleMoveWithinScene(scene.scene_id, localIdx, 'up')}
                      className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded bg-white/95 text-slate-700 text-[10px] font-medium shadow-xs hover:bg-white cursor-pointer"
                      title="在本幕内上移一格（同一分镜的副本，交换不影响后端顺序校验）"
                    >
                      <ArrowUp className="w-3 h-3" />
                      上移
                    </button>
                  )}
                  {localIdx < scene.panel_ids.length - 1 &&
                    canSwapWithinScene(scene.panel_ids, localIdx, 'down') && (
                      <button
                        type="button"
                        onClick={() => handleMoveWithinScene(scene.scene_id, localIdx, 'down')}
                        className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded bg-white/95 text-slate-700 text-[10px] font-medium shadow-xs hover:bg-white cursor-pointer"
                        title="在本幕内下移一格（同一分镜的副本，交换不影响后端顺序校验）"
                      >
                        下移
                        <ArrowDown className="w-3 h-3" />
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

      {/* 行内提示：本幕只剩 1 格（硬约束） + 与后端校验冲突的顺序/重复提示 */}
      {(scene.panel_ids.length <= 1 || issues.length > 0) && (
        <div className="px-4 pb-3 space-y-2">
          {scene.panel_ids.length <= 1 && (
            <p className="flex items-start gap-1.5 px-2.5 py-1.5 rounded-lg bg-amber-50 border border-amber-200 text-amber-900 text-[11px]">
              <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
              本幕只有 1 个分镜，每幕至少保留 1 个分镜，无法再移除。
            </p>
          )}
          {issues.map((issue) => (
            <p
              key={issue}
              className="flex items-start gap-1.5 px-2.5 py-1.5 rounded-lg bg-amber-50 border border-amber-200 text-amber-900 text-[11px]"
            >
              <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
              {issue}
            </p>
          ))}
        </div>
      )}

      {/* 幕内勾选添加分镜：同一格可重复加入，供多句解说复用同一画面 */}
      {pickerOpen && (
        <div className="px-4 pb-4">
          <div className="flex items-center justify-between gap-2 mb-2">
            <div className="text-xs font-medium text-slate-700">
              选择分镜加入本幕（同一格可重复加入，供多句解说复用同一画面）
            </div>
            <button
              type="button"
              onClick={() => setPickerSceneId(null)}
              className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px] text-slate-500 hover:text-slate-800 hover:bg-slate-100 cursor-pointer"
              title="收起选择面板"
            >
              <ChevronDown className="w-3.5 h-3.5" />
              收起
            </button>
          </div>
          {allPanelIds.length === 0 ? (
            <p className="text-[11px] text-slate-400">本话没有可用分镜（缺少 Stage 3 视觉结果）。</p>
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2 max-h-72 overflow-y-auto pr-1">
              {allPanelIds.map((panelId) => {
                const info = panelIndex[panelId];
                const inSceneCount = scene.panel_ids.filter((pid) => pid === panelId).length;
                // 其它幕已经用过的分镜不能再引到本幕：后端会判「跨幕重复引用」并 400。
                const otherSceneIdxs = (panelOwners.get(panelId) ?? []).filter((idx) => idx !== sceneIdx);
                const crossSceneUsed = otherSceneIdxs.length > 0;
                return (
                  <div
                    key={panelId}
                    className={`flex gap-2 p-1.5 rounded-lg border ${
                      crossSceneUsed
                        ? 'border-slate-200 bg-slate-50'
                        : inSceneCount > 0
                          ? 'border-blue-300 bg-blue-50'
                          : 'border-slate-200 bg-white'
                    }`}
                  >
                    <img
                      src={panelThumbUrl(info?.image_url ?? '', 140)}
                      alt={panelId}
                      referrerPolicy="no-referrer"
                      loading="lazy"
                      decoding="async"
                      className="w-10 h-14 object-cover rounded bg-slate-100 shrink-0"
                    />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5">
                        <input
                          type="checkbox"
                          checked={inSceneCount > 0}
                          disabled={crossSceneUsed && inSceneCount === 0}
                          onChange={() => {
                            if (inSceneCount > 0) handleRemovePanelAll(scene.scene_id, panelId);
                            else handleAddPanel(scene.scene_id, panelId);
                          }}
                          title={crossSceneUsed ? '该分镜已在其它幕使用，不能跨幕重复引用' : '加入/移出本幕'}
                          className="rounded border-slate-300 cursor-pointer disabled:cursor-not-allowed"
                        />
                        <span className="text-[10px] font-mono-tabular text-slate-600 truncate">{panelId}</span>
                      </div>
                      {crossSceneUsed ? (
                        <p className="mt-0.5 text-[10px] leading-tight text-slate-500">
                          已在第 {otherSceneIdxs.map((idx) => idx + 1).join('、')} 幕使用
                          {inSceneCount > 0 ? `（本幕也有 ${inSceneCount} 格）` : ''}
                        </p>
                      ) : inSceneCount > 0 ? (
                        <p className="mt-0.5 text-[10px] leading-tight text-blue-700">
                          本幕已有 {inSceneCount} 格
                        </p>
                      ) : (
                        <p className="mt-0.5 text-[10px] leading-tight text-slate-400">未使用</p>
                      )}
                      {inSceneCount > 0 && (
                        <button
                          type="button"
                          onClick={() => handleAddPanel(scene.scene_id, panelId)}
                          className="mt-1 text-[10px] font-medium text-blue-700 underline cursor-pointer"
                          title="同一张漫画再加入一格，供另一句解说复用"
                        >
                          再加一格
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          <p className="mt-2 text-[11px] text-slate-500">
            勾选即把该分镜加入本幕；已在本幕的分镜可以重复加入（取消勾选会移除本幕里的该分镜）。
            已在其它幕使用的分镜会置灰，因为同一分镜不能跨幕重复引用。
          </p>
        </div>
      )}

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
            value={quoteDraft}
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
});

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
  const [jobNotice, setJobNotice] = useState('');
  const [openRecap, setOpenRecap] = useState<Set<number>>(new Set());
  const [quoteDraft, setQuoteDraft] = useState<Record<string, string>>({});
  // 展开「幕内勾选添加分镜」面板的幕（scene_id）；拆幕/合幕会改变 scene_id，届时关闭面板。
  const [pickerSceneId, setPickerSceneId] = useState<string | null>(null);

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

  // Patch v4.0 Patch 20.1：二次确认 3 秒后自动复位。
  // 否则「恢复自动分组」按钮会一直停在「确认恢复？」状态，
  // 用户忘记点取消就只能一直看着危险提示。
  useEffect(() => {
    if (!confirmReset) return;
    const timer = window.setTimeout(() => setConfirmReset(false), 3000);
    return () => window.clearTimeout(timer);
  }, [confirmReset]);

  const dirty = useMemo(() => {
    if (!data) return false;
    return JSON.stringify(data.scenes) !== JSON.stringify(scenes);
  }, [data, scenes]);

  const totalPanels = useMemo(
    () => scenes.reduce((sum, scene) => sum + scene.panel_ids.length, 0),
    [scenes]
  );

  /**
   * 本话全部分镜清单：后端 GET /storyboard 下发的 panels_index 覆盖本话全部视觉分镜，
   * 是「幕外可选分镜」的唯一数据源（totalPanels 只是幕内格数统计，不是清单）。
   */
  const allPanelIds = useMemo(
    () => sortPanelIds(Object.keys(data?.panels_index ?? {})),
    [data]
  );

  /** 每个分镜当前落在哪些幕（选择面板用它标注「本幕已有 / 第 N 幕已有」）。 */
  const panelOwners = useMemo(() => {
    const map = new Map<string, number[]>();
    scenes.forEach((scene, sceneIdx) => {
      scene.panel_ids.forEach((panelId) => {
        const list = map.get(panelId) ?? [];
        if (!list.includes(sceneIdx)) list.push(sceneIdx);
        map.set(panelId, list);
      });
    });
    return map;
  }, [scenes]);

  /** 面板索引（幕卡片要共享同一份引用，否则 memo 会失效）。 */
  const panelIndex = useMemo(() => data?.panels_index ?? {}, [data]);

  /** 逐幕的顺序/重复引用提示（镜像后端校验，只提示不阻断保存）。 */
  const sceneIssues = useMemo(() => findSceneOrderIssues(scenes), [scenes]);

  const patchScene = useCallback((sceneId: string, patch: Partial<StoryboardScene>) => {
    setScenes((prev) => prev.map((scene) => (scene.scene_id === sceneId ? { ...scene, ...patch } : scene)));
  }, []);

  /** 展开/收起某幕的「添加分镜」勾选面板。 */
  const togglePicker = useCallback((sceneId: string) => {
    setPickerSceneId((prev) => (prev === sceneId ? null : sceneId));
  }, []);

  /** 往幕里加一格分镜（允许重复）。 */
  const handleAddPanel = useCallback((sceneId: string, panelId: string) => {
    setScenes((prev) => {
      const sceneIdx = prev.findIndex((scene) => scene.scene_id === sceneId);
      if (sceneIdx < 0) return prev;
      return addPanelToScene(prev, sceneIdx, panelId);
    });
  }, []);

  /** 从幕里移除指定那一格（幕内只剩 1 格时后端会拒收，这里直接拒绝）。 */
  const handleRemovePanel = useCallback((sceneId: string, localIdx: number) => {
    setScenes((prev) => {
      const sceneIdx = prev.findIndex((scene) => scene.scene_id === sceneId);
      if (sceneIdx < 0) return prev;
      return removePanelFromScene(prev, sceneIdx, localIdx);
    });
  }, []);

  /**
   * 取消勾选：把某个分镜从本幕整体移除（含重复副本）。
   * 与缩略图上的「移除」不同——后者只删被点的那一格，这里是勾选框的「是否在本幕」语义。
   */
  const handleRemovePanelAll = useCallback((sceneId: string, panelId: string) => {
    setScenes((prev) => {
      const sceneIdx = prev.findIndex((scene) => scene.scene_id === sceneId);
      if (sceneIdx < 0) return prev;
      const target = prev[sceneIdx];
      const ids = target.panel_ids.filter((pid) => pid !== panelId);
      // 移除后为空说明这一格就是本幕唯一分镜，禁止清空某一幕。
      if (ids.length === 0 || ids.length === target.panel_ids.length) return prev;
      // Patch v4.29：整格移除后分镜组合变了，画面复核摘要过期，必须清空。
      return prev.map((scene, index) =>
        index === sceneIdx ? { ...scene, panel_ids: ids, visual_recap: '' } : scene
      );
    });
  }, []);

  /** 幕内上移 / 下移一格。 */
  const handleMoveWithinScene = useCallback(
    (sceneId: string, localIdx: number, dir: 'up' | 'down') => {
      setScenes((prev) => {
        const sceneIdx = prev.findIndex((scene) => scene.scene_id === sceneId);
        if (sceneIdx < 0) return prev;
        return movePanelWithinScene(prev, sceneIdx, localIdx, dir);
      });
    },
    []
  );

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
      setJobNotice('');
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
        // 只保留一条任务通知（jobNotice 横幅），避免同时渲染两条绿色提示。
        setJobNotice(
          mode === 'phase1_only'
            ? `镜头组剧本重生成任务已提交（job_id=${payload.job.job_id}）。`
            : `镜头组一键制片任务已提交（job_id=${payload.job.job_id}）。`
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
            {episode.beats.length === 0
              ? '按镜头组生成剧本 (S4)'
              : '按镜头组重生成剧本 (S4)'}
          </button>
          <button
            type="button"
            onClick={() => handleGenerate('full_video')}
            disabled={busy !== ''}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-slate-900 rounded-lg hover:bg-slate-800 disabled:opacity-50 cursor-pointer disabled:cursor-not-allowed"
          >
            {busy === 'full' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Film className="w-3.5 h-3.5" />}
            {episode.beats.length === 0
              ? '一键制片 (S4→S7)'
              : '按镜头组一键制片 (S4→S7)'}
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
              前往剧本审核 →
            </button>
          </div>
        </div>
      )}

      {jobNotice && !error && (
        <div className="flex items-start gap-2 p-3 text-xs text-emerald-800 bg-emerald-50 border border-emerald-200 rounded-lg">
          <Film className="w-4 h-4 shrink-0 mt-0.5" />
          <div className="flex-1 flex flex-wrap items-center gap-3">
            <p>{jobNotice}</p>
            <div className="flex flex-wrap items-center gap-3">
              <button
                type="button"
                onClick={onSwitchToScriptTab}
                className="font-medium underline cursor-pointer"
              >
                前往剧本审核 →
              </button>
              <button
                type="button"
                onClick={() => setJobNotice('')}
                className="text-emerald-700/70 hover:text-emerald-900 cursor-pointer"
              >
                知道了
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 场景卡片流 */}
      <div className="space-y-4">
        {scenes.map((scene, sceneIdx) => (
          <SceneCard
            key={scene.scene_id}
            scene={scene}
            sceneIdx={sceneIdx}
            sceneCount={scenes.length}
            panelIndex={panelIndex}
            recapOpen={openRecap.has(scene.scene_index)}
            issues={sceneIssues.get(scene.scene_id) ?? []}
            pickerOpen={pickerSceneId === scene.scene_id}
            allPanelIds={allPanelIds}
            panelOwners={panelOwners}
            quoteDraft={quoteDraft[scene.scene_id] ?? ''}
            patchScene={patchScene}
            togglePicker={togglePicker}
            setPickerSceneId={setPickerSceneId}
            setScenes={setScenes}
            handleRemovePanel={handleRemovePanel}
            handleRemovePanelAll={handleRemovePanelAll}
            handleAddPanel={handleAddPanel}
            handleMoveWithinScene={handleMoveWithinScene}
            toggleQuote={toggleQuote}
            addManualQuote={addManualQuote}
            setQuoteDraft={setQuoteDraft}
            toggleRecap={toggleRecap}
          />
        ))}
      </div>
    </div>
  );
};
