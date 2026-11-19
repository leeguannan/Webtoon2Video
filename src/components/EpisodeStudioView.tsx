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
  Wand2,
  Music,
  Sparkles,
} from 'lucide-react';
import type {
  AutoPacingResponse,
  AutoPacingSummary,
  BgmLibraryResponse,
  CameraMotion,
  EpisodeItem,
  EpisodeRenderOptions,
  EpisodeStoryboard,
  OutroMode,
  PronunciationRule,
  ScriptBeat,
  SeriesTtsConfigResponse,
  StageId,
  StoryboardScene,
  StudioConfig,
  VoiceOption,
} from '../types/studio';
import { formatMs, playPreviewTone, resolveSpokenText } from '../utils/pronunciation';
import { PanelZoomModal, type RoleOption, type SaveStatus } from './PanelZoomModal';
import { StoryboardEditor } from './StoryboardEditor';
import BgmLibraryDrawer from './BgmLibraryDrawer';

interface EpisodeStudioViewProps {
  episode: EpisodeItem;
  seriesId: string;
  config: StudioConfig;
  rules: PronunciationRule[];
  onRerunFromStage: (episodeId: string, stageId: StageId) => Promise<void> | void;
  /** 按步向后级联清理（Patch v4.1 Patch 21）：清空该步及下游旧产物与完成标记。 */
  onResetFromStage?: (
    episodeId: string,
    stageId: StageId,
    includeHumanStoryboard?: boolean
  ) => Promise<void> | void;
  onTogglePanelBlank: (episodeId: string, panelId: string) => void;
  /**
   * 保存解说稿。返回是否落盘成功（审核台据此决定是否清掉「未保存改动」标记）。
   * triggerFromStage5=true 表示保存后顺带从 Stage 5 重跑配音与出片。
   */
  onUpdateBeats: (
    episodeId: string,
    beats: ScriptBeat[],
    triggerFromStage5: boolean
  ) => Promise<boolean> | boolean;
  /** 分镜大图弹窗里的字幕原文自动保存（不触发重跑配音）。缺省时退化为本地草稿保存。 */
  onAutoSaveBeats?: (episodeId: string, beats: ScriptBeat[]) => Promise<void>;
  /** Patch v4.6：一键叙事节奏留白（后端改写 pause_after_ms 并同步 audio_manifest）。 */
  onAutoPacing?: (episodeId: string) => Promise<AutoPacingResponse | null>;
  /** Patch v4.5：保存本话封页/结尾片包选项（写入 render_options.json，仅重跑 S7 生效）。 */
  onUpdateEpisodeRenderOptions?: (
    episodeId: string,
    options: EpisodeRenderOptions
  ) => Promise<void> | void;
  onExportEpisode: (episodeId: string, format: 'mp4' | 'ass' | 'script') => void;
}

const DEFAULT_RENDER_OPTIONS: EpisodeRenderOptions = {
  cover_enabled: true,
  cover_text_override: null,
  outro_mode: 'inherit',
};

const EPISODE_OUTRO_OPTIONS: Array<{ value: OutroMode; label: string }> = [
  { value: 'inherit', label: '跟随作品默认' },
  { value: 'continue', label: '待续' },
  { value: 'final', label: '完结' },
  { value: 'none', label: '不加结尾' },
];

type StudioTab = 'panels' | 'storyboard' | 'script' | 'timeline' | 'preview';

