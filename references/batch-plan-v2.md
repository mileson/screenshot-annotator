# v2 批处理计划

## 目录

- [用途](#用途)
- [顶层字段](#顶层字段)
- [隐私对象](#隐私对象)
- [流程对象](#流程对象)
- [总览对象](#总览对象)
- [资产对象](#资产对象)
- [标注对象](#标注对象)
- [可选坐标网格](#可选坐标网格)
- [输出约定](#输出约定)

## 用途

`batch-plan-v2.json` 是 v2 引擎的任务清单。顶层必须包含 `schema_version`、`job_id`、`style`、`privacy`、`flow`、`overview` 和 `assets`；可选 `qa`。示例见 `examples/batch-plan-v2.json`。

计划只描述任务，不修改源图。引擎应在新的版本目录中复制源图并生成标注图、总览和 QA 产物。

## 顶层字段

| 字段 | 要求 | 说明 |
| --- | --- | --- |
| `schema_version` | 必填 | 固定为 `"2.0"`。 |
| `job_id` | 必填 | 使用稳定、可读且不含个人信息的任务标识。 |
| `style` | 必填 | v2 至少提供 `preset`；默认使用 `action-orange-v2`，新版红色文字标签使用 `action-red-v2`。`classic-red-v1` 只由旧版单图入口使用。 |
| `privacy` | 必填 | 声明源图、副本、敏感信息和水印的处理边界。 |
| `flow` | 必填 | 声明真实页面顺序和页面之间的边。 |
| `overview` | 必填 | 声明总览布局和像素预算。 |
| `qa` | 可选 | 声明辅助网格等 QA 产物，不改变生产标注。 |
| `assets` | 必填 | 非空截图数组。 |

## 隐私对象

推荐使用以下值：

```json
{
  "status": "not_required",
  "required": false,
  "source_handling": "read_only_copy",
  "sensitive_data": "inherit",
  "watermark": "preserve_unknown",
  "authorized_watermark_removal": false,
  "upstream_audit": null
}
```

- `status`：上游审计状态；合成素材不需要审计时可用 `not_required`。
- `required`：是否必须提供上游隐私审计。
- `read_only_copy`：不覆盖源图，只在任务版本目录处理副本。
- `inherit`：衍生图继承源图的敏感级别和传播限制。
- `preserve_unknown`：未知版权水印不得自动去除。
- 只有自有或明确授权的水印，并且能够确定性冲底时，才把 `authorized_watermark_removal` 设为 `true`；仍需保留授权和变换记录。
- `upstream_audit`：单素材审计、包含逐素材 `reports` 的批量审计，或等价内联 object。`required: true` 时必须存在。
- `upstream_audits`：多素材时推荐使用的 `{asset_id: 审计路径或 object}` 映射；不能和 `upstream_audit` 同时声明。
- 每张审计的 `output_sha256` 必须等于当前待标注脱敏副本的 SHA-256。`source_sha256` 只表示脱敏前原图的来源链，不能用它给当前资产放行。

需要上游审计时，`status` 必须由审计证据得出，不能在没有审计文件或输出哈希绑定时手写为 `passed`。多资产任务必须逐一覆盖每个 asset ID。

若声明已移除自有或已授权水印，还必须提供 `watermark_audit` 或逐素材 `watermark_audits`。每张报告必须明确 `passed: true`、`authorized: true`、`deterministic: true`、`outside_changed_pixels: 0`，并以 `output_sha256` 绑定当前资产。

## 流程对象

`flow.order` 给出资产的语义顺序。`flow.edges` 描述真正的页面衔接，不得从文件名字典序推断流程。

`flow.numbering_scope` 可选：

- `job`：默认值，整套任务连续编号，保持旧计划兼容
- `section`：按资产的 `section_id` 分别编号，每个章节都从 `1` 和 `A` 重新开始

选择 `section` 时，每张资产必须提供非空 `section_id`，否则确定性校验阻断。

每条边包含：

- `from`：起点资产 `id`。
- `callout_id`：触发跳转的标注 `id`。
- `to`：终点资产 `id`。
- `kind`：`next` 表示连续步骤，`alternative` 表示互斥入口。

`callout_id` 必填。`next` 只能由 `single/sequence` 标注触发，`alternative` 只能由 `alternative` 标注触发。同一 callout 不得指向多个目标，边不得重复、自环或形成流程环路。

互斥入口在图内使用 `A`、`B`，真实连续步骤使用数字。内部页码、资产索引和跳转编号只留在清单中，不得写进图内动作标签。

## 总览对象

`overview.mode` 允许：

- `auto`：根据流程和预算选择布局。
- `vertical`：线性流程使用纵向长图。
- `grid`：总览使用网格，`columns` 必须为 4 至 7，并自动换行。

`overview.max_pixels` 是单张总览的总像素预算，`overview.max_page_height` 是单张分片的最大高度。`overview.min_label_font_px` 是总览中标签的最小有效字号，默认 14 px。预计画布超出预算或有效字号低于下限时必须分片或调整布局；不得通过拉伸目标、裁切内容或不可读缩放规避预算。

## 资产对象

每个资产至少包含：

- `id`：流程和 QA 中使用的稳定语义标识。
- `source`：源图路径。文件只读，处理前复制进版本目录。

可选字段：

- `output_name`：语义化输出文件名，不使用 `page-01.png` 一类生产页码；v2 必须以 `.png` 结尾，以便执行无损像素门禁。
- `title`：面向复核者的中文标题。
- `section_id`：文章章节或教程部分的稳定标识；`flow.numbering_scope: section` 时必填。
- `kind`：建议使用 `sequential_action`、`alternative`、`information`、`result` 或 `reference`。
- `protected_regions`：要保护的标题、必填星号、按钮、红字或关键值。
- `annotations`：该图的标注数组。

## 标注对象

每条标注必须包含：

- `id`：稳定语义标识。
- `rect`：原始截图像素坐标 `[x1, y1, x2, y2]`。
- `semantic_role`：`single`、`sequence` 或 `alternative`。
- `callout`：动作标签对象，其中 `text` 为必填的简体中文动作或结果标签。
- `target_contract`：可选目标完整性契约；旧计划省略时保持兼容。

`callout` 内的可选控制：

- `callout.marker`：默认 `auto`。`single` 只允许 `auto/none`，`sequence` 只允许 `auto/number`，`alternative` 只允许 `auto/letter`；不得用手动 marker 绕过语义规则。
- `callout.anchor`：`auto`、`bottom_right`、`inside_top_right`、`top_right`、`bottom_left`、`top_left`、`inside_bottom_right`、`inside_top_left` 或 `manual`。
- `callout.label_rect`：仅当 `callout.anchor` 为 `manual` 时必填。

canonical v2 只在 `callout` 中写这三个字段。引擎可宽容读取旧计划的标注顶层字段，但解析后必须规范化到 `callout`，新示例不得继续示范旧写法。

`target_contract` 可包含：

- `target_kind`：`bottom_nav_item`、`menu_row`、`option_row`、`action_button`、`file_result_row`、`message_row`、`composite_step` 或 `custom`。
- `boundary_tolerance_px`：0 至 100 的边界容差。
- `must_include`：目标框必须完整覆盖的 `id`、`role`、`rect`。
- `must_exclude`：目标框不得相交的相邻控件。

场景必要角色、完整示例和候选框辅助见 `references/target-contract-and-scene-templates.md`。

自动标记规则：

- `single` 默认不显示编号。
- `sequence` 按真实操作顺序显示数字，即使同一张图只有一个连续步骤也保留真实顺序。
- `alternative` 使用 `A`、`B`，只表示互斥入口。

标签位置由最少遮挡策略决定。右下、框内右上白区、`A` 左上和 `B` 左下都是候选，不是固定位置。

## 可选坐标网格

可选顶层对象 `qa` 可用 `qa.coordinate_grids` 生成 100 px 辅助网格：

- `false` 或省略：关闭。
- `true`：为所有可渲染资产生成。
- 资产 `id` 数组：只为指定资产生成。
- `{"enabled": true, "assets": ["资产标识"], "step": 100}`：显式指定资产和间距。

网格只用于人工核对，不参与目标框吸附、缩放或重塑。

## 输出约定

v2 引擎在新的 `vNNN/` 目录中生成：

解析并规范化后的清单固定写入 `plans/resolved-plan.json`。

```text
vNNN/
├── manifest.json
├── plans/
│   └── resolved-plan.json
├── sources/
├── annotated/
├── overview/
└── qa/
    ├── crops/
    │   └── <asset-id>/<annotation-id>.png
    └── full-contact-sheet.png
```

版本目录旁还会创建 `reviews/vNNN/review-001.json`。它绑定最终 `manifest.json` 哈希；后续复核只追加连续编号的 review 文件，不覆盖旧记录。

每次返修创建最小差异的新版本或仅替换明确失败的任务资产，不覆盖源图，也不把未签核的自动 QA 当成最终通过。
