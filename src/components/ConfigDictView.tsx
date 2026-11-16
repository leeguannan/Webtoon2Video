import React, { useState } from 'react';
import { Plus, Trash2, Save, Check } from 'lucide-react';
import type { PronunciationRule, StudioConfig } from '../types/studio';

interface ConfigDictViewProps {
  config: StudioConfig;
  rules: PronunciationRule[];
  onSaveConfig: (cfg: StudioConfig) => void;
  onAddRule: (rule: Omit<PronunciationRule, 'id'>) => void;
  onDeleteRule: (id: string) => void;
}

export const ConfigDictView: React.FC<ConfigDictViewProps> = ({
  config,
  rules,
  onSaveConfig,
  onAddRule,
  onDeleteRule,
}) => {
  const [draft, setDraft] = useState<StudioConfig>(config);
  const [savedNotice, setSavedNotice] = useState(false);

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
                <option value="qwen3-vl-plus">qwen3-vl-plus (推荐主力)</option>
                <option value="qwen-vl-max">qwen-vl-max (密集小字增强)</option>
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
                <option value="qwen-plus">qwen-plus (高性价比)</option>
                <option value="qwen3-max">qwen3-max (长篇精修)</option>
                <option value="deepseek-v3">deepseek-v3 (百炼内置)</option>
              </select>
            </div>
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
                <option value="qwen3-tts-flash">qwen3-tts-flash (指令情绪)</option>
                <option value="qwen-audio-3.1-tts-plus">qwen-audio-3.1-tts-plus (支持SSML)</option>
              </select>
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                音色 ID (Voice)
              </label>
              <input
                type="text"
                value={draft.ttsVoice}
                onChange={(e) => setDraft({ ...draft, ttsVoice: e.target.value })}
                className="w-full px-3 py-2 text-xs font-mono-tabular bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
              />
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
