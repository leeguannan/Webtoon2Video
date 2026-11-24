import React, { useEffect, useMemo, useState } from 'react';
import {
  BookMarked,
  CheckCircle2,
  GitBranch,
  History,
  Pencil,
  Plus,
  Trash2,
  Users,
} from 'lucide-react';
import type {
  CharacterProfile,
  CharacterSaveOptions,
  PlotThread,
  SeriesItem,
} from '../types/studio';

interface MemoryViewProps {
  series: SeriesItem;
  onAddCharacter: (seriesId: string, char: CharacterProfile) => void;
  /**
   * Patch v4.26：人物实体图谱可编辑（改名 / 改字段 / 合并 / 删除）。
   *
   * `options.character_renames` 让后端把专属音色键迁到新主名；
   * `options.deleted_character_names` 让后端把该角色从按话快照里剔除。
   */
  onSaveCharacters: (
    seriesId: string,
    characters: CharacterProfile[],
    options?: CharacterSaveOptions
  ) => void;
  onAddPlotThread: (seriesId: string, thread: Omit<PlotThread, 'thread_id'>) => void;
  onToggleThreadStatus: (seriesId: string, threadId: string) => void;
  onUpdateStyle: (seriesId: string, style: string) => void;
}

type Gender = 'male' | 'female' | 'unknown';

interface CharacterDraft {
  name: string;
  gender: Gender;
  aliases: string;
  first_seen_ep: number;
  last_seen_ep: number;
  visual_traits: string;
  current_status: string;
  /** 改名时是否把原主名保留为别名（默认不勾选）。 */
  keepOldNameAsAlias: boolean;
  /** 合并到哪个角色（空 = 不合并）。 */
  mergeInto: string;
  /** 合并后是否把原角色收为别名（默认不勾选）。 */
  mergeKeepAsAlias: boolean;
}

const GENDER_LABELS: Record<Gender, string> = {
  male: '男',
  female: '女',
  unknown: '未标注',
};

const GENDER_BADGE_CLASS: Record<Gender, string> = {
  male: 'bg-sky-100 text-sky-700 border-sky-200',
  female: 'bg-pink-100 text-pink-700 border-pink-200',
  unknown: 'bg-slate-100 text-slate-500 border-slate-200',
};

function toDraft(ch: CharacterProfile): CharacterDraft {
  return {
    name: ch.name,
    gender: ch.gender ?? 'unknown',
    aliases: ch.aliases.join('、'),
    first_seen_ep: ch.first_seen_ep,
    last_seen_ep: ch.last_seen_ep,
    visual_traits: ch.visual_traits,
    current_status: ch.current_status,
    keepOldNameAsAlias: false,
    mergeInto: '',
    mergeKeepAsAlias: false,
  };
}

