---
name: camofox-search-rule
description: "管理 camofox-search 的搜索规则与内容规则。当网页搜索返回 0 结果、页面提取内容为空/缺失、需要新增搜索引擎或修复某个站点的正文提取时，用 camofox-search-manage 工具创建/更新/删除规则，改完立即用 web_search / web_extract 验证。"
---

# camofox-search 规则管理（搜索规则 + 内容规则）

camofox-search 插件的搜索与提取行为由规则数据库驱动。本技能教你**主动**维护这些规则：
搜索失败不放弃——诊断页面结构，写规则，验证，必要时修正。

## 数据位置与结构

- 数据库：`HERMES_HOME/camofox-search/camofox-search.db`（WAL 模式，可直接用 sqlite3 查看）
- `search_rules`（搜索规则）：`domain`（搜索引擎域名，如 `bing.com`）、`url_template`
  （SERP URL 模板）、`extract_js`（页面内提取 JS）
- `content_rules`（内容规则）：`domain`（内容网页域名，如 `zhihu.com`）、`extract_js`

内置引擎（bing / google / duckduckgo）首次建库时作为种子写入 `search_rules`，之后不会再被覆盖——
它们可以直接编辑或删除。

## 工具：camofox-search-manage

参数：`action`（list/get/create/update/delete）、`rule_type`（search/content）、
`domain`、`url_template`（仅 search 规则）、`extract_js`、`notes`（可选备注）。
调用示例（工具参数即下面的 JSON）：

```json
{"action": "list", "rule_type": "search"}
{"action": "get", "rule_type": "search", "domain": "bing.com"}
{"action": "create", "rule_type": "search",
 "domain": "sogou.com",
 "url_template": "https://www.sogou.com/web?query={query}",
 "extract_js": "JSON.stringify({ok:true,results:Array.from(document.querySelectorAll('.vrwrap')).slice(0,@LIMIT@).map(function(el){var a=el.querySelector('h3 a');return {title:(a&&a.textContent||'').trim(),url:(a&&a.href)||'',description:(el.querySelector('.str-text')&&el.querySelector('.str-text').textContent||'').trim()}}).filter(function(r){return r.url})})",
 "notes": "搜狗中文搜索"}
{"action": "update", "rule_type": "content", "domain": "example.com",
 "extract_js": "JSON.stringify({ok:true,title:document.title,url:location.href,text:(document.querySelector('article')||document.body).innerText.slice(0,50000)})"}
{"action": "delete", "rule_type": "search", "domain": "sogou.com"}
```

`domain` 接受裸域名、主机名或完整 URL（`https://www.example.com/x` → `example.com`）。
`update` 是合并语义：只改你传入的字段。`create` 遇到已存在的域名会报错——先 `get` 再 `update`。

## 搜索规则契约

- `url_template`：必须以 `http(s)://` 开头并包含 `{query}`（自动 URL 编码）；
  `{limit}` 可选（结果数量）。`extract_js` 里的 `@LIMIT@` 会被替换为同一数量。
- `extract_js`：**单个** JS 表达式，在 SERP 页面上下文求值，必须返回
  `JSON.stringify({ok:true,results:[{title,url,description},...]})`；
  无链接的条目会被丢弃，按顺序取前 `@LIMIT@` 条。
- 生效方式：`CAMOFOX_SEARCH_ENGINE`（默认 `bing`）按**域名匹配**规则——
  `bing`、`bing.com`、`cn.bing.com` 都会命中 `bing.com` 规则（最长域名优先）。
  所以新增引擎 = 建规则 + 把 `CAMOFOX_SEARCH_ENGINE` 指到该域名。
- 该域名在数据库中无规则时，退回内置引擎（bing/google/duckduckgo）。

## 内容规则契约

- 只按域名匹配：命中 `example.com` 的规则适用于 `example.com` 及其所有子域
  （`news.example.com` 也命中；最长匹配优先）。
- `extract_js`：单个 JS 表达式，在目标页面上下文求值。返回
  `JSON.stringify({ok:true,title,url,text})`（`text` 为正文；也可用 `content` 键），
  **或直接返回字符串**（即正文）。`@LIMIT@` 不替换。
- 规则返回空内容或 `ok:false` 时自动回退到默认全文提取；`web_extract` 结果的
  `metadata.extract_rule` 告诉你正文来自哪条规则（`default` 或域名）；
  规则被尝试但失败时另有 `metadata.extract_rule_attempted`。

## 主动管理流程（重要）

1. **发现**：搜索返回 0 结果、结果与页面不符、或 `web_extract` 返回空/无关内容时，
   先 `{"action":"list","rule_type":"search"}` / `"content"` 看现状。
2. **诊断**：用可用工具打开真实页面确认 DOM——搜索页看结果条目选择器，
   内容页看正文容器（`article`、`main`、阅读器样式类名等）。
   可用 `web_extract`（看默认提取效果）或浏览器工具执行 `document.querySelectorAll(...)` 验证选择器。
3. **写规则**：保持 JS 为**单表达式**、无 `;` 结尾的完整语句块；用 `.slice(0,@LIMIT@)`
   控制数量；过滤无 `url` 的条目；尽量多写选择器回退（`a || b || c`）。
4. **验证**：规则改完**立即**调用 `web_search`（搜索引擎）或 `web_extract`（内容页）验证；
   失败就 `update` 迭代，或 `delete` 回到默认行为。不要凭猜测宣布成功。
5. **记录**：用 `notes` 写清原因（页面改版、站点特殊结构），方便以后排查。

## 安全与撤销

- 规则对当前 profile 的**所有**搜索/提取生效；一次只改一条规则并验证。
- 删除规则即回到内置引擎 / 默认提取；内置种子规则可以编辑，别整表清空。
- 规则库损坏或不可写时插件自动回退到内置引擎和默认提取，不会中断搜索——
  但日志里会有 `camofox-search: ... rule ... failed` 警告，遇到时先检查
  `HERMES_HOME/camofox-search/` 目录的读写权限。
- 数据库用 WAL 策略；查看或备份直接复制 `camofox-search.db`（及 `-wal`/`-shm`）即可。
