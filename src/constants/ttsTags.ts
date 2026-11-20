/**
 * MiniMax Speech 2.8 送音标签常量。
 *
 * 两类标签：
 * - 语气词标签（`(laughs)` 等）：写进送音文本，模型会按标签表演对应情绪/拟声。
 * - 停顿标签（`<#0.5#>`）：只能放在句中两段文字之间，句尾停顿请用 `pause_after_ms`。
 *
 * 这里是唯一的清单来源：前端面板按此渲染，后端不做二次校验，
 * 避免前后端各维护一份标签表导致不一致。
 */

/** 单个标签定义。 */
export interface TtsTagDef {
  /** 实际写入送音文本的标签，如 "(laughs)" */
  tag: string;
  /** 中文名，展示用 */
  label: string;
  /** 简短说明（可选），展示为次要提示 */
  hint?: string;
}

/** 语气词分组。 */
export interface TtsTagGroup {
  /** 分组名 */
  group: string;
  tags: TtsTagDef[];
}

/** MiniMax Speech 2.8 官方支持的 19 个语气词标签（顺序与下表一致，不可增删）。 */
export const MINIMAX_INTERJECTION_TAGS: TtsTagDef[] = [
  { tag: '(laughs)', label: '笑', hint: '轻笑' },
  { tag: '(chuckle)', label: '轻笑', hint: '忍不住的低声笑' },
  { tag: '(snorts)', label: '哼声', hint: '从鼻子里哼出来' },
  { tag: '(sighs)', label: '叹气', hint: '松一口气' },
  { tag: '(breath)', label: '呼吸声', hint: '一口呼吸' },
  { tag: '(pant)', label: '气喘', hint: '跑完之后喘气' },
  { tag: '(inhale)', label: '吸气', hint: '深吸一口气' },
  { tag: '(exhale)', label: '呼气', hint: '长长呼出' },
  { tag: '(coughs)', label: '咳嗽', hint: '忍不住咳' },
  { tag: '(clear-throat)', label: '清嗓子', hint: '清了清嗓子' },
  { tag: '(groans)', label: '呻吟', hint: '吃痛的声音' },
  { tag: '(gasps)', label: '倒吸气', hint: '突然吸一口气' },
  { tag: '(emm)', label: '嗯…', hint: '沉吟' },
  { tag: '(humming)', label: '哼歌', hint: '随口哼的调子' },
  { tag: '(hissing)', label: '嘶声', hint: '倒吸气的声音' },
  { tag: '(sniffs)', label: '吸鼻子', hint: '鼻子发出的声音' },
  { tag: '(burps)', label: '打嗝', hint: '打嗝声' },
  { tag: '(lip-smacking)', label: '咂嘴', hint: '舔嘴的声音' },
  { tag: '(sneezes)', label: '打喷嚏', hint: '打喷嚏' },
];

/**
 * 语气词按 4 组组织：笑与冷哼 / 叹息与呼吸 / 咳与哭痛 / 其它拟声。
 * 与 `MINIMAX_INTERJECTION_TAGS` 同源，摊平顺序一致。
 */
export const INTERJECTION_TAG_GROUPS: TtsTagGroup[] = [
  {
    group: '笑与冷哼',
    tags: [
      { tag: '(laughs)', label: '笑', hint: '轻笑' },
      { tag: '(chuckle)', label: '轻笑', hint: '忍不住的低声笑' },
      { tag: '(snorts)', label: '哼声', hint: '从鼻子里哼出来' },
    ],
  },
  {
    group: '叹息与呼吸',
    tags: [
      { tag: '(sighs)', label: '叹气', hint: '松一口气' },
      { tag: '(breath)', label: '呼吸声', hint: '一口呼吸' },
      { tag: '(pant)', label: '气喘', hint: '跑完之后喘气' },
      { tag: '(inhale)', label: '吸气', hint: '深吸一口气' },
      { tag: '(exhale)', label: '呼气', hint: '长长呼出' },
    ],
  },
  {
    group: '咳与哭痛',
    tags: [
      { tag: '(coughs)', label: '咳嗽', hint: '忍不住咳' },
      { tag: '(clear-throat)', label: '清嗓子', hint: '清了清嗓子' },
      { tag: '(groans)', label: '呻吟', hint: '吃痛的声音' },
      { tag: '(gasps)', label: '倒吸气', hint: '突然吸一口气' },
      { tag: '(emm)', label: '嗯…', hint: '沉吟' },
    ],
  },
  {
    group: '其它拟声',
    tags: [
      { tag: '(humming)', label: '哼歌', hint: '随口哼的调子' },
      { tag: '(hissing)', label: '嘶声', hint: '倒吸气的声音' },
      { tag: '(sniffs)', label: '吸鼻子', hint: '鼻子发出的声音' },
      { tag: '(burps)', label: '打嗝', hint: '打嗝声' },
      { tag: '(lip-smacking)', label: '咂嘴', hint: '舔嘴的声音' },
      { tag: '(sneezes)', label: '打喷嚏', hint: '打喷嚏' },
    ],
  },
];

/** 句中停顿快捷秒数。 */
export const PAUSE_QUICK_SECONDS: number[] = [0.3, 0.5, 0.8, 1.2, 2.0];

/** 停顿标签最小秒数。 */
export const PAUSE_TAG_MIN_SECONDS = 0.01;

/** 停顿标签最大秒数。 */
export const PAUSE_TAG_MAX_SECONDS = 99.99;

/**
 * 把秒数格式化成 MiniMax 停顿标签，如 0.5 → "<#0.5#>"、2 → "<#2#>"。
 *
 * 规则：钳位到 [0.01, 99.99]；最多两位小数；去掉多余的尾随 0（2.0 → "<#2#>"）。
 */
export function formatPauseTag(seconds: number): string {
  // NaN 没有可用值，退回最小值，保证返回的是合法标签；±Infinity 交给钳位处理。
  const safe = Number.isNaN(seconds) ? PAUSE_TAG_MIN_SECONDS : seconds;
  const clamped = Math.min(PAUSE_TAG_MAX_SECONDS, Math.max(PAUSE_TAG_MIN_SECONDS, safe));
  // 先四舍五入到两位小数，避免 0.1+0.2 之类的浮点误差写出 "<#0.30000000000000004#>"；
  // 补 1e-9 是为了抵消二进制表示误差（1.005 实际存成 1.00499999999999989），让进位符合十进制直觉。
  const rounded = Math.round(clamped * 100 + 1e-9) / 100;
  let text = rounded.toFixed(2);
  if (text.includes('.')) {
    text = text.replace(/0+$/, '').replace(/\.$/, '');
  }
  return `<#${text}#>`;
}

/**
 * 判断 MiniMax 模型是否属于 speech-2.8-* 系列（用于温和提醒）。
 *
 * 只认 `speech-2.8` 开头且后接分隔符的模型名，避免把 `speech-2.6-hd` 误判为可用标签的模型。
 */
export function isSpeech28Model(model: string | undefined | null): boolean {
  if (!model) return false;
  const name = model.trim().toLowerCase();
  const prefix = 'speech-2.8';
  if (!name.startsWith(prefix)) return false;
  const rest = name.slice(prefix.length);
  if (rest === '') return true;
  return ['-', '_', '.'].includes(rest[0]);
}
