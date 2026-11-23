export type CameraMotion =
  | 'static'
  | 'zoom_in'
  | 'zoom_out'
  | 'pan_down'
  | 'pan_up'
  | 'shake';

export type StageId =
  | 's0_catalog'
  | 's1_crawler'
  | 's2_segmenter'
  | 's3_vision'
  | 's4_scriptwriter'
  | 's5_tts'
  | 's6_timeline'
  | 's7_renderer';

export type StageStatus = 'pending' | 'running' | 'done' | 'failed';

export type RunMode = 'phase1_only' | 'full_video';

export interface StageProgress {
  id: StageId;
  code: string;
  label: string;
  phase: 1 | 2;
  status: StageStatus;
  durationSec?: number;
  summary?: string;
}

export interface DialogueBubble {
  speaker_guess: string;
  text: string;
  emotion: 'calm' | 'happy' | 'angry' | 'sad' | 'shocked' | 'whisper' | 'tense';
}

export interface PanelItem {
  panel_id: string;
  image_url: string;
  width: number;
  height: number;
  source_slice_range: [number, number];
  is_tall_panel: boolean;
  is_transition_blank: boolean;
  scene_summary: string;
  dialogues: DialogueBubble[];
  sfx_list: string[];
  focus_region: [number, number, number, number];
}

/** 一条 beat 的配音落点（后端 Stage 5 解析结果，Patch v3.4）。 */
export interface BeatVoiceAssignment {
  /** 实际使用的音色 ID。 */
  voice: string;
  /** 语速倍率。 */
  speed: number;
  /** 来源代号：narrator / character / default_male / default_female / legacy_*。 */
  source: string;
  /** 人类可读来源说明，例如「讲解者（旁白）」「角色专属 · 恩彗」。 */
  label: string;
  /** 命中的角色名。 */
  role: string;
  /** 解析出的说话人性别。 */
  gender: string;
  /** 逐句指定音色（voice_override）时，附上「清空指定后按角色会用哪个音色」。 */
  role_voice?: BeatVoiceAssignment;
  /** Patch v4.4：按角色解析的默认语速（未微调时等于 speed；微调后 speed 是本句生效语速）。 */
  role_speed?: number;
}

export interface ScriptBeat {
  beat_id: string;
  narration_text: string;
  tts_text: string;
  emotion_instruction: string;
  target_panel_ids: string[];
  motion: CameraMotion;
  pause_after_ms: number;
  audio_duration_ms: number;
  /** Stage 4 产出的角色标注（narrator / female_lead / male_lead / female_mature / male_antagonist，或具体人名）。 */
  speaker_role?: string;
  /** Stage 4 产出的说话人性别（male / female / unknown），Patch v3.2 Patch 12。 */
  speaker_gender?: 'male' | 'female' | 'unknown';
  /** Stage 4 产出的可选音效提示（heart_thump / door_slam / whoosh / gasp / phone_msg / footstep / crash）。 */
  sfx_cue?: string | null;
  /** 取景提示（full / top / bottom / center）。 */
  focus_hint?: string;
  /** Patch v3.4：后端解析出的「本句配音」（音色 + 来源），审核台直接展示。 */
  resolved_voice?: BeatVoiceAssignment;
  /** Patch v4.2：逐句指定音色（空串 / 缺省 = 跟随角色解析）。 */
  voice_override?: string;
  /** Patch v4.4：单句语速微调（null / 缺省 = 跟随角色默认语速）。 */
  speed_override?: number | null;
  /** Patch v4.13：这条解说归属的镜头组（幕）scene_id；缺省表示尚未归属。 */
  scene_id?: string | null;
}

/** 百炼官方音色的一条配音规格（Patch v3.2 Patch 13）。 */
export interface VoiceSpec {
  voice: string;
  speed: number;
}

