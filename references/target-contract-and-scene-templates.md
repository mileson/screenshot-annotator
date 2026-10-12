# 目标契约与场景辅助识别

`target_contract` 用于把“圈选目标”从一个自由矩形提升为可验证的视觉操作单元。旧计划不包含该字段时保持兼容。

```json
{
  "target_contract": {
    "target_kind": "bottom_nav_item",
    "boundary_tolerance_px": 8,
    "must_include": [
      {"id": "container", "role": "container", "rect": [820, 2100, 1170, 2380]},
      {"id": "icon", "role": "icon", "rect": [980, 2140, 1080, 2230]},
      {"id": "label", "role": "label", "rect": [990, 2250, 1080, 2310]}
    ],
    "must_exclude": [
      {"id": "adjacent-tab", "role": "adjacent_tab", "rect": [620, 2100, 815, 2380]}
    ]
  }
}
```

## 场景类型

- `bottom_nav_item`：必须包含 `container`、`icon`、`label`。
- `menu_row`：必须包含 `icon`、`label`。
- `option_row`：必须包含选项 `label`。
- `action_button`：必须包含 `button`。
- `file_result_row`：必须包含 `file_row`。
- `message_row`：必须包含 `message_row`。
- `composite_step`：按实际依赖声明必要区域。
- `custom`：自定义 include / exclude。

## 门禁

- 每个 `must_include` 的覆盖率必须为 100%；允许 `boundary_tolerance_px` 提供小范围容差。
- 每个 `must_exclude` 与目标框的重叠面积必须为 0。
- 场景必要角色缺失时，计划解析阶段直接失败。
- 引擎把覆盖率和排除重叠写入 `resolved-plan.json`；独立 validator 会重新计算，不能用篡改的检查结果绕过。

## 候选框辅助

```bash
python3 scripts/suggest_target_rect.py input.png \
  --scene bottom_nav_item \
  --container-box 20 2100 1160 2380 \
  --index 3 --count 4
```

当前辅助器支持：

- 等宽底部导航分区。
- 菜单、选项、文件和消息行的横向边界对比。
- 彩色按钮连通域。

辅助器只输出候选框、置信度和像素依据，不直接渲染。低置信度时应结合 OCR 坐标、100 px 网格、局部放大和人工复核。

底部导航的等宽分区只按几何结构给出中等置信度候选。若激活项存在不等宽点击区、浮动头像或特殊圆角背景，必须以完整视觉容器和 `must_include` 契约为准，不能把等宽计算当成最终边界。
