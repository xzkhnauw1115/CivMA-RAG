"""一键集成入口：启动 FEniCS 仿真服务器 + 多Agent + Gradio 前端

运行示例：
    python all_in_one.py            # 默认启动仿真服务器 + Gradio UI
    python all_in_one.py --demo "生成一个1m 1kN 悬臂梁脚本并运行"
    python all_in_one.py --interactive
    python all_in_one.py --show-stats     # 查看脚本运行/修复/验证统计
    python all_in_one.py --no-gradio  # 仅启动服务器（可配合 demo/interactive）

组件：
 - FEniCS 仿真服务器 (WSL 调用) 来自 src/fenics_mcp_server.py
 - 多Agent（Manager, Researcher, Coder, Fixer）来自 chat_bot_multiagent.py
 - Gradio 前端（单次请求模式）

注意：
 - 服务器线程为后台 daemon，若主进程退出即停止。
 - Gradio 使用 127.0.0.1:7860，可通过 --port 调整。
 - 若需要持久多轮会话，请后续扩展当前前端逻辑。
"""

import os
# 解决 OpenMP 多重加载错误
os.environ['KMP_DUPLICATE_LIB_OK'] = 'TRUE'

import json
import time
import threading
import argparse
import logging
import ssl
import socket
import subprocess
from typing import List, Tuple

# 尝试绕过 SSL 验证 (解决 HuggingFace 模型下载问题)
try:
    _create_unverified_https_context = ssl._create_unverified_context
except AttributeError:
    pass
else:
    ssl._create_default_https_context = _create_unverified_https_context

import requests

import chat_bot_multiagent as chat_agents
from chat_bot_multiagent import run_demo, run_interactive, get_run_statistics
FENICS_SERVER_URL = chat_agents.FENICS_SERVER_URL
from src.fenics_mcp_server import FenicsMCPServer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _parse_server_url(url: str) -> Tuple[str, int]:
    try:
        host_port = url.split("://", 1)[-1]
        host, port_text = host_port.rsplit(":", 1)
        return host, int(port_text)
    except Exception:
        return "127.0.0.1", 5000


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _find_free_port(host: str, start_port: int) -> int:
    for port in range(start_port, start_port + 100):
        if not _port_in_use(host, port):
            return port
    raise RuntimeError(f"未找到可用 FEniCS 服务端口: {start_port}-{start_port + 99}")


def set_fenics_server_url(host: str, port: int) -> str:
    global FENICS_SERVER_URL
    FENICS_SERVER_URL = f"http://{host}:{port}"
    chat_agents.FENICS_SERVER_URL = FENICS_SERVER_URL
    return FENICS_SERVER_URL

# ================== 启动 FEniCS 仿真服务器 ==================
def start_fenics_server(host: str = None, port: int = None):
    """启动 FEniCS 服务器于后台线程。"""
    # 从现有 URL 推断 host/port (简单解析)
    if not host or not port:
        try:
            # FENICS_SERVER_URL 形如 http://127.0.0.1:5000
            parts = FENICS_SERVER_URL.split('://', 1)[-1].split(':')
            host = host or parts[0]
            port = port or int(parts[1])
        except Exception:
            host = host or '127.0.0.1'
            port = port or 5000
    server = FenicsMCPServer(host=host, port=port)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    logger.info(f"FEniCS 仿真服务器后台启动: http://{host}:{port}")
    return server, t


def wait_server_ready(url: str, timeout: float = 15.0):
    """轮询 /status 确认服务可访问。"""
    status_url = f"{url}/status"
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(status_url, timeout=2)
            if r.status_code == 200:
                logger.info(f"后端服务已就绪: {r.json()}")
                return True
        except Exception:
            pass
        time.sleep(1)
    logger.warning("后端服务在超时时间内未完全确认就绪，继续但可能出现连接失败。")
    return False