// ---------------------------------------------------------------------------
// Patch v4.23：人工保存前的角色名写法检查
// ---------------------------------------------------------------------------
/** 角色名写法的告警原因。 */
export type CharacterWarningReason =
  /** 别名写法（例如「南道振」应写成「道振」）。 */
  | 'alias_variant'
  /** 同音异写（例如「恩慧」应写成「恩彗」）。 */
  | 'homophone_variant'
  /** 说话人不在作品角色表里，没有建议名。 */
  | 'unregistered_speaker';

/** 一条角色名写法告警（与后端 CharacterCheckWarning 一一对应）。 */
export interface CharacterCheckWarning {
  /** 所属解说句的 ID。 */
  beat_id: string;
  /** 所属解说句在本话中的序号（0-based）。 */
  beat_index: number;
  /** 出问题的字段。 */
  field: 'speaker_role' | 'narration_text' | 'tts_text';
  /** 当前写法。 */
  found_name: string;
  /** 建议的规范名；null 表示没有建议。 */
  suggested_canonical: string | null;
  /** 告警原因。 */
  reason: CharacterWarningReason;
  /** 给人看的一句话说明。 */
  message: string;
  /** 上下文片段（出错位置前后各若干字）。 */
  snippet: string;
}

/** POST /script/check-characters 的响应。 */
export interface CharacterCheckResponse {
  has_warnings: boolean;
  warnings: CharacterCheckWarning[];
}

/** PUT /script 的响应（Patch v4.23 起带角色名告警）。 */
export interface ScriptUpdateResponse {
  script: { beats: ScriptBeat[] } & Record<string, unknown>;
  job: Record<string, unknown> | null;
  character_warnings: CharacterCheckWarning[];
}

/** 前端保存解说稿后的结果（用于刷新本地数据与温和提示）。 */
export interface ScriptSaveOutcome {
  /** 是否落盘成功。 */
  ok: boolean;
  /** 后端返回的角色名写法告警（默认保存时通常为空）。 */
  warnings: CharacterCheckWarning[];
}

// ---------------------------------------------------------------------------
// Patch v4.25：把角色名写法同步回作品角色档案
// ---------------------------------------------------------------------------
/**
 * 角色档案同步动作（与后端 ``sync_character_updates`` 的 ``action`` 一一对应）。
 *
 * - `add_alias`：把 found_name 记为 target_canonical 的合法别名（以后不再提醒）。
 * - `promote_canonical`：把 found_name 提升为规范名，原 target_canonical 降为别名。
 * - `register_character`：把 found_name 登记进作品角色档案（新建角色）。
 */
export type CharacterSyncAction =
  | 'add_alias'
  | 'promote_canonical'
  | 'register_character';

/** 一条角色档案同步指令（PUT /script 请求体 sync_character_updates 的一项）。 */
export interface CharacterSyncUpdate {
  /** 要执行的动作。 */
  action: CharacterSyncAction;
  /** 用户当前写的角色名。 */
  found_name: string;
  /** 作品里的规范名；登记新角色时为 null。 */
  target_canonical?: string | null;
}

/** 作品级配音设置（讲解者 / 通用男声 / 通用女声 / 角色专属音色）。 */
export interface SeriesTtsConfig {
  narrator: VoiceSpec;
  default_male: VoiceSpec;
  default_female: VoiceSpec;
  character_voices: Record<string, VoiceSpec>;
}

/** GET /api/tts/voices 返回的单个音色元数据（Patch v3.3：34 音色结构）。 */
export interface VoiceOption {
  // 规范字段
  voice_id: string;
  display_name: string;
  gender: string;
  age_group: 'child' | 'youth' | 'middle' | 'elder' | string;
  category: 'narrator' | 'character' | 'dialect' | 'drama' | string;
  description: string;
  /** Patch v3.4 Patch 16：供应商标记（qwen / minimax），前端据此分组下拉。 */
  provider?: 'qwen' | 'minimax' | string;
  // 后端同时返回的兼容别名
  id: string;
  name: string;
  label: string;
  sample_url: string;
  sample_ready: boolean;
}

