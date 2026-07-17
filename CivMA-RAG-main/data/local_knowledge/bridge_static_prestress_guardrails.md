
## 必须遵守的建模约束
- 任务是 FEniCS 2019.1.0 线性静力：自重 + 普通钢筋弥散刚度 + 预应力初应力。
- Coder 必须先读取 `recipe_bridge_static_prestress_guardrails.py`，再写 `temp_scripts/fenics_drafts/current_fenics_script.py`。
- 严禁声称“无法调用工具”后直接写脚本；必须等待 User_Proxy 返回真实 RAG 内容。
- 结果必须输出 `--- FENICS JOB RESULT ---` 后接一行 JSON。

## 关键反幻觉规则
- 不允许把 `top_prestress_cells` 或 `bottom_prestress_cells` 写成主梁单元数。必须真实统计 DG0 预应力数组中等于顶板/底板初应力的单元数。
- 如果 `beam_cells == 0`、`left_pier_cells == 0`、`right_pier_cells == 0`、`top_prestress_cells == 0`、`bottom_prestress_cells == 0`，脚本必须在求解前 `CRITICAL ERROR` 并退出。
- 严禁用超大空隙区低刚度实体参与主方程后再把其结果当结构响应；如果使用 bounding box ghost mesh，必须只在材料区统计应力和关键点，且不得让空隙区控制刚度。更稳妥方式是采用可运行的等效实体梁 + 桥墩模型，透明说明测试网格。
- 关键点位移必须用 `u_sol(Point(...))` 取值，不要猜 dofmap。
- 点求值必须落在材料区；跨中顶板点可用 `(140,0,130.5)` 或靠近顶面材料内部点，避免落在空隙。

## 预应力和荷载规则
- 用户给定分区初应力是顶板约 -4 MPa、底板约 -15 MPa；不要随意改成 -40 MPa、-72 MPa 或更大，除非 Researcher 明确基于真实输出要求校准。
- 底板区应随梁底 `z_bottom(x)` 动态计算：`z_bottom(x) <= z <= z_bottom(x)+2.0`。不能写死为 `118~120`，因为主梁底标高沿 x 变化且有些位置底部大于 120。
- 顶板区为靠近梁顶：`129 <= z <= 131`。
- 预应力弱式按脚本约定保持一致，并用一次真实结果判断符号。不要在连续失败时反复翻转符号。

## 合理性门槛
- 对 72.5+135+72.5 m 预应力混凝土连续刚构测试网格，跨中竖向位移不应是几十米，也通常不应超过 1 m。若 `abs(mid_span_uz_m) > 1.0`，必须判定验证失败。
- 若位移约 0.03~0.20 m 且应力小于 C50 设计强度 22.4 MPa，可进一步判断是否合理。
- `max_von_mises_pa` 必须来自结构材料区，不得来自空隙区、ghost 区或奇异后处理。

## 推荐输出 JSON字段
`converged`, `beam_cells`, `left_pier_cells`, `right_pier_cells`, `top_prestress_cells`, `bottom_prestress_cells`, `mid_span_uz_m`, `side_span_uz_m`, `pier_top_ux_m`, `max_von_mises_pa`, `output_dir`, `output_files`。