/** 单话流水线顺序（S1 ~ S7）；S0 是作品级，不参与「向后清理」。 */
const STAGE_SEQUENCE: StageId[] = [
  's1_crawler',
  's2_segmenter',
  's3_vision',
  's4_scriptwriter',
  's5_tts',
  's6_timeline',
  's7_renderer',
];

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
  onResetFromStage,
  onTogglePanelBlank,
  onUpdateBeats,
  onAutoSaveBeats,
  onAutoPacing,
  onUpdateEpisodeRenderOptions,
  onExportEpisode,
}) => {
  const [activeTab, setActiveTab] = useState<StudioTab>('script');

  // ---- Patch v4.5：本话封页与结尾片包（render_options.json，仅重跑 S7 生效） ----
  const brandingOptions: EpisodeRenderOptions = episode.render_options ?? DEFAULT_RENDER_OPTIONS;
  const [coverOverrideDraft, setCoverOverrideDraft] = useState(
    brandingOptions.cover_text_override ?? ''
  );
  const [brandingSaving, setBrandingSaving] = useState(false);
  const [brandingSavedAt, setBrandingSavedAt] = useState<string | null>(null);
  const [brandingError, setBrandingError] = useState<string | null>(null);

  // ---- Patch v4.6：叙事节奏自动留白（后端规则改写停顿并同步 audio_manifest） ----
  const [autoPacingLoading, setAutoPacingLoading] = useState(false);
  const [autoPacingSummary, setAutoPacingSummary] = useState<AutoPacingSummary | null>(null);
  const [autoPacingError, setAutoPacingError] = useState<string | null>(null);

  // 外部数据刷新（如切换话次、保存回包）后同步本地草稿。
  useEffect(() => {
    setCoverOverrideDraft(episode.render_options?.cover_text_override ?? '');
  }, [episode.episode_id, episode.render_options?.cover_text_override]);

  const persistRenderOptions = useCallback(
    async (next: EpisodeRenderOptions) => {
      if (!onUpdateEpisodeRenderOptions) return;
      setBrandingSaving(true);
      setBrandingError(null);
      try {
        await onUpdateEpisodeRenderOptions(episode.episode_id, next);
        setBrandingSavedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
      } catch (err) {
        setBrandingError(err instanceof Error ? err.message : String(err));
      } finally {
        setBrandingSaving(false);
      }
    },
    [episode.episode_id, onUpdateEpisodeRenderOptions]
  );

  /**
   * Patch v4.6：一键自动节奏留白。表格有未保存改动时拒绝执行并内联提示，
   * 避免后端基于旧稿计算、再被本地草稿覆盖。成功后后端已同时回写
   * script.json 与 audio_manifest.json，本地草稿与落盘值对齐并清掉脏标记，
   * 用户只需从 S6 重跑时间轴与成片，无需重跑 S5 配音。
   */
  const handleRunAutoPacing = useCallback(async () => {
    if (!onAutoPacing || autoPacingLoading) return;
    if (tableDirtyRef.current) {
      setSaveHint('请先保存解说稿再执行自动节奏留白');
      return;
    }
    setAutoPacingLoading(true);
    setAutoPacingError(null);
    try {
      const result = await onAutoPacing(episode.episode_id);
      if (!result) {
        setAutoPacingError('自动节奏留白执行失败，请查看后端日志后重试');
        return;
      }
      draftBeatsRef.current = result.beats;
      setDraftBeats(result.beats);
      tableDirtyRef.current = false;
      setTableDirty(false);
      setSaveHint('');
      setAutoPacingSummary(result.summary);
      // Patch v4.8：停顿被后端改写过 → 必须重建时间轴，跳过 S6 会吃到旧停顿。
      if (result.summary.updated_count > 0) {
        setNeedsTimelineRebuild(true);
      }
    } catch (err) {
      setAutoPacingError(err instanceof Error ? err.message : String(err));
    } finally {
      setAutoPacingLoading(false);
    }
  }, [autoPacingLoading, episode.episode_id, onAutoPacing]);

  // ---- Patch v4.7：按幕 BGM（storyboard 幕配乐 + 作品曲库） ----
  const [sceneStoryboard, setSceneStoryboard] = useState<EpisodeStoryboard | null>(null);
  const [bgmLibrary, setBgmLibrary] = useState<BgmLibraryResponse | null>(null);
  const [bgmDrawerOpen, setBgmDrawerOpen] = useState(false);
  const [suggestBgmLoading, setSuggestBgmLoading] = useState(false);
  const [sceneBgmSavingId, setSceneBgmSavingId] = useState<string | null>(null);
  const [bgmHint, setBgmHint] = useState('');
  const [bgmError, setBgmError] = useState('');

  // 只在解说审核 Tab 加载幕编排与曲库；storyboard 不存在（未做 S3b）时静默回退。
  useEffect(() => {
    if (activeTab !== 'script') return;
    let cancelled = false;
    const episodeBase = `/api/episodes/${encodeURIComponent(episode.episode_id)}`;
    void (async () => {
      const [storyboardRes, libraryRes] = await Promise.all([
        fetch(`${episodeBase}/storyboard`),
        fetch(`/api/series/${encodeURIComponent(seriesId)}/bgm-library`),
      ]);
      if (cancelled) return;
      if (storyboardRes.ok) {
        setSceneStoryboard((await storyboardRes.json()) as EpisodeStoryboard);
      } else {
        setSceneStoryboard(null);
      }
      if (libraryRes.ok) {
        setBgmLibrary((await libraryRes.json()) as BgmLibraryResponse);
      } else {
        setBgmLibrary(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeTab, episode.episode_id, seriesId]);

  useEffect(() => {
    if (!bgmHint) return;
    const timer = window.setTimeout(() => setBgmHint(''), 3600);
    return () => window.clearTimeout(timer);
  }, [bgmHint]);

  /** 把解说句按 storyboard 启用幕的 panel_ids 归组；未命中进末尾兜底组（memo 本体在 draftBeats 声明后）。 */

  /** 保存单幕配乐（全量 PUT storyboard，后端按 scene_id 继承未提交字段）。 */
  const persistSceneBgm = useCallback(
    async (sceneId: string, patch: Partial<StoryboardScene>) => {
      if (!sceneStoryboard) return;
      const nextScenes = sceneStoryboard.scenes.map((scene) =>
        scene.scene_id === sceneId ? { ...scene, ...patch } : scene
      );
      setSceneBgmSavingId(sceneId);
      setBgmError('');
      try {
        const res = await fetch(
          `/api/episodes/${encodeURIComponent(episode.episode_id)}/storyboard`,
          {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scenes: nextScenes }),
          }
        );
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
          throw new Error(body.detail || `保存失败（HTTP ${res.status}）`);
        }
        setSceneStoryboard(body as EpisodeStoryboard);
        setBgmHint('已保存本幕配乐，点击上方 S7 从此步重跑即可出片');
      } catch (err) {
        setBgmError(err instanceof Error ? err.message : '本幕配乐保存失败');
      } finally {
        setSceneBgmSavingId(null);
      }
    },
    [sceneStoryboard, episode.episode_id]
  );

  /** 一键补全各幕 AI 配乐建议（只写 mood/suggestion，不动人工选择的曲目）。 */
  const handleSuggestBgm = useCallback(async () => {
    if (suggestBgmLoading) return;
    setSuggestBgmLoading(true);
    setBgmError('');
    try {
      const res = await fetch(
        `/api/episodes/${encodeURIComponent(episode.episode_id)}/storyboard/suggest-bgm`,
        { method: 'POST' }
      );
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(body.detail || `配乐建议生成失败（HTTP ${res.status}）`);
      }
      setSceneStoryboard(body as EpisodeStoryboard);
      const count = (body as EpisodeStoryboard).scenes.filter(
        (scene) => scene.enabled !== false
      ).length;
      setBgmHint(`已补全 ${count} 幕的 AI 配乐建议，可在各幕卡片选择具体曲目`);
    } catch (err) {
      setBgmError(err instanceof Error ? err.message : '配乐建议生成失败');
    } finally {
      setSuggestBgmLoading(false);
    }
  }, [suggestBgmLoading, episode.episode_id]);

  // 封面文字覆盖：输入防抖 500ms 自动保存；空白归一为 null（后端同样会兜底）。
  useEffect(() => {
    const normalized = coverOverrideDraft.trim() ? coverOverrideDraft.trim() : null;
    if ((episode.render_options?.cover_text_override ?? null) === normalized) return;
    const timer = setTimeout(() => {
      void persistRenderOptions({ ...brandingOptions, cover_text_override: normalized });
    }, 500);
    return () => clearTimeout(timer);
    // brandingOptions 每次渲染都是新对象，只依赖草稿与当前落盘值，避免重复触发。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [coverOverrideDraft, episode.render_options?.cover_text_override]);
  /** Patch v4.0 Patch 20：镜头编排 Tab 的幕数（由 StoryboardEditor 加载后回传）。 */
  const [storyboardSceneCount, setStoryboardSceneCount] = useState<number | null>(null);
  const [selectedPanelId, setSelectedPanelId] = useState<string>(
    episode.panels[0]?.panel_id ?? ''
  );
  const [showFocusOverlay, setShowFocusOverlay] = useState(true);
  const [draftBeats, setDraftBeats] = useState<ScriptBeat[]>(episode.beats);
  /** Patch v4.7：解说句按 storyboard 启用幕归组，未命中分镜进末尾兜底组。 */
  const beatSceneGroups = useMemo(() => {
    const scenes = (sceneStoryboard?.scenes ?? []).filter(
      (scene) => scene.enabled !== false
    );
    if (scenes.length === 0) {
      return [{ key: '__all__', scene: null as StoryboardScene | null, beats: draftBeats }];
    }
    const groups = scenes.map((scene) => ({
      key: scene.scene_id,
      scene,
      beats: [] as ScriptBeat[],
    }));
    const orphan = {
      key: '__orphan__',
      scene: null as StoryboardScene | null,
      beats: [] as ScriptBeat[],
    };
    for (const beat of draftBeats) {
      const panelId = beat.target_panel_ids[0];
      const owner = scenes.find((scene) => scene.panel_ids.includes(panelId));
      if (owner) {
        groups.find((group) => group.key === owner.scene_id)?.beats.push(beat);
      } else {
        orphan.beats.push(beat);
      }
    }
    return orphan.beats.length > 0 ? [...groups, orphan] : groups;
  }, [sceneStoryboard, draftBeats]);
  /** 表格内是否有未落盘的改动（改字 / 改角色 / 增删句）。 */
  const [tableDirty, setTableDirty] = useState(false);
  /** 保存中状态，避免重复点击。 */
  const [savingBeats, setSavingBeats] = useState(false);
  /** 表格保存失败或校验失败时的提示文案。 */
  const [saveHint, setSaveHint] = useState('');
  const [playingBeatId, setPlayingBeatId] = useState<string | null>(null);
  /** 作品级配音设置（角色清单 + 各角色音色），用于「本句配音」的角色下拉。 */
  const [seriesTts, setSeriesTts] = useState<SeriesTtsConfigResponse | null>(null);
  /** Patch v4.2：全部可选音色（百炼 + MiniMax），供弹窗逐句指定音色。 */
  const [voiceOptions, setVoiceOptions] = useState<VoiceOption[]>([]);
  /** Patch v4.1 Patch 21：待确认的「清理重置」目标阶段（内联确认横幅）。 */
  const [resetTarget, setResetTarget] = useState<StageId | null>(null);
  const [resetIncludeHuman, setResetIncludeHuman] = useState(false);
  const [resetBusy, setResetBusy] = useState(false);

  // ---- Patch v4.8：重跑前草稿防残留保护 ----
  /** 句间停顿被改动过：必须先重建时间轴（S6）再出片，跳过 S6 会吃到旧停顿。 */
  const [needsTimelineRebuild, setNeedsTimelineRebuild] = useState(false);
  /** 正在发起的重跑阶段（非空时禁用步骤条按钮，避免重复提交）。 */
  const [rerunningStage, setRerunningStage] = useState<StageId | null>(null);
  /** 待用户裁决的重跑（草稿改了送音内容，但用户想从 S6/S7 直接跑）。 */
  const [pendingRerunChoice, setPendingRerunChoice] = useState<StageId | null>(null);
  /** 重跑相关的内联说明（例如停顿已更新、自动升级到 S6）。 */
  const [rerunNotice, setRerunNotice] = useState('');

  // 分镜大图弹窗 + 字幕原文保存（切换分镜 / 关闭弹窗时落盘，编辑过程中不写库）
  const [zoomBeatId, setZoomBeatId] = useState<string | null>(null);
  /** 当前弹窗打开的那一条解说（弹窗里的角色候选与配音预览都基于它）。 */
  const zoomBeat = useMemo(
    () => draftBeats.find((b) => b.beat_id === zoomBeatId),
    [draftBeats, zoomBeatId]
  );
  const [saveStatus, setSaveStatus] = useState<SaveStatus>('idle');
  const [saveMessage, setSaveMessage] = useState('');
  const draftBeatsRef = useRef<ScriptBeat[]>(episode.beats);
  /** 待保存的草稿：{ episode_id, beats }；只在弹窗里改字时写入，落盘后清空 */
  const pendingRef = useRef<{ episodeId: string; beats: ScriptBeat[] } | null>(null);
  /** 表格未保存标记的 ref 副本：后台回填 effect 里读它，避免把 dirty 塞进依赖导致回填反复触发。 */
  const tableDirtyRef = useRef(false);
  /**
   * 未保存草稿所属的话次。
   *
   * 双保险：即便组件没有因 key 变化而重挂载，切到另一话时也能识别出
   * 「这份 dirty 属于上一话」，拒绝把上一话的草稿套到新话数据上。
   */
  const tableDirtyEpisodeRef = useRef<string>('');

  // Video simulator state (9:16 standard)
  const [isPlayingVideo, setIsPlayingVideo] = useState(false);
  const [videoTimeMs, setVideoTimeMs] = useState(0);
  const isVideoReady = episode.stages.find((s) => s.id === 's7_renderer')?.status === 'done';
  const epFolderName = `ep_${String(episode.episode_no).padStart(3, '0')}`;
  const localOutputPath = `workspace/${seriesId}/episodes/${epFolderName}/07_out/${episode.episode_id}.mp4`;

  useEffect(() => {
    // 后台会定时刷新剧集数据并回填 episode.beats；只要本话还有未保存的改动
    // （表格内编辑或弹窗改字），就保留本地草稿，别让用户正在改的内容被服务端数据冲掉。
    const pending = pendingRef.current;
    const dirty = tableDirtyRef.current;
    // dirty 只在「属于当前话」时才保护草稿；切话次后旧 dirty 必须作废。
    const dirtyBelongsHere = dirty && tableDirtyEpisodeRef.current === episode.episode_id;
    if ((!pending || pending.episodeId !== episode.episode_id) && !dirtyBelongsHere) {
      setDraftBeats(episode.beats);
      draftBeatsRef.current = episode.beats;
    }
    if (dirty && !dirtyBelongsHere) {
      // 话次切换：清掉上一话遗留的未保存标记与确认横幅，避免草稿串话。
      tableDirtyRef.current = false;
      setTableDirty(false);
      setSaveHint('');
      setResetTarget(null);
      tableDirtyEpisodeRef.current = '';
    }
    setSelectedPanelId(episode.panels[0]?.panel_id ?? '');
    setVideoTimeMs(0);
    setIsPlayingVideo(false);
  }, [episode.episode_id, episode.beats, episode.panels]);

  // Patch v4.8：换话次时清掉「停顿已改 / 待裁决重跑」的跨话残留状态。
  useEffect(() => {
    setNeedsTimelineRebuild(false);
    setPendingRerunChoice(null);
    setRerunNotice('');
  }, [episode.episode_id]);

  // Patch v3.4：拉作品级配音设置——审核台要显示「本句配音用哪个角色 / 哪个音色」，
  // 并允许就地改角色（改完保存并重跑 Stage 5 生效）。
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`/api/series/${encodeURIComponent(seriesId)}/tts-config`);
        if (!res.ok) return;
        const data = (await res.json()) as SeriesTtsConfigResponse;
        if (!cancelled) setSeriesTts(data);
      } catch {
        // 拉不到就退回「只显示后端已解析结果」的降级模式，不影响审核台其他功能。
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [seriesId]);

  /** Patch v4.2：拉全部可选音色（百炼 + MiniMax），弹窗里可逐句指定音色。 */
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch('/api/tts/voices');
        if (!res.ok) return;
        const data = (await res.json()) as {
          voices?: VoiceOption[];
          minimax_voices?: VoiceOption[];
        };
        if (cancelled) return;
        setVoiceOptions([...(data.voices ?? []), ...(data.minimax_voices ?? [])]);
      } catch {
        // 拉不到就退回「只显示后端已解析结果」的降级模式。
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  /** 可选角色名（作品角色表 + 已单独配置音色的角色）。 */
  const roleNames = useMemo(() => {
    const names = new Set<string>();
    seriesTts?.characters?.forEach((c) => names.add(c.name));
    Object.keys(seriesTts?.character_voices ?? {}).forEach((n) => names.add(n));
    return Array.from(names);
  }, [seriesTts]);

  /**
   * 角色 → 性别。
   *
   * 改角色时必须同步 speaker_gender：Stage 4 留下的性别属于**原来那个角色**
   * （男角色改成女角色后仍是 male），不跟着改就会用错音色的兜底槽位。
   */
  const genderForRole = (role: string): 'male' | 'female' | 'unknown' => {
    const lowered = role.trim().toLowerCase();
    if (!lowered) return 'unknown';
    if (lowered === 'male' || lowered.startsWith('male_')) return 'male';
    if (lowered === 'female' || lowered.startsWith('female_')) return 'female';
    const character = (seriesTts?.characters ?? []).find((c) =>
      [c.name, ...(c.aliases ?? [])].some(
        (key) => key.trim().toLowerCase() === lowered
      )
    );
    const gender = (character?.gender ?? '').toLowerCase();
    return gender === 'male' || gender === 'female' ? gender : 'unknown';
  };

  /**
   * 改说话人角色时要同步落的字段（表格行内 select 与分镜弹窗共用同一套规则）：
   * a) speaker_gender 始终跟着角色走，选回「不指定」（空串）时重置为 unknown；
   * b) 清掉历史逐句音色 voice_override，恢复按角色解析，避免界面角色与实际音色不一致；
   * c) Patch v4.4：一并清空单句语速微调 speed_override，让新角色自带的默认语速自动生效。
   */
  const speakerRolePatch = (
    beat: ScriptBeat,
    nextRole: string
  ): Partial<ScriptBeat> => {
    const patch: Partial<ScriptBeat> = {
      speaker_role: nextRole,
      speaker_gender: nextRole ? genderForRole(nextRole) : 'unknown',
      speed_override: null,
    };
    if ((beat.voice_override ?? '').trim()) patch.voice_override = '';
    return patch;
  };

  /**
   * 说话人候选（审核台表格与分镜弹窗共用同一份）。
   *
   * 只让用户选**角色**，音色由角色自动带出（讲解者 / 通用男声 / 通用女声 / 角色专属），
   * 避免在一堆音色 ID 里挑花眼。当前角色可能是旧槽位代号（如 male_antagonist），
   * 不在候选里时补一条，免得下拉显示空白、用户看不出这一句到底是谁在念。
   */
  const roleOptionsFor = (beat: ScriptBeat): RoleOption[] => {
    const currentRole = (beat.speaker_role ?? '').trim();
    const options: RoleOption[] = [];
    if (
      currentRole &&
      !['narrator', 'male', 'female'].includes(currentRole) &&
      !roleNames.includes(currentRole)
    ) {
      options.push({
        value: currentRole,
        label: `当前角色 ${currentRole}（旧槽位）`,
      });
    }
    options.push({ value: '', label: '不指定（按性别兜底）' });
    options.push({ value: 'narrator', label: '讲解者（旁白）' });
    options.push({ value: 'male', label: '通用男声' });
    options.push({ value: 'female', label: '通用女声' });
    roleNames.forEach((name) => {
      options.push({
        value: name,
        label: seriesTts?.character_voices?.[name]
          ? name
          : `${name}（未单独配置音色）`,
      });
    });
    return options;
  };

  /**
   * Patch v4.4：按当前 ``speaker_role`` 从作品级 TTS 配置算出角色默认语速。
   *
   * 讲解者 / 角色专属 / 通用男女声各取对应槽位的 ``speed``；本地配置找不到时，
   * 依次回落后端下发的 ``role_speed``、当前生效 ``speed``，最后兜底 1.15。
   */
  const roleSpeedForBeat = (beat: ScriptBeat): number => {
    const fallback =
      beat.resolved_voice?.role_speed ?? beat.resolved_voice?.speed ?? 1.15;
    if (!seriesTts) return fallback;
    const role = (beat.speaker_role ?? '').trim();
    if (!role) return fallback;
    if (role === 'narrator' || role === '旁白' || role === '解说') {
      return seriesTts.narrator.speed;
    }
    const cast = seriesTts.character_voices[role];
    if (cast) return cast.speed;
    if (role === 'male') return seriesTts.default_male.speed;
    if (role === 'female') return seriesTts.default_female.speed;
    return fallback;
  };

  /**
   * 一条 beat 的「本句配音」说明。
   *
   * 后端已给出权威结果（``resolved_voice``，与 Stage 5 同一套解析）；这里再按当前
   * 下拉选择做一次**本地预览**，改角色后能立刻看到会换成谁，保存重跑后以服务端为准。
   *
   * Patch v4.4：返回值同时携带语速三件套——``roleSpeed``（角色默认语速）、
   * ``speed``（当前生效语速，微调后等于 ``speed_override``）、``speedOverridden``。
   */
  const beatVoiceInfo = (
    beat: ScriptBeat
  ): {
    label: string;
    voice: string;
    pending: boolean;
    hint: string;
    speed: number;
    roleSpeed: number;
    speedOverridden: boolean;
  } => {
    const roleSpeed = roleSpeedForBeat(beat);
    const speedOverridden = beat.speed_override != null;
    const speed = speedOverridden
      ? (beat.speed_override as number)
      : roleSpeed;
    const speedFields = { speed, roleSpeed, speedOverridden };
    // Patch v4.2：逐句指定音色（voice_override）优先级最高，与后端 s5_tts 解析保持一致。
    const override = (beat.voice_override ?? '').trim();
    if (override) {
      return {
        label: '指定音色',
        voice: override,
        pending: false,
        hint: '已逐句指定音色，改选角色可恢复按角色解析',
        ...speedFields,
      };
    }
    const resolved = beat.resolved_voice;
    const role = (beat.speaker_role ?? '').trim();
    const base = {
      label: resolved?.label ?? '待解析',
      voice: resolved?.voice ?? '—',
      pending: false,
      hint: '',
      ...speedFields,
    };
    if (!seriesTts) return base;
    if (!role) {
      return {
        label: '不指定 · 按性别兜底',
        voice: base.voice,
        pending: !!resolved,
        hint: '按角色性别选通用男声 / 通用女声',
        ...speedFields,
      };
    }
    if (role === 'narrator' || role === '旁白' || role === '解说') {
      return {
        label: '讲解者（旁白）',
        voice: seriesTts.narrator.voice,
        pending: false,
        hint: '',
        ...speedFields,
      };
    }
    const cast = seriesTts.character_voices[role];
    if (cast) {
      return {
        label: `角色专属 · ${role}`,
        voice: cast.voice,
        pending: false,
        hint: '',
        ...speedFields,
      };
    }
    if (role === 'male') {
      return {
        label: '通用男声',
        voice: seriesTts.default_male.voice,
        pending: false,
        hint: '',
        ...speedFields,
      };
    }
    if (role === 'female') {
      return {
        label: '通用女声',
        voice: seriesTts.default_female.voice,
        pending: false,
        hint: '',
        ...speedFields,
      };
    }
    if (roleNames.includes(role)) {
      // 作品角色表里有这个角色，但没单独配音色 → Stage 5 按性别兜底。
      return {
        label: `角色 ${role} · 按性别兜底`,
        voice: base.voice,
        pending: false,
        hint: '该角色未单独配置音色，可改选通用男声 / 通用女声',
        ...speedFields,
      };
    }
    // 旧槽位角色（male_antagonist 之类）：按后端给出的性别落到作品级通用槽位。
    if (resolved?.gender === 'male') {
      return {
        label: '通用男声',
        voice: seriesTts.default_male.voice,
        pending: false,
        hint: '旧槽位角色，改选角色名可切到专属音色',
        ...speedFields,
      };
    }
    if (resolved?.gender === 'female') {
      return {
        label: '通用女声',
        voice: seriesTts.default_female.voice,
        pending: false,
        hint: '旧槽位角色，改选角色名可切到专属音色',
        ...speedFields,
      };
    }
    return { ...base, hint: '旧槽位角色，改选角色名可切到专属音色' };
  };

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

  /** 标记表格里有未落盘的改动。 */
  const markTableDirty = useCallback(() => {
    tableDirtyRef.current = true;
    tableDirtyEpisodeRef.current = episode.episode_id;
    setTableDirty(true);
  }, [episode.episode_id]);

  /**
   * 表格里改一个字段。
   *
   * 统一以 draftBeatsRef 为单一数据源：ref 与 state 必须同步更新，
   * 否则「表格改字 → 再开弹窗改字」会拿旧的 ref 当基线，把表格的改动整份覆盖掉。
   */
  const updateBeatField = <K extends keyof ScriptBeat>(
    beatId: string,
    key: K,
    value: ScriptBeat[K]
  ) => {
    const next = draftBeatsRef.current.map((b) =>
      b.beat_id === beatId ? { ...b, [key]: value } : b
    );
    draftBeatsRef.current = next;
    setDraftBeats(next);
    // Patch v4.8：改了句间停顿就必须重建时间轴，否则从 S7 重跑会吃到旧停顿。
    if (key === 'pause_after_ms') {
      setNeedsTimelineRebuild(true);
    }
    markTableDirty();
  };

  // ── 分镜大图弹窗：字幕原文改完，在切换分镜 / 关闭弹窗时落盘 ────────────
  /**
   * 把待保存的草稿写回后端；没有待保存内容就不发请求。
   * 返回是否保存成功（失败时保留弹窗，让用户看到错误并可重试）。
   */
  const flushSave = useCallback(async (): Promise<boolean> => {
    const pending = pendingRef.current;
    if (!pending) return true;
    const { episodeId } = pending;
    // 提交当前**最新**草稿而不是 pending 里的快照：弹窗打开期间表格可能又改了字，
    // 用旧快照落盘会把表格里的改动整份丢掉。
    const beats = draftBeatsRef.current;
    setSaveStatus('saving');
    try {
      if (!onAutoSaveBeats) {
        // 没有独立通道时退化为普通保存（不触发重跑配音）
        const ok = await onUpdateBeats(episodeId, beats, false);
        if (ok === false) throw new Error('保存失败，请检查后端服务后重试');
      } else {
        await onAutoSaveBeats(episodeId, beats);
      }
      pendingRef.current = null;
      tableDirtyRef.current = false;
      tableDirtyEpisodeRef.current = '';
      setTableDirty(false);
      setSaveStatus('saved');
      setSaveMessage(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
      return true;
    } catch (err) {
      setSaveStatus('error');
      setSaveMessage(err instanceof Error ? err.message : String(err));
      return false;
    }
  }, [onAutoSaveBeats, onUpdateBeats]);

  /** 弹窗内改一个字段：只改本地草稿，落盘交给切格 / 关闭时统一做 */
  const updateZoomBeat = <K extends keyof ScriptBeat>(
    beatId: string,
    key: K,
    value: ScriptBeat[K]
  ) => {
    const next = draftBeatsRef.current.map((b) =>
      b.beat_id === beatId ? { ...b, [key]: value } : b
    );
    draftBeatsRef.current = next;
    setDraftBeats(next);
    pendingRef.current = { episodeId: episode.episode_id, beats: next };
    markTableDirty();
    setSaveStatus('dirty');
    setSaveMessage('切换分镜或关闭弹窗时自动保存');
  };

  /** 弹窗内改字：只改本地草稿，落盘交给切格 / 关闭时统一做 */
  const handleZoomNarrationChange = (beatId: string, text: string) => {
    updateZoomBeat(beatId, 'narration_text', text);
  };

  /**
   * Patch v4.2：弹窗内改说话人角色。
   *
   * 角色决定音色（讲解者 / 通用男声 / 通用女声 / 角色专属），所以只改角色就够；
   * 与表格行内 select 走同一套 speakerRolePatch：同步 speaker_gender、
   * 清空 voice_override，避免界面显示的角色与实际合成音色不一致。
   */
  const handleZoomRoleChange = (beatId: string, role: string) => {
    const beat = draftBeatsRef.current.find((b) => b.beat_id === beatId);
    if (!beat) return;
    const patch = speakerRolePatch(beat, role);
    (Object.keys(patch) as Array<keyof ScriptBeat>).forEach((key) => {
      updateZoomBeat(beatId, key, patch[key] as ScriptBeat[typeof key]);
    });
  };

  /** Patch v4.2：弹窗内改情绪指令（送音语气）。 */
  const handleZoomEmotionChange = (beatId: string, text: string) => {
    updateZoomBeat(beatId, 'emotion_instruction', text);
  };

  /**
   * Patch v4.4：弹窗内调单句语速。
   *
   * 只改本地草稿，落盘与其他弹窗字段一样交给切格 / 关闭时的统一保存；
   * 传 ``null`` 表示清空微调、恢复该角色的默认语速。
   */
  const handleZoomSpeedChange = (beatId: string, speed: number | null) => {
    updateZoomBeat(beatId, 'speed_override', speed);
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

  /** 生成一个当前列表里没被占用的 beat_id（避免与既有 id 撞车）。 */
  const uniqueBeatId = (beats: ScriptBeat[]): string => {
    const used = new Set(beats.map((b) => b.beat_id));
    let n = beats.length + 1;
    let candidate = `b_new_${String(n).padStart(3, '0')}`;
    while (used.has(candidate)) {
      n += 1;
      candidate = `b_new_${String(n).padStart(3, '0')}`;
    }
    return candidate;
  };

  const handleAddBeat = () => {
    const prev = draftBeatsRef.current;
    const firstPanel = episode.panels[0]?.panel_id ?? 'p_001';
    const newBeat: ScriptBeat = {
      beat_id: uniqueBeatId(prev),
      narration_text: '请在此输入新的短句解说文案。',
      tts_text: '',
      emotion_instruction: '用自然平稳的漫评解说语气说出',
      target_panel_ids: [firstPanel],
      motion: 'zoom_in',
      pause_after_ms: 250,
      audio_duration_ms: 0,
    };
    const next = [...prev, newBeat];
    draftBeatsRef.current = next;
    setDraftBeats(next);
    markTableDirty();
  };

  /**
   * 在指定 beat 之后插入一条新解说句（Patch v3.4）。
   *
   * 画面默认继承**下一句**的分镜（插入的镜头承接后面的画面；没有下一句时继承当前句），
   * 情绪指令 / 运镜 / 停顿 / 说话角色沿用当前句，避免插进去就变味。
   * 新句尚未合成，时长记 0；落盘需要点「保存并从 Stage 5 重跑配音」。
   */
  const handleInsertBeat = (afterBeatId: string) => {
    const prev = draftBeatsRef.current;
    const index = prev.findIndex((b) => b.beat_id === afterBeatId);
    if (index < 0) return;
    const current = prev[index];
    const next = prev[index + 1];
    const newBeat: ScriptBeat = {
      beat_id: uniqueBeatId(prev),
      narration_text: '请在此输入新的短句解说文案。',
      tts_text: '',
      emotion_instruction: current.emotion_instruction,
      target_panel_ids: next
        ? [...next.target_panel_ids]
        : [...current.target_panel_ids],
      motion: current.motion,
      pause_after_ms: current.pause_after_ms,
      audio_duration_ms: 0,
      speaker_role: current.speaker_role,
    };
    const nextBeats = [...prev];
    nextBeats.splice(index + 1, 0, newBeat);
    draftBeatsRef.current = nextBeats;
    setDraftBeats(nextBeats);
    markTableDirty();
  };

  const handleDeleteBeat = (beatId: string) => {
    const next = draftBeatsRef.current.filter((b) => b.beat_id !== beatId);
    draftBeatsRef.current = next;
    setDraftBeats(next);
    markTableDirty();
  };

  /**
   * 保存解说稿。
   *
   * triggerFromStage5=false：只落盘剧本（不重跑配音）；
   * triggerFromStage5=true ：落盘后顺带从 Stage 5 重跑配音与出片。
   * 保存失败时保留草稿与「未保存改动」标记，别让用户以为已经存上了。
   */
  const handleSaveBeats = async (triggerFromStage5: boolean) => {
    const beats = draftBeatsRef.current;
    const invalid = beats.find((b) => (b.narration_text ?? '').trim().length < 2);
    if (invalid) {
      setSaveHint(
        `「${invalid.beat_id}」的字幕原文为空或不足 2 字，后端会拒收整批，请先补齐再保存。`
      );
      return;
    }
    setSaveHint('');
    setSavingBeats(true);
    try {
      const ok = await onUpdateBeats(episode.episode_id, beats, triggerFromStage5);
      if (ok === false) {
        setSaveHint('保存失败：后端拒绝了这次保存，草稿仍保留在本地，请检查后重试。');
        return;
      }
      tableDirtyRef.current = false;
      tableDirtyEpisodeRef.current = '';
      setTableDirty(false);
      pendingRef.current = null;
    } finally {
      setSavingBeats(false);
    }
  };

  /**
   * Patch v4.8：草稿里是否改动了「必须重跑 Stage 5 才会生效」的字段。
   *
   * 对比本地草稿与后端已落盘的 `episode.beats`（停顿不在其中——停顿改完
   * 只需重建时间轴，不必重跑配音）。改动送音文本 / 角色 / 情绪 / 音色 /
   * 语速，或增删句子时，旧配音就是残留数据，必须提醒用户从 S5 重跑。
   */
  const hasVoiceRelevantDraftChanges = useCallback((): boolean => {
    const draft = draftBeatsRef.current;
    const saved = episode.beats ?? [];
    if (draft.length !== saved.length) return true;
    const savedMap = new Map(saved.map((beat) => [beat.beat_id, beat]));
    const norm = (value: unknown): unknown =>
      value === null || value === undefined ? '' : value;
    for (const beat of draft) {
      const other = savedMap.get(beat.beat_id);
      if (!other) return true;
      if (
        (beat.narration_text ?? '') !== (other.narration_text ?? '') ||
        (beat.tts_text ?? '') !== (other.tts_text ?? '') ||
        (beat.speaker_role ?? '') !== (other.speaker_role ?? '') ||
        (beat.emotion_instruction ?? '') !== (other.emotion_instruction ?? '') ||
        norm(beat.voice_override) !== norm(other.voice_override) ||
        norm(beat.speed_override) !== norm(other.speed_override)
      ) {
        return true;
      }
    }
    return false;
  }, [episode.beats]);

  /**
   * Patch v4.8：统一接管步骤条上的「从此步重跑」。
   *
   * 三道保护：
   * 1. 有未保存草稿时，先校验并自动落盘（同步 audio_manifest），失败即中止；
   * 2. 草稿改过送音内容却想从 S6/S7 跑 → 内联横幅让用户选「从 S5 重跑」还是「仍从当前阶段跑」；
   * 3. 停顿被改过却想跳过 S6 → 自动把起点升级成 S6（0 TTS 成本），并说明原因。
   */
  const handleStageRerunClick = useCallback(
    async (stageId: StageId) => {
      if (rerunningStage) return;

      // ① 草稿是否影响配音（必须在自动保存之前比较：保存后两边就一致了）。
      const voiceChanged = hasVoiceRelevantDraftChanges();
      const dirty = tableDirtyRef.current || pendingRef.current !== null;

      // ② 目标阶段为 S5/S6/S7 且有未保存草稿 → 先自动落盘。
      if (dirty && (stageId === 's5_tts' || stageId === 's6_timeline' || stageId === 's7_renderer')) {
        const beats = draftBeatsRef.current;
        const invalid = beats.find((b) => (b.narration_text ?? '').trim().length < 2);
        if (invalid) {
          setSaveHint(
            `「${invalid.beat_id}」的字幕原文为空或不足 2 字，后端会拒收整批，请先补齐再重跑。`
          );
          return;
        }
        setSaveHint('');
        setSavingBeats(true);
        try {
          const ok = await onUpdateBeats(episode.episode_id, beats, false);
          if (ok === false) {
            setSaveHint('保存失败：后端拒绝了这次保存，已中止重跑，草稿仍保留在本地。');
            return;
          }
          tableDirtyRef.current = false;
          tableDirtyEpisodeRef.current = '';
          setTableDirty(false);
          pendingRef.current = null;
        } catch (err) {
          setSaveHint(
            `保存失败，已中止重跑：${err instanceof Error ? err.message : String(err)}`
          );
          return;
        } finally {
          setSavingBeats(false);
        }
      }

      // ③ 停顿改过却要跳过 S6 → 自动升级起点，保证新停顿进入成片。
      let targetStage = stageId;
      if (stageId === 's7_renderer' && needsTimelineRebuild) {
        targetStage = 's6_timeline';
        setNeedsTimelineRebuild(false);
        setRerunNotice('检测到句间停顿已更新，已自动先保存并从 Stage 6 开始重跑以确保停顿生效');
      }

      // ④ 草稿改了送音内容却要从 S6/S7 跑 → 内联横幅让用户决定。
      if (voiceChanged && (targetStage === 's6_timeline' || targetStage === 's7_renderer')) {
        setPendingRerunChoice(targetStage);
        setRerunNotice('');
        return;
      }

      setPendingRerunChoice(null);
      setRerunningStage(targetStage);
      try {
        await onRerunFromStage(episode.episode_id, targetStage);
        // 从 S6 或更早阶段重跑会连带重建时间轴，停顿标记可以清掉。
        if (targetStage !== 's7_renderer') {
          setNeedsTimelineRebuild(false);
        }
      } catch (err) {
        setSaveHint(err instanceof Error ? err.message : '重跑失败，请稍后重试');
      } finally {
        setRerunningStage(null);
      }
    },
    [
      episode.episode_id,
      hasVoiceRelevantDraftChanges,
      needsTimelineRebuild,
      onRerunFromStage,
      onUpdateBeats,
      rerunningStage,
    ]
  );

  /** 横幅里的「仍从当前阶段重跑」：放弃重跑配音，继续用旧音频出片。 */
  const handleRerunKeepStage = useCallback(async () => {
    const stage = pendingRerunChoice;
    if (!stage) return;
    setPendingRerunChoice(null);
    setRerunningStage(stage);
    try {
      await onRerunFromStage(episode.episode_id, stage);
      if (stage !== 's7_renderer') {
        setNeedsTimelineRebuild(false);
      }
    } catch (err) {
      setSaveHint(err instanceof Error ? err.message : '重跑失败，请稍后重试');
    } finally {
      setRerunningStage(null);
    }
  }, [episode.episode_id, onRerunFromStage, pendingRerunChoice]);

  /** 横幅里的「改为从 Stage 5 重跑配音（推荐）」：旧配音作废，重新合成。 */
  const handleRerunFromStage5 = useCallback(async () => {
    setPendingRerunChoice(null);
    setRerunningStage('s5_tts');
    try {
      await onRerunFromStage(episode.episode_id, 's5_tts');
      setNeedsTimelineRebuild(false);
    } catch (err) {
      setSaveHint(err instanceof Error ? err.message : '重跑失败，请稍后重试');
    } finally {
      setRerunningStage(null);
    }
  }, [episode.episode_id, onRerunFromStage]);

  // 有未落盘的改动时，刷新 / 关页面前提醒一次，避免草稿被静默丢掉。
  useEffect(() => {
    if (!tableDirty) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [tableDirty]);

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

  /* ------------------------------------------------------------------
   * Patch v4.1 Patch 21：按步向后级联清理（内联确认，不用 window.confirm）
   * ---------------------------------------------------------------- */
  const resetAffectedStages = useMemo(() => {
    if (!resetTarget) return [] as StageId[];
    const index = STAGE_SEQUENCE.indexOf(resetTarget);
    return index < 0 ? [] : STAGE_SEQUENCE.slice(index);
  }, [resetTarget]);

  const resetAffectedCodes = useMemo(
    () =>
      resetAffectedStages.map((stageId) => {
        const found = episode.stages.find((item) => item.id === stageId);
        return found ? `${found.code} ${found.label}` : stageId;
      }),
    [episode.stages, resetAffectedStages]
  );

  const resetStageLabel = (stageId: StageId): string => {
    const found = episode.stages.find((item) => item.id === stageId);
    return found ? `${found.code} ${found.label}` : stageId;
  };

  const handleConfirmReset = useCallback(async () => {
    if (!resetTarget || !onResetFromStage) return;
    setResetBusy(true);
    try {
      await onResetFromStage(episode.episode_id, resetTarget, resetIncludeHuman);
      setResetTarget(null);
      setResetIncludeHuman(false);
    } finally {
      setResetBusy(false);
    }
  }, [episode.episode_id, onResetFromStage, resetIncludeHuman, resetTarget]);

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
              点击「从此步重跑」或「向后清理」都会先清空该步及全部下游旧产物与完成标记，保证状态与真实成片一致。
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

                <div className="flex flex-col gap-1.5">
                  <button
                    type="button"
                    disabled={rerunningStage !== null}
                    onClick={() => void handleStageRerunClick(st.id)}
                    className="w-full py-1 px-2 text-[11px] font-medium text-slate-700 bg-white border border-slate-200 rounded hover:border-blue-400 hover:text-blue-600 transition-colors whitespace-nowrap shrink-0 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {rerunningStage === st.id ? '正在重跑…' : '从此步重跑'}
                  </button>
                  {st.id !== 's0_catalog' && (
                    <button
                      type="button"
                      disabled={isRunning}
                      onClick={() => {
                        setResetIncludeHuman(false);
                        setResetTarget(st.id);
                      }}
                      className="w-full py-1 px-2 text-[11px] font-medium text-slate-500 bg-white border border-slate-200 rounded hover:border-red-300 hover:text-red-600 transition-colors whitespace-nowrap shrink-0 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      清理重置
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>

        {/* Patch v4.1 Patch 21：清理重置的内联确认（不用 window.confirm） */}
        {resetTarget && (
          <div className="mt-3 p-3.5 rounded-lg border border-red-200 bg-red-50/60 text-xs text-red-900">
            <div className="flex items-start gap-2">
              <AlertCircle className="w-4 h-4 shrink-0 mt-0.5 text-red-600" />
              <div className="flex-1 space-y-2">
                <p className="font-medium">
                  确认清理并重置「{resetStageLabel(resetTarget)}」及之后全部阶段（
                  {resetAffectedCodes.join(' → ')}）？
                </p>
                <ul className="list-disc pl-4 space-y-0.5 text-red-800/90">
                  <li>将删除该阶段及其下游的磁盘产物（脚本 / 配音 / 时间轴 / 成片）与绿色完成标记。</li>
                  {resetTarget === 's4_scriptwriter' && (
                    <li>剧本、故事稿会被删除；人工编排的镜头组默认保留。</li>
                  )}
                  {(resetTarget === 's1_crawler' ||
                    resetTarget === 's2_segmenter' ||
                    resetTarget === 's3_vision') && (
                    <li>会一并清除旧分镜与识图结果，下游镜头组编排也会被重新生成。</li>
                  )}
                  {resetTarget === 's7_renderer' && (
                    <li>作品目录下的导出成片（EP{episode.episode_no}_*）也会被删除。</li>
                  )}
                </ul>
                {resetTarget === 's4_scriptwriter' && (
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={resetIncludeHuman}
                      onChange={(event) => setResetIncludeHuman(event.target.checked)}
                      className="accent-red-600"
                    />
                    同时清空人工编排的镜头组（storyboard.json）（默认保留）
                  </label>
                )}
                <div className="flex items-center gap-2 pt-0.5">
                  <button
                    type="button"
                    disabled={resetBusy}
                    onClick={() => void handleConfirmReset()}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-red-600 rounded hover:bg-red-700 transition-colors cursor-pointer disabled:opacity-50"
                  >
                    {resetBusy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                    确认清理
                  </button>
                  <button
                    type="button"
                    disabled={resetBusy}
                    onClick={() => {
                      setResetTarget(null);
                      setResetIncludeHuman(false);
                    }}
                    className="px-3 py-1.5 text-xs font-medium text-slate-600 bg-white border border-slate-200 rounded hover:bg-slate-50 transition-colors cursor-pointer disabled:opacity-50"
                  >
                    取消
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}
      </section>

      {/* Patch v4.8：重跑前草稿防残留保护——保存失败即中止；送音内容改动需用户裁决；停顿改动自动升级起点。 */}
      {pendingRerunChoice && (
        <div className="mt-3 p-3.5 rounded-lg border border-amber-200 bg-amber-50/70 text-xs text-amber-900">
          <div className="flex items-start gap-2">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5 text-amber-600" />
            <div className="flex-1 space-y-2">
              <p className="font-medium">
                检测到解说稿改动会影响配音（送音文本 / 角色 / 情绪 / 音色 / 语速 / 增删句）。
              </p>
              <p className="text-amber-800/90">
                直接从{' '}
                {pendingRerunChoice === 's7_renderer' ? 'Stage 7' : 'Stage 6'}{' '}
                重跑会沿用旧配音，新改动不会出现在成片里。草稿已自动保存，请选择重跑起点：
              </p>
              <div className="flex flex-wrap items-center gap-2 pt-0.5">
                <button
                  type="button"
                  disabled={rerunningStage !== null}
                  onClick={() => void handleRerunFromStage5()}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-600 rounded hover:bg-blue-700 transition-colors cursor-pointer disabled:opacity-50"
                >
                  {rerunningStage === 's5_tts' && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                  改为从 Stage 5 重跑配音（推荐）
                </button>
                <button
                  type="button"
                  disabled={rerunningStage !== null}
                  onClick={() => void handleRerunKeepStage()}
                  className="px-3 py-1.5 text-xs font-medium text-slate-600 bg-white border border-slate-200 rounded hover:bg-slate-50 transition-colors cursor-pointer disabled:opacity-50"
                >
                  仍从当前阶段重跑
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
      {!pendingRerunChoice && rerunNotice && (
        <div className="mt-3 flex items-start gap-2 px-3 py-2 text-xs text-emerald-800 bg-emerald-50 border border-emerald-200 rounded-lg">
          <CheckCircle2 className="w-4 h-4 mt-0.5 shrink-0" />
          <span>{rerunNotice}</span>
        </div>
      )}

      {/* Patch v4.5：本话封页与结尾片包（仅重跑 S7 合成成片即可生效，无需重跑 S4~S6） */}
      <section className="bg-white border border-slate-200 rounded-xl px-5 py-4">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
          <h3 className="inline-flex items-center gap-2 text-sm font-semibold text-slate-900">
            <Film className="w-4 h-4 text-blue-600" />
            本话封页与结尾片包
            <span className="text-xs font-normal text-slate-400">仅重跑 S7 生效</span>
          </h3>
          <div className="inline-flex items-center gap-2 text-xs text-slate-500">
            {brandingSaving && (
              <>
                <Loader2 className="w-3.5 h-3.5 animate-spin text-blue-600" />
                正在保存…
              </>
            )}
            {!brandingSaving && brandingError && (
              <span className="inline-flex items-center gap-1 text-red-600">
                <AlertCircle className="w-3.5 h-3.5" />
                {brandingError}
              </span>
            )}
            {!brandingSaving && !brandingError && brandingSavedAt && (
              <span className="text-emerald-600">
                已保存 {brandingSavedAt} · 修改后仅需从 S7 合成成片重跑即可生效
              </span>
            )}
            {!brandingSaving && !brandingError && !brandingSavedAt && (
              <span>修改后仅需从 S7 合成成片重跑即可生效</span>
            )}
          </div>
        </div>
        <div className="flex flex-wrap items-end gap-4">
          <label className="inline-flex items-center gap-2 text-xs text-slate-700 cursor-pointer">
            <input
              type="checkbox"
              checked={brandingOptions.cover_enabled}
              onChange={(e) =>
                void persistRenderOptions({
                  ...brandingOptions,
                  cover_enabled: e.target.checked,
                })
              }
              className="accent-blue-600 cursor-pointer"
            />
            启用作品通用封页
          </label>
          <label className="block min-w-[220px] flex-1 max-w-sm">
            <span className="block text-xs text-slate-500 mb-1">
              封面文字覆盖（留空自动填如「第一话」）
            </span>
            <input
              type="text"
              value={coverOverrideDraft}
              onChange={(e) => setCoverOverrideDraft(e.target.value)}
              placeholder="留空则按作品模板自动生成"
              className="w-full rounded-md border border-slate-300 px-2.5 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-blue-600/20 focus:border-blue-600"
            />
          </label>
          <label className="block">
            <span className="block text-xs text-slate-500 mb-1">结尾图</span>
            <select
              aria-label="本话结尾图模式"
              value={brandingOptions.outro_mode}
              onChange={(e) =>
                void persistRenderOptions({
                  ...brandingOptions,
                  outro_mode: e.target.value as OutroMode,
                })
              }
              className="rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs text-slate-700 focus:outline-none focus:ring-2 focus:ring-blue-600/20 focus:border-blue-600 cursor-pointer"
            >
              {EPISODE_OUTRO_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      </section>

      {/* Main Studio Workspace Tabs */}
      <section className="bg-white border border-slate-200 rounded-xl overflow-hidden">
        <div className="px-5 py-3.5 border-b border-slate-200 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-1 p-1 bg-slate-100 rounded-lg overflow-x-auto">
            {(
              [
                { id: 'panels', label: `分镜与视觉抽取 (${episode.panels.length})` },
                {
                  id: 'storyboard',
                  label:
                    storyboardSceneCount === null
                      ? '镜头编排 (Storyboard)'
                      : `镜头编排 (${storyboardSceneCount})`,
                },
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
                disabled={autoPacingLoading || !onAutoPacing}
                onClick={() => void handleRunAutoPacing()}
                title={tableDirty ? '请先保存解说稿再执行自动节奏留白' : '按叙事节奏规则自动优化句间停顿（pause_after_ms）'}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-slate-700 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 whitespace-nowrap shrink-0 cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {autoPacingLoading ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Wand2 className="w-3.5 h-3.5" />
                )}
                自动节奏留白
              </button>
              <button
                type="button"
                onClick={() => setBgmDrawerOpen(true)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-slate-700 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 whitespace-nowrap shrink-0 cursor-pointer"
              >
                <Music className="w-3.5 h-3.5" />
                BGM 曲库管理
              </button>
              <button
                type="button"
                disabled={suggestBgmLoading}
                onClick={() => void handleSuggestBgm()}
                title="按各幕标题/地点/视觉摘要与分镜台词，AI 推断五档情绪与搜歌关键词"
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-slate-700 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 whitespace-nowrap shrink-0 cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {suggestBgmLoading ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Sparkles className="w-3.5 h-3.5" />
                )}
                补全各幕配乐建议
              </button>
              {tableDirty && (
                <span className="inline-flex items-center gap-1 px-2 py-1 text-xs font-medium text-amber-700 bg-amber-50 border border-amber-200 rounded-lg whitespace-nowrap shrink-0">
                  <AlertCircle className="w-3.5 h-3.5" />
                  有未保存的改动
                </span>
              )}
              <button
                type="button"
                disabled={savingBeats}
                onClick={() => void handleSaveBeats(false)}
                className={`inline-flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-medium rounded-lg whitespace-nowrap shrink-0 cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed ${
                  tableDirty
                    ? 'text-blue-700 bg-blue-50 border border-blue-300 hover:bg-blue-100'
                    : 'text-slate-700 bg-white border border-slate-200 hover:bg-slate-50'
                }`}
              >
                {savingBeats ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <FileText className="w-3.5 h-3.5" />
                )}
                保存解说稿
              </button>
              <button
                type="button"
                disabled={savingBeats}
                onClick={() => void handleSaveBeats(true)}
                className="inline-flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 whitespace-nowrap shrink-0 cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {savingBeats ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <RotateCcw className="w-3.5 h-3.5" />
                )}
                保存并从 Stage 5 重跑配音
              </button>
            </div>
          )}
        </div>

        {activeTab === 'script' && saveHint && (
          <div className="mt-2 flex items-start gap-2 px-3 py-2 text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-lg">
            <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
            <span>{saveHint}</span>
          </div>
        )}

        {activeTab === 'script' && autoPacingSummary && (
          <div className="mt-2 flex items-start gap-2 px-3 py-2 text-xs text-emerald-800 bg-emerald-50 border border-emerald-200 rounded-lg">
            <CheckCircle2 className="w-4 h-4 mt-0.5 shrink-0" />
            <div className="space-y-0.5 flex-1">
              {autoPacingSummary.updated_count > 0 ? (
                <>
                  <p>
                    自动节奏留白完成：共调整 {autoPacingSummary.updated_count} 处停顿（跨幕{' '}
                    {autoPacingSummary.scene_end_count} / 转折 {autoPacingSummary.turn_count} / 换气{' '}
                    {autoPacingSummary.breath_count}），已同步 {autoPacingSummary.audio_manifest_synced}{' '}
                    条音频清单。可直接从 S6 重跑，无需重跑 S5 配音。
                  </p>
                  {/* Patch v4.8：一键从 S6 重跑成片（只重建时间轴，0 TTS 费用），避免用户手点步骤条又吃到旧停顿。 */}
                  <button
                    type="button"
                    disabled={rerunningStage !== null}
                    onClick={() => void handleStageRerunClick('s6_timeline')}
                    className="mt-1.5 inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-emerald-600 rounded hover:bg-emerald-700 transition-colors cursor-pointer disabled:opacity-50"
                  >
                    {rerunningStage === 's6_timeline' ? (
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <Film className="w-3.5 h-3.5" />
                    )}
                    立即从 S6 重跑成片（0 TTS 费用）
                  </button>
                </>
              ) : (
                <p>自动节奏留白检查完成：现有停顿已符合叙事节奏规则，未做调整。</p>
              )}
            </div>
          </div>
        )}
        {activeTab === 'script' && autoPacingError && (
          <div className="mt-2 flex items-start gap-2 px-3 py-2 text-xs text-red-700 bg-red-50 border border-red-200 rounded-lg">
            <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
            <span>{autoPacingError}</span>
          </div>
        )}

        {activeTab === 'script' && bgmHint && (
          <div className="mt-2 flex items-start gap-2 px-3 py-2 text-xs text-emerald-800 bg-emerald-50 border border-emerald-200 rounded-lg">
            <CheckCircle2 className="w-4 h-4 mt-0.5 shrink-0" />
            <span>{bgmHint}</span>
          </div>
        )}
        {activeTab === 'script' && bgmError && (
          <div className="mt-2 flex items-start gap-2 px-3 py-2 text-xs text-red-700 bg-red-50 border border-red-200 rounded-lg">
            <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
            <span>{bgmError}</span>
          </div>
        )}

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

            {/* Patch v4.0 Patch 20: 镜头编排（Storyboard）Tab */}
            {activeTab === 'storyboard' && (
              <StoryboardEditor
                episode={episode}
                onJobSubmitted={() => {
                  // 后台任务进度由 App 层定时刷新剧集 stages 驱动，这里无需额外轮询。
                }}
                onSwitchToScriptTab={() => setActiveTab('script')}
                onScenesLoaded={setStoryboardSceneCount}
              />
            )}

            {/* TAB 2: Script & Polyphone Editor */}
            {activeTab === 'script' && (
              <div className="space-y-5">
                {beatSceneGroups.map((group) => {
                  const scene = group.scene;
                  const sceneDurationMs = group.beats.reduce(
                    (sum, beat) => sum + (beat.audio_duration_ms || 0),
                    0
                  );
                  const explicitTrack = scene?.bgm
                    ? bgmLibrary?.tracks.find((track) => track.filename === scene.bgm)
                    : undefined;
                  // 自动匹配：本幕未显式选曲时，按情绪档同名曲目（去后缀）尝试命中，
                  // 与后端 resolve_scene_bgm_path 的 mood 同名匹配口径一致。
                  const autoMatchedTrack =
                    scene && !scene.bgm && scene.bgm_mood
                      ? bgmLibrary?.tracks.find(
                          (track) => track.filename.replace(/\.[^.]+$/, '') === scene.bgm_mood
                        )
                      : undefined;
                  const selectedTrack = explicitTrack ?? autoMatchedTrack;
                  const isAutoMatched = !explicitTrack && !!autoMatchedTrack;
                  return (
                    <section
                      key={group.key}
                      className="bg-white border border-slate-200 rounded-xl overflow-hidden"
                    >
                      {scene && (
                        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3 border-b border-slate-100 bg-slate-50/60">
                          <span className="inline-flex items-center justify-center min-w-[3.5rem] px-2 py-1 rounded-md bg-slate-900 text-white text-xs font-semibold">
                            第 {scene.scene_index} 幕
                          </span>
                          <div className="min-w-[10rem]">
                            <p className="text-sm font-semibold text-slate-800">
                              {scene.title || '未命名幕'}
                              {scene.location && (
                                <span className="ml-2 text-xs font-normal text-slate-500">
                                  · {scene.location}
                                </span>
                              )}
                            </p>
                            <p className="text-[11px] text-slate-500">
                              {group.beats.length} 句 · 配音合计{' '}
                              {(sceneDurationMs / 1000).toFixed(1)}s
                            </p>
                          </div>
                          {(scene.bgm_mood || scene.bgm_suggestion) && (
                            <span
                              className="inline-flex items-center gap-1.5 rounded-full border border-violet-200 bg-violet-50 px-2.5 py-1 text-[11px] text-violet-700 max-w-[24rem]"
                              title={scene.bgm_suggestion}
                            >
                              <Music className="h-3.5 w-3.5 shrink-0" />
                              {scene.bgm_mood && (
                                <span className="font-medium shrink-0">{scene.bgm_mood}</span>
                              )}
                              {scene.bgm_suggestion && (
                                <span className="truncate">{scene.bgm_suggestion}</span>
                              )}
                            </span>
                          )}
                          <div className="flex items-center gap-2 ml-auto">
                            <select
                              value={scene.bgm ?? ''}
                              disabled={sceneBgmSavingId === scene.scene_id}
                              onChange={(event) =>
                                void persistSceneBgm(scene.scene_id, {
                                  bgm: event.target.value === '' ? null : event.target.value,
                                })
                              }
                              title="本幕 BGM：自动按情绪匹配 / 显式静音 / 指定曲目"
                              className="max-w-[11rem] px-2 py-1.5 text-[11px] bg-white border border-slate-200 rounded-lg"
                            >
                              <option value="">自动匹配（按情绪/作品默认）</option>
                              <option value="none">本幕静音（none）</option>
                              {(bgmLibrary?.tracks ?? []).map((track) => (
                                <option key={`${track.scope}-${track.filename}`} value={track.filename}>
                                  {track.scope === 'series' ? '作品' : '全局'}·{track.filename}
                                </option>
                              ))}
                            </select>
                            <select
                              value={scene.bgm_gain_db ?? ''}
                              disabled={sceneBgmSavingId === scene.scene_id}
                              onChange={(event) =>
                                void persistSceneBgm(scene.scene_id, {
                                  bgm_gain_db:
                                    event.target.value === '' ? null : Number(event.target.value),
                                })
                              }
                              title="本幕 BGM 音量增益"
                              className="px-2 py-1.5 text-[11px] bg-white border border-slate-200 rounded-lg"
                            >
                              <option value="">默认音量</option>
                              <option value={-10}>-10 dB</option>
                              <option value={-12}>-12 dB</option>
                              <option value={-14}>-14 dB</option>
                              <option value={-16}>-16 dB</option>
                              <option value={-18}>-18 dB</option>
                            </select>
                            {isAutoMatched && autoMatchedTrack && (
                              <span
                                className="inline-flex items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[11px] text-emerald-700 whitespace-nowrap"
                                title="本幕未显式选曲，按情绪档自动命中的曲目"
                              >
                                自动命中：{autoMatchedTrack.filename}
                              </span>
                            )}
                            {selectedTrack && (
                              <audio
                                controls
                                preload="none"
                                src={selectedTrack.url}
                                className="h-8 w-40"
                                title={isAutoMatched ? '试听自动命中的曲目' : '试听本幕曲目'}
                              />
                            )}
                            {sceneBgmSavingId === scene.scene_id && (
                              <Loader2 className="h-4 w-4 animate-spin text-blue-600" />
                            )}
                          </div>
                        </div>
                      )}
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
                              <th className="py-3 pl-3 pr-5 w-32 text-right">试听 / 插入 / 删</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-slate-100 text-xs align-top">
                            {group.beats.map((beat) => {
                              const panel = episode.panels.find(
                                (p) => p.panel_id === beat.target_panel_ids[0]
                              );
                              const { spokenText, matchedRules } = resolveSpokenText(beat, rules);
                              const voiceInfo = beatVoiceInfo(beat);
                              const roleOptions = roleOptionsFor(beat);
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
                              {config.ttsModel}
                            </div>
                            {/* Patch v3.4：显示「本句配音」（角色 + 音色），并可就地改角色。 */}
                            <div className="mt-1.5 space-y-1">
                              <div className="text-[11px] leading-snug text-slate-600">
                                本句配音：{voiceInfo.label} · {voiceInfo.voice} ·{' '}
                                {voiceInfo.speed.toFixed(2)}x
                                {voiceInfo.pending && (
                                  <span className="text-amber-600">（保存重跑后生效）</span>
                                )}
                                {voiceInfo.speedOverridden && (
                                  <span className="text-slate-400">
                                    （已微调，默认 {voiceInfo.roleSpeed.toFixed(2)}x）
                                  </span>
                                )}
                              </div>
                              <select
                                value={beat.speaker_role ?? ''}
                                onChange={(e) => {
                                  const nextRole = e.target.value;
                                  // 与弹窗走同一套规则：同步 speaker_gender（空值重置 unknown）、
                                  // 清空 voice_override。
                                  const patch = speakerRolePatch(beat, nextRole);
                                  (Object.keys(patch) as Array<keyof ScriptBeat>).forEach(
                                    (key) => {
                                      updateBeatField(
                                        beat.beat_id,
                                        key,
                                        patch[key] as ScriptBeat[typeof key]
                                      );
                                    }
                                  );
                                }}
                                aria-label={`${beat.beat_id} 配音角色`}
                                className="w-full px-2 py-1 text-[11px] bg-white border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
                              >
                                {roleOptions.map((option) => (
                                  <option
                                    key={option.value || '__none__'}
                                    value={option.value}
                                  >
                                    {option.label}
                                  </option>
                                ))}
                              </select>
                              {voiceInfo.hint && (
                                <div className="text-[10px] leading-snug text-slate-400">
                                  {voiceInfo.hint}
                                </div>
                              )}
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
                                    // Patch v4.8：整数 + 0~2000ms 钳位，避免越界值让时间轴算崩。
                                    Math.max(
                                      0,
                                      Math.min(2000, Math.round(Number(e.target.value) || 0))
                                    )
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
                                onClick={() => handleInsertBeat(beat.beat_id)}
                                title="在本格之后插入一条解说句"
                                aria-label={`在 ${beat.beat_id} 之后插入解说句`}
                                className="p-1.5 text-slate-400 hover:text-blue-600 cursor-pointer"
                              >
                                <Plus className="w-3.5 h-3.5" />
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
                    </section>
                  );
                })}
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

      {/* Patch v4.7：作品曲库管理抽屉（情绪关键词 / 试听 / 上传 / 删除） */}
      <BgmLibraryDrawer
        seriesId={seriesId}
        open={bgmDrawerOpen}
        onClose={() => setBgmDrawerOpen(false)}
        onLibraryChange={setBgmLibrary}
      />

      {/* 分镜大图 + 字幕原文编辑（改完自动保存） */}
      <PanelZoomModal
        open={zoomBeatId !== null}
        beats={draftBeats}
        panels={episode.panels}
        activeBeatId={zoomBeatId}
        saveStatus={saveStatus}
        saveMessage={saveMessage}
        roleOptions={zoomBeat ? roleOptionsFor(zoomBeat) : []}
        voiceInfo={
          zoomBeat
            ? beatVoiceInfo(zoomBeat)
            : {
                label: '待解析',
                voice: '—',
                hint: '',
                speed: 1.15,
                roleSpeed: 1.15,
                speedOverridden: false,
              }
        }
        voiceOptions={voiceOptions}
        onClose={handleZoomClose}
        onSelectBeat={handleZoomSelectBeat}
        onChangeNarration={handleZoomNarrationChange}
        onChangeRole={handleZoomRoleChange}
        onChangeEmotion={handleZoomEmotionChange}
        onChangeSpeed={handleZoomSpeedChange}
      />
    </div>
  );
};