/** GET /api/series/{id}/tts-config 的完整响应。 */
export interface SeriesTtsConfigResponse extends SeriesTtsConfig {
  series_id: string;
  characters: Array<{
    name: string;
    aliases: string[];
    gender: 'male' | 'female' | 'unknown';
    traits: string;
  }>;
  voices: VoiceOption[];
  /** Patch v3.4 Patch 16：MiniMax 28 音色（与百炼清单分开，避免混用）。 */
  minimax_voices?: VoiceOption[];
}

// ---------------------------------------------------------------------------
// Patch v4.5：作品级通用封页写字 + 双结尾图片包
// ---------------------------------------------------------------------------
/** 单话结尾图策略：inherit=跟随作品默认，none=不加结尾，continue=待续，final=完结。 */
export type OutroMode = 'inherit' | 'none' | 'continue' | 'final';

/** 作品级默认结尾策略（单话的 inherit 会解析成它）。 */
export type SeriesDefaultOutroMode = 'none' | 'continue' | 'final';

/** 封页话次文字的排版规格（与后端 CoverTextSpec 字段一致）。 */
export interface CoverTextSpec {
  enabled: boolean;
  /** 文字锚点的归一化横坐标（0.05~0.95）。 */
  x_ratio: number;
  /** 文字锚点的归一化纵坐标（0.05~0.95）。 */
  y_ratio: number;
  font_size: number;
  color: string;
  stroke_color: string;
  stroke_width: number;
  font_name: string;
  /** 文案模板，占位符：{ep}=中文/阿拉伯话次、{ep_no}=恒阿拉伯数字。 */
  template: string;
  use_chinese_numeral: boolean;
}

/** 作品级片包配置（series_config.yaml 的 branding 键）。 */
export interface SeriesBrandingConfig {
  cover_image: string | null;
  outro_continue_image: string | null;
  outro_final_image: string | null;
  cover_duration_ms: number;
  cover_fade_in_ms: number;
  cover_fade_out_ms: number;
  outro_duration_ms: number;
  outro_fade_in_ms: number;
  outro_fade_out_ms: number;
  default_outro_mode: SeriesDefaultOutroMode;
  cover_text: CoverTextSpec;
}

/** GET /api/series/{id}/branding 的完整响应（配置 + 图片 URL + 字体清单）。 */
export interface SeriesBrandingResponse extends SeriesBrandingConfig {
  series_id: string;
  cover_url: string | null;
  outro_continue_url: string | null;
  outro_final_url: string | null;
  font_options: Array<{ name: string; path: string }>;
}

/** 单话片包选项（episodes/ep_XXX/render_options.json）。 */
export interface EpisodeRenderOptions {
  cover_enabled: boolean;
  /** 单话封面文字覆盖；null/空串 = 按模板自动生成「第一话」。 */
  cover_text_override: string | null;
  outro_mode: OutroMode;
}

export interface EpisodeItem {
  episode_no: number;
  episode_id: string;
  title: string;
  publish_date: string;
  viewer_url: string;
  is_locked: boolean;
  selected: boolean;
  current_stage: StageId;
  status: 'idle' | 'queued' | 'running' | 'done' | 'failed';
  video_duration_ms: number;
  stages: StageProgress[];
  panels: PanelItem[];
  beats: ScriptBeat[];
  /** Patch v4.5：单话封页/结尾片包选项（后端缺省返回全默认值）。 */
  render_options?: EpisodeRenderOptions;
  logs: string[];
  /** Patch v4.11：是否已清理过程文件（仅留成片）。 */
  archived?: boolean;
  /** Patch v4.11：话次目录当前占用字节。 */
  disk_bytes?: number;
  /** Patch v4.11：一键清理可回收字节（已归档话次为 0）。 */
  reclaimable_bytes?: number;
}

export interface CharacterProfile {
  name: string;
  aliases: string[];
  visual_traits: string;
  current_status: string;
  first_seen_ep: number;
  last_seen_ep: number;
  /** 角色性别（male / female / unknown），Patch v3.2 Patch 12。 */
  gender?: 'male' | 'female' | 'unknown';
}

