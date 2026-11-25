import { useCallback, useMemo, useRef, useState } from 'react';
import {
  Check,
  Loader2,
  Pencil,
  Plus,
  RotateCcw,
  Search,
  Trash2,
  Upload,
  Volume2,
  X,
} from 'lucide-react';
import type { SfxGroupCategory, SfxLibraryItem, SfxLibraryResponse } from '../types/studio';
import {
  createSfxAsset,
  deleteSfxAsset,
  restoreSfxAsset,
  updateSfxAsset,
  uploadSfxAsset,
} from '../api/studioApi';

interface SfxLibraryViewProps {
  /** 当前生效曲库（由 App 持有）。 */
  library: SfxLibraryResponse | null;
  /** 增删改成功后回传后端返回的最新曲库，App 据此同步给审核台与标签浮层。 */
  onLibraryChange: (next: SfxLibraryResponse) => void;
}

const GROUPS: SfxGroupCategory[] = ['动作', '环境', '转场', '道具'];

const SOURCE_LABELS: Record<string, string> = {
  freesound_cc0: 'Freesound CC0 采编',
  builtin_cc0: '仓库内置 CC0',
  custom_upload: '用户上传',
  custom: '自定义（未上传音频）',
};

/** 新增自定义音效时的表单初值。 */
const EMPTY_DRAFT = {
  key: '',
  label: '',
  group: '动作' as SfxGroupCategory,
  category: '',
  fallback_key: '',
};

/**
 * Patch v4.43：全局音效资源库。
 *
 * 之前音效只能在单话审核台里逐句选，素材也只有仓库内置的那一套。
 * 这里把「音效」提升成**全局资源**：新增自定义音效、改中文名 / 分类、
 * 上传替换音频、删除自定义条目、把内置条目停用或还原默认。
 *
 * 铁律：
 * - 音效的唯一入口是送音文本里的 `[sfx:key]` 标签（本页只管曲库，不管落点）；
 * - 自定义素材的音频落在 `assets/sfx/custom/<key>.<ext>`，与仓库默认素材分开，
 *   删除 / 还原时互不牵连；
 * - 停用内置条目只是从生效曲库里摘掉，仓库里的默认音频始终保留，可随时还原。
 */
