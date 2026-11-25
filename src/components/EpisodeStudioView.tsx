import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Archive,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Download,
  Eye,
  FileText,
  Film,
  FolderOutput,
  HardDrive,
  Loader2,
  Pause,
  Play,
  Plus,
  RotateCcw,
  Search,
  Square,
  Terminal,
  Trash2,
  Volume2,
  X,
  AlertCircle,
  Wand2,
  Music,
  Sparkles,
} from 'lucide-react';
import type {
  AutoMotionResponse,
  AutoMotionSummary,
  AutoPacingResponse,
  AutoPacingSummary,
  BeatRegenerateTTSResponse,
  BgmLibraryResponse,
  CameraMotion,
  CharacterCheckResponse,
  CharacterCheckWarning,
  CharacterSyncAction,
  CharacterSyncUpdate,
  EpisodeItem,
  EpisodeRenderOptions,
  EpisodeStoryboard,
  OutroMode,
  PanelItem,
  PronunciationRule,
  ScriptBeat,
  ScriptSaveOutcome,
  SeriesTtsConfigResponse,
  SfxLibraryItem,
  StageId,
  StoryboardScene,
  StudioConfig,
  VoiceOption,
} from '../types/studio';
import { formatMs, playPreviewTone, resolveSpokenText } from '../utils/pronunciation';
import { fetchSfxLibrary, sfxFileUrl } from '../api/studioApi';
import { formatBytes } from '../utils/format';
import { PanelZoomModal, type RoleOption, type SaveStatus } from './PanelZoomModal';
import {
  CUSTOM_VOICE,
  findVoiceOption,
  groupVoices,
  useVoicePreview,
  type VoiceGroup,
} from './VoicePicker';
import { StoryboardEditor } from './StoryboardEditor';
import { TtsTagPicker } from './TtsTagPicker';
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
   * 保存解说稿。返回落盘结果（审核台据此决定是否清掉「未保存改动」标记）。
   * triggerFromStage5=true 表示保存后顺带从 Stage 5 重跑配音与出片。
   * autoUnifyCharacters=true 表示按规范名归一角色名（Patch v4.23；默认不归一）。
   * syncCharacterUpdates 是 Patch v4.25 的可选「角色档案同步」清单；
   * 不传（或空数组）表示只放行这一次保存，不改作品角色档案。
   */
  onUpdateBeats: (
    episodeId: string,
    beats: ScriptBeat[],
    triggerFromStage5: boolean,
    autoUnifyCharacters?: boolean,
    syncCharacterUpdates?: CharacterSyncUpdate[]
  ) => Promise<ScriptSaveOutcome> | ScriptSaveOutcome;
  /** 分镜大图弹窗里的字幕原文自动保存（不触发重跑配音）。缺省时退化为本地草稿保存。 */
  onAutoSaveBeats?: (
    episodeId: string,
    beats: ScriptBeat[]
  ) => Promise<ScriptSaveOutcome | void>;
  /**
   * Patch v4.23：保存前的角色名写法**只读**检查（后端不改写任何文本）。
   * 缺省时跳过预检，直接保存。
   */
  onCheckCharacters?: (
    episodeId: string,
    beats: ScriptBeat[]
  ) => Promise<CharacterCheckResponse | null>;
  /** Patch v4.6：一键叙事节奏留白（后端改写 pause_after_ms 并同步 audio_manifest）。 */
  onAutoPacing?: (episodeId: string) => Promise<AutoPacingResponse | null>;
  /** Patch v4.28：一键自动分配画面动效（后端改写 motion；不碰配音）。 */
  onAutoMotions?: (episodeId: string) => Promise<AutoMotionResponse | null>;
  /**
   * Patch v4.35：就地新增一条多音字词典规则。
   *
   * 审核台命令栏的「+ 多音字规则」弹层用它落库；
   * 父组件负责写盘并刷新 rules 列表（缺省时该入口隐藏）。
   */
  onAddRule?: (rule: Omit<PronunciationRule, 'id'>) => void;
  /** Patch v4.5：保存本话封页/结尾片包选项（写入 render_options.json，仅重跑 S7 生效）。 */
  onUpdateEpisodeRenderOptions?: (
    episodeId: string,
    options: EpisodeRenderOptions
  ) => Promise<void> | void;
  onExportEpisode: (episodeId: string, format: 'mp4' | 'ass' | 'script') => void;
  /** Patch v4.11：一键清理过程文件（只留成片），返回是否成功。 */
  onCleanupIntermediates?: (episodeId: string) => Promise<boolean>;
  /** Patch v4.12：单句重配 TTS，rerunFromS6=true 时顺带从 S6 提交出片任务。 */
  onRegenerateBeatTts?: (
    episodeId: string,
    beatId: string,
    beat: ScriptBeat,
    rerunFromS6: boolean
  ) => Promise<BeatRegenerateTTSResponse | null>;
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

/**
 * Patch v4.23：角色名写法告警的确认弹窗状态。
 *
 * 用户点了「保存解说稿」「保存并从 Stage 5 重跑」或带脏草稿点「从此步重跑」时，
 * 先做一次**只读**检查；发现写法不一致就停下来，让用户在三个选项里选。
 */
interface CharacterPrompt {
  /** 后端返回的告警明细。 */
  warnings: CharacterCheckWarning[];
  /** 触发检查时的草稿快照（保存时用它，避免弹窗期间改动错位）。 */
  beats: ScriptBeat[];
  /** 保存后是否顺带从 Stage 5 重跑配音。 */
  triggerFromStage5: boolean;
  /** 保存完成后的后续动作（例如继续「从此步重跑」）。 */
  onDone?: () => void;
  /** 弹窗里的保存动作是否进行中。 */
  busy: boolean;
}

/** 告警字段的中文名（弹窗里显示）。 */
const CHARACTER_FIELD_LABELS: Record<string, string> = {
  speaker_role: '说话人',
  narration_text: '字幕原文',
  tts_text: '送音文本',
};

/** 告警原因的中文说明（弹窗里显示）。 */
const CHARACTER_REASON_LABELS: Record<string, string> = {
  alias_variant: '别名写法',
  homophone_variant: '同音异写',
  unregistered_speaker: '未登记的角色',
};

/**
 * Patch v4.25：把角色名告警聚合成「一次档案同步」的粒度。
 *
 * 后端 `sync_character_updates` 是按 (found_name, target_canonical) 落地的，
 * 所以同一个写法在几句里重复出现时，弹窗里只给一个选项组。
 */
interface CharacterSyncGroup {
  /** 聚合键：found_name + '\u0000' + suggested_canonical。 */
  key: string;
  /** 用户实际写出来的角色名。 */
  found_name: string;
  /** 后端建议的规范名（未登记角色没有建议名）。 */
  suggested_canonical: string | null;
  /** 归一后的告警原因（同一组内取第一条）。 */
  reason: string;
  /** 涉及的第几句（弹窗里做提示用）。 */
  beatIds: string[];
  /** 本组允许的档案同步动作。 */
  actions: CharacterSyncAction[];
}

/** Patch v4.25：档案同步动作的中文名（弹窗里显示）。 */
const CHARACTER_SYNC_ACTION_LABELS: Record<CharacterSyncAction, string> = {
  promote_canonical: '记为规范名（原名降为别名）',
  add_alias: '记为合法别名（不再提醒）',
  register_character: '登记进作品角色档案',
};

/** Patch v4.25：某条告警允许的档案同步动作。 */
const characterSyncActionsFor = (
  reason: string,
  suggestedCanonical: string | null
): CharacterSyncAction[] => {
  if (reason === 'unregistered_speaker') return ['register_character'];
  if (reason === 'alias_variant' || reason === 'homophone_variant') {
    // 没有规范名就没法「降级原名 / 挂到规范名下」，只能放行不改档案。
    if (!suggestedCanonical) return [];
    return ['promote_canonical', 'add_alias'];
  }
  return [];
};

/**
 * Patch v4.25：按 (found_name, suggested_canonical) 聚合告警。
 *
 * 同一组里出现多种原因时，取并集（未登记角色只会是 register_character）。
 */
const buildCharacterSyncGroups = (
  warnings: CharacterCheckWarning[]
): CharacterSyncGroup[] => {
  const groups = new Map<string, CharacterSyncGroup>();
  warnings.forEach((warning) => {
    const found = (warning.found_name ?? '').trim();
    if (!found) return;
    const suggested = warning.suggested_canonical ?? null;
    const key = `${found}\u0000${suggested ?? ''}`;
    const existing = groups.get(key);
    if (existing) {
      if (!existing.beatIds.includes(warning.beat_id)) {
        existing.beatIds.push(warning.beat_id);
      }
      characterSyncActionsFor(warning.reason, suggested).forEach((action) => {
        if (!existing.actions.includes(action)) existing.actions.push(action);
      });
      return;
    }
    groups.set(key, {
      key,
      found_name: found,
      suggested_canonical: suggested,
      reason: warning.reason,
      beatIds: [warning.beat_id],
      actions: characterSyncActionsFor(warning.reason, suggested),
    });
  });
  return Array.from(groups.values());
};

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

/**
 * Patch v4.12：用户在审核台可以改的字段。
 *
 * 判定「草稿是否还有未落盘改动」时只看这些字段；`audio_duration_ms`、
 * `resolved_voice` 都是后端派生的，不算用户改动。
 */
const DRAFT_EDITABLE_FIELDS: Array<keyof ScriptBeat> = [
  'narration_text',
  'tts_text',
  'emotion_instruction',
  'speaker_role',
  'speaker_gender',
  'voice_override',
  'speed_override',
  'pause_after_ms',
  'target_panel_ids',
  'motion',
  'focus_hint',
  'sfx_cue',
  /** Patch v4.13：这条解说归属的镜头组（幕）。 */
  'scene_id',
];

/** 把字段值归一化成可比字符串（null/undefined 视作空，数组按 JSON 比）。 */
const normDraftField = (value: unknown): string => {
  if (value === null || value === undefined) return '';
  if (Array.isArray(value)) return JSON.stringify(value);
  return String(value);
};

/**
 * Patch v4.12：草稿与已落盘剧本之间是否还有差异。
 *
 * `skipBeatId` 用于单句重配：那一句的改动已经随请求落盘，不该再算作未保存改动。
 */
const hasDraftChanges = (
  draft: ScriptBeat[],
  saved: ScriptBeat[],
  skipBeatId?: string
): boolean => {
  if (draft.length !== saved.length) return true;
  const savedMap = new Map(saved.map((beat) => [beat.beat_id, beat]));
  for (const beat of draft) {
    if (skipBeatId && beat.beat_id === skipBeatId) continue;
    const other = savedMap.get(beat.beat_id);
    if (!other) return true;
    for (const field of DRAFT_EDITABLE_FIELDS) {
      if (normDraftField(beat[field]) !== normDraftField(other[field])) return true;
    }
  }
  return false;
};

/**
 * Patch v4.14：句中可插停顿标签的标点（取第一个「前后都有文字」的标点）。
 *
 * 停顿标签必须夹在两段可发音文字之间，所以只能落在句中标点之后；
 * 句首 / 句尾的标点不算（前面或后面没有文字）。
 */
const PAUSE_INSERT_MARKS = ['，', '、', '；', '……', '——'] as const;

const findSentenceMark = (
  text: string
): { index: number; text: string } | null => {
  let best: { index: number; text: string } | null = null;
  for (const mark of PAUSE_INSERT_MARKS) {
    const index = text.indexOf(mark);
    if (index < 0) continue;
    if (!best || index < best.index) best = { index, text: mark };
  }
  if (!best) return null;
  if (best.index <= 0) return null;
  if (best.index + best.text.length >= text.length) return null;
  return best;
};

const MOTION_LABELS: Record<CameraMotion, string> = {
  zoom_in: 'ZOOM_IN 缓推',
  zoom_out: 'ZOOM_OUT 缓拉',
  pan_down: 'PAN_DOWN 竖扫',
  pan_up: 'PAN_UP 上移',
  // Patch v4.28：横向扫镜。
  pan_left: 'PAN_LEFT 左横扫',
  pan_right: 'PAN_RIGHT 右横扫',
  shake: 'SHAKE 震动',
  static: 'STATIC 静止',
};

// Patch v4.31：下拉里**推荐**的动效清单——竖扫（pan_down / pan_up）已从自动
// 链路下线，默认不再展示，避免用户被推荐一个系统不会再自动分配的动作。
// 老剧本里残留的竖扫仍可通过「保留当前值」显示出来（见 motionOptions）。
const RECOMMENDED_MOTIONS: CameraMotion[] = [
  'static',
  'zoom_in',
  'zoom_out',
  'pan_left',
  'pan_right',
  'shake',
];

/**
 * 审核表运镜下拉的选项：推荐的 6 种 + （仅当该句当前值是已下线的旧版竖扫时）
 * 临时保留当前值，避免 select 找不到匹配项而显示异常。
 */
const motionOptions = (current: CameraMotion): CameraMotion[] =>
  RECOMMENDED_MOTIONS.includes(current) ? RECOMMENDED_MOTIONS : [current, ...RECOMMENDED_MOTIONS];

// ---------------------------------------------------------------------------
// Patch v4.35：折叠偏好（localStorage 记忆）
// ---------------------------------------------------------------------------

/**
 * 读取折叠偏好。
 *
 * 流水线 / 封页两块信息密度高，一话 200~300 分镜时会把首屏撑满，
 * 用户要滚很久才看到分镜与审核表。折叠态记在 localStorage 里，跨话次保留。
 * 隐私模式下 localStorage 不可用 → 退化为「不折叠」，不影响功能。
 */
function readCollapsePref(key: string): boolean {
  try {
    return window.localStorage.getItem(key) === '1';
  } catch {
    return false;
  }
}

/** 写回折叠偏好。 */
function writeCollapsePref(key: string, collapsed: boolean): void {
  try {
    window.localStorage.setItem(key, collapsed ? '1' : '0');
  } catch {
    /* 忽略：仅影响折叠偏好，不影响任何数据 */
  }
}

/** Patch v4.35：流水线折叠偏好的 localStorage 键。 */
const PIPELINE_COLLAPSE_KEY = 'w2v.studio.pipelineCollapsed';
/** Patch v4.35：封页折叠偏好的 localStorage 键。 */
const BRANDING_COLLAPSE_KEY = 'w2v.studio.brandingCollapsed';

// ---------------------------------------------------------------------------
// Patch v4.35：审核台的轻量筛选
// ---------------------------------------------------------------------------

/**
 * 审核台的筛选档位。
 *
 * 搜索 + 筛选是**定位**手段，不是改稿手段：命中的句子照常可编辑可保存，
 * 没命中的句子只是暂时不渲染，内容一条都不会丢。
 */
type ScriptFilterKey =
  | 'all'
  | 'length'
  | 'multi_panel'
  | 'custom_tts'
  | 'voice_tuned';

const SCRIPT_FILTER_LABELS: Record<ScriptFilterKey, string> = {
  all: '全部',
  length: '字数预警',
  multi_panel: '多格轮播',
  custom_tts: '自定义送音',
  voice_tuned: '改过音色/语速',
};

/** 筛选档位的展示顺序（与命令栏从左到右一致）。 */
const SCRIPT_FILTER_ORDER: ScriptFilterKey[] = [
  'all',
  'length',
  'multi_panel',
  'custom_tts',
  'voice_tuned',
];

// ---------------------------------------------------------------------------
// Patch v4.25：分镜缩略图 / 角色解析工具 / 审核表行组件
// ---------------------------------------------------------------------------

/**
 * Patch v4.25：分镜缩略图 URL。
 *
 * 后端提供 `GET /api/episodes/{id}/panels/{filename}?w=<像素>` 缩略图接口（返回 JPEG），
 * 这里在原图 URL 上追加 / 替换 `w` 查询参数：原图 URL 没有 `?` 时用 `?w=`，
 * 有 `?` 时用 `&w=`；已经带 `w=` 的直接替换，避免出现 `?w=140&w=180` 这种重复键。
 *
 * 大图预览与分镜放大弹窗（PanelZoomModal）继续用原图 URL，不走缩略图。
 */
export const panelThumbUrl = (url: string, width: number): string => {
  if (!url) return url;
  if (/[?&]w=\d+/.test(url)) {
    return url.replace(/([?&])w=\d+/, `$1w=${width}`);
  }
  return `${url}${url.includes('?') ? '&' : '?'}w=${width}`;
};

/**
 * Patch v4.25：角色 → 性别（从组件体内提到模块级，供审核表行组件复用）。
 *
 * 改角色时必须同步 speaker_gender：Stage 4 留下的性别属于**原来那个角色**
 * （男角色改成女角色后仍是 male），不跟着改就会用错音色的兜底槽位。
 */
