# 主动知识库索引 / Active RAG Knowledge Index

本目录只保留会直接参与 Agent 检索学习的知识。不得把旧教程、泛化 demo、不同桥梁几何或旧输出协议当作主模板。

## 全局规则

1. `.md` 知识卡用 `local_search(filename="xxx.md")` 精准读取。
2. `.py` 模板用 `get_golden_scripts(filename="xxx.py")` 精准读取。
3. Coder 写代码前必须先读当前分析类型的专用规则卡；若没有专用规则卡，再读通用稳定模式。
4. 生成脚本必须写入 `temp_scripts/fenics_drafts/current_fenics_script.py`，聊天里只显示摘要和工具返回。
5. Researcher 做验证时必须读 `physics_validation_rules.md`，不能只看脚本“运行成功”。

## 当前主动知识文件

### 通用稳定规则

- `fenics2019_reliable_patterns.md`
  - 适用：FEniCS 2019 / old dolfin 稳定 API、DG0 分区、显式组装、PVD/JSON 输出。
- `fenics2019_error_fix_matrix.md`
  - 适用：FEniCS 2019 常见错误到完整重写策略。
- `failure_case_review.md`
  - 适用：历史失败教训，尤其是半截脚本、伪结果、RAG 无效、输出格式错误。
- `physics_validation_rules.md`
  - 适用：Researcher 验证仿真是否物理可信。

### 悬臂施工 / 幽灵单元法

- `fenics2019_ghost_element_staged_construction.md`
- `golden_scripts/recipe_fenics2019_ghost_staged_static.py`

关键点：DG0 active/ghost 材料场、阶段独立求解、阶段位移 CSV、最终阶段 PVD。

### 自重 + 配筋 + 预应力初应力

- `bridge_static_prestress_guardrails.md`
- `golden_scripts/recipe_bridge_static_prestress_guardrails.py`
- 可辅助：`golden_scripts/recipe_fenics2019_3d_elastic_dg0_initial_stress.py`

关键点：sigma0 是二阶张量；顶/底板单元动态按 `z_bottom(x)` 分类；输出 `--- FENICS JOB RESULT ---` JSON。

### 温度梯度热-力耦合

- `bridge_thermal_coupling_guardrails.md`
- `golden_scripts/recipe_bridge_thermal_coupling_guardrails.py`

关键点：热荷载用弱形式 `+ inner(sigma_th, eps(v))*dx`，不要把 DG0 热应力拿去直接 `div`。

### 高墩几何非线性 P-Delta

- `bridge_pdelta_guardrails.md`
- `golden_scripts/recipe_bridge_pdelta_guardrails.py`

关键点：预应力用等效外荷载法；`w_net` 从 N/m 转 N/m3；必须同时求线性与非线性并输出 P-Delta 放大系数。

### 移动荷载动力响应 / Newmark-beta

- `bridge_newmark_dynamics_guardrails.md`
- `golden_scripts/recipe_bridge_newmark_dynamics.py`

关键点：质量矩阵必须含密度；先求自重静力初始状态；40 步 Newmark；移动荷载必须有非零顶面 facet 或体积正则化单元。

### Scanlan 简化颤振稳定性扫描

- `bridge_flutter_scan_guardrails.md`
- `bridge_eigen_solver_ladder.md`
- `golden_scripts/recipe_bridge_flutter_scan.py`

关键点：先通过零风速基准模态烟测，再做风速扫描；气动刚度必须装配成双线性矩阵；3D SLEPc 不收敛时切换到 1D 等效模态代理并诚实报告 `model_level`。

### SHM 刚度反演 / 损伤识别参数扫描

- `bridge_shm_damage_guardrails.md`
- `golden_scripts/recipe_bridge_shm_damage_scan.py`

关键点：损伤坐标以 `x in [60,90]` 为准，不得移动到 `x=140`；质量矩阵不随 alpha 折减；优先使用 Hermite 梁单元矩阵代理，必须检查频率下降单调性。

