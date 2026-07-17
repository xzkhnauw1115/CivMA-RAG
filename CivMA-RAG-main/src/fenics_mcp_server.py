"""
FEniCS仿真服务器
提供与FEniCSMCPServer兼容的API接口，但使用FEniCS作为求解器
"""

import os
import json
import time
import threading
import numpy as np
import subprocess
import sys
import logging
import ast
from typing import Dict, Tuple, Optional, Any, List
from flask import Flask, request, jsonify

# 尝试导入 pyflakes 用于高级语法检查
try:
    from pyflakes import api as pyflakes_api
    from pyflakes import reporter as pyflakes_reporter
    HAS_PYFLAKES = True
except ImportError:
    HAS_PYFLAKES = False

# 设置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class FenicsResultParser:
    """FEniCS结果解析器"""
    
    def __init__(self):
        self.supported_outputs = [
            "displacement", "stress", "strain", "reaction_force"
        ]

    def parse_fenics_output(self, work_dir: str) -> Dict[str, Any]:
        """解析FEniCS输出"""
        results = {
            "converged": False,  # 默认未收敛
            "outputs": {}
        }

        # 检查标准输出文件
        stdout_file = os.path.join(work_dir, "stdout.log")
        json_result = None
        
        if os.path.exists(stdout_file):
            with open(stdout_file) as f:
                stdout_content = f.read()
                results["stdout"] = stdout_content
                
                # 尝试从stdout中提取JSON结果
                try:
                    # 查找JSON开始和结束位置
                    start_idx = stdout_content.find('--- FENICS JOB RESULT ---')
                    if start_idx != -1:
                        after_marker = stdout_content[start_idx + len('--- FENICS JOB RESULT ---'):].strip()
                        # 跳过空行，找到第一个非空行
                        for line in after_marker.splitlines():
                            line = line.strip()
                            if line:
                                json_result = json.loads(line)
                                break
                except json.JSONDecodeError as e:
                    logger.error(f"JSON解析失败: {e}")
        
                # 尝试从 stdout 中提取（可能为多行缩进的）JSON 结果
                marker = '--- FENICS JOB RESULT ---'
                start_idx = stdout_content.find(marker)
                if start_idx != -1:
                    after_marker = stdout_content[start_idx + len(marker):]
                    lines = after_marker.splitlines()
                    # 跳过前导空行，定位 JSON 起始 '{' 或 '['
                    idx = 0
                    while idx < len(lines) and not lines[idx].strip():
                        idx += 1
                    # 自适应多行 JSON（括号计数匹配）
                    if idx < len(lines):
                        first = lines[idx].lstrip()
                        if first.startswith('{') or first.startswith('['):
                            brace_count = first.count('{') - first.count('}')
                            bracket_count = first.count('[') - first.count(']')
                            collected = [first]
                            j = idx + 1
                            # 继续收集直至括号/方括号平衡
                            while (brace_count > 0 or bracket_count > 0) and j < len(lines):
                                l = lines[j]
                                collected.append(l)
                                brace_count += l.count('{') - l.count('}')
                                bracket_count += l.count('[') - l.count(']')
                                j += 1
                            potential_json = '\n'.join(collected).strip()
                            # 去除尾部可能的多余文本（如随后空行、中文说明）
                            # 简单策略：若后续第一行是 '分析完成' 等非 JSON 内容则忽略
                            # 尝试解析
                            try:
                                json_result = json.loads(potential_json)
                            except json.JSONDecodeError as e:
                                logger.error(f"多行JSON解析失败: {e}; 片段预览: {potential_json[:120]}...")
                        else:
                            logger.warning("JSON起始行未找到 '{' 或 '['，跳过解析。")
                else:
                    logger.warning("输出中未找到结果标记 '--- FENICS JOB RESULT ---'")
        
        # 如果成功解析JSON，则使用解析结果（支持对象或数组）
        if json_result is not None:
            try:
                if isinstance(json_result, dict):
                    results.update(json_result)
                elif isinstance(json_result, list):
                    # 视为多个工况结果数组
                    results['cases'] = json_result
                    # 计算最大挠度
                    defls = [c.get('deflection_m') for c in json_result if isinstance(c, dict) and isinstance(c.get('deflection_m'), (int, float))]
                    if defls:
                        max_defl = max(defls)
                        results.setdefault('outputs', {})
                        if isinstance(results['outputs'], dict):
                            results['outputs'].setdefault('displacement', {})
                            results['outputs']['displacement'].update({'max': max_defl, 'unit': 'm'})
                        results['max_displacement_m'] = max_defl
                        results['fenics_simulation'] = True
                # 标记收敛（宽松）
                if "converged" not in results:
                    results["converged"] = True
                # 追加中文键映射
                if isinstance(json_result, dict):
                    try:
                        if '最大合位移' in results and isinstance(results['最大合位移'], (int, float)):
                            val = float(results['最大合位移'])
                            if 0 <= val < 1e6:
                                results.setdefault('outputs', {})
                                if isinstance(results['outputs'], dict):
                                    results['outputs'].setdefault('displacement', {})
                                    results['outputs']['displacement'].update({'max': val, 'unit': 'm'})
                                results['max_displacement_m'] = val
                                results['fenics_simulation'] = True
                        for stress_key in ['最大von Mises应力', '最大vonMises应力']:
                            if stress_key in results and isinstance(results[stress_key], (int, float)):
                                sval = float(results[stress_key])
                                if 0 <= sval < 1e12:
                                    results.setdefault('outputs', {})
                                    if isinstance(results['outputs'], dict):
                                        results['outputs'].setdefault('stress', {})
                                        results['outputs']['stress'].update({'max': sval, 'unit': 'Pa'})
                                    results['fenics_simulation'] = True
                                    break
                        if '安全系数' in results and isinstance(results['安全系数'], (int, float)):
                            results.setdefault('outputs', {})
                            if isinstance(results['outputs'], dict):
                                results['outputs'].setdefault('safety_factor', {})
                                results['outputs']['safety_factor'].update({'value': float(results['安全系数'])})
                    except Exception as _map_e:
                        logger.warning(f"JSON中文键映射失败: {_map_e}")
            except Exception as e:
                logger.warning(f"解析JSON结果时出现异常(忽略继续): {e}")
        else:
            logger.warning("未找到有效的JSON结果，使用原始输出")

        # ---- 后处理增强：若缺失 outputs.displacement / stress，则尝试从 stdout 解析 ----
        try:
            outputs = results.setdefault('outputs', {}) if isinstance(results, dict) else {}
            if isinstance(outputs, dict):
                # 位移提取（改进：识别“最大合位移/最大位移”并转换为 m，同时补顶层核心指标）
                if 'displacement' not in outputs or not outputs.get('displacement', {}).get('max'):
                    disp_info = self._extract_displacement(stdout_content if 'stdout_content' in locals() else '')
                    if disp_info:
                        outputs.setdefault('displacement', {}).update(disp_info)
                        # 补充顶层核心指标 (m)
                        if 'max_m' in disp_info:
                            results['max_displacement_m'] = disp_info['max_m']
                # 若JSON结果里已有最大位移字段但未映射到统一键，也尝试映射
                if 'max_displacement' in results and 'max_displacement_m' not in results:
                    try:
                        val = float(results['max_displacement'])
                        results['max_displacement_m'] = val
                    except Exception:
                        pass
                # 简单应力解析：匹配 "最大应力" 或 "Max stress" 数字 (MPa 优先)
                if 'stress' not in outputs or not outputs.get('stress', {}).get('max'):
                    import re
                    pattern = r'(最大应力|Max\s*stress)[:：\s]+([0-9]+\.?[0-9]*)\s*(MPa|Pa)?'
                    m = re.search(pattern, stdout_content, re.IGNORECASE)
                    if m:
                        val = float(m.group(2))
                        unit = (m.group(3) or '').lower()
                        # 统一转为 Pa
                        if unit == 'mpa':
                            val_pa = val * 1e6
                        else:
                            val_pa = val
                        outputs.setdefault('stress', {}).update({'max': val_pa, 'unit': 'Pa'})
                # 若解析到任何核心输出，认为是有效仿真
                if any(k in results for k in ['max_displacement_m']) or 'displacement' in outputs:
                    results.setdefault('fenics_simulation', True)
        except Exception as e:
            logger.warning(f"后处理解析输出字段失败: {e}")

        # 检查可视化文件
        plot_files = [f for f in os.listdir(work_dir) if f.endswith(".png")]
        if plot_files:
            results["visualizations"] = plot_files

        # 保存结果到文件
        result_file = os.path.join(work_dir, "results.json")
        try:
            with open(result_file, 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            logger.info(f"结果已保存到: {result_file}")
        except Exception as e:
            logger.error(f"保存结果失败: {e}")

        return results

    def _extract_displacement(self, content: str) -> Dict[str, Any]:
        """从输出中提取位移数据"""
        try:
            import re
            lines = content.split('\n') if content else []
            # 允许 JSON 键中出现引号："最大合位移": 123.4
            pattern = re.compile(r'最大(合)?位移"?[:：]\s*([0-9.eE+-]+)\s*(m|mm)?')
            for line in lines:
                m = pattern.search(line)
                if m:
                    val = float(m.group(2))
                    unit = (m.group(3) or 'm').lower()
                    if unit == 'mm':
                        val_m = val / 1000.0
                    else:
                        val_m = val
                    return {
                        'max': val_m,  # 统一内部以 m 存储
                        'unit': 'm',
                        'max_m': val_m,
                        'nodes': []
                    }
        except Exception as e:
            logger.warning(f"解析位移数据时出错: {e}")
        return {}

def _load_project_config() -> Dict[str, Any]:
    config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'config.json')
    try:
        with open(config_path, 'r', encoding='utf-8-sig') as f:
            return json.load(f)
    except Exception as exc:
        logger.warning(f"无法加载配置文件 {config_path}: {exc}")
        return {}


