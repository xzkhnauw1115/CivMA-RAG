# 本项目失败案例复盘

本文件来自本项目历史运行统计和日志，不是外部猜测。Coder 修复时应先匹配这些失败模式，再完整重写脚本。

## 检索关键词总览

FEniCS 2019 脚本 代码 失败案例 错误修复 cell_dofs Expression UserExpression out_dir output_dir 截断 END_OF_SCRIPT 求解器 收敛 mumps boundary condition 物理验证 验证失败 空壳脚本 RAG。

## 失败模式 1：输出目录变量混用

历史错误：
```text
NameError: name 'out_dir' is not defined
```

原因：
- 脚本前面定义 `output_dir`，后面写文件时使用 `out_dir`。

稳定修复：
- 全脚本只保留 `output_dir`。
- 所有文件写入都使用 `os.path.join(output_dir, filename)`。

## 失败模式 2：DG0 / cell_dofs 接口误用

历史错误：
```text
TypeError: cell_dofs(): incompatible function arguments
```

原因：
- 对 DG0 单元常数空间过度使用 `dofmap().cell_dofs()`。
- 传入参数类型不符合 dolfin 2019 接口。

稳定修复：
- 对 DG0 每单元常数，使用 `cell.index()` 写向量。
- 写入后 `vector().apply("insert")`。

## 失败模式 3：Expression 无法 evaluate

历史错误：
```text
*** Error: Unable to evaluate expression.
```

原因：
- `UserExpression.eval()` 分量未全部赋值。
- `value_shape()` 不匹配。
- 点取值落在网格外。

稳定修复：
- 分区常量优先改为 DG0 Function。
- 若必须 `UserExpression`，必须给所有分量赋值并定义 `value_shape()`。
- 关键点取值必须落在实体内部。

## 失败模式 4：脚本截断

历史错误：
```text
missing # END_OF_SCRIPT marker; script may be truncated
```

原因：
- LLM 输出脚本过长被截断。
- Coder 追加了不完整代码。

稳定修复：
- 写入固定文件前先 RAG。
- `reset_current_fenics_script()` 后写完整脚本。
- 最后一行放 `# END_OF_SCRIPT`。
- 运行前检查 `get_current_fenics_script_status()`。

## 失败模式 5：空壳脚本被运行

历史现象：
- `current_fenics_script.py` 只剩 `# END_OF_SCRIPT`。
- 后端仍提交了 16 字符脚本。

已加系统保护：
- `run_fenics_script_file()` 会拒绝过短脚本。
- Coder 不得只追加尾标记。

Coder 注意：
- `# END_OF_SCRIPT` 是完整性标记，不是脚本内容。
- 写入后必须确认 `lines` 和 `chars` 合理。

## 失败模式 6：仿真失败却被误判物理通过

历史现象：
- WSL/FEniCS 执行失败，但 Researcher 因脚本形式完整而写“验证通过”。

已加系统保护：
- `record_physics_validation(True, ...)` 会检查最近一次仿真是否成功。
- 最近一次仿真未成功时，会自动改记为验证失败。

Researcher 注意：
- “脚本结构正确”不等于“物理验证通过”。
- 必须基于最新仿真结果 JSON 和输出文件验证。

## 失败模式 7：RAG 未真正参与生成

历史现象：
- Coder 没读 RAG 就直接 `reset_current_fenics_script()`。
- Coder 把 RAG 调用和脚本写入放在同一代码块。

已加系统保护：
- 未先读取专用 RAG，脚本工具会被拒绝。
- RAG 工具和脚本工具混在一个代码块会被拒绝。
- 伪造 RAG 输出会被拒绝。

Coder 正确流程：
```python
print(get_golden_scripts("recipe_bridge_static_prestress_guardrails.py"))
```

等待 User_Proxy 返回后，再单独：
```python
print(local_search("FEniCS 2019 DG0 初应力 错误修复 物理验证"))
```

等待返回后，最后再写脚本和运行。


