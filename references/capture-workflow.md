# 截图流程

当用户给的是产品、应用、网页或目标位置，而不是现成截图时，使用这份流程。

## 截图来源

- 桌面应用或用户明确要求 Computer Use：优先使用 Computer Use。
- 浏览器目标、localhost 或文件 URL：使用可用的浏览器自动化工具；Playwright 更安全、更稳定时可作为兜底。
- 用户已提供截图：跳过截图步骤，直接标注提供的文件。

## 鼠标指针截图前门禁

1. 在操作界面前确定实际采集范围。全窗或全屏截图以原始画面为准；计划后期裁切不等于截图时指针已移出采集范围。
2. 完成最后一次点击、拖拽或悬停后，用当前工具的鼠标移动能力把指针移到采集范围之外。工具不能单独移动指针时，只能在范围外有安全空白位置且不会改变页面状态的情况下，用无副作用点击移开；随后重新确认窗口、弹窗和目标控件仍处于预期状态。
3. 无法安全移出指针时，调整实际采集范围或改用不包含指针的截图能力。不要先截下带指针的画面，再靠涂抹、生成式擦除或仅裁掉教学内容来冒充合格截图。
4. 截图后立即打开原图，检查指针、指针阴影和悬停效果是否意外入镜；再检查最终裁切范围。发现非教学所需指针就重截，并保留旧图作为未采用素材，不进入标注和分享链。
5. 只有教程明确在教鼠标位置、悬停或拖拽时，才可有意保留指针，并在截图计划与人工复核中说明其教学作用。用户提供且无法重拍的截图若有干扰指针，只能作参考或请用户补图，不伪称已通过采集门禁。

## 安全边界

- 点击、输入、导航或截图前，确认当前窗口属于本任务。
- 如果窗口焦点被用户切走，暂停高风险操作。
- 避免截入无关隐私内容。
- 除非用户明确要求，不要在截图过程中提交表单、删除内容、切换账号或发送消息。

## 输出目录

- 用户指定文件夹时，把计划 JSON、标注图、QA 裁切图、contact sheet 和 review 文件都放进去。
- 用户未指定时，创建：

```text
~/Desktop/screenshot-annotations/YYYYMMDD-HHMMSS/
```

## 推荐产物结构

普通截图标注任务可使用简洁结构：

```text
output-folder/
├── annotation-plan.json
├── annotated-step-1.png
├── annotated-step-2.png
└── qa/
    ├── qa-1.png
    ├── qa-2.png
    ├── qa-contact-sheet.png
    ├── review.json
    └── qa-summary.json
```

产品教程文档任务使用结构化交付摘要；完整目录、资产清单和 QA 字段以 `references/tutorial-document-workflow.md` 为准：

```text
output-folder/
├── README.md
├── tutorial.md
├── tutorial-test-path.md
├── tutorial-data-plan.md
├── raw/
├── plans/
├── annotated/
└── qa/
    ├── full-annotated-contact-sheet.png
    └── manual-review-summary.json
```

详细流程见 `references/tutorial-document-workflow.md`。

批量素材、流程排序、隐私继承、长图或多列总览使用 v2 版本目录，不要把它和上面的 v1 简洁结构混用：

```text
output/job-id/
├── versions/
│   └── v001/
│       ├── manifest.json
│       ├── sources/
│       ├── plans/resolved-plan.json
│       ├── annotated/
│       ├── overview/
│       └── qa/
└── reviews/
```

完整字段和版本规则见 `references/batch-plan-v2.md`。
