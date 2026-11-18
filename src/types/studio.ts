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