export interface PlotThread {
  thread_id: string;
  title: string;
  description: string;
  introduced_ep: number;
  status: 'open' | 'resolved';
  resolved_ep?: number;
}

export interface ArcSummary {
  arc_name: string;
  ep_range: [number, number];
  summary: string;
}

export interface EpisodeDigest {
  episode_no: number;
  one_line_hook: string;
  summary: string;
  keywords: string[];
  characters_present: string[];
}

export interface StoryBibleV2 {
  series_id: string;
  narration_style: string;
  characters: CharacterProfile[];
  plot_threads: PlotThread[];
  arc_summaries: ArcSummary[];
  recent_digests: EpisodeDigest[];
  snapshots_ep_nos: number[];
}

export interface SeriesItem {
  series_id: string;
  series_title: string;
  list_url: string;
  narration_voice_override?: string;
  story_bible: StoryBibleV2;
  episodes: EpisodeItem[];
}

export interface StudioConfig {
  bailianBaseUrl: string;
  visionModel: string;
  /** Patch v4.22：识图（视觉抽取）模型下拉。 */
  visionModelOptions?: string[];
  scriptModel: string;
  /** Patch v3.3 Patch 15：Stage 4A 说书体模型（qwen4-max / qwen3.8-max …）。 */
  storyModel?: string;
  storyModelOptions?: string[];
  scriptModelOptions?: string[];
  ttsModel: string;
  ttsVoice: string;
  /** Patch v3.4 Patch 16：TTS 模型下拉（百炼 / MiniMax / Edge）。 */
  ttsModelOptions?: string[];
  /** Patch v3.4 Patch 16：可选音色清单（百炼 34 / MiniMax 28），供下拉 + 试听。 */
  ttsVoiceOptions?: VoiceOption[];
  minimaxVoiceOptions?: VoiceOption[];
  maxImageLongEdge: number;
  aspectRatio: '9:16';
  bgmDuckingDb: number;
}

export interface PronunciationRule {
  id: string;
  series_id?: string; // undefined = global rule; string = per-series rule
  source: string;
  replacement: string;
  note: string;
}

// ---------------------------------------------------------------------------
// Patch v4.0 Patch 20：镜头编排（Storyboard）
// ---------------------------------------------------------------------------
/** 一幕（镜头组）的人工编排，落盘于 04_script/storyboard.json。 */
export interface StoryboardScene {
  scene_id: string;
  scene_index: number;
  title: string;
  location: string;
  panel_ids: string[];
  /** 参与讲述：false 时 Stage 4 跳过本幕。 */
  enabled: boolean;
  /** 必讲不可漏：true 时覆盖率硬校验会拦截漏讲。 */
  must_cover: boolean;
  /** 编导必讲要点（纠正识图误差 / 补因果）。 */
  user_note: string;
  /** 必保原话：命中本幕的解说必须包含这些台词片段。 */
  locked_quotes: string[];
  /** Stage 3b 整幕视觉联看摘要（AI 生成，只读参考）。 */
  visual_recap?: string;
  // ---- Patch v4.7：按幕 BGM ----
  /** AI 配乐情绪档位（五档之一），空串表示尚未生成。 */
  bgm_mood?: string;
  /** AI 配乐建议（乐器/BPM/氛围 + 英文搜歌关键词）。 */
  bgm_suggestion?: string;
  /** 本幕曲目文件名；null/undefined=自动，'none'=显式静音。 */
  bgm?: string | null;
  /** 本幕独立增益（dB），null=跟随作品默认。 */
  bgm_gain_db?: number | null;
}

/** panels_index 中单格的精简信息（后端 Storyboard 接口下发）。 */
export interface StoryboardPanelInfo {
  panel_id: string;
  image_url: string;
  scene_summary: string;
  dialogues: DialogueBubble[];
}

