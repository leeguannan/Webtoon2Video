import React, { useEffect, useRef, useState } from 'react';
import {
  ChevronDown,
  Eye,
  Image as ImageIcon,
  Loader2,
  Save,
  Trash2,
  Upload,
} from 'lucide-react';
import type {
  CoverTextSpec,
  SeriesBrandingResponse,
  SeriesDefaultOutroMode,
} from '../types/studio';

/**
 * 作品级片包面板（Patch v4.5）。
 *
 * 三块能力：
 * 1. 三张片包图上传（通用封页 / 待续结尾 / 完结结尾），Base64 走 JSON 接口；
 * 2. 9:16 封页可视化编辑器：点击/拖拽定位话次文字，滑块与坐标双向同步，
 *    并可调后端 /cover-preview 做 Pillow 真实渲染校验；
 * 3. 片头/片尾时长与作品默认结尾策略，保存进 series_config.yaml。
 *
 * 样式与 SeriesTtsPanel 对齐：默认折叠的圆角白卡片 + 蓝色主按钮 + 红色错误条。
 */

interface SeriesBrandingPanelProps {
  seriesId: string;
}

type SlotKey = 'cover' | 'outro_continue' | 'outro_final';

const SLOT_META: Record<SlotKey, { title: string; hint: string; urlField: keyof SeriesBrandingResponse }> = {
  cover: { title: '通用封页底图', hint: '每话开场自动写上话次标题', urlField: 'cover_url' },
  outro_continue: { title: '待续结尾图', hint: '连载中各话的默认片尾', urlField: 'outro_continue_url' },
  outro_final: { title: '完结结尾图', hint: '大结局话次使用', urlField: 'outro_final_url' },
};

const CN_DIGITS = ['零', '一', '二', '三', '四', '五', '六', '七', '八', '九'];
const CN_UNITS = ['', '十', '百', '千'];

/** 与后端 int_to_chinese 同口径的前端预览版（1~9999，超出回退阿拉伯数字）。 */
function intToChinese(n: number): string {
  if (!Number.isInteger(n) || n < 1 || n > 9999) return String(n);
  const chars = String(n).split('').map(Number);
  let result = '';
  let zeroPending = false;
  for (let i = 0; i < chars.length; i += 1) {
    const digit = chars[i];
    const unit = CN_UNITS[chars.length - 1 - i];
    if (digit === 0) {
      if (result.length > 0) zeroPending = true;
    } else {
      if (zeroPending) {
        result += '零';
        zeroPending = false;
      }
      result += CN_DIGITS[digit] + unit;
    }
  }
  // 口语修正：10~19 「一十X」→「十X」。
  if (result.startsWith('一十')) result = result.slice(1);
  return result;
}

/** 与后端 format_cover_label 同口径的模板格式化（仅用于前端实时预览）。 */
function formatCoverLabel(template: string, episodeNo: number, useChinese: boolean): string {
  const ep = useChinese ? intToChinese(episodeNo) : String(episodeNo);
  return template
    .replaceAll('{ep}', ep)
    .replaceAll('{ep_no}', String(episodeNo));
}

/** 毫秒滑块控件：左标题、右数值，与配音面板滑块同一样式。 */
const MsSlider: React.FC<{
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  suffix?: string;
  onChange: (value: number) => void;
}> = ({ label, value, min, max, step = 50, suffix = 'ms', onChange }) => (
  <label className="block">
    <span className="flex items-center justify-between text-xs text-slate-500 mb-1">
      <span>{label}</span>
      <span className="font-mono-tabular text-slate-700">
        {value}
        {suffix}
      </span>
    </span>
    <input
      type="range"
      min={min}
      max={max}
      step={step}
      value={value}
      onChange={(e) => onChange(Number(e.target.value))}
      className="w-full accent-blue-600 cursor-pointer"
      aria-label={label}
    />
  </label>
);

