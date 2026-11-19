/**
 * Patch v4.10：成片（Stage 7 / s7_renderer）完成后的声音提醒与桌面通知。
 *
 * 设计要点：
 * - 不依赖任何外部音频文件，提示音全部用 Web Audio 现场合成，
 *   避免为一个提示音引入静态资源与加载时序问题。
 * - 所有浏览器 API 调用都包在 try/catch 中：受限环境（无 Notification、
 *   自动播放策略拦截、隐私模式禁用 localStorage）最多是"没声音/没弹窗"，
 *   绝不能把整个流水线界面弄崩。
 * - 偏好写入 localStorage，刷新页面后仍然记住用户的选择。
 */

/** localStorage 键名。 */
export const RENDER_NOTIFY_PREFS_KEY = 'w2v_render_notify_prefs';

/** 提醒偏好。 */
export interface RenderNotifyPrefs {
  /** 成片完成/失败时是否播放提示音。 */
  soundEnabled: boolean;
  /** 成片完成/失败时是否弹桌面通知。 */
  desktopEnabled: boolean;
}

const DEFAULT_PREFS: RenderNotifyPrefs = {
  soundEnabled: true,
  desktopEnabled: true,
};

/* ------------------------------------------------------------------ */
/* 偏好持久化                                                          */
/* ------------------------------------------------------------------ */

/** 读取提醒偏好；存储不可用或数据损坏时回落到默认值。 */
export function loadRenderNotifyPrefs(): RenderNotifyPrefs {
  try {
    if (typeof window === 'undefined' || !window.localStorage) {
      return { ...DEFAULT_PREFS };
    }
    const raw = window.localStorage.getItem(RENDER_NOTIFY_PREFS_KEY);
    if (!raw) return { ...DEFAULT_PREFS };
    const parsed = JSON.parse(raw) as Partial<RenderNotifyPrefs> | null;
    if (!parsed || typeof parsed !== 'object') return { ...DEFAULT_PREFS };
    return {
      soundEnabled:
        typeof parsed.soundEnabled === 'boolean' ? parsed.soundEnabled : DEFAULT_PREFS.soundEnabled,
      desktopEnabled:
        typeof parsed.desktopEnabled === 'boolean'
          ? parsed.desktopEnabled
          : DEFAULT_PREFS.desktopEnabled,
    };
  } catch {
    return { ...DEFAULT_PREFS };
  }
}

/** 写入提醒偏好；失败（隐私模式/配额）时静默忽略。 */
export function saveRenderNotifyPrefs(prefs: RenderNotifyPrefs): void {
  try {
    if (typeof window === 'undefined' || !window.localStorage) return;
    window.localStorage.setItem(
      RENDER_NOTIFY_PREFS_KEY,
      JSON.stringify({
        soundEnabled: prefs.soundEnabled,
        desktopEnabled: prefs.desktopEnabled,
      })
    );
  } catch {
    /* 存储不可用时忽略即可，不影响主流程 */
  }
}

/* ------------------------------------------------------------------ */
/* Web Audio 提示音                                                    */
/* ------------------------------------------------------------------ */

/** 模块级单例 AudioContext：重复创建会触发浏览器的连接数上限。 */
let audioContext: AudioContext | null = null;

function getAudioContext(): AudioContext | null {
  if (audioContext) return audioContext;
  try {
    const Ctor =
      window.AudioContext ||
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctor) return null;
    audioContext = new Ctor();
    return audioContext;
  } catch {
    return null;
  }
}

/**
 * 解锁音频上下文：浏览器要求音频播放必须发生在用户手势内，
 * 因此在首次交互（或用户点击开关）时预先创建并 resume。
 */
export function primeNotificationAudio(): void {
  try {
    const ctx = getAudioContext();
    if (!ctx) return;
    if (ctx.state === 'suspended') {
      void ctx.resume().catch(() => undefined);
    }
  } catch {
    /* 浏览器禁用音频时忽略 */
  }
}

/** 单个柔和音符：快起慢落，峰值受控，避免突兀爆音。 */
function scheduleTone(
  ctx: AudioContext,
  freq: number,
  startAt: number,
  duration: number,
  peak: number
): void {
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = 'sine';
  osc.frequency.setValueAtTime(freq, startAt);
  gain.gain.setValueAtTime(0.0001, startAt);
  gain.gain.exponentialRampToValueAtTime(peak, startAt + 0.02);
  gain.gain.exponentialRampToValueAtTime(0.0001, startAt + duration);
  osc.connect(gain);
  gain.connect(ctx.destination);
  osc.start(startAt);
  osc.stop(startAt + duration + 0.02);
}

/** 成片完成：C5 → E5 → G5 上升三音，总时长约 0.55s。 */
export function playRenderCompleteChime(): void {
  try {
    const ctx = getAudioContext();
    if (!ctx) return;
    if (ctx.state === 'suspended') {
      void ctx.resume().catch(() => undefined);
    }
    const base = ctx.currentTime + 0.02;
    scheduleTone(ctx, 523.25, base, 0.3, 0.09);
    scheduleTone(ctx, 659.25, base + 0.12, 0.3, 0.09);
    scheduleTone(ctx, 783.99, base + 0.24, 0.3, 0.09);
  } catch {
    /* 浏览器禁用音频时忽略 */
  }
}

/** 成片失败：温和的低音双音（E4 → C4），与完成提示明显区分。 */
export function playRenderFailedTone(): void {
  try {
    const ctx = getAudioContext();
    if (!ctx) return;
    if (ctx.state === 'suspended') {
      void ctx.resume().catch(() => undefined);
    }
    const base = ctx.currentTime + 0.02;
    scheduleTone(ctx, 329.63, base, 0.26, 0.08);
    scheduleTone(ctx, 261.63, base + 0.16, 0.34, 0.08);
  } catch {
    /* 浏览器禁用音频时忽略 */
  }
}

/* ------------------------------------------------------------------ */
/* 桌面通知                                                            */
/* ------------------------------------------------------------------ */

/** 当前通知权限；浏览器不支持时返回 'unsupported'。 */
export function getDesktopNotificationPermission(): NotificationPermission | 'unsupported' {
  try {
    if (typeof window === 'undefined' || !('Notification' in window)) return 'unsupported';
    return Notification.permission;
  } catch {
    return 'unsupported';
  }
}

/** 申请通知权限；浏览器不支持时返回 'unsupported'。 */
export async function requestDesktopNotificationPermission(): Promise<
  NotificationPermission | 'unsupported'
> {
  try {
    if (typeof window === 'undefined' || !('Notification' in window)) return 'unsupported';
    const permission = await Notification.requestPermission();
    return permission ?? Notification.permission;
  } catch {
    return getDesktopNotificationPermission();
  }
}

export interface DesktopNotificationOptions {
  title: string;
  body: string;
  /** 同一 tag 的新通知会替换旧通知，避免同一话次重复堆积。 */
  tag?: string;
  /** 用户点击通知时的回调（用于跳转到对应话次）。 */
  onClick?: () => void;
}

/** 弹出桌面通知；未授权或浏览器不支持时静默跳过。 */
export function showDesktopNotification(options: DesktopNotificationOptions): void {
  try {
    if (typeof window === 'undefined' || !('Notification' in window)) return;
    if (Notification.permission !== 'granted') return;
    const notification = new Notification(options.title, {
      body: options.body,
      tag: options.tag,
    });
    notification.onclick = () => {
      try {
        window.focus();
      } catch {
        /* 某些环境没有 focus */
      }
      notification.close();
      options.onClick?.();
    };
  } catch {
    /* 通知被系统拦截时忽略 */
  }
}
