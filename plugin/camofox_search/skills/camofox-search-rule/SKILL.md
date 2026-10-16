---
name: camofox-search-rule
description: "Manage camofox-search search/content rules when web_search returns no or wrong results, web_extract content is empty or missing, a new search engine is needed, a site's extraction needs fixing, or the search/content URL has NO matching rule yet. Create, update, or delete rules with the camofox-search-manage tool and verify with web_search / web_extract."
---

# camofox-search rule management (search rules + content rules)

camofox-search's search and extraction behavior is driven by a rule database.
This skill is how you maintain those rules **proactively**: don't give up when
a search or an extraction fails — diagnose the page, write a rule, verify it,
iterate.

## When to manage rules

Treat any of these as a trigger to inspect and manage rules:

- `web_search` returns zero results, or results that clearly don't match the
  page (blocked page, consent wall, redesigned layout).
- `web_extract` returns empty, truncated, or irrelevant content for a page.
- The user asks for a new search engine, or a site's extraction keeps needing
  post-processing.
- **No rule covers the search URL or the content URL you are working with.**
  A call that fell back to a built-in engine, or to the default full-page
  reader (`metadata.extract_rule == "default"`), means no rule exists for that
  domain — rule management is in scope too: normally create a rule that captures
  the working behavior so it becomes explicit, editable, and shared; skip
  throwaway one-off pages nobody will revisit.

Look at the current state first:

```json
{"action": "list", "rule_type": "search"}
{"action": "list", "rule_type": "content"}
{"action": "get", "rule_type": "search", "domain": "bing.com"}
```

## Data location and schema

- Database: `HERMES_HOME/camofox-search/camofox-search.db` (WAL mode; readable
  directly with sqlite3).
- `search_rules` (search rules): `domain` (search-engine domain, e.g.
  `bing.com`), `url_template` (SERP URL template), `extract_js` (page-context
  extraction JS).
- `content_rules` (content rules): `domain` (content-page domain, e.g.
  `zhihu.com`), `extract_js`.

The built-in engines (bing / google / duckduckgo) are seeded into `search_rules`
the first time the database is created and are never overwritten afterwards —
edit or delete them like any other rule.

## Tool: camofox-search-manage

Arguments: `action` (`list | get | create | update | delete`), `rule_type`
(`search | content`), `domain`, `url_template` (search rules only),
`extract_js`, `notes` (optional). Example calls (the JSON is the tool
argument):

```json
{"action": "list", "rule_type": "search"}
{"action": "get", "rule_type": "search", "domain": "bing.com"}
{"action": "create", "rule_type": "search",
 "domain": "sogou.com",
 "url_template": "https://www.sogou.com/web?query={query}",
 "extract_js": "JSON.stringify({ok:true,results:Array.from(document.querySelectorAll('.vrwrap')).slice(0,@LIMIT@).map(function(el){var a=el.querySelector('h3 a');return {title:(a&&a.textContent||'').trim(),url:(a&&a.href)||'',description:(el.querySelector('.str-text')&&el.querySelector('.str-text').textContent||'').trim()}}).filter(function(r){return r.url})})",
 "notes": "Sogou Chinese web search"}
{"action": "update", "rule_type": "content", "domain": "example.com",
 "extract_js": "JSON.stringify({ok:true,title:document.title,url:location.href,text:(document.querySelector('article')||document.body).innerText.slice(0,50000)})"}
{"action": "delete", "rule_type": "search", "domain": "sogou.com"}
```

`domain` accepts a bare domain, a hostname, or a full URL
(`https://www.example.com/x` → `example.com`). `update` is merge-style: only
the fields you pass change. `create` fails when the domain already exists —
`get` first, then `update`.

## Search rule contract

- `url_template`: must start with `http(s)://` and contain `{query}` (the query
  is URL-encoded automatically); `{limit}` is optional (result count). In
  `extract_js`, `@LIMIT@` is replaced with that same count.
- `extract_js`: a **single** JS expression evaluated in the SERP page context.
  It must return
  `JSON.stringify({ok:true,results:[{title,url,description},...]})`. Entries
  without a URL are dropped; the first `@LIMIT@` entries are kept, in order.
- Engine selection: `CAMOFOX_SEARCH_ENGINE` (default `bing`) matches rules **by
  domain** — `bing`, `bing.com`, and `cn.bing.com` all resolve to the
  `bing.com` rule (longest domain wins). Adding an engine = create a rule +
  point `CAMOFOX_SEARCH_ENGINE` at its domain.
- When no rule matches, the built-in engines are the fallback.

## Content rule contract

- Domain-matched only: a rule for `example.com` applies to `example.com` and
  all of its subdomains (`news.example.com` matches too; longest match wins).
- `extract_js`: a single JS expression evaluated in the target page context.
  Return `JSON.stringify({ok:true,title,url,text})` (`text` is the body; the
  `content` key works too), **or plain text** (used as the content). `@LIMIT@`
  is not substituted for content rules.
- When a rule yields empty content or `ok:false`, extraction falls back to the
  default full-page reader. The `web_extract` result metadata records which
  path produced the content: `metadata.extract_rule` is `default` or the rule
  domain, and `metadata.extract_rule_attempted` is present when a rule was
  tried but failed.

## Proactive workflow

1. **Detect** — a trigger fired. List the rules to see what already exists
   (`{"action":"list","rule_type":"search"}` / `"content"`).
2. **Diagnose** — open the real page with whatever browser tools are available
   and confirm the DOM: for a SERP, the result-item selectors; for a content
   page, the main-body container (`article`, `main`, reader-mode class names).
   `document.querySelectorAll(...)` / `document.body.innerText` probes work, or
   use the default `web_extract` output as a baseline.
3. **Write the rule** — keep `extract_js` a **single expression** (no statement
   blocks, no trailing `;`); bound counts with `.slice(0,@LIMIT@)`; drop
   entries without a `url`; chain selector fallbacks (`a || b || c`) where
   layouts vary.
4. **Verify** — immediately re-run `web_search` (search rules) or `web_extract`
   (content rules) on the affected URL. If it fails, `update` and iterate, or
   `delete` to fall back to the default behavior. Never claim success without
   this check.
5. **Record** — put the why in `notes` (site redesign, special layout) so the
   next maintainer has the context.

## Safety and rollback

- Rules apply to **every** search/extraction in the current profile; change one
  rule at a time and verify. Changes take effect on the next `web_search` /
  `web_extract` call — no restart.
- Deleting a rule reverts that domain to the built-in engine / default reader.
  The seeded built-in rules can be edited; don't wipe the tables.
- If the rule database is unwritable or corrupt, the plugin degrades gracefully
  (built-in engines + default extraction) and logs
  `camofox-search: ... rule ... failed` warnings — check read/write access to
  `HERMES_HOME/camofox-search/` when you see those.
- The database uses WAL; to back it up, copy `camofox-search.db` (plus the
  `-wal` / `-shm` files if present).
