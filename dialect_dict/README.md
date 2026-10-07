# 方言词典审校与检索系统

纯 Python 3 标准库实现（`http.server` + `sqlite3` + `threading`），无第三方依赖。
**完整词库持久化在 SQLite 后端**，前端页面全部通过 API 取数，不存在浏览器端静态词表。

```
run_server.py            启动入口（首次自动播种+发布 v1+后台异步索引）
core/
  db.py                  SQLite 模式：追加式版本表 + 发布闭包 + 索引任务
  normalize.py           规范化检索键 / 原文分词（含偏移映射，供可解释高亮）
  phonology.py           标音方案注册表与无损转换
  service.py             领域层：词条身份、合并拆分、引用、发布、缓存、撤权
  indexer.py             异步索引：乱序完成安全 + 版本水位线
  search.py              双路召回（规范键 + 原文索引）与 compare 对比
  api.py                 HTTP 路由，服务端逐端点强制角色
  seed.py                演示数据（同音异义/异体字/地区变体/语境说明/媒体）
static/public.html       公开检索页（尊重性语境说明、无音频文本、方案切换）
static/editor.html       审校后台（编辑/批准角色分离）
tests/                   15 个验收测试（unittest，零依赖）
```

运行：`python3 run_server.py --port 8000` → 公开页 `/`，后台 `/editor`
（editor1/ed-token 编辑；approver1/ap-token 审校）。测试：`python3 -m unittest discover -s tests`。

## 1. 词条身份：不靠拼音

`entries.id` 是代理键，与词形、标音均无关：