def check_fenics_environment(require_wsl: bool = True) -> Tuple[bool, str]:
    """Preflight check before spending LLM/API calls on a simulation that cannot run."""
    if not require_wsl:
        return True, "已跳过本机 WSL/FEniCS 预检"
    runtime = chat_agents.CONFIG.get("fenics_runtime", {})
    wsl_distro = runtime.get("wsl_distro", "Ubuntu")
    conda_env = runtime.get("conda_env", "fenics")
    python_command = runtime.get("python_command", "python")
    fallback_python = runtime.get("fallback_python_command", "python3")
    check_code = "from dolfin import *\nprint('FENICS_OK')"
    wsl_command = (
        "CONDA_SH=''; "
        "for p in \"$HOME/miniconda3/etc/profile.d/conda.sh\" \"$HOME/anaconda3/etc/profile.d/conda.sh\" \"/opt/conda/etc/profile.d/conda.sh\"; do "
        "if [ -f \"$p\" ]; then CONDA_SH=\"$p\"; break; fi; "
        "done; "
        f"if [ -n \"$CONDA_SH\" ]; then source \"$CONDA_SH\" && conda activate '{conda_env}' && {python_command} -c \"{check_code}\"; "
        f"else {fallback_python} -c \"{check_code}\"; fi"
    )
    try:
        proc = subprocess.run(
            ["wsl", "-d", wsl_distro, "-e", "bash", "-lc", wsl_command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            timeout=30,
        )
        stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
        output = (stdout + "\n" + stderr).strip()
        if proc.returncode == 0 and "FENICS_OK" in output:
            return True, f"WSL {wsl_distro} / FEniCS 可用"
        return False, f"WSL {wsl_distro} / FEniCS 不可用，返回码={proc.returncode}，输出={output[:800]}"
    except Exception as exc:
        return False, f"WSL {wsl_distro} / FEniCS 预检失败：{exc}"# ================== Gradio 前端 ==================
def run_multi_agent_once(request: str) -> Tuple[List[Tuple[str, str]], str]:
    """运行一次多Agent协作（无状态 RAG 模式）"""
    if not request.strip():
        return [], "请输入有效请求"

    from chat_bot_multiagent import build_multiagent_system

    manager, user_proxy, agents = build_multiagent_system()
    user_proxy.initiate_chat(manager, message=request)

    msgs = []
    final_summary = ""

    for m in manager.groupchat.messages:
        role = m.get('name', m.get('role', 'unknown'))
        content = m.get('content', '')
        msgs.append((role, content))

        if 'TERMINATE' in content:
            final_summary = content

    if not final_summary:
        for m in reversed(manager.groupchat.messages):
            if m.get('name', '').startswith('Manager'):
                final_summary = m.get('content', '')
                break

    return msgs, final_summary or "(未找到总结)"

def build_gradio(port: int = 7860):
    import gradio as gr
    with gr.Blocks(title="FEniCS 多Agent 协作") as demo:
        gr.Markdown("""
        # FEniCS 多Agent 协作界面

        **功能**：输入物理/仿真需求，系统自动：RAG检索 -> 生成脚本 -> 运行 -> 脚本修复 -> 验证 ->总结

        **模式**：
        - 只使用本地知识库 RAG
        """)
        request_box = gr.Textbox(label="需求", placeholder="例如：计算4m简支梁，均布荷载5kN/m，C30混凝土", lines=3)
        run_btn = gr.Button("运行")
        chatlog = gr.Dataframe(headers=["角色", "消息"], datatype=["str", "str"], label="对话记录", interactive=False)
        summary = gr.Textbox(label="最终总结/终止语", lines=4)

        def _on_run(req):
            msgs, summ = run_multi_agent_once(req)
            return msgs, summ

        run_btn.click(_on_run, inputs=request_box, outputs=[chatlog, summary])
        
        gr.Markdown("""
        **提示**：
        - 系统只使用当前输入和本地 RAG
        - 如需交互模式，运行：`python chat_bot_multiagent.py --interactive`
        """)
    actual_port = port
    if _port_in_use("127.0.0.1", actual_port):
        actual_port = _find_free_port("127.0.0.1", actual_port + 1)
        logger.warning("Gradio 端口 %s 已被占用，改用 %s。", port, actual_port)
    demo.launch(server_name="127.0.0.1", server_port=actual_port, show_error=True)


# ================== 主控制入口 ==================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--demo', type=str, help='单次演示请求')
    parser.add_argument('--demo-file', type=str, help='从文本文件读取单次演示请求，适合超长需求')
    parser.add_argument('--interactive', action='store_true', help='命令行交互模式')
    parser.add_argument('--no-gradio', action='store_true', help='不启动 Gradio 前端')
    parser.add_argument('--show-stats', action='store_true', help='显示固定脚本运行统计后退出')
    parser.add_argument('--port', type=int, default=7860, help='Gradio 端口')
    parser.add_argument('--no-server', action='store_true', help='不启动内置 FEniCS 服务器 (假设已外部启动)')
    parser.add_argument('--skip-fenics-preflight', action='store_true', help='跳过本机 WSL/FEniCS 预检；仅在使用外部可用后端时使用')
    parser.add_argument('--fenics-port', type=int, default=None, help='内置 FEniCS 服务端口；默认读取配置，若占用则自动顺延')
    parser.add_argument('--disable-remote', action='store_true', help='安全模式：禁止远程 LLM；当前未实现本地多智能体回退，会直接退出')
    args = parser.parse_args()

    if args.show_stats:
        print(get_run_statistics())
        return

    if args.disable_remote:
        raise SystemExit(
            "--disable-remote 已阻止启动：当前多智能体流程仍依赖远程 LLM，"
            "尚未实现真正的本地规则回退。为避免误发 DeepSeek API，请去掉该参数或先实现本地回退。"
        )

    if not args.no_server and not args.skip_fenics_preflight:
        ok, message = check_fenics_environment(require_wsl=True)
        if not ok:
            raise SystemExit(
                "FEniCS 环境预检失败，已在调用远程 LLM 前停止，避免无效消耗 API。\n"
                + message +
                "\n请安装/恢复 WSL 发行版 Ubuntu，并在其中配置 FEniCS 2019.1.0；"
                "如果你确认已有外部后端，请使用 --no-server 或 --skip-fenics-preflight。"
            )
        logger.info(message)

    # 启动服务器
    host, configured_port = _parse_server_url(FENICS_SERVER_URL)
    desired_port = args.fenics_port or configured_port
    if not args.no_server:
        actual_port = desired_port
        if _port_in_use(host, actual_port):
            next_port = _find_free_port(host, actual_port + 1)
            logger.warning("FEniCS 服务端口 %s 已被占用，改用 %s，避免连接到旧服务。", actual_port, next_port)
            actual_port = next_port
        set_fenics_server_url(host, actual_port)
        start_fenics_server(host, actual_port)
        wait_server_ready(FENICS_SERVER_URL)
    else:
        set_fenics_server_url(host, desired_port)
        logger.info("跳过服务器启动，使用外部服务: %s", FENICS_SERVER_URL)

    # 模式分支
    # 模式分支
    demo_request = args.demo
    if args.demo_file:
        with open(args.demo_file, 'r', encoding='utf-8') as f:
            demo_request = f.read()

    if demo_request:
        if args.disable_remote:
            os.environ['DISABLE_REMOTE_GENERATE'] = '1'
        run_demo(demo_request)
        if not args.no_gradio:
            build_gradio(args.port)
    elif args.interactive:
        if args.disable_remote:
            os.environ['DISABLE_REMOTE_GENERATE'] = '1'
        run_interactive()
        if not args.no_gradio:
            build_gradio(args.port)
    else:
        if args.disable_remote:
            os.environ['DISABLE_REMOTE_GENERATE'] = '1'
        if args.no_gradio:
            logger.info("未选择 demo/interactive，且关闭 Gradio；程序空闲退出。")
        else:
            build_gradio(args.port)


if __name__ == '__main__':
    main()

