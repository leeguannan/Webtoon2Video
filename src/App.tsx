import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Bell,
  BellOff,
  BookOpen,
  Brain,
  CheckCircle2,
  Download,
  Library,
  Play,
  Plus,
  Sliders,
  Video,
  Volume2,
  VolumeX,
  X,
} from 'lucide-react';
import type {
  ArcSummary,
  AutoPacingResponse,
  CharacterProfile,
  EpisodeDigest,
  EpisodeItem,
  EpisodeRenderOptions,
  EpisodeResetResponse,
  PlotThread,
  PronunciationRule,
  RunMode,
  ScriptBeat,
  SeriesItem,
  StageId,
  StageStatus,
  StudioConfig,
  VoiceOption,
} from './types/studio';
import { CatalogView } from './components/CatalogView';
import { EpisodeStudioView } from './components/EpisodeStudioView';
import { ConfigDictView } from './components/ConfigDictView';
import { MemoryView } from './components/MemoryView';
import {
  getDesktopNotificationPermission,
  loadRenderNotifyPrefs,
  playRenderCompleteChime,
  playRenderFailedTone,
  primeNotificationAudio,
  requestDesktopNotificationPermission,
  saveRenderNotifyPrefs,
  showDesktopNotification,
  type RenderNotifyPrefs,
} from './utils/notifications';

type NavView = 'catalog' | 'studio' | 'memory' | 'config';

const STAGE_ORDER: StageId[] = [
  's0_catalog',
  's1_crawler',
  's2_segmenter',
  's3_vision',
  's4_scriptwriter',
  's5_tts',
  's6_timeline',
  's7_renderer',
];

const EPISODE_STATUSES: EpisodeItem['status'][] = [
  'idle',
  'queued',
  'running',
  'done',
  'failed',
];

const STAGE_STATUSES: StageStatus[] = ['pending', 'running', 'done', 'failed'];

const API_BASE = '/api';

/** 后端未就绪时的兜底配置（仅用于渲染，不回写后端）。 */
const FALLBACK_CONFIG: StudioConfig = {
  bailianBaseUrl: '',
  visionModel: '',
  scriptModel: '',
  storyModel: '',
  ttsModel: '',
  ttsVoice: 'Cherry',
  maxImageLongEdge: 1280,
  aspectRatio: '9:16',
  bgmDuckingDb: -18,
};

/* ------------------------------------------------------------------ *
 * 后端 REST 契约（backend/src/web/server.py）
 * ------------------------------------------------------------------ */

type RawStoryBible = {
  series_id: string;
  narration_style: string;
  characters: CharacterProfile[];
  plot_threads: PlotThread[];
  arc_summaries: ArcSummary[];
  recent_digests: EpisodeDigest[];
  snapshots_ep_nos: number[];
};

type RawEpisode = {
  episode_no: number;
  episode_id: string;
  title?: string;
  publish_date?: string;
  viewer_url?: string;
  is_locked?: boolean;
  selected?: boolean;
  current_stage?: string;
  status?: string;
  video_duration_ms?: number;
  stages?: EpisodeItem['stages'];
  panels?: EpisodeItem['panels'];
  beats?: EpisodeItem['beats'];
  logs?: string[];
};

type RawSeries = {
  series_id: string;
  series_title: string;
  list_url?: string;
  narration_voice_override?: string | null;
  story_bible: RawStoryBible;
  episodes: RawEpisode[];
};

type RawConfig = {
  bailianBaseUrl: string;
  visionModel: string;
  scriptModel: string;
  /** Patch v3.3 Patch 15：Stage 4A 说书体模型与候选清单。 */
  storyModel?: string;
  storyModelOptions?: string[];
  scriptModelOptions?: string[];
  ttsModel: string;
  ttsVoice: string;
  /** Patch v3.4 Patch 16：TTS 模型候选 + 两套供应商的音色清单（供下拉与试听）。 */
  ttsModelOptions?: string[];
  ttsVoiceOptions?: VoiceOption[];
  minimaxVoiceOptions?: VoiceOption[];
  maxImageLongEdge: number;
  aspectRatio: string;
  bgmDuckingDb: number;
  timeoutS?: number;
  maxRetries?: number;
};

type RawRule = {
  id: string;
  series_id?: string | null;
  source: string;
  replacement: string;
  note?: string;
};

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      if (typeof data?.detail === 'string') {
        detail = data.detail;
      } else if (Array.isArray(data?.detail) && data.detail.length > 0) {
        // FastAPI 422 校验错误是对象数组，压成一行人话。
        detail = data.detail
          .map((item: { loc?: unknown[]; msg?: string }) => {
            const field = Array.isArray(item?.loc) ? item.loc.slice(-1)[0] : '';
            return field ? `${field}: ${item?.msg ?? ''}` : String(item?.msg ?? '');
          })
          .join('；');
      }
    } catch {
      /* 响应体不是 JSON 时保留状态码 */
    }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

async function downloadFromApi(path: string, fallbackName: string): Promise<void> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      if (data && typeof data.detail === 'string') detail = data.detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  const blob = await res.blob();
  const disposition = res.headers.get('content-disposition') ?? '';
  const matched = /filename="?([^";]+)"?/i.exec(disposition);
  const filename = matched ? decodeURIComponent(matched[1]) : fallbackName;
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(objectUrl);
}

/* ------------------------------------------------------------------ *
 * 后端响应 → 前端类型
 * ------------------------------------------------------------------ */

function toStageId(value: string | undefined): StageId {
  return STAGE_ORDER.includes(value as StageId) ? (value as StageId) : 's0_catalog';
}

function toEpisodeStatus(value: string | undefined): EpisodeItem['status'] {
  return EPISODE_STATUSES.includes(value as EpisodeItem['status'])
    ? (value as EpisodeItem['status'])
    : 'idle';
}

function toStageStatus(value: string | undefined): StageStatus {
  return STAGE_STATUSES.includes(value as StageStatus)
    ? (value as StageStatus)
    : 'pending';
}