export const SeriesBrandingPanel: React.FC<SeriesBrandingPanelProps> = ({ seriesId }) => {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<SeriesBrandingResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [uploadingSlot, setUploadingSlot] = useState<SlotKey | null>(null);
  const [episodeNo, setEpisodeNo] = useState(1);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const previewBoxRef = useRef<HTMLDivElement | null>(null);
  const fileInputs = useRef<Record<SlotKey, HTMLInputElement | null>>({
    cover: null,
    outro_continue: null,
    outro_final: null,
  });

  const base = `/api/series/${encodeURIComponent(seriesId)}/branding`;

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(base);
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      setData((await res.json()) as SeriesBrandingResponse);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (open && data === null && !loading) {
      void load();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // 卸载时释放预览图对象 URL。
  useEffect(() => {
    return () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    };
  }, [previewUrl]);

  const patchData = (patch: Partial<SeriesBrandingResponse>) => {
    if (!data) return;
    setData({ ...data, ...patch });
    setDirty(true);
  };

  const patchCoverText = (patch: Partial<CoverTextSpec>) => {
    if (!data) return;
    setData({ ...data, cover_text: { ...data.cover_text, ...patch } });
    setDirty(true);
  };

  /** 上传图片：FileReader 转 Base64（带 data: 前缀）后走 JSON 接口。 */
  const handleFile = async (slot: SlotKey, file: File | undefined) => {
    if (!file || !data) return;
    if (!/^image\/(png|jpe?g|webp)$/.test(file.type)) {
      setError('仅支持 PNG / JPEG / WebP 图片');
      return;
    }
    setUploadingSlot(slot);
    setError(null);
    try {
      const contentBase64 = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result));
        reader.onerror = () => reject(new Error('文件读取失败'));
        reader.readAsDataURL(file);
      });
      const res = await fetch(`${base}/upload`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ slot, content_base64: contentBase64, filename: file.name }),
      });
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`;
        try {
          const payload = await res.json();
          if (typeof payload?.detail === 'string') detail = payload.detail;
        } catch {
          /* 非 JSON 响应保留状态码 */
        }
        throw new Error(detail);
      }
      setData((await res.json()) as SeriesBrandingResponse);
      setDirty(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setUploadingSlot(null);
      const input = fileInputs.current[slot];
      if (input) input.value = '';
    }
  };

  const handleClear = async (slot: SlotKey) => {
    if (!data) return;
    setError(null);
    try {
      const res = await fetch(`${base}/${slot}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      setData((await res.json()) as SeriesBrandingResponse);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  /** 拖拽/点击封页预览盒：把指针位置归一化成 0.05~0.95 的坐标。 */
  const updateAnchorFromPointer = (clientX: number, clientY: number) => {
    const box = previewBoxRef.current;
    if (!box) return;
    const rect = box.getBoundingClientRect();
    const x = Math.min(0.95, Math.max(0.05, (clientX - rect.left) / rect.width));
    const y = Math.min(0.95, Math.max(0.05, (clientY - rect.top) / rect.height));
    patchCoverText({ x_ratio: Number(x.toFixed(3)), y_ratio: Number(y.toFixed(3)) });
  };

  const save = async () => {
    if (!data) return;
    setSaving(true);
    setError(null);
    try {
      const body = {
        cover_text: data.cover_text,
        cover_duration_ms: data.cover_duration_ms,
        cover_fade_in_ms: data.cover_fade_in_ms,
        cover_fade_out_ms: data.cover_fade_out_ms,
        outro_duration_ms: data.outro_duration_ms,
        outro_fade_in_ms: data.outro_fade_in_ms,
        outro_fade_out_ms: data.outro_fade_out_ms,
        default_outro_mode: data.default_outro_mode,
      };
      const res = await fetch(base, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`;
        try {
          const payload = await res.json();
          if (typeof payload?.detail === 'string') detail = payload.detail;
        } catch {
          /* 非 JSON 响应保留状态码 */
        }
        throw new Error(detail);
      }
      setData((await res.json()) as SeriesBrandingResponse);
      setDirty(false);
      setSavedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  /** 调后端 Pillow 真实渲染，返回 blob 在内联预览区展示。 */
  const requestBackendPreview = async () => {
    if (!data) return;
    setPreviewLoading(true);
    setError(null);
    try {
      const res = await fetch(`${base}/cover-preview`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          episode_no: episodeNo,
          cover_text: data.cover_text,
        }),
      });
      if (!res.ok) {
        let detail = `${res.status} ${res.statusText}`;
        try {
          const payload = await res.json();
          if (typeof payload?.detail === 'string') detail = payload.detail;
        } catch {
          /* 非 JSON 响应保留状态码 */
        }
        throw new Error(detail);
      }
      const blob = await res.blob();
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      setPreviewUrl(URL.createObjectURL(blob));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPreviewLoading(false);
    }
  };

  const coverText = data?.cover_text;
  const previewLabel = coverText
    ? formatCoverLabel(coverText.template, episodeNo, coverText.use_chinese_numeral)
    : '';

  return (
    <section className="rounded-xl border border-slate-200 bg-white overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="w-full flex items-center justify-between gap-3 px-5 py-3.5 text-left hover:bg-slate-50 transition-colors cursor-pointer"
        aria-expanded={open}
      >
        <div className="flex items-center gap-2.5 min-w-0">
          <ImageIcon className="w-4 h-4 text-blue-600 shrink-0" />
          <div className="min-w-0">
            <div className="text-sm font-semibold text-slate-900">作品封页与结尾片包</div>
            <div className="text-xs text-slate-500 mt-0.5">
              通用封页自动写话次 · 待续/完结双结尾图 · 淡入淡出转场时长
            </div>
          </div>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          {savedAt && !dirty && <span className="text-xs text-emerald-600">已保存 {savedAt}</span>}
          {dirty && <span className="text-xs text-amber-600">有未保存修改</span>}
          <ChevronDown
            className={`w-4 h-4 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`}
          />
        </div>
      </button>

      {open && (
        <div className="border-t border-slate-200 px-5 py-4 space-y-5">
          {loading && (
            <div className="flex items-center gap-2 text-sm text-slate-500 py-6 justify-center">
              <Loader2 className="w-4 h-4 animate-spin" />
              正在加载片包配置…
            </div>
          )}

          {error && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              {error}
            </div>
          )}

          {data && !loading && (
            <>
              {/* ---- 三图上传区 ---- */}
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                {(Object.keys(SLOT_META) as SlotKey[]).map((slot) => {
                  const meta = SLOT_META[slot];
                  const url = data[meta.urlField] as string | null;
                  const busy = uploadingSlot === slot;
                  return (
                    <div
                      key={slot}
                      className="rounded-lg border border-slate-200 bg-slate-50/60 p-3 space-y-2"
                    >
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="text-xs font-semibold text-slate-800">{meta.title}</span>
                        <span className="text-[11px] text-slate-400">{meta.hint}</span>
                      </div>
                      <div className="aspect-[9/16] max-h-44 w-full rounded-md border border-slate-200 bg-white overflow-hidden flex items-center justify-center">
                        {url ? (
                          <img src={url} alt={meta.title} className="w-full h-full object-contain" />
                        ) : (
                          <span className="text-[11px] text-slate-400 px-2 text-center">
                            未上传（PNG / JPEG / WebP）
                          </span>
                        )}
                      </div>
                      <input
                        ref={(el) => {
                          fileInputs.current[slot] = el;
                        }}
                        type="file"
                        accept="image/png,image/jpeg,image/webp"
                        className="hidden"
                        onChange={(e) => void handleFile(slot, e.target.files?.[0])}
                      />
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => fileInputs.current[slot]?.click()}
                          className="inline-flex flex-1 items-center justify-center gap-1.5 px-2.5 py-1.5 text-[11px] font-medium text-blue-700 bg-blue-50 rounded-md hover:bg-blue-100 transition-colors disabled:opacity-50 cursor-pointer"
                        >
                          {busy ? (
                            <Loader2 className="w-3 h-3 animate-spin" />
                          ) : (
                            <Upload className="w-3 h-3" />
                          )}
                          {url ? '重新上传' : '上传图片'}
                        </button>
                        {url && (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => void handleClear(slot)}
                            className="inline-flex items-center justify-center p-1.5 text-slate-400 hover:text-red-600 transition-colors disabled:opacity-50 cursor-pointer"
                            aria-label={`清除${meta.title}`}
                            title="清除图片"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>

              {/* ---- 封页写字可视化编辑器 ---- */}
              <div className="rounded-lg border border-slate-200 p-4 space-y-4">
                <div className="flex items-center justify-between gap-3">
                  <h4 className="text-sm font-semibold text-slate-900">封页话次文字</h4>
                  <label className="inline-flex items-center gap-2 text-xs text-slate-600 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={coverText?.enabled ?? true}
                      onChange={(e) => patchCoverText({ enabled: e.target.checked })}
                      className="accent-blue-600 cursor-pointer"
                    />
                    在封面自动写话次文字
                  </label>
                </div>

                <div className="grid grid-cols-1 lg:grid-cols-[240px_minmax(0,1fr)] gap-5">
                  {/* 9:16 交互预览盒 */}
                  <div className="space-y-2">
                    <div
                      ref={previewBoxRef}
                      onPointerDown={(e) => {
                        if (!coverText?.enabled) return;
                        setDragging(true);
                        e.currentTarget.setPointerCapture(e.pointerId);
                        updateAnchorFromPointer(e.clientX, e.clientY);
                      }}
                      onPointerMove={(e) => {
                        if (dragging) updateAnchorFromPointer(e.clientX, e.clientY);
                      }}
                      onPointerUp={() => setDragging(false)}
                      onPointerCancel={() => setDragging(false)}
                      className={`relative aspect-[9/16] w-full max-w-[240px] rounded-lg border border-slate-200 bg-slate-100 overflow-hidden ${
                        coverText?.enabled ? 'cursor-crosshair touch-none' : 'opacity-60'
                      }`}
                    >
                      {data.cover_url ? (
                        <img
                          src={data.cover_url}
                          alt="封页底图"
                          className="absolute inset-0 w-full h-full object-contain pointer-events-none"
                          draggable={false}
                        />
                      ) : (
                        <div className="absolute inset-0 flex items-center justify-center p-3">
                          <span className="text-[11px] text-slate-400 text-center">
                            上传通用封页底图后在此预览
                          </span>
                        </div>
                      )}
                      {coverText?.enabled && (
                        <span
                          className="absolute whitespace-nowrap select-none pointer-events-none"
                          style={{
                            left: `${coverText.x_ratio * 100}%`,
                            top: `${coverText.y_ratio * 100}%`,
                            transform: 'translate(-50%, -50%)',
                            fontSize: `${Math.max(10, coverText.font_size * (240 / 1080))}px`,
                            color: coverText.color,
                            WebkitTextStroke:
                              coverText.stroke_width > 0
                                ? `${Math.max(0.5, coverText.stroke_width * (240 / 1080))}px ${coverText.stroke_color}`
                                : undefined,
                          }}
                        >
                          {previewLabel}
                        </span>
                      )}
                    </div>
                    <p className="text-[11px] text-slate-400">
                      点击或拖拽移动文字位置，与滑块双向同步（坐标夹在 5%~95%）。
                    </p>
                    <div className="flex items-center gap-2">
                      <label className="text-[11px] text-slate-500">预览话次</label>
                      <input
                        type="number"
                        min={1}
                        max={9999}
                        value={episodeNo}
                        onChange={(e) => setEpisodeNo(Math.max(1, Number(e.target.value) || 1))}
                        className="w-20 rounded-md border border-slate-300 px-2 py-1 text-xs"
                      />
                      <button
                        type="button"
                        disabled={previewLoading || !data.cover_url}
                        onClick={() => void requestBackendPreview()}
                        className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-[11px] font-medium text-white bg-slate-700 rounded-md hover:bg-slate-800 transition-colors disabled:opacity-50 cursor-pointer"
                        title={data.cover_url ? '调用后端 Pillow 真实渲染' : '请先上传封页底图'}
                      >
                        {previewLoading ? (
                          <Loader2 className="w-3 h-3 animate-spin" />
                        ) : (
                          <Eye className="w-3 h-3" />
                        )}
                        后端真实渲染校验
                      </button>
                    </div>
                    {previewUrl && (
                      <img
                        src={previewUrl}
                        alt="后端真实渲染预览"
                        className="w-full max-w-[240px] rounded-lg border border-slate-200"
                      />
                    )}
                  </div>

                  {/* 排版控件 */}
                  <div className="space-y-3 min-w-0">
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                      <label className="block">
                        <span className="flex items-center justify-between text-xs text-slate-500 mb-1">
                          <span>X 位置</span>
                          <span className="font-mono-tabular text-slate-700">
                            {Math.round((coverText?.x_ratio ?? 0.5) * 100)}%
                          </span>
                        </span>
                        <input
                          type="range"
                          min={0.05}
                          max={0.95}
                          step={0.01}
                          value={coverText?.x_ratio ?? 0.5}
                          onChange={(e) => patchCoverText({ x_ratio: Number(e.target.value) })}
                          className="w-full accent-blue-600 cursor-pointer"
                          aria-label="文字横坐标"
                        />
                      </label>
                      <label className="block">
                        <span className="flex items-center justify-between text-xs text-slate-500 mb-1">
                          <span>Y 位置</span>
                          <span className="font-mono-tabular text-slate-700">
                            {Math.round((coverText?.y_ratio ?? 0.78) * 100)}%
                          </span>
                        </span>
                        <input
                          type="range"
                          min={0.05}
                          max={0.95}
                          step={0.01}
                          value={coverText?.y_ratio ?? 0.78}
                          onChange={(e) => patchCoverText({ y_ratio: Number(e.target.value) })}
                          className="w-full accent-blue-600 cursor-pointer"
                          aria-label="文字纵坐标"
                        />
                      </label>
                    </div>

                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                      <label className="block">
                        <span className="flex items-center justify-between text-xs text-slate-500 mb-1">
                          <span>字号</span>
                          <span className="font-mono-tabular text-slate-700">
                            {coverText?.font_size ?? 72}px
                          </span>
                        </span>
                        <input
                          type="range"
                          min={24}
                          max={160}
                          step={1}
                          value={coverText?.font_size ?? 72}
                          onChange={(e) => patchCoverText({ font_size: Number(e.target.value) })}
                          className="w-full accent-blue-600 cursor-pointer"
                          aria-label="字号"
                        />
                      </label>
                      <label className="block">
                        <span className="flex items-center justify-between text-xs text-slate-500 mb-1">
                          <span>描边宽度</span>
                          <span className="font-mono-tabular text-slate-700">
                            {coverText?.stroke_width ?? 0}px
                          </span>
                        </span>
                        <input
                          type="range"
                          min={0}
                          max={16}
                          step={1}
                          value={coverText?.stroke_width ?? 0}
                          onChange={(e) => patchCoverText({ stroke_width: Number(e.target.value) })}
                          className="w-full accent-blue-600 cursor-pointer"
                          aria-label="描边宽度"
                        />
                      </label>
                    </div>

                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                      <label className="block">
                        <span className="block text-xs text-slate-500 mb-1">文案模板</span>
                        <input
                          type="text"
                          value={coverText?.template ?? ''}
                          onChange={(e) => patchCoverText({ template: e.target.value })}
                          placeholder="第{ep}话"
                          className="w-full rounded-md border border-slate-300 px-2.5 py-1.5 text-xs"
                        />
                        <span className="block text-[11px] text-slate-400 mt-1">
                          占位符：{'{ep}'} / {'{ep_no}'}
                        </span>
                      </label>
                      <label className="block">
                        <span className="block text-xs text-slate-500 mb-1">文字颜色</span>
                        <input
                          type="color"
                          value={coverText?.color ?? '#FFFFFF'}
                          onChange={(e) => patchCoverText({ color: e.target.value })}
                          className="w-full h-8 rounded-md border border-slate-300 cursor-pointer bg-white"
                          aria-label="文字颜色"
                        />
                      </label>
                      <label className="block">
                        <span className="block text-xs text-slate-500 mb-1">描边颜色</span>
                        <input
                          type="color"
                          value={coverText?.stroke_color ?? '#000000'}
                          onChange={(e) => patchCoverText({ stroke_color: e.target.value })}
                          className="w-full h-8 rounded-md border border-slate-300 cursor-pointer bg-white"
                          aria-label="描边颜色"
                        />
                      </label>
                    </div>

                    <label className="inline-flex items-center gap-2 text-xs text-slate-600 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={coverText?.use_chinese_numeral ?? true}
                        onChange={(e) =>
                          patchCoverText({ use_chinese_numeral: e.target.checked })
                        }
                        className="accent-blue-600 cursor-pointer"
                      />
                      中文数字（第一话）；关闭则用阿拉伯数字（第1话）
                    </label>
                  </div>
                </div>
              </div>

              {/* ---- 时长与默认结尾策略 ---- */}
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
                <div className="rounded-lg border border-slate-200 p-4 space-y-3">
                  <h4 className="text-sm font-semibold text-slate-900">片头转场</h4>
                  <MsSlider
                    label="封面停顿总时长"
                    value={data.cover_duration_ms}
                    min={500}
                    max={8000}
                    onChange={(v) => patchData({ cover_duration_ms: v })}
                  />
                  <MsSlider
                    label="黑场淡入时长"
                    value={data.cover_fade_in_ms}
                    min={0}
                    max={2000}
                    onChange={(v) => patchData({ cover_fade_in_ms: v })}
                  />
                  <MsSlider
                    label="叠化到正文时长"
                    value={data.cover_fade_out_ms}
                    min={0}
                    max={2000}
                    onChange={(v) => patchData({ cover_fade_out_ms: v })}
                  />
                </div>
                <div className="rounded-lg border border-slate-200 p-4 space-y-3">
                  <h4 className="text-sm font-semibold text-slate-900">片尾转场</h4>
                  <MsSlider
                    label="结尾停留总时长"
                    value={data.outro_duration_ms}
                    min={500}
                    max={10000}
                    onChange={(v) => patchData({ outro_duration_ms: v })}
                  />
                  <MsSlider
                    label="结尾图浮现时长"
                    value={data.outro_fade_in_ms}
                    min={0}
                    max={2500}
                    onChange={(v) => patchData({ outro_fade_in_ms: v })}
                  />
                  <MsSlider
                    label="淡出至黑场时长"
                    value={data.outro_fade_out_ms}
                    min={0}
                    max={2500}
                    onChange={(v) => patchData({ outro_fade_out_ms: v })}
                  />
                </div>
              </div>

              <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
                <div className="space-y-1.5">
                  <span className="block text-xs text-slate-500">作品默认结尾图</span>
                  <div className="inline-flex rounded-lg border border-slate-300 p-0.5 bg-slate-50">
                    {(
                      [
                        ['continue', '待续'],
                        ['final', '完结'],
                        ['none', '不加结尾'],
                      ] as Array<[SeriesDefaultOutroMode, string]>
                    ).map(([mode, label]) => (
                      <button
                        key={mode}
                        type="button"
                        onClick={() => patchData({ default_outro_mode: mode })}
                        className={`px-3 py-1.5 text-xs rounded-md transition-colors cursor-pointer ${
                          data.default_outro_mode === mode
                            ? 'bg-blue-600 text-white font-medium'
                            : 'text-slate-600 hover:bg-slate-200'
                        }`}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </div>
                <button
                  type="button"
                  disabled={!dirty || saving}
                  onClick={() => void save()}
                  className="inline-flex items-center gap-1.5 px-4 py-2 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
                >
                  {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
                  保存片包配置
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </section>
  );
};