function splitAliases(raw: string): string[] {
  return raw
    .split(/[,，、]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export const MemoryView: React.FC<MemoryViewProps> = ({
  series,
  onAddCharacter,
  onSaveCharacters,
  onAddPlotThread,
  onToggleThreadStatus,
  onUpdateStyle,
}) => {
  const { story_bible } = series;

  const [styleDraft, setStyleDraft] = useState(story_bible.narration_style);
  const [charName, setCharName] = useState('');
  const [charAliases, setCharAliases] = useState('');
  const [charTraits, setCharTraits] = useState('');
  const [charStatus, setCharStatus] = useState('');
  const [charGender, setCharGender] = useState<Gender>('unknown');
  const [charEp, setCharEp] = useState(1);

  const [threadTitle, setThreadTitle] = useState('');
  const [threadDesc, setThreadDesc] = useState('');
  const [threadEp, setThreadEp] = useState(1);

  // Patch v4.26：行内编辑态
  const [editingName, setEditingName] = useState<string | null>(null);
  const [draft, setDraft] = useState<CharacterDraft | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [formError, setFormError] = useState('');

  // 切换作品或叙事风格更新时，同步风格草稿并收起编辑态。
  useEffect(() => {
    setStyleDraft(story_bible.narration_style);
    setEditingName(null);
    setDraft(null);
    setConfirmDelete(null);
  }, [series.series_id, story_bible.narration_style]);

  const characters = story_bible.characters;
  const nameTaken = useMemo(
    () => characters.some((c) => c.name === charName.trim()),
    [characters, charName]
  );

  const handleAddChar = (e: React.FormEvent) => {
    e.preventDefault();
    if (!charName.trim()) return;
    if (nameTaken) {
      setFormError(`角色主名「${charName.trim()}」已存在，请换一个名字`);
      return;
    }
    setFormError('');
    onAddCharacter(series.series_id, {
      name: charName.trim(),
      aliases: splitAliases(charAliases),
      visual_traits: charTraits.trim() || '待百炼视觉抽取补充',
      current_status: charStatus.trim() || '初次登场',
      first_seen_ep: charEp,
      last_seen_ep: charEp,
      gender: charGender,
    });
    setCharName('');
    setCharAliases('');
    setCharTraits('');
    setCharStatus('');
    setCharGender('unknown');
    setCharEp(1);
  };

  const startEdit = (ch: CharacterProfile) => {
    setEditingName(ch.name);
    setDraft(toDraft(ch));
    setConfirmDelete(null);
    setFormError('');
  };

  const cancelEdit = () => {
    setEditingName(null);
    setDraft(null);
    setFormError('');
  };

  const handleSaveCharacter = () => {
    if (!editingName || !draft) return;
    const original = characters.find((c) => c.name === editingName);
    if (!original) {
      cancelEdit();
      return;
    }
    const newName = draft.name.trim();
    if (!newName) {
      setFormError('角色主名不能为空');
      return;
    }
    if (
      newName !== original.name &&
      characters.some((c) => c.name === newName)
    ) {
      setFormError(`角色主名「${newName}」已被占用`);
      return;
    }
    if (draft.first_seen_ep < 1) {
      setFormError('首现话次必须 ≥ 1');
      return;
    }
    if (draft.last_seen_ep < draft.first_seen_ep) {
      setFormError('最近出场话次不能早于首现话次');
      return;
    }

    const options: CharacterSaveOptions = {};
    const nextCharacters: CharacterProfile[] = [];

    const mergeTarget = draft.mergeInto
      ? characters.find((c) => c.name === draft.mergeInto)
      : undefined;

    for (const ch of characters) {
      if (ch.name === original.name) {
        if (mergeTarget) {
          // 合并：原角色退出档案，其信息并入目标角色。
          continue;
        }
        const aliases = splitAliases(draft.aliases);
        if (newName !== original.name && draft.keepOldNameAsAlias) {
          if (!aliases.includes(original.name)) aliases.push(original.name);
        }
        nextCharacters.push({
          name: newName,
          aliases,
          visual_traits: draft.visual_traits.trim(),
          current_status: draft.current_status.trim(),
          first_seen_ep: draft.first_seen_ep,
          last_seen_ep: draft.last_seen_ep,
          gender: draft.gender,
        });
        continue;
      }
      if (mergeTarget && ch.name === mergeTarget.name) {
        const mergedAliases = [...ch.aliases];
        if (draft.mergeKeepAsAlias && !mergedAliases.includes(original.name)) {
          mergedAliases.push(original.name);
        }
        for (const alias of original.aliases) {
          if (!mergedAliases.includes(alias)) mergedAliases.push(alias);
        }
        nextCharacters.push({
          ...ch,
          aliases: mergedAliases,
          first_seen_ep: Math.min(ch.first_seen_ep, draft.first_seen_ep),
          last_seen_ep: Math.max(ch.last_seen_ep, draft.last_seen_ep),
        });
        continue;
      }
      nextCharacters.push(ch);
    }

    if (newName !== original.name && !mergeTarget) {
      options.character_renames = [{ from_name: original.name, to_name: newName }];
    }
    if (mergeTarget) {
      options.deleted_character_names = [original.name];
    }

    onSaveCharacters(series.series_id, nextCharacters, options);
    cancelEdit();
  };

  const handleDeleteCharacter = (name: string) => {
    const nextCharacters = characters.filter((c) => c.name !== name);
    onSaveCharacters(series.series_id, nextCharacters, {
      deleted_character_names: [name],
    });
    setConfirmDelete(null);
    if (editingName === name) cancelEdit();
  };

  const handleAddThread = (e: React.FormEvent) => {
    e.preventDefault();
    if (!threadTitle.trim()) return;
    onAddPlotThread(series.series_id, {
      title: threadTitle.trim(),
      description: threadDesc.trim() || threadTitle.trim(),
      introduced_ep: threadEp,
      status: 'open',
    });
    setThreadTitle('');
    setThreadDesc('');
  };

  const openThreads = story_bible.plot_threads.filter((t) => t.status === 'open');
  const resolvedThreads = story_bible.plot_threads.filter((t) => t.status === 'resolved');

  return (
    <div className="space-y-6">
      {/* Architecture Overview & Snapshot Time-Travel Banner */}
      <section className="bg-white border border-slate-200 rounded-xl p-5 flex flex-col lg:flex-row lg:items-center justify-between gap-4">
        <div className="space-y-1">
          <div className="flex items-center gap-2 text-sm font-semibold text-slate-900">
            <History className="w-4 h-4 text-blue-600" />
            四层增强跨话记忆库（StoryBible v2 · 隔离工作区 {series.series_id}）
          </div>
          <p className="text-xs text-slate-600 leading-relaxed max-w-3xl">
            针对 50~200 话长篇连载，记忆库按「人物实体图谱 + 分层摘要（近 3 话详摘 + 篇章归档） + 未回收伏笔追踪 + 按话只读快照」组织。重跑第 N 话时自动加载第 N-1 话快照，彻底杜绝跨话剧透污染。
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-4 shrink-0 text-xs">
          <div>
            <div className="text-slate-500">已归档话次快照</div>
            <div className="font-mono-tabular font-semibold text-slate-900 mt-0.5">
              {story_bible.snapshots_ep_nos.map((n) => `ep_${String(n).padStart(3, '0')}`).join(', ') || '暂无'}
            </div>
          </div>
          <div className="pl-4 border-l border-slate-200">
            <div className="text-slate-500">活跃未回收伏笔</div>
            <div className="font-mono-tabular font-semibold text-amber-700 mt-0.5">
              {openThreads.length} 条 open
            </div>
          </div>
        </div>
      </section>

      {/* Series Narration Persona */}
      <section className="bg-white border border-slate-200 rounded-xl p-5">
        <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-3">
          <div className="flex-1">
            <label className="block text-xs font-semibold text-slate-800 mb-1.5">
              作品专属解说人设与叙事基调（仅作用于《{series.series_title}》）
            </label>
            <input
              type="text"
              value={styleDraft}
              onChange={(e) => setStyleDraft(e.target.value)}
              className="w-full px-3.5 py-2 text-xs bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
            />
          </div>
          <button
            type="button"
            onClick={() => onUpdateStyle(series.series_id, styleDraft)}
            className="px-4 py-2 text-xs font-medium text-white bg-slate-900 rounded-lg hover:bg-slate-800 whitespace-nowrap shrink-0 cursor-pointer"
          >
            保存本作品叙事风格
          </button>
        </div>
      </section>

      {/* Layer 1 & Layer 3 Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-6 items-start">
        {/* Layer 1: Character Entity Graph */}
        <section className="lg:col-span-7 bg-white border border-slate-200 rounded-xl p-5 space-y-4">
          <div className="flex items-center justify-between border-b border-slate-100 pb-3">
            <div className="flex items-center gap-2">
              <Users className="w-4 h-4 text-slate-600" />
              <h3 className="text-sm font-semibold text-slate-900">
                Layer 1 · 人物实体图谱（同时注入 Stage 3 识图防脸盲）
              </h3>
            </div>
            <span className="text-xs font-mono-tabular text-slate-400">
              {characters.length} 名角色
            </span>
          </div>

          {formError && (
            <div className="px-3 py-2 rounded-lg border border-rose-200 bg-rose-50 text-xs text-rose-700">
              {formError}
            </div>
          )}

          <div className="space-y-3">
            {characters.map((ch) => {
              const isEditing = editingName === ch.name && draft !== null;
              return (
                <div
                  key={ch.name}
                  className="p-3.5 border border-slate-200 rounded-lg bg-slate-50/50 space-y-1.5"
                >
                  {isEditing && draft ? (
                    <div
                      className="space-y-2"
                      onKeyDown={(e) => {
                        if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
                          e.preventDefault();
                          handleSaveCharacter();
                        } else if (e.key === 'Escape') {
                          e.preventDefault();
                          cancelEdit();
                        }
                      }}
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <input
                          type="text"
                          value={draft.name}
                          onChange={(e) =>
                            setDraft({ ...draft, name: e.target.value })
                          }
                          placeholder="角色主名"
                          className="flex-1 min-w-[8rem] px-2.5 py-1.5 text-xs bg-white border border-slate-200 rounded-lg focus:outline-none focus:border-blue-600"
                          aria-label="角色主名"
                        />
                        <select
                          value={draft.gender}
                          onChange={(e) =>
                            setDraft({
                              ...draft,
                              gender: e.target.value as Gender,
                            })
                          }
                          className="px-2 py-1.5 text-xs bg-white border border-slate-200 rounded-lg"
                          aria-label="角色性别"
                        >
                          <option value="unknown">性别未标注</option>
                          <option value="male">男</option>
                          <option value="female">女</option>
                        </select>
                        <input
                          type="number"
                          min={1}
                          value={draft.first_seen_ep}
                          onChange={(e) =>
                            setDraft({
                              ...draft,
                              first_seen_ep: Number(e.target.value) || 1,
                            })
                          }
                          className="w-16 px-2 py-1.5 text-xs font-mono-tabular bg-white border border-slate-200 rounded-lg"
                          aria-label="首现话次"
                        />
                        <input
                          type="number"
                          min={1}
                          value={draft.last_seen_ep}
                          onChange={(e) =>
                            setDraft({
                              ...draft,
                              last_seen_ep: Number(e.target.value) || 1,
                            })
                          }
                          className="w-16 px-2 py-1.5 text-xs font-mono-tabular bg-white border border-slate-200 rounded-lg"
                          aria-label="最近出场话次"
                        />
                      </div>
                      <input
                        type="text"
                        value={draft.aliases}
                        onChange={(e) =>
                          setDraft({ ...draft, aliases: e.target.value })
                        }
                        placeholder="别名（逗号分隔）"
                        className="w-full px-2.5 py-1.5 text-xs bg-white border border-slate-200 rounded-lg"
                        aria-label="别名"
                      />
                      <input
                        type="text"
                        value={draft.visual_traits}
                        onChange={(e) =>
                          setDraft({ ...draft, visual_traits: e.target.value })
                        }
                        placeholder="外貌特征（注入识图）"
                        className="w-full px-2.5 py-1.5 text-xs bg-white border border-slate-200 rounded-lg"
                        aria-label="外貌特征"
                      />
                      <input
                        type="text"
                        value={draft.current_status}
                        onChange={(e) =>
                          setDraft({ ...draft, current_status: e.target.value })
                        }
                        placeholder="最新状态"
                        className="w-full px-2.5 py-1.5 text-xs bg-white border border-slate-200 rounded-lg"
                        aria-label="最新状态"
                      />

                      {draft.name.trim() !== ch.name && (
                        <label className="flex items-center gap-1.5 text-[11px] text-slate-600">
                          <input
                            type="checkbox"
                            checked={draft.keepOldNameAsAlias}
                            onChange={(e) =>
                              setDraft({
                                ...draft,
                                keepOldNameAsAlias: e.target.checked,
                              })
                            }
                          />
                          原主名「{ch.name}」保留为别名
                        </label>
                      )}

                      <div className="flex flex-wrap items-center gap-2">
                        <select
                          value={draft.mergeInto}
                          onChange={(e) =>
                            setDraft({ ...draft, mergeInto: e.target.value })
                          }
                          className="px-2 py-1.5 text-xs bg-white border border-slate-200 rounded-lg"
                          aria-label="合并到"
                        >
                          <option value="">不合并</option>
                          {characters
                            .filter((c) => c.name !== ch.name)
                            .map((c) => (
                              <option key={c.name} value={c.name}>
                                合并到「{c.name}」
                              </option>
                            ))}
                        </select>
                        {draft.mergeInto && (
                          <label className="flex items-center gap-1.5 text-[11px] text-slate-600">
                            <input
                              type="checkbox"
                              checked={draft.mergeKeepAsAlias}
                              onChange={(e) =>
                                setDraft({
                                  ...draft,
                                  mergeKeepAsAlias: e.target.checked,
                                })
                              }
                            />
                            原角色收为别名
                          </label>
                        )}
                      </div>

                      <div className="flex flex-wrap items-center gap-2 pt-1">
                        <button
                          type="button"
                          onClick={handleSaveCharacter}
                          className="px-3 py-1.5 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 cursor-pointer"
                        >
                          保存
                        </button>
                        <button
                          type="button"
                          onClick={cancelEdit}
                          className="px-3 py-1.5 text-xs font-medium text-slate-700 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 cursor-pointer"
                        >
                          取消
                        </button>
                        <span className="text-[11px] text-slate-400">
                          Cmd/Ctrl+Enter 保存 · Esc 取消
                        </span>
                      </div>
                    </div>
                  ) : (
                    <>
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-semibold text-slate-900">{ch.name}</span>
                          <span
                            className={`px-1.5 py-0.5 text-[10px] rounded border ${
                              GENDER_BADGE_CLASS[ch.gender ?? 'unknown']
                            }`}
                          >
                            {GENDER_LABELS[ch.gender ?? 'unknown']}
                          </span>
                          {ch.aliases.length > 0 && (
                            <span className="text-xs text-slate-500">
                              别名：{ch.aliases.join(' / ')}
                            </span>
                          )}
                        </div>
                        <div className="flex items-center gap-2">
                          <span className="text-[11px] font-mono-tabular text-slate-500">
                            EP.{ch.first_seen_ep} – EP.{ch.last_seen_ep}
                          </span>
                          <button
                            type="button"
                            onClick={() => startEdit(ch)}
                            className="inline-flex items-center gap-1 text-[11px] font-medium text-blue-600 hover:underline cursor-pointer"
                            aria-label={`编辑角色 ${ch.name}`}
                          >
                            <Pencil className="w-3 h-3" />
                            编辑
                          </button>
                          {confirmDelete === ch.name ? (
                            <span className="inline-flex items-center gap-1">
                              <span className="text-[11px] text-rose-600">确认删除？</span>
                              <button
                                type="button"
                                onClick={() => handleDeleteCharacter(ch.name)}
                                className="px-2 py-0.5 text-[11px] font-medium text-white bg-rose-600 rounded hover:bg-rose-700 cursor-pointer"
                              >
                                删除
                              </button>
                              <button
                                type="button"
                                onClick={() => setConfirmDelete(null)}
                                className="px-2 py-0.5 text-[11px] text-slate-600 border border-slate-200 rounded hover:bg-slate-50 cursor-pointer"
                              >
                                取消
                              </button>
                            </span>
                          ) : (
                            <button
                              type="button"
                              onClick={() => setConfirmDelete(ch.name)}
                              className="inline-flex items-center gap-1 text-[11px] font-medium text-rose-600 hover:underline cursor-pointer"
                              aria-label={`删除角色 ${ch.name}`}
                            >
                              <Trash2 className="w-3 h-3" />
                              删除
                            </button>
                          )}
                        </div>
                      </div>
                      <p className="text-xs text-slate-600">
                        <strong className="font-medium text-slate-700">外貌特征：</strong>
                        {ch.visual_traits}
                      </p>
                      <p className="text-xs text-slate-700">
                        <strong className="font-medium text-slate-800">最新状态：</strong>
                        {ch.current_status}
                      </p>
                    </>
                  )}
                </div>
              );
            })}
          </div>

          <form
            onSubmit={handleAddChar}
            className="pt-3 border-t border-slate-100 grid grid-cols-1 sm:grid-cols-12 gap-2"
          >
            <input
              type="text"
              value={charName}
              onChange={(e) => {
                setCharName(e.target.value);
                setFormError('');
              }}
              placeholder="角色主名"
              className="sm:col-span-3 px-2.5 py-1.5 text-xs bg-slate-50 border border-slate-200 rounded-lg"
            />
            <select
              value={charGender}
              onChange={(e) => setCharGender(e.target.value as Gender)}
              className="sm:col-span-2 px-2.5 py-1.5 text-xs bg-slate-50 border border-slate-200 rounded-lg"
              aria-label="角色性别"
            >
              <option value="unknown">性别未标注</option>
              <option value="male">男</option>
              <option value="female">女</option>
            </select>
            <input
              type="number"
              min={1}
              value={charEp}
              onChange={(e) => setCharEp(Number(e.target.value) || 1)}
              className="sm:col-span-2 px-2.5 py-1.5 text-xs font-mono-tabular bg-slate-50 border border-slate-200 rounded-lg"
              aria-label="角色登场话次"
            />
            <input
              type="text"
              value={charAliases}
              onChange={(e) => setCharAliases(e.target.value)}
              placeholder="别名(逗号分隔)"
              className="sm:col-span-3 px-2.5 py-1.5 text-xs bg-slate-50 border border-slate-200 rounded-lg"
            />
            <input
              type="text"
              value={charTraits}
              onChange={(e) => setCharTraits(e.target.value)}
              placeholder="外貌特征（注入识图）"
              className="sm:col-span-4 px-2.5 py-1.5 text-xs bg-slate-50 border border-slate-200 rounded-lg"
            />
            <button
              type="submit"
              disabled={nameTaken}
              className="sm:col-span-2 inline-flex items-center justify-center gap-1 px-3 py-1.5 text-xs font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 whitespace-nowrap cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Plus className="w-3.5 h-3.5" />
              收录
            </button>
            <input
              type="text"
              value={charStatus}
              onChange={(e) => setCharStatus(e.target.value)}
              placeholder="当前状态（如：第3话加入主角小队）"
              className="sm:col-span-12 px-2.5 py-1.5 text-xs bg-slate-50 border border-slate-200 rounded-lg"
            />
            {nameTaken && charName.trim() && (
              <p className="sm:col-span-12 text-[11px] text-rose-600">
                角色主名「{charName.trim()}」已存在，请换一个名字
              </p>
            )}
          </form>
        </section>

        {/* Layer 3: PlotThread Foreshadowing Tracker */}
        <section className="lg:col-span-5 bg-white border border-slate-200 rounded-xl p-5 space-y-4">
          <div className="flex items-center justify-between border-b border-slate-100 pb-3">
            <div className="flex items-center gap-2">
              <GitBranch className="w-4 h-4 text-slate-600" />
              <h3 className="text-sm font-semibold text-slate-900">
                Layer 3 · 跨话伏笔追踪器
              </h3>
            </div>
            <span className="text-xs font-mono-tabular text-slate-400">
              {openThreads.length} 未回收 · {resolvedThreads.length} 已回收
            </span>
          </div>

          <div className="space-y-2.5">
            {story_bible.plot_threads.map((t) => (
              <div
                key={t.thread_id}
                className={`p-3 rounded-lg border text-xs space-y-1.5 ${
                  t.status === 'open'
                    ? 'border-amber-200 bg-amber-50/40'
                    : 'border-slate-200 bg-slate-50 opacity-65'
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  <span className="font-semibold text-slate-900">{t.title}</span>
                  <button
                    type="button"
                    onClick={() => onToggleThreadStatus(series.series_id, t.thread_id)}
                    className="inline-flex items-center gap-1 text-[11px] font-medium text-blue-600 hover:underline whitespace-nowrap shrink-0 cursor-pointer"
                  >
                    <CheckCircle2 className="w-3 h-3" />
                    {t.status === 'open' ? '标为已回收' : '重新激活'}
                  </button>
                </div>
                <p className="text-slate-600 leading-relaxed">{t.description}</p>
                <div className="text-[11px] font-mono-tabular text-slate-400">
                  埋下于 EP.{t.introduced_ep} · 状态：{t.status}
                  {t.resolved_ep ? ` (EP.${t.resolved_ep} 回收)` : ''}
                </div>
              </div>
            ))}
          </div>

          <form onSubmit={handleAddThread} className="pt-3 border-t border-slate-100 space-y-2">
            <div className="flex gap-2">
              <input
                type="text"
                value={threadTitle}
                onChange={(e) => setThreadTitle(e.target.value)}
                placeholder="新增伏笔标题（如：黑钥匙用途）"
                className="flex-1 px-2.5 py-1.5 text-xs bg-slate-50 border border-slate-200 rounded-lg"
              />
              <input
                type="number"
                min={1}
                value={threadEp}
                onChange={(e) => setThreadEp(Number(e.target.value) || 1)}
                className="w-16 px-2 py-1.5 text-xs font-mono-tabular bg-slate-50 border border-slate-200 rounded-lg"
                aria-label="伏笔出现话次"
              />
            </div>
            <div className="flex gap-2">
              <input
                type="text"
                value={threadDesc}
                onChange={(e) => setThreadDesc(e.target.value)}
                placeholder="伏笔上下文描述（Stage 4 会自动在相关话次呼应）"
                className="flex-1 px-2.5 py-1.5 text-xs bg-slate-50 border border-slate-200 rounded-lg"
              />
              <button
                type="submit"
                className="px-3 py-1.5 text-xs font-medium text-white bg-slate-900 rounded-lg hover:bg-slate-800 whitespace-nowrap cursor-pointer"
              >
                添加伏笔
              </button>
            </div>
          </form>
        </section>
      </div>

      {/* Layer 2: Arc Summaries & Recent Sliding Window Digests */}
      <section className="bg-white border border-slate-200 rounded-xl p-5 space-y-4">
        <div className="flex items-center gap-2 border-b border-slate-100 pb-3">
          <BookMarked className="w-4 h-4 text-slate-600" />
          <h3 className="text-sm font-semibold text-slate-900">
            Layer 2 · 分层剧情记忆（长期篇章归档 + 近 3 话滑动窗口详摘）
          </h3>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div className="space-y-2.5">
            <div className="text-xs font-medium text-slate-500">
              篇章级长期归档（Arc Summaries · 不被后续话次覆盖）
            </div>
            {story_bible.arc_summaries.length > 0 ? (
              story_bible.arc_summaries.map((arc, i) => (
                <div key={i} className="p-3 border border-slate-200 rounded-lg bg-slate-50 text-xs space-y-1">
                  <div className="flex items-center justify-between font-semibold text-slate-900">
                    <span>{arc.arc_name}</span>
                    <span className="font-mono-tabular text-slate-500">
                      EP.{arc.ep_range[0]}–{arc.ep_range[1]}
                    </span>
                  </div>
                  <p className="text-slate-600 leading-relaxed">{arc.summary}</p>
                </div>
              ))
            ) : (
              <div className="p-4 border border-dashed border-slate-200 rounded-lg text-xs text-slate-400">
                连载满一个篇章（或每 10 话）后由 qwen-plus 自动触发压缩归档
              </div>
            )}
          </div>

          <div className="space-y-2.5">
            <div className="text-xs font-medium text-slate-500">
              近话高保真摘要与关键词索引（Recent Digests · 按需召回）
            </div>
            {story_bible.recent_digests.map((d) => (
              <div key={d.episode_no} className="p-3 border border-slate-200 rounded-lg text-xs space-y-1">
                <div className="flex items-center justify-between">
                  <span className="font-mono-tabular font-semibold text-slate-900">
                    EP.{String(d.episode_no).padStart(3, '0')} · {d.one_line_hook}
                  </span>
                </div>
                <p className="text-slate-600">{d.summary}</p>
                <div className="text-[11px] text-slate-400 font-mono-tabular">
                  出场：{d.characters_present.join(' / ')} · 关键词：{d.keywords.join(', ')}
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>
    </div>
  );
};
