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
}

/** 百炼官方音色的一条配音规格（Patch v3.2 Patch 13）。 */
export interface VoiceSpec {
  voice: string;
  speed: number;
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