/** GET /api/episodes/{id}/storyboard 的完整响应。 */
export interface EpisodeStoryboard {
  episode_id: string;
  source: 'auto' | 'human';
  scenes: StoryboardScene[];
  panels_index?: Record<string, StoryboardPanelInfo>;
}

/** POST /storyboard/generate 的响应。 */
export interface StoryboardGenerateResponse {
  job: {
    job_id: string;
    episode_id: string;
    series_id: string;
    episode_no: number;
    status: string;
  };
  storyboard: EpisodeStoryboard;
}

/** POST /api/episodes/{id}/reset 的响应（按步向后级联清理）。 */
export interface EpisodeResetResponse {
  episode_id: string;
  from_stage: StageId;
  cleared_stages: StageId[];
  removed_count: number;
  preserved_human_storyboard: boolean;
  stages: StageProgress[];
  logs?: string[];
}

/** POST /api/episodes/{id}/cleanup-intermediates 的响应（Patch v4.11）。 */
export interface EpisodeCleanupResponse {
  episode_id: string;
  freed_bytes: number;
  kept_bytes: number;
  archived: boolean;
  episode: EpisodeItem;
}

/** POST /api/series/{id}/cleanup-intermediates 的批量响应（Patch v4.11）。 */
export interface SeriesCleanupResponse {
  cleaned_count: number;
  freed_bytes: number;
  skipped_episode_ids: string[];
}

/** 单句配音清单项（对应后端 AudioClipMeta，Patch v4.12）。 */
export interface AudioClipMetaItem {
  beat_id: string;
  audio_path: string;
  duration_ms: number;
  pause_after_ms: number;
  word_timestamps: unknown[];
}

/** POST /api/episodes/{id}/beats/{bid}/regenerate-tts 的响应（Patch v4.12）。 */
export interface BeatRegenerateTTSResponse {
  episode_id: string;
  beat_id: string;
  clip: AudioClipMetaItem;
  duration_ms: number;
  /** 重配后最新的整话 beats（含新的 audio_duration_ms），直接替换本地基准。 */
  beats: ScriptBeat[];
  /** rerun_from_s6=true 时返回的后台任务，否则为 null。 */
  job: {
    job_id: string;
    episode_id: string;
    series_id: string;
    episode_no: number;
    status: string;
  } | null;
}

// ---------------------------------------------------------------------------
// Patch v4.6：叙事节奏自动留白（auto-pacing）
// ---------------------------------------------------------------------------
/** 自动节奏留白的执行明细（与后端 PacingReport + manifest 同步数对齐）。 */
export interface AutoPacingSummary {
  updated_count: number;
  scene_end_count: number;
  turn_count: number;
  breath_count: number;
  /** Patch v4.9：跨分镜切换底线留白命中次数。 */
  panel_switch_count?: number;
  changed_beat_ids: string[];
  /** 同步进 05_audio/audio_manifest.json 的条目数。 */
  audio_manifest_synced: number;
}

/** POST /api/episodes/{id}/script/auto-pacing 的完整响应。 */
export interface AutoPacingResponse {
  beats: ScriptBeat[];
  summary: AutoPacingSummary;
}

// ---------------------------------------------------------------------------
// Patch v4.7：按幕 AI 配乐与曲库管理
// ---------------------------------------------------------------------------
/** 五档配乐情绪预设（与后端 s3b_scenes.BGM_MOOD_PRESETS 对齐）。 */
export interface BgmMoodPreset {
  mood: string;
  label: string;
  description: string;
  search_keywords: string[];
  default_suggestion: string;
}

/** 曲库中的一首曲目（作品级 scope='series' / 全局 scope='global'）。 */
export interface BgmTrackItem {
  filename: string;
  scope: 'series' | 'global';
  size_bytes: number;
  /** 经后端的流式试听地址。 */
  url: string;
}

/** GET /api/series/{id}/bgm-library 的完整响应。 */
export interface BgmLibraryResponse {
  series_id: string;
  moods: BgmMoodPreset[];
  tracks: BgmTrackItem[];
}