function sameJson(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** 从 episode_id（`{series_id}_ep_001`）反推 series_id。 */
function seriesIdOf(episodeId: string): string {
  const idx = episodeId.lastIndexOf('_ep_');
  return idx > 0 ? episodeId.slice(0, idx) : episodeId;
}

/**
 * 把后端 series_item 转成前端 SeriesItem。
 * `prev` 用于保持数组引用稳定，避免轮询时打断审核台里正在编辑的草稿。
 */
function toSeriesItem(raw: RawSeries, prev?: SeriesItem): SeriesItem {
  const prevEpisodes = prev?.episodes ?? [];
  const episodes: EpisodeItem[] = (raw.episodes ?? []).map((item) => {
    const next: EpisodeItem = {
      episode_no: item.episode_no,
      episode_id: item.episode_id,
      title: item.title ?? '',
      publish_date: item.publish_date ?? '',
      viewer_url: item.viewer_url ?? '',
      is_locked: !!item.is_locked,
      selected: false,
      current_stage: toStageId(item.current_stage),
      status: toEpisodeStatus(item.status),
      video_duration_ms: item.video_duration_ms ?? 0,
      stages: item.stages ?? [],
      panels: item.panels ?? [],
      beats: item.beats ?? [],
      logs: item.logs ?? [],
    };
    const old = prevEpisodes.find((ep) => ep.episode_id === next.episode_id);
    if (!old) return next;
    return {
      ...next,
      stages: sameJson(old.stages, next.stages) ? old.stages : next.stages,
      panels: sameJson(old.panels, next.panels) ? old.panels : next.panels,
      beats: sameJson(old.beats, next.beats) ? old.beats : next.beats,
    };
  });

  const bible: RawStoryBible = raw.story_bible ?? {
    series_id: raw.series_id,
    narration_style: '',
    characters: [],
    plot_threads: [],
    arc_summaries: [],
    recent_digests: [],
    snapshots_ep_nos: [],
  };

  return {
    series_id: raw.series_id,
    series_title: raw.series_title || raw.series_id,
    list_url: raw.list_url ?? '',
    narration_voice_override: raw.narration_voice_override ?? undefined,
    story_bible: {
      series_id: bible.series_id ?? raw.series_id,
      narration_style: bible.narration_style ?? '',
      characters: bible.characters ?? [],
      plot_threads: bible.plot_threads ?? [],
      arc_summaries: bible.arc_summaries ?? [],
      recent_digests: bible.recent_digests ?? [],
      snapshots_ep_nos: bible.snapshots_ep_nos ?? [],
    },
    episodes,
  };
}

function toStudioConfig(raw: RawConfig): StudioConfig {
  return {
    bailianBaseUrl: raw.bailianBaseUrl,
    visionModel: raw.visionModel,
    scriptModel: raw.scriptModel,
    storyModel: raw.storyModel ?? '',
    storyModelOptions: raw.storyModelOptions,
    scriptModelOptions: raw.scriptModelOptions,
    ttsModel: raw.ttsModel,
    ttsVoice: raw.ttsVoice,
    ttsModelOptions: raw.ttsModelOptions,
    ttsVoiceOptions: raw.ttsVoiceOptions,
    minimaxVoiceOptions: raw.minimaxVoiceOptions,
    maxImageLongEdge: raw.maxImageLongEdge,
    aspectRatio: '9:16',
    bgmDuckingDb: raw.bgmDuckingDb,
  };
}

function toPronunciationRule(raw: RawRule): PronunciationRule {
  return {
    id: raw.id,
    series_id: raw.series_id ?? undefined,
    source: raw.source,
    replacement: raw.replacement,
    note: raw.note ?? '',
  };
}

/* ------------------------------------------------------------------ */

export default function App() {
  const [navView, setNavView] = useState<NavView>('catalog');
  const [seriesList, setSeriesList] = useState<SeriesItem[]>([]);
  const [activeSeriesId, setActiveSeriesId] = useState<string>('');
  const [activeEpisodeId, setActiveEpisodeId] = useState<string>('');
  const [runMode, setRunMode] = useState<RunMode>('phase1_only');
  const [config, setConfig] = useState<StudioConfig | null>(null);
  const [rules, setRules] = useState<PronunciationRule[]>([]);
  const [toast, setToast] = useState<string | null>(null);
  const [showAddSeries, setShowAddSeries] = useState(false);
  const [newSeriesTitle, setNewSeriesTitle] = useState('');
  const [newSeriesUrl, setNewSeriesUrl] = useState('');
  const [booted, setBooted] = useState(false);
  /** 后端不保存勾选态，这里单独维护。 */
  const [selectedMap, setSelectedMap] = useState<Record<string, boolean>>({});
  /** Patch v4.10：成片完成的声音/桌面通知偏好（localStorage 持久化）。 */
  const [notifyPrefs, setNotifyPrefs] = useState<RenderNotifyPrefs>(() => loadRenderNotifyPrefs());
  /** Patch v4.10：当前浏览器的桌面通知权限。 */
  const [desktopPermission, setDesktopPermission] = useState<NotificationPermission | 'unsupported'>(
    () => getDesktopNotificationPermission()
  );

  const timersRef = useRef<number[]>([]);
  const seriesRef = useRef<SeriesItem[]>([]);
  const pollTickRef = useRef(0);
  /**
   * Patch v4.10：通知偏好的实时快照。
   * 状态跃迁检测的 effect 只依赖 [booted, seriesList]，
   * 用 ref 读取最新偏好，避免用户开关提醒时把 effect 重新触发一遍。
   */
  const notifyPrefsRef = useRef<RenderNotifyPrefs>(notifyPrefs);
  /** Patch v4.10：各话次 s7_renderer 的上一次状态，用于检测跃迁。 */
  const prevS7StatusRef = useRef<Map<string, StageStatus>>(new Map());
  /** Patch v4.10：是否已经建立过基线（首次建立基线时不发任何通知）。 */
  const s7BaselineReadyRef = useRef(false);
  /** Patch v4.10：标签页标题闪烁定时器与原始标题。 */
  const titleFlashTimerRef = useRef<number | null>(null);
  const originalTitleRef = useRef<string>('');

  useEffect(() => {
    seriesRef.current = seriesList;
  }, [seriesList]);

  useEffect(() => {
    return () => {
      timersRef.current.forEach((t) => window.clearTimeout(t));
    };
  }, []);

  // 用 useCallback 固定引用：showToast → reportError → 首屏加载 effect 的依赖链，
  // 若每次渲染都换新引用会导致首屏 effect 反复触发、无限重渲染。
  const showToast = useCallback((msg: string) => {
    setToast(msg);
    const id = window.setTimeout(() => {
      setToast((cur) => (cur === msg ? null : cur));
    }, 3600);
    timersRef.current.push(id);
  }, []);

  const reportError = useCallback(
    (prefix: string, err: unknown) => {
      const message = err instanceof Error ? err.message : String(err);
      showToast(`${prefix}：${message}`);
    },
    [showToast]
  );

  /* -------------------- 数据加载 -------------------- */

  const applySeries = useCallback((raw: RawSeries) => {
    setSeriesList((prev) => {
      const item = toSeriesItem(raw, prev.find((s) => s.series_id === raw.series_id));
      const index = prev.findIndex((s) => s.series_id === item.series_id);
      if (index === -1) return [...prev, item];
      const next = prev.slice();
      next[index] = item;
      return next;
    });
  }, []);

  const refreshSeries = useCallback(
    async (seriesId: string) => {
      if (!seriesId) return;
      try {
        const raw = await apiFetch<RawSeries>(`/series/${encodeURIComponent(seriesId)}`);
        applySeries(raw);
      } catch {
        /* 轮询失败不打扰用户 */
      }
    },
    [applySeries]
  );

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [seriesRaw, configRaw, rulesRaw] = await Promise.all([
          apiFetch<RawSeries[]>('/series'),
          apiFetch<RawConfig>('/config'),
          apiFetch<{ rules: RawRule[] }>('/pronunciation-rules'),
        ]);
        if (cancelled) return;
        const items = (seriesRaw ?? []).map((raw) => toSeriesItem(raw));
        setSeriesList(items);
        setConfig(toStudioConfig(configRaw));
        setRules((rulesRaw?.rules ?? []).map(toPronunciationRule));
        if (items.length > 0) {
          setActiveSeriesId((current) =>
            items.some((s) => s.series_id === current) ? current : items[0].series_id
          );
          setActiveEpisodeId((current) =>
            items.some((s) => s.episodes.some((e) => e.episode_id === current))
              ? current
              : items[0].episodes[0]?.episode_id ?? ''
          );
        }
      } catch (err) {
        if (!cancelled) reportError('加载工作区失败', err);
      } finally {
        if (!cancelled) setBooted(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reportError]);

  /* 轮询：仅在有任务在跑时高频刷新，空闲时每 20 秒兜底一次 */
  useEffect(() => {
    const id = window.setInterval(() => {
      const seriesId = activeSeriesId;
      if (!seriesId) return;
      pollTickRef.current += 1;
      const busy = seriesRef.current.some(
        (s) =>
          s.series_id === seriesId &&
          s.episodes.some((e) => e.status === 'running' || e.status === 'queued')
      );
      if (busy || pollTickRef.current % 4 === 0) {
        void refreshSeries(seriesId);
      }
    }, 5000);
    return () => window.clearInterval(id);
  }, [activeSeriesId, refreshSeries]);

  /* -------------------- 本地选中态（后端不保存） -------------------- */

  const viewSeriesList = useMemo(
    () =>
      seriesList.map((s) => ({
        ...s,
        episodes: s.episodes.map((ep) => ({
          ...ep,
          selected: !!selectedMap[ep.episode_id],
        })),
      })),
    [seriesList, selectedMap]
  );

  const activeSeries =
    viewSeriesList.find((s) => s.series_id === activeSeriesId) ?? viewSeriesList[0];
  const activeEpisode =
    activeSeries?.episodes.find((e) => e.episode_id === activeEpisodeId) ??
    activeSeries?.episodes[0];

  const patchEpisode = useCallback(
    (episodeId: string, updater: (ep: EpisodeItem) => EpisodeItem) => {
      setSeriesList((prev) =>
        prev.map((series) => ({
          ...series,
          episodes: series.episodes.map((ep) =>
            ep.episode_id === episodeId ? updater(ep) : ep
          ),
        }))
      );
    },
    []
  );

  /**
   * Patch v4.5：保存单话封页/结尾片包选项并同步本地状态。
   * 后端写入 episodes/ep_XXX/render_options.json，重跑 S7 即生效。
   */
  const handleUpdateEpisodeRenderOptions = useCallback(
    async (episodeId: string, options: EpisodeRenderOptions) => {
      const result = await apiFetch<{
        episode_id: string;
        render_options: EpisodeRenderOptions;
      }>(`/episodes/${encodeURIComponent(episodeId)}/render-options`, {
        method: 'PUT',
        body: JSON.stringify(options),
      });
      patchEpisode(episodeId, (ep) => ({ ...ep, render_options: result.render_options }));
    },
    [patchEpisode]
  );

  /* -------------------- SSE 实时日志与状态 -------------------- */

  useEffect(() => {
    if (!activeEpisodeId) return;
    const source = new EventSource(
      `${API_BASE}/episodes/${encodeURIComponent(activeEpisodeId)}/events`
    );

    source.addEventListener('log', (event) => {
      try {
        const payload = JSON.parse((event as MessageEvent).data);
        const message = String(payload.message ?? '');
        const episodeId = String(payload.episode_id ?? activeEpisodeId);
        if (!message) return;
        patchEpisode(episodeId, (ep) =>
          ep.logs.includes(message) ? ep : { ...ep, logs: [...ep.logs, message] }
        );
      } catch {
        /* 忽略非法事件 */
      }
    });

    source.addEventListener('state', (event) => {
      try {
        const payload = JSON.parse((event as MessageEvent).data);
        const episodeId = String(payload.episode_id ?? activeEpisodeId);
        patchEpisode(episodeId, (ep) => {
          const stageStates = (payload.stages ?? {}) as Record<
            string,
            { status?: string; duration_sec?: number }
          >;
          return {
            ...ep,
            status: toEpisodeStatus(payload.status),
            current_stage: toStageId(payload.current_stage),
            stages: ep.stages.map((st) => {
              const info = stageStates[st.id];
              if (!info) return st;
              return {
                ...st,
                status: toStageStatus(info.status),
                durationSec: info.duration_sec ?? st.durationSec,
              };
            }),
          };
        });
        if (payload.status === 'done' || payload.status === 'failed') {
          void refreshSeries(seriesIdOf(episodeId));
        }
      } catch {
        /* 忽略非法事件 */
      }
    });

    return () => source.close();
  }, [activeEpisodeId, patchEpisode, refreshSeries]);

  /* -------------------- Patch v4.10：成片完成提醒 -------------------- */

  /** 通知偏好变更：state + ref + localStorage 三处同步。 */
  const updateNotifyPrefs = useCallback((patch: Partial<RenderNotifyPrefs>) => {
    const next = { ...notifyPrefsRef.current, ...patch };
    notifyPrefsRef.current = next;
    setNotifyPrefs(next);
    saveRenderNotifyPrefs(next);
  }, []);

  /** 停止标签页标题闪烁并还原原标题。 */
  const stopTitleFlash = useCallback(() => {
    if (titleFlashTimerRef.current === null) return;
    window.clearInterval(titleFlashTimerRef.current);
    titleFlashTimerRef.current = null;
    document.title = originalTitleRef.current;
    originalTitleRef.current = '';
  }, []);

  /** 在标签页标题上轮播闪烁提示（页面在后台时用户看不见 toast）。 */
  const startTitleFlash = useCallback(
    (message: string) => {
      stopTitleFlash();
      originalTitleRef.current = document.title;
      document.title = message;
      let showMessage = false;
      titleFlashTimerRef.current = window.setInterval(() => {
        showMessage = !showMessage;
        document.title = showMessage ? message : originalTitleRef.current;
      }, 700);
    },
    [stopTitleFlash]
  );

  /* 偏好 state → ref 同步（切换开关时立即生效，且不触发状态跃迁 effect）。 */
  useEffect(() => {
    notifyPrefsRef.current = notifyPrefs;
  }, [notifyPrefs]);

  /* 首次交互解锁音频：浏览器要求音频必须在用户手势之后才能播放。 */
  useEffect(() => {
    const unlock = () => {
      primeNotificationAudio();
    };
    window.addEventListener('pointerdown', unlock, { once: true, capture: true });
    window.addEventListener('keydown', unlock, { once: true, capture: true });
    return () => {
      window.removeEventListener('pointerdown', unlock, true);
      window.removeEventListener('keydown', unlock, true);
    };
  }, []);

  /* 切回前台时停止标题闪烁；组件卸载时同样清理。 */
  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        stopTitleFlash();
        /* 用户可能在浏览器设置里改过通知权限，回到前台时同步一次。 */
        setDesktopPermission(getDesktopNotificationPermission());
      }
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      document.removeEventListener('visibilitychange', onVisibilityChange);
      stopTitleFlash();
    };
  }, [stopTitleFlash]);

  /* S7 状态跃迁检测：只认「本次会话里从非 done 变成 done」的跃迁。 */
  useEffect(() => {
    if (!booted) return;

    const current = new Map<string, StageStatus>();
    seriesList.forEach((series) => {
      series.episodes.forEach((ep) => {
        const stage = ep.stages.find((s) => s.id === 's7_renderer');
        if (stage) current.set(ep.episode_id, stage.status);
      });
    });

    /* 首次就绪：只建立基线，绝不对历史已完成的话次发通知。 */
    if (!s7BaselineReadyRef.current) {
      prevS7StatusRef.current = current;
      s7BaselineReadyRef.current = true;
      return;
    }

    const prefs = notifyPrefsRef.current;
    seriesList.forEach((series) => {
      series.episodes.forEach((ep) => {
        const stage = ep.stages.find((s) => s.id === 's7_renderer');
        if (!stage) return;
        const prev = prevS7StatusRef.current.get(ep.episode_id);
        const now = stage.status;

        const finished = prev !== undefined && prev !== 'done' && now === 'done';
        const failed = prev === 'running' && now === 'failed';
        if (!finished && !failed) return;

        if (prefs.soundEnabled) {
          if (finished) playRenderCompleteChime();
          else playRenderFailedTone();
        }

        const label = `${series.series_title} · ${ep.title}`;
        if (finished) {
          showToast(`成片已生成：${label}，可试看或导出`);
        } else {
          showToast(`成片合成失败：${label}，请到审核台查看日志`);
        }

        if (prefs.desktopEnabled && getDesktopNotificationPermission() === 'granted') {
          showDesktopNotification({
            title: finished ? '成片已生成' : '成片合成失败',
            body: finished
              ? `${label}，可试看或导出`
              : `${label}，请到审核台查看日志`,
            tag: `w2v-s7-${ep.episode_id}`,
            onClick: () => {
              setActiveSeriesId(series.series_id);
              setActiveEpisodeId(ep.episode_id);
              setNavView('studio');
            },
          });
        }

        if (document.hidden) {
          startTitleFlash(
            finished ? `【成片完成】${ep.title}` : `【合成失败】${ep.title}`
          );
        }
      });
    });

    prevS7StatusRef.current = current;
  }, [booted, seriesList, showToast, startTitleFlash]);

  /** 声音提醒开关：打开时当场播一声确认音，让用户确认音量正常。 */
  const handleToggleSound = useCallback(() => {
    const nextOn = !notifyPrefsRef.current.soundEnabled;
    updateNotifyPrefs({ soundEnabled: nextOn });
    if (nextOn) {
      primeNotificationAudio();
      playRenderCompleteChime();
      showToast('已开启成片完成提示音');
    } else {
      showToast('已关闭成片完成提示音');
    }
  }, [updateNotifyPrefs, showToast]);

  /** 桌面通知开关：首次点击需要向浏览器申请权限。 */
  const handleToggleDesktop = useCallback(async () => {
    const permission = getDesktopNotificationPermission();
    setDesktopPermission(permission);

    if (permission === 'unsupported') {
      showToast('当前浏览器不支持桌面通知，可改用提示音与页内提醒');
      return;
    }
    if (permission === 'denied') {
      showToast('浏览器已拒绝通知权限，请在地址栏站点设置中允许通知后重试');
      return;
    }
    if (permission === 'default') {
      const result = await requestDesktopNotificationPermission();
      setDesktopPermission(result);
      if (result === 'granted') {
        updateNotifyPrefs({ desktopEnabled: true });
        primeNotificationAudio();
        playRenderCompleteChime();
        showToast('桌面通知已开启，成片完成时会提醒你');
      } else {
        updateNotifyPrefs({ desktopEnabled: false });
        showToast('未获得通知权限，成片完成时仍会用页内提示与提示音提醒');
      }
      return;
    }
    /* 已授权：在开/关之间切换。 */
    const nextOn = !notifyPrefsRef.current.desktopEnabled;
    updateNotifyPrefs({ desktopEnabled: nextOn });
    showToast(nextOn ? '已开启桌面通知' : '已关闭桌面通知');
  }, [updateNotifyPrefs, showToast]);

  /* -------------------- 操作回调 -------------------- */

  const handleSelectSeries = (seriesId: string) => {
    setActiveSeriesId(seriesId);
    const target = seriesList.find((s) => s.series_id === seriesId);
    if (target && target.episodes[0]) {
      setActiveEpisodeId(target.episodes[0].episode_id);
    }
  };

  const handleCreateSeries = async (e: React.FormEvent) => {
    e.preventDefault();
    const source = newSeriesUrl.trim();
    if (!source) {
      showToast('请填写 WEBTOON 列表 URL 或本地目录路径');
      return;
    }
    try {
      const raw = await apiFetch<RawSeries>('/series', {
        method: 'POST',
        body: JSON.stringify({ url: source, title: newSeriesTitle.trim() }),
      });
      const item = toSeriesItem(raw);
      applySeries(raw);
      setActiveSeriesId(item.series_id);
      setActiveEpisodeId(item.episodes[0]?.episode_id ?? '');
      setNewSeriesTitle('');
      setNewSeriesUrl('');
      setShowAddSeries(false);
      setNavView('catalog');
      showToast(`已创建独立漫画工作区：${item.series_title} (${item.series_id})`);
    } catch (err) {
      reportError('导入漫画失败', err);
    }
  };

  const handleParseCatalogUrl = async (url: string) => {
    const source = url.trim();
    if (!source) {
      showToast('请填写 WEBTOON 列表 URL 或本地目录路径');
      return;
    }
    showToast('正在重新索引目录…');
    try {
      const raw = await apiFetch<RawSeries>('/series', {
        method: 'POST',
        body: JSON.stringify({ url: source }),
      });
      applySeries(raw);
      const item = toSeriesItem(raw);
      setActiveSeriesId(item.series_id);
      setActiveEpisodeId(item.episodes[0]?.episode_id ?? '');
      const count = raw.episodes?.length ?? 0;
      showToast(`Stage 0 已重新索引目录：共发现 ${count} 话`);
    } catch (err) {
      reportError('目录索引失败', err);
    }
  };

  const handleToggleEpisode = (episodeId: string) => {
    setSelectedMap((prev) => ({ ...prev, [episodeId]: !prev[episodeId] }));
  };

  const handleApplyRangeSelection = (epNos: number[]) => {
    if (!activeSeries) return;
    const noSet = new Set(epNos);
    setSelectedMap((prev) => {
      const next = { ...prev };
      activeSeries.episodes.forEach((ep) => {
        next[ep.episode_id] = !ep.is_locked && noSet.has(ep.episode_no);
      });
      return next;
    });
    showToast(`已按区间勾选 ${epNos.join(', ')} 话（自动排除锁定话次）`);
  };

  const handleStartBatch = async () => {
    if (!activeSeries) return;
    const targets = activeSeries.episodes
      .filter((e) => e.selected && !e.is_locked)
      .sort((a, b) => a.episode_no - b.episode_no);

    if (targets.length === 0) return;

    try {
      await apiFetch<{ jobs: unknown[] }>('/jobs/batch', {
        method: 'POST',
        body: JSON.stringify({
          items: targets.map((ep) => ({
            series_id: activeSeries.series_id,
            episode_no: ep.episode_no,
          })),
          mode: runMode,
        }),
      });
      showToast(
        `已将《${activeSeries.series_title}》${targets.length} 话加入本作品调度队列`
      );
      void refreshSeries(activeSeries.series_id);
    } catch (err) {
      reportError('下发批量任务失败', err);
    }
  };

  const rerunEpisode = async (
    episodeId: string,
    fromStage: StageId,
    mode: RunMode
  ) => {
    try {
      await apiFetch(`/episodes/${encodeURIComponent(episodeId)}/rerun`, {
        method: 'POST',
        body: JSON.stringify({ from_stage: fromStage, mode }),
      });
      showToast(
        `${episodeId} 已从 ${fromStage} 重新排队（${
          mode === 'phase1_only' ? 'Phase 1' : '全流程'
        }）`
      );
      void refreshSeries(seriesIdOf(episodeId));
    } catch (err) {
      reportError('重跑失败', err);
    }
  };

  const handleRerunFromStage = (episodeId: string, stageId: StageId) => {
    const idx = STAGE_ORDER.indexOf(stageId);
    const mode: RunMode = idx >= 5 ? 'full_video' : runMode;
    // Patch v4.8：把 Promise 交回给审核台，便于它在重跑前先保存草稿 / 提示升级起点。
    return rerunEpisode(episodeId, stageId, mode);
  };

  /** 按步向后级联清理：清空该步及下游的旧产物与完成标记，状态回到「待执行」。 */
  const handleResetFromStage = async (
    episodeId: string,
    stageId: StageId,
    includeHumanStoryboard = false
  ) => {
    try {
      const result = await apiFetch<EpisodeResetResponse>(
        `/episodes/${encodeURIComponent(episodeId)}/reset`,
        {
          method: 'POST',
          body: JSON.stringify({
            from_stage: stageId,
            include_human_storyboard: includeHumanStoryboard,
          }),
        }
      );
      patchEpisode(episodeId, (ep) => ({
        ...ep,
        stages: result.stages ?? ep.stages,
        panels: [],
        beats: [],
        video_duration_ms: 0,
        logs: result.logs ?? ep.logs,
      }));
      showToast(`已清理 ${stageId} 起的旧产物并重置为待执行`);
      void refreshSeries(seriesIdOf(episodeId));
    } catch (err) {
      reportError('清理重置失败', err);
    }
  };

  const handleTogglePanelBlank = async (episodeId: string, panelId: string) => {
    const episode = seriesRef.current
      .flatMap((s) => s.episodes)
      .find((ep) => ep.episode_id === episodeId);
    const panel = episode?.panels.find((p) => p.panel_id === panelId);
    if (!panel) return;
    const next = !panel.is_transition_blank;
    try {
      await apiFetch(
        `/episodes/${encodeURIComponent(episodeId)}/panels/${encodeURIComponent(
          panelId
        )}`,
        {
          method: 'PATCH',
          body: JSON.stringify({ is_transition_blank: next }),
        }
      );
      patchEpisode(episodeId, (ep) => ({
        ...ep,
        panels: ep.panels.map((p) =>
          p.panel_id === panelId ? { ...p, is_transition_blank: next } : p
        ),
      }));
      showToast(`已更新分镜 ${panelId} 剪辑状态`);
    } catch (err) {
      reportError('更新分镜状态失败', err);
    }
  };

  /**
   * 保存解说稿。
   *
   * 返回是否落盘成功：审核台据此决定要不要清掉「有未保存改动」标记，
   * 保存失败时必须保留草稿与标记，别让用户以为改好了其实没存上。
   */
  const handleUpdateBeats = async (
    episodeId: string,
    beats: ScriptBeat[],
    triggerFromStage5: boolean
  ): Promise<boolean> => {
    try {
      await apiFetch(`/episodes/${encodeURIComponent(episodeId)}/script`, {
        method: 'PUT',
        body: JSON.stringify({
          beats,
          from_stage: triggerFromStage5 ? 's5_tts' : undefined,
          mode: 'full_video',
        }),
      });
      patchEpisode(episodeId, (ep) => ({
        ...ep,
        beats,
        logs: [
          ...ep.logs,
          `[人工审核] 已保存 ${beats.length} 条解说与多音字纠偏配置`,
        ],
      }));
      showToast(
        triggerFromStage5 ? '已保存脚本修改，并从 Stage 5 重新出片' : '已保存脚本修改'
      );
      return true;
    } catch (err) {
      reportError('保存脚本失败', err);
      return false;
    }
  };

  // 分镜大图弹窗的字幕原文自动保存：静默落盘（不弹 toast、不重跑配音），失败抛给弹窗展示
  const handleAutoSaveBeats = async (
    episodeId: string,
    beats: ScriptBeat[]
  ) => {
    await apiFetch(`/episodes/${encodeURIComponent(episodeId)}/script`, {
      method: 'PUT',
      body: JSON.stringify({
        beats,
        mode: 'full_video',
      }),
    });
    patchEpisode(episodeId, (ep) => ({
      ...ep,
      beats,
    }));
  };

  /**
   * Patch v4.6：一键叙事节奏留白。后端按确定性规则改写 pause_after_ms，
   * 同步 script.json 与 audio_manifest.json，返回最新 beats 与调整明细；
   * 成功后本地不可变更新该话 beats，调用方据此刷新草稿并提示只重跑 S6→S7。
   * 失败时上报错误并返回 null，由组件展示内联错误，不使用浏览器弹窗。
   */
  const handleAutoPacing = useCallback(
    async (episodeId: string): Promise<AutoPacingResponse | null> => {
      try {
        const result = await apiFetch<AutoPacingResponse>(
          `/episodes/${encodeURIComponent(episodeId)}/script/auto-pacing`,
          { method: 'POST' }
        );
        patchEpisode(episodeId, (ep) => ({
          ...ep,
          beats: result.beats as ScriptBeat[],
        }));
        return result;
      } catch (err) {
        reportError('自动节奏留白失败', err);
        return null;
      }
    },
    [patchEpisode]
  );

  const handleExportEpisode = async (
    episodeId: string,
    format: 'mp4' | 'ass' | 'script'
  ) => {
    const fallback =
      format === 'mp4'
        ? `${episodeId}.mp4`
        : format === 'ass'
        ? `${episodeId}.ass`
        : `${episodeId}_script.md`;
    try {
      await downloadFromApi(
        `/episodes/${encodeURIComponent(episodeId)}/export?format=${format}`,
        fallback
      );
      showToast(`已导出 ${format.toUpperCase()}：${fallback}`);
    } catch (err) {
      reportError('导出失败', err);
    }
  };

  const handleExportSeriesZip = async (seriesId: string) => {
    const target = seriesList.find((s) => s.series_id === seriesId);
    if (!target) return;
    const readyEps = target.episodes.filter(
      (e) => e.stages.find((s) => s.id === 's7_renderer')?.status === 'done'
    );
    try {
      await downloadFromApi(
        `/series/${encodeURIComponent(seriesId)}/export-zip`,
        `${seriesId}_videos.zip`
      );
      showToast(`已打包下载《${target.series_title}》${readyEps.length} 话 9:16 成片`);
    } catch (err) {
      reportError('批量导出失败', err);
    }
  };

  /* -------------------- 记忆体 -------------------- */

  const saveMemory = async (
    seriesId: string,
    payload: Record<string, unknown>,
    successMessage: string
  ) => {
    try {
      const bible = await apiFetch<RawStoryBible>(
        `/series/${encodeURIComponent(seriesId)}/memory`,
        { method: 'PUT', body: JSON.stringify(payload) }
      );
      setSeriesList((prev) =>
        prev.map((s) =>
          s.series_id === seriesId
            ? { ...s, story_bible: { ...s.story_bible, ...bible } }
            : s
        )
      );
      showToast(successMessage);
    } catch (err) {
      reportError('更新记忆体失败', err);
    }
  };

  const handleAddCharacter = (sid: string, character: CharacterProfile) => {
    const current = seriesList.find((s) => s.series_id === sid);
    if (!current) return;
    void saveMemory(
      sid,
      { characters: [...current.story_bible.characters, character] },
      `已收录角色「${character.name}」至 ${sid} 记忆体`
    );
  };

  const handleAddPlotThread = (sid: string, thread: Omit<PlotThread, 'thread_id'>) => {
    const current = seriesList.find((s) => s.series_id === sid);
    if (!current) return;
    const next: PlotThread = { ...thread, thread_id: `pt_${Date.now()}` };
    void saveMemory(
      sid,
      { plot_threads: [...current.story_bible.plot_threads, next] },
      `已记录伏笔「${thread.title}」`
    );
  };

  const handleToggleThreadStatus = (sid: string, threadId: string) => {
    const current = seriesList.find((s) => s.series_id === sid);
    if (!current) return;
    // 回收伏笔时记录当前话次号（取本作品已索引的最大话次）。
    const resolvedAt =
      current.episodes.length > 0
        ? current.episodes[current.episodes.length - 1].episode_no
        : 0;
    const plotThreads = current.story_bible.plot_threads.map((t) =>
      t.thread_id === threadId
        ? {
            ...t,
            status: t.status === 'open' ? 'resolved' : 'open',
            resolved_ep:
              t.status === 'open' ? resolvedAt : (undefined as number | undefined),
          }
        : t
    );
    void saveMemory(sid, { plot_threads: plotThreads }, '已更新伏笔状态');
  };

  const handleUpdateStyle = (sid: string, style: string) => {
    void saveMemory(
      sid,
      { narration_style: style },
      '已更新本作品专属叙事基调'
    );
  };

  /* -------------------- 配置与词典 -------------------- */

  const handleSaveConfig = async (next: StudioConfig) => {
    try {
      const raw = await apiFetch<RawConfig>('/config', {
        method: 'PUT',
        body: JSON.stringify({
          bailianBaseUrl: next.bailianBaseUrl,
          visionModel: next.visionModel,
          scriptModel: next.scriptModel,
          storyModel: next.storyModel,
          ttsModel: next.ttsModel,
          ttsVoice: next.ttsVoice,
          maxImageLongEdge: next.maxImageLongEdge,
          aspectRatio: next.aspectRatio,
          bgmDuckingDb: next.bgmDuckingDb,
        }),
      });
      setConfig(toStudioConfig(raw));
      showToast('百炼模型与合成参数已保存');
    } catch (err) {
      reportError('保存配置失败', err);
    }
  };

  const pushRules = async (scope: string, nextRules: PronunciationRule[]) => {
    try {
      const raw = await apiFetch<{ rules: RawRule[] }>('/pronunciation-rules', {
        method: 'PUT',
        body: JSON.stringify({
          scope,
          rules: nextRules.map((r) => ({
            source: r.source,
            replacement: r.replacement,
          })),
        }),
      });
      setRules((raw?.rules ?? []).map(toPronunciationRule));
    } catch (err) {
      reportError('更新多音字词典失败', err);
    }
  };

  const handleAddRule = (rule: Omit<PronunciationRule, 'id'>) => {
    const scope = rule.series_id ?? 'global';
    // 后端按 scope 整体覆盖，所以只提交该作用域内的规则 + 新规则。
    const scoped = rules.filter((r) => (r.series_id ?? 'global') === scope);
    const nextRules = [
      ...scoped,
      { ...rule, id: `r_${Date.now()}` } as PronunciationRule,
    ];
    void pushRules(scope, nextRules);
    showToast(`已添加多音字纠偏：${rule.source} → ${rule.replacement}`);
  };

  const handleDeleteRule = (id: string) => {
    const target = rules.find((r) => r.id === id);
    if (!target) return;
    const scope = target.series_id ?? 'global';
    const nextRules = rules.filter(
      (r) => r.id !== id && (r.series_id ?? 'global') === scope
    );
    void pushRules(scope, nextRules);
  };

  const activeSeriesRules = useMemo(() => {
    if (!activeSeries) return rules;
    return rules.filter(
      (r) => !r.series_id || r.series_id === activeSeries.series_id
    );
  }, [rules, activeSeries]);

  return (
    <div className="min-h-screen flex flex-col lg:flex-row bg-slate-50 text-slate-900">
      {/* Left Sidebar (264px) */}
      <aside className="w-full lg:w-[264px] bg-white border-b lg:border-b-0 lg:border-r border-slate-200 flex flex-col shrink-0">
        <div className="px-5 py-4 border-b border-slate-200 flex items-center justify-between">
          <span className="text-base font-bold tracking-tight text-slate-900 whitespace-nowrap">
            Webtoon2Video
          </span>
          <span className="text-xs font-mono-tabular text-slate-400">多作品控制台</span>
        </div>

        {/* Multi-Series Bookshelf */}
        <div className="p-3 border-b border-slate-200">
          <div className="flex items-center justify-between px-2 mb-2">
            <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-500">
              <Library className="w-3.5 h-3.5" />
              漫画作品库 ({seriesList.length})
            </span>
            <button
              type="button"
              onClick={() => setShowAddSeries((v) => !v)}
              className="inline-flex items-center gap-1 text-xs font-medium text-blue-600 hover:text-blue-700 cursor-pointer"
            >
              <Plus className="w-3.5 h-3.5" />
              导入新漫
            </button>
          </div>

          {showAddSeries && (
            <form onSubmit={handleCreateSeries} className="mb-2.5 p-2.5 bg-slate-50 border border-slate-200 rounded-lg space-y-2">
              <input
                type="text"
                value={newSeriesTitle}
                onChange={(e) => setNewSeriesTitle(e.target.value)}
                placeholder="漫画标题（可选）"
                className="w-full px-2.5 py-1.5 text-xs bg-white border border-slate-200 rounded"
              />
              <input
                type="text"
                value={newSeriesUrl}
                onChange={(e) => setNewSeriesUrl(e.target.value)}
                placeholder="WEBTOON 列表 URL 或本地目录（必填）"
                className="w-full px-2.5 py-1.5 text-xs bg-white border border-slate-200 rounded font-mono-tabular"
              />
              <div className="flex justify-end gap-1.5">
                <button
                  type="button"
                  onClick={() => setShowAddSeries(false)}
                  className="px-2 py-1 text-[11px] text-slate-600 hover:text-slate-900 cursor-pointer"
                >
                  取消
                </button>
                <button
                  type="submit"
                  className="px-2.5 py-1 text-[11px] font-medium text-white bg-blue-600 rounded hover:bg-blue-700 cursor-pointer"
                >
                  创建工作区
                </button>
              </div>
            </form>
          )}

          <div className="space-y-1">
            {viewSeriesList.map((s) => {
              const isSelected = s.series_id === activeSeries?.series_id;
              const runningCount = s.episodes.filter((e) => e.status === 'running').length;
              return (
                <button
                  key={s.series_id}
                  type="button"
                  onClick={() => handleSelectSeries(s.series_id)}
                  className={`w-full text-left px-3 py-2 rounded-lg text-xs transition-colors flex items-center justify-between gap-2 cursor-pointer ${
                    isSelected
                      ? 'bg-blue-50 text-blue-800 font-semibold border border-blue-200'
                      : 'text-slate-700 hover:bg-slate-50 border border-transparent'
                  }`}
                >
                  <div className="min-w-0">
                    <div className="truncate">{s.series_title}</div>
                    <div className="text-[11px] font-mono-tabular text-slate-400">
                      {s.series_id} · {s.episodes.length} 话
                    </div>
                  </div>
                  {runningCount > 0 && (
                    <span className="text-[11px] font-mono-tabular text-blue-600 shrink-0">
                      {runningCount} 运行中
                    </span>
                  )}
                </button>
              );
            })}
            {booted && viewSeriesList.length === 0 && (
              <p className="px-2 py-3 text-[11px] leading-relaxed text-slate-400">
                工作区还没有作品。点右上「导入新漫」，填入 WEBTOON 列表页 URL 或本地图片目录即可建立独立工作区。
              </p>
            )}
          </div>
        </div>

        {/* Navigation */}
        <nav className="p-3 space-y-1">
          <button
            type="button"
            onClick={() => setNavView('catalog')}
            className={`w-full flex items-center gap-2.5 px-3 py-2 text-sm font-medium rounded-lg transition-colors cursor-pointer ${
              navView === 'catalog'
                ? 'bg-slate-900 text-white'
                : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
            }`}
          >
            <BookOpen className="w-4 h-4 shrink-0" />
            <span className="whitespace-nowrap">剧集目录与调度</span>
          </button>

          <button
            type="button"
            onClick={() => setNavView('studio')}
            className={`w-full flex items-center gap-2.5 px-3 py-2 text-sm font-medium rounded-lg transition-colors cursor-pointer ${
              navView === 'studio'
                ? 'bg-slate-900 text-white'
                : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
            }`}
          >
            <Video className="w-4 h-4 shrink-0" />
            <span className="whitespace-nowrap">单话监控与审核台</span>
          </button>

          <button
            type="button"
            onClick={() => setNavView('memory')}
            className={`w-full flex items-center gap-2.5 px-3 py-2 text-sm font-medium rounded-lg transition-colors cursor-pointer ${
              navView === 'memory'
                ? 'bg-slate-900 text-white'
                : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
            }`}
          >
            <Brain className="w-4 h-4 shrink-0" />
            <span className="whitespace-nowrap">跨话记忆库与伏笔</span>
          </button>

          <button
            type="button"
            onClick={() => setNavView('config')}
            className={`w-full flex items-center gap-2.5 px-3 py-2 text-sm font-medium rounded-lg transition-colors cursor-pointer ${
              navView === 'config'
                ? 'bg-slate-900 text-white'
                : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
            }`}
          >
            <Sliders className="w-4 h-4 shrink-0" />
            <span className="whitespace-nowrap">百炼配置与多音字</span>
          </button>
        </nav>

        {/* Quick Episode Switcher */}
        <div className="px-4 py-3 mt-1 border-t border-slate-100 flex-1 overflow-y-auto hidden lg:block">
          <div className="text-xs font-medium text-slate-400 mb-2">
            当前作品剧集快切
          </div>
          <div className="space-y-1">
            {(activeSeries?.episodes ?? []).map((ep) => {
              const isCurrent = ep.episode_id === activeEpisode?.episode_id;
              return (
                <button
                  key={ep.episode_id}
                  type="button"
                  disabled={ep.is_locked}
                  onClick={() => {
                    setActiveEpisodeId(ep.episode_id);
                    setNavView('studio');
                  }}
                  className={`w-full text-left px-2.5 py-2 rounded-lg text-xs transition-colors flex items-center justify-between gap-2 cursor-pointer disabled:opacity-45 disabled:cursor-not-allowed ${
                    isCurrent && navView === 'studio'
                      ? 'bg-blue-50 text-blue-700 font-medium'
                      : 'text-slate-600 hover:bg-slate-50'
                  }`}
                >
                  <span className="truncate font-mono-tabular">
                    EP.{String(ep.episode_no).padStart(3, '0')}{' '}
                    {ep.title.replace(/^第 \d+ 话：/, '')}
                  </span>
                  <span className="text-[11px] font-mono-tabular text-slate-400 shrink-0">
                    {ep.stages.filter((s) => s.status === 'done').length}/8
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      </aside>

      {/* Main Content Area */}
      <div className="flex-1 min-w-0 flex flex-col">
        {/* Contextual Top Bar */}
        <header className="bg-white border-b border-slate-200 px-6 py-3.5 flex items-center justify-between gap-8">
          <div className="flex items-center gap-2 text-sm min-w-0">
            <span className="text-slate-500 whitespace-nowrap shrink-0">
              {activeSeries?.series_title ?? '未选择作品'}
            </span>
            <span className="text-slate-300" aria-hidden="true">
              /
            </span>
            <span className="font-semibold text-slate-900 truncate">
              {navView === 'catalog'
                ? '剧集目录与按话调度'
                : navView === 'memory'
                ? '四层跨话记忆库（StoryBible v2）'
                : navView === 'config'
                ? '阿里云百炼模型与多音字词典'
                : activeEpisode?.title ?? '未选择话次'}
            </span>
          </div>

          <div className="flex items-center gap-3 shrink-0">
            {/* Patch v4.10：成片完成提醒开关（声音 + 桌面通知） */}
            <div className="flex items-center gap-1 pr-3 border-r border-slate-200">
              <button
                type="button"
                onClick={handleToggleSound}
                title={
                  notifyPrefs.soundEnabled
                    ? '成片完成提示音：已开启（点击关闭）'
                    : '成片完成提示音：已关闭（点击开启）'
                }
                aria-label={
                  notifyPrefs.soundEnabled
                    ? '关闭成片完成提示音'
                    : '开启成片完成提示音'
                }
                aria-pressed={notifyPrefs.soundEnabled}
                className={`inline-flex items-center justify-center w-8 h-8 rounded-lg border transition-colors cursor-pointer ${
                  notifyPrefs.soundEnabled
                    ? 'text-blue-600 bg-blue-50 border-blue-200 hover:bg-blue-100'
                    : 'text-slate-400 bg-white border-slate-200 hover:bg-slate-100'
                }`}
              >
                {notifyPrefs.soundEnabled ? (
                  <Volume2 className="w-4 h-4" />
                ) : (
                  <VolumeX className="w-4 h-4" />
                )}
              </button>
              <button
                type="button"
                onClick={() => void handleToggleDesktop()}
                title={
                  desktopPermission === 'denied'
                    ? '桌面通知：浏览器已拒绝授权，请在地址栏站点设置中允许'
                    : desktopPermission === 'default'
                    ? '桌面通知：未授权（点击授权）'
                    : notifyPrefs.desktopEnabled
                    ? '桌面通知：已开启（点击关闭）'
                    : '桌面通知：已关闭（点击开启）'
                }
                aria-label={
                  desktopPermission === 'denied'
                    ? '桌面通知已被浏览器拒绝'
                    : notifyPrefs.desktopEnabled
                    ? '关闭成片完成桌面通知'
                    : '开启成片完成桌面通知'
                }
                aria-pressed={notifyPrefs.desktopEnabled && desktopPermission === 'granted'}
                className={`inline-flex items-center justify-center w-8 h-8 rounded-lg border transition-colors cursor-pointer ${
                  notifyPrefs.desktopEnabled && desktopPermission === 'granted'
                    ? 'text-blue-600 bg-blue-50 border-blue-200 hover:bg-blue-100'
                    : 'text-slate-400 bg-white border-slate-200 hover:bg-slate-100'
                }`}
              >
                {notifyPrefs.desktopEnabled && desktopPermission === 'granted' ? (
                  <Bell className="w-4 h-4" />
                ) : (
                  <BellOff className="w-4 h-4" />
                )}
              </button>
            </div>
            {navView === 'studio' ? (
              <>
                {activeEpisode &&
                  activeEpisode.stages.find((s) => s.id === 's7_renderer')?.status ===
                    'done' && (
                    <button
                      type="button"
                      onClick={() =>
                        void handleExportEpisode(activeEpisode.episode_id, 'mp4')
                      }
                      className="inline-flex items-center gap-1.5 px-3.5 py-2 text-xs font-medium text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg hover:bg-emerald-100 transition-colors whitespace-nowrap shrink-0 cursor-pointer"
                    >
                      <Download className="w-3.5 h-3.5" />
                      导出 9:16 成片
                    </button>
                  )}
                <button
                  type="button"
                  disabled={!activeEpisode}
                  onClick={() => {
                    if (!activeEpisode) return;
                    void rerunEpisode(activeEpisode.episode_id, 's1_crawler', 'full_video');
                  }}
                  className="inline-flex items-center gap-1.5 px-4 py-2 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors whitespace-nowrap shrink-0 cursor-pointer disabled:opacity-50"
                >
                  <Play className="w-3.5 h-3.5" />
                  重跑本话全流程
                </button>
              </>
            ) : (
              <button
                type="button"
                onClick={() => setNavView('studio')}
                className="inline-flex items-center gap-1.5 px-4 py-2 text-xs font-medium text-slate-900 bg-slate-100 rounded-lg hover:bg-slate-200 transition-colors whitespace-nowrap shrink-0 cursor-pointer"
              >
                打开审核台
              </button>
            )}
          </div>
        </header>

        {/* Main Viewport */}
        <main className="flex-1 p-6 max-w-[1400px] w-full mx-auto">
          {!booted && (
            <p className="text-sm text-slate-500">正在连接后端工作区…</p>
          )}

          {booted && !activeSeries && (
            <div className="rounded-xl border border-dashed border-slate-300 bg-white p-8 text-center">
              <p className="text-sm font-semibold text-slate-700">工作区还没有漫画作品</p>
              <p className="mt-1.5 text-xs text-slate-500">
                左侧「导入新漫」填入 WEBTOON 列表页 URL 或本地图片目录，后端会建立独立工作区并抓取目录。
              </p>
            </div>
          )}

          {booted && activeSeries && navView === 'catalog' && (
            <CatalogView
              series={activeSeries}
              runMode={runMode}
              onChangeRunMode={setRunMode}
              onParseCatalogUrl={handleParseCatalogUrl}
              onToggleEpisode={handleToggleEpisode}
              onApplyRangeSelection={handleApplyRangeSelection}
              onStartBatch={handleStartBatch}
              onOpenEpisode={(epId) => {
                setActiveEpisodeId(epId);
                setNavView('studio');
              }}
              onExportEpisode={handleExportEpisode}
              onExportSeriesZip={handleExportSeriesZip}
              onUpdateEpisodeRenderOptions={handleUpdateEpisodeRenderOptions}
            />
          )}

          {booted && activeSeries && navView === 'studio' && activeEpisode && (
            <EpisodeStudioView
              key={activeEpisode.episode_id}
              episode={activeEpisode}
              seriesId={activeSeries.series_id}
              config={config ?? FALLBACK_CONFIG}
              rules={activeSeriesRules}
              onRerunFromStage={handleRerunFromStage}
              onResetFromStage={handleResetFromStage}
              onTogglePanelBlank={handleTogglePanelBlank}
              onUpdateBeats={handleUpdateBeats}
              onAutoSaveBeats={handleAutoSaveBeats}
              onAutoPacing={handleAutoPacing}
              onUpdateEpisodeRenderOptions={handleUpdateEpisodeRenderOptions}
              onExportEpisode={handleExportEpisode}
            />
          )}

          {booted && activeSeries && navView === 'memory' && (
            <MemoryView
              series={activeSeries}
              onAddCharacter={handleAddCharacter}
              onAddPlotThread={handleAddPlotThread}
              onToggleThreadStatus={handleToggleThreadStatus}
              onUpdateStyle={handleUpdateStyle}
            />
          )}

          {booted && config && navView === 'config' && (
            <ConfigDictView
              config={config}
              rules={rules}
              onSaveConfig={handleSaveConfig}
              onAddRule={handleAddRule}
              onDeleteRule={handleDeleteRule}
            />
          )}
        </main>
      </div>

      {/* Transient Completion Toast */}
      {toast && (
        <div className="fixed bottom-5 right-5 z-50 flex items-center gap-2.5 bg-slate-900 text-white px-4 py-2.5 rounded-lg shadow-lg text-xs">
          <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0" />
          <span>{toast}</span>
          <button
            type="button"
            onClick={() => setToast(null)}
            aria-label="关闭提示"
            className="text-slate-400 hover:text-white ml-1 cursor-pointer"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      )}
    </div>
  );
}
