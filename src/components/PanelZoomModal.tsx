import React, { useEffect, useRef } from 'react';
import {
  AlertCircle,
  Check,
  ChevronLeft,
  ChevronRight,
  Loader2,
  X,
} from 'lucide-react';
import type { PanelItem, ScriptBeat } from '../types/studio';

/** 字幕原文自动保存的状态机。 */
export type SaveStatus = 'idle' | 'dirty' | 'saving' | 'saved' | 'error';

interface PanelZoomModalProps {
  open: boolean;
  beats: ScriptBeat[];
  panels: PanelItem[];
  activeBeatId: string | null;
  saveStatus: SaveStatus;
  saveMessage?: string;
  onClose: () => void;
  onSelectBeat: (beatId: string) => void;
  onChangeNarration: (beatId: string, text: string) => void;
}

const SAVE_STATUS_TEXT: Record<SaveStatus, string> = {
  idle: '尚未修改',
  dirty: '待保存',
  saving: '保存中…',
  saved: '已保存',
  error: '保存失败',
};

/**
 * 分镜大图 + 字幕原文编辑弹窗。
 *
 * 存在的理由：审核台表格里的分镜只有 48×64 的小图，字与画面对不上号，
 * 改字幕只能盲改。这里把面板原图放大到可看清的尺寸，右侧直接编辑该条解说的字幕原文，
 * 并支持上一格 / 下一格连续翻看，编辑后自动落盘。
 */
