/**
 * Patch v4.38：审核台用到的后端 API 封装。
 *
 * 说明：仓库里其余接口都直接写在 `src/App.tsx` 的 `apiFetch` 里；这里把
 * **音效曲库**这三只接口单独收成一个模块，方便审核台组件直接引用，
 * 也避免把曲库逻辑塞进体量已经很大的 `App.tsx`。
 */
import type {
  CompressMontageResponse,
  SeriesStyleProfileResponse,
  SfxLibraryResponse,
  StyleProfileSummary,
} from '../types/studio';

const API_BASE = '/api';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      if (typeof data?.detail === 'string') {
        detail = data.detail;
      }
    } catch {
      /* 响应体不是 JSON 时保留状态码 */
    }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** 拉取音效曲库（24 个标准音效键 + 盘面可用情况）。 */
export async function fetchSfxLibrary(): Promise<SfxLibraryResponse> {
  return request<SfxLibraryResponse>('/sfx-library');
}

/** 上传（覆盖）一个音效素材，返回刷新后的曲库。 */
export async function uploadSfxAsset(
  sfxKey: string,
  filename: string,
  contentBase64: string,
): Promise<SfxLibraryResponse> {
  return request<SfxLibraryResponse>('/sfx-library/upload', {
    method: 'POST',
    body: JSON.stringify({
      sfx_key: sfxKey,
      filename,
      content_base64: contentBase64,
    }),
  });
}

/** 音效试听地址（浏览器 <audio> / new Audio() 直接用）。 */
export function sfxFileUrl(sfxKey: string): string {
  return `${API_BASE}/sfx-file/${encodeURIComponent(sfxKey)}`;
}

/* ------------------------------------------------------------------ */
/* Patch v4.43：全局音效资源库（新增 / 改元数据 / 上传替换 / 删除还原）  */
/* ------------------------------------------------------------------ */

/** 新增自定义音效时提交的内容（音频可为空 = 先只登记元数据）。 */
export interface SfxAssetDraft {
  /** 音效键（后端校验 ^[a-z][a-z0-9_]{1,39}$ 且不与已有条目冲突）。 */
  key: string;
  /** 中文名。 */
  label: string;
  /** 一级大类（动作 / 环境 / 转场 / 道具）。 */
  group: string;
  /** 细分类（自由文本）。 */
  category: string;
  /** 缺素材时的兜底音效键（可空）。 */
  fallback_key?: string;
  /** 原始文件名（可空）。 */
  filename?: string;
  /** 音频内容的 base64（可空）。 */
  content_base64?: string;
}

/** 可修改的元数据字段（全部可选，只传要改的）。 */
export interface SfxAssetMetaPatch {
  label?: string;
  group?: string;
  category?: string;
  fallback_key?: string;
}

/** 新增一个自定义音效，返回刷新后的曲库。 */
export async function createSfxAsset(draft: SfxAssetDraft): Promise<SfxLibraryResponse> {
  return request<SfxLibraryResponse>('/sfx-library', {
    method: 'POST',
    body: JSON.stringify(draft),
  });
}

/** 改一个音效的元数据（中文名 / 大类 / 细类 / 兜底），返回刷新后的曲库。 */
export async function updateSfxAsset(
  sfxKey: string,
  patch: SfxAssetMetaPatch,
): Promise<SfxLibraryResponse> {
  return request<SfxLibraryResponse>(`/sfx-library/${encodeURIComponent(sfxKey)}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
}

/**
 * 删除一个音效。
 *
 * - 自定义音效：移出曲库并删掉音频文件；
 * - 内置音效：`mode='restore_audio'` 还原成仓库默认素材，
 *   `mode='disable'` 把它停用（不再出现在曲库里）。
 */
export async function deleteSfxAsset(
  sfxKey: string,
  mode?: 'restore_audio' | 'disable',
): Promise<SfxLibraryResponse> {
  const query = mode ? `?mode=${encodeURIComponent(mode)}` : '';
  return request<SfxLibraryResponse>(
    `/sfx-library/${encodeURIComponent(sfxKey)}${query}`,
    { method: 'DELETE' },
  );
}

/** 还原一个被停用的内置音效，返回刷新后的曲库。 */
export async function restoreSfxAsset(sfxKey: string): Promise<SfxLibraryResponse> {
  return request<SfxLibraryResponse>(
    `/sfx-library/${encodeURIComponent(sfxKey)}/restore`,
    { method: 'POST' },
  );
}

/* ------------------------------------------------------------------ */
/* Patch v4.44：漫画风格预设 + 连招蒙太奇                                  */
/* ------------------------------------------------------------------ */

/** 拉取全部漫画风格预设。 */
export async function fetchStyleProfiles(): Promise<StyleProfileSummary[]> {
  const data = await request<{ profiles: StyleProfileSummary[] }>('/style-profiles');
  return data.profiles ?? [];
}

/** 按来源 URL / 类型标签推荐风格预设。 */
export async function suggestStyleProfile(urlOrGenre: string): Promise<string> {
  const data = await request<{ style_profile: string }>(
    `/style-profiles/suggest?url_or_genre=${encodeURIComponent(urlOrGenre)}`,
  );
  return data.style_profile;
}

/** 拉取作品当前生效的风格预设。 */
export async function fetchSeriesStyleProfile(
  seriesId: string,
): Promise<SeriesStyleProfileResponse> {
  return request<SeriesStyleProfileResponse>(
    `/series/${encodeURIComponent(seriesId)}/style-profile`,
  );
}

/** 保存作品级风格预设（未知预设后端返回 400）。 */
export async function updateSeriesStyleProfile(
  seriesId: string,
  payload: { style_profile?: string; style_overrides?: Record<string, unknown> },
): Promise<SeriesStyleProfileResponse> {
  return request<SeriesStyleProfileResponse>(
    `/series/${encodeURIComponent(seriesId)}/style-profile`,
    { method: 'PUT', body: JSON.stringify(payload) },
  );
}

/** 把某一幕内连续的动作解说压成多镜蒙太奇。 */
export async function compressSceneMontage(
  episodeId: string,
  sceneId: string,
): Promise<CompressMontageResponse> {
  return request<CompressMontageResponse>(
    `/episodes/${encodeURIComponent(episodeId)}/scenes/${encodeURIComponent(sceneId)}/compress-montage`,
    { method: 'POST' },
  );
}
