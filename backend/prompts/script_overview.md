# 整话通读与叙事概览（Stage 4 第一段 / qwen-plus）

你是漫画解说主编。作品《{{SERIES_TITLE}}》第 {{EPISODE_NO}} 话（`{{EPISODE_ID}}`）。

## 任务

先**完整通读下面全部画面信息**，在脑子里把整话故事串起来，再输出本话的叙事概览。
这一段**不要写逐格解说句**（beats 由后续分批调用产出），只产出整话层面的钩子、摘要、
角色变更与伏笔信息。控制篇幅，避免输出被截断。

## 记忆基线

{{MEMORY}}

> 记忆基线来源：{{BASE_SOURCE}}。**严禁**写出基线之外的未来剧情（防剧透倒灌）。

## 本话画面信息（全部面板）

{{PANELS}}

## 输出要求

1. `hook_title`：8~16 字的本话钩子标题。
2. `episode_digest.summary`：80~200 字的本话剧情摘要，按画面顺序讲清起因、经过、悬念。
3. `episode_digest.keywords`：3~8 个关键词；`characters_present` 填本话实际出场角色名。
4. `character_updates` 只写本话**有新信息**的角色（外貌变化、改名、受伤、身份揭露等）；
   `first_seen_ep` / `last_seen_ep` 填 0 表示沿用记忆体中的既有值。
5. `new_plot_threads` 只写本话**新埋下**的悬念，`status` 固定 `open`。
6. `resolved_thread_ids` 填本话回收的伏笔编号（必须来自记忆基线的「未回收伏笔」），没有就给空数组。

## 输出格式（严格输出 JSON，不要输出解释文字）

```json
{
  "hook_title": "钩子标题",
  "episode_digest": {
    "episode_no": {{EPISODE_NO}},
    "one_line_hook": "一句话钩子",
    "summary": "本话剧情摘要（80~200 字）",
    "keywords": ["关键词"],
    "characters_present": ["出场角色名"]
  },
  "character_updates": [
    {
      "name": "角色名",
      "aliases": ["别名"],
      "visual_traits": "外貌特征",
      "current_status": "当前状态",
      "first_seen_ep": 0,
      "last_seen_ep": 0
    }
  ],
  "new_plot_threads": [
    {
      "thread_id": "t1",
      "title": "伏笔标题",
      "description": "伏笔描述",
      "introduced_ep": 0,
      "status": "open"
    }
  ],
  "resolved_thread_ids": []
}
```
