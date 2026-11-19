import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Check,
  Copy,
  Loader2,
  Music,
  Trash2,
  Upload,
  X,
} from 'lucide-react';
import type { BgmLibraryResponse, BgmTrackItem } from '../types/studio';

interface BgmLibraryDrawerProps {
  seriesId: string;
  open: boolean;
  onClose: () => void;
}

const ACCEPTED_EXTENSIONS = ['.mp3', '.m4a', '.wav', '.ogg'];
const MAX_BYTES = 30 * 1024 * 1024;

/**
 * Patch v4.7 作品曲库抽屉：五档情绪说明、关键词复制、曲目试听/删除、
 * 本地上传（可绑定为某档情绪的默认曲目）。全部走 JSON+Base64，无 multipart。
 */
export default function BgmLibraryDrawer({
  seriesId,
  open,
  onClose,
}: BgmLibraryDrawerProps) {
  const [data, setData] = useState<BgmLibraryResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [uploading, setUploading] = useState(false);
  const [moodSlot, setMoodSlot] = useState('');
  const [copiedMood, setCopiedMood] = useState('');
  const [confirmDelete, setConfirmDelete] = useState('');
  const [hint, setHint] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);
  const base = `/api/series/${encodeURIComponent(seriesId)}/bgm-library`;

  const loadLibrary = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const res = await fetch(base);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `加载失败（HTTP ${res.status}）`);
      }
      setData(await res.json());
    } catch (err) {
      setError(err instanceof Error ? err.message : '曲库加载失败');
    } finally {
      setLoading(false);
    }
  }, [base]);

  useEffect(() => {
    if (open) {
      void loadLibrary();
    }
  }, [open, loadLibrary]);

  useEffect(() => {
    if (!hint) {
      return;
    }
    const timer = window.setTimeout(() => setHint(''), 2600);
    return () => window.clearTimeout(timer);
  }, [hint]);

  if (!open) {
    return null;
  }

  const handleCopy = async (mood: string, keywords: string[]) => {
    const text = keywords.join(' ');
    try {
      await navigator.clipboard.writeText(text);
      setCopiedMood(mood);
      window.setTimeout(() => setCopiedMood(''), 1800);
    } catch {
      setHint('浏览器拒绝了剪贴板访问，请手动选择关键词复制');
    }
  };

  const handleUpload = async (file: File) => {
    const lower = file.name.toLowerCase();
    if (!ACCEPTED_EXTENSIONS.some((ext) => lower.endsWith(ext))) {
      setError(`只支持 ${ACCEPTED_EXTENSIONS.join(' / ')} 格式`);
      return;
    }
    if (file.size > MAX_BYTES) {
      setError('音频体积超过 30MB 上限');
      return;
    }
    setUploading(true);
    setError('');
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
        body: JSON.stringify({
          filename: file.name,
          content_base64: contentBase64,
          mood_slot: moodSlot || null,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(body.detail || `上传失败（HTTP ${res.status}）`);
      }
      setData(body as BgmLibraryResponse);
      setHint(moodSlot ? `已绑定为「${moodSlot}」情绪默认曲目` : '曲目已上传到作品曲库');
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '上传失败');
    } finally {
      setUploading(false);
    }
  };

  const handleDelete = async (track: BgmTrackItem) => {
    if (confirmDelete !== track.filename) {
      setConfirmDelete(track.filename);
      window.setTimeout(() => setConfirmDelete(''), 3000);
      return;
    }
    setError('');
    try {
      const res = await fetch(
        `${base}/${encodeURIComponent(track.filename)}`,
        { method: 'DELETE' }
      );
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(body.detail || `删除失败（HTTP ${res.status}）`);
      }
      setData(body as BgmLibraryResponse);
      setConfirmDelete('');
      setHint('已删除作品曲目');
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败');
    }
  };

  const seriesTracks = data?.tracks.filter((t) => t.scope === 'series') ?? [];
  const globalTracks = data?.tracks.filter((t) => t.scope === 'global') ?? [];

  return (
    <div
      className="fixed inset-0 z-40 flex justify-end bg-slate-900/40"
      role="dialog"
      aria-modal="true"
      aria-label="BGM 曲库管理"
      onClick={onClose}
    >
      <div
        className="flex h-full w-full max-w-xl flex-col bg-white shadow-2xl"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-slate-200 px-5 py-4">
          <div className="flex items-center gap-2">
            <Music className="h-5 w-5 text-blue-600" />
            <h2 className="text-base font-semibold text-slate-800">BGM 曲库管理</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-700"
            aria-label="关闭曲库抽屉"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="flex-1 space-y-5 overflow-y-auto px-5 py-4">
          {error && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              {error}
            </div>
          )}
          {hint && (
            <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
              {hint}
            </div>
          )}

          <section>
            <h3 className="mb-2 text-sm font-semibold text-slate-800">五档情绪与搜歌关键词</h3>
            <div className="space-y-2">
              {loading && !data && (
                <div className="flex items-center gap-2 text-xs text-slate-500">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  正在加载曲库…
                </div>
              )}
              {(data?.moods ?? []).map((preset) => (
                <div
                  key={preset.mood}
                  className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm font-medium text-slate-800">
                      {preset.label}
                      <span className="ml-2 font-mono text-[11px] text-slate-400">
                        {preset.mood}
                      </span>
                    </span>
                    <button
                      type="button"
                      onClick={() => void handleCopy(preset.mood, preset.search_keywords)}
                      className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-100"
                    >
                      {copiedMood === preset.mood ? (
                        <>
                          <Check className="h-3.5 w-3.5 text-emerald-600" />
                          已复制
                        </>
                      ) : (
                        <>
                          <Copy className="h-3.5 w-3.5" />
                          复制关键词
                        </>
                      )}
                    </button>
                  </div>
                  <p className="mt-1 text-xs leading-5 text-slate-600">{preset.description}</p>
                  <p className="mt-1 break-words font-mono text-[11px] text-blue-700">
                    {preset.search_keywords.join(' ')}
                  </p>
                </div>
              ))}
            </div>
          </section>

          <section>
            <h3 className="mb-2 text-sm font-semibold text-slate-800">上传作品曲目</h3>
            <div className="flex flex-wrap items-center gap-2">
              <select
                value={moodSlot}
                onChange={(event) => setMoodSlot(event.target.value)}
                className="rounded-lg border border-slate-300 px-2.5 py-2 text-xs text-slate-700"
              >
                <option value="">保留原文件名（不绑定情绪）</option>
                {(data?.moods ?? []).map((preset) => (
                  <option key={preset.mood} value={preset.mood}>
                    绑定为 {preset.label} 默认曲（{preset.mood}）
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
                className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-2 text-xs font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-blue-300"
              >
                {uploading ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Upload className="h-4 w-4" />
                )}
                {uploading ? '上传中…' : '选择音频上传'}
              </button>
              <input
                ref={fileInputRef}
                type="file"
                accept=".mp3,.m4a,.wav,.ogg,audio/mpeg,audio/mp4,audio/wav,audio/ogg"
                className="hidden"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) {
                    void handleUpload(file);
                  }
                }}
              />
            </div>
            <p className="mt-1.5 text-[11px] text-slate-500">
              支持 mp3 / m4a / wav / ogg，单文件不超过 30MB；绑定情绪后按「情绪键.后缀」命名，自动匹配对应幕。
            </p>
          </section>

          <section className="space-y-3">
            <div>
              <h3 className="mb-2 text-sm font-semibold text-slate-800">
                作品专属曲目（{seriesTracks.length}）
              </h3>
              {seriesTracks.length === 0 && (
                <p className="rounded-lg border border-dashed border-slate-300 px-3 py-3 text-xs text-slate-500">
                  还没有作品专属曲目，上传后只供本作品使用并可删除。
                </p>
              )}
              <div className="space-y-2">
                {seriesTracks.map((track) => (
                  <TrackRow
                    key={`${track.scope}-${track.filename}`}
                    track={track}
                    deleting={confirmDelete === track.filename}
                    onDelete={() => void handleDelete(track)}
                  />
                ))}
              </div>
            </div>
            <div>
              <h3 className="mb-2 text-sm font-semibold text-slate-800">
                全局曲库（{globalTracks.length}，所有作品共享、不可在此删除）
              </h3>
              <div className="space-y-2">
                {globalTracks.map((track) => (
                  <TrackRow
                    key={`${track.scope}-${track.filename}`}
                    track={track}
                    deleting={false}
                    onDelete={null}
                  />
                ))}
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}

function TrackRow({
  track,
  deleting,
  onDelete,
}: {
  track: BgmTrackItem;
  deleting: boolean;
  onDelete: (() => void) | null;
}) {
  return (
    <div className="rounded-lg border border-slate-200 px-3 py-2">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-slate-800">{track.filename}</p>
          <p className="text-[11px] text-slate-500">
            {track.scope === 'series' ? '作品专属' : '全局'} ·{' '}
            {(track.size_bytes / 1024 / 1024).toFixed(2)} MB
          </p>
        </div>
        {onDelete && (
          <button
            type="button"
            onClick={onDelete}
            className={`inline-flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-[11px] ${
              deleting
                ? 'bg-red-600 text-white hover:bg-red-700'
                : 'border border-red-200 text-red-600 hover:bg-red-50'
            }`}
          >
            <Trash2 className="h-3.5 w-3.5" />
            {deleting ? '确认删除？' : '删除'}
          </button>
        )}
      </div>
      <audio controls preload="none" src={track.url} className="mt-2 h-9 w-full" />
    </div>
  );
}
