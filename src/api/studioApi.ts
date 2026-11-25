/**
 * Patch v4.38：审核台用到的后端 API 封装。
 *
 * 说明：仓库里其余接口都直接写在 `src/App.tsx` 的 `apiFetch` 里；这里把
 * **音效曲库**这三只接口单独收成一个模块，方便审核台组件直接引用，
 * 也避免把曲库逻辑塞进体量已经很大的 `App.tsx`。
 */
import type { SfxLibraryResponse } from '../types/studio';

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