export const SfxLibraryView: React.FC<SfxLibraryViewProps> = ({
  library,
  onLibraryChange,
}) => {
  const [groupFilter, setGroupFilter] = useState<string>('全部');
  const [keyword, setKeyword] = useState('');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [showCreate, setShowCreate] = useState(false);
  const [draft, setDraft] = useState(EMPTY_DRAFT);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editMeta, setEditMeta] = useState({ label: '', group: '动作', category: '', fallback_key: '' });
  const [uploadTarget, setUploadTarget] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const pendingUploadKeyRef = useRef<string>('');

  const items = useMemo(() => library?.items ?? [], [library]);
  const hiddenKeys = useMemo(() => library?.hidden_builtin_keys ?? [], [library]);

  const counts = useMemo(() => {
    const map = new Map<string, number>();
    for (const item of items) {
      map.set(item.group ?? '其他', (map.get(item.group ?? '其他') ?? 0) + 1);
    }
    return map;
  }, [items]);

  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    return items.filter((item) => {
      if (groupFilter !== '全部' && (item.group ?? '') !== groupFilter) return false;
      if (!kw) return true;
      return (
        item.key.toLowerCase().includes(kw) ||
        item.label.toLowerCase().includes(kw) ||
        (item.category ?? '').toLowerCase().includes(kw)
      );
    });
  }, [items, groupFilter, keyword]);

  const run = useCallback(
    async (action: () => Promise<SfxLibraryResponse>, okText: string) => {
      setBusy(true);
      setError('');
      setNotice('');
      try {
        const next = await action();
        onLibraryChange(next);
        setNotice(okText);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    },
    [onLibraryChange]
  );

  const preview = useCallback((sfxKey: string) => {
    if (!sfxKey) return;
    const audio = new Audio(`/api/sfx-file/${encodeURIComponent(sfxKey)}`);
    audio.play().catch(() => {
      /* 浏览器自动播放策略拦截时静默忽略 */
    });
  }, []);

  const handleCreate = useCallback(
    (file: File | null) => {
      const key = draft.key.trim().toLowerCase();
      const label = draft.label.trim();
      if (!key || !label) {
        setError('音效键与中文名都要填。');
        return;
      }
      if (!/^[a-z][a-z0-9_]{1,39}$/.test(key)) {
        setError('音效键只能是小写字母开头的小写字母 / 数字 / 下划线，长度 2~40。');
        return;
      }
      const submit = async (contentBase64: string, filename: string) => {
        await run(
          () =>
            createSfxAsset({
              key,
              label,
              group: draft.group,
              category: draft.category.trim() || draft.group,
              fallback_key: draft.fallback_key.trim(),
              filename,
              content_base64: contentBase64,
            }),
          `已新增音效 ${label}（${key}）。`
        );
        setShowCreate(false);
        setDraft(EMPTY_DRAFT);
      };
      if (!file) {
        void submit('', '');
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        void submit(String(reader.result ?? ''), file.name);
      };
      reader.onerror = () => setError('文件读取失败。');
      reader.readAsDataURL(file);
    },
    [draft, run]
  );

  const handleUpload = useCallback(
    (sfxKey: string, file: File) => {
      const reader = new FileReader();
      reader.onload = () => {
        void run(
          () => uploadSfxAsset(sfxKey, file.name, String(reader.result ?? '')),
          `已替换 ${sfxKey} 的音频。`
        );
      };
      reader.onerror = () => setError('文件读取失败。');
      reader.readAsDataURL(file);
    },
    [run]
  );

  const handleDelete = useCallback(
    (item: SfxLibraryItem) => {
      if (item.is_builtin === false) {
        void run(
          () => deleteSfxAsset(item.key),
          `已删除自定义音效 ${item.label}（${item.key}）。`
        );
        return;
      }
      // 内置条目：先还原覆盖的音频（若用户换过），再停用它。
      void run(
        async () => {
          let next = library ?? null;
          if (item.has_custom_audio) {
            next = await deleteSfxAsset(item.key, 'restore_audio');
          }
          return (await deleteSfxAsset(item.key, 'disable')) ?? next ?? (library as SfxLibraryResponse);
        },
        `已停用内置音效 ${item.label}（${item.key}），仓库默认素材仍然保留，可随时还原。`
      );
    },
    [library, run]
  );

  const handleRestore = useCallback(
    (sfxKey: string) => {
      void run(() => restoreSfxAsset(sfxKey), `已还原音效 ${sfxKey}。`);
    },
    [run]
  );

  const handleSaveMeta = useCallback(
    (sfxKey: string) => {
      void run(
        () =>
          updateSfxAsset(sfxKey, {
            label: editMeta.label.trim(),
            group: editMeta.group,
            category: editMeta.category.trim(),
            fallback_key: editMeta.fallback_key.trim(),
          }),
        `已更新 ${sfxKey} 的信息。`
      );
      setEditingKey(null);
    },
    [editMeta, run]
  );

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold text-slate-900">音效资源库</h2>
          <p className="mt-1 text-xs text-slate-500 leading-relaxed">
            全局音效曲库：内置 {items.filter((i) => i.is_builtin !== false).length} 条 + 自定义{' '}
            {items.filter((i) => i.is_builtin === false).length} 条。音效在成片里的落点由送音文本里的{' '}
            <code className="px-1 rounded bg-slate-100 text-slate-700">[sfx:key]</code> 标签决定，本页只管曲库。
          </p>
        </div>
        <button
          type="button"
          onClick={() => {
            setShowCreate((v) => !v);
            setError('');
          }}
          className="shrink-0 inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-700 disabled:opacity-50"
          disabled={busy}
        >
          {showCreate ? <X className="w-3.5 h-3.5" /> : <Plus className="w-3.5 h-3.5" />}
          {showCreate ? '取消' : '新增自定义音效'}
        </button>
      </div>

      {notice && (
        <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
          {notice}
        </div>
      )}
      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {error}
        </div>
      )}

      {showCreate && (
        <div className="rounded-lg border border-slate-200 bg-slate-50 p-3 space-y-2">
          <div className="grid grid-cols-2 gap-2">
            <label className="text-[11px] text-slate-600 space-y-1">
              <span>音效键（小写，2~40 位）</span>
              <input
                value={draft.key}
                onChange={(e) => setDraft({ ...draft, key: e.target.value })}
                placeholder="例如 knife_slice"
                className="w-full px-2 py-1 text-xs bg-white border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
              />
            </label>
            <label className="text-[11px] text-slate-600 space-y-1">
              <span>中文名</span>
              <input
                value={draft.label}
                onChange={(e) => setDraft({ ...draft, label: e.target.value })}
                placeholder="例如 刀锋划过"
                className="w-full px-2 py-1 text-xs bg-white border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
              />
            </label>
            <label className="text-[11px] text-slate-600 space-y-1">
              <span>一级大类</span>
              <select
                value={draft.group}
                onChange={(e) =>
                  setDraft({ ...draft, group: e.target.value as SfxGroupCategory })
                }
                className="w-full px-2 py-1 text-xs bg-white border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
              >
                {GROUPS.map((group) => (
                  <option key={group} value={group}>
                    {group}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-[11px] text-slate-600 space-y-1">
              <span>细分类（可空）</span>
              <input
                value={draft.category}
                onChange={(e) => setDraft({ ...draft, category: e.target.value })}
                placeholder="例如 击打"
                className="w-full px-2 py-1 text-xs bg-white border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
              />
            </label>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => handleCreate(null)}
              disabled={busy}
              className="rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            >
              仅登记（先不上传音频）
            </button>
            <label className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-700 hover:border-blue-400 cursor-pointer">
              <Upload className="w-3.5 h-3.5" />
              上传音频并新增
              <input
                type="file"
                accept=".mp3,.wav,.m4a,.ogg"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0] ?? null;
                  e.target.value = '';
                  if (file) handleCreate(file);
                }}
              />
            </label>
            <span className="text-[11px] text-slate-400">
              支持 mp3 / wav / m4a / ogg，单个 ≤ 30MB；后端会自动裁掉首尾静音并统一响度。
            </span>
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <div className="inline-flex rounded-lg border border-slate-200 bg-white p-0.5">
          {['全部', ...GROUPS].map((group) => (
            <button
              key={group}
              type="button"
              onClick={() => setGroupFilter(group)}
              className={`px-2.5 py-1 text-[11px] rounded-md transition-colors ${
                groupFilter === group
                  ? 'bg-slate-900 text-white'
                  : 'text-slate-600 hover:bg-slate-100'
              }`}
            >
              {group}
              <span className="ml-1 text-[10px] opacity-70">
                {group === '全部' ? items.length : (counts.get(group) ?? 0)}
              </span>
            </button>
          ))}
        </div>
        <div className="relative">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
          <input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="搜索音效键 / 中文名 / 分类"
            className="w-64 pl-7 pr-2 py-1 text-xs bg-white border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
          />
        </div>
        <span className="text-[11px] text-slate-400">
          命中 {filtered.length} / {items.length}
        </span>
      </div>

      <div className="rounded-lg border border-slate-200 overflow-hidden">
        <table className="w-full text-left">
          <thead>
            <tr className="bg-slate-50 text-[11px] text-slate-500">
              <th className="px-3 py-2 font-medium">音效</th>
              <th className="px-3 py-2 font-medium">分类</th>
              <th className="px-3 py-2 font-medium">时长</th>
              <th className="px-3 py-2 font-medium">来源</th>
              <th className="px-3 py-2 font-medium text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((item) => {
              const editing = editingKey === item.key;
              const usedFallback =
                item.resolved_filename !== '' &&
                item.resolved_filename.replace(/\.[^.]+$/, '') !== item.key;
              return (
                <tr key={item.key} className="border-t border-slate-100 align-top">
                  <td className="px-3 py-2">
                    {editing ? (
                      <input
                        value={editMeta.label}
                        onChange={(e) => setEditMeta({ ...editMeta, label: e.target.value })}
                        className="w-40 px-2 py-1 text-xs bg-white border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
                      />
                    ) : (
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          onClick={() => preview(item.key)}
                          disabled={!item.available}
                          title={item.available ? '试听' : '盘面暂无素材'}
                          className="shrink-0 w-6 h-6 flex items-center justify-center rounded border border-slate-200 text-slate-500 hover:border-blue-400 hover:text-blue-700 disabled:opacity-40"
                        >
                          <Volume2 className="w-3.5 h-3.5" />
                        </button>
                        <div className="min-w-0">
                          <div className="text-xs text-slate-800 font-medium">
                            {item.label}
                            {item.is_builtin === false && (
                              <span className="ml-1 rounded bg-blue-50 px-1 py-0.5 text-[10px] text-blue-700">
                                自定义
                              </span>
                            )}
                            {item.has_custom_audio && (
                              <span className="ml-1 rounded bg-amber-50 px-1 py-0.5 text-[10px] text-amber-700">
                                已换音频
                              </span>
                            )}
                            {usedFallback && (
                              <span className="ml-1 rounded bg-slate-100 px-1 py-0.5 text-[10px] text-slate-500">
                                ·通用兜底
                              </span>
                            )}
                          </div>
                          <div className="font-mono text-[10px] text-slate-400">
                            [sfx:{item.key}]
                          </div>
                        </div>
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2 text-[11px] text-slate-600">
                    {editing ? (
                      <div className="space-y-1">
                        <select
                          value={editMeta.group}
                          onChange={(e) => setEditMeta({ ...editMeta, group: e.target.value })}
                          className="w-24 px-1.5 py-1 text-[11px] bg-white border border-slate-200 rounded"
                        >
                          {GROUPS.map((group) => (
                            <option key={group} value={group}>
                              {group}
                            </option>
                          ))}
                        </select>
                        <input
                          value={editMeta.category}
                          onChange={(e) => setEditMeta({ ...editMeta, category: e.target.value })}
                          placeholder="细类"
                          className="w-24 px-1.5 py-1 text-[11px] bg-white border border-slate-200 rounded"
                        />
                      </div>
                    ) : (
                      <>
                        <div>{item.group || '其他'}</div>
                        <div className="text-slate-400">{item.category}</div>
                      </>
                    )}
                  </td>
                  <td className="px-3 py-2 text-[11px] text-slate-600 font-mono-tabular">
                    {item.duration_ms ? `${(item.duration_ms / 1000).toFixed(2)}s` : '—'}
                  </td>
                  <td className="px-3 py-2 text-[11px] text-slate-600">
                    {SOURCE_LABELS[item.source_type ?? 'builtin_cc0'] ?? item.source_type}
                    {item.cc0_source?.author && (
                      <div className="text-[10px] text-slate-400">
                        {item.cc0_source.author}
                        {item.cc0_source.freesound_url && (
                          <a
                            href={item.cc0_source.freesound_url}
                            target="_blank"
                            rel="noreferrer"
                            className="ml-1 text-blue-600 hover:underline"
                          >
                            原始素材页
                          </a>
                        )}
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex items-center justify-end gap-1">
                      {editing ? (
                        <>
                          <button
                            type="button"
                            onClick={() => handleSaveMeta(item.key)}
                            className="w-6 h-6 flex items-center justify-center rounded border border-slate-200 text-emerald-600 hover:border-emerald-400"
                            title="保存"
                          >
                            <Check className="w-3.5 h-3.5" />
                          </button>
                          <button
                            type="button"
                            onClick={() => setEditingKey(null)}
                            className="w-6 h-6 flex items-center justify-center rounded border border-slate-200 text-slate-500 hover:border-slate-400"
                            title="取消"
                          >
                            <X className="w-3.5 h-3.5" />
                          </button>
                        </>
                      ) : (
                        <>
                          <button
                            type="button"
                            onClick={() => {
                              setEditingKey(item.key);
                              setEditMeta({
                                label: item.label,
                                group: item.group || '动作',
                                category: item.category || '',
                                fallback_key: item.fallback_key || '',
                              });
                            }}
                            title="编辑信息"
                            className="w-6 h-6 flex items-center justify-center rounded border border-slate-200 text-slate-500 hover:border-blue-400 hover:text-blue-700"
                          >
                            <Pencil className="w-3.5 h-3.5" />
                          </button>
                          <button
                            type="button"
                            onClick={() => {
                              pendingUploadKeyRef.current = item.key;
                              setUploadTarget(item.key);
                              fileInputRef.current?.click();
                            }}
                            title="上传替换音频"
                            className="w-6 h-6 flex items-center justify-center rounded border border-slate-200 text-slate-500 hover:border-blue-400 hover:text-blue-700"
                          >
                            <Upload className="w-3.5 h-3.5" />
                          </button>
                          <button
                            type="button"
                            onClick={() => handleDelete(item)}
                            disabled={busy}
                            title={
                              item.is_builtin === false
                                ? '删除这个自定义音效'
                                : '停用这个内置音效（仓库默认素材仍保留）'
                            }
                            className="w-6 h-6 flex items-center justify-center rounded border border-slate-200 text-slate-500 hover:border-rose-400 hover:text-rose-600 disabled:opacity-40"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
            {filtered.length === 0 && (
              <tr>
                <td colSpan={5} className="px-3 py-6 text-center text-xs text-slate-400">
                  没有匹配的音效。
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <input
        ref={fileInputRef}
        type="file"
        accept=".mp3,.wav,.m4a,.ogg"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0] ?? null;
          const target = uploadTarget || pendingUploadKeyRef.current;
          e.target.value = '';
          setUploadTarget(null);
          if (file && target) handleUpload(target, file);
        }}
      />

      {hiddenKeys.length > 0 && (
        <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
          <div className="text-xs font-medium text-slate-700 mb-1.5">
            已停用的内置音效（{hiddenKeys.length}）
          </div>
          <div className="flex flex-wrap gap-1.5">
            {hiddenKeys.map((key) => (
              <button
                key={key}
                type="button"
                onClick={() => handleRestore(key)}
                disabled={busy}
                className="inline-flex items-center gap-1 rounded-md border border-slate-300 bg-white px-2 py-1 text-[11px] text-slate-600 hover:border-blue-400 hover:text-blue-700 disabled:opacity-50"
              >
                <RotateCcw className="w-3 h-3" />
                {key}
              </button>
            ))}
          </div>
        </div>
      )}

      {busy && (
        <div className="flex items-center gap-1.5 text-[11px] text-slate-500">
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
          处理中…
        </div>
      )}
    </div>
  );
};