- **同音多义**：`生`(saang1，出生） 与 `甥`(saang1，外甥） 是两个词条，用
  `relations(type='homophone')` 边关联。改其中一方的标音不会合并、不会串义
  （`TestHomophonePolysemy`）。
- **异体字**：同一词条下多个 `forms` 行（`臺`标准繁体 / `台`简体 / `枱`俗写），
  带 `script` 标签；若异体在不同地区已分化成独立词，则用 `variant_char` 边跨词条关联。
- **地区异义**：义项带 `region`，跨词条的地区异名用 `regional_variant` 边
  （玉米↔粟米）。来源（`sources`）、地区变体、关系边全部落库持久化。

## 2. 标音方案：保留原始记录，不可转换即「未转换」

- `prons` 表永远保存**原始方案 + 原始记录**；切换显示方案只是另算一条
  `conversions` 记录（按 `rule_version` 缓存，规则升级自动重算），原行永不修改。
- 转换必须**无损**：音节切分失败、韵母/调类在目标方案无对应、目标方案表达不了
  某对立（简式罗马字无入声尾 `-p/-t/-k`）→ `status=unconvertible`，`value=null`，
  前端显示「未转换」及原因，**绝不猜读**。IPA→本地拼音只承认映射表能精确生成的
  规范形，表外一律拒绝。
- 演示：`baak6→ipa` 得 `paːk̚˨`；`baak6→plain` 与 `xeo3→ipa` 均「未转换」。

## 3. 规范化检索键 vs 原文分字段索引

系统同时维护两套索引（`GET /api/compare?q=…` 可并排对比）：

| | 规范化检索键 `idx_norm` | 原文分字段索引 `idx_tokens` |
|---|---|---|
| 处理 | NFKC→casefold→繁折简→剥离标点→剥离组合符/调号→IPA 近位折叠 | 不折叠，CJK 单字+二元、拉丁整词 |
| 匹配 | 规范键子串匹配 | token 精确相等（AND） |
| 优点 | 简繁、标点、音标组合字符差异都能召回 | 零误伤、偏移天然精确、可证伪 |
| 缺点 | 可能误召回（折叠是有损的） | 「台湾」查不到「臺灣」，「khe」查不到「kʰɛ́˥」 |
| 高亮 | 靠偏移映射表还原原文区间 | token 自带原文偏移 |

**组合字符匹配**：`é`（预组合 U+00E9）与 `e+◌́`（U+0065 U+0301）经 NFD 剥离 Mn
类后都归一为 `e`；IPA 调值字母 `˥˦˧˨˩`、上标调号 `¹²³⁴⁵` 剥离；修饰字母 `ʰ`
经 NFKC 折为 `h`；`ɛ→e、ɔ→o、ŋ→ng` 等近位折叠。**调类数字（baak6 的 6）保留**，
因为在本词典方案中它承载对立——这是有意的设计取舍，explain 会如实告知。

**可解释高亮**：`normalize()` 在生成规范键的同时记录每个键字符的原文下标
（offsets）与触发的每条规则（rules）。命中后 `spans_to_original()` 把键上区间映回
原文区间（尾部组合符并入高亮），explain 字段列出实际触发规则，例如
`trad2simp:臺→台; strip_combining:U+0301; strip_tone:˥`，前端据 spans 包 `<mark>`。

## 4. 版本化关系闭包与缓存纪律

- 所有实体表追加式版本化：更新=插入 `version+1` 新行，旧行永不动。
- `publish()`（仅 approver）把当时**全部实体**的 `(kind,id,version)` 写入
  `closure`——闭包同时钉住关系两端、义项、例句、媒体的版本，因此旧深链接
  `/entry/<id>?v=2#sense-1` 永远解析到 v2 那一套自洽内容。
- **删除被引用义项**：软删除（`deleted=1`）。`citations` 钉住 `sense_version`，
  解析时返回存档内容 + `removed`（或 `updated`）状态与说明，不 404、不张冠李戴。
- **缓存**：键 = `(entry_id, 发布版本号, rights_epoch)`，视图完全由闭包组装，
  因此**旧解释与新例句不可能拼进同一缓存项**（`TestCacheVersionIsolation`）。
- **音频撤权**：媒体内容版本化，但 `media_rights` 是「永远当前」的表——撤权即时
  生效于包括历史版本在内的所有视图；`rights_epoch` 递增使旧缓存键整体失效，
  公开页改播**无音频文本**（transcript）。

## 5. 异步索引：乱序完成安全

- 发布时为每个 active 词条派发 `index_tasks`；后台 worker **随机取任务**，完成顺序
  天然乱序。
- 索引文档按 `(entry_id, version_id)` 命名空间隔离：旧版本的迟到任务写旧命名空间，
  不会覆盖新文档；同任务重跑幂等（先删后插）。
- 检索可见性由**版本水位线** `pointers.indexed` 控制：只有当 ≤v 的所有任务完成，
  水位线才推进到 v。同版本乱序无所谓；跨版本乱序只会推迟可见性，绝不出现
  半新半旧（`TestOutOfOrderIndexing` 含过期任务重放场景）。

## 6. 角色与公开页

- 服务端逐端点强制：`editor` 改草稿；`approver` 发布与撤权；公开端点只读发布闭包。
  编辑调 `/api/publish` 得 403（`TestRoles` + HTTP 集成测试）。
- 公开页：词条可挂 `context_note`（尊重性语境说明，如旧时詈语的时代/语域提示）；
  音频被撤权或缺失时显示 transcript 无音频文本。

## 7. 验收场景对照

| 验收项 | 测试 |
|---|---|
| 词条合并再拆分（义项随迁、id 不变、旧深链接不变） | `TestMergeThenSplit` |
| 同音异义身份独立、关系边关联 | `TestHomophonePolysemy` |
| 删除被引用义项（存档+removed/updated） | `TestDeleteCitedSense` |
| 索引任务乱序完成、水位线、过期任务重放 | `TestOutOfOrderIndexing` |
| 音频撤权即时生效（含历史版本）+无音频文本 | `TestAudioRevocation` |
| 标音不可转换→未转换不猜读 | `TestUnconvertible` |
| 简繁/标点/组合字符匹配+可解释高亮 | `TestNormalizationAndHighlight` |
| 缓存版本隔离（旧释不配新例） | `TestCacheVersionIsolation` |
| 编辑/批准角色分离 | `TestRoles`、`test_http.py` |