class FenicsMCPServer:
    def __init__(self, host: str = 'localhost', port: int = 5000):
        self.host = host
        self.port = port
        config = _load_project_config()
        self.fenics_runtime = config.get("fenics_runtime", {})
        self.task_queue = []
        self.completed_tasks = {}
        self.lock = threading.Lock()

        # 创建Flask应用实例
        self.app = Flask(__name__)
        self._setup_routes()

        # 初始化结果解析器
        self.result_parser = FenicsResultParser()

        self.project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.jobs_root = os.path.join(self.project_root, "fenics_jobs")
        self.temp_scripts_root = os.path.join(self.project_root, "temp_scripts")
        os.makedirs(self.jobs_root, exist_ok=True)
        os.makedirs(self.temp_scripts_root, exist_ok=True)

    # 启动工作线程
        self.worker_thread = threading.Thread(target=self.worker, daemon=True)
        self.worker_thread.start()

    # 注：已彻底移除任何模板兜底与程序化脚本改写，按原样执行LLM生成的脚本。

    def _setup_routes(self):
        """设置Flask路由"""
        
        @self.app.route('/status', methods=['GET'])
        def server_status():
            return jsonify({
                "status": "running",
                "version": "1.0",
                "solver": "FEniCS"
            })

        @self.app.route('/status/<job_id>', methods=['GET'])
        def job_status(job_id):
            logger.info(f"查询作业状态，ID: '{job_id}'")
            with self.lock:
                task = self.completed_tasks.get(job_id, None)
            
            if task:
                return jsonify(task)
            else:
                # 检查是否仍在队列中
                with self.lock:
                    for queued_job_id, _ in self.task_queue:
                        if queued_job_id == job_id:
                            return jsonify({"status": "queued"})
                # 增加日志，表明找不到任务
                logger.warning(f"找不到作业ID为 '{job_id}' 的任务。返回 'processing'。")
                return jsonify({"status": "processing"}), 200

        @self.app.route('/submit', methods=['POST'])
        def submit_job():
            data = request.json
            if not data or "script" not in data:
                return jsonify({"error": "Invalid request"}), 400

            job_id = f"fenics_{int(time.time())}"
            script_content = data["script"]
            # 注意：不在此处净化脚本，避免队列中的代码与最终执行代码不一致导致行号错位；
            # 统一在 worker 执行阶段进行 sanitize + autofix，并在结果中返回最终执行脚本与变换信息。

            with self.lock:
                self.task_queue.append((job_id, script_content))

            return jsonify({
                "job_id": job_id,
                "status": "queued"
            })

        @self.app.route('/result/<job_id>', methods=['GET'])
        def get_result(job_id):
            with self.lock:
                task = self.completed_tasks.get(job_id, None)
            
            if task:
                return jsonify(task)
            else:
                return jsonify({"error": "Job not found"}), 404

    def run(self) -> None:
        """启动Flask服务器"""
        logger.info(f"启动FEniCS服务器: http://{self.host}:{self.port}")
        self.app.run(host=self.host, port=self.port, threaded=True)

    def validate_and_fix_script(self, script_content: str) -> Tuple[str, List[str]]:
        """
        Strict validation only. Do not auto-fix, auto-close, comment out, add imports,
        or append success markers. The agent workflow owns the fixed script file and
        must provide a complete script ending with # END_OF_SCRIPT.
        """
        fixes = []
        if "# END_OF_SCRIPT" not in script_content:
            fixes.append("Rejected script without # END_OF_SCRIPT marker")
            rejected = (
                'raise RuntimeError("Missing # END_OF_SCRIPT marker; '
                'refusing to run incomplete or non-fixed-script workflow input")\n'
            )
            return rejected, fixes

        try:
            ast.parse(script_content)
        except SyntaxError as exc:
            fixes.append(f"Syntax validation failed at line {exc.lineno}: {exc.msg}")
            return script_content, fixes

        return script_content, fixes

    def run_fenics_job(self, job_id: str, script_content: str):
        """执行FEniCS作业 - 在WSL2中运行真实仿真"""
        work_dir = os.path.join(self.jobs_root, job_id)
        os.makedirs(work_dir, exist_ok=True)

        # 预处理：严格校验脚本，不做自动修复
        clean_code, fixes = self.validate_and_fix_script(script_content)
        if fixes:
            logger.info(f"作业 {job_id} 脚本校验提示: {fixes}")

        script_file_path = os.path.join(work_dir, f"{job_id}.py")
        with open(script_file_path, "w", encoding='utf-8') as f:
            f.write(clean_code)

        try:
            start_time = time.time()

            # Windows 路径转 WSL
            wsl_script_path = self.windows_to_wsl_path(script_file_path)
            wsl_work_dir = self.windows_to_wsl_path(work_dir)
            wsl_distro = self.fenics_runtime.get("wsl_distro", "Ubuntu")
            conda_env = self.fenics_runtime.get("conda_env", "fenics")
            python_command = self.fenics_runtime.get("python_command", "python")
            fallback_python = self.fenics_runtime.get("fallback_python_command", "python3")
            timeout_sec = int(self.fenics_runtime.get("job_timeout_sec", 1800))

            # 在WSL中切换到工作目录后执行脚本，确保输出文件保存在正确位置。
            # 优先自动寻找 Miniconda/Anaconda 并激活 conda_env；找不到则使用 python3。
            wsl_command = (
                "CONDA_SH=''; "
                "for p in \"$HOME/miniconda3/etc/profile.d/conda.sh\" \"$HOME/anaconda3/etc/profile.d/conda.sh\" \"/opt/conda/etc/profile.d/conda.sh\"; do "
                "if [ -f \"$p\" ]; then CONDA_SH=\"$p\"; break; fi; "
                "done; "
                f"if [ -n \"$CONDA_SH\" ]; then source \"$CONDA_SH\" && conda activate '{conda_env}' && cd '{wsl_work_dir}' && {python_command} -u '{wsl_script_path}'; "
                f"else cd '{wsl_work_dir}' && {fallback_python} -u '{wsl_script_path}'; fi"
            )
            cmd = ['wsl', '-d', wsl_distro, '-e', 'bash', '-lc', wsl_command]

            logger.info(f"执行FEniCS作业 {job_id}...")
            logger.debug(f"工作目录(WSL): {wsl_work_dir}")
            logger.debug(f"WSL命令: {wsl_command}")

            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=timeout_sec
            )

            # 写出 stdout/stderr
            stdout_file = os.path.join(work_dir, "stdout.log")
            with open(stdout_file, "w", encoding='utf-8') as log_f:
                log_f.write(process.stdout or "")
                if process.stderr:
                    log_f.write("\n--- STDERR ---\n")
                    log_f.write(process.stderr)

            if process.returncode != 0:
                logger.error(f"WSL命令执行失败，返回码: {process.returncode}")
                logger.error(f"WSL命令标准输出: {process.stdout}")
                logger.error(f"WSL命令标准错误: {process.stderr}")

            results = self.result_parser.parse_fenics_output(work_dir)
            execution_time = time.time() - start_time

            # 将执行脚本与变换信息、错误上下文加入结果，便于上层LLM严格对齐
            try:
                with open(script_file_path, 'r', encoding='utf-8') as _f:
                    executed_preview = _f.read(4000)
            except Exception:
                executed_preview = ""

            transforms = {
                "sanitized": False,
                "autofixed": False,
                "applied": []
            }

            # 解析stderr中的报错文件与行号，提取上下文
            error_context = self._extract_error_context(
                stderr=process.stderr or "",
                primary_path=wsl_script_path,
                alt_path=script_file_path,
                context_lines=3
            ) if (process.returncode != 0) else None

            # 附加辅助对齐信息
            results.setdefault("meta", {})
            results["meta"].update({
                "job_dir": os.path.relpath(work_dir, self.project_root),
                "job_abs_dir": os.path.abspath(work_dir),
                "job_wsl_dir": wsl_work_dir,
                "executed_script_path": os.path.relpath(script_file_path, self.project_root),
                "executed_script_abs_path": os.path.abspath(script_file_path),
                "executed_script_wsl_path": wsl_script_path,
                "transforms": transforms,
                "error_context": error_context
            })

            # 判断是否仅存在非致命的 UserWarning 而无 traceback/ERROR
            stderr_lower = (process.stderr or "").lower()
            stdout_lower = (process.stdout or "").lower()
            
            # 判定核心指标（放宽：映射中文键后 outputs 中有位移/应力或 max_displacement_m 即视为有效）
            has_core_metric = (
                any(k in results for k in ["fenics_simulation", "max_displacement_m", "maximum_bending_moment_Nm", "maximum_shear_force_N"]) or
                (isinstance(results.get('outputs'), dict) and any(k in results['outputs'] for k in ['displacement','stress']))
            )
            
            # 检测stdout中是否有实际物理结果输出（挠度、位移、应力等）
            has_physical_output = any(keyword in stdout_lower for keyword in [
                '最大挠度', '最大位移', '最大应力', '最大弯矩', '最大剪力',
                'max deflection', 'max displacement', 'max stress', 'maximum bending moment'
            ])
            
            # 基础成功：退出码为0 且 (有核心指标 或 有物理输出) 或 显式 converged 且有核心指标
            parsed_success = (
                (process.returncode == 0 and (has_core_metric or has_physical_output)) or 
                (bool(results.get("converged")) and has_core_metric)
            )
            only_warning = (
                'userwarning' in stderr_lower and 'traceback' not in stderr_lower and 'error' not in stderr_lower
            )
            classification_reason = None
            if parsed_success:
                status = "completed"
                if has_physical_output and not has_core_metric:
                    classification_reason = "physical_output_detected"
                else:
                    classification_reason = "parsed_json_converged"
            elif only_warning and parsed_success:
                status = "completed"
                classification_reason = "only_warning"
            elif process.returncode == 0 and parsed_success:
                status = "completed"
                classification_reason = "return_code_zero"
            else:
                status = "failed"
                classification_reason = f"failed_rc={process.returncode}" if process else "failed_unknown"
                results["error"] = results.get("error", "Simulation script failed to execute or did not converge.")
                results["error_summary"] = self._summarize_error(process.stderr)
                results["wsl_return_code"] = process.returncode
                results["wsl_stdout"] = process.stdout
                results["wsl_stderr"] = process.stderr
            try:
                logger.info(f"作业 {job_id} 分类结果: status={status}, reason={classification_reason}, rc={process.returncode}, converged={results.get('converged')}")
            except Exception:
                pass

            # 用户需求：若仅出现特定 pkg_resources 弃用 UserWarning 亦视为成功
            # 用户强调：出现该弃用警告本身不应视为失败；若除该警告外无其它 traceback/error 则强制成功
            deprecation_sig = 'pkg_resources is deprecated as an api'
            only_deprecation = (
                deprecation_sig in stderr_lower and
                'traceback' not in stderr_lower and
                'error' not in stderr_lower.replace('pkg_resources is deprecated as an api','')
            )
            if status == 'failed' and only_deprecation and process.returncode == 0:
                status = 'completed'
                results.setdefault('meta', {})
                reasons = results['meta'].get('forced_success_reasons', [])
                reasons.append('pkg_resources_deprecation_warning_only')
                results['meta']['forced_success_reasons'] = reasons
                # 去除失败标签字段
                for k in ['error', 'error_summary', 'wsl_return_code']:
                    if k in results:
                        results.pop(k)
                try:
                    logger.info(f"作业 {job_id} 因仅含 pkg_resources 弃用警告被标记为成功。")
                except Exception:
                    pass

            try:
                result_file = os.path.join(work_dir, "results.json")
                with open(result_file, "w", encoding="utf-8") as f:
                    json.dump(results, f, ensure_ascii=False, indent=2)
            except Exception as save_exc:
                logger.warning(f"写入增强结果 results.json 失败: {save_exc}")

            with self.lock:
                self.completed_tasks[job_id] = {
                    "status": status,
                    "results": results,
                    "execution_time": execution_time
                }
            logger.info(f"作业 {job_id} {status}，耗时: {execution_time:.2f}秒")

        except subprocess.TimeoutExpired as e:
            error_msg = f"作业 {job_id} 执行超时"
            logger.error(error_msg)
            stdout_file = os.path.join(work_dir, "stdout.log")
            with open(stdout_file, "w", encoding='utf-8') as log_f:
                log_f.write(f"--- TIMEOUT ---\n{error_msg}\n")
                if e.stdout:
                    log_f.write("\n--- STDOUT ---\n")
                    log_f.write(e.stdout)
                if e.stderr:
                    log_f.write("\n--- STDERR ---\n")
                    log_f.write(e.stderr)
            with self.lock:
                self.completed_tasks[job_id] = {
                    "status": "failed",
                    "error": error_msg,
                    "wsl_stdout": e.stdout if e.stdout else "",
                    "wsl_stderr": e.stderr if e.stderr else ""
                }
        except Exception as e:
            error_msg = f"作业 {job_id} 执行时发生未知异常: {str(e)}"
            logger.error(error_msg, exc_info=True)
            with self.lock:
                self.completed_tasks[job_id] = {
                    "status": "failed",
                    "error": error_msg
                }

    def _extract_error_context(self, stderr: str, primary_path: str, alt_path: Optional[str] = None, context_lines: int = 3) -> Optional[Dict[str, Any]]:
        """从Python traceback中提取与目标脚本相关的错误行上下文。
        - primary_path: 期望匹配的WSL路径
        - alt_path: 可备选的Windows路径
        返回 { file, line, start_line, end_line, snippet } 或 None
        """
        try:
            if not stderr:
                return None
            import re
            # 匹配 traceback 的 File 行：File "...", line N, in ...
            pattern = r"File \"([^\"]+)\", line (\d+)"
            matches = re.findall(pattern, stderr)
            if not matches:
                return None

            # 取最后一个匹配，并尽量匹配到我们的脚本文件
            target_match = None
            for fpath, lno in reversed(matches):
                if fpath == primary_path:
                    target_match = (fpath, int(lno))
                    break
                if alt_path and fpath == alt_path:
                    target_match = (fpath, int(lno))
                    break
            if not target_match:
                # 放宽：只要文件名一致也接受
                primary_name = os.path.basename(primary_path)
                alt_name = os.path.basename(alt_path) if alt_path else None
                for fpath, lno in reversed(matches):
                    if os.path.basename(fpath) == primary_name or (alt_name and os.path.basename(fpath) == alt_name):
                        target_match = (fpath, int(lno))
                        break
            if not target_match:
                return None

            matched_path, line_no = target_match
            # 以Windows路径读取源文件（如果WSL路径匹配，也映射回Windows）
            local_path = alt_path if matched_path == primary_path and alt_path else matched_path
            if matched_path == primary_path and alt_path:
                local_path = alt_path
            # 若仍是WSL路径，尝试转换回Windows路径（用于读取）
            if local_path.startswith("/mnt/") and not os.path.exists(local_path):
                # 将 /mnt/c/... 转回 C:\...
                try:
                    parts = local_path.split('/')
                    drive = parts[2].upper() + ':'
                    rest = '\\'.join(parts[3:])
                    candidate = f"{drive}\\{rest}"
                    if os.path.exists(candidate):
                        local_path = candidate
                except Exception:
                    pass

            # 读取并切片上下文
            if not os.path.exists(local_path):
                # 若找不到文件，仍返回基础信息
                return {"file": matched_path, "line": line_no}

            with open(local_path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
            idx = max(1, line_no)
            start = max(1, idx - context_lines)
            end = min(len(lines), idx + context_lines)
            snippet_lines = []
            for i in range(start, end + 1):
                prefix = '>' if i == idx else ' '
                snippet_lines.append(f"{prefix}{i:5d}: {lines[i-1].rstrip()}\n")
            snippet = ''.join(snippet_lines)
            return {
                "file": matched_path,
                "line": line_no,
                "start_line": start,
                "end_line": end,
                "snippet": snippet
            }
        except Exception:
            return None

    def windows_to_wsl_path(self, windows_path: str) -> str:
        """将Windows绝对路径转换为WSL路径"""
        abs_path = os.path.abspath(windows_path)
        drive, path = os.path.splitdrive(abs_path)
        path = path.replace('\\', '/')
        return f"/mnt/{drive.lower()[0]}{path}"

    def _is_wsl2(self) -> bool:
        """检测是否运行在WSL2环境中"""
        try:
            with open("/proc/version", "r") as f:
                return "WSL2" in f.read()
        except:
            return False

    def worker(self):
        """工作线程，从队列中获取任务并将其分派到新线程中执行"""
        while True:
            job_id, script_content = None, None
            with self.lock:
                if self.task_queue:
                    job_id, script_content = self.task_queue.pop(0)
            
            if job_id and script_content:
                logger.info(f"分派作业: {job_id}")
                job_thread = threading.Thread(
                    target=self.run_fenics_job,
                    args=(job_id, script_content),
                    daemon=True
                )
                job_thread.start()

            time.sleep(0.1) # 减少延迟

    # 注：已删除 sanitize_script/autofix_common_issues 等程序化修改，避免干扰错误回流

    def _summarize_error(self, stderr: str) -> str:
        """
        从stderr提取关键报错的简短摘要，给上层LLM用于快速修复。
        """
        try:
            if not stderr:
                return ""
            import re
            # 抓取最后一个异常类型行
            lines = [ln for ln in stderr.splitlines() if ln.strip()]
            last_err = next((ln for ln in reversed(lines) if 
                             ("Error:" in ln or "Exception" in ln or "Traceback" in ln or "TypeError:" in ln)), "")
            # 针对 IntervalMesh 常见错误给出指引
            if "IntervalMesh" in stderr and "incompatible constructor arguments" in stderr:
                return "TypeError: IntervalMesh 需要3个参数 (n, a, b)。将 IntervalMesh(n, L) 改为 IntervalMesh(n, 0, L)。"
            # 常见JSON解析
            if "JSONDecodeError" in stderr:
                return "JSONDecodeError: 输出中JSON格式不正确，确保打印'--- FENICS JOB RESULT ---'后紧接一行有效JSON。"
            return last_err.strip()
        except Exception:
            return ""

def start_server():
    """创建并运行服务器实例"""
    # 从 ../config/config.json 加载配置
    try:
        config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'config.json')
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        fenics_config = config.get("fenics_server", {})
        host = fenics_config.get("host", "127.0.0.1")
        port = fenics_config.get("port", 5000)
    except Exception as e:
        logger.error(f"无法加载配置文件，将使用默认值: {e}")
        host = "127.0.0.1"
        port = 5000
        
    server = FenicsMCPServer(host=host, port=port)
    # run() 方法会阻塞，直到服务器停止
    server.run()

if __name__ == '__main__':
    # 当此脚本作为主程序直接运行时，启动服务器
    start_server()