export const PanelZoomModal: React.FC<PanelZoomModalProps> = ({
  open,
  beats,
  panels,
  activeBeatId,
  saveStatus,
  saveMessage,
  onClose,
  onSelectBeat,
  onChangeNarration,
}) => {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const index = beats.findIndex((b) => b.beat_id === activeBeatId);
  const beat = index >= 0 ? beats[index] : undefined;
  const panelId = beat?.target_panel_ids[0] ?? '';
  const panel = panels.find((p) => p.panel_id === panelId);

  // 打开时把光标放进字幕框，直接就能改字。
  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => {
      textareaRef.current?.focus();
      // 光标落在文末（不整段选中，避免翻格后一打字就把整句覆盖掉）
      const len = textareaRef.current?.value.length ?? 0;
      textareaRef.current?.setSelectionRange(len, len);
    }, 50);
    return () => window.clearTimeout(timer);
  }, [open, activeBeatId]);

  // Esc 关闭；左右方向键翻页（焦点在文本框里时让给光标）。
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === 'TEXTAREA' || tag === 'INPUT' || target?.isContentEditable) return;
      if (index < 0) return;
      const nextIndex = event.key === 'ArrowLeft' ? index - 1 : index + 1;
      if (nextIndex < 0 || nextIndex >= beats.length) return;
      event.preventDefault();
      onSelectBeat(beats[nextIndex].beat_id);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open, index, beats, onClose, onSelectBeat]);

  if (!open || !beat) return null;

  const charLen = beat.narration_text.trim().length;
  const isLengthWarn = charLen < 8 || charLen > 30;
  const canPrev = index > 0;
  const canNext = index < beats.length - 1;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/80 backdrop-blur-sm"
      onClick={onClose}
      role="presentation"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="分镜大图与字幕原文编辑"
        className="w-full max-w-6xl max-h-[92vh] bg-white rounded-xl shadow-2xl overflow-hidden flex flex-col"
        onClick={(event) => event.stopPropagation()}
      >
        {/* 顶部：定位信息与关闭 */}
        <div className="flex items-center justify-between gap-3 px-5 py-3 border-b border-slate-200 shrink-0">
          <div className="flex items-center gap-3 min-w-0">
            <span className="font-mono-tabular text-sm font-semibold text-slate-900">
              {beat.beat_id}
            </span>
            <span className="font-mono-tabular text-xs text-slate-500">{panelId}</span>
            <span className="text-xs text-slate-400 font-mono-tabular">
              第 {index + 1} / {beats.length} 条
            </span>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭弹窗"
            className="p-1.5 text-slate-400 hover:text-slate-700 cursor-pointer"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_360px]">
          {/* 左：大图 */}
          <div className="min-h-0 bg-slate-900 flex items-center justify-center p-4 overflow-hidden">
            {panel ? (
              <img
                src={panel.image_url}
                alt={`${panel.panel_id} 分镜大图`}
                referrerPolicy="no-referrer"
                className="max-h-full max-w-full object-contain rounded"
              />
            ) : (
              <p className="text-xs text-slate-400">该条解说未绑定分镜</p>
            )}
          </div>

          {/* 右：字幕原文编辑 */}
          <div className="min-h-0 overflow-y-auto p-5 space-y-4 bg-white">
            {panel?.scene_summary && (
              <div>
                <div className="text-[11px] font-medium text-slate-400 mb-1">画面摘要</div>
                <p className="text-xs text-slate-600 leading-relaxed">{panel.scene_summary}</p>
              </div>
            )}

            <div>
              <label
                htmlFor="zoom-narration-text"
                className="block text-xs font-medium text-slate-700 mb-1.5"
              >
                字幕原文 (narration_text · 屏幕显示)
              </label>
              <textarea
                id="zoom-narration-text"
                ref={textareaRef}
                rows={6}
                value={beat.narration_text}
                onChange={(event) => onChangeNarration(beat.beat_id, event.target.value)}
                className="w-full p-3 text-sm text-slate-900 bg-white border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600 resize-y"
              />
              <div className="flex items-center justify-between mt-1.5 text-[11px] font-mono-tabular">
                <span className={isLengthWarn ? 'text-amber-600' : 'text-slate-400'}>
                  {charLen} 字（建议 10–28 字）
                </span>
                <span className="text-slate-400">
                  时长 {(beat.audio_duration_ms / 1000).toFixed(2)}s
                </span>
              </div>
            </div>

            {/* 保存状态 */}
            <div
              className="flex items-center gap-1.5 text-[11px]"
              role="status"
              aria-live="polite"
            >
              {saveStatus === 'saving' && (
                <>
                  <Loader2 className="w-3.5 h-3.5 text-blue-600 animate-spin" />
                  <span className="text-slate-500">保存中…</span>
                </>
              )}
              {saveStatus === 'saved' && (
                <>
                  <Check className="w-3.5 h-3.5 text-emerald-600" />
                  <span className="text-emerald-700">
                    已保存{saveMessage ? ` ${saveMessage}` : ''}
                  </span>
                </>
              )}
              {saveStatus === 'error' && (
                <>
                  <AlertCircle className="w-3.5 h-3.5 text-red-600" />
                  <span className="text-red-600">
                    保存失败{saveMessage ? `：${saveMessage}` : '，请重试'}
                  </span>
                </>
              )}
              {(saveStatus === 'dirty' || saveStatus === 'idle') && (
                <>
                  <span className="text-slate-400">
                    {saveStatus === 'dirty'
                      ? '待保存 · 切换分镜或关闭弹窗时自动保存'
                      : SAVE_STATUS_TEXT.idle}
                  </span>
                </>
              )}
            </div>

            {/* 上下格切换 */}
            <div className="flex items-center gap-2 pt-1">
              <button
                type="button"
                onClick={() => canPrev && onSelectBeat(beats[index - 1].beat_id)}
                disabled={!canPrev}
                className="inline-flex items-center gap-1 px-3 py-2 text-xs font-medium text-slate-700 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
              >
                <ChevronLeft className="w-3.5 h-3.5" />
                上一格
              </button>
              <button
                type="button"
                onClick={() => canNext && onSelectBeat(beats[index + 1].beat_id)}
                disabled={!canNext}
                className="inline-flex items-center gap-1 px-3 py-2 text-xs font-medium text-slate-700 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
              >
                下一格
                <ChevronRight className="w-3.5 h-3.5" />
              </button>
            </div>

            <p className="text-[11px] text-slate-400 leading-relaxed">
              快捷键：Esc 关闭 · ←/→ 翻格（焦点在输入框内时为移动光标）
              <br />
              修改字幕原文后，在切换分镜或关闭弹窗时自动保存，不会触发重新配音；如需重出配音请回审核台点「保存并从 Stage 5 重跑配音」。
            </p>
          </div>
        </div>
      </div>
    </div>
  );
};
