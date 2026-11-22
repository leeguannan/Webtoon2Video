import React, { useState } from 'react';
import { Plus, Trash2, Save, Check } from 'lucide-react';
import type { PronunciationRule, StudioConfig, VoiceOption } from '../types/studio';
import { VoicePreviewButton, VoiceSelect, findVoiceOption, useVoicePreview } from './VoicePicker';

interface ConfigDictViewProps {
  config: StudioConfig;
  rules: PronunciationRule[];
  onSaveConfig: (cfg: StudioConfig) => void;
  onAddRule: (rule: Omit<PronunciationRule, 'id'>) => void;
  onDeleteRule: (id: string) => void;
}

/** Patch v3.3 Patch 15：Stage 4 撰稿模型候选（后端未给选项时的兜底）。 */
const FALLBACK_SCRIPT_MODEL_OPTIONS = [
  'qwen3.7-plus',
  'qwen3-max',
  'qwen-plus',
  'deepseek-v3',
  'deepseek-v4.1-flash-1017',
];

/** Patch v3.3 Patch 15：Stage 4A 说书体模型候选（后端未给选项时的兜底）。 */
const FALLBACK_STORY_MODEL_OPTIONS = [
  'qwen4-max',
  'qwen3.8-max',
  'qwen3.7-plus',
  'qwen-plus',
  'deepseek-v4.1-flash-1017',
];

/** Patch v3.4 Patch 16：Stage 5 TTS 模型候选（后端未给选项时的兜底）。 */
const FALLBACK_TTS_MODEL_OPTIONS = [
  'qwen3-tts-flash',
  'qwen-audio-3.1-tts-plus',
  'speech-2.8-hd',
  'speech-2.8-turbo',
  'edge-tts',
];

/** Patch v4.22：识图（视觉抽取）模型候选（后端未给选项时的兜底）。 */
const FALLBACK_VISION_MODEL_OPTIONS = [
  'qwen3.7-plus',
  'qwen-vl-max',
  'qwen3-vl-plus',
  'deepseek-v4.1-flash-1017',
];

/**
 * Patch v4.22：识图模型下拉标签。
 * qwen3.7-plus 原生多图 + 长上下文，Stage 3 走批窗抽取（主力推荐）；
 * qwen-vl-max 密集小字 / 气泡归属更强，但百炼该模型 ID 已下线（404）；
 * qwen3-vl-plus 经典视觉线（单图模型，逐格抽取）；
 * deepseek-v4.1-flash-1017 实测对图片输入返回空内容（仅作对比测试）。
 */
const VISION_MODEL_LABELS: Record<string, string> = {
  'qwen3.7-plus': 'qwen3.7-plus (原生多模态 · 主力推荐)',
  'qwen-vl-max': 'qwen-vl-max (密集小字增强 · 百炼已下线 404)',
  'qwen3-vl-plus': 'qwen3-vl-plus (经典视觉线)',
  'deepseek-v4.1-flash-1017': 'deepseek-v4.1-flash-1017 (百炼多模态 · 对比测试)',
};

const SCRIPT_MODEL_LABELS: Record<string, string> = {
  'qwen3.7-plus': 'qwen3.7-plus (新版主力)',
  'qwen3.8-max': 'qwen3.8-max (长篇精修)',
  'qwen3-max': 'qwen3-max (长篇精修)',
  'qwen-plus': 'qwen-plus (高性价比)',
  'deepseek-v3': 'deepseek-v3 (百炼内置)',
  'deepseek-v4.1-flash-1017': 'deepseek-v4.1-flash-1017 (百炼 · DeepSeek 高性价比)',
};

const STORY_MODEL_LABELS: Record<string, string> = {
  'qwen4-max': 'qwen4-max (最新说书体)',
  'qwen3.8-max': 'qwen3.8-max (备选)',
  'qwen3.7-plus': 'qwen3.7-plus (备选)',
  'qwen-plus': 'qwen-plus (兜底)',
  'deepseek-v4.1-flash-1017': 'deepseek-v4.1-flash-1017 (百炼 · DeepSeek 高性价比)',
};

/** Patch v3.4 Patch 16：TTS 模型下拉标签（百炼 / MiniMax / Edge 三套 provider）。 */
const TTS_MODEL_LABELS: Record<string, string> = {
  'qwen3-tts-flash': 'qwen3-tts-flash (百炼 · 指令情绪)',
  'qwen-audio-3.1-tts-plus': 'qwen-audio-3.1-tts-plus (百炼 · 支持 SSML)',
  'speech-2.8-hd': 'speech-2.8-hd (MiniMax · 高清)',
  'speech-2.8-turbo': 'speech-2.8-turbo (MiniMax · 快速)',
  'edge-tts': 'edge-tts (微软 Edge · 免费)',
};

