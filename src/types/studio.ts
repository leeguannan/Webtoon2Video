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