const genderForRoleOf = (
  role: string,
  seriesTts: SeriesTtsConfigResponse | null
): 'male' | 'female' | 'unknown' => {
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
 * Patch v4.25：说话人候选（模块级版本，参数化以便审核表行组件复用）。
 *
 * 只让用户选**角色**，音色由角色自动带出（讲解者 / 通用男声 / 通用女声 / 角色专属），
 * 避免在一堆音色 ID 里挑花眼。
 */
const roleOptionsForBeat = (
  beat: ScriptBeat,
  seriesTts: SeriesTtsConfigResponse | null,
  roleNames: string[]
): RoleOption[] => {
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
 * Patch v4.25：按当前 ``speaker_role`` 从作品级 TTS 配置算出角色默认语速（模块级版本）。
 */
const roleSpeedForBeatOf = (
  beat: ScriptBeat,
  seriesTts: SeriesTtsConfigResponse | null
): number => {
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
 * Patch v4.25：一条 beat 的「本句配音」说明（模块级版本）。
 *
 * 后端已给出权威结果（``resolved_voice``，与 Stage 5 同一套解析）；这里再按当前
 * 下拉选择做一次**本地预览**，改角色后能立刻看到会换成谁，保存重跑后以服务端为准。
 */
const beatVoiceInfoBase = (
  beat: ScriptBeat,
  seriesTts: SeriesTtsConfigResponse | null,
  roleNames: string[]
): {
  label: string;
  voice: string;
  pending: boolean;
  hint: string;
  speed: number;
  roleSpeed: number;
  speedOverridden: boolean;
} => {
  const roleSpeed = roleSpeedForBeatOf(beat, seriesTts);
  const speedOverridden = beat.speed_override != null;
  const speed = speedOverridden ? (beat.speed_override as number) : roleSpeed;
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

/** Patch v4.27：一条 beat 的「本句配音」说明（含音色的友好中文名）。 */
type BeatVoiceInfo = ReturnType<typeof beatVoiceInfoBase> & {
  /** 音色的友好中文名（取不到就回退成音色 ID 本身）。 */
  voiceName: string;
};

/**
 * Patch v4.27：一条 beat 的「本句配音」说明（模块级版本）。
 *
 * 与 `beatVoiceInfoBase` 同口径，只是多带一个 `voiceName`：MiniMax 音色 ID
 * 长且是英文（如 `Chinese (Mandarin)_Radio_Host`），界面上要显示「电台男主播」
 * 这类中文名才认得出来，所以按 voice_id / id 索引查表。
 */
const beatVoiceInfoOf = (
  beat: ScriptBeat,
  seriesTts: SeriesTtsConfigResponse | null,
  roleNames: string[],
  voiceOptionMap?: Map<string, VoiceOption>
): BeatVoiceInfo => {
  const base = beatVoiceInfoBase(beat, seriesTts, roleNames);
  const friendly = voiceOptionMap?.get(base.voice)?.display_name?.trim();
  return { ...base, voiceName: friendly || base.voice };
};

/**
 * Patch v4.38：音效试听（模块级**单一** Audio 实例）。
 *
 * 一话 120 行、每行一个试听按钮，用 new Audio() 逐个 new 会让多个音效
 * 同时响（点一个响一片）。这里全局只留一个实例，播新的先停掉旧的。
 */
let sfxPreviewAudio: HTMLAudioElement | null = null;

function playSfxPreview(url: string): void {
  if (!url) return;
  try {
    if (sfxPreviewAudio) {
      sfxPreviewAudio.pause();
      sfxPreviewAudio.currentTime = 0;
    }
    const audio = new Audio(url);
    sfxPreviewAudio = audio;
    void audio.play().catch(() => {
      /* 浏览器自动播放策略拦截时静默失败 */
    });
  } catch {
    /* 素材损坏 / 格式不支持时不影响审核台其余功能 */
  }
}

/**
 * Patch v4.25：改说话人角色时要同步落的字段（模块级版本）。
 *
 * a) speaker_gender 始终跟着角色走，选回「不指定」（空串）时重置为 unknown；
 * b) 清掉历史逐句音色 voice_override，恢复按角色解析，避免界面角色与实际音色不一致；
 * c) 一并清空单句语速微调 speed_override，让新角色自带的默认语速自动生效。
 */
const speakerRolePatchOf = (
  beat: ScriptBeat,
  nextRole: string,
  seriesTts: SeriesTtsConfigResponse | null
): Partial<ScriptBeat> => {
  const patch: Partial<ScriptBeat> = {
    speaker_role: nextRole,
    speaker_gender: nextRole ? genderForRoleOf(nextRole, seriesTts) : 'unknown',
    speed_override: null,
  };
  if ((beat.voice_override ?? '').trim()) patch.voice_override = '';
  return patch;
};

/**
 * Patch v4.25：空幕列表常量。
 *
 * `sceneStoryboard?.scenes ?? []` 每次渲染都会造一个新数组，会让依赖它的
 * useMemo（幕选项、脏标记）每次都重算；这里给一个稳定的空数组兜底。
 */
const EMPTY_SCENES: StoryboardScene[] = [];

/**
 * Patch v4.25：审核表「一行」的 props。
 *
 * 所有回调都由父组件用 useCallback 提供（引用稳定），其余派生值在行内按 beat
 * 自身的引用做 useMemo 缓存 —— 没被编辑的 beat 对象引用不变，派生值就不会重算。
 */
interface ScriptBeatRowProps {
  beat: ScriptBeat;
  panel?: PanelItem;
  panelOptions: Array<{ value: string; label: string }>;
  sceneOptions: Array<{ value: string; label: string }>;
  rules: PronunciationRule[];
  ttsModel: string;
  seriesTts: SeriesTtsConfigResponse | null;
  roleNames: string[];
  /** Patch v4.27：MiniMax 音色分组（按供应商 · 分类渲染 optgroup），引用稳定。 */
  voiceGroups: VoiceGroup[];
  /** Patch v4.27：全部 MiniMax 音色（试听样例时按 ID 找条目），引用稳定。 */
  voiceOptions: VoiceOption[];
  /** Patch v4.27：音色 ID → 音色条目（按 voice_id / id 双索引），引用稳定。 */
  voiceOptionMap: Map<string, VoiceOption>;
  regenerateEnabled: boolean;
  playingBeatId: string | null;
  tagPickerOpen: boolean;
  ttsTextareaRefs: React.MutableRefObject<Record<string, HTMLTextAreaElement | null>>;
  ttsSelections: React.MutableRefObject<
    Record<string, { start: number; end: number }>
  >;
  onChangeText: (
    beatId: string,
    key: 'narration_text' | 'tts_text',
    value: string
  ) => void;
  onChangeRole: (beatId: string, role: string) => void;
  /** Patch v4.27：逐句指定 MiniMax 音色（voice_override）；传空串表示恢复按角色解析。 */
  onChangeVoiceOverride: (beatId: string, voiceId: string) => void;
  /** Patch v4.27：试听这一句的音色样例（静态样例，不消耗 API 额度）。 */
  onPreviewVoice: (beatId: string, voiceId: string, speed: number) => void;
  onNudgeSpeed: (beatId: string, delta: number) => void;
  onResetSpeed: (beatId: string) => void;
  onChangeMotion: (beatId: string, motion: CameraMotion) => void;
  onChangePause: (beatId: string, ms: number) => void;
  onChangePanel: (beatId: string, panelId: string) => void;
  /** Patch v4.32：改第 index 个目标分镜（保留其它目标格，不再一改就压成一格）。 */
  onChangePanelAt: (beatId: string, index: number, panelId: string) => void;
  /** Patch v4.32：删掉第 index 个目标分镜（至少保留一格）。 */
  onRemovePanelAt: (beatId: string, index: number) => void;
  /** Patch v4.32：镜头编排（启用幕）覆盖的分镜集合；空集合＝不限制。 */
  scopePanelIds: Set<string>;
  /** Patch v4.35：panel_id → 分镜，用于画第 2..N 格的迷你缩略图。 */
  panelsById: Map<string, PanelItem>;
  /** Patch v4.35：给这一句再加一格轮播画面。 */
  onAddPanelAt?: (beatId: string) => void;
  /** Patch v4.35：从分镜详情跳到审核台时高亮这一行。 */
  highlight?: boolean;
  /** Patch v4.38：音效曲库（24 个标准键 + 盘面可用情况）。 */
  sfxLibrary?: SfxLibraryItem[];
  /** Patch v4.38：改这一句的音效提示（传 null = 清空）。 */
  onChangeSfxCue?: (beatId: string, sfxKey: string | null) => void;
  onChangeScene: (beatId: string, sceneId: string | null) => void;
  onToggleTagPicker: (beatId: string) => void;
  onPickTag: (beatId: string, snippet: string) => void;
  onPreview: (beatId: string) => void;
  onRegenerate: (beatId: string) => void;
  onInsert: (beatId: string) => void;
  onDelete: (beatId: string) => void;
  onZoom: (beatId: string) => void;
}

/**
 * Patch v4.25：审核表的一行（独立 memo 子组件）。
 *
 * 一话 200~300 分镜时整表重绘会明显卡顿，因此把「一行」拆成独立组件：
 * 编辑其中一句时，只有那一行会因为 beat 引用变化而重绘，其余行直接复用。
 */
const ScriptBeatRow = React.memo(function ScriptBeatRow(props: ScriptBeatRowProps) {
  const {
    beat,
    panel,
    panelOptions,
    sceneOptions,
    rules,
    ttsModel,
    seriesTts,
    roleNames,
    voiceGroups,
    voiceOptions,
    voiceOptionMap,
    regenerateEnabled,
    playingBeatId,
    tagPickerOpen,
    ttsTextareaRefs,
    ttsSelections,
    onChangeText,
    onChangeRole,
    onChangeVoiceOverride,
    onPreviewVoice,
    onNudgeSpeed,
    onResetSpeed,
    onChangeMotion,
    onChangePause,
    onChangePanel,
    onChangePanelAt,
    onRemovePanelAt,
    scopePanelIds,
    panelsById,
    onAddPanelAt,
    highlight,
    sfxLibrary,
    onChangeSfxCue,
    onChangeScene,
    onToggleTagPicker,
    onPickTag,
    onPreview,
    onRegenerate,
    onInsert,
    onDelete,
    onZoom,
  } = props;

  const roleOptions = useMemo(
    () => roleOptionsForBeat(beat, seriesTts, roleNames),
    [beat, seriesTts, roleNames]
  );
  /* Patch v4.38：音效曲库按分类分组；「usedFallback」= 盘面没有该键的素材，
     实际回源用了兜底音效（前端要标出来，别让人以为听到的是原音效）。 */
  const sfxGroups = useMemo(() => {
    const groups: Array<{
      category: string;
      items: Array<SfxLibraryItem & { usedFallback: boolean }>;
    }> = [];
    const byCategory = new Map<string, Array<SfxLibraryItem & { usedFallback: boolean }>>();
    for (const item of sfxLibrary ?? []) {
      const usedFallback =
        item.resolved_filename !== '' &&
        !item.resolved_filename.startsWith(item.key);
      const enriched = { ...item, usedFallback };
      const bucket = byCategory.get(item.category);
      if (bucket) bucket.push(enriched);
      else byCategory.set(item.category, [enriched]);
    }
    for (const [category, items] of byCategory) {
      groups.push({ category, items });
    }
    return groups;
  }, [sfxLibrary]);
  const currentSfx = (sfxLibrary ?? []).find((item) => item.key === (beat.sfx_cue ?? ''));
  const currentSfxPlayable = Boolean(currentSfx?.available);
  const previewSfx = useCallback((sfxKey: string) => {
    if (!sfxKey) return;
    playSfxPreview(sfxFileUrl(sfxKey));
  }, []);
  const voiceInfo = useMemo(
    () => beatVoiceInfoOf(beat, seriesTts, roleNames, voiceOptionMap),
    [beat, seriesTts, roleNames, voiceOptionMap]
  );
  // Patch v4.27：逐句音色覆盖（voice_override）的行内控件。
  // 「跟随角色」是默认项；选了不在音色表里的 ID（自定义音色）时保留一条兜底选项，
  // 免得下拉显示空白、用户看不出这一句到底用的哪个音色。
  const [customVoiceEditing, setCustomVoiceEditing] = useState(false);
  const [customVoiceDraft, setCustomVoiceDraft] = useState('');
  const overrideVoiceId = (beat.voice_override ?? '').trim();
  const overrideKnown = !overrideVoiceId || voiceOptionMap.has(overrideVoiceId);
  /**
   * Patch v4.35：送音文本默认折叠。
   *
   * 一话 120 行，每行都挂一个 textarea 会把审核台压得没法看；
   * 已经有 tts_text 的行默认展开（用户改过的东西要看得见），
   * 其余只给一行摘要，点一下才展开。
   */
  const [ttsExpanded, setTtsExpanded] = useState(() => !!beat.tts_text?.trim());
  /**
   * Patch v4.35：单句音色选择框默认折叠。
   *
   * 已经指定过音色的行默认展开（要让人看见「这一句被改过」），
   * 其余只显示落点音色摘要 + 试听，点「换音色」才展开下拉。
   */
  const [voicePickerOpen, setVoicePickerOpen] = useState(() => !!overrideVoiceId);
  const roleVoiceId = beat.resolved_voice?.voice ?? '';
  const roleVoiceDisplayName =
    voiceOptionMap.get(roleVoiceId)?.display_name || roleVoiceId || '按角色解析';
  const previewOption = findVoiceOption(voiceOptions, voiceInfo.voice);
  const previewReady = !!previewOption?.sample_ready;
  const commitCustomVoice = useCallback(() => {
    const next = customVoiceDraft.trim();
    setCustomVoiceEditing(false);
    if (!next || next === overrideVoiceId) return;
    onChangeVoiceOverride(beat.beat_id, next);
  }, [customVoiceDraft, overrideVoiceId, onChangeVoiceOverride, beat.beat_id]);
  const { spokenText, matchedRules } = useMemo(
    () => resolveSpokenText(beat, rules),
    [beat, rules]
  );

  /**
   * Patch v4.32：这一句实际会播出的全部分镜（Stage 6 会按格数均分时长轮播）。
   *
   * 以前审核台只暴露第 1 格，成片里出现的第 2..N 格在审核台完全看不见，
   * 用户观感就是「这帧我没选过」。现在全部目标格都显式列出来、可改可删。
   */
  const targetPanelIds = beat.target_panel_ids ?? [];

  /**
   * Patch v4.32：分镜下拉只列「镜头编排范围内」的分镜。
   *
   * - 编排范围为空（还没编排 / 没有启用幕）→ 不限制，列全部分镜；
   * - 否则只列编排内的分镜，但**始终保留本句当前用到的分镜**，
   *   否则老剧本里的越界引用在下拉里会显示空白（用户看不出当前是什么）。
   */
  const scopedPanelOptions = useMemo(() => {
    if (scopePanelIds.size === 0) return panelOptions;
    const inScope = panelOptions.filter((option) => scopePanelIds.has(option.value));
    const missing = targetPanelIds.filter(
      (panelId) => !scopePanelIds.has(panelId) && !inScope.some((o) => o.value === panelId)
    );
    return [
      ...inScope,
      ...missing.map((panelId) => ({ value: panelId, label: `${panelId}（编排外）` })),
    ];
  }, [panelOptions, scopePanelIds, targetPanelIds]);

  // Patch v4.25：送音文本框的 ref 与选区记忆用 useCallback 固定下来，
  // 避免每次渲染都换 ref 回调导致 DOM ref 被反复置空。
  const setTextareaRef = useCallback(
    (el: HTMLTextAreaElement | null) => {
      ttsTextareaRefs.current[beat.beat_id] = el;
    },
    [beat.beat_id, ttsTextareaRefs]
  );
  const rememberSelection = useCallback(
    (el: HTMLTextAreaElement) => {
      ttsSelections.current[beat.beat_id] = {
        start: el.selectionStart,
        end: el.selectionEnd,
      };
    },
    [beat.beat_id, ttsSelections]
  );

  const charLen = beat.narration_text.trim().length;
  const isLengthWarn = charLen < 8 || charLen > 30;

  return (
    <tr
      data-beat-id={beat.beat_id}
      className={`hover:bg-slate-50/50 ${
        highlight ? 'bg-amber-50 ring-2 ring-inset ring-amber-300' : ''
      }`}
    >
      <td className="py-3.5 pl-5 pr-2">
        <div className="space-y-1.5">
          <div className="font-mono-tabular font-semibold text-slate-700">
            {beat.beat_id}
          </div>
          <div className="flex items-start gap-1.5">
            {panel && (
              <button
                type="button"
                onClick={() => onZoom(beat.beat_id)}
                title="放大查看分镜并编辑字幕原文"
                aria-label={`放大查看 ${panel.panel_id} 并编辑字幕原文`}
                className="block w-12 h-16 rounded border border-slate-200 overflow-hidden hover:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/40 cursor-pointer transition-colors shrink-0"
              >
                <img
                  src={panelThumbUrl(panel.image_url, 140)}
                  alt={panel.panel_id}
                  referrerPolicy="no-referrer"
                  loading="lazy"
                  decoding="async"
                  className="w-12 h-16 object-cover"
                />
              </button>
            )}
            {/* Patch v4.35：第 2..N 格的迷你缩略图并排跟在主图旁边，
                「第 2 格到底是哪张」不用再靠下拉猜。 */}
            {targetPanelIds.length > 1 && (
              <div className="flex flex-col gap-1">
                {targetPanelIds.slice(1).map((panelId, index) => {
                  const extra = panelsById.get(panelId);
                  if (!extra) return null;
                  return (
                    <img
                      key={`${panelId}-${index}`}
                      src={panelThumbUrl(extra.image_url, 140)}
                      alt={extra.panel_id}
                      title={`第 ${index + 2} 格：${extra.panel_id}`}
                      referrerPolicy="no-referrer"
                      loading="lazy"
                      decoding="async"
                      className="w-8 h-10 object-cover rounded border border-amber-300"
                    />
                  );
                })}
              </div>
            )}
          </div>
          {/* Patch v4.32：这一句实际会播出的每一格分镜都列出来，可改可删。 */}
          {targetPanelIds.map((panelId, index) => (
            <div key={`${panelId}-${index}`} className="flex items-center gap-1">
              <select
                value={panelId}
                onChange={(e) =>
                  index === 0
                    ? onChangePanel(beat.beat_id, e.target.value)
                    : onChangePanelAt(beat.beat_id, index, e.target.value)
                }
                title={
                  index === 0
                    ? `这一句的主画面分镜${
                        targetPanelIds.length > 1
                          ? `（本句共 ${targetPanelIds.length} 格，会按格数均分时长轮播）`
                          : ''
                      }`
                    : `这一句的第 ${index + 1} 格分镜（成片会按格数均分时长轮播）`
                }
                aria-label={`${beat.beat_id} 第 ${index + 1} 格分镜`}
                className={
                  index > 0
                    ? 'w-20 px-1 py-0.5 text-[11px] font-mono-tabular bg-amber-50/70 border border-amber-300 rounded ring-1 ring-amber-200'
                    : 'w-20 px-1 py-0.5 text-[11px] font-mono-tabular bg-white border border-slate-200 rounded'
                }
              >
                {scopedPanelOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
              {index > 0 && (
                <button
                  type="button"
                  onClick={() => onRemovePanelAt(beat.beat_id, index)}
                  title="删掉这一格分镜"
                  aria-label={`${beat.beat_id} 删掉第 ${index + 1} 格分镜`}
                  className="w-5 h-5 text-[11px] leading-none text-slate-400 hover:text-red-600 border border-slate-200 rounded bg-white"
                >
                  ×
                </button>
              )}
            </div>
          ))}
          {/* Patch v4.35：给这一句再加一格轮播画面。 */}
          {onAddPanelAt && scopedPanelOptions.length > 0 && (
            <button
              type="button"
              onClick={() => onAddPanelAt(beat.beat_id)}
              title="给这一句再加一格轮播画面（成片会按格数均分时长）"
              aria-label={`${beat.beat_id} 增加一格轮播分镜`}
              className="w-20 h-5 flex items-center justify-center gap-0.5 text-[10px] text-slate-500 bg-white border border-dashed border-slate-300 rounded hover:border-blue-400 hover:text-blue-700 cursor-pointer"
            >
              <Plus className="w-2.5 h-2.5" />
              加一格
            </button>
          )}
          {/* Patch v4.13：归属幕与分镜解耦，这里单独指定这一句讲哪一幕。 */}
          {sceneOptions.length > 0 && (
            <select
              value={beat.scene_id ?? ''}
              onChange={(e) =>
                onChangeScene(
                  beat.beat_id,
                  e.target.value === '' ? null : e.target.value
                )
              }
              title="这一句归属哪一幕（留空＝按分镜自动回退）"
              aria-label={`${beat.beat_id} 归属幕`}
              className="w-24 px-1 py-0.5 text-[11px] bg-white border border-slate-200 rounded"
            >
              <option value="">自动</option>
              {sceneOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          )}
        </div>
      </td>

      <td className="py-3.5 px-3">
        <textarea
          rows={2}
          value={beat.narration_text}
          onChange={(e) => onChangeText(beat.beat_id, 'narration_text', e.target.value)}
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
        <div className="relative">
          {ttsExpanded ? (
            <>
              <div className="mb-1 flex items-center justify-between gap-2">
                <span className="text-[10px] text-slate-400">
                  标签只写送音文本，字幕不受影响
                </span>
                <div className="flex items-center gap-1">
                  {/* Patch v4.14：语气词 / 句中停顿标签浮层入口。 */}
                  <button
                    type="button"
                    onClick={() => onToggleTagPicker(beat.beat_id)}
                    aria-label={`${beat.beat_id} 插入语气或停顿标签`}
                    title="插入 MiniMax 语气词（如 (laughs)）或句中停顿（如 <#0.5#>）"
                    className="rounded-md border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] text-slate-600 hover:border-blue-400 hover:text-blue-700 transition-colors"
                  >
                    + 语气 / 停顿标签
                  </button>
                  <button
                    type="button"
                    onClick={() => setTtsExpanded(false)}
                    title="收起送音编辑"
                    aria-label={`${beat.beat_id} 收起送音编辑`}
                    className="rounded-md border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] text-slate-500 hover:border-slate-400 hover:text-slate-600"
                  >
                    收起
                  </button>
                </div>
              </div>
              <textarea
                rows={2}
                value={beat.tts_text}
                ref={setTextareaRef}
                onSelect={(e) => rememberSelection(e.currentTarget)}
                onClick={(e) => rememberSelection(e.currentTarget)}
                onKeyUp={(e) => rememberSelection(e.currentTarget)}
                onBlur={(e) => rememberSelection(e.currentTarget)}
                onChange={(e) => onChangeText(beat.beat_id, 'tts_text', e.target.value)}
                placeholder="留空则自动继承字幕 + 词典替换；填写时必须是整句送音文本..."
                className="w-full p-2 text-xs text-slate-800 bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600 resize-none"
              />
            </>
          ) : (
            /* Patch v4.35：tts_text 为空时只给一行摘要，点一下才展开编辑框——
               一话 120 行表格，每行都挂一个 textarea 会把审核台压到没法看。 */
            <button
              type="button"
              onClick={() => setTtsExpanded(true)}
              title="展开送音文本编辑"
              aria-label={`${beat.beat_id} 展开送音文本编辑`}
              className="w-full text-left px-2 py-1.5 rounded-lg border border-slate-200 bg-slate-50 hover:border-blue-400 transition-colors"
            >
              {matchedRules.length > 0 ? (
                <span className="text-[11px] leading-snug text-blue-700">
                  词典 {matchedRules.map((r) => `${r.source}→${r.replacement}`).join('、')}
                  {' · '}送音「{spokenText}」
                </span>
              ) : (
                <span className="text-[11px] leading-snug text-slate-500">
                  同字幕（未改送音）
                </span>
              )}
              <span className="mt-0.5 block text-[10px] text-blue-600">
                改送音 / 加标签
              </span>
            </button>
          )}
          {tagPickerOpen && (
            <div data-tts-tag-popover className="absolute right-0 top-full z-40 mt-1">
              <TtsTagPicker
                value={beat.tts_text ?? ''}
                selection={ttsSelections.current[beat.beat_id] ?? null}
                ttsModel={ttsModel}
                onPick={(snippet) => onPickTag(beat.beat_id, snippet)}
                onClose={() => onToggleTagPicker(beat.beat_id)}
              />
            </div>
          )}
        </div>
        {ttsExpanded && (
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
        )}
      </td>

      <td className="py-3.5 px-3">
        {/* Patch v4.18：移除情绪指令输入框——情绪指令不再由前端设置，
            合成侧固定使用后端契约里的默认/继承值。
            Patch v4.35：默认只展示「角色 + 落点音色 + 语速」，
            单句音色选择框点「换音色」再展开；ttsModel 提到审核台顶部展示一次。 */}
        <div className="space-y-1.5">
          {/* Patch v3.4：显示「本句配音」（角色 + 音色），并可就地改角色。 */}
          <select
            value={beat.speaker_role ?? ''}
            onChange={(e) => onChangeRole(beat.beat_id, e.target.value)}
            aria-label={`${beat.beat_id} 配音角色`}
            className="w-full px-2 py-1 text-[11px] bg-white border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
          >
            {roleOptions.map((option) => (
              <option key={option.value || '__none__'} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <div className="flex items-center gap-1 text-[11px]">
            <span
              className="text-slate-600 truncate"
              title={`本句配音：${voiceInfo.label} · ${voiceInfo.voiceName} · ${voiceInfo.speed.toFixed(2)}x`}
            >
              {voiceInfo.label} · {voiceInfo.voiceName}
            </span>
            <button
              type="button"
              onClick={() => setVoicePickerOpen(true)}
              title="改这一句的音色（只影响这一句）"
              aria-label={`${beat.beat_id} 换音色`}
              className="shrink-0 rounded border border-slate-200 bg-white px-1 py-0.5 text-[10px] text-slate-500 hover:border-slate-400 hover:text-slate-800"
            >
              换音色
            </button>
            <button
              type="button"
              onClick={() => onPreviewVoice(beat.beat_id, voiceInfo.voice, voiceInfo.speed)}
              disabled={!previewReady}
              aria-label={`${beat.beat_id} 试听当前音色样例`}
              title="播放 MiniMax 静态样例（不消耗 API 额度）"
              className="shrink-0 w-5 h-5 flex items-center justify-center rounded border border-slate-200 bg-white text-slate-500 hover:border-slate-400 hover:text-slate-900 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <Volume2 className="w-3 h-3" />
            </button>
          </div>
          {(voiceInfo.pending || voiceInfo.speedOverridden) && (
            <div className="text-[10px] leading-snug text-slate-400">
              {voiceInfo.pending && (
                <span className="text-amber-600">（保存重跑后生效）</span>
              )}
              {voiceInfo.speedOverridden && (
                <span>（已微调，默认 {voiceInfo.roleSpeed.toFixed(2)}x）</span>
              )}
            </div>
          )}
          {voiceInfo.hint && (
            <div className="text-[10px] leading-snug text-slate-400">
              {voiceInfo.hint}
            </div>
          )}
          {voicePickerOpen && (
            /* Patch v4.27：单句音色覆盖 —— 给一次性 / 龙套角色单独指定 MiniMax 音色。
               只改这一句的 voice_override，不动角色，也不影响其它句。 */
            <div className="space-y-1">
              <div className="flex items-center justify-between gap-1 text-[10px] text-slate-400">
                <span>单句音色覆盖</span>
                <div className="flex items-center gap-1">
                  {overrideVoiceId && (
                    <button
                      type="button"
                      onClick={() => onChangeVoiceOverride(beat.beat_id, '')}
                      aria-label={`${beat.beat_id} 清除单句音色`}
                      title="清除单句指定，恢复按角色解析音色"
                      className="rounded border border-slate-200 bg-white px-1 py-0.5 text-[10px] text-slate-500 hover:border-slate-400 hover:text-slate-800"
                    >
                      清除
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => setVoicePickerOpen(false)}
                    title="收起单句音色选择"
                    aria-label={`${beat.beat_id} 收起单句音色选择`}
                    className="rounded border border-slate-200 bg-white px-1 py-0.5 text-[10px] text-slate-500 hover:border-slate-400 hover:text-slate-800"
                  >
                    收起
                  </button>
                </div>
              </div>
              {customVoiceEditing ? (
                <div className="flex items-center gap-1">
                  <input
                    value={customVoiceDraft}
                    autoFocus
                    onChange={(e) => setCustomVoiceDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        e.preventDefault();
                        commitCustomVoice();
                      } else if (e.key === 'Escape') {
                        e.preventDefault();
                        setCustomVoiceEditing(false);
                      }
                    }}
                    onBlur={() => commitCustomVoice()}
                    placeholder="填写 MiniMax 音色 ID"
                    aria-label={`${beat.beat_id} 自定义音色 ID`}
                    className="w-full px-1.5 py-0.5 text-[11px] bg-white border border-slate-200 rounded focus:outline-none focus:border-blue-600"
                  />
                  <button
                    type="button"
                    onClick={() => setCustomVoiceEditing(false)}
                    aria-label={`${beat.beat_id} 返回下拉选择音色`}
                    title="← 返回下拉选择"
                    className="shrink-0 rounded border border-slate-200 bg-white px-1 py-0.5 text-[10px] text-slate-500 hover:border-slate-400 hover:text-slate-800"
                  >
                    ←
                  </button>
                </div>
              ) : (
                <div className="flex items-center gap-1">
                  <select
                    value={customVoiceEditing ? CUSTOM_VOICE : overrideVoiceId}
                    onChange={(e) => {
                      const next = e.target.value;
                      if (next === CUSTOM_VOICE) {
                        setCustomVoiceDraft(overrideVoiceId);
                        setCustomVoiceEditing(true);
                        return;
                      }
                      setCustomVoiceEditing(false);
                      onChangeVoiceOverride(beat.beat_id, next);
                    }}
                    aria-label={`${beat.beat_id} 单句音色`}
                    className="w-full px-1.5 py-0.5 text-[11px] bg-white border border-slate-200 rounded focus:outline-none focus:border-blue-600"
                  >
                    <option value="">跟随角色（{roleVoiceDisplayName}）</option>
                    {overrideVoiceId && !overrideKnown && (
                      <option value={overrideVoiceId}>自定义：{overrideVoiceId}</option>
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
                    <option value={CUSTOM_VOICE}>✎ 自定义音色 ID…</option>
                  </select>
                </div>
              )}
              {overrideVoiceId && (
                <div className="text-[10px] leading-snug text-blue-700">
                  已单独指定：{voiceInfo.voiceName}
                </div>
              )}
            </div>
          )}
          {/* Patch v4.13：表格行紧凑语速微调（-/+ 步进 + 数字 + 恢复默认）。 */}
          <div className="flex items-center gap-1 text-[11px] font-mono-tabular">
            <span className="text-slate-400">语速</span>
            <button
              type="button"
              onClick={() => onNudgeSpeed(beat.beat_id, -0.02)}
              aria-label={`${beat.beat_id} 语速降低`}
              className="w-5 h-5 rounded border border-slate-200 bg-white text-slate-600 leading-none hover:border-slate-400 hover:text-slate-900"
            >
              −
            </button>
            <span
              className={`w-12 text-center ${
                voiceInfo.speedOverridden
                  ? 'text-blue-700 font-semibold'
                  : 'text-slate-500'
              }`}
              title={
                voiceInfo.speedOverridden
                  ? `已逐句微调，角色默认 ${voiceInfo.roleSpeed.toFixed(2)}x`
                  : `跟随角色默认 ${voiceInfo.roleSpeed.toFixed(2)}x`
              }
            >
              {voiceInfo.speed.toFixed(2)}x
            </span>
            <button
              type="button"
              onClick={() => onNudgeSpeed(beat.beat_id, 0.02)}
              aria-label={`${beat.beat_id} 语速提高`}
              className="w-5 h-5 rounded border border-slate-200 bg-white text-slate-600 leading-none hover:border-slate-400 hover:text-slate-900"
            >
              +
            </button>
            {voiceInfo.speedOverridden && (
              <button
                type="button"
                onClick={() => onResetSpeed(beat.beat_id)}
                aria-label={`${beat.beat_id} 语速恢复默认`}
                title="恢复为角色默认语速"
                className="rounded border border-slate-200 bg-white px-1.5 py-0.5 text-[10px] text-slate-500 hover:border-slate-400 hover:text-slate-800"
              >
                恢复默认
              </button>
            )}
          </div>
          {/* Patch v4.38：本句音效提示（sfx_cue）——下拉选 24 个标准音效之一，
              紧邻一个试听按钮；盘面缺素材时会标注「·通用兜底」。 */}
          <div className="flex items-center gap-1 pt-1">
            <span className="shrink-0 text-[10px] text-slate-400">音效</span>
            <select
              value={beat.sfx_cue ?? ''}
              onChange={(e) =>
                onChangeSfxCue?.(beat.beat_id, e.target.value || null)
              }
              aria-label={`${beat.beat_id} 音效`}
              title="这一句的音效提示；保存后从 S6 重跑即可生效"
              className="min-w-0 flex-1 px-2 py-1 text-[11px] bg-white border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
            >
              <option value="">无音效</option>
              {sfxGroups.map((group) => (
                <optgroup key={group.category} label={group.category}>
                  {group.items.map((item) => (
                    <option key={item.key} value={item.key}>
                      {item.label}
                      {item.usedFallback ? '·通用兜底' : ''}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            <button
              type="button"
              onClick={() => previewSfx(beat.sfx_cue ?? '')}
              disabled={!beat.sfx_cue || !currentSfxPlayable}
              aria-label={`${beat.beat_id} 试听当前音效`}
              title={
                beat.sfx_cue
                  ? currentSfxPlayable
                    ? '试听这一句的音效素材'
                    : '盘面暂无该音效素材'
                  : '这一句还没有选音效'
              }
              className="shrink-0 w-5 h-5 flex items-center justify-center rounded border border-slate-200 bg-white text-slate-500 hover:border-slate-400 hover:text-slate-900 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <Volume2 className="w-3 h-3" />
            </button>
          </div>
        </div>
      </td>

      <td className="py-3.5 px-3 space-y-2">
        <select
          value={beat.motion}
          onChange={(e) =>
            onChangeMotion(beat.beat_id, e.target.value as CameraMotion)
          }
          className="w-full px-2 py-1.5 text-[11px] font-mono-tabular bg-white border border-slate-200 rounded-lg"
        >
          {motionOptions(beat.motion).map((val) => (
            <option key={val} value={val}>
              {MOTION_LABELS[val]}
            </option>
          ))}
        </select>

        <div className="flex items-center gap-1.5">
          <span className="text-[11px] text-slate-500 whitespace-nowrap">停顿ms</span>
          <input
            type="number"
            step={50}
            min={0}
            max={2000}
            value={beat.pause_after_ms}
            onChange={(e) =>
              onChangePause(
                beat.beat_id,
                // Patch v4.8：整数 + 0~2000ms 钳位，避免越界值让时间轴算崩。
                Math.max(0, Math.min(2000, Math.round(Number(e.target.value) || 0)))
              )
            }
            // Patch v4.29：数字框太窄时 Chrome 的上下微调箭头会把数字整段挤掉，
            // 用户只能看到一个「‹」。这里给足最小宽度并让输入框吃掉剩余空间，
            // 同时用 hide-spinner 隐藏箭头（见 index.css），保证 0~2000 的数字完整可见。
            className="hide-spinner flex-1 min-w-[76px] w-auto px-2 py-1 text-[11px] font-mono-tabular bg-white border border-slate-200 rounded"
          />
        </div>
      </td>

      <td className="py-3.5 pl-3 pr-5 text-right">
        <div className="flex items-center justify-end gap-1.5">
          <button
            type="button"
            onClick={() => onPreview(beat.beat_id)}
            className={`inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border text-[11px] font-medium whitespace-nowrap shrink-0 cursor-pointer transition-colors ${
              playingBeatId === beat.beat_id
                ? 'border-blue-600 bg-blue-50 text-blue-700'
                : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
            }`}
          >
            <Volume2 className="w-3.5 h-3.5" />
            {playingBeatId === beat.beat_id ? '试听中' : '试听'}
          </button>
          {regenerateEnabled && (
            <button
              type="button"
              onClick={() => onRegenerate(beat.beat_id)}
              title="只重新合成这一句的配音，其它句子不受影响"
              className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg border text-[11px] font-medium whitespace-nowrap shrink-0 cursor-pointer transition-colors border-slate-200 bg-white text-slate-700 hover:bg-amber-50 hover:border-amber-300 hover:text-amber-700"
            >
              <RotateCcw className="w-3.5 h-3.5" />
              重配
            </button>
          )}
          <button
            type="button"
            onClick={() => onInsert(beat.beat_id)}
            title="在本格之后插入一条解说句"
            aria-label={`在 ${beat.beat_id} 之后插入解说句`}
            className="p-1.5 text-slate-400 hover:text-blue-600 cursor-pointer"
          >
            <Plus className="w-3.5 h-3.5" />
          </button>
          <button
            type="button"
            onClick={() => onDelete(beat.beat_id)}
            aria-label={`删除 ${beat.beat_id}`}
            className="p-1.5 text-slate-400 hover:text-red-600 cursor-pointer"
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        </div>
      </td>
    </tr>
  );
});

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
  onCheckCharacters,
  onAutoPacing,
  onAutoMotions,
  onAddRule,
  onUpdateEpisodeRenderOptions,
  onExportEpisode,
  onCleanupIntermediates,
  onRegenerateBeatTts,
}) => {
  const [activeTab, setActiveTab] = useState<StudioTab>('script');
  // Patch v4.36：切幕预备模式下，用户先看到的是「镜头编排」而不是解说审核台
  // （此刻还没有解说稿）。有分镜但一句稿都没有时自动切过去——每个话次只
  // 自动切一次，之后用户手动选 tab 不会被抢回来。
  const storyboardDefaultAppliedRef = useRef<string>('');
  useEffect(() => {
    if (!episode.episode_id) return;
    if (storyboardDefaultAppliedRef.current === episode.episode_id) return;
    storyboardDefaultAppliedRef.current = episode.episode_id;
    if (episode.panels.length > 0 && episode.beats.length === 0) {
      setActiveTab('storyboard');
    }
  }, [episode.episode_id, episode.panels.length, episode.beats.length]);

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

  // ---- Patch v4.28：全自动分配画面动效（后端按分镜几何 / 情绪改写 motion） ----
  const [autoMotionLoading, setAutoMotionLoading] = useState(false);
  const [autoMotionSummary, setAutoMotionSummary] = useState<AutoMotionSummary | null>(null);
  const [autoMotionError, setAutoMotionError] = useState<string | null>(null);

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

  /* ------------------------------------------------------------------
   * Patch v4.28：一键自动分配画面动效
   * 后端按分镜几何（超长竖格 / 横向宽格）与情绪（冲击 / 紧张）改写
   * 每句的 motion；只改运镜，不动配音，因此用户只需重跑 S6~S7。
   * ---------------------------------------------------------------- */
  const handleRunAutoMotions = useCallback(async () => {
    if (!onAutoMotions || autoMotionLoading) return;
    if (tableDirtyRef.current) {
      setSaveHint('请先保存解说稿再执行自动分配动效');
      return;
    }
    setAutoMotionLoading(true);
    setAutoMotionError(null);
    try {
      const result = await onAutoMotions(episode.episode_id);
      if (!result) {
        setAutoMotionError('自动分配动效执行失败，请查看后端日志后重试');
        return;
      }
      draftBeatsRef.current = result.beats;
      setDraftBeats(result.beats);
      tableDirtyRef.current = false;
      setTableDirty(false);
      setSaveHint('');
      setAutoMotionSummary(result.summary);
      // Patch v4.28：运镜被后端改写过 → 必须重建时间轴与成片（S6~S7）。
      if (result.summary.updated_count > 0) {
        setNeedsTimelineRebuild(true);
      }
    } catch (err) {
      setAutoMotionError(err instanceof Error ? err.message : String(err));
    } finally {
      setAutoMotionLoading(false);
    }
  }, [autoMotionLoading, episode.episode_id, onAutoMotions]);

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
  /** Patch v4.13：本话的镜头组（幕），供「归属幕」下拉选择。 */
  const storyboardScenes = sceneStoryboard?.scenes ?? EMPTY_SCENES;
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
      // Patch v4.13：幕归属优先用剧本里显式记录的 scene_id（幕与分句画面已解耦，
      // 同一格漫画可以被多句复用）；没有 scene_id 时才回退按第一张分镜反查。
      const sceneId = (beat.scene_id ?? '').trim();
      const owner = sceneId
        ? scenes.find((scene) => scene.scene_id === sceneId)
        : scenes.find((scene) => scene.panel_ids.includes(beat.target_panel_ids[0]));
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
  /**
   * Patch v4.25：哪些幕在审核表里处于展开状态。
   *
   * 默认空集 = **全部幕默认折叠**（一话 200~300 分镜时全表渲染太重）；
   * 折叠态不挂载 `<table>` / `<tr>` DOM，只渲染一行幕头部。
   */
  const [expandedSceneKeys, setExpandedSceneKeys] = useState<Set<string>>(
    () => new Set<string>()
  );
  /** 保存中状态，避免重复点击。 */
  const [savingBeats, setSavingBeats] = useState(false);
  /** Patch v4.25：角色名预检进行中（预检期间禁用保存按钮，避免双击重复提交）。 */
  const [characterCheckBusy, setCharacterCheckBusy] = useState(false);
  /** 表格保存失败或校验失败时的提示文案。 */
  const [saveHint, setSaveHint] = useState('');
  /**
   * Patch v4.33：「全部按主画面」的处理结果提示。
   *
   * Stage 4 会给一句解说配多格分镜（Stage 6 按格数均分时长轮播），
   * 用户没有逐格确认过 —— 成片里就会播到用户没参与决策的画面。
   * 这个提示把「改了几句、保存后要重跑什么」一次说清。
   */
  const [multiPanelHint, setMultiPanelHint] = useState('');
  /* Patch v4.38：音效曲库（24 个标准键 + 盘面可用情况）。 */
  const [sfxLibrary, setSfxLibrary] = useState<SfxLibraryItem[]>([]);
  useEffect(() => {
    let cancelled = false;
    fetchSfxLibrary()
      .then((data) => {
        if (!cancelled) setSfxLibrary(data.items ?? []);
      })
      .catch(() => {
        /* 音效曲库拉不到不阻塞审核台：下拉退化成只有「无音效」 */
      });
    return () => {
      cancelled = true;
    };
  }, []);
  // ---- Patch v4.35：审核台命令栏的本地派生状态 ----
  /** 即时搜索框的关键词（beat_id / 字幕 / 角色 / 目标分镜）。 */
  const [scriptQuery, setScriptQuery] = useState('');
  /** 审核台的轻量筛选档位。 */
  const [scriptFilter, setScriptFilter] = useState<ScriptFilterKey>('all');
  /** 「批量处理」下拉菜单是否展开。 */
  const [batchMenuOpen, setBatchMenuOpen] = useState(false);
  /** 「+ 多音字规则」快捷弹层是否展开。 */
  const [rulePopoverOpen, setRulePopoverOpen] = useState(false);
  /** 多音字规则弹层里的待新增词条草稿。 */
  const [ruleDraft, setRuleDraft] = useState({ source: '', replacement: '' });
  /** 已被用户手动关掉的结果条 key（同一 key 不再重复弹出）。 */
  const [dismissedNotices, setDismissedNotices] = useState<string[]>([]);
  /** Patch v4.23：角色名写法告警的确认弹窗（null = 不显示）。 */
  const [charPrompt, setCharPrompt] = useState<CharacterPrompt | null>(null);
  /**
   * Patch v4.25：角色档案同步选项。
   *
   * key = (found_name, suggested_canonical) 聚合键；value = 用户勾选的同步动作。
   * 缺省（没有 key）表示「仅本次放行，不改档案」——这是默认行为。
   */
  const [charSyncChoices, setCharSyncChoices] = useState<
    Record<string, CharacterSyncAction>
  >({});
  const [playingBeatId, setPlayingBeatId] = useState<string | null>(null);
  /** 作品级配音设置（角色清单 + 各角色音色），用于「本句配音」的角色下拉。 */
  const [seriesTts, setSeriesTts] = useState<SeriesTtsConfigResponse | null>(null);
  /** Patch v4.2：全部可选音色（百炼 + MiniMax），供弹窗逐句指定音色。 */
  const [voiceOptions, setVoiceOptions] = useState<VoiceOption[]>([]);
  /** Patch v4.1 Patch 21：待确认的「清理重置」目标阶段（内联确认横幅）。 */
  const [resetTarget, setResetTarget] = useState<StageId | null>(null);
  const [resetIncludeHuman, setResetIncludeHuman] = useState(false);
  const [resetBusy, setResetBusy] = useState(false);
  /** Patch v4.11：一键清理过程文件的内联二次确认条与忙碌态。 */
  const [cleanupConfirm, setCleanupConfirm] = useState(false);
  const [cleanupBusy, setCleanupBusy] = useState(false);

  // ---- Patch v4.35：顶部「流水线」与「封页」两块可折叠，首屏空间还给分镜与审核表 ----
  /**
   * 折叠态（localStorage 记忆，跨话次保留偏好）。
   *
   * 但**有 running / failed 阶段时必须自动展开**——那正是用户此刻要看的东西，
   * 折叠起来等于把故障藏起来。
   */
  const [pipelineCollapsed, setPipelineCollapsed] = useState(() =>
    readCollapsePref(PIPELINE_COLLAPSE_KEY)
  );
  const [brandingCollapsed, setBrandingCollapsed] = useState(() =>
    readCollapsePref(BRANDING_COLLAPSE_KEY)
  );

  // ---- Patch v4.12：单句重配 TTS + 真实试听 ----
  /** 重配确认条当前指向的 beat_id；非空时显示内联确认条（禁用 window.confirm）。 */
  const [regenerateTarget, setRegenerateTarget] = useState<string | null>(null);
  const [regenerateBusy, setRegenerateBusy] = useState(false);
  /** 音频 URL 的防缓存版本号：每次重配成功后自增，强制 <audio> 拉新文件。 */
  const [audioRevision, setAudioRevision] = useState(0);
  /** 全局只保留一个正在播放的 audio 元素，便于停止 / 切换。 */
  const audioElRef = useRef<HTMLAudioElement | null>(null);

  // ---- Patch v4.14：MiniMax 语气词 / 句中停顿标签 ----
  /** 标签浮层当前作用的 beat_id（非空即展开浮层）。 */
  const [tagPickerBeatId, setTagPickerBeatId] = useState<string | null>(null);
  /** 每条分镜的送音文本 textarea 引用（插入后要还焦、光标停在标签后）。 */
  const ttsTextareaRefs = useRef<Record<string, HTMLTextAreaElement | null>>({});
  /** 每条分镜最近一次的选区 {start, end}。 */
  const ttsSelections = useRef<Record<string, { start: number; end: number }>>({});

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

  /**
   * Patch v4.29：重跑 Stage 4 后本话的 beats 会整体换一批（编排重组后
   * 解说跟着重排）。若幕编排还是重组前的那份，按幕分组渲染就会把新剧本
   * 挂到旧幕上——审核页看到的就是「分镜头乱序、内容与画面对不上」。
   * 这里在 beats 变化时同步补拉一次最新的 storyboard，保证分组依据与
   * 新剧本一致（与上面按 Tab 加载的 effect 互相独立，互为兜底）。
   */
  useEffect(() => {
    if (activeTab !== 'script') return;
    if (!episode.beats || episode.beats.length === 0) return;
    let cancelled = false;
    const episodeBase = `/api/episodes/${encodeURIComponent(episode.episode_id)}`;
    void (async () => {
      const res = await fetch(`${episodeBase}/storyboard`);
      if (cancelled) return;
      if (res.ok) {
        setSceneStoryboard((await res.json()) as EpisodeStoryboard);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeTab, episode.episode_id, episode.beats]);

  // Patch v4.8：换话次时清掉「停顿已改 / 待裁决重跑」的跨话残留状态。
  useEffect(() => {
    setNeedsTimelineRebuild(false);
    setPendingRerunChoice(null);
    setRerunNotice('');
    // Patch v4.25：换话次时幕的折叠状态重置为「全部折叠」。
    setExpandedSceneKeys(new Set<string>());
  }, [episode.episode_id]);

  // Patch v3.4：拉作品级配音设置——审核台要显示「本句配音用哪个角色 / 哪个音色」，
  // 并允许就地改角色（改完保存并重跑 Stage 5 生效）。
  /** Patch v4.25：同步完角色档案后要重新拉一次，让「本句配音」下拉立刻反映新档案。 */
  const reloadSeriesTts = useCallback(async () => {
    try {
      const res = await fetch(`/api/series/${encodeURIComponent(seriesId)}/tts-config`);
      if (!res.ok) return;
      const data = (await res.json()) as SeriesTtsConfigResponse;
      setSeriesTts(data);
    } catch {
      // 拉不到就退回「只显示后端已解析结果」的降级模式，不影响审核台其他功能。
    }
  }, [seriesId]);

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
        // Patch v4.15：音色选择仅保留 MiniMax（剔除百炼 / 阿里音色）。
        setVoiceOptions([...(data.minimax_voices ?? [])]);
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
   * Patch v4.27：MiniMax 音色分组（供应商 · 分类）。
   *
   * 一话 200~300 句时每行都渲染一份音色下拉，分组必须**只算一次**：
   * 顶层算好后把引用稳定的数组传给每个 memo 行，行内不再重复 groupVoices。
   */
  const voiceGroups = useMemo(() => groupVoices(voiceOptions), [voiceOptions]);

  /**
   * Patch v4.27：音色 ID → 音色条目。
   *
   * 后端给的是 voice_id（个别条目只有 id），两个键都索引一份，查表 O(1)。
   */
  const voiceOptionMap = useMemo(() => {
    const map = new Map<string, VoiceOption>();
    voiceOptions.forEach((option) => {
      if (option.voice_id) map.set(option.voice_id, option);
      if (option.id) map.set(option.id, option);
    });
    return map;
  }, [voiceOptions]);

  /**
   * Patch v4.27：全表共享的音色试听器。
   *
   * 只在顶层挂一个（一个 <audio> 实例），行内按钮都调它，避免 300 行各挂一个。
   * 换话次或组件卸载时停掉正在播放的样例。
   */
  const { preview: previewVoiceSample, stop: stopVoiceSample } = useVoicePreview();
  useEffect(() => {
    stopVoiceSample();
  }, [episode.episode_id, stopVoiceSample]);
  useEffect(() => () => stopVoiceSample(), [stopVoiceSample]);

  /** Patch v4.27：试听某一句的音色样例（静态样例，不消耗 API 额度）。 */
  const handlePreviewBeatVoice = useCallback(
    (beatId: string, voiceId: string, speed: number) => {
      previewVoiceSample(`beat:${beatId}`, { voice: voiceId, speed }, voiceOptions);
    },
    [previewVoiceSample, voiceOptions]
  );

  /**
   * 角色 → 性别。
   *
   * 改角色时必须同步 speaker_gender：Stage 4 留下的性别属于**原来那个角色**
   * （男角色改成女角色后仍是 male），不跟着改就会用错音色的兜底槽位。
   */
  const genderForRole = (role: string): 'male' | 'female' | 'unknown' =>
    genderForRoleOf(role, seriesTts);

  /**
   * 改说话人角色时要同步落的字段（表格行内 select 与分镜弹窗共用同一套规则）：
   * a) speaker_gender 始终跟着角色走，选回「不指定」（空串）时重置为 unknown；
   * b) 清掉历史逐句音色 voice_override，恢复按角色解析，避免界面角色与实际音色不一致；
   * c) Patch v4.4：一并清空单句语速微调 speed_override，让新角色自带的默认语速自动生效。
   */
  const speakerRolePatch = (
    beat: ScriptBeat,
    nextRole: string
  ): Partial<ScriptBeat> => speakerRolePatchOf(beat, nextRole, seriesTts);

  /**
   * 说话人候选（审核台表格与分镜弹窗共用同一份）。
   *
   * 只让用户选**角色**，音色由角色自动带出（讲解者 / 通用男声 / 通用女声 / 角色专属），
   * 避免在一堆音色 ID 里挑花眼。当前角色可能是旧槽位代号（如 male_antagonist），
   * 不在候选里时补一条，免得下拉显示空白、用户看不出这一句到底是谁在念。
   */
  const roleOptionsFor = (beat: ScriptBeat): RoleOption[] =>
    roleOptionsForBeat(beat, seriesTts, roleNames);

  /**
   * Patch v4.4：按当前 ``speaker_role`` 从作品级 TTS 配置算出角色默认语速。
   *
   * 讲解者 / 角色专属 / 通用男女声各取对应槽位的 ``speed``；本地配置找不到时，
   * 依次回落后端下发的 ``role_speed``、当前生效 ``speed``，最后兜底 1.15。
   */
  const roleSpeedForBeat = (beat: ScriptBeat): number =>
    roleSpeedForBeatOf(beat, seriesTts);

  /**
   * 一条 beat 的「本句配音」说明。
   *
   * 后端已给出权威结果（``resolved_voice``，与 Stage 5 同一套解析）；这里再按当前
   * 下拉选择做一次**本地预览**，改角色后能立刻看到会换成谁，保存重跑后以服务端为准。
   *
   * Patch v4.4：返回值同时携带语速三件套——``roleSpeed``（角色默认语速）、
   * ``speed``（当前生效语速，微调后等于 ``speed_override``）、``speedOverridden``。
   */
  const beatVoiceInfo = (beat: ScriptBeat) =>
    beatVoiceInfoOf(beat, seriesTts, roleNames, voiceOptionMap);

  /**
   * Patch v4.25：分镜 ID → 分镜 的查找表。
   *
   * 原来审核表每一行都在 `episode.panels.find(...)` 里线性扫一遍（O(N×M)），
   * 一话 200~300 分镜时整表要扫 6 万次；改成 Map 之后每行只查一次哈希。
   */
  const panelMap = useMemo(() => {
    const map = new Map<string, PanelItem>();
    episode.panels.forEach((panel) => map.set(panel.panel_id, panel));
    return map;
  }, [episode.panels]);

  /** Patch v4.25：分镜下拉的选项数组预计算（引用稳定，编辑单句不会重建整表）。 */
  const panelOptions = useMemo(
    () =>
      episode.panels.map((panel) => ({
        value: panel.panel_id,
        label: panel.panel_id,
      })),
    [episode.panels]
  );

  /**
   * Patch v4.32：镜头编排（启用幕）覆盖的分镜集合 —— 「用户在分镜里选过的画面」
   * 的唯一权威口径。
   *
   * 审核台的分镜下拉只列这批分镜，避免用户在这里挑到编排范围外的画面
   * （那批画面从没进过镜头编排，成片里出现就是「我没选过的画面」）。
   * 编排为空（还没编排 / 没有启用幕）时返回空集合 = 不限制，与后端口径一致。
   */
  const scopePanelIds = useMemo(() => {
    const ids = new Set<string>();
    storyboardScenes.forEach((scene) => {
      if (scene.enabled === false) return;
      scene.panel_ids.forEach((pid) => ids.add(pid));
    });
    return ids;
  }, [storyboardScenes]);

  /** Patch v4.25：归属幕下拉的选项数组预计算（引用稳定）。 */
  const sceneOptions = useMemo(
    () =>
      storyboardScenes.map((scene) => ({
        value: scene.scene_id,
        label: `${scene.scene_index}. ${scene.title || scene.scene_id}`,
      })),
    [storyboardScenes]
  );

  /**
   * Patch v4.25：某条 beat 属于哪一幕（与 ``beatSceneGroups`` 同一口径）。
   *
   * 优先用 beat.scene_id；没写就按它指向的分镜反查幕；都没有则落到「未归幕」兜底组。
   */
  const sceneKeyOfBeat = useCallback(
    (beat: ScriptBeat, scenes: StoryboardScene[]): string => {
      if (scenes.length === 0) return '__all__';
      const explicit = (beat.scene_id ?? '').trim();
      if (explicit && scenes.some((scene) => scene.scene_id === explicit)) {
        return explicit;
      }
      const panelId = beat.target_panel_ids[0];
      const matched = scenes.find((scene) => scene.panel_ids.includes(panelId));
      return matched ? matched.scene_id : '__orphan__';
    },
    []
  );

  /**
   * Patch v4.25：哪些幕里还有没落盘的草稿改动。
   *
   * 折叠态的幕头部要显示一个「未保存」小圆点，提示用户这一幕改了还没存。
   * 判定口径与 ``hasDraftChanges`` 一致：只比对用户可编辑字段，
   * 后端派生的 audio_duration_ms / resolved_voice 不算改动。
   */
  const dirtySceneKeys = useMemo(() => {
    const savedMap = new Map(episode.beats.map((beat) => [beat.beat_id, beat]));
    const draftIds = new Set(draftBeats.map((beat) => beat.beat_id));
    const scenes = storyboardScenes.filter((scene) => scene.enabled !== false);
    const keys = new Set<string>();
    draftBeats.forEach((beat) => {
      const other = savedMap.get(beat.beat_id);
      const changed =
        !other ||
        DRAFT_EDITABLE_FIELDS.some(
          (field) => normDraftField(beat[field]) !== normDraftField(other[field])
        );
      if (changed) keys.add(sceneKeyOfBeat(beat, scenes));
    });
    // 被删掉的句子要在它原来那一幕上留下脏标记。
    episode.beats.forEach((beat) => {
      if (!draftIds.has(beat.beat_id)) keys.add(sceneKeyOfBeat(beat, scenes));
    });
    return keys;
  }, [draftBeats, episode.beats, storyboardScenes, sceneKeyOfBeat]);

  /**
   * Patch v4.35：流水线里有没有「正在跑」或「失败」的阶段。
   *
   * 有的话强制展开折叠块——用户点开这一页就是想看谁卡住了。
   */
  const pipelineNeedsAttention = useMemo(
    () =>
      episode.stages.some(
        (st) => st.status === 'running' || st.status === 'failed'
      ),
    [episode.stages]
  );

  useEffect(() => {
    if (pipelineNeedsAttention) setPipelineCollapsed(false);
  }, [pipelineNeedsAttention]);

  /**
   * Patch v4.35：这些情况下流水线块**不允许折叠**。
   *
   * 有阶段在跑 / 跑挂了、或者用户正处在「清理重置 / 清理过程文件」的确认流程里，
   * 这时把整块折起来等于把问题或半完成的操作藏起来。
   */
  const pipelineForceOpen =
    pipelineNeedsAttention || resetTarget !== null || cleanupConfirm;

  /**
   * Patch v4.35：封页块在「正在保存 / 保存出错」时不折叠——
   * 用户要立刻看到保存结果，折叠起来会把反馈吞掉。
   */
  const brandingForceOpen = brandingSaving || !!brandingError;

  /** Patch v4.35：折叠 / 展开流水线块（偏好落 localStorage）。 */
  const togglePipelineCollapsed = useCallback(() => {
    setPipelineCollapsed((prev) => {
      const next = !prev;
      writeCollapsePref(PIPELINE_COLLAPSE_KEY, next);
      return next;
    });
  }, []);

  /** Patch v4.35：折叠 / 展开封页块（偏好落 localStorage）。 */
  const toggleBrandingCollapsed = useCallback(() => {
    setBrandingCollapsed((prev) => {
      const next = !prev;
      writeCollapsePref(BRANDING_COLLAPSE_KEY, next);
      return next;
    });
  }, []);

  /**
   * Patch v4.35：流水线摘要条用的阶段概览（已完成 / 总数 + 各阶段状态短记）。
   */
  const pipelineSummary = useMemo(() => {
    const total = episode.stages.length;
    const done = episode.stages.filter((st) => st.status === 'done').length;
    const running = episode.stages.filter((st) => st.status === 'running').length;
    const failed = episode.stages.filter((st) => st.status === 'failed').length;
    return { total, done, running, failed };
  }, [episode.stages]);

  /**
   * Patch v4.35：审核台的搜索 + 筛选命中集合（各档位计数）。
   *
   * 搜索口径：beat_id / 字幕原文 / 角色 / 目标分镜 id（任一命中即算命中）。
   * 筛选档位与命令栏上的五个切换按钮一一对应。
   */
  const scriptFilterState = useMemo(() => {
    const query = scriptQuery.trim().toLowerCase();
    const counts: Record<ScriptFilterKey, number> = {
      all: 0,
      length: 0,
      multi_panel: 0,
      custom_tts: 0,
      voice_tuned: 0,
    };
    const matched = new Set<string>();
    for (const beat of draftBeats) {
      const charLen = beat.narration_text.trim().length;
      const isLengthWarn = charLen < 8 || charLen > 30;
      const isMultiPanel = (beat.target_panel_ids?.length ?? 0) > 1;
      const { matchedRules } = resolveSpokenText(beat, rules);
      const isCustomTts = !!beat.tts_text?.trim() || matchedRules.length > 0;
      const isVoiceTuned =
        !!beat.voice_override?.trim() || beat.speed_override !== null && beat.speed_override !== undefined;
      counts.all += 1;
      if (isLengthWarn) counts.length += 1;
      if (isMultiPanel) counts.multi_panel += 1;
      if (isCustomTts) counts.custom_tts += 1;
      if (isVoiceTuned) counts.voice_tuned += 1;
      if (query) {
        const haystack = [
          beat.beat_id,
          beat.narration_text,
          beat.speaker_role ?? '',
          (beat.target_panel_ids ?? []).join(' '),
        ]
          .join(' ')
          .toLowerCase();
        if (!haystack.includes(query)) continue;
      }
      const pass =
        scriptFilter === 'all' ||
        (scriptFilter === 'length' && isLengthWarn) ||
        (scriptFilter === 'multi_panel' && isMultiPanel) ||
        (scriptFilter === 'custom_tts' && isCustomTts) ||
        (scriptFilter === 'voice_tuned' && isVoiceTuned);
      if (pass) matched.add(beat.beat_id);
    }
    return { counts, matched };
  }, [draftBeats, rules, scriptQuery, scriptFilter]);

  /**
   * Patch v4.35：未保存的句子条数。
   *
   * 命令栏右上角显示「N 处未保存」——比一个模糊的圆点徽标更能说明「还剩多少活没落盘」。
   * 口径与 dirtySceneKeys 一致：只比对用户可编辑字段。
   */
  const dirtyBeatCount = useMemo(() => {
    const savedMap = new Map(episode.beats.map((beat) => [beat.beat_id, beat]));
    const draftIds = new Set(draftBeats.map((beat) => beat.beat_id));
    let count = 0;
    draftBeats.forEach((beat) => {
      const other = savedMap.get(beat.beat_id);
      const changed =
        !other ||
        DRAFT_EDITABLE_FIELDS.some(
          (field) => normDraftField(beat[field]) !== normDraftField(other[field])
        );
      if (changed) count += 1;
    });
    // 被删掉的句子也算一处未保存。
    episode.beats.forEach((beat) => {
      if (!draftIds.has(beat.beat_id)) count += 1;
    });
    return count;
  }, [draftBeats, episode.beats]);

  /**
   * Patch v4.35：分镜列表的搜索与筛选。
   *
   * 一话 200~300 张分镜时，「翻列表找那一格」全靠滚。这里给搜索 + 四态筛选，
   * 再加 ↑/↓ 键盘切换，找格子的成本从「滚动 + 辨认」降到「输入 + 方向键」。
   */
  const [panelQuery, setPanelQuery] = useState('');
  const [panelFilter, setPanelFilter] = useState<'all' | 'dialogue' | 'skipped' | 'tall'>(
    'all'
  );
  /** Patch v4.35：从分镜详情跳到审核台要高亮的句子。 */
  const [highlightBeatId, setHighlightBeatId] = useState<string | null>(null);
  /** Patch v4.35：分镜列表容器（键盘 ↑/↓ 切换时用来 scrollIntoView）。 */
  const panelListRef = useRef<HTMLDivElement | null>(null);
  /** Patch v4.35：分镜 → 所属幕（按编排页的幕顺序取第一个命中的）。 */
  const panelSceneOwner = useMemo(() => {
    const map = new Map<string, { sceneId: string; sceneIndex: number; title: string }>();
    (sceneStoryboard?.scenes ?? []).forEach((scene, index) => {
      scene.panel_ids.forEach((panelId) => {
        if (!map.has(panelId)) {
          map.set(panelId, { sceneId: scene.scene_id, sceneIndex: index, title: scene.title });
        }
      });
    });
    return map;
  }, [sceneStoryboard]);

  /** Patch v4.35：分镜 → 关联解说句（target_panel_ids 命中即算关联）。 */
  const panelBeatsByPanel = useMemo(() => {
    const map = new Map<string, ScriptBeat[]>();
    draftBeats.forEach((beat) => {
      (beat.target_panel_ids ?? []).forEach((panelId) => {
        const list = map.get(panelId) ?? [];
        list.push(beat);
        map.set(panelId, list);
      });
    });
    return map;
  }, [draftBeats]);

  /** Patch v4.35：按搜索 + 筛选过滤后的分镜列表。 */
  const filteredPanels = useMemo(() => {
    const query = panelQuery.trim().toLowerCase();
    return episode.panels.filter((panel) => {
      if (panelFilter === 'skipped' && !panel.is_transition_blank) return false;
      if (panelFilter === 'dialogue' && !(panel.dialogues?.length ?? 0)) return false;
      // Patch v4.35：长竖格判定与后端 TALL_PANEL_RATIO（1.5）同口径。
      if (panelFilter === 'tall' && !(panel.height > panel.width * 1.5)) return false;
      if (!query) return true;
      const dialoguesText = (panel.dialogues ?? []).map((d) => d.text).join(' ');
      const haystack = `${panel.panel_id} ${panel.scene_summary ?? ''} ${dialoguesText}`.toLowerCase();
      return haystack.includes(query);
    });
  }, [episode.panels, panelFilter, panelQuery]);

  /** Patch v4.35：↑/↓ 切换分镜并把选中项滚进可视区。 */
  const movePanelSelection = useCallback(
    (delta: number) => {
      const list = filteredPanels;
      if (list.length === 0) return;
      const current = list.findIndex((p) => p.panel_id === selectedPanelId);
      const nextIndex =
        current < 0 ? 0 : Math.min(list.length - 1, Math.max(0, current + delta));
      const next = list[nextIndex];
      if (!next) return;
      setSelectedPanelId(next.panel_id);
      requestAnimationFrame(() => {
        const node = panelListRef.current?.querySelector<HTMLElement>(
          `[data-panel-id="${next.panel_id}"]`
        );
        node?.scrollIntoView({ block: 'nearest' });
      });
    },
    [filteredPanels, selectedPanelId]
  );

  /** Patch v4.35：从分镜详情跳到审核台——展开对应幕并高亮该句。 */
  const jumpToBeatInScript = useCallback(
    (beatId: string) => {
      const group = beatSceneGroups.find((g) => g.beats.some((b) => b.beat_id === beatId));
      if (group) setExpandedSceneKeys((prev) => new Set(prev).add(group.key));
      setHighlightBeatId(beatId);
      setActiveTab('script');
    },
    [beatSceneGroups]
  );

  /** Patch v4.35：从分镜跳到审核台后，把目标行滚到屏幕中央。 */
  useEffect(() => {
    if (activeTab !== 'script' || !highlightBeatId) return;
    const scrollTimer = window.setTimeout(() => {
      const node = document.querySelector<HTMLElement>(`[data-beat-id="${highlightBeatId}"]`);
      node?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }, 150);
    const clearTimer = window.setTimeout(() => setHighlightBeatId(null), 6000);
    return () => {
      window.clearTimeout(scrollTimer);
      window.clearTimeout(clearTimer);
    };
  }, [activeTab, expandedSceneKeys, highlightBeatId]);
  const activePanel = useMemo(
    () => panelMap.get(selectedPanelId) ?? episode.panels[0],
    [panelMap, episode.panels, selectedPanelId]
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
      const panel = panelMap.get(beat.target_panel_ids[0]) ?? episode.panels[0];
      return {
        beat,
        panel,
        startMs,
        speechEndMs,
        endMs,
        totalBeatMs,
      };
    });
  }, [draftBeats, panelMap, episode.panels]);

  const totalSimDurationMs = useMemo(
    () =>
      timelineSegments.length > 0
        ? timelineSegments[timelineSegments.length - 1].endMs
        : 0,
    [timelineSegments]
  );

  /** Patch v4.13：原漫链接（后端已做 catalog → episodes_catalog → episode.json 回退）。 */
  const viewerUrl = (episode.viewer_url ?? '').trim();
  /** 只有 http/https 才渲染成可点外链；local:// 之类只显示「本地目录导入」标签。 */
  const isHttpViewerUrl = /^https?:\/\//i.test(viewerUrl);

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
   *
   * Patch v4.25：改成 useCallback，审核表每一行（memo 子组件）才能拿到引用稳定的回调，
   * 编辑一句时其余行不会被连带重绘。
   */
  const updateBeatField = useCallback(
    <K extends keyof ScriptBeat>(beatId: string, key: K, value: ScriptBeat[K]) => {
      const next = draftBeatsRef.current.map((b) =>
        b.beat_id === beatId ? { ...b, [key]: value } : b
      );
      draftBeatsRef.current = next;
      setDraftBeats(next);
      // Patch v4.8：改了句间停顿就必须重建时间轴，否则从 S7 重跑会吃到旧停顿。
      // Patch v4.38：改了音效提示同理——S6 排音效事件、S7 取素材，
      // 不重跑 S6~S7 就还是旧的音效。
      if (key === 'pause_after_ms' || key === 'sfx_cue') {
        setNeedsTimelineRebuild(true);
      }
      markTableDirty();
    },
    [markTableDirty]
  );

  /**
   * Patch v4.13：表格行里的紧凑语速微调（-/+ 步进）。
   *
   * 只改这一句的 speed_override，不动角色默认语速；「恢复默认」写回 null。
   */
  const nudgeBeatSpeed = (beat: ScriptBeat, delta: number) => {
    const base = beat.speed_override ?? roleSpeedForBeat(beat);
    const next = Math.min(2, Math.max(0.5, Math.round((base + delta) * 100) / 100));
    updateBeatField(beat.beat_id, 'speed_override', next);
  };

  /** 插入标签后还焦，并把光标停在插入内容正后方。 */
  const focusAfterTag = useCallback((beatId: string, caret: number) => {
    requestAnimationFrame(() => {
      const el = ttsTextareaRefs.current[beatId];
      if (!el) return;
      el.focus();
      const pos = Math.max(0, Math.min(caret, el.value.length));
      el.setSelectionRange(pos, pos);
    });
  }, []);

  /**
   * Patch v4.14：把选中的语气词 / 停顿标签写进这句的送音文本。
   *
   * 规则（与后端 tts_tags.py 对齐）：
   * - 送音文本为空 → 用字幕原文铺底；
   *   · 语气词插句首（`{tag} {narration_text}`），光标停在标签之后；
   *   · 停顿标签：字幕原文里有「前后都有文字」的句中标点时，插在该标点之后
   *     （如「别怕，<#0.5#>有我在。」）；没有句中标点时先把字幕原文铺底、
   *     光标停到句中，提示用户确认位置后再点一次插入（绝不空手返回）；
   * - 送音文本非空 → 按最近选区替换（无选区则按光标处插入）；
   * - 相邻已有停顿标签 → 替换掉旧的那个，避免连用（后端也会合并，这里先一步收敛）。
   */
  const insertTtsTag = useCallback((beat: ScriptBeat, snippet: string) => {
    const isPauseTag = /^<#/.test(snippet);
    const current = beat.tts_text ?? '';
    const trimmed = current.trim();
    if (trimmed === '') {
      const base = (beat.narration_text ?? '').trim();
      if (!base) {
        setSaveHint('这句还没有字幕原文，无法铺底插入标签。');
        return;
      }
      if (!isPauseTag) {
        const next = `${snippet} ${base}`;
        updateBeatField(beat.beat_id, 'tts_text', next);
        focusAfterTag(beat.beat_id, snippet.length + 1);
        return;
      }
      // 停顿标签：插在句中标点之后（停顿必须夹在两段文字之间）。
      const mark = findSentenceMark(base);
      if (mark) {
        const insertAt = mark.index + mark.text.length;
        const next = `${base.slice(0, insertAt)}${snippet}${base.slice(insertAt)}`;
        updateBeatField(beat.beat_id, 'tts_text', next);
        focusAfterTag(beat.beat_id, insertAt + snippet.length);
        setSaveHint(`已把停顿标签插在「${mark.text}」后面，可继续微调位置。`);
        return;
      }
      // 没有句中标点：先把字幕原文铺底，光标停在句中，等用户确认位置后再点一次插入。
      const mid = Math.floor(base.length / 2);
      updateBeatField(beat.beat_id, 'tts_text', base);
      ttsSelections.current[beat.beat_id] = { start: mid, end: mid };
      focusAfterTag(beat.beat_id, mid);
      setSaveHint(
        '已把字幕原文铺底为送音文本，光标放在句中；确认位置后再点一次「插入停顿」。'
      );
      return;
    }

    // 选区：优先用最近一次记录的选区；没有记录时回退读文本框当前的
    // selectionStart / selectionEnd（失焦后浏览器仍会保留光标位置）。
    const saved = ttsSelections.current[beat.beat_id];
    const el = ttsTextareaRefs.current[beat.beat_id];
    const live = el
      ? { start: el.selectionStart ?? current.length, end: el.selectionEnd ?? el.selectionStart ?? current.length }
      : null;
    const picked = saved ?? live;
    const length = current.length;
    const start = Math.max(0, Math.min(picked ? picked.start : length, length));
    const end = Math.max(start, Math.min(picked ? picked.end : start, length));
    let head = current.slice(0, start);
    let tail = current.slice(end);
    if (isPauseTag) {
      // 相邻停顿标签：把紧邻的旧标签摘掉，避免写成 <#0.5#><#0.8#>。
      // 标签格式与后端 tts_tags.py 的 MINIMAX_PAUSE_TAG_PATTERN 保持一致。
      head = head.replace(/<#\d+(?:\.\d+)?#>\s*$/, '');
      tail = tail.replace(/^\s*<#\d+(?:\.\d+)?#>/, '');
    }
    const next = `${head}${snippet}${tail}`;
    updateBeatField(beat.beat_id, 'tts_text', next);
    focusAfterTag(beat.beat_id, head.length + snippet.length);
  }, [updateBeatField, focusAfterTag]);

  /** 浮层打开时，点面板外部即关闭（Esc 由 TtsTagPicker 内部处理）。 */
  useEffect(() => {
    if (!tagPickerBeatId) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && target.closest('[data-tts-tag-popover]')) return;
      setTagPickerBeatId(null);
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [tagPickerBeatId]);

  // ── 分镜大图弹窗：字幕原文改完，在切换分镜 / 关闭弹窗时落盘 ────────────
  /**
   * 把待保存的草稿写回后端；没有待保存内容就不发请求。
   * 返回是否保存成功（失败时保留弹窗，让用户看到错误并可重试）。
   *
   * Patch v4.23：大图弹窗里的逐字微调**一律原样落盘**，不做角色名归一；
   * 后端回传的角色名告警只做温和提示，不阻断输入。
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
      let warnings: CharacterCheckWarning[] = [];
      if (!onAutoSaveBeats) {
        // 没有独立通道时退化为普通保存（不触发重跑配音）
        const outcome = await onUpdateBeats(episodeId, beats, false);
        if (!outcome || outcome.ok === false) {
          throw new Error('保存失败，请检查后端服务后重试');
        }
        warnings = outcome.warnings ?? [];
      } else {
        const outcome = await onAutoSaveBeats(episodeId, beats);
        if (outcome && outcome.ok === false) {
          throw new Error('保存失败，请检查后端服务后重试');
        }
        warnings = outcome?.warnings ?? [];
      }
      pendingRef.current = null;
      tableDirtyRef.current = false;
      tableDirtyEpisodeRef.current = '';
      setTableDirty(false);
      setSaveStatus('saved');
      setSaveMessage(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
      if (warnings.length > 0) {
        setSaveHint(
          `已保存，但有 ${warnings.length} 处角色名写法与规范名不一致（点「保存解说稿」可查看详情）。`
        );
      } else {
        // Patch v4.25：这次没有角色名告警，就把上一轮遗留的告警条清掉，
        // 免得用户以为还有没处理的写法问题。
        setSaveHint((prev) => (prev.includes('角色名写法') ? '' : prev));
      }
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

  /**
   * Patch v4.25：展开指定的一幕（新增 / 插入解说句后自动展开所在幕）。
   */
  const expandSceneKey = useCallback((key: string) => {
    setExpandedSceneKeys((prev) => (prev.has(key) ? prev : new Set(prev).add(key)));
  }, []);

  /** Patch v4.25：展开 / 收起一幕（幕头部点击）。 */
  const toggleScene = useCallback((key: string) => {
    setExpandedSceneKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  /** Patch v4.25：展开全部幕。 */
  const expandAllScenes = useCallback(() => {
    setExpandedSceneKeys(new Set(beatSceneGroups.map((group) => group.key)));
  }, [beatSceneGroups]);

  /** Patch v4.25：全部折叠。 */
  const collapseAllScenes = useCallback(() => {
    setExpandedSceneKeys(new Set<string>());
  }, []);

  const handleAddBeat = useCallback(() => {
    const prev = draftBeatsRef.current;
    // Patch v4.32：新句默认落在「镜头编排范围内」的第一格分镜。
    // 以前取的是 episode.panels[0]（全部分镜的第一格），那一格往往根本不在
    // 用户编排的幕里 —— 于是新句从一开始就指向一张用户没选过的画面。
    const scopedFirst =
      episode.panels.find((p) => scopePanelIds.has(p.panel_id))?.panel_id ??
      episode.panels[0]?.panel_id ??
      'p_001';
    const firstPanel = scopedFirst;
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
    // Patch v4.25：新句落在哪一幕就自动展开哪一幕，别让用户自己找。
    expandSceneKey(
      sceneKeyOfBeat(
        newBeat,
        storyboardScenes.filter((scene) => scene.enabled !== false)
      )
    );
  }, [
    episode.panels,
    markTableDirty,
    storyboardScenes,
    sceneKeyOfBeat,
    expandSceneKey,
    scopePanelIds,
  ]);

  /**
   * 在指定 beat 之后插入一条新解说句（Patch v3.4）。
   *
   * 画面默认继承**下一句**的分镜（插入的镜头承接后面的画面；没有下一句时继承当前句），
   * 情绪指令 / 运镜 / 停顿 / 说话角色沿用当前句，避免插进去就变味。
   * 新句尚未合成，时长记 0；落盘需要点「保存并从 Stage 5 重跑配音」。
   */
  const handleInsertBeat = useCallback((afterBeatId: string) => {
    const prev = draftBeatsRef.current;
    const index = prev.findIndex((b) => b.beat_id === afterBeatId);
    if (index < 0) return;
    const current = prev[index];
    const next = prev[index + 1];
    // Patch v4.32：继承分镜时只继承「镜头编排范围内」的那些格，
    // 避免把越界（用户没在编排里选过）的画面带进新句。
    const inherited = next
      ? [...next.target_panel_ids]
      : [...current.target_panel_ids];
    const inScope = scopePanelIds.size
      ? inherited.filter((pid) => scopePanelIds.has(pid))
      : inherited;
    const fallbackPanel =
      episode.panels.find((p) => scopePanelIds.has(p.panel_id))?.panel_id ??
      episode.panels[0]?.panel_id ??
      'p_001';
    const newBeat: ScriptBeat = {
      beat_id: uniqueBeatId(prev),
      narration_text: '请在此输入新的短句解说文案。',
      tts_text: '',
      emotion_instruction: current.emotion_instruction,
      target_panel_ids: inScope.length ? inScope : [fallbackPanel],
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
    // Patch v4.25：插入后自动展开新句所在的那一幕。
    expandSceneKey(
      sceneKeyOfBeat(
        newBeat,
        storyboardScenes.filter((scene) => scene.enabled !== false)
      )
    );
  }, [
    markTableDirty,
    storyboardScenes,
    sceneKeyOfBeat,
    expandSceneKey,
    scopePanelIds,
    episode.panels,
  ]);

  const handleDeleteBeat = useCallback((beatId: string) => {
    const next = draftBeatsRef.current.filter((b) => b.beat_id !== beatId);
    draftBeatsRef.current = next;
    setDraftBeats(next);
    markTableDirty();
  }, [markTableDirty]);

  /**
   * Patch v4.23：角色名写法的**只读**预检。
   *
   * 后端不改写任何文本，只回传告警；预检失败（网络异常等）不阻断保存。
   */
  const checkCharacters = useCallback(
    async (beats: ScriptBeat[]): Promise<CharacterCheckWarning[]> => {
      if (!onCheckCharacters) return [];
      // Patch v4.25：预检期间锁住保存入口，避免用户连点造成重复提交。
      setCharacterCheckBusy(true);
      try {
        const res = await onCheckCharacters(episode.episode_id, beats);
        return res?.warnings ?? [];
      } catch {
        // 预检只是提醒，检查不了就直接保存。
        return [];
      } finally {
        setCharacterCheckBusy(false);
      }
    },
    [episode.episode_id, onCheckCharacters]
  );

  /**
   * 真正落盘（不含角色名预检）。
   *
   * autoUnifyCharacters=false：原样保存用户写的角色名（Patch v4.23 默认）；
   * autoUnifyCharacters=true ：按作品规范名归一后再落盘。
   */
  const persistBeats = useCallback(
    async (
      beats: ScriptBeat[],
      triggerFromStage5: boolean,
      autoUnifyCharacters: boolean,
      syncCharacterUpdates?: CharacterSyncUpdate[]
    ): Promise<boolean> => {
      setSavingBeats(true);
      try {
        const outcome = await onUpdateBeats(
          episode.episode_id,
          beats,
          triggerFromStage5,
          autoUnifyCharacters,
          syncCharacterUpdates
        );
        if (!outcome || outcome.ok === false) {
          setSaveHint('保存失败：后端拒绝了这次保存，草稿仍保留在本地，请检查后重试。');
          return false;
        }
        tableDirtyRef.current = false;
        tableDirtyEpisodeRef.current = '';
        setTableDirty(false);
        pendingRef.current = null;
        // Patch v4.32：后端把编排范围外的分镜引用收口了 —— 如实告诉用户改了哪几处。
        const scopeViolations = outcome.scope_violations ?? [];
        if (scopeViolations.length > 0) {
          const detail = scopeViolations
            .map(
              (item) =>
                `${item.beat_id}：去掉 ${item.removed.join('、')}` +
                (item.replacement ? ` → 改用 ${item.replacement}` : '')
            )
            .join('；');
          setSaveHint(
            `已保存，但有 ${scopeViolations.length} 处分镜引用不在镜头编排范围内，已自动改回编排内（${detail}）。`
          );
        } else if ((outcome.warnings ?? []).length > 0) {
          setSaveHint(
            `已保存，但有 ${outcome.warnings.length} 处角色名写法与规范名不一致（点「保存解说稿」可查看详情）。`
          );
        } else {
          // Patch v4.25：这次没有角色名告警，就把上一轮遗留的告警条清掉。
          setSaveHint((prev) =>
            prev.includes('角色名写法') ? '' : prev
          );
        }
        return true;
      } catch (err) {
        setSaveHint(
          `保存失败：${err instanceof Error ? err.message : String(err)}`
        );
        return false;
      } finally {
        setSavingBeats(false);
      }
    },
    [episode.episode_id, onUpdateBeats]
  );

  /**
   * 保存解说稿。
   *
   * triggerFromStage5=false：只落盘剧本（不重跑配音）；
   * triggerFromStage5=true ：落盘后顺带从 Stage 5 重跑配音与出片。
   * 保存失败时保留草稿与「未保存改动」标记，别让用户以为已经存上了。
   *
   * Patch v4.23：落盘前先做一次角色名写法的**只读**检查；发现不一致就弹
   * 应用内确认框，由用户决定「原样保存」还是「统一成规范名」。
   */
  const handleSaveBeats = useCallback(async (triggerFromStage5: boolean) => {
    const beats = draftBeatsRef.current;
    const invalid = beats.find((b) => (b.narration_text ?? '').trim().length < 2);
    if (invalid) {
      setSaveHint(
        `「${invalid.beat_id}」的字幕原文为空或不足 2 字，后端会拒收整批，请先补齐再保存。`
      );
      return;
    }
    setSaveHint('');
    const warnings = await checkCharacters(beats);
    if (warnings.length > 0) {
      // Patch v4.25：每次弹窗都从「不改档案」开始。
      setCharSyncChoices({});
      setCharPrompt({
        warnings,
        beats,
        triggerFromStage5,
        busy: false,
      });
      return;
    }
    await persistBeats(beats, triggerFromStage5, false);
  }, [checkCharacters, persistBeats]);

  /**
   * Patch v4.25：把弹窗里勾选的档案同步选项翻译成后端契约。
   *
   * 没有勾选的分组表示「仅本次放行，不改档案」，不会出现在请求体里。
   */
  const collectSyncUpdates = useCallback(
    (warnings: CharacterCheckWarning[]): CharacterSyncUpdate[] => {
      const updates: CharacterSyncUpdate[] = [];
      buildCharacterSyncGroups(warnings).forEach((group) => {
        const action = charSyncChoices[group.key];
        if (!action || !group.actions.includes(action)) return;
        updates.push({
          action,
          found_name: group.found_name,
          target_canonical: group.suggested_canonical,
        });
      });
      return updates;
    },
    [charSyncChoices]
  );

  /** Patch v4.23 + v4.25：确认框「仍按我的修改保存（放行）」——原样落盘，不归一。 */
  const handleCharPromptSaveAsIs = async () => {
    const prompt = charPrompt;
    if (!prompt || prompt.busy) return;
    setCharPrompt({ ...prompt, busy: true });
    const syncUpdates = collectSyncUpdates(prompt.warnings);
    const ok = await persistBeats(
      prompt.beats,
      prompt.triggerFromStage5,
      false,
      syncUpdates
    );
    setCharPrompt(null);
    setCharSyncChoices({});
    if (ok) {
      // Patch v4.25：真的改了作品角色档案，重新拉一次 TTS 配置，
      // 让「本句配音」下拉立刻反映新的规范名/别名。
      if (syncUpdates.length > 0) {
        await reloadSeriesTts();
      }
      prompt.onDone?.();
    }
  };

  /** Patch v4.23：确认框「按建议统一为规范名并保存」——后端归一后落盘。 */
  const handleCharPromptUnify = async () => {
    const prompt = charPrompt;
    if (!prompt || prompt.busy) return;
    setCharPrompt({ ...prompt, busy: true });
    const ok = await persistBeats(prompt.beats, prompt.triggerFromStage5, true);
    setCharPrompt(null);
    setCharSyncChoices({});
    if (ok) prompt.onDone?.();
  };

  /** Patch v4.23：确认框「返回继续修改」——不保存，保留草稿。 */
  const handleCharPromptCancel = () => {
    if (!charPrompt || charPrompt.busy) return;
    setCharPrompt(null);
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
   * Patch v4.8：真正发起「从此步重跑」（③④ 两步）。
   *
   * 拆出来是为了让 Patch v4.23 的角色名确认弹窗可以在用户确认后接着跑。
   */
  const runStageRerun = useCallback(
    async (stageId: StageId, voiceChanged: boolean) => {
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
    [episode.episode_id, needsTimelineRebuild, onRerunFromStage]
  );

  /**
   * Patch v4.8：统一接管步骤条上的「从此步重跑」。
   *
   * 三道保护：
   * 1. 有未保存草稿时，先校验并自动落盘（同步 audio_manifest），失败即中止；
   * 2. 草稿改过送音内容却想从 S6/S7 跑 → 内联横幅让用户选「从 S5 重跑」还是「仍从当前阶段跑」；
   * 3. 停顿被改过却想跳过 S6 → 自动把起点升级成 S6（0 TTS 成本），并说明原因。
   *
   * Patch v4.23：落盘前先做角色名写法的**只读**检查，有告警就弹确认框。
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
        // Patch v4.23：先只读检查角色名写法，有告警就停下来让用户确认。
        const warnings = await checkCharacters(beats);
        if (warnings.length > 0) {
          // Patch v4.25：每次弹窗都从「不改档案」开始。
          setCharSyncChoices({});
          setCharPrompt({
            warnings,
            beats,
            triggerFromStage5: false,
            busy: false,
            onDone: () => {
              void runStageRerun(stageId, voiceChanged);
            },
          });
          return;
        }
        const ok = await persistBeats(beats, false, false);
        if (!ok) {
          setSaveHint('保存失败：后端拒绝了这次保存，已中止重跑，草稿仍保留在本地。');
          return;
        }
      }

      await runStageRerun(stageId, voiceChanged);
    },
    [
      checkCharacters,
      hasVoiceRelevantDraftChanges,
      persistBeats,
      rerunningStage,
      runStageRerun,
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

  /**
   * Patch v4.25：审核台快捷键。
   *
   * - Cmd/Ctrl+S      → 保存解说稿
   * - Cmd/Ctrl+Shift+S→ 保存并从 Stage 5 重跑配音
   * - Esc             → 关掉角色名写法确认弹窗
   *
   * 焦点在输入框里也要拦（否则 Cmd+S 会变成浏览器「保存网页」）；
   * 有应用内弹窗遮挡、或正在保存 / 预检时不抢快捷键。
   * 大图弹窗（PanelZoomModal）自己处理 Cmd/Ctrl+S，这里要让开，避免保存两次。
   */
  useEffect(() => {
    if (activeTab !== 'script') return;
    const onKeyDown = (event: KeyboardEvent) => {
      const key = event.key.toLowerCase();
      if ((event.metaKey || event.ctrlKey) && !event.altKey && key === 's') {
        if (zoomBeatId) return;
        event.preventDefault();
        if (charPrompt || regenerateTarget || resetTarget || cleanupConfirm) return;
        if (savingBeats || characterCheckBusy) return;
        void handleSaveBeats(event.shiftKey);
        return;
      }
      if (key === 'escape' && charPrompt && !charPrompt.busy) {
        event.preventDefault();
        setCharPrompt(null);
        setCharSyncChoices({});
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [
    activeTab,
    charPrompt,
    regenerateTarget,
    resetTarget,
    cleanupConfirm,
    savingBeats,
    characterCheckBusy,
    zoomBeatId,
    handleSaveBeats,
  ]);

  /** Patch v4.12：本话是否已有真实配音（S5 完成即可按 beat_id 回源 WAV）。 */
  const isTtsReady =
    episode.stages.find((stage) => stage.id === 's5_tts')?.status === 'done';

  /** 停止当前真实音频播放并清空 audio 元素。 */
  const stopPreviewAudio = useCallback(() => {
    const el = audioElRef.current;
    if (el) {
      el.pause();
      el.src = '';
      audioElRef.current = null;
    }
    setPlayingBeatId(null);
  }, []);

  // 组件卸载 / 切换话次时确保音频停止，避免离开页面后仍在播放。
  useEffect(() => {
    return () => {
      const el = audioElRef.current;
      if (el) {
        el.pause();
        el.src = '';
        audioElRef.current = null;
      }
    };
  }, [episode.episode_id]);

  /**
   * Patch v4.35：幕 BGM 试听（迷你按钮）。
   *
   * 原来每个幕挂一个原生 `<audio controls>`（宽 160px），12 个幕就把幕头部
   * 撑爆了；换成 [▶/■] 小按钮，并且**全局只保留一个 Audio 实例**，
   * 点谁停谁，不会出现多幕串音。
   */
  const bgmAudioRef = useRef<HTMLAudioElement | null>(null);
  const [bgmPlayingSceneId, setBgmPlayingSceneId] = useState<string | null>(null);

  const stopBgmPreview = useCallback(() => {
    const el = bgmAudioRef.current;
    if (el) {
      el.pause();
      el.src = '';
      bgmAudioRef.current = null;
    }
    setBgmPlayingSceneId(null);
  }, []);

  const toggleBgmPreview = useCallback(
    (sceneId: string, url: string) => {
      if (bgmPlayingSceneId === sceneId) {
        stopBgmPreview();
        return;
      }
      stopBgmPreview();
      const audio = new Audio(url);
      bgmAudioRef.current = audio;
      setBgmPlayingSceneId(sceneId);
      audio.onended = () => {
        if (bgmAudioRef.current === audio) {
          bgmAudioRef.current = null;
          setBgmPlayingSceneId(null);
        }
      };
      void audio.play().catch(() => {
        if (bgmAudioRef.current === audio) bgmAudioRef.current = null;
        setBgmPlayingSceneId(null);
      });
    },
    [bgmPlayingSceneId, stopBgmPreview]
  );

  // 离开话次时停掉 BGM 试听。
  useEffect(() => {
    return () => {
      const el = bgmAudioRef.current;
      if (el) {
        el.pause();
        el.src = '';
        bgmAudioRef.current = null;
      }
    };
  }, [episode.episode_id]);

  const handlePreviewAudio = useCallback((beatId: string) => {
    // Patch v4.12：只要这一句在清单里有真实时长，就优先播放后端 WAV；
    // 否则退回提示音（S5 未跑或该句尚未合成）。
    const beatAudioMs =
      draftBeatsRef.current.find((b) => b.beat_id === beatId)?.audio_duration_ms ??
      episode.beats.find((b) => b.beat_id === beatId)?.audio_duration_ms ??
      0;
    const hasRealAudio = isTtsReady || beatAudioMs > 0;
    // 已有真实配音：播放后端 WAV。再次点击同一句=停止；点别的句=切换音源。
    if (hasRealAudio) {
      const current = audioElRef.current;
      if (current && playingBeatId === beatId) {
        stopPreviewAudio();
        return;
      }
      if (current) {
        current.pause();
        current.src = '';
      }
      const audio = new Audio(
        `/api/episodes/${encodeURIComponent(episode.episode_id)}/audio/${encodeURIComponent(
          beatId
        )}?t=${audioRevision}`
      );
      audioElRef.current = audio;
      setPlayingBeatId(beatId);
      audio.onended = () => {
        if (audioElRef.current === audio) {
          audioElRef.current = null;
          setPlayingBeatId((cur) => (cur === beatId ? null : cur));
        }
      };
      audio.onerror = () => {
        if (audioElRef.current === audio) {
          audioElRef.current = null;
          setPlayingBeatId((cur) => (cur === beatId ? null : cur));
          // 该句的 WAV 取不到（清单有记录但文件缺失）：退回提示音，避免完全无声。
          setSaveHint(`「${beatId}」的配音文件取不到，已退回提示音；请确认 Stage 5 已完成`);
          playPreviewTone();
        }
      };
      void audio.play().catch(() => {
        // 浏览器拦截自动播放时退回本地提示音，保证按钮仍有反馈。
        if (audioElRef.current === audio) {
          audioElRef.current = null;
        }
        playPreviewTone();
      });
      return;
    }
    // 尚未生成配音：保留原有提示音降级路径。
    if (playingBeatId === beatId) {
      setPlayingBeatId(null);
      return;
    }
    playPreviewTone();
    setPlayingBeatId(beatId);
    setTimeout(() => {
      setPlayingBeatId((cur) => (cur === beatId ? null : cur));
    }, 1200);
  }, [episode.episode_id, episode.beats, isTtsReady, playingBeatId, audioRevision, stopPreviewAudio]);

  // ── Patch v4.25：审核表行的稳定回调 ──────────────────────────────────────
  /**
   * 审核表每一行都是 memo 子组件，回调引用一变整表都会重绘；
   * 这里统一用 useCallback 固定住，并从 draftBeatsRef 里取当前这一句的最新值。
   */
  const handleRowChangeText = useCallback(
    (beatId: string, key: 'narration_text' | 'tts_text', value: string) => {
      updateBeatField(beatId, key, value);
    },
    [updateBeatField]
  );

  const handleRowChangeRole = useCallback(
    (beatId: string, nextRole: string) => {
      const beat = draftBeatsRef.current.find((b) => b.beat_id === beatId);
      if (!beat) return;
      const patch = speakerRolePatchOf(beat, nextRole, seriesTts);
      (Object.keys(patch) as Array<keyof ScriptBeat>).forEach((key) => {
        updateBeatField(beatId, key, patch[key] as ScriptBeat[typeof key]);
      });
    },
    [updateBeatField, seriesTts]
  );

  /**
   * Patch v4.27：逐句指定 MiniMax 音色（voice_override）。
   *
   * 只改这一句的音色字段，不动角色，也不清单句语速微调；传空串＝清除指定，
   * 恢复按角色解析（与后端 ``voice_override`` 为空串的语义一致）。
   */
  const handleRowChangeVoiceOverride = useCallback(
    (beatId: string, voiceId: string) => {
      updateBeatField(beatId, 'voice_override', voiceId);
    },
    [updateBeatField]
  );

  const handleRowNudgeSpeed = useCallback(
    (beatId: string, delta: number) => {
      const beat = draftBeatsRef.current.find((b) => b.beat_id === beatId);
      if (!beat) return;
      const base = beat.speed_override ?? roleSpeedForBeatOf(beat, seriesTts);
      const next = Math.min(2, Math.max(0.5, Math.round((base + delta) * 100) / 100));
      updateBeatField(beatId, 'speed_override', next);
    },
    [updateBeatField, seriesTts]
  );

  const handleRowResetSpeed = useCallback(
    (beatId: string) => updateBeatField(beatId, 'speed_override', null),
    [updateBeatField]
  );

  const handleRowChangeMotion = useCallback(
    (beatId: string, motion: CameraMotion) =>
      updateBeatField(beatId, 'motion', motion),
    [updateBeatField]
  );

  const handleRowChangePause = useCallback(
    (beatId: string, ms: number) => updateBeatField(beatId, 'pause_after_ms', ms),
    [updateBeatField]
  );

  /**
   * Patch v4.32：改这一句的**主画面**分镜（第 1 格）。
   *
   * 以前这里是 `updateBeatField(beatId, 'target_panel_ids', [panelId])` ——
   * 一改就把整句的多目标压成一格，Stage 4 分配的「一句多格」信息当场丢失，
   * 成片随之与审核台对不上。现在只替换第 1 格，其余目标格原样保留。
   */
  const handleRowChangePanel = useCallback(
    (beatId: string, panelId: string) => {
      const beat = draftBeatsRef.current.find((b) => b.beat_id === beatId);
      if (!beat) return;
      const next = [...(beat.target_panel_ids ?? [])];
      if (next.length === 0) {
        updateBeatField(beatId, 'target_panel_ids', [panelId]);
        return;
      }
      next[0] = panelId;
      updateBeatField(beatId, 'target_panel_ids', next);
    },
    [updateBeatField]
  );

  /**
   * Patch v4.32：改第 index 个目标分镜（保留其它目标格）。
   *
   * 以前改分镜会把多目标压成一格 —— Stage 4 分配的「一句多格」信息一改就丢，
   * 成片随之与审核台对不上。现在只替换被点的那一格。
   */
  const handleRowChangePanelAt = useCallback(
    (beatId: string, index: number, panelId: string) => {
      const beat = draftBeatsRef.current.find((b) => b.beat_id === beatId);
      if (!beat) return;
      const next = [...(beat.target_panel_ids ?? [])];
      if (index < 0 || index >= next.length) return;
      next[index] = panelId;
      updateBeatField(beatId, 'target_panel_ids', next);
    },
    [updateBeatField]
  );

  /** Patch v4.32：删掉第 index 个目标分镜（至少保留一格，否则后端校验会拒）。 */
  const handleRowRemovePanelAt = useCallback(
    (beatId: string, index: number) => {
      const beat = draftBeatsRef.current.find((b) => b.beat_id === beatId);
      if (!beat) return;
      const current = beat.target_panel_ids ?? [];
      if (current.length <= 1 || index < 0 || index >= current.length) return;
      const next = current.filter((_, i) => i !== index);
      updateBeatField(beatId, 'target_panel_ids', next);
    },
    [updateBeatField]
  );

  /**
   * Patch v4.35：给这一句再加一格轮播画面。
   *
   * 候选范围与下拉一致（编排范围内优先）；已经用过的格不再重复。
   */
  const handleRowAddPanelAt = useCallback(
    (beatId: string) => {
      const beat = draftBeatsRef.current.find((b) => b.beat_id === beatId);
      if (!beat) return;
      const current = beat.target_panel_ids ?? [];
      const candidates =
        scopePanelIds.size > 0
          ? panelOptions.filter((option) => scopePanelIds.has(option.value))
          : panelOptions;
      const next = candidates.find((option) => !current.includes(option.value));
      if (!next) return;
      updateBeatField(beatId, 'target_panel_ids', [...current, next.value]);
    },
    [panelOptions, scopePanelIds, updateBeatField]
  );

  /**
   * Patch v4.33：一键把「一句多格画面」收敛为「只播主画面」。
   *
   * 背景：Stage 4 会给一句解说配多格分镜（Stage 6 按格数**均分时长**轮播它们）。
   * 用户在审核台并没有逐格确认过这些画面 —— 成片里就会播到「用户没参与决策」
   * 的画面（真实案例：wt_3924 EP.001 的 b4 配了 p_014 + p_017，成片 26 秒
   * 播的是第 2 格 p_017，而审核台那一行只让人看到第 1 格）。
   *
   * 这个按钮把全话所有多目标句统一收敛成只保留第 1 格（主画面）：
   * - 满足铁律「成片播的画面 = 审核台这一行选中的画面」；
   * - 想要轮播的用户，仍然可以在行内手动加回第 2 格（Patch v4.32 的逐格编辑）。
   */
  const handleCollapseToMainPanel = useCallback(() => {
    const beats = draftBeatsRef.current;
    let changed = 0;
    const next = beats.map((beat) => {
      const ids = beat.target_panel_ids ?? [];
      if (ids.length <= 1) return beat;
      changed += 1;
      return { ...beat, target_panel_ids: [ids[0]] };
    });
    if (changed === 0) {
      setMultiPanelHint('当前没有「一句多格画面」的句子，无需处理。');
      return;
    }
    draftBeatsRef.current = next;
    setDraftBeats(next);
    markTableDirty();
    setMultiPanelHint(
      `已把 ${changed} 句的「一句多格画面」收敛为只播主画面（第 1 格）。` +
        '保存解说稿后重跑 S6~S7 即可生效（无需重跑配音）。'
    );
  }, [markTableDirty]);

  /**
   * Patch v4.38：改这一句的音效提示。
   *
   * 置空时传 `null`（后端 `NULLABLE_BEAT_FIELDS` 含 sfx_cue，传 null 才是
   * 真的清空，传空串会被当成「不改」继承旧值）。
   */
  const handleRowChangeSfxCue = useCallback(
    (beatId: string, sfxKey: string | null) => {
      updateBeatField(beatId, 'sfx_cue', sfxKey);
    },
    [updateBeatField]
  );

  const handleRowChangeScene = useCallback(
    (beatId: string, sceneId: string | null) =>
      updateBeatField(beatId, 'scene_id', sceneId),
    [updateBeatField]
  );

  /**
   * Patch v4.35：命令栏「+ 多音字规则」弹层的提交动作。
   *
   * 只新增送音替换规则（source → replacement），绝不碰字幕原文；
   * 规则保存后由后端整体覆盖同一 scope，父组件收到后刷新 rules 列表。
   */
  const commitRule = useCallback(() => {
    const source = ruleDraft.source.trim();
    const replacement = ruleDraft.replacement.trim();
    if (!source || !replacement || !onAddRule) return;
    onAddRule({ source, replacement, note: '审核台快捷添加' });
    setRuleDraft({ source: '', replacement: '' });
    setRulePopoverOpen(false);
  }, [onAddRule, ruleDraft.replacement, ruleDraft.source]);

  const handleRowToggleTagPicker = useCallback((beatId: string) => {
    setTagPickerBeatId((prev) => (prev === beatId ? null : beatId));
  }, []);

  const handleRowPickTag = useCallback(
    (beatId: string, snippet: string) => {
      const beat = draftBeatsRef.current.find((b) => b.beat_id === beatId);
      if (beat) insertTtsTag(beat, snippet);
    },
    [insertTtsTag]
  );

  const handleRowRegenerate = useCallback(
    (beatId: string) => {
      stopPreviewAudio();
      setRegenerateTarget(beatId);
    },
    [stopPreviewAudio]
  );

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
    // Patch v4.28：横向扫镜（左右横扫）预览。
    if (motion === 'pan_right') {
      const x = -16 + 32 * progress;
      return { transform: `scale(1.12) translateX(${x.toFixed(1)}px)` };
    }
    if (motion === 'pan_left') {
      const x = 16 - 32 * progress;
      return { transform: `scale(1.12) translateX(${x.toFixed(1)}px)` };
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

  /** Patch v4.11：确认清理过程文件（只留成片）。 */
  const handleConfirmCleanup = useCallback(async () => {
    if (!onCleanupIntermediates) return;
    setCleanupBusy(true);
    try {
      const ok = await onCleanupIntermediates(episode.episode_id);
      if (ok) setCleanupConfirm(false);
    } finally {
      setCleanupBusy(false);
    }
  }, [episode.episode_id, onCleanupIntermediates]);

  /** Patch v4.12：「重配此句」按钮的可用条件——S5 已完成、未归档、非运行中且回调就绪。 */
  const regenerateEnabled =
    isTtsReady &&
    !episode.archived &&
    episode.status !== 'running' &&
    episode.status !== 'queued' &&
    !!onRegenerateBeatTts;

  /** Patch v4.13：重配弹窗里展示的「这一句」信息。 */
  const regeneratePreview = useMemo(() => {
    if (!regenerateTarget) return null;
    const beat =
      draftBeatsRef.current.find((b) => b.beat_id === regenerateTarget) ??
      draftBeats.find((b) => b.beat_id === regenerateTarget);
    if (!beat) return null;
    const voice = beatVoiceInfo(beat);
    return {
      narrationText: beat.narration_text ?? '',
      ttsText: beat.tts_text ?? '',
      role: (beat.speaker_role ?? '').trim() || voice.label,
      voice: voice.voice,
      speed: voice.speed,
      speedOverridden: voice.speedOverridden,
      pauseAfterMs: beat.pause_after_ms ?? 0,
    };
  }, [regenerateTarget, draftBeats]);

  /** Patch v4.13：重配弹窗按 Esc 关闭（合成中锁定，防止半途改目标）。 */
  useEffect(() => {
    if (!regenerateTarget) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      if (regenerateBusy) return;
      setRegenerateTarget(null);
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [regenerateTarget, regenerateBusy]);

  /** Patch v4.12：确认单句重配；rerunFromS6=true 时后端顺带提交从 S6 出片的任务。 */
  const handleConfirmRegenerate = useCallback(
    async (rerunFromS6: boolean) => {
      if (!regenerateTarget || !onRegenerateBeatTts) return;
      const draft = draftBeatsRef.current.find((b) => b.beat_id === regenerateTarget);
      if (!draft) {
        setSaveHint(`找不到「${regenerateTarget}」的草稿，无法重配`);
        return;
      }
      if ((draft.narration_text ?? '').trim().length < 2) {
        setSaveHint(`「${regenerateTarget}」的字幕原文为空或不足 2 字，请先补齐再重配`);
        return;
      }
      setSaveHint('');
      setRegenerateBusy(true);
      try {
        const result = await onRegenerateBeatTts(
          episode.episode_id,
          regenerateTarget,
          draft,
          rerunFromS6
        );
        if (!result) return; // 错误提示已在 App 层 reportError 弹出
        // Patch v4.12：只把这一句换成后端最新状态（含新的 audio_duration_ms），
        // 其余句子保留用户本地草稿，避免冲掉别的句子上尚未保存的改动。
        const serverBeat = result.beats.find((b) => b.beat_id === regenerateTarget);
        const mergedDraft = draftBeatsRef.current.map((b) =>
          b.beat_id === regenerateTarget && serverBeat ? { ...b, ...serverBeat } : b
        );
        draftBeatsRef.current = mergedDraft;
        setDraftBeats(mergedDraft);
        // 后端返回的 beats 即最新已落盘基准：若除本句外仍有差异，说明别的句子还有
        // 未保存改动，必须保持 dirty，否则后台回填会把那些改动覆盖掉。
        const otherPending = hasDraftChanges(mergedDraft, result.beats, regenerateTarget);
        if (otherPending) {
          tableDirtyRef.current = true;
          tableDirtyEpisodeRef.current = episode.episode_id;
          setTableDirty(true);
        } else {
          tableDirtyRef.current = false;
          tableDirtyEpisodeRef.current = '';
          setTableDirty(false);
          pendingRef.current = null;
        }
        setAudioRevision((v) => v + 1);
        setRegenerateTarget(null);
        stopPreviewAudio();
        if (!rerunFromS6) {
          // 音频时长已变，旧时间轴失效：标记需要从 S6 重建（点 S7 时会自动升级到 S6）。
          setNeedsTimelineRebuild(true);
          setRerunNotice(
            `「${regenerateTarget}」音频已更新，可直接从 Stage 6 重跑出新成片`
          );
        }
      } finally {
        setRegenerateBusy(false);
      }
    },
    [
      episode.episode_id,
      onRegenerateBeatTts,
      regenerateTarget,
      stopPreviewAudio,
    ]
  );

  /**
   * Patch v4.35：审核台结果条候选。
   *
   * 以前是 6~8 条并列 Banner 同时占屏，把表格挤到屏幕外；现在收敛成
   * 「错误 → 提示 → 成功」排序、最多同时显示 2 条，且每条都能手动关掉
   * （关掉的记在 dismissedNotices 里，不会再冒出来）。
   */
  const scriptNotices: Array<{
    key: string;
    tone: 'error' | 'success' | 'warn';
    node: React.ReactNode;
  }> = [];
  if (autoPacingError) {
    scriptNotices.push({
      key: 'autoPacingError',
      tone: 'error',
      node: <span>{autoPacingError}</span>,
    });
  }
  if (autoMotionError) {
    scriptNotices.push({
      key: 'autoMotionError',
      tone: 'error',
      node: <span>{autoMotionError}</span>,
    });
  }
  if (bgmError) {
    scriptNotices.push({ key: 'bgmError', tone: 'error', node: <span>{bgmError}</span> });
  }
  if (saveHint) {
    scriptNotices.push({ key: 'saveHint', tone: 'warn', node: <span>{saveHint}</span> });
  }
  if (multiPanelHint) {
    scriptNotices.push({
      key: 'multiPanelHint',
      tone: 'warn',
      node: <span>{multiPanelHint}</span>,
    });
  }
  if (autoPacingSummary) {
    scriptNotices.push({
      key: 'autoPacingSummary',
      tone: 'success',
      node: autoPacingSummary.updated_count > 0 ? (
        <>
          <p>
            自动节奏留白完成：共调整 {autoPacingSummary.updated_count} 处停顿（跨幕{' '}
            {autoPacingSummary.scene_end_count} / 转折 {autoPacingSummary.turn_count} / 换气{' '}
            {autoPacingSummary.breath_count} / 切分镜{' '}
            {autoPacingSummary.panel_switch_count ?? 0}），已同步{' '}
            {autoPacingSummary.audio_manifest_synced} 条音频清单。可直接从 S6 重跑，无需重跑 S5
            配音。
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
      ),
    });
  }
  if (autoMotionSummary) {
    scriptNotices.push({
      key: 'autoMotionSummary',
      tone: 'success',
      node: autoMotionSummary.updated_count > 0 ? (
        <>
          <p>
            自动分配动效完成：共调整 {autoMotionSummary.updated_count} 处（横扫{' '}
            {autoMotionSummary.wide_pan_count} / 推拉 {autoMotionSummary.push_pull_count} / 静止{' '}
            {autoMotionSummary.static_count} / 震动 {autoMotionSummary.shake_count}），重跑
            S6~S7 即可生效，无需重跑配音。
          </p>
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
        <p>自动分配动效检查完成：现有运镜已符合分配规则，未做调整。</p>
      ),
    });
  }
  if (bgmHint) {
    scriptNotices.push({ key: 'bgmHint', tone: 'success', node: <span>{bgmHint}</span> });
  }
  return (
    <div className="space-y-6">
      {/* 8-Step Interactive Pipeline Stepper */}
      <section className="bg-white border border-slate-200 rounded-xl p-5">
        <div
          className={`flex flex-col sm:flex-row sm:items-center justify-between gap-3 ${
            pipelineCollapsed && !pipelineForceOpen ? '' : 'mb-4'
          }`}
        >
          <div>
            <h2 className="text-sm font-semibold text-slate-900">
              流水线执行状态与断点重跑（manifest.json）
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              点击「从此步重跑」或「向后清理」都会先清空该步及全部下游旧产物与完成标记，保证状态与真实成片一致。
            </p>
          </div>
          {/* Patch v4.35：窄视口（1280）下允许换行，且按钮文字不折行。 */}
          <div className="flex flex-wrap items-center gap-3 text-xs font-mono-tabular text-slate-500">
            <span className="whitespace-nowrap">
              {episode.episode_id} · 总时长{' '}
              {totalSimDurationMs > 0 ? formatMs(totalSimDurationMs) : '未合成'}
            </span>
            {/* Patch v4.13：原漫链接。只渲染 http/https 外链；local:// 目录导入只给标签。 */}
            {viewerUrl && isHttpViewerUrl ? (
              <a
                href={viewerUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-md border border-slate-300 bg-white px-2 py-1 text-[11px] font-medium text-slate-700 hover:border-slate-400 hover:text-slate-900"
              >
                <Eye size={12} />
                查看原漫
              </a>
            ) : viewerUrl ? (
              <span className="inline-flex items-center gap-1 rounded-md border border-slate-200 bg-slate-50 px-2 py-1 text-[11px] text-slate-500">
                <FolderOutput size={12} />
                本地目录导入
              </span>
            ) : null}
            {/* Patch v4.35：折叠开关——把首屏垂直空间还给分镜与审核表。 */}
            <button
              type="button"
              onClick={togglePipelineCollapsed}
              aria-label={pipelineCollapsed ? '展开流水线' : '收起流水线'}
              title={
                pipelineCollapsed
                  ? '展开流水线（显示各阶段状态与重跑入口）'
                  : '收起流水线，只留一行摘要'
              }
              className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-md border border-slate-300 bg-white px-2 py-1 text-[11px] font-medium text-slate-700 hover:border-slate-400 hover:text-slate-900"
            >
              {pipelineCollapsed ? (
                <ChevronDown className="w-3 h-3" />
              ) : (
                <ChevronRight className="w-3 h-3" />
              )}
              {pipelineCollapsed ? '展开' : '收起'}
            </button>
          </div>
        </div>

        {pipelineCollapsed && !pipelineForceOpen ? (
          /* Patch v4.35：折叠态的一行摘要（EP / 阶段总览 / 总时长 / 封页状态）。 */
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg border border-slate-200 bg-slate-50/70 px-3 py-2 text-xs text-slate-600">
            <span className="font-mono-tabular font-semibold text-slate-800">
              {episode.episode_id}
            </span>
            <span className="text-slate-300">·</span>
            <span>
              {pipelineSummary.done}/{pipelineSummary.total} 阶段完成
            </span>
            {pipelineSummary.running > 0 && (
              <>
                <span className="text-slate-300">·</span>
                <span className="text-blue-600">{pipelineSummary.running} 个执行中</span>
              </>
            )}
            {pipelineSummary.failed > 0 && (
              <>
                <span className="text-slate-300">·</span>
                <span className="text-red-600">{pipelineSummary.failed} 个失败</span>
              </>
            )}
            <span className="text-slate-300">·</span>
            <span className="font-mono-tabular">
              总时长 {totalSimDurationMs > 0 ? formatMs(totalSimDurationMs) : '未合成'}
            </span>
            <span className="text-slate-300">·</span>
            <span>封页 {brandingOptions.cover_enabled ? '开' : '关'}</span>
            <button
              type="button"
              onClick={togglePipelineCollapsed}
              className="ml-auto inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2 py-1 text-[11px] font-medium text-slate-700 hover:border-slate-400 hover:text-slate-900"
            >
              <ChevronDown className="w-3 h-3" />
              展开流水线
            </button>
          </div>
        ) : (
          <>
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

        {/* Patch v4.11：磁盘占用与一键清理过程文件（只留成片） */}
        <div className="mt-4 flex flex-col sm:flex-row sm:items-center justify-between gap-3 rounded-lg border border-slate-200 bg-slate-50/60 px-3.5 py-3">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-600">
            <HardDrive className="w-4 h-4 text-slate-400 shrink-0" />
            <span>
              磁盘占用{' '}
              <span className="font-mono-tabular font-semibold text-slate-800">
                {formatBytes(episode.disk_bytes)}
              </span>
            </span>
            {episode.archived ? (
              <span className="inline-flex items-center gap-1 rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700">
                <Archive className="w-3 h-3" />
                已归档（仅留成片）
              </span>
            ) : (
              (episode.reclaimable_bytes ?? 0) > 0 && (
                <span>
                  · 清理可释放约{' '}
                  <span className="font-mono-tabular font-semibold text-amber-700">
                    {formatBytes(episode.reclaimable_bytes)}
                  </span>
                </span>
              )
            )}
          </div>
          {!episode.archived && (
            <button
              type="button"
              disabled={
                !isVideoReady ||
                episode.status === 'running' ||
                episode.status === 'queued' ||
                cleanupBusy ||
                !onCleanupIntermediates
              }
              onClick={() => setCleanupConfirm(true)}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-slate-700 bg-white border border-slate-200 rounded hover:border-amber-400 hover:text-amber-700 transition-colors whitespace-nowrap shrink-0 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <Archive className="w-3.5 h-3.5" />
              清理过程文件（只留成片）
            </button>
          )}
        </div>

        {/* Patch v4.11：清理过程文件的内联二次确认（不用 window.confirm） */}
        {cleanupConfirm && (
          <div className="mt-3 p-3.5 rounded-lg border border-amber-200 bg-amber-50/70 text-xs text-amber-900">
            <div className="flex items-start gap-2">
              <AlertCircle className="w-4 h-4 shrink-0 mt-0.5 text-amber-600" />
              <div className="flex-1 space-y-2">
                <p className="font-medium">
                  确认清理本话过程文件、只保留最终成片？
                  {(episode.reclaimable_bytes ?? 0) > 0 && (
                    <>
                      预计释放约{' '}
                      <span className="font-mono-tabular">
                        {formatBytes(episode.reclaimable_bytes)}
                      </span>
                      。
                    </>
                  )}
                </p>
                <ul className="list-disc pl-4 space-y-0.5 text-amber-800/90">
                  <li>删除原始截图、切图、识图、分镜、剧本、配音与时间轴等过程目录，9:16 成片完整保留，试看与导出不受影响。</li>
                  <li>作品级剧情记忆（story_bible.json）保留，不影响后续集数的记忆连贯。</li>
                  <li>07_out 与 exports 两份成片会合并为硬链接，同盘只占一份体积。</li>
                  <li>日后需要修改本集时，得从 Stage 1 重新跑完整流水线。</li>
                </ul>
                <div className="flex items-center gap-2 pt-0.5">
                  <button
                    type="button"
                    disabled={cleanupBusy}
                    onClick={() => void handleConfirmCleanup()}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-amber-600 rounded hover:bg-amber-700 transition-colors cursor-pointer disabled:opacity-50"
                  >
                    {cleanupBusy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                    确认清理
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
          </>
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
        <div
          className={`flex flex-wrap items-center justify-between gap-3 ${
            brandingCollapsed && !brandingForceOpen ? '' : 'mb-3'
          }`}
        >
          <h3 className="inline-flex items-center gap-2 text-sm font-semibold text-slate-900">
            <Film className="w-4 h-4 text-blue-600" />
            本话封页与结尾片包
            <span className="text-xs font-normal text-slate-400">仅重跑 S7 生效</span>
          </h3>
          <div className="inline-flex flex-wrap items-center gap-2 text-xs text-slate-500">
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
            {/* Patch v4.35：折叠开关。 */}
            <button
              type="button"
              onClick={toggleBrandingCollapsed}
              aria-label={brandingCollapsed ? '展开封页选项' : '收起封页选项'}
              title={brandingCollapsed ? '展开封页与结尾片包选项' : '收起封页选项，只留一行摘要'}
              className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-md border border-slate-300 bg-white px-2 py-1 text-[11px] font-medium text-slate-700 hover:border-slate-400 hover:text-slate-900"
            >
              {brandingCollapsed ? (
                <ChevronDown className="w-3 h-3" />
              ) : (
                <ChevronRight className="w-3 h-3" />
              )}
              {brandingCollapsed ? '展开' : '收起'}
            </button>
          </div>
        </div>
        {brandingCollapsed && !brandingForceOpen ? (
          /* Patch v4.35：折叠态一行摘要。 */
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg border border-slate-200 bg-slate-50/70 px-3 py-2 text-xs text-slate-600">
            <span>封页 {brandingOptions.cover_enabled ? '开' : '关'}</span>
            <span className="text-slate-300">·</span>
            <span>
              结尾图{' '}
              {EPISODE_OUTRO_OPTIONS.find((o) => o.value === brandingOptions.outro_mode)
                ?.label ?? brandingOptions.outro_mode}
            </span>
            {brandingOptions.cover_text_override && (
              <>
                <span className="text-slate-300">·</span>
                <span className="truncate max-w-[16rem]">
                  封面文字「{brandingOptions.cover_text_override}」
                </span>
              </>
            )}
            <span className="text-slate-300">·</span>
            <span className="font-mono-tabular">
              总时长 {totalSimDurationMs > 0 ? formatMs(totalSimDurationMs) : '未合成'}
            </span>
            <button
              type="button"
              onClick={toggleBrandingCollapsed}
              className="ml-auto inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2 py-1 text-[11px] font-medium text-slate-700 hover:border-slate-400 hover:text-slate-900"
            >
              <ChevronDown className="w-3 h-3" />
              展开封页选项
            </button>
          </div>
        ) : (
          <>
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
          </>
        )}
      </section>

      {/* Main Studio Workspace Tabs */}
      <section className="bg-white border border-slate-200 rounded-xl">
        <div className="px-5 py-3.5 border-b border-slate-200 rounded-t-xl flex flex-wrap items-center justify-between gap-3">
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

        </div>
          {activeTab === 'script' && (
            /* Patch v4.35：吸顶双区命令栏。
               左区＝定位（搜索 / 筛选 / 展开折叠），右区＝落盘（未保存计数 + 两个保存按钮），
               第二行＝批量处理与辅助入口。sticky 让长表格滚动时命令栏始终在手边，
               也修掉了 v4.34「按钮全挤成图标、标题看不出功能」的可用性问题。 */
            <div className="sticky top-0 z-20 px-5 py-2.5 bg-white/95 backdrop-blur border-b border-slate-200">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                {/* 左区：即时搜索 */}
                <div className="relative">
                  <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400 pointer-events-none" />
                  <input
                    type="search"
                    value={scriptQuery}
                    onChange={(event) => setScriptQuery(event.target.value)}
                    placeholder="搜索 beat_id / 字幕 / 角色 / 分镜"
                    aria-label="搜索解说句"
                    className="w-[240px] pl-8 pr-2 py-1.5 text-xs bg-white border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-600/20"
                  />
                </div>
                {/* 左区：轻量筛选（带计数） */}
                <div className="flex items-center gap-1" role="group" aria-label="解说句筛选">
                  {SCRIPT_FILTER_ORDER.map((key) => (
                    <button
                      key={key}
                      type="button"
                      onClick={() => setScriptFilter(key)}
                      aria-pressed={scriptFilter === key}
                      className={`inline-flex items-center gap-1 px-2 py-1 text-[11px] rounded-lg border cursor-pointer transition-colors ${
                        scriptFilter === key
                          ? 'text-blue-700 bg-blue-50 border-blue-300'
                          : 'text-slate-600 bg-white border-slate-200 hover:bg-slate-50'
                      }`}
                    >
                      {SCRIPT_FILTER_LABELS[key]}
                      <span className="font-mono-tabular text-slate-400">
                        {scriptFilterState.counts[key]}
                      </span>
                    </button>
                  ))}
                </div>
                {/* 左区：展开 / 折叠（带文字，v4.25 起保留的能力） */}
                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    onClick={expandAllScenes}
                    title="展开全部幕，逐句审核解说与多音字"
                    className="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] text-slate-700 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 cursor-pointer"
                  >
                    <ChevronDown className="w-3.5 h-3.5" />
                    展开全部
                  </button>
                  <button
                    type="button"
                    onClick={collapseAllScenes}
                    title="全部折叠，只保留幕标题与要点"
                    className="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] text-slate-700 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 cursor-pointer"
                  >
                    <ChevronRight className="w-3.5 h-3.5" />
                    全部折叠
                  </button>
                </div>
                {/* 右区：未保存计数 + 保存 */}
                <div className="ml-auto flex items-center gap-2">
                  <span
                    title={dirtyBeatCount > 0 ? '这些句子还没保存到磁盘' : '所有改动都已保存'}
                    className={`text-xs ${dirtyBeatCount > 0 ? 'text-amber-700' : 'text-slate-400'}`}
                  >
                    {dirtyBeatCount > 0 ? `${dirtyBeatCount} 处未保存` : '已全部保存'}
                  </span>
                  <button
                    type="button"
                    disabled={savingBeats || characterCheckBusy}
                    onClick={() => void handleSaveBeats(false)}
                    title={characterCheckBusy ? '正在预检角色名写法，请稍候…' : '保存解说稿（Cmd/Ctrl+S）'}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-slate-700 bg-white border border-slate-300 rounded-lg hover:bg-slate-50 cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed"
                  >
                    {savingBeats ? (
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <FileText className="w-3.5 h-3.5" />
                    )}
                    保存草稿
                    <kbd className="text-[10px] text-slate-400 font-sans">⌘S</kbd>
                  </button>
                  <button
                    type="button"
                    disabled={savingBeats || characterCheckBusy}
                    onClick={() => void handleSaveBeats(true)}
                    title="保存并从 Stage 5 重跑配音（会作废旧配音重新合成）"
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed"
                  >
                    {savingBeats ? (
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <RotateCcw className="w-3.5 h-3.5" />
                    )}
                    保存并重配
                    <kbd className="text-[10px] text-blue-200 font-sans">⇧⌘S</kbd>
                  </button>
                </div>
              </div>

              {/* 第二行：批量处理 + 辅助入口 */}
              <div className="flex flex-wrap items-center gap-2 mt-2">
                <div className="relative">
                  <button
                    type="button"
                    onClick={() => setBatchMenuOpen((open) => !open)}
                    aria-haspopup="menu"
                    aria-expanded={batchMenuOpen}
                    className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium text-slate-700 bg-white border border-slate-300 rounded-lg hover:bg-slate-50 cursor-pointer"
                  >
                    <Wand2 className="w-3.5 h-3.5" />
                    批量处理
                    <ChevronDown className="w-3.5 h-3.5" />
                  </button>
                  {batchMenuOpen && (
                    <div
                      role="menu"
                      className="absolute left-0 top-full z-30 mt-1 w-[340px] rounded-xl border border-slate-200 bg-white shadow-lg py-1"
                    >
                      {(
                        [
                          {
                            key: 'add',
                            icon: Plus,
                            title: '新增解说句',
                            hint: '在末尾加一句新解说；新句要重新配音，需从 S5 起重跑',
                            run: () => handleAddBeat(),
                            disabled: false,
                          },
                          {
                            key: 'pacing',
                            icon: Clock,
                            title: '自动节奏留白',
                            hint: '按叙事节奏优化句间停顿；仅需重跑 S6~S7，0 TTS 费用',
                            run: () => void handleRunAutoPacing(),
                            disabled: autoPacingLoading || !onAutoPacing,
                          },
                          {
                            key: 'motion',
                            icon: Wand2,
                            title: '自动分配动效',
                            hint: '按分镜尺寸与情绪自动挑运镜；仅需重跑 S6~S7，0 TTS 费用',
                            run: () => void handleRunAutoMotions(),
                            disabled: autoMotionLoading || !onAutoMotions,
                          },
                          {
                            key: 'collapse',
                            icon: Film,
                            title: '全部按主画面',
                            hint: '把「一句多格画面」收敛为只播主画面；仅需重跑 S6~S7，0 TTS 费用',
                            run: () => handleCollapseToMainPanel(),
                            disabled: false,
                          },
                          {
                            key: 'bgm',
                            icon: Sparkles,
                            title: '补全各幕 AI 配乐建议',
                            hint: 'AI 推断各幕配乐情绪与搜歌关键词；仅需重跑 S7，0 TTS 费用',
                            run: () => void handleSuggestBgm(),
                            disabled: suggestBgmLoading,
                          },
                        ] as const
                      ).map((item) => {
                        const ItemIcon = item.icon;
                        return (
                          <button
                            key={item.key}
                            type="button"
                            role="menuitem"
                            disabled={item.disabled}
                            onClick={() => {
                              setBatchMenuOpen(false);
                              item.run();
                            }}
                            className="w-full px-3 py-2 text-left hover:bg-slate-50 flex gap-2.5 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            <ItemIcon className="w-4 h-4 mt-0.5 shrink-0 text-slate-500" />
                            <span className="min-w-0">
                              <span className="block text-xs font-medium text-slate-800">
                                {item.title}
                              </span>
                              <span className="block text-[11px] text-slate-500">{item.hint}</span>
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => setBgmDrawerOpen(true)}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs text-slate-700 bg-white border border-slate-300 rounded-lg hover:bg-slate-50 cursor-pointer"
                >
                  <Music className="w-3.5 h-3.5" />
                  BGM 曲库
                </button>
                <div className="relative">
                  <button
                    type="button"
                    onClick={() => setRulePopoverOpen((open) => !open)}
                    aria-expanded={rulePopoverOpen}
                    className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs text-slate-700 bg-white border border-slate-300 rounded-lg hover:bg-slate-50 cursor-pointer"
                  >
                    <BookOpen className="w-3.5 h-3.5" />
                    多音字规则
                  </button>
                  {rulePopoverOpen && (
                    <div className="absolute left-0 top-full z-30 mt-1 w-[300px] rounded-xl border border-slate-200 bg-white shadow-lg p-3 space-y-2">
                      <p className="text-[11px] text-slate-500 leading-relaxed">
                        就地新增一条送音替换规则（只影响送音文本，不改字幕）。添加后规则列表立即刷新。
                      </p>
                      <input
                        value={ruleDraft.source}
                        onChange={(event) =>
                          setRuleDraft((prev) => ({ ...prev, source: event.target.value }))
                        }
                        placeholder="原字，如 旻知"
                        aria-label="多音字原字"
                        className="w-full px-2 py-1.5 text-xs border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-600/20"
                      />
                      <input
                        value={ruleDraft.replacement}
                        onChange={(event) =>
                          setRuleDraft((prev) => ({ ...prev, replacement: event.target.value }))
                        }
                        placeholder="送音字，如 闵智"
                        aria-label="多音字送音字"
                        className="w-full px-2 py-1.5 text-xs border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-600/20"
                      />
                      <div className="flex items-center justify-end gap-2">
                        <button
                          type="button"
                          onClick={() => {
                            setRuleDraft({ source: '', replacement: '' });
                            setRulePopoverOpen(false);
                          }}
                          className="px-2.5 py-1 text-xs text-slate-600 hover:text-slate-900 cursor-pointer"
                        >
                          取消
                        </button>
                        <button
                          type="button"
                          onClick={commitRule}
                          disabled={!ruleDraft.source.trim() || !ruleDraft.replacement.trim() || !onAddRule}
                          className="inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          <Plus className="w-3.5 h-3.5" />
                          添加规则
                        </button>
                      </div>
                    </div>
                  )}
                </div>
                {/* Patch v4.35：配音模型只在这里展示一次，不再在 120 行里重复。 */}
                <span className="ml-auto text-[11px] text-slate-500">
                  配音模型：{config.ttsModel}
                </span>
              </div>
            </div>
          )}

        {activeTab === 'script' && scriptNotices.length > 0 && (
          /* Patch v4.35：结果条收敛为「最多 2 条 + 可关闭」，
             避免 6~8 条并列 Banner 把表格挤到屏幕外。 */
          <div className="mt-2 space-y-2">
            {scriptNotices
              .filter((notice) => !dismissedNotices.includes(notice.key))
              .slice(0, 2)
              .map((notice) => (
                <div
                  key={notice.key}
                  className={`flex items-start gap-2 px-3 py-2 text-xs rounded-lg border ${
                    notice.tone === 'error'
                      ? 'text-red-700 bg-red-50 border-red-200'
                      : notice.tone === 'success'
                        ? 'text-emerald-800 bg-emerald-50 border-emerald-200'
                        : 'text-amber-800 bg-amber-50 border-amber-200'
                  }`}
                >
                  {notice.tone === 'success' ? (
                    <CheckCircle2 className="w-4 h-4 mt-0.5 shrink-0" />
                  ) : notice.tone === 'error' ? (
                    <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
                  ) : (
                    <Film className="w-4 h-4 mt-0.5 shrink-0" />
                  )}
                  <div className="space-y-0.5 flex-1">{notice.node}</div>
                  <button
                    type="button"
                    onClick={() => setDismissedNotices((prev) => [...prev, notice.key])}
                    title="关闭这条提示"
                    aria-label="关闭这条提示"
                    className="shrink-0 opacity-60 hover:opacity-100 cursor-pointer"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))}
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
                {/* 左列：搜索 + 筛选 + 分镜列表 */}
                <div className="lg:col-span-4 space-y-3">
                  {/* Patch v4.35：搜索 + 四态筛选（原来只能一路往下滚）。 */}
                  <div className="space-y-2">
                    <div className="relative">
                      <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400 pointer-events-none" />
                      <input
                        type="search"
                        value={panelQuery}
                        onChange={(event) => setPanelQuery(event.target.value)}
                        placeholder="搜索分镜号 / 画面描述 / 气泡台词"
                        aria-label="搜索分镜"
                        className="w-full pl-8 pr-2 py-1.5 text-xs bg-white border border-slate-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-600/20"
                      />
                    </div>
                    <div
                      className="flex flex-wrap items-center gap-1"
                      role="group"
                      aria-label="分镜筛选"
                    >
                      {(
                        [
                          { key: 'all', label: '全部' },
                          { key: 'dialogue', label: '有台词' },
                          { key: 'skipped', label: '已跳过' },
                          { key: 'tall', label: '长竖格' },
                        ] as const
                      ).map((item) => (
                        <button
                          key={item.key}
                          type="button"
                          onClick={() => setPanelFilter(item.key)}
                          aria-pressed={panelFilter === item.key}
                          className={`px-2 py-1 text-[11px] rounded-lg border cursor-pointer transition-colors ${
                            panelFilter === item.key
                              ? 'text-blue-700 bg-blue-50 border-blue-300'
                              : 'text-slate-600 bg-white border-slate-200 hover:bg-slate-50'
                          }`}
                        >
                          {item.label}
                        </button>
                      ))}
                      <span className="ml-auto text-[11px] font-mono-tabular text-slate-400">
                        {filteredPanels.length} / {episode.panels.length}
                      </span>
                    </div>
                  </div>

                  {/* Patch v4.35：↑ / ↓ 键盘切换分镜（容器聚焦后可用）。 */}
                  <div
                    ref={panelListRef}
                    tabIndex={0}
                    onKeyDown={(event) => {
                      if (event.key === 'ArrowDown') {
                        event.preventDefault();
                        movePanelSelection(1);
                      } else if (event.key === 'ArrowUp') {
                        event.preventDefault();
                        movePanelSelection(-1);
                      }
                    }}
                    title="点一下列表后可用 ↑ / ↓ 切换分镜"
                    className="space-y-2.5 max-h-[560px] overflow-y-auto pr-1 rounded-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-600/30"
                  >
                    {filteredPanels.length === 0 ? (
                      <p className="px-3 py-6 text-xs text-slate-500 text-center">
                        没有符合当前筛选的分镜，换个筛选或清空搜索词试试。
                      </p>
                    ) : (
                      filteredPanels.map((p) => {
                        const isSelected = p.panel_id === activePanel.panel_id;
                        const owner = panelSceneOwner.get(p.panel_id);
                        const beatCount = panelBeatsByPanel.get(p.panel_id)?.length ?? 0;
                        return (
                          <button
                            key={p.panel_id}
                            type="button"
                            data-panel-id={p.panel_id}
                            onClick={() => setSelectedPanelId(p.panel_id)}
                            className={`w-full text-left p-2.5 rounded-lg border flex items-start gap-3 transition-colors cursor-pointer [content-visibility:auto] [contain-intrinsic-size:88px] ${
                              isSelected
                                ? 'border-blue-600 bg-blue-50/40'
                                : 'border-slate-200 hover:bg-slate-50'
                            } ${p.is_transition_blank ? 'opacity-50' : ''}`}
                          >
                            {/* Patch v4.25：缩略图走 ?w=180，并让浏览器懒加载；
                                容器用 content-visibility:auto，200~300 张也不会一次性解码。 */}
                            <img
                              src={panelThumbUrl(p.image_url, 180)}
                              alt={p.panel_id}
                              referrerPolicy="no-referrer"
                              loading="lazy"
                              decoding="async"
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
                              {/* Patch v4.35：补上「所属幕次」与「关联句数」，一眼看出这格在成片里的位置。 */}
                              <div className="text-[11px] text-slate-400 font-mono-tabular mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1">
                                <span>
                                  {p.width}×{p.height}
                                </span>
                                <span>{p.dialogues.length} 条气泡</span>
                                {owner ? (
                                  <span className="text-blue-600 not-italic">
                                    第 {owner.sceneIndex + 1} 幕 · {owner.title}
                                  </span>
                                ) : (
                                  <span>未编入任何幕</span>
                                )}
                                {beatCount > 0 ? (
                                  <span className="text-emerald-600">{beatCount} 句引用</span>
                                ) : (
                                  <span>未被引用</span>
                                )}
                                {p.is_transition_blank ? <span>已跳过</span> : null}
                              </div>
                            </div>
                          </button>
                        );
                      })
                    )}
                  </div>
                </div>

                {/* 右列：紧凑信息头 + 真比例预览 + 画面详情 */}
                <div className="lg:col-span-8 space-y-4">
                  {/* Patch v4.35：紧凑信息头（分镜号 / 尺寸 / 切片 / 跳过切换 / 上下格）。 */}
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-3.5 py-2.5 border border-slate-200 rounded-lg bg-slate-50/60">
                    <span className="text-sm font-mono-tabular font-semibold text-slate-900">
                      {activePanel.panel_id}
                    </span>
                    <span className="text-xs text-slate-600 font-mono-tabular">
                      {activePanel.width}×{activePanel.height}
                    </span>
                    <span className="text-xs text-slate-500 font-mono-tabular">
                      切片 {activePanel.source_slice_range[0]}–{activePanel.source_slice_range[1]}
                    </span>
                    {activePanel.is_transition_blank ? (
                      <span className="text-[11px] text-amber-700">已标记跳过</span>
                    ) : null}
                    <div className="ml-auto flex items-center gap-1.5">
                      <button
                        type="button"
                        onClick={() => movePanelSelection(-1)}
                        className="px-2 py-1 text-[11px] text-slate-700 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 cursor-pointer"
                      >
                        上一格
                      </button>
                      <button
                        type="button"
                        onClick={() => movePanelSelection(1)}
                        className="px-2 py-1 text-[11px] text-slate-700 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 cursor-pointer"
                      >
                        下一格
                      </button>
                      <button
                        type="button"
                        onClick={() =>
                          onTogglePanelBlank(episode.episode_id, activePanel.panel_id)
                        }
                        className="px-2 py-1 text-[11px] text-slate-600 underline cursor-pointer"
                      >
                        {activePanel.is_transition_blank ? '恢复参与剪辑' : '标记为过渡废格(跳过)'}
                      </button>
                    </div>
                    {/* Patch v4.36：说明「跳过」到底跳过了什么。 */}
                    <p className="text-[11px] text-slate-500 leading-relaxed">
                      移出/跳过纯对话与空镜仅不出镜，S4 写稿仍会参考其台词全貌；需整段跳过请关闭整幕开关。
                    </p>
                  </div>

                  <div className="grid grid-cols-1 md:grid-cols-2 gap-6 items-start">
                    <div className="bg-slate-900 rounded-xl p-3 flex flex-col items-center">
                      {/* Patch v4.35：按真实比例渲染。
                          原来固定 aspect-[3/4] + object-cover 会把长竖图裁掉，
                          focus_region 覆盖框因此和真实画面完全对不上。 */}
                      <div
                        className="relative w-full max-w-[320px] overflow-hidden rounded-lg"
                        style={{
                          aspectRatio: `${activePanel.width} / ${activePanel.height}`,
                          maxHeight: '68vh',
                        }}
                      >
                        <img
                          src={activePanel.image_url}
                          alt={activePanel.panel_id}
                          referrerPolicy="no-referrer"
                          className="w-full h-full object-contain"
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
                        <span className="font-mono-tabular text-slate-400">
                          [{activePanel.focus_region.map((n) => n.toFixed(2)).join(', ')}]
                        </span>
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
                        {activePanel.dialogues.length === 0 ? (
                          <p className="text-xs text-slate-400 px-1 py-2">
                            这一格没有识别到气泡文字（纯画面分镜或 OCR 未命中）。
                          </p>
                        ) : (
                          <div className="space-y-2">
                            {activePanel.dialogues.map((d, idx) => (
                              <div
                                key={idx}
                                className="p-3 bg-slate-50 border border-slate-200 rounded-lg text-xs"
                              >
                                <div className="flex items-center justify-between text-slate-500 mb-1">
                                  <span className="font-semibold text-slate-800">
                                    {d.speaker_guess}
                                  </span>
                                  <span className="font-mono-tabular">emotion: {d.emotion}</span>
                                </div>
                                <p className="text-slate-800">{d.text}</p>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>

                      <div className="pt-3 border-t border-slate-100 text-xs text-slate-600">
                        <span>拟声词 (sfx_list)：{activePanel.sfx_list.join(' / ') || '无'}</span>
                      </div>

                      {/* Patch v4.35：关联解说句——点一下直接跳到审核台并高亮那一行。 */}
                      <div className="pt-3 border-t border-slate-100">
                        <div className="text-xs font-medium text-slate-500 mb-2">
                          关联解说句（{panelBeatsByPanel.get(activePanel.panel_id)?.length ?? 0}）
                        </div>
                        {(panelBeatsByPanel.get(activePanel.panel_id) ?? []).length === 0 ? (
                          <p className="text-xs text-slate-400 px-1 py-2">
                            这一格还没有被任何解说句引用，成片里不会出现。
                          </p>
                        ) : (
                          <div className="flex flex-wrap gap-1.5">
                            {(panelBeatsByPanel.get(activePanel.panel_id) ?? []).map((b) => (
                              <button
                                key={b.beat_id}
                                type="button"
                                onClick={() => jumpToBeatInScript(b.beat_id)}
                                title={`跳到审核台：${b.narration_text.slice(0, 40)}`}
                                className="px-2 py-1 text-[11px] font-mono-tabular text-blue-700 bg-blue-50 border border-blue-200 rounded-lg hover:bg-blue-100 cursor-pointer"
                              >
                                {b.beat_id}
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* Patch v4.0 Patch 20: 镜头编排（Storyboard）Tab */}
            {activeTab === 'storyboard' && (
              <div className="space-y-3">
                {/* Patch v4.36：说明「移出/跳过」与「关闭整幕」的区别。 */}
                <p className="text-[11px] text-slate-500 leading-relaxed">
                  移出/跳过纯对话与空镜仅不出镜，S4 写稿仍会参考其台词全貌；需整段跳过请关闭整幕开关。
                </p>
                <StoryboardEditor
                  episode={episode}
                  onJobSubmitted={() => {
                    // 后台任务进度由 App 层定时刷新剧集 stages 驱动，这里无需额外轮询。
                  }}
                  onSwitchToScriptTab={() => setActiveTab('script')}
                  onScenesLoaded={setStoryboardSceneCount}
                />
              </div>
            )}

            {/* TAB 2: Script & Polyphone Editor */}
            {activeTab === 'script' && (
              <div className="space-y-5">
                {beatSceneGroups.map((group) => {
                  const scene = group.scene;
                  // Patch v4.25：这一幕当前是否展开（折叠态不挂载表格 DOM）。
                  const isExpanded = expandedSceneKeys.has(group.key);
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
                      {/* Patch v4.25：幕头部兼折叠开关。
                          折叠态只渲染这一行（<table> / <tr> 完全不挂载），
                          200~300 分镜的一话才不会在打开审核台时卡住。 */}
                      <div
                        role="button"
                        tabIndex={0}
                        aria-expanded={isExpanded}
                        onClick={() => toggleScene(group.key)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter' || event.key === ' ') {
                            event.preventDefault();
                            toggleScene(group.key);
                          }
                        }}
                        title={isExpanded ? '点击收起本幕' : '点击展开本幕'}
                        className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3 border-b border-slate-100 bg-slate-50/60 cursor-pointer hover:bg-slate-100/70"
                      >
                        <span className="inline-flex items-center justify-center w-5 h-5 rounded text-slate-500">
                          {isExpanded ? (
                            <ChevronDown className="h-4 w-4" />
                          ) : (
                            <ChevronRight className="h-4 w-4" />
                          )}
                        </span>
                        <span className="inline-flex items-center justify-center min-w-[3.5rem] px-2 py-1 rounded-md bg-slate-900 text-white text-xs font-semibold">
                          {scene ? `第 ${scene.scene_index} 幕` : '未归幕'}
                        </span>
                        <div className="min-w-[10rem]">
                          <p className="text-sm font-semibold text-slate-800">
                            {scene ? scene.title || '未命名幕' : '未归幕的解说句'}
                            {scene?.location && (
                              <span className="ml-2 text-xs font-normal text-slate-500">
                                · {scene.location}
                              </span>
                            )}
                          </p>
                          <p className="text-[11px] text-slate-500">
                            {group.beats.length} 句 · 配音合计{' '}
                            {(sceneDurationMs / 1000).toFixed(1)}s
                          </p>
                          {/* Patch v4.35：去掉「未保存 / bgm_mood」胶囊徽标，
                              改成一行纯文本 + 圆点分隔，别把幕头部撑成两行。 */}
                          {(dirtySceneKeys.has(group.key) ||
                            (scene && (scene.bgm_mood || scene.bgm_suggestion))) && (
                            <p className="text-[11px] text-slate-500 flex items-center gap-1.5 flex-wrap mt-0.5">
                              {dirtySceneKeys.has(group.key) && (
                                <span className="text-amber-600" title="这一幕有未保存的改动">
                                  未保存
                                </span>
                              )}
                              {scene?.bgm_mood && (
                                <span className="inline-flex items-center gap-1 text-violet-600">
                                  <Music className="h-3 w-3" />
                                  {scene.bgm_mood}
                                </span>
                              )}
                              {scene?.bgm_suggestion && (
                                <span className="truncate max-w-[16rem]" title={scene.bgm_suggestion}>
                                  {scene.bgm_suggestion}
                                </span>
                              )}
                            </p>
                          )}
                        </div>
                        {scene && (
                          // Patch v4.25：点 BGM 控件不能顺手把幕折叠掉。
                          <div
                            className="flex items-center gap-2 ml-auto"
                            onClick={(event) => event.stopPropagation()}
                            onKeyDown={(event) => event.stopPropagation()}
                          >
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
                                className="text-[11px] text-emerald-700 whitespace-nowrap"
                                title="本幕未显式选曲，按情绪档自动命中的曲目"
                              >
                                自动命中：{autoMatchedTrack.filename}
                              </span>
                            )}
                            {selectedTrack && (
                              /* Patch v4.35：原生 <audio controls>（160px）换成迷你按钮，
                                 全局单一 Audio 实例，点谁停谁。 */
                              <button
                                type="button"
                                onClick={() =>
                                  toggleBgmPreview(scene.scene_id, selectedTrack.url)
                                }
                                aria-label={
                                  isAutoMatched ? '试听自动命中的曲目' : '试听本幕曲目'
                                }
                                title={isAutoMatched ? '试听自动命中的曲目' : '试听本幕曲目'}
                                className="w-7 h-7 flex items-center justify-center rounded border border-slate-200 bg-white text-slate-500 hover:border-slate-400 hover:text-slate-900 cursor-pointer"
                              >
                                {bgmPlayingSceneId === scene.scene_id ? (
                                  <Square className="w-3 h-3" />
                                ) : (
                                  <Play className="w-3 h-3" />
                                )}
                              </button>
                            )}
                            {sceneBgmSavingId === scene.scene_id && (
                              <Loader2 className="h-4 w-4 animate-spin text-blue-600" />
                            )}
                          </div>
                        )}
                      </div>
                      {isExpanded && (
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
                                <th className="py-3 px-3 min-w-[185px]">
                                  配音角色与语速
                                </th>
                                {/* Patch v4.29：加宽「运镜与停顿」列，保证停顿数字框不被挤扁。 */}
                                <th className="py-3 px-3 w-44 min-w-[176px]">运镜与停顿</th>
                                <th className="py-3 pl-3 pr-5 w-32 text-right">试听 / 插入 / 删</th>
                              </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-100 text-xs align-top">
                              {group.beats.map((beat) => (
                                <ScriptBeatRow
                                  key={beat.beat_id}
                                  beat={beat}
                                  panel={panelMap.get(beat.target_panel_ids[0])}
                                  panelOptions={panelOptions}
                                  sceneOptions={sceneOptions}
                                  rules={rules}
                                  ttsModel={config.ttsModel}
                                  seriesTts={seriesTts}
                                  roleNames={roleNames}
                                  voiceGroups={voiceGroups}
                                  voiceOptions={voiceOptions}
                                  voiceOptionMap={voiceOptionMap}
                                  regenerateEnabled={regenerateEnabled}
                                  playingBeatId={playingBeatId}
                                  tagPickerOpen={tagPickerBeatId === beat.beat_id}
                                  ttsTextareaRefs={ttsTextareaRefs}
                                  ttsSelections={ttsSelections}
                                  onChangeText={handleRowChangeText}
                                  onChangeRole={handleRowChangeRole}
                                  onChangeVoiceOverride={handleRowChangeVoiceOverride}
                                  onPreviewVoice={handlePreviewBeatVoice}
                                  onNudgeSpeed={handleRowNudgeSpeed}
                                  onResetSpeed={handleRowResetSpeed}
                                  onChangeMotion={handleRowChangeMotion}
                                  onChangePause={handleRowChangePause}
                                  onChangePanel={handleRowChangePanel}
                                  onChangePanelAt={handleRowChangePanelAt}
                                  onRemovePanelAt={handleRowRemovePanelAt}
                                  scopePanelIds={scopePanelIds}
                                  panelsById={panelMap}
                                  onAddPanelAt={handleRowAddPanelAt}
                                  sfxLibrary={sfxLibrary}
                                  onChangeSfxCue={handleRowChangeSfxCue}
                                  onChangeScene={handleRowChangeScene}
                                  onToggleTagPicker={handleRowToggleTagPicker}
                                  onPickTag={handleRowPickTag}
                                  onPreview={handlePreviewAudio}
                                  onRegenerate={handleRowRegenerate}
                                  onInsert={handleInsertBeat}
                                  onDelete={handleDeleteBeat}
                                  onZoom={setZoomBeatId}
                                />
                              ))}
                            </tbody>
                          </table>
                        </div>
                      )}
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
                              {/* Patch v4.25：时间轴也用 ?w=140 缩略图，别再拉原图。 */}
                              {seg.panel?.image_url && (
                                <img
                                  src={panelThumbUrl(seg.panel.image_url, 140)}
                                  alt={seg.beat.target_panel_ids.join('+')}
                                  referrerPolicy="no-referrer"
                                  loading="lazy"
                                  decoding="async"
                                  className="h-8 w-auto object-cover rounded border border-slate-200 shrink-0 ml-1"
                                />
                              )}
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
        onChangeVoiceOverride={(beatId, voiceId) =>
          updateZoomBeat(beatId, 'voice_override', voiceId)
        }
        onChangeSpeed={handleZoomSpeedChange}
        onQuickSave={flushSave}
        /* Patch v4.35：补全 PanelZoomModal 的专注审校能力。 */
        onPreviewAudio={handlePreviewAudio}
        playingBeatId={playingBeatId}
        rules={rules}
        onChangeTtsText={(beatId, text) => handleRowChangeText(beatId, 'tts_text', text)}
        onChangeMotion={handleRowChangeMotion}
        onChangePause={handleRowChangePause}
        onPickTag={handleRowPickTag}
      />

      {/* Patch v4.13：单句重配 TTS 的居中确认弹窗（禁用 window.confirm / window.alert）。 */}
      {regenerateTarget && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 backdrop-blur-[1px] p-4"
          role="dialog"
          aria-modal="true"
          aria-label="确认重配这一句的配音"
          onClick={() => {
            if (!regenerateBusy) setRegenerateTarget(null);
          }}
        >
          <div
            className="w-full max-w-md rounded-xl border border-slate-200 bg-white shadow-xl"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="flex items-start gap-2 border-b border-slate-100 px-4 py-3">
              <AlertCircle className="w-4 h-4 shrink-0 mt-0.5 text-amber-600" />
              <div className="flex-1 space-y-1">
                <p className="text-sm font-semibold text-slate-900">
                  确认重配「
                  <span className="font-mono-tabular">{regenerateTarget}</span>
                  」这一句的配音？
                </p>
                <p className="text-[11px] leading-4 text-slate-500">
                  按当前草稿重新合成，只调用 1 次 TTS；其它句子的音频与顺序完全不动。
                </p>
              </div>
            </div>
            <div className="space-y-2.5 px-4 py-3 text-xs text-slate-700">
              <div className="rounded-lg bg-slate-50 px-2.5 py-2">
                <div className="text-[11px] text-slate-500">待合成文本</div>
                <div className="mt-0.5 whitespace-pre-wrap break-words leading-5 text-slate-800">
                  {regeneratePreview?.ttsText ||
                    regeneratePreview?.narrationText ||
                    '（这句还没有草稿文本）'}
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-slate-500">
                <span>
                  配音角色：{regeneratePreview?.role || '未指定'}
                  {regeneratePreview?.voice ? ` · ${regeneratePreview.voice}` : ''}
                </span>
                <span>
                  语速：
                  {regeneratePreview
                    ? `${regeneratePreview.speed.toFixed(2)}x${
                        regeneratePreview.speedOverridden ? '（已逐句微调）' : ''
                      }`
                    : '—'}
                </span>
                <span>停顿：{regeneratePreview ? `${regeneratePreview.pauseAfterMs}ms` : '—'}</span>
              </div>
            </div>
            <div className="flex flex-wrap items-center justify-end gap-2 border-t border-slate-100 px-4 py-3">
              <button
                type="button"
                disabled={regenerateBusy}
                onClick={() => setRegenerateTarget(null)}
                className="px-3 py-1.5 text-xs font-medium text-slate-600 bg-white border border-slate-200 rounded hover:bg-slate-50 transition-colors cursor-pointer disabled:opacity-50"
              >
                取消
              </button>
              <button
                type="button"
                disabled={regenerateBusy}
                onClick={() => void handleConfirmRegenerate(false)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-amber-600 rounded hover:bg-amber-700 transition-colors cursor-pointer disabled:opacity-50"
              >
                {regenerateBusy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                只重配此句
              </button>
              <button
                type="button"
                disabled={regenerateBusy}
                onClick={() => void handleConfirmRegenerate(true)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-600 rounded hover:bg-blue-700 transition-colors cursor-pointer disabled:opacity-50"
              >
                {regenerateBusy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                重配并从 S6 重新出片
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Patch v4.23：角色名写法告警的应用内确认弹窗（禁用 window.confirm / window.alert）。 */}
      {charPrompt && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 backdrop-blur-[1px] p-4"
          role="dialog"
          aria-modal="true"
          aria-label="角色名写法确认"
          onClick={() => {
            if (!charPrompt.busy) setCharPrompt(null);
          }}
        >
          <div
            className="w-full max-w-2xl max-h-[85vh] flex flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="flex shrink-0 items-start gap-2 border-b border-slate-100 px-4 py-3">
              <AlertCircle className="w-4 h-4 shrink-0 mt-0.5 text-amber-600" />
              <div className="flex-1 space-y-1">
                <p className="text-sm font-semibold text-slate-900">
                  这批解说里有 {charPrompt.warnings.length} 处角色名写法与规范名不一致
                </p>
                <p className="text-[11px] leading-4 text-slate-500">
                  你的修改不会被自动改写。选择「仍按我的修改保存」原样落盘；
                  选择「按建议统一为规范名并保存」才会把下面这些写法替换成规范名。
                </p>
              </div>
              {/* Patch v4.27：右上角关闭按钮（保存进行中时禁用，避免中断落盘）。 */}
              <button
                type="button"
                disabled={charPrompt.busy}
                onClick={() => setCharPrompt(null)}
                aria-label="关闭角色名写法确认"
                title="关闭"
                className="shrink-0 w-6 h-6 flex items-center justify-center rounded text-slate-400 hover:bg-slate-100 hover:text-slate-700 transition-colors cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            {/* Patch v4.27：明细列表与「档案同步」合并进同一个滚动容器，
                否则下面的档案同步区会把上面的明细压扁、底部按钮被挤出屏幕。 */}
            <div className="flex-1 min-h-0 overflow-y-auto px-4 py-3 text-xs text-slate-700">
              <div className="space-y-2">
                {charPrompt.warnings.map((warning, index) => (
                  <div
                    key={`${warning.beat_id}-${warning.field}-${warning.found_name}-${index}`}
                    className="rounded-lg bg-slate-50 px-2.5 py-2"
                  >
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                      <span className="font-medium text-slate-800">
                        第 {warning.beat_index + 1} 句（{warning.beat_id}）
                      </span>
                      <span className="text-[11px] text-slate-500">
                        {CHARACTER_FIELD_LABELS[warning.field] ?? warning.field}
                      </span>
                      <span className="text-[11px] text-slate-500">
                        {CHARACTER_REASON_LABELS[warning.reason] ?? warning.reason}
                      </span>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-2">
                      <span className="rounded bg-white px-1.5 py-0.5 font-mono-tabular text-slate-700 ring-1 ring-slate-200">
                        {warning.found_name}
                      </span>
                      {warning.suggested_canonical ? (
                        <>
                          <span className="text-slate-400">→</span>
                          <span className="rounded bg-blue-50 px-1.5 py-0.5 font-mono-tabular text-blue-700 ring-1 ring-blue-200">
                            {warning.suggested_canonical}
                          </span>
                        </>
                      ) : (
                        <span className="text-[11px] text-slate-500">（没有建议名）</span>
                      )}
                    </div>
                    {warning.snippet && (
                      <div className="mt-1 break-words text-[11px] leading-4 text-slate-500">
                        上下文：{warning.snippet}
                      </div>
                    )}
                    <div className="mt-1 text-[11px] leading-4 text-slate-500">
                      {warning.message}
                    </div>
                  </div>
                ))}
              </div>
              {/* Patch v4.25：角色档案同步选项。
                  默认全部「仅本次放行，不改档案」——用户明确勾选后，
                  「仍按我的修改保存」才会带上 sync_character_updates 提交。 */}
              <div className="mt-4 border-t border-slate-200 pt-3 space-y-2">
                <div className="flex items-baseline gap-2">
                  <p className="text-xs font-semibold text-slate-800">
                    要不要顺手把作品角色档案一起改掉？
                  </p>
                  <p className="text-[11px] text-slate-500">
                    默认不改档案，只放行这一次保存；勾选后按「仍按我的修改保存」才会写入作品设定。
                  </p>
                </div>
                {buildCharacterSyncGroups(charPrompt.warnings).length === 0 && (
                  <p className="text-[11px] text-slate-500">没有可同步的角色名写法。</p>
                )}
                {buildCharacterSyncGroups(charPrompt.warnings).map((group, groupIndex) => (
                  <div
                    key={group.key}
                    className="rounded-lg border border-slate-200 bg-white px-2.5 py-2 space-y-1.5"
                  >
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                      <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono-tabular text-slate-700">
                        {group.found_name}
                      </span>
                      {group.suggested_canonical ? (
                        <>
                          <span className="text-slate-400">→</span>
                          <span className="rounded bg-blue-50 px-1.5 py-0.5 font-mono-tabular text-blue-700 ring-1 ring-blue-200">
                            {group.suggested_canonical}
                          </span>
                        </>
                      ) : (
                        <span className="text-[11px] text-slate-500">
                          {CHARACTER_REASON_LABELS[group.reason] ?? group.reason}（没有建议名）
                        </span>
                      )}
                      <span className="text-[11px] text-slate-500">
                        涉及 {group.beatIds.length} 句
                      </span>
                    </div>
                    <div className="space-y-1">
                      <label className="flex items-start gap-2 cursor-pointer">
                        <input
                          type="radio"
                          name={`char-sync-${groupIndex}`}
                          className="mt-0.5 accent-blue-600"
                          checked={!charSyncChoices[group.key]}
                          onChange={() =>
                            setCharSyncChoices((prev) => {
                              const next = { ...prev };
                              delete next[group.key];
                              return next;
                            })
                          }
                        />
                        <span className="text-[11px] text-slate-700">
                          仅本次放行，不改档案
                        </span>
                      </label>
                      {group.actions.map((action) => (
                        <label key={action} className="flex items-start gap-2 cursor-pointer">
                          <input
                            type="radio"
                            name={`char-sync-${groupIndex}`}
                            className="mt-0.5 accent-blue-600"
                            checked={charSyncChoices[group.key] === action}
                            onChange={() =>
                              setCharSyncChoices((prev) => ({
                                ...prev,
                                [group.key]: action,
                              }))
                            }
                          />
                          <span className="text-[11px] text-slate-700">
                            {CHARACTER_SYNC_ACTION_LABELS[action]}
                          </span>
                        </label>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
            <div className="flex shrink-0 flex-wrap items-center justify-end gap-2 bg-white border-t border-slate-100 px-4 py-3">
              <button
                type="button"
                disabled={charPrompt.busy}
                onClick={handleCharPromptCancel}
                className="px-3 py-1.5 text-xs font-medium text-slate-600 bg-white border border-slate-200 rounded hover:bg-slate-50 transition-colors cursor-pointer disabled:opacity-50"
              >
                返回继续修改
              </button>
              <button
                type="button"
                disabled={charPrompt.busy}
                onClick={() => void handleCharPromptSaveAsIs()}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-slate-700 bg-white border border-slate-300 rounded hover:bg-slate-50 transition-colors cursor-pointer disabled:opacity-50"
              >
                {charPrompt.busy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                仍按我的修改保存
              </button>
              <button
                type="button"
                disabled={charPrompt.busy}
                onClick={() => void handleCharPromptUnify()}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-600 rounded hover:bg-blue-700 transition-colors cursor-pointer disabled:opacity-50"
              >
                {charPrompt.busy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                按建议统一为规范名并保存
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