export const ConfigDictView: React.FC<ConfigDictViewProps> = ({
  config,
  rules,
  onSaveConfig,
  onAddRule,
  onDeleteRule,
}) => {
  const [draft, setDraft] = useState<StudioConfig>(config);
  const [savedNotice, setSavedNotice] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  /** Patch v3.4 Patch 16：试听静态样例（不调用 TTS 合成）。 */
  const { playingKey, preview, stop } = useVoicePreview((message) => setPreviewError(message));

  const [newSource, setNewSource] = useState('');
  const [newReplacement, setNewReplacement] = useState('');
  const [newNote, setNewNote] = useState('');

  const handleSaveCfg = (e: React.FormEvent) => {
    e.preventDefault();
    onSaveConfig(draft);
    setSavedNotice(true);
    setTimeout(() => setSavedNotice(false), 2000);
  };

  const handleAddRule = (e: React.FormEvent) => {
    e.preventDefault();
    if (!newSource.trim() || !newReplacement.trim()) return;
    onAddRule({
      source: newSource.trim(),
      replacement: newReplacement.trim(),
      note: newNote.trim() || '自定义多音字纠偏',
    });
    setNewSource('');
    setNewReplacement('');
    setNewNote('');
  };

  // Patch v3.3 Patch 15：模型下拉以服务端候选为准，并把当前值兜底进列表避免回退丢值。
  const scriptModelOptions = React.useMemo(() => {
    const base =
      draft.scriptModelOptions && draft.scriptModelOptions.length > 0
        ? draft.scriptModelOptions
        : FALLBACK_SCRIPT_MODEL_OPTIONS;
    return base.includes(draft.scriptModel) ? base : [draft.scriptModel, ...base];
  }, [draft.scriptModelOptions, draft.scriptModel]);

  const storyModelOptions = React.useMemo(() => {
    const base =
      draft.storyModelOptions && draft.storyModelOptions.length > 0
        ? draft.storyModelOptions
        : FALLBACK_STORY_MODEL_OPTIONS;
    const current = draft.storyModel ?? '';
    return current && !base.includes(current) ? [current, ...base] : base;
  }, [draft.storyModelOptions, draft.storyModel]);

  /** Patch v3.4 Patch 16：TTS 模型候选（后端为准，当前值兜底进列表避免回退丢值）。 */
  const ttsModelOptions = React.useMemo(() => {
    const base =
      draft.ttsModelOptions && draft.ttsModelOptions.length > 0
        ? draft.ttsModelOptions
        : FALLBACK_TTS_MODEL_OPTIONS;
    return base.includes(draft.ttsModel) ? base : [draft.ttsModel, ...base];
  }, [draft.ttsModelOptions, draft.ttsModel]);

  /** Patch v4.22：识图模型候选（后端为准，当前值兜底进列表避免回退丢值）。 */
  const visionModelOptions = React.useMemo(() => {
    const base =
      draft.visionModelOptions && draft.visionModelOptions.length > 0
        ? draft.visionModelOptions
        : FALLBACK_VISION_MODEL_OPTIONS;
    return base.includes(draft.visionModel) ? base : [draft.visionModel, ...base];
  }, [draft.visionModelOptions, draft.visionModel]);

  /** Patch v4.15：音色选择仅保留 MiniMax（剔除百炼 / 阿里音色）。 */
  const voiceOptions: VoiceOption[] = React.useMemo(
    () => [...(draft.minimaxVoiceOptions ?? [])],
    [draft.minimaxVoiceOptions],
  );
  const currentVoice = findVoiceOption(voiceOptions, draft.ttsVoice);
  const minimaxModel = draft.ttsModel.startsWith('speech-');
  const voiceProviderMismatch = minimaxModel && currentVoice?.provider === 'qwen';

  return (
    <div className="grid grid-cols-1 lg:grid-cols-12 gap-6 items-start">
      {/* Left: Bailian & Video Config */}
      <form
        onSubmit={handleSaveCfg}
        className="lg:col-span-6 bg-white border border-slate-200 rounded-xl p-6 space-y-5"
      >
        <div className="flex items-center justify-between border-b border-slate-100 pb-4">
          <div>
            <h2 className="text-base font-semibold text-slate-900">
              阿里云百炼与合成参数（config.yaml）
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              视觉理解、剧本生成与 Qwen TTS 共用 DASHSCOPE_API_KEY
            </p>
          </div>
          <button
            type="submit"
            className="inline-flex items-center gap-1.5 px-3.5 py-2 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors whitespace-nowrap shrink-0 cursor-pointer"
          >
            {savedNotice ? <Check className="w-3.5 h-3.5" /> : <Save className="w-3.5 h-3.5" />}
            {savedNotice ? '已更新配置' : '保存配置'}
          </button>
        </div>

        <div className="space-y-4">
          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">
              百炼兼容模式 Base URL
            </label>
            <input
              type="text"
              value={draft.bailianBaseUrl}
              onChange={(e) => setDraft({ ...draft, bailianBaseUrl: e.target.value })}
              className="w-full px-3 py-2 text-xs font-mono-tabular bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
            />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                视觉抽取模型 (Stage 3)
              </label>
              <select
                value={draft.visionModel}
                onChange={(e) => setDraft({ ...draft, visionModel: e.target.value })}
                className="w-full px-3 py-2 text-xs font-mono-tabular bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
              >
                {visionModelOptions.map((model) => (
                  <option key={model} value={model}>
                    {VISION_MODEL_LABELS[model] ?? model}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                解说撰稿模型 (Stage 4)
              </label>
              <select
                value={draft.scriptModel}
                onChange={(e) => setDraft({ ...draft, scriptModel: e.target.value })}
                className="w-full px-3 py-2 text-xs font-mono-tabular bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
              >
                {scriptModelOptions.map((model) => (
                  <option key={model} value={model}>
                    {SCRIPT_MODEL_LABELS[model] ?? model}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">
              说书体模型 (Stage 4A · 故事反写)
            </label>
            <select
              value={draft.storyModel ?? ''}
              onChange={(e) => setDraft({ ...draft, storyModel: e.target.value })}
              className="w-full px-3 py-2 text-xs font-mono-tabular bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
            >
              {storyModelOptions.map((model) => (
                <option key={model} value={model}>
                  {STORY_MODEL_LABELS[model] ?? model}
                </option>
              ))}
            </select>
            <p className="text-[11px] text-slate-400 mt-1">
              账号未开通该模型时自动降级：qwen4-max → qwen3.8-max → qwen3.7-plus → qwen-plus；
              选用 deepseek-v4.1-flash-1017 前需先在百炼控制台开通该模型的调用权限
            </p>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                TTS 语音模型 (Stage 5)
              </label>
              <select
                value={draft.ttsModel}
                onChange={(e) => setDraft({ ...draft, ttsModel: e.target.value })}
                className="w-full px-3 py-2 text-xs font-mono-tabular bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
              >
                {ttsModelOptions.map((model) => (
                  <option key={model} value={model}>
                    {TTS_MODEL_LABELS[model] ?? model}
                  </option>
                ))}
              </select>
              <p className="text-[11px] text-slate-400 mt-1">
                MiniMax 走 speech-2.8 系列（需 .env 里配置 MINIMAX_API_KEY）
              </p>
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                音色 ID (Voice)
              </label>
              <div className="flex items-start gap-2">
                <div className="flex-1 min-w-0">
                  <VoiceSelect
                    value={draft.ttsVoice}
                    voices={voiceOptions}
                    ariaLabel="默认音色"
                    placeholder="输入音色 ID，例如 Chinese (Mandarin)_Radio_Host"
                    className="w-full px-3 py-2 text-xs font-mono-tabular bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
                    onChange={(voice) => setDraft({ ...draft, ttsVoice: voice })}
                  />
                  <p className="text-[11px] text-slate-400 mt-1">
                    {currentVoice
                      ? `${currentVoice.provider === 'minimax' ? 'MiniMax' : '百炼'} · ${currentVoice.description}`
                      : `目录外音色 ID：${draft.ttsVoice}（无静态样例，无法试听）`}
                  </p>
                </div>
                <VoicePreviewButton
                  playing={playingKey === 'ttsVoice'}
                  sampleReady={currentVoice?.sample_ready ?? false}
                  onPreview={() => preview('ttsVoice', { voice: draft.ttsVoice, speed: 1 }, voiceOptions)}
                  onStop={stop}
                />
              </div>
              {voiceProviderMismatch && (
                <p className="text-[11px] text-amber-600 mt-1">
                  当前音色属于百炼，MiniMax 模型会自动替换为 MiniMax 旁白音色（保存后回显）。
                </p>
              )}
              {previewError && (
                <p className="text-[11px] text-red-600 mt-1">试听失败：{previewError}</p>
              )}
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                送图最大长边 (px)
              </label>
              <input
                type="number"
                value={draft.maxImageLongEdge}
                onChange={(e) =>
                  setDraft({ ...draft, maxImageLongEdge: Number(e.target.value) || 1280 })
                }
                className="w-full px-3 py-2 text-xs font-mono-tabular bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                输出视频画幅（已锁定）
              </label>
              <div className="w-full px-3 py-2 text-xs font-mono-tabular bg-slate-100 border border-slate-200 rounded-lg text-slate-700">
                9:16 竖屏成片 (1080×1920)
              </div>
              <p className="text-[11px] text-slate-400 mt-1">
                契合条漫竖向构图，保留完整分镜不裁切
              </p>
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                BGM 闪避压低 (dB)
              </label>
              <input
                type="number"
                value={draft.bgmDuckingDb}
                onChange={(e) =>
                  setDraft({ ...draft, bgmDuckingDb: Number(e.target.value) || -18 })
                }
                className="w-full px-3 py-2 text-xs font-mono-tabular bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
              />
            </div>
          </div>
        </div>
      </form>

      {/* Right: Polyphone Dictionary */}
      <div className="lg:col-span-6 bg-white border border-slate-200 rounded-xl p-6 space-y-5">
        <div className="border-b border-slate-100 pb-4">
          <h2 className="text-base font-semibold text-slate-900">
            多音字与人名发音纠偏词典（pronunciation_dict.yaml）
          </h2>
          <p className="text-xs text-slate-500 mt-0.5">
            仅作用于发送给 Qwen TTS 的 spoken_text，屏幕 ASS 字幕保持原字面显示，零错别字
          </p>
        </div>

        <form onSubmit={handleAddRule} className="grid grid-cols-1 sm:grid-cols-12 gap-2.5">
          <input
            type="text"
            value={newSource}
            onChange={(e) => setNewSource(e.target.value)}
            placeholder="原词（如：校尉）"
            className="sm:col-span-3 px-3 py-2 text-xs bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
          />
          <input
            type="text"
            value={newReplacement}
            onChange={(e) => setNewReplacement(e.target.value)}
            placeholder="同音替换（如：叫尉）"
            className="sm:col-span-3 px-3 py-2 text-xs bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
          />
          <input
            type="text"
            value={newNote}
            onChange={(e) => setNewNote(e.target.value)}
            placeholder="备注（如：官职读音）"
            className="sm:col-span-4 px-3 py-2 text-xs bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
          />
          <button
            type="submit"
            className="sm:col-span-2 inline-flex items-center justify-center gap-1 px-3 py-2 text-xs font-medium text-white bg-slate-900 rounded-lg hover:bg-slate-800 transition-colors whitespace-nowrap shrink-0 cursor-pointer"
          >
            <Plus className="w-3.5 h-3.5" />
            添加
          </button>
        </form>

        <div className="border border-slate-200 rounded-lg overflow-hidden">
          <table className="w-full text-left border-collapse text-xs">
            <thead>
              <tr className="bg-slate-50 border-b border-slate-200 text-slate-500 font-medium">
                <th className="py-2.5 px-3">字幕原词</th>
                <th className="py-2.5 px-3">TTS 读音替换</th>
                <th className="py-2.5 px-3">说明</th>
                <th className="py-2.5 px-3 w-12 text-right">删</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {rules.map((r) => (
                <tr key={r.id} className="hover:bg-slate-50">
                  <td className="py-2.5 px-3 font-medium text-slate-900">{r.source}</td>
                  <td className="py-2.5 px-3 font-mono-tabular text-blue-700">{r.replacement}</td>
                  <td className="py-2.5 px-3 text-slate-500">{r.note}</td>
                  <td className="py-2.5 px-3 text-right">
                    <button
                      type="button"
                      onClick={() => onDeleteRule(r.id)}
                      aria-label={`删除规则 ${r.source}`}
                      className="text-slate-400 hover:text-red-600 transition-colors cursor-pointer"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </td>
                </tr>
              ))}
              {rules.length === 0 && (
                <tr>
                  <td colSpan={4} className="py-6 text-center text-slate-400">
                    暂无自定义多音字替换规则
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
};
