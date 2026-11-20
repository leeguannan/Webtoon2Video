/**
 * Patch v4.11：磁盘体积展示工具。
 *
 * 一键清理过程文件功能需要在按钮、确认条与徽标里展示话次占用与可回收空间，
 * 统一口径避免各组件各写一套换算。
 */

/** 把字节数格式化为可读体积：``98 MB`` / ``1.25 GB`` / ``<1 MB``。 */
export function formatBytes(bytes: number | undefined | null): string {
  const value = Number(bytes) || 0;
  if (value <= 0) return '0 MB';
  const gb = value / 1024 ** 3;
  if (gb >= 1) return `${gb.toFixed(2)} GB`;
  const mb = value / 1024 ** 2;
  if (mb < 1) return '<1 MB';
  return `${Math.round(mb)} MB`;
}
