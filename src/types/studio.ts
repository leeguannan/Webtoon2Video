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

/** GET /api/tts/voices 返回的单个音色元数据。 */
export interface VoiceOption {
  id: string;
  label: string;
  name: string;
  gender: string;
  traits: string;
  use_case: string;
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
  ttsModel: string;
  ttsVoice: string;
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
