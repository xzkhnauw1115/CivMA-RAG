"""多Agent群聊系统 - 完整工作流版本

实现完整的 GroupChat 工作流：
- Manager: 项目经理，负责规划、委派、监控、总结
- Researcher: 研究员，负责信息检索
- Coder: 程序员，负责生成 FEniCS 脚本并请求执行
- Coder: 负责编写和完整重写修复失败脚本
- User_Proxy: 自动检测并执行脚本

工作流：User -> Manager -> Coder -> User_Proxy(自动执行) -> Researcher -> (Coder if fail) -> Manager -> User
"""

import os
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
import json
import time
import argparse
import logging
import re
import ast
import uuid
from typing import Dict, Any, List

from autogen import AssistantAgent, UserProxyAgent, GroupChat, GroupChatManager
import requests
import urllib3
from bs4 import BeautifulSoup
import chromadb
from chromadb.utils import embedding_functions
from sentence_transformers import SentenceTransformer

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =============== 配置加载 =================
def _expand_env_placeholders(value):
    """Resolve config values like ${DEEPSEEK_API_KEY} without storing secrets in files."""
    if isinstance(value, str):
        match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value.strip())
        if match:
            return os.environ.get(match.group(1), "")
        return value
    if isinstance(value, list):
        return [_expand_env_placeholders(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env_placeholders(item) for key, item in value.items()}
    return value


def load_config() -> Dict[str, Any]:
    cfg_path = os.path.join(os.path.dirname(__file__), 'config', 'config.json')
    try:
        with open(cfg_path, 'r', encoding='utf-8') as f:
            return _expand_env_placeholders(json.load(f))
    except Exception as e:
        logger.warning(f"无法加载配置文件 {cfg_path}: {e}")
        return {}

CONFIG = load_config()
FENICS_SERVER_URL = CONFIG.get('fenics_server', {}).get('url', 'http://127.0.0.1:5000')
LLM_BASE_CONFIG = CONFIG.get('autogen', {}).get('llm_config', {})
PROJECT_ROOT = os.path.dirname(__file__)
FENICS_DRAFT_DIR = os.path.join(PROJECT_ROOT, 'temp_scripts', 'fenics_drafts')
CURRENT_FENICS_SCRIPT_FILE = 'current_fenics_script.py'
CURRENT_FENICS_SCRIPT_PATH = os.path.join(FENICS_DRAFT_DIR, CURRENT_FENICS_SCRIPT_FILE)
RUN_STATISTICS_PATH = os.path.join(PROJECT_ROOT, 'state', 'run_statistics.json')

# =============== 全局状态 =================
_LAST_SCRIPT = None  # 保存最后生成的脚本，供 Executor 使用

def _new_stats() -> Dict[str, Any]:
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    return {
        "created_at": now,
        "updated_at": now,
        "current_script_path": _relative_to_project(CURRENT_FENICS_SCRIPT_PATH) if 'CURRENT_FENICS_SCRIPT_PATH' in globals() else "",
        "totals": {
            "script_resets": 0,
            "script_appends": 0,
            "code_line_edits": 0,
            "simulation_runs": 0,
            "simulation_success": 0,
            "simulation_failed": 0,
            "physics_validation_passed": 0,
            "physics_validation_failed": 0,
            "rag_tool_calls": 0,
            "rag_guardrail_hits": 0,
            "rag_guardrail_blocks": 0,
            "rag_policy_blocks": 0,
        },
        "error_categories": {},
        "last_run": None,
        "runs": [],
        "edits": [],
        "validation_events": [],
        "rag_events": []
    }


def _load_stats() -> Dict[str, Any]:
    os.makedirs(os.path.dirname(RUN_STATISTICS_PATH), exist_ok=True)
    if not os.path.exists(RUN_STATISTICS_PATH):
        return _new_stats()
    try:
        with open(RUN_STATISTICS_PATH, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
        base = _new_stats()
        base.update(data if isinstance(data, dict) else {})
        totals = base.get("totals") if isinstance(base.get("totals"), dict) else {}
        merged_totals = _new_stats()["totals"]
        merged_totals.update(totals)
        base["totals"] = merged_totals
        base.setdefault("error_categories", {})
        base.setdefault("runs", [])
        base.setdefault("edits", [])
        base.setdefault("validation_events", [])
        base.setdefault("rag_events", [])
        return base
    except Exception:
        return _new_stats()


def _status_cn(status: Any) -> str:
    mapping = {
        "completed": "运行成功",
        "failed": "运行失败",
        "queued": "排队中",
        "processing": "运行中",
        "unknown": "未知状态",
    }
    return mapping.get(str(status), str(status))


def _error_category_cn(category: Any) -> str:
    mapping = {
        None: "无",
        "none": "无",
        "syntax_error": "语法错误",
        "fenics_expression_error": "FEniCS 表达式/运行错误",
        "undefined_name": "变量未定义",
        "boundary_condition_error": "边界条件错误",
        "solver_convergence_error": "求解器/收敛错误",
        "timeout": "运行超时",
        "physics_validation_failed": "物理验证失败",
        "runtime_error": "运行时错误",
        "other": "其他错误",
    }
    return mapping.get(category, str(category))


def _run_entry_cn(run: Any) -> Any:
    if not isinstance(run, dict):
        return run
    return {
        "时间": run.get("time"),
        "任务编号": run.get("job_id"),
        "状态": _status_cn(run.get("status")),
        "是否成功": "是" if run.get("success") else "否",
        "错误类型": _error_category_cn(run.get("error_category")),
        "错误摘要": run.get("error_summary"),
        "源脚本文件": run.get("source_script_file"),
        "结果目录": run.get("job_dir"),
        "结果目录绝对路径": run.get("job_abs_dir"),
        "实际执行脚本": run.get("executed_script_path"),
        "实际执行脚本绝对路径": run.get("executed_script_abs_path"),
    }


def _edit_entry_cn(edit: Any) -> Any:
    if not isinstance(edit, dict):
        return edit
    return {
        "时间": edit.get("time"),
        "起始行": edit.get("start_line"),
        "结束行": edit.get("end_line"),
        "替换后行数": edit.get("replacement_lines"),
        "脚本路径": edit.get("script_path"),
    }


def _validation_entry_cn(event: Any) -> Any:
    if not isinstance(event, dict):
        return event
    return {
        "时间": event.get("time"),
        "是否通过": "是" if event.get("passed") else "否",
        "类型": _error_category_cn(event.get("category")),
        "摘要": event.get("summary"),
    }


def _stats_chinese_view(stats: Dict[str, Any], include_history: bool = True) -> Dict[str, Any]:
    totals = stats.get("totals", {}) if isinstance(stats.get("totals"), dict) else {}
    errors = stats.get("error_categories", {}) if isinstance(stats.get("error_categories"), dict) else {}
    view = {
        "统计文件路径": _relative_to_project(RUN_STATISTICS_PATH),
        "当前固定脚本": _relative_to_project(CURRENT_FENICS_SCRIPT_PATH),
        "创建时间": stats.get("created_at"),
        "更新时间": stats.get("updated_at"),
        "累计统计": {
            "脚本清空次数": totals.get("script_resets", 0),
            "脚本写入次数": totals.get("script_appends", 0),
            "代码行号修改次数": totals.get("code_line_edits", 0),
            "仿真运行总次数": totals.get("simulation_runs", 0),
            "仿真运行成功次数": totals.get("simulation_success", 0),
            "仿真运行失败次数": totals.get("simulation_failed", 0),
            "物理验证通过次数": totals.get("physics_validation_passed", 0),
            "物理验证失败次数": totals.get("physics_validation_failed", 0),
        },
        "RAG作用统计": {
            "RAG工具调用次数": totals.get("rag_tool_calls", 0),
            "命中专用硬规则次数": totals.get("rag_guardrail_hits", 0),
            "因未先RAG被拦截次数": totals.get("rag_guardrail_blocks", 0),
            "伪造/混合工具等策略拦截次数": totals.get("rag_policy_blocks", 0),
        },
        "错误类型统计": {
            _error_category_cn(key): value for key, value in errors.items()
        },
        "最近一次运行": _run_entry_cn(stats.get("last_run")),
    }
    if include_history:
        view["最近10次运行"] = [_run_entry_cn(item) for item in stats.get("runs", [])[-10:]]
        view["最近10次代码修改"] = [_edit_entry_cn(item) for item in stats.get("edits", [])[-10:]]
        view["最近5次物理验证"] = [_validation_entry_cn(item) for item in stats.get("validation_events", [])[-5:]]
        view["最近10次RAG事件"] = stats.get("rag_events", [])[-10:]
    return view

def _save_stats(data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(RUN_STATISTICS_PATH), exist_ok=True)
    data["updated_at"] = time.strftime('%Y-%m-%d %H:%M:%S')
    for key, limit in [("runs", 200), ("edits", 500), ("validation_events", 200), ("rag_events", 300)]:
        if isinstance(data.get(key), list) and len(data[key]) > limit:
            data[key] = data[key][-limit:]
    data["中文摘要"] = _stats_chinese_view(data, include_history=False)
    with open(RUN_STATISTICS_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _record_rag_event(event_type: str, role: str = "", detail: str = "") -> None:
    stats = _load_stats()
    totals = stats.setdefault("totals", {})
    if event_type == "tool_call":
        totals["rag_tool_calls"] = totals.get("rag_tool_calls", 0) + 1
    elif event_type == "guardrail_hit":
        totals["rag_guardrail_hits"] = totals.get("rag_guardrail_hits", 0) + 1
    elif event_type == "guardrail_block":
        totals["rag_guardrail_blocks"] = totals.get("rag_guardrail_blocks", 0) + 1
    elif event_type == "policy_block":
        totals["rag_policy_blocks"] = totals.get("rag_policy_blocks", 0) + 1
    stats.setdefault("rag_events", []).append({
        "time": time.strftime('%Y-%m-%d %H:%M:%S'),
        "type": event_type,
        "role": role,
        "detail": (detail or "")[:500]
    })
    _save_stats(stats)


def _classify_error(text: str) -> str:
    s = (text or "").lower()
    if not s:
        return "none"
    if "syntaxerror" in s or "syntax validation" in s:
        return "syntax_error"
    if "indexerror" in s or "tuple index out of range" in s or "ufl" in s or "unable to evaluate expression" in s:
        return "fenics_expression_error"
    if "nameerror" in s or "undefined" in s:
        return "undefined_name"
    if "no bc" in s or "boundary" in s or "fixed dofs: 0" in s:
        return "boundary_condition_error"
    if "linear solver" in s or "converge" in s or "diverg" in s or "ksp" in s or "mumps" in s:
        return "solver_convergence_error"
    if "timeout" in s:
        return "timeout"
    if "验证失败" in s or "不合理" in s or "偏离" in s:
        return "physics_validation_failed"
    if "traceback" in s or "runtimeerror" in s or "error" in s:
        return "runtime_error"
    return "other"


def _increment_error_category(stats: Dict[str, Any], category: str) -> None:
    if not category or category == "none":
        return
    stats.setdefault("error_categories", {})
    stats["error_categories"][category] = stats["error_categories"].get(category, 0) + 1


def _record_script_reset() -> None:
    stats = _load_stats()
    stats["totals"]["script_resets"] = stats["totals"].get("script_resets", 0) + 1
    _save_stats(stats)


def _record_script_append(chars: int, lines: int) -> None:
    stats = _load_stats()
    stats["totals"]["script_appends"] = stats["totals"].get("script_appends", 0) + 1
    stats["last_append"] = {"time": time.strftime('%Y-%m-%d %H:%M:%S'), "chars": chars, "lines": lines}
    _save_stats(stats)


def _record_code_edit(start_line: int, end_line: int, replacement_lines: int) -> None:
    stats = _load_stats()
    stats["totals"]["code_line_edits"] = stats["totals"].get("code_line_edits", 0) + 1
    stats.setdefault("edits", []).append({
        "time": time.strftime('%Y-%m-%d %H:%M:%S'),
        "start_line": start_line,
        "end_line": end_line,
        "replacement_lines": replacement_lines,
        "script_path": _relative_to_project(CURRENT_FENICS_SCRIPT_PATH),
    })
    _save_stats(stats)


def _record_simulation_result(result: Dict[str, Any]) -> None:
    stats = _load_stats()
    stats["totals"]["simulation_runs"] = stats["totals"].get("simulation_runs", 0) + 1
    status = result.get("status") or result.get("data", {}).get("status") or "unknown"
    ok = status == "completed"
    if ok:
        stats["totals"]["simulation_success"] = stats["totals"].get("simulation_success", 0) + 1
    else:
        stats["totals"]["simulation_failed"] = stats["totals"].get("simulation_failed", 0) + 1

    data = result.get("data", {}) if isinstance(result.get("data"), dict) else {}
    results = data.get("results", {}) if isinstance(data.get("results"), dict) else {}
    meta = results.get("meta", {}) if isinstance(results.get("meta"), dict) else {}
    error_text = "\n".join(str(x) for x in [
        result.get("error"), results.get("error"), results.get("error_summary"),
        results.get("stderr"), results.get("wsl_stderr")
    ] if x)
    category = _classify_error(error_text)
    if not ok:
        _increment_error_category(stats, category)

    run_entry = {
        "time": time.strftime('%Y-%m-%d %H:%M:%S'),
        "job_id": result.get("job_id"),
        "status": status,
        "success": ok,
        "error_category": None if ok else category,
        "error_summary": results.get("error_summary") or results.get("error") or result.get("error"),
        "source_script_file": result.get("source_script_file") or _relative_to_project(CURRENT_FENICS_SCRIPT_PATH),
        "job_dir": meta.get("job_dir"),
        "job_abs_dir": meta.get("job_abs_dir"),
        "executed_script_path": meta.get("executed_script_path"),
        "executed_script_abs_path": meta.get("executed_script_abs_path"),
    }
    stats["last_run"] = run_entry
    stats.setdefault("runs", []).append(run_entry)
    _save_stats(stats)


def _latest_simulation_succeeded(stats: Dict[str, Any] = None) -> bool:
    stats = stats or _load_stats()
    last_run = stats.get("last_run") or {}
    return bool(last_run.get("success") is True and last_run.get("status") == "completed")


def record_physics_validation(passed: bool, summary: str = "") -> str:
    stats = _load_stats()
    if passed and not _latest_simulation_succeeded(stats):
        passed = False
        last_run = stats.get("last_run") or {}
        summary = (
            "系统拦截：Researcher 试图记录验证通过，但最近一次仿真并未成功完成。"
            f" 最近状态={last_run.get('status')}, 错误={last_run.get('error_summary')}.\n"
            + (summary or "")
        )
    key = "physics_validation_passed" if passed else "physics_validation_failed"
    stats["totals"][key] = stats["totals"].get(key, 0) + 1
    category = "physics_validation_passed" if passed else _classify_error(summary or "验证失败")
    if not passed:
        _increment_error_category(stats, category)
    stats.setdefault("validation_events", []).append({
        "time": time.strftime('%Y-%m-%d %H:%M:%S'),
        "passed": passed,
        "category": category,
        "summary": (summary or "")[:1000]
    })
    _save_stats(stats)
    return json.dumps({"success": True, "passed": passed, "stats_path": _relative_to_project(RUN_STATISTICS_PATH)}, ensure_ascii=False, indent=2)


def get_run_statistics() -> str:
    stats = _load_stats()
    return json.dumps(_stats_chinese_view(stats), ensure_ascii=False, indent=2)
# =============== 工具函数实现 =================

def get_knowledge_structure() -> str:
    """获取本地知识库的目录结构（文件名和一级/二级标题）"""
    logger.info("[TOOL] get_knowledge_structure 被调用")
    base = os.path.join(os.path.dirname(__file__), 'data', 'local_knowledge')
    structure = {}
    
    if not os.path.isdir(base):
        return json.dumps({"error": "local_knowledge dir not found"}, ensure_ascii=False)
    
    for fname in os.listdir(base):
        if not fname.endswith('.md'):
            continue
        fpath = os.path.join(base, fname)
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            headers = []
            for line in lines:
                line = line.strip()
                if line.startswith('# '):
                    headers.append(f"H1: {line[2:]}")
                elif line.startswith('## '):
                    headers.append(f"  H2: {line[3:]}")
            
            structure[fname] = headers
        except Exception as e:
            structure[fname] = [f"Error reading file: {str(e)}"]
            
    return json.dumps(structure, ensure_ascii=False, indent=2)

# =============== 知识库管理器 (RAG) =================

class KnowledgeBase:
    def __init__(self):
        self.persist_directory = os.path.join(os.path.dirname(__file__), 'state', 'chroma_db_active')
        os.makedirs(self.persist_directory, exist_ok=True)
        self.client = chromadb.PersistentClient(path=self.persist_directory)
        
        # 使用 sentence-transformers
        self.embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        
        self.collection = self.client.get_or_create_collection(
            name="fenics_knowledge",
            embedding_function=self.embedding_fn
        )
        
        # 本地知识库会被持续整理。每次启动重建小型索引，避免 Chroma 缓存旧/归档知识继续误导检索。
        logger.info("[KnowledgeBase] 重建主动知识索引，清除旧缓存...")
        self.rebuild_index()
            
    def rebuild_index(self):
        """重建索引。只索引 data/local_knowledge 根目录的主动 .md 文件，不索引归档目录。"""
        try:
            self.client.delete_collection("fenics_knowledge")
        except Exception:
            pass
        self.collection = self.client.get_or_create_collection(
            name="fenics_knowledge",
            embedding_function=self.embedding_fn
        )
        base = os.path.join(os.path.dirname(__file__), 'data', 'local_knowledge')
        if not os.path.isdir(base):
            logger.warning("[KnowledgeBase] local_knowledge 目录不存在")
            return

        ids = []
        documents = []
        metadatas = []
        
        for fname in os.listdir(base):
            if not fname.endswith('.md'):
                continue
            fpath = os.path.join(base, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                # 按二级标题切分
                sections = re.split(r'(?=\n##+ )', content)
                
                for i, section in enumerate(sections):
                    if not section.strip():
                        continue
                        
                    # 提取标题作为 metadata
                    lines = section.strip().split('\n')
                    title = lines[0] if lines else "Unknown"
                    
                    chunk_id = f"{fname}_{i}"
                    ids.append(chunk_id)
                    documents.append(section.strip())
                    metadatas.append({"source": fname, "title": title})
                    
            except Exception as e:
                logger.error(f"读取文件失败 {fname}: {e}")
        
        if documents:
            # 批量添加
            batch_size = 100
            for i in range(0, len(documents), batch_size):
                end = min(i + batch_size, len(documents))
                self.collection.add(
                    ids=ids[i:end],
                    documents=documents[i:end],
                    metadatas=metadatas[i:end]
                )
            logger.info(f"[KnowledgeBase] 已索引 {len(documents)} 个文档片段")

    def add_knowledge(self, content: str, metadata: Dict[str, Any]):
        """添加单条知识到向量库"""
        try:
            import uuid
            doc_id = str(uuid.uuid4())
            self.collection.add(
                ids=[doc_id],
                documents=[content],
                metadatas=[metadata]
            )
            logger.info(f"[KnowledgeBase] 已添加新知识: {metadata.get('title', 'Untitled')}")
            return True
        except Exception as e:
            logger.error(f"[KnowledgeBase] 添加知识失败: {e}")
            return False

    def search(self, query: str, n_results: int = 5) -> List[Dict]:
        """语义搜索"""
        results = self.collection.query(
            query_texts=[query],
            n_results=n_results
        )
        
        output = []
        if results['documents']:
            for i in range(len(results['documents'][0])):
                output.append({
                    "content": results['documents'][0][i],
                    "metadata": results['metadatas'][0][i],
                    "distance": results['distances'][0][i] if results['distances'] else 0
                })
        return output

_KB_INSTANCE = None

def get_knowledge_base():
    global _KB_INSTANCE
    if _KB_INSTANCE is None:
        _KB_INSTANCE = KnowledgeBase()
    return _KB_INSTANCE

def _keyword_search(query: str) -> str:
    """关键词搜索回退机制 (支持 .md 和 golden_scripts 中的 .py)"""
    logger.info(f"[TOOL] 触发关键词搜索回退: {query}")
    base = os.path.join(os.path.dirname(__file__), 'data', 'local_knowledge')
    golden_dir = os.path.join(base, 'golden_scripts')
    
    if not os.path.isdir(base):
        return json.dumps({"error": "local_knowledge not found"}, ensure_ascii=False)
    
    keywords = query.lower().split()
    chunks = []
    
    # 1. 搜索根目录下的 .md 文件
    files_to_search = []
    for fname in os.listdir(base):
        if fname.endswith('.md'):
            files_to_search.append(os.path.join(base, fname))
            
    # 2. 搜索 golden_scripts 下的 .py 文件
    if os.path.isdir(golden_dir):
        for fname in os.listdir(golden_dir):
            if fname.endswith('.py'):
                files_to_search.append(os.path.join(golden_dir, fname))
    
    for fpath in files_to_search:
        fname = os.path.basename(fpath)
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 对于 .py 文件，整个文件作为一个 chunk
            if fname.endswith('.py'):
                sections = [content]
            else:
                # 对于 .md 文件，按二级标题切分
                sections = re.split(r'(?=\n##+ )', content)
            
            for section in sections:
                if not section.strip():
                    continue
                
                section_lower = section.lower()
                score = 0
                matched_keywords = []
                
                for kw in keywords:
                    if kw in section_lower:
                        score += 1
                        matched_keywords.append(kw)
                        # 标题/文件名加权
                        if kw in fname.lower():
                            score += 3
                        first_line = section.split('\n')[0].lower()
                        if kw in first_line:
                            score += 2
                
                if score > 0:
                    chunks.append({
                        "file": fname,
                        "content": section.strip(),
                        "score": score,
                        "matched": matched_keywords,
                        "length": len(section)
                    })
        except Exception as e:
            logger.warning(f"读取文件失败 {fname}: {e}")
    
    chunks.sort(key=lambda x: (-x['score'], x['length']))
    top_chunks = chunks[:6]
    
    return json.dumps({
        "query": query,
        "method": "keyword_fallback_enhanced",
        "results": [
            {
                "file": c["file"],
                "score": c["score"],
                "matched": c["matched"],
                "content": c["content"][:2000] + "..." if len(c["content"]) > 2000 else c["content"]
            }
            for c in top_chunks
        ]
    }, ensure_ascii=False, indent=2)

def local_search(query: str, filename: str = None, **kwargs) -> str:
    """
    智能检索本地知识库 (语义检索 + 关键词增强)
    """
    if filename:
        safe_name = os.path.basename(str(filename))
        if safe_name != str(filename) or ".." in safe_name:
            return json.dumps({"error": "invalid filename", "hint": "Use a plain local_knowledge filename."}, ensure_ascii=False, indent=2)
        roots = [
            os.path.join(os.path.dirname(__file__), "data", "local_knowledge"),
            os.path.join(os.path.dirname(__file__), "data", "local_knowledge", "golden_scripts"),
        ]
        for root in roots:
            candidate = os.path.join(root, safe_name)
            if os.path.exists(candidate):
                try:
                    with open(candidate, "r", encoding="utf-8") as f:
                        return json.dumps({
                            "query": query,
                            "method": "filename_direct_read",
                            "results": [{
                                "file": safe_name,
                                "title": "Direct filename match",
                                "content": f.read(),
                                "relevance": 1.0,
                                "type": "filename"
                            }]
                        }, ensure_ascii=False, indent=2)
                except Exception as exc:
                    return json.dumps({"error": str(exc), "file": safe_name}, ensure_ascii=False, indent=2)
        return json.dumps({
            "error": "file not found",
            "file": safe_name,
            "hint": "For .md knowledge cards use local_search(filename='name.md'); for .py templates use get_golden_scripts(filename='name.py')."
        }, ensure_ascii=False, indent=2)
    # 兼容性处理：有些 Agent 会幻觉出 limit 参数
    n_results = kwargs.get('n_results', 5)
    if 'limit' in kwargs:
        n_results = kwargs['limit']
        
    logger.info(f"[TOOL] local_search 被调用: {query} (n={n_results})")
    
    q_lower = (query or "").lower()
    is_code_query = any(k in q_lower for k in ['script', 'code', 'python', 'verified', 'golden', 'example', 'dg0', 'ghost', 'staged', 'thermal', 'thermo', 'temperature', 'case4', 'case5', 'case6', 'case7', 'newmark', 'dynamic', 'flutter', 'scanlan', 'shm', 'damage', 'eigen', 'p-delta', 'pdelta', 'geometric', 'nonlinear', '脚本', '代码', '幽灵', '阶段', '温度', '热力', '热-力', '工况1', '工况3', '工况4', '工况5', '工况6', '工况7', '移动荷载', '动力', '颤振', '损伤', '刚度反演', '几何非线性', '非线性'])
    direct_priority_files = []
    if any(k in q_lower for k in ["case7", "工况7", "shm", "damage", "damage identification", "stiffness inversion", "损伤", "损伤识别", "刚度反演"]):
        direct_priority_files = [
            ("bridge_shm_damage_guardrails.md", "Mandatory SHM damage guardrail", 1.0),
            (os.path.join("golden_scripts", "recipe_bridge_shm_damage_scan.py"), "Mandatory SHM damage golden recipe", 0.98),
        ]
    elif any(k in q_lower for k in ["case6", "工况6", "flutter", "scanlan", "wind", "aero", "eigen", "颤振", "风速", "特征值"]):
        direct_priority_files = [
            ("bridge_eigen_solver_ladder.md", "Mandatory eigen solver ladder", 1.0),
            ("bridge_flutter_scan_guardrails.md", "Mandatory flutter guardrail", 0.99),
            (os.path.join("golden_scripts", "recipe_bridge_flutter_scan.py"), "Mandatory flutter golden recipe", 0.98),
            ("physics_validation_rules.md", "Flutter physics validation rules", 0.90),
        ]
    elif any(k in q_lower for k in ["case5", "工况5", "newmark", "dynamic", "moving load", "moving train", "移动荷载", "列车", "动力", "时程"]):
        direct_priority_files = [
            ("bridge_newmark_dynamics_guardrails.md", "Mandatory Newmark dynamics guardrail", 1.0),
            (os.path.join("golden_scripts", "recipe_bridge_newmark_dynamics.py"), "Mandatory Newmark dynamics golden recipe", 0.98),
        ]
    elif any(k in q_lower for k in ["case4", "工况4", "p-delta", "pdelta", "p delta", "geometric nonlinear", "几何非线性", "非线性", "st venant", "hyperelastic"]):
        direct_priority_files = [
            ("bridge_pdelta_guardrails.md", "Mandatory P-Delta guardrail", 1.0),
            (os.path.join("golden_scripts", "recipe_bridge_pdelta_guardrails.py"), "Mandatory P-Delta golden recipe", 0.98),
        ]
    elif any(k in q_lower for k in ["case3", "工况3", "thermal", "thermo", "temperature", "温度", "热力", "热-力"]):
        direct_priority_files = [
            ("bridge_thermal_coupling_guardrails.md", "Mandatory thermal guardrail", 1.0),
            (os.path.join("golden_scripts", "recipe_bridge_thermal_coupling_guardrails.py"), "Mandatory thermal golden recipe", 0.98),
        ]
    elif any(k in q_lower for k in ["case2", "工况2", "sigma0", "初应力", "预应力"]):
        direct_priority_files = [
            ("bridge_static_prestress_guardrails.md", "Mandatory static prestress guardrail", 1.0),
            (os.path.join("golden_scripts", "recipe_bridge_static_prestress_guardrails.py"), "Mandatory static prestress golden recipe", 0.98),
        ]
    elif any(k in q_lower for k in ["validation", "validate", "verification", "converged", "von mises", "result file", "output file", "物理验证", "验证", "通过", "结果文件", "输出文件", "收敛", "应力", "位移"]):
        direct_priority_files = [
            ("physics_validation_rules.md", "Mandatory Physics Validation Rules", 1.0),
            ("failure_case_review.md", "Mandatory Failure Case Review", 0.96),
        ]
    # Fast path for injected bridge-analysis guardrails. This avoids slow embedding
    # model startup and makes the anti-hallucination card reliably visible.
    if any(k in q_lower for k in ["case2", "工况2", "case3", "工况3", "case4", "工况4", "case5", "工况5", "case6", "工况6", "case7", "工况7", "newmark", "dynamic", "moving", "flutter", "scanlan", "eigen", "shm", "damage", "p-delta", "pdelta", "geometric", "nonlinear", "几何非线性", "非线性", "移动荷载", "动力", "颤振", "风速", "损伤", "刚度反演", "初应力", "sigma0", "guardrails", "thermal", "thermo", "temperature", "温度", "热力", "热-力", "validation", "validate", "verification", "converged", "von mises", "物理验证", "验证", "通过", "结果文件", "输出文件", "收敛", "位移", "应力"]):
        try:
            kw_res = json.loads(_keyword_search(query))
            if kw_res.get("results") or direct_priority_files:
                priority_results = []
                base_knowledge = os.path.join(os.path.dirname(__file__), "data", "local_knowledge")
                for rel_file, title, relevance in direct_priority_files:
                    fpath = os.path.join(base_knowledge, rel_file)
                    if os.path.exists(fpath):
                        try:
                            with open(fpath, "r", encoding="utf-8") as f:
                                content = f.read()
                            priority_results.append({
                                "file": rel_file.replace("\\", "/"),
                                "title": title,
                                "content": content[:2500] + "..." if len(content) > 2500 else content,
                                "relevance": relevance,
                                "type": "mandatory_direct_hit"
                            })
                        except Exception as exc:
                            logger.warning("failed to read priority RAG file %s: %s", rel_file, exc)
                seen_files = {item.get("file") for item in priority_results}
                keyword_results = []
                for item in kw_res.get("results", []):
                    fname = item.get("file")
                    if fname in seen_files:
                        continue
                    keyword_results.append({
                        "file": fname,
                        "title": "Domain RAG Guardrail" if "guardrail" in (fname or "").lower() else "Keyword Match",
                        "content": item.get("content"),
                        "relevance": 0.99 if any(k in (fname or '').lower() for k in ['bridge_', 'guardrail', 'recipe_']) else 0.75,
                        "type": "keyword_fast_path"
                    })
                return json.dumps({
                    "query": query,
                    "method": "keyword_fast_path_domain_guardrail",
                    "results": (priority_results + keyword_results)[:6]
                }, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"领域关键词快路径失败，回退常规RAG: {e}")
    results_pool = []
    
    # 1. 语义检索默认关闭：本项目优先使用可解释的本地关键词/文件名检索，避免 sentence-transformers 联网或旧向量缓存误导。
    if False and not is_code_query:
        try:
            kb = get_knowledge_base()
            semantic_results = kb.search(query, n_results=n_results)
            for res in semantic_results:
                results_pool.append({
                    "file": res['metadata']['source'],
                    "title": res['metadata']['title'],
                    "content": res['content'],
                    "relevance": 1.0 - res['distance'],
                    "type": "semantic"
                })
        except BaseException as e:
            logger.warning(f"语义检索失败: {e}")
    
    # 2. 关键词检索 (针对脚本查找特别增强)
    # 如果查询包含代码相关关键词，或者语义检索结果太少，强制运行关键词搜索
    
    if is_code_query or len(results_pool) < 2:
        logger.info(f"[TOOL] 触发关键词增强搜索: {query}")
        try:
            kw_res_json = _keyword_search(query)
            kw_res = json.loads(kw_res_json)
            if "results" in kw_res:
                for res in kw_res["results"]:
                    # 避免重复 (简单通过文件名判断)
                    if not any(existing['file'] == res['file'] for existing in results_pool):
                        results_pool.append({
                            "file": res['file'],
                            "title": "Keyword Match",
                            "content": res['content'],
                            "relevance": 0.9 if 'golden' in res['file'] else 0.5, # 提升 golden script 的权重
                            "type": "keyword"
                        })
        except Exception as e:
            logger.warning(f"关键词搜索失败: {e}")

    # 3. 排序与截断
    # 优先显示 golden_scripts
    results_pool.sort(key=lambda x: ((3 if any(k in x['file'].lower() for k in ['bridge_', 'thermal', 'guardrails', 'recipe_']) else 0) + (1 if 'golden' in x['file'] else 0), x['relevance']), reverse=True)
    
    final_results = results_pool[:5]
    
    return json.dumps({
        "query": query,
        "method": "hybrid_search",
        "results": final_results
    }, ensure_ascii=False, indent=2)

def get_golden_scripts(filename: str = None, **kwargs) -> str:
    """Return local RAG golden scripts; accepts query/tags kwargs defensively."""
    logger.info("[TOOL] get_golden_scripts called (filename=%s, kwargs=%s)", filename, kwargs)
    base = os.path.join(os.path.dirname(__file__), 'data', 'local_knowledge', 'golden_scripts')

    if not os.path.isdir(base):
        return json.dumps({"error": "golden_scripts dir not found", "scripts": []}, ensure_ascii=False)

    if filename is None:
        for alias in ("name", "file", "script", "path"):
            if kwargs.get(alias):
                filename = str(kwargs.get(alias))
                break

    query_terms = []
    for key in ("query", "tag", "tags", "keyword", "keywords"):
        value = kwargs.get(key)
        if isinstance(value, (list, tuple, set)):
            raw_values = [str(v).lower() for v in value]
        elif value:
            raw_values = str(value).lower().replace(",", " ").split()
        else:
            raw_values = []
        for raw in raw_values:
            query_terms.append(raw)
            query_terms.extend(part for part in raw.replace("_", " ").replace("-", " ").split() if part)

    if filename:
        if ".." in filename or "/" in filename or "\\" in filename:
            return "Error: Invalid filename"

        if str(filename).lower().endswith(".md"):
            return json.dumps({
                "success": False,
                "error": "wrong tool for markdown knowledge card",
                "file": filename,
                "correct_tool": "local_search",
                "correct_call": "print(local_search(query='read exact knowledge card', filename='%s'))" % filename,
                "hint": "Use get_golden_scripts only for .py files under data/local_knowledge/golden_scripts. Use local_search for .md knowledge cards."
            }, ensure_ascii=False, indent=2)
        if not str(filename).lower().endswith(".py"):
            return json.dumps({
                "success": False,
                "error": "golden script filename must end with .py",
                "file": filename,
                "hint": "Use get_golden_scripts(filename='name.py') for Python templates, or local_search(filename='name.md') for knowledge cards."
            }, ensure_ascii=False, indent=2)

        fpath = os.path.join(base, filename)
        if os.path.exists(fpath):
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    return f.read()
            except Exception as e:
                return f"Error reading file: {e}"
        return f"File not found: {filename}"

    scripts = []
    def _sort_key(name):
        lname = name.lower()
        if "recipe_bridge_shm_damage" in lname:
            priority = -50
        elif "recipe_bridge_flutter" in lname:
            priority = -45
        elif "recipe_bridge_newmark" in lname:
            priority = -40
        elif "recipe_bridge_pdelta" in lname:
            priority = -35
        elif "recipe_bridge_thermal" in lname:
            priority = -30
        elif "recipe_bridge_static" in lname:
            priority = -25
        elif "bridge_" in lname and "guardrails" in lname:
            priority = -20
        elif any(k in lname for k in ["ghost", "stage", "staged", "case2", "case3", "case4", "case5", "case6", "case7", "thermal", "newmark", "dynamic", "flutter", "scanlan", "shm", "damage", "pdelta", "nonlinear", "guardrails"]):
            priority = 0
        else:
            priority = 1
        return (priority, name)

    for fname in sorted(os.listdir(base), key=_sort_key):
        if not fname.endswith('.py'):
            continue
        fpath = os.path.join(base, fname)
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                content = f.read()
            haystack = (fname + "\n" + content[:3000]).lower()
            match_score = sum(1 for term in query_terms if term in haystack) if query_terms else 0
            if query_terms and match_score <= 0:
                continue
            scripts.append({
                "filename": fname,
                "summary": content[:500],
                "match_score": match_score
            })
        except Exception as e:
            logger.warning(f"failed to read golden script {fname}: {e}")

    if scripts and query_terms:
        scripts.sort(key=lambda item: (-int(item.get("match_score", 0)), _sort_key(item.get("filename", ""))))

    fallback_used = False
    if query_terms and not scripts:
        fallback_used = True
        for fname in sorted(os.listdir(base), key=_sort_key):
            if not fname.endswith('.py'):
                continue
            fpath = os.path.join(base, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    content = f.read()
                scripts.append({"filename": fname, "summary": content[:500]})
            except Exception as e:
                logger.warning(f"failed to read golden script {fname}: {e}")

    return json.dumps({
        "count": len(scripts),
        "accepted_kwargs": sorted(kwargs.keys()),
        "fallback_used": fallback_used,
        "usage": "Use get_golden_scripts() to list scripts, or get_golden_scripts(filename='name.py') for full content.",
        "scripts": scripts
    }, ensure_ascii=False, indent=2)

def online_search(query: str) -> str:
    """
    真实在线搜索 (基于 Bing Scraper，解决 SSL/GFW 问题)
    """
    logger.info(f"[TOOL] online_search 被调用: {query}")
    print(f"[System] 正在进行联网搜索: {query} ...")
    
    url = "https://www.bing.com/search"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    params = {"q": query}
    
    try:
        # 使用 verify=False 绕过 SSL 问题
        # 设置连接超时为5秒，读取超时为10秒
        resp = requests.get(url, params=params, headers=headers, verify=False, timeout=(5, 10))
        
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            results = []
            
            # Bing results are usually in <li class="b_algo">
            for li in soup.find_all('li', class_='b_algo'):
                h2 = li.find('h2')
                if h2 and h2.find('a'):
                    link_tag = h2.find('a')
                    title = link_tag.get_text(strip=True)
                    link = link_tag['href']
                    
                    # Snippet is usually in <div class="b_caption"> <p>
                    snippet = ""
                    caption = li.find('div', class_='b_caption')
                    if caption:
                        p = caption.find('p')
                        if p:
                            snippet = p.get_text(strip=True)
                    
                    results.append({
                        "title": title,
                        "link": link,
                        "snippet": snippet
                    })
            
            if not results:
                logger.info(f"[TOOL] 搜索完成，未找到结果")
                return json.dumps({
                    "query": query,
                    "results": [],
                    "message": "未找到相关结果"
                }, ensure_ascii=False)
            
            logger.info(f"[TOOL] 搜索完成，找到 {len(results)} 个结果")
            return json.dumps({
                "query": query,
                "method": "bing_scraper",
                "results": results[:5]  # 返回前5个结果
            }, ensure_ascii=False, indent=2)
            
        else:
            logger.error(f"[TOOL] 搜索失败: HTTP {resp.status_code}")
            return json.dumps({
                "error": f"HTTP {resp.status_code}",
                "message": "搜索引擎返回错误状态码"
            }, ensure_ascii=False)
            
    except requests.exceptions.Timeout:
        logger.error(f"[TOOL] 搜索超时")
        return json.dumps({
            "error": "Timeout",
            "message": "搜索请求超时，请检查网络连接"
        }, ensure_ascii=False)
    except Exception as e:
        logger.error(f"[TOOL] 搜索异常: {e}")
        return json.dumps({
            "error": str(e),
            "message": "搜索过程中发生异常"
        }, ensure_ascii=False)

def run_simulation(script: str) -> str:
    """提交脚本到 FEniCS 服务器并等待结果"""
    logger.info("[TOOL] run_simulation 被调用")
    submit_url = f"{FENICS_SERVER_URL}/submit"
    
    try:
        r = requests.post(submit_url, json={'script': script}, timeout=15)
        r.raise_for_status()
        job_id = r.json().get('job_id')
        
        if not job_id:
            return json.dumps({'error': 'no job_id returned'}, ensure_ascii=False)
        
        logger.info(f"Job ID: {job_id}，轮询结果...")
        
        # 轮询结果
        result_url = f"{FENICS_SERVER_URL}/result/{job_id}"
        start = time.time()
        while time.time() - start < 1800:
            rr = requests.get(result_url, timeout=10)
            if rr.status_code == 200:
                data = rr.json()
                status = data.get('status')
                if status in ['completed', 'failed']:
                    logger.info(f"仿真完成，状态: {status}")
                    return json.dumps({
                        'job_id': job_id,
                        'status': status,
                        'data': data
                    }, ensure_ascii=False, indent=2)
            time.sleep(2)
        
        return json.dumps({'job_id': job_id, 'error': 'timeout'}, ensure_ascii=False)
        
    except Exception as e:
        logger.error(f"运行仿真失败: {e}")
        return json.dumps({'error': str(e)}, ensure_ascii=False)

def _normalize_fenics_script_path(path: str) -> str:
    """Return a safe absolute path under temp_scripts/fenics_drafts."""
    if not path:
        raise ValueError("script path is required")

    os.makedirs(FENICS_DRAFT_DIR, exist_ok=True)
    candidate = path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)
    abs_path = os.path.abspath(candidate)
    allowed_root = os.path.abspath(FENICS_DRAFT_DIR)

    try:
        common = os.path.commonpath([allowed_root, abs_path])
    except ValueError as exc:
        raise ValueError("script path must stay inside temp_scripts/fenics_drafts") from exc

    if common != allowed_root:
        raise ValueError("script path must stay inside temp_scripts/fenics_drafts")
    if not abs_path.endswith(".py"):
        raise ValueError("script path must end with .py")
    return abs_path


def _relative_to_project(path: str) -> str:
    try:
        return os.path.relpath(path, PROJECT_ROOT)
    except ValueError:
        return path



def _fenics_script_completion_check(content: str) -> Dict[str, Any]:
    """Heuristic guard: END_OF_SCRIPT only counts after a runnable solve/output skeleton exists."""
    text = content or ""
    lines = [line.strip() for line in text.splitlines()]
    has_independent_end_marker = bool(lines) and lines[-1] == "# END_OF_SCRIPT"
    has_dx_measure = bool(re.search(r"(\*\s*dx\b|\bdx\s*\()", text))
    has_variational_form = (
        ("inner(" in text or "dot(" in text or ".dx(" in text or "grad(" in text)
        and has_dx_measure
    )
    has_assembly_or_solve = (
        "assemble(" in text
        or "solve(" in text
        or "LUSolver" in text
        or "SLEPcEigenSolver" in text
        or "PETScMatrix" in text
    )
    checks = {
        "has_import": "from dolfin import" in text,
        "has_function_spaces": "FunctionSpace(" in text or "VectorFunctionSpace(" in text,
        "has_trial_test": "TrialFunction" in text and "TestFunction" in text,
        "has_variational_form": has_variational_form,
        "has_assembly_or_solve_form": has_assembly_or_solve,
        "has_solver": "solve(" in text or "LUSolver" in text or ".solve(" in text or "SLEPcEigenSolver" in text,
        "has_result_marker": "--- FENICS JOB RESULT ---" in text and "json.dumps" in text,
        "has_output_files": "File(" in text or ".pvd" in text or "csv" in text.lower(),
        "has_final_end_marker": has_independent_end_marker,
    }
    missing = [name for name, ok in checks.items() if not ok]
    return {
        "completion_ok": not missing,
        "missing_completion_markers": missing,
        "completion_checks": checks,
    }

def _has_usable_rag_output(output: str) -> bool:
    lower = (output or "").lower()
    if not (output or "").strip():
        return False
    bad_markers = [
        "traceback",
        "file not found",
        "wrong tool for markdown",
        "golden script filename must end with .py",
        "invalid filename",
        "\"results\": []",
        "\"count\": 0",
    ]
    return not any(marker in lower for marker in bad_markers)

def create_fenics_script_file(filename: str = None, overwrite: bool = False) -> str:
    """Create an empty FEniCS draft script file and return its safe path."""
    os.makedirs(FENICS_DRAFT_DIR, exist_ok=True)
    raw_name = filename or f"fenics_{uuid.uuid4().hex[:10]}.py"
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_name).strip("._")
    if not safe_name:
        safe_name = f"fenics_{uuid.uuid4().hex[:10]}.py"
    if not safe_name.endswith(".py"):
        safe_name += ".py"

    abs_path = _normalize_fenics_script_path(os.path.join(FENICS_DRAFT_DIR, safe_name))
    if os.path.exists(abs_path) and not overwrite:
        return json.dumps({
            "success": False,
            "error": "file exists; pass overwrite=True or choose another filename",
            "path": _relative_to_project(abs_path)
        }, ensure_ascii=False, indent=2)

    with open(abs_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("")

    return json.dumps({
        "success": True,
        "path": _relative_to_project(abs_path),
        "message": "Draft created. Append code chunks, then run get_fenics_script_status and run_fenics_script_file."
    }, ensure_ascii=False, indent=2)


def append_fenics_script_file(path: str, chunk: str, add_newline: bool = True) -> str:
    """Append one code chunk to a FEniCS draft script file."""
    abs_path = _normalize_fenics_script_path(path)
    if not os.path.exists(abs_path):
        return json.dumps({"success": False, "error": "script file does not exist", "path": path}, ensure_ascii=False, indent=2)
    if chunk is None:
        chunk = ""

    with open(abs_path, "r", encoding="utf-8") as f:
        existing = f.read()
    if "# END_OF_SCRIPT" in [line.strip() for line in existing.splitlines()] and chunk.strip():
        status = json.loads(get_fenics_script_status(path))
        status.update({
            "success": False,
            "error": "script already has final # END_OF_SCRIPT marker; do not append another partial script. Call status/run if complete, or reset and rewrite the full script after RAG."
        })
        return json.dumps(status, ensure_ascii=False, indent=2)

    with open(abs_path, "a", encoding="utf-8", newline="\n") as f:
        f.write(chunk)
        if add_newline and not chunk.endswith("\n"):
            f.write("\n")

    return get_fenics_script_status(path)


def get_fenics_script_status(path: str) -> str:
    """Report draft size, syntax status, and end-marker presence without echoing the full script."""
    try:
        abs_path = _normalize_fenics_script_path(path)
        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()

        syntax_ok = True
        syntax_error = None
        try:
            ast.parse(content or "\n")
        except SyntaxError as exc:
            syntax_ok = False
            syntax_error = f"line {exc.lineno}: {exc.msg}"

        completion = _fenics_script_completion_check(content)
        return json.dumps({
            "success": True,
            "path": _relative_to_project(abs_path),
            "chars": len(content),
            "lines": len(content.splitlines()),
            "syntax_ok": syntax_ok,
            "syntax_error": syntax_error,
            "has_end_marker": completion["completion_checks"]["has_final_end_marker"],
            "completion_ok": completion["completion_ok"],
            "missing_completion_markers": completion["missing_completion_markers"],
            "completion_checks": completion["completion_checks"],
            "required_end_marker": "# END_OF_SCRIPT",
            "end_marker_hint": "Append exactly '# END_OF_SCRIPT' as an independent final line, and only after solve/output/result JSON are present. Runtime markers such as '--- END FENICS JOB RESULT ---' do not satisfy the file completeness marker.",
            "tail": content[-500:]
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc), "path": path}, ensure_ascii=False, indent=2)


def run_fenics_script_file(path: str, require_end_marker: bool = True) -> str:
    """Read a completed draft script from disk and submit it to the FEniCS backend."""
    try:
        abs_path = _normalize_fenics_script_path(path)
        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()

        meaningful_lines = [
            line for line in content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if len(content.strip()) < 200 or len(meaningful_lines) < 5 or "from dolfin import" not in content:
            return json.dumps({
                "success": False,
                "error": "script is too short or incomplete; refusing to submit empty/truncated script",
                "path": _relative_to_project(abs_path),
                "chars": len(content),
                "meaningful_lines": len(meaningful_lines)
            }, ensure_ascii=False, indent=2)

        if require_end_marker and "# END_OF_SCRIPT" not in content:
            return json.dumps({
                "success": False,
                "error": "missing # END_OF_SCRIPT marker; script may be truncated",
                "path": _relative_to_project(abs_path)
            }, ensure_ascii=False, indent=2)

        completion = _fenics_script_completion_check(content)
        if not completion["completion_ok"]:
            return json.dumps({
                "success": False,
                "error": "script has END marker but is not a complete runnable solve/output script",
                "path": _relative_to_project(abs_path),
                "missing_completion_markers": completion["missing_completion_markers"],
                "completion_checks": completion["completion_checks"]
            }, ensure_ascii=False, indent=2)

        try:
            ast.parse(content)
        except SyntaxError as exc:
            return json.dumps({
                "success": False,
                "error": f"syntax error before simulation: line {exc.lineno}: {exc.msg}",
                "path": _relative_to_project(abs_path)
            }, ensure_ascii=False, indent=2)

        logger.info("[TOOL] run_fenics_script_file 提交文件: %s (%d chars)", abs_path, len(content))
        result = json.loads(run_simulation(content))
        if isinstance(result, dict):
            result.setdefault("source_script_file", _relative_to_project(abs_path))
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.error("run_fenics_script_file 失败: %s", exc)
        return json.dumps({"success": False, "error": str(exc), "path": path}, ensure_ascii=False, indent=2)

def get_current_fenics_script_path() -> str:
    """Return the single fixed script path used by Coder."""
    os.makedirs(FENICS_DRAFT_DIR, exist_ok=True)
    return json.dumps({
        "success": True,
        "path": _relative_to_project(CURRENT_FENICS_SCRIPT_PATH),
        "absolute_path": os.path.abspath(CURRENT_FENICS_SCRIPT_PATH)
    }, ensure_ascii=False, indent=2)


def reset_current_fenics_script() -> str:
    """Clear the fixed current FEniCS script before Coder writes a new solution."""
    os.makedirs(FENICS_DRAFT_DIR, exist_ok=True)
    with open(CURRENT_FENICS_SCRIPT_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write("")
    _record_script_reset()
    return json.dumps({
        "success": True,
        "path": _relative_to_project(CURRENT_FENICS_SCRIPT_PATH),
        "message": "Fixed current script reset. Append code chunks to this file only."
    }, ensure_ascii=False, indent=2)


def append_current_fenics_script(chunk: str = "", add_newline: bool = True, content: str = None) -> str:
    """Append a code chunk to the single fixed current FEniCS script."""
    if content is not None and not chunk:
        chunk = content
    result = append_fenics_script_file(_relative_to_project(CURRENT_FENICS_SCRIPT_PATH), chunk, add_newline=add_newline)
    try:
        payload = json.loads(result)
        if payload.get("success"):
            _record_script_append(len(chunk or ""), len((chunk or "").splitlines()))
    except Exception:
        pass
    return result


def get_current_fenics_script_status() -> str:
    """Report status for the single fixed current FEniCS script."""
    return get_fenics_script_status(_relative_to_project(CURRENT_FENICS_SCRIPT_PATH))


def get_current_fenics_script_lines(start_line: int = 1, end_line: int = None, context: int = 0) -> str:
    """Return numbered lines from the fixed script for diagnostics."""
    try:
        abs_path = _normalize_fenics_script_path(CURRENT_FENICS_SCRIPT_PATH)
        if not os.path.exists(abs_path):
            return json.dumps({"success": False, "error": "current script does not exist", "path": _relative_to_project(abs_path)}, ensure_ascii=False, indent=2)
        with open(abs_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()

        total = len(lines)
        if end_line is None:
            end_line = total
        start = max(1, int(start_line) - max(0, int(context)))
        end = min(total, int(end_line) + max(0, int(context)))
        if total == 0:
            start, end = 1, 0
        if start > end and total > 0:
            return json.dumps({"success": False, "error": "invalid line range", "total_lines": total}, ensure_ascii=False, indent=2)

        numbered = [f"{i}: {lines[i-1]}" for i in range(start, end + 1)] if total else []
        return json.dumps({
            "success": True,
            "path": _relative_to_project(abs_path),
            "start_line": start,
            "end_line": end,
            "total_lines": total,
            "content": "\n".join(numbered)
        }, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, indent=2)


def replace_current_fenics_script_lines(start_line: int, end_line: int, replacement: str) -> str:
    """Replace an inclusive line range in the fixed script, then report status."""
    try:
        abs_path = _normalize_fenics_script_path(CURRENT_FENICS_SCRIPT_PATH)
        if not os.path.exists(abs_path):
            return json.dumps({"success": False, "error": "current script does not exist", "path": _relative_to_project(abs_path)}, ensure_ascii=False, indent=2)
        with open(abs_path, "r", encoding="utf-8") as f:
            original = f.read()
        lines = original.splitlines()
        total = len(lines)
        start = int(start_line)
        end = int(end_line)
        if start < 1 or end < start or end > total:
            return json.dumps({
                "success": False,
                "error": "invalid replacement range",
                "start_line": start,
                "end_line": end,
                "total_lines": total
            }, ensure_ascii=False, indent=2)

        repl_lines = [] if replacement is None or replacement == "" else replacement.splitlines()
        new_lines = lines[:start-1] + repl_lines + lines[end:]
        trailing_newline = "\n" if original.endswith("\n") or repl_lines else ""
        with open(abs_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(new_lines) + trailing_newline)

        status = json.loads(get_current_fenics_script_status())
        status.update({
            "edited": True,
            "replaced_start_line": start,
            "replaced_end_line": end,
            "replacement_lines": len(repl_lines)
        })
        _record_code_edit(start, end, len(repl_lines))
        return json.dumps(status, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, indent=2)


def run_current_fenics_script(require_end_marker: bool = True) -> str:
    """Run the single fixed current FEniCS script."""
    result = run_fenics_script_file(_relative_to_project(CURRENT_FENICS_SCRIPT_PATH), require_end_marker=require_end_marker)
    try:
        _record_simulation_result(json.loads(result))
    except Exception:
        pass
    return result
def check_backend() -> str:
    """检查 FEniCS 后端状态"""
    logger.info("[TOOL] check_backend 被调用")
    try:
        r = requests.get(f"{FENICS_SERVER_URL}/status", timeout=5)
        return json.dumps({'backend_status': r.json()}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({'error': str(e)}, ensure_ascii=False)

def fix_script(script: str, error_log: str) -> str:
    """自动修复脚本中的常见错误"""
    logger.info("[TOOL] fix_script 被调用")
    fixed = script
    fixes_applied = []
    
    # 常见错误修复规则
    if 'IntervalMesh' in error_log and 'incompatible constructor' in error_log:
        # IntervalMesh(N, L) -> IntervalMesh(N, 0.0, L)
        fixed = re.sub(r'IntervalMesh\(\s*(\w+)\s*,\s*(\w+)\s*\)', 
                      r'IntervalMesh(\1, 0.0, \2)', fixed)
        fixes_applied.append("修复 IntervalMesh 构造函数参数")
    
    if 'NameError' in error_log and 'dolfin' in error_log:
        # 添加缺失的导入
        if 'from dolfin import *' not in fixed:
            fixed = "from dolfin import *\n" + fixed
            fixes_applied.append("添加 dolfin 导入")
    
    if 'SyntaxError' in error_log:
        # 尝试修复常见语法错误
        fixes_applied.append("检测到语法错误，建议人工检查")
    
    return json.dumps({
        'fixed_script': fixed,
        'fixes_applied': fixes_applied,
        'original_length': len(script),
        'fixed_length': len(fixed),
        'changed': script != fixed
    }, ensure_ascii=False, indent=2)

# =============== System Prompts =================

MANAGER_PROMPT = """
你是项目经理（Manager_Agent），负责指导 Researcher_Agent 与 Coder_Agent 完成 FEniCS 仿真任务。

**你的身份**: 你只负责调度、判断下一步和总结，绝不亲自写代码。

**绝对禁令**:
❌ 每次只能委派一个 Agent。
❌ 严禁输出 Python 代码、代码块、`script = ...`、固定脚本工具调用或仿真结果。
❌ 严禁直接指挥 User_Proxy。
❌ 严禁要求 Coder “以代码块形式输出脚本供 User_Proxy 执行”；必须要求 Coder 使用固定脚本工具写入并运行。
❌ 只要 Researcher 没有基于真实仿真结果明确说 "验证通过"，不得发送 `TERMINATE`。
✅ 代码生成与修复都交给 `Coder_Agent`。
✅ 物理解释、理论核验、结果真假判断交给 `Researcher_Agent`。
✅ Coder 每次生成或修复脚本前必须先使用 RAG，但由 Coder 根据当前工况自主决定检索关键词和资料，不要在 Manager 指令中指定某个固定知识文件。

**硬性纠偏规则**:
- 如果 User_Proxy 返回拒绝、工具失败、空输出或 Coder 空消息，你只能重新指派 `@Coder_Agent` 进行正确的下一步工具调用，不能进入验证阶段。
- 如果最近一次 `run_current_fenics_script` 失败、segfault、边界/关键区域为0、或脚本明确报告 `converged=false`，必须指派 `@Coder_Agent` 重新 RAG 并完整重写；严禁要求 Researcher 接受旧成功版本、定性近似或带关键警告的结果。
- 你不能自己说“验证通过”“定性验证通过”“允许终止”或类似结论；这些结论只能由 `Researcher_Agent` 以自己的消息给出。
- 给 Researcher 的调度指令不要包含 `TERMINATE` 字样；只要求其回复“验证通过”或“验证失败”。
- 严禁说“RAG 为空但可基于通用知识继续”；RAG 不足时必须让 Coder 换关键词继续检索。
- 严禁编造 `Stage 0/1/2` 位移、求解器收敛、输出文件已生成等结果；只有 `run_current_fenics_script()` 的真实返回才算仿真结果。
- 严禁把 Manager 自己写的文字当成工具执行结果。

**决策逻辑**:
1. 收到用户需求 -> 指派 `@Coder_Agent`。
   - 只要求它先自主检索与当前工况相关的本地知识/稳定模板，再通过固定脚本工具 reset/append/status/run；不得要求它在聊天里贴完整脚本。
2. User_Proxy 返回仿真执行成功 -> 指派 `@Researcher_Agent` 做物理验证。
3. User_Proxy 返回仿真执行失败 -> 指派 `@Coder_Agent` 基于真实错误日志，先 RAG，再完整重写固定脚本。
4. Researcher 说 "验证失败" -> 指派 `@Coder_Agent` 基于验证意见，先 RAG，再完整重写固定脚本。
5. Researcher_Agent 以自己的消息说 "验证通过" 且最近一次仿真成功 -> 总结并 `TERMINATE`。若最近一次仿真失败，即使历史上有成功版本，也不得结束。

**标准工作流**:
用户需求 -> Manager -> Coder 自主RAG -> User_Proxy执行RAG -> Coder重写固定脚本 -> User_Proxy检查/运行 -> Manager -> Researcher自主RAG验证 -> 未通过回Coder / 通过结束。

**最终报告格式**:
项目全流程已完成。
- 固定脚本路径: ...
- 结果文件路径: ...
- 统计器摘要: ...
TERMINATE

**普通阶段唯一允许输出格式**:
@Coder_Agent
任务: ...

或

@Researcher_Agent
任务: ...

除最终报告外，禁止输出问候、解释、分析过程、代码、工具调用、编号长清单、Markdown代码块、模拟结果或给 User_Proxy 的指令。
"""
RESEARCHER_PROMPT = """
你是研究员（Researcher_Agent），负责物理建模咨询、理论验证和结果分析。
你的核心价值是利用本地知识库 (RAG) 与真实仿真输出，判断结果是否可信。

**核心原则**:
1. 先检索，后验证；检索关键词由你根据当前工况、错误日志、输出字段自主选择。
2. 只能基于真实 `stdout.log` / `results.json` / User_Proxy 返回 / 固定脚本状态做判断，禁止臆测不存在的结果。
3. 没有最近一次真实仿真成功记录时，禁止说 "验证通过"。
3a. 如果最近一次 User_Proxy 返回的是 `SIMULATION_FAILED`、`仿真执行失败`、`converged=false`、segfault、边界 DOF/关键区域为0、或关键边界出现未施加警告，必须判定为 **验证失败**；不得选择历史成功 job、不得接受“定性近似”来绕过最新失败。
3b. 不要输出“允许 Manager 发送 TERMINATE”这类措辞；只输出结论本身：**验证通过** 或 **验证失败**。
4. 一轮验证只检索一次；拿到 RAG 返回后必须给出明确结论，不要反复检索拖延。
5. 如果看到的“仿真结果”来自 Manager 或 Coder 的文字描述，而不是 User_Proxy 的 `SIMULATION_COMPLETED` / `仿真执行成功` / 统计器 last_run，则必须判定为验证失败，禁止基于这些文字做通过结论。

**工作流程**:
1. 被 Manager 唤起后，先用 `local_search(...)` 或 `get_golden_scripts()` 自主检索与当前工况相关的物理规则、稳定模板、错误修复案例或验证门槛。
2. 收到工具返回后，提取真实仿真指标：位移、应力、阶段结果、材料/边界/荷载诊断、输出文件列表等。
3. 对照任务要求与 RAG 知识判断：
   - 必要输出缺失、关键区域计数为0、边界 DOF 为0、阶段定义错误、荷载方向错误、未真实运行就有结果，均判定为验证失败。
   - 若结果量级合理、字段完整、输出文件存在、且与当前工况物理直觉一致，才可判定为验证通过。
4. 若失败，给 Coder_Agent 1-3 条完整重写建议，不要求按行号小修。

**输出格式**:
- 真实仿真值来源: ...
- 理论/物理核验: ...
- 结论: **验证通过** / **验证失败**
- 若失败，给 Coder 的完整重写建议: ...
"""
CODER_PROMPT = """
你是 FEniCS 2019 资深程序员（Coder_Agent）。
目标是生成可运行、可验证、符合当前工况物理意义的 FEniCS 2019.1.0 Python 脚本，并在失败时完整重写修复。

# 反幻觉规则
1. 必须先 RAG，后写代码。检索关键词由你根据用户工况自主决定；不要等待 Manager 指定某个固定知识文件。
2. RAG 工具调用必须单独发送并等待 User_Proxy 真实返回；禁止在同一个代码块里同时检索和写脚本/运行脚本。
3. 禁止模拟工具返回、禁止伪造仿真结果、禁止没运行就输出 `--- FENICS JOB RESULT ---`。
4. 只使用 FEniCS 2019.1.0 / dolfin 稳定 API；不确定 API 不要凭空编造。
5. 修复失败脚本时也必须重新 RAG，然后完整重写固定脚本；不要做局部行号小修。

# 固定脚本状态机（系统会强制检查）
每次生成或修复必须按下面顺序分步调用，每个工具块只做一个阶段：
1. RAG: `local_search(...)` / `get_golden_scripts(...)` / `get_knowledge_structure()`，由你自主选择。
   - `.md` 知识卡必须用 `local_search(filename="xxx.md")` 或关键词 `local_search(query="...")`。
   - `.py` golden 模板必须用 `get_golden_scripts(filename="xxx.py")`。
   - 如果工具返回 `File not found`、`wrong tool`、`count=0` 或空结果，说明本次 RAG 无效，必须换正确工具/关键词继续检索，不能 reset 或写代码。
   - 对温度梯度任务，专用知识优先级高于通用热分析 demo；不要把 `recipe_bridge_thermal_3d.py` 当作主模板。
   - 对 P-Delta 任务，专用知识优先级高于通用 nonlinear demo；不要把 `recipe_bridge_nonlinear_3d.py` 当作主模板。
2. Reset: `reset_current_fenics_script()`。
3. Append: `append_current_fenics_script(...)`。必须优先一次性完整写入整个脚本；只有模型单次输出长度不够时才分块，分块也应控制在 2~4 块内，禁止按十几行小块追加。
   - 分块写入时，中间块严禁包含 `# END_OF_SCRIPT`。
   - 只有最后一块，且脚本已经包含材料/边界、变分形式、组装或求解、PVD/CSV输出、`--- FENICS JOB RESULT ---` 与 `json.dumps` 后，才允许追加独立行 `# END_OF_SCRIPT`。
4. Status: `get_current_fenics_script_status()`，确认 `syntax_ok=true`、`has_end_marker=true` 且 `completion_ok=true`。若尚无 `# END_OF_SCRIPT` 可继续 append；若已有 `# END_OF_SCRIPT` 但 `completion_ok=false`，不得追加小片段，必须下一轮先 RAG、reset，再完整重写。
5. Run: `run_current_fenics_script()`。

# 工具调用格式（强制）
- 每一条 Coder 回复只能包含一个工具调用块；工具块前后禁止出现任何自然语言、解释、问候或总结。
- 不要把 reset/append/status/run 写在同一条回复里。
- Python 工具块必须是真实调用，例如 `print(local_search(query="..."))` 或 `print(reset_current_fenics_script())`；不要写“预期返回”“已执行”“内容同上”。
- `get_golden_scripts` 的正确用法是 `print(get_golden_scripts())` 或 `print(get_golden_scripts(filename="xxx.py"))`；不要依赖 tags 参数。
- 收到 User_Proxy 的工具结果后，只决定并发起下一步工具调用；不要口头宣布脚本已写入、已运行或文件已生成。
- 若被系统提示“当前阶段需要 RAG/reset/append/status/run”，下一条回复必须只调用该阶段允许的工具。

唯一正确示例：
```python
print(local_search(query="FEniCS 2019 BoxMesh DG0 UserExpression boundary condition"))
```

错误示例：
好的，现在我先检索。
```python
print(local_search(query="..."))
```

# 编码要求
1. 不要在聊天中输出完整脚本，只能写入 `temp_scripts/fenics_drafts/current_fenics_script.py`。
2. 脚本必须保留用户给出的几何、材料、边界、荷载、输出文件和验证指标；测试网格可以粗化，但必须在脚本注释和控制台诊断中说明生产网格参数。
3. 求解前必须打印材料/阶段/边界/荷载诊断；关键区域或边界 DOF 为0时应 `CRITICAL ERROR` 并退出，避免浪费长仿真。
4. 大模型优先显式组装：`assemble(a, keep_diagonal=True)`, `bc.apply(A,b)`, `A.ident_zeros()`, `LUSolver(A, "default")`。
5. CSV/JSON 输出使用 UTF-8；控制台结果必须在真实求解后打印 `--- FENICS JOB RESULT ---` 和 JSON；脚本文件最后必须另起独立一行写 `# END_OF_SCRIPT` 作为完整性标记。
6. 输出 JSON 字段应覆盖当前工况要求，例如阶段位移、材料/边界诊断、收敛状态、输出文件列表。
7. 禁止使用 `...`、`省略`、`同上` 代替代码；禁止旧格式 `script = '''...'''`。

# 自检清单
写脚本前和运行前都要自检：导入兼容、边界可命中、材料/阶段非空、荷载符号正确、输出文件完整、JSON 字段真实来自求解结果。
"""
# =============== Agent 构建 =================

def build_multiagent_system():
    """
    构建完整的多Agent系统
    
    本系统为无状态 RAG 模式：不加载/保存对话历史。
    """
    
    # 清洗 LLM 配置
    allowed_keys = {"config_list", "temperature", "timeout"}
    llm_conf = {k: v for k, v in LLM_BASE_CONFIG.items() if k in allowed_keys}
    
    cleaned_list = []
    for item in llm_conf.get("config_list", []):
        if not isinstance(item, dict):
            continue
        new_item = item.copy()
        if 'max_new_tokens' in new_item:
            new_item['max_tokens'] = new_item.pop('max_new_tokens')
        # 移除不应在 config_list 项中的参数 (应该在顶层)
        for bad_key in ['stop_token_ids', 'stop_sequences', 'functions', 'tools', 'temperature']:
            new_item.pop(bad_key, None)
        cleaned_list.append(new_item)
    llm_conf['config_list'] = cleaned_list
    
    # UserProxy - 实际执行所有工具函数,并自动检测脚本执行请求
    class SmartUserProxy(UserProxyAgent):
        """增强型UserProxy,自动检测Executor的脚本并执行"""
        
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._executed_scripts = set()  # 记录已执行的脚本hash,防止重复执行
            self._domain_guardrail_seen = False
            self._coder_workflow_phase = "need_rag"

        def _extract_tool_sequence(self, code: str, tool_names: set) -> List[str]:
            """Return actual tool calls in source order, ignoring names inside strings."""
            try:
                tree = ast.parse(code)
            except SyntaxError:
                return []

            calls = []

            class _CallVisitor(ast.NodeVisitor):
                def visit_Call(self, node):
                    name = None
                    if isinstance(node.func, ast.Name):
                        name = node.func.id
                    elif isinstance(node.func, ast.Attribute):
                        name = node.func.attr
                    if name in tool_names:
                        calls.append((getattr(node, "lineno", 0), getattr(node, "col_offset", 0), name))
                    self.generic_visit(node)

            _CallVisitor().visit(tree)
            calls.sort(key=lambda item: (item[0], item[1]))
            return [name for _, _, name in calls]

        def _compile_tool_code_with_printed_exprs(self, code: str, tool_names: set):
            """Compile code after wrapping bare top-level tool calls in print(...)."""
            tree = ast.parse(code)

            class _BareToolPrinter(ast.NodeTransformer):
                def visit_Expr(self, node):
                    self.generic_visit(node)
                    call = node.value
                    if isinstance(call, ast.Call):
                        name = None
                        if isinstance(call.func, ast.Name):
                            name = call.func.id
                        elif isinstance(call.func, ast.Attribute):
                            name = call.func.attr
                        if name in tool_names:
                            return ast.copy_location(
                                ast.Expr(value=ast.Call(func=ast.Name(id="print", ctx=ast.Load()), args=[call], keywords=[])),
                                node,
                            )
                    return node

            tree = _BareToolPrinter().visit(tree)
            ast.fix_missing_locations(tree)
            return compile(tree, "<agent_tool_block>", "exec")

        def _extract_dsml_tool_calls(self, content: str) -> List[Dict[str, Any]]:
            """Parse DeepSeek DSML-style tool calls into [{name, kwargs}]."""
            calls = []
            for match in re.finditer(r'<[^<]*invoke\s+name="([^"]+)"[^>]*>(.*?)</[^<]*invoke>', content or "", re.DOTALL):
                name = match.group(1).strip()
                body = match.group(2) or ""
                kwargs = {}
                for p in re.finditer(r'<[^<]*parameter\s+name="([^"]+)"[^>]*>(.*?)</[^<]*parameter>', body, re.DOTALL):
                    key = p.group(1).strip()
                    value = re.sub(r'<[^>]+>', '', p.group(2) or '').strip()
                    kwargs[key] = value
                calls.append({"name": name, "kwargs": kwargs})
            return calls

        def _extract_xml_function_calls(self, content: str) -> List[Dict[str, Any]]:
            """Parse <function><function_name>name</function_name><function_argument>{...}</function_argument></function>."""
            calls = []
            for match in re.finditer(r'<function>\s*<function_name>(.*?)</function_name>\s*<function_argument>(.*?)</function_argument>\s*</function>', content or "", re.DOTALL):
                name = re.sub(r'<[^>]+>', '', match.group(1) or '').strip()
                raw_args = (match.group(2) or '').strip()
                kwargs = {}
                if raw_args:
                    try:
                        parsed = json.loads(raw_args)
                        if isinstance(parsed, dict):
                            kwargs = parsed
                    except Exception:
                        kwargs = {"__raw_argument__": raw_args}
                calls.append({"name": name, "kwargs": kwargs})
            return calls

        def _is_pure_tool_message(self, content: str) -> bool:
            """Coder/Researcher tool messages must contain only one tool-call block."""
            s = (content or "").strip()
            if not s:
                return False
            if re.fullmatch(r"```python\s*.*?\s*```", s, re.DOTALL):
                return True
            if re.fullmatch(r"(?:<function>\s*<function_name>.*?</function_name>\s*<function_argument>.*?</function_argument>\s*</function>\s*)+", s, re.DOTALL):
                return True
            if "<｜｜DSML" in s:
                return s.startswith("<｜｜DSML") and s.endswith("</｜｜DSML｜｜tool_calls>")
            return False

        def _coder_phase_hint(self) -> str:
            hints = {
                "need_rag": "当前阶段需要 Coder 先单独调用 RAG，并由 Coder 根据当前工况自主选择 local_search(...) / get_golden_scripts(...) / get_knowledge_structure()。",
                "need_reset": "当前阶段只允许 Coder 调用 reset_current_fenics_script()。",
                "need_append": "当前阶段只允许 Coder 调用 append_current_fenics_script(...)。优先一次性写入完整脚本；如必须分块，请用尽量少的大块，不要十几行小块追加。",
                "need_status": "当前阶段只允许 Coder 继续 append_current_fenics_script(...) 或调用 get_current_fenics_script_status()。仅当脚本还没有 # END_OF_SCRIPT 时才可继续 append；一旦已有 END 但 completion_ok=false，必须停止追加，下一轮重新 RAG 后 reset 并完整重写。不要 run。",
                "need_run": "当前阶段只允许 Coder 调用 run_current_fenics_script()。",
            }
            return hints.get(self._coder_workflow_phase, f"未知阶段: {self._coder_workflow_phase}")

        def _validate_coder_tool_phase(self, sequence: List[str], called_tools: set, rag_tools: set, script_tools: set) -> str:
            if not called_tools:
                return ""
            if called_tools & rag_tools:
                return ""

            phase = self._coder_workflow_phase
            sequence_script_tools = [name for name in sequence if name in script_tools]
            if not sequence_script_tools:
                return ""
            if len(sequence_script_tools) != 1:
                return (
                    "❌ 已拒绝执行：Coder 的脚本工具必须按状态机分步调用，"
                    "每个工具块只能包含一个脚本阶段工具。顺序是 RAG -> reset -> append -> status -> run。"
                )

            tool = sequence_script_tools[0]
            allowed_by_phase = {
                "need_rag": set(),
                "need_reset": {"reset_current_fenics_script"},
                "need_append": {"append_current_fenics_script"},
                "need_status": {"append_current_fenics_script", "get_current_fenics_script_status"},
                "need_run": {"run_current_fenics_script"},
            }
            if tool not in allowed_by_phase.get(phase, set()):
                return f"❌ 已拒绝执行：Coder 工具调用乱序。{self._coder_phase_hint()}"
            return ""

        def _update_coder_phase_after_success(self, called_tools: set, output: str, rag_tools: set) -> None:
            guardrail_markers = [
                "assign_sigma0_for_cell",
                "must_abort_before_solve",
                "RAG硬规则卡",
                "关键反幻觉规则",
            ]
            if called_tools & rag_tools:
                lower_output = (output or "").lower()
                has_usable_rag = _has_usable_rag_output(output)
                if has_usable_rag:
                    self._domain_guardrail_seen = True
                    self._coder_workflow_phase = "need_reset"
                    detail = "Coder completed real RAG; script rewrite is allowed"
                    if any(marker in output for marker in guardrail_markers):
                        detail = "Coder matched domain RAG guardrails; script rewrite is allowed"
                    _record_rag_event("guardrail_hit", "Coder", detail)
                    logger.info("[SmartUserProxy] Coder completed real RAG")
                return

            if "reset_current_fenics_script" in called_tools:
                self._coder_workflow_phase = "need_append"
            elif "append_current_fenics_script" in called_tools:
                try:
                    append_payload = json.loads(output.strip())
                    append_ok = (
                        append_payload.get("success") is True
                        and append_payload.get("syntax_ok") is True
                        and append_payload.get("has_end_marker") is True
                        and append_payload.get("completion_ok") is True
                        and int(append_payload.get("chars") or 0) >= 200
                    )
                    has_final_but_incomplete = (
                        append_payload.get("has_end_marker") is True
                        and append_payload.get("completion_ok") is not True
                    )
                except Exception:
                    append_ok = False
                    has_final_but_incomplete = False
                if append_ok:
                    self._coder_workflow_phase = "need_run"
                elif has_final_but_incomplete:
                    self._coder_workflow_phase = "need_rag"
                    self._domain_guardrail_seen = False
                    _record_rag_event("policy_block", "Coder", "脚本已有END但完整性检查未通过，禁止继续追加，要求重新RAG并完整重写")
                else:
                    self._coder_workflow_phase = "need_status"
            elif "get_current_fenics_script_status" in called_tools:
                try:
                    status_payload = json.loads(output.strip())
                    ok = (
                        status_payload.get("success") is True
                        and status_payload.get("syntax_ok") is True
                        and status_payload.get("has_end_marker") is True
                        and status_payload.get("completion_ok") is True
                        and int(status_payload.get("chars") or 0) >= 200
                    )
                except Exception:
                    ok = False
                if ok:
                    self._coder_workflow_phase = "need_run"
                else:
                    self._coder_workflow_phase = "need_rag"
                    self._domain_guardrail_seen = False
                    _record_rag_event("policy_block", "Coder", "脚本状态检查未通过，要求重新RAG并完整重写")
            elif "run_current_fenics_script" in called_tools:
                self._coder_workflow_phase = "need_rag"
                self._domain_guardrail_seen = False
        def generate_reply(self, messages=None, sender=None, **kwargs):
            # 关键修复: 从GroupChat的消息历史获取所有消息
            # 因为传入的messages可能是空的!
            if sender:
                all_messages = self.chat_messages.get(sender, [])
            else:
                all_messages = messages if messages else []
            
            logger.info("[SmartUserProxy] generate_reply被调用, 消息数量: %d, sender: %s", 
                       len(all_messages), sender.name if sender else None)
            
            # 只有 Manager 直接尝试 TERMINATE 时才拦截；调度/验证话语中提到 TERMINATE 不应抢先触发。
            if all_messages:
                _last = all_messages[-1]
                _last_content = _last.get('content', '') or ''
                _last_name = _last.get('name', '') or ''
                _direct_terminate = (
                    'Manager' in _last_name
                    and 'TERMINATE' in _last_content
                    and '@Researcher_Agent' not in _last_content
                    and '@Coder_Agent' not in _last_content
                )
            else:
                _direct_terminate = False
            if _direct_terminate:
                latest_validation_passed = False
                for _msg in reversed(all_messages):
                    _name = _msg.get('name', '')
                    _content = _msg.get('content', '') or ''
                    if "Researcher" not in _name:
                        continue
                    if "验证通过" in _content:
                        latest_validation_passed = _latest_simulation_succeeded()
                        break
                    if "验证失败" in _content:
                        latest_validation_passed = False
                        break
                if latest_validation_passed:
                    logger.info("[SmartUserProxy] 检测到TERMINATE且Researcher已验证通过且最近仿真成功,停止执行脚本")
                    return None
                logger.warning("[SmartUserProxy] 拦截提前TERMINATE: Researcher尚未验证通过")
                return "❌ 已拦截提前 TERMINATE：Researcher 尚未基于最近一次成功仿真明确验证通过。请安排 Researcher 验证或安排 Coder 修复。"
            # 检查所有消息中是否有来自Coder/Researcher的脚本
            if all_messages:
                # 倒序检查最近的消息,优先处理最新的脚本
                for i, msg in enumerate(reversed(all_messages[-10:])):  # 只检查最近10条
                    content = msg.get('content', '')
                    msg_name = msg.get('name', '')
                    
                    logger.info("[SmartUserProxy] 检查消息 %d: name='%s', content前50字符='%s'", 
                               i, msg_name, content[:50] if content else '')
                    
                    if "Manager" in msg_name:
                        latest_researcher_validation_passed = False
                        for _m in reversed(all_messages):
                            _name = _m.get("name", "")
                            _content = _m.get("content", "") or ""
                            if "Researcher" not in _name:
                                continue
                            if "验证通过" in _content:
                                latest_researcher_validation_passed = _latest_simulation_succeeded()
                                break
                            if "验证失败" in _content:
                                latest_researcher_validation_passed = False
                                break
                        stripped_manager = (content or "").strip()
                        normalized_manager = stripped_manager.lstrip("*").strip()
                        is_manager_delegation = (
                            normalized_manager.startswith("@Coder_Agent")
                            or normalized_manager.startswith("@Researcher_Agent")
                            or normalized_manager.startswith("@Fixer_Agent")
                        )
                        is_manager_final = "TERMINATE" in stripped_manager and latest_researcher_validation_passed
                        if not is_manager_delegation and not is_manager_final:
                            logger.warning("[SmartUserProxy] 拒绝 Manager 非协议输出")
                            _record_rag_event("policy_block", "Manager", "Manager输出不符合调度协议")
                            return "❌ Manager 输出格式违规：普通阶段只能输出 `@Coder_Agent\n任务: ...` 或 `@Researcher_Agent\n任务: ...`；最终总结只能在 Researcher 验证通过且最近仿真成功后输出并以 TERMINATE 结束。"
                        if "验证通过" in content and "@Researcher_Agent" not in content and not latest_researcher_validation_passed:
                            logger.warning("[SmartUserProxy] 拒绝 Manager 越权声明验证通过")
                            return "❌ Manager 越权：只有 Researcher_Agent 基于最近一次成功仿真才能声明验证通过。Manager 不能自称验证通过或替 Researcher 下结论。"
                        premature_done_markers = ["任务已完成", "任务正式结束", "无需进一步操作", "对话结束", "流程合规", "正式结束"]
                        if any(marker in content for marker in premature_done_markers) and not latest_researcher_validation_passed:
                            logger.warning("[SmartUserProxy] 拒绝 Manager 提前宣布完成")
                            return "❌ Manager 越权：Researcher_Agent 尚未基于最近一次成功仿真明确验证通过，不能宣布任务完成或结束。"
                        manager_forbidden_markers = [
                            "```python", "```bash", "<function", "<function_name>",
                            "script =", "from dolfin import", "python3 ", "copy", "save", "manual run",
                            "--- FENICS JOB RESULT ---", "Linear solver", "Stage 0", "Stage 1", "Stage 2", "u_z", "common knowledge", "expected result", "simulation completed", "results saved",
                        ]
                        if any(marker in content for marker in manager_forbidden_markers):
                            logger.warning("[SmartUserProxy] 拒绝 Manager 越权输出代码或工具调用")
                            _record_rag_event("policy_block", "Manager", "Manager越权输出代码/工具/伪结果")
                            return "❌ Manager 越权：Manager 只能调度 @Coder_Agent / @Researcher_Agent，不能写 Python、不能调用工具、不能声明仿真结果。请只输出给 Coder 或 Researcher 的下一步指令。"
                    # 1. 优先检测Coder、Researcher的脚本输出 (script = '''...''')
                    if ("Coder" in msg_name or "Researcher" in msg_name):
                        if "Researcher" in msg_name and "验证通过" in content and not _latest_simulation_succeeded():
                            logger.warning("[SmartUserProxy] 拒绝 Researcher 在最近仿真失败/缺失时声明验证通过")
                            return "❌ Researcher 验证无效：最近一次仿真未成功，不能声明验证通过。请基于真实失败日志给出验证失败和重写建议。"
                        logger.info("[SmartUserProxy] 检查来自%s的消息，是否包含脚本", msg_name)
                        import re, textwrap, hashlib
                        
                        # 多重清理：去除所有可能的代码块包裹
                        cleaned = content.strip()
                        
                        # 清理方案1: 移除Markdown代码块 ```python ... ```
                        cleaned = re.sub(r"```[\w]*\s*\n", "", cleaned)  # 移除开头 ```
                        cleaned = re.sub(r"\n```\s*$", "", cleaned)      # 移除结尾 ```
                        
                        # 清理方案2: 移除 <code> 标签
                        cleaned = re.sub(r"<code>|</code>", "", cleaned)
                        
                        # 清理方案3: 多行Markdown代码块 (嵌套在其他内容中)
                        cleaned = re.sub(r"```[\w\s]*\n", "", cleaned, flags=re.MULTILINE)
                        cleaned = re.sub(r"\n```", "", cleaned, flags=re.MULTILINE)
                        
                        # 现在尝试匹配 script = '''...''' 格式
                        # 使用多种匹配策略
                        script_match = None
                        
                        # 策略1: 标准格式 script = '''...'''
                        m1 = re.search(r"script\s*=\s*'''(.*?)'''", cleaned, re.DOTALL)
                        
                        # 策略2: 检测到旧式内联脚本但没有闭合时，直接拒绝执行。
                        if not m1 and re.search(r"script\s*=\s*'''", cleaned):
                            logger.warning("[SmartUserProxy] 检测到未闭合的旧式内联脚本，拒绝执行")
                            return "❌ 检测到脚本被截断：缺少结尾三引号。请使用文件式工具分块写入并运行脚本。"
                        
                        if m1 and not script_match:
                            script_match = m1.group(1)
                        # 固定文件模式下禁止执行旧式内联脚本。
                        if script_match:
                            logger.warning("[SmartUserProxy] 拒绝执行旧式内联脚本；必须使用 current_fenics_script.py 固定文件工具")
                            return "❌ 已禁止旧式内联脚本执行。请使用 reset_current_fenics_script / append_current_fenics_script / run_current_fenics_script。"
                    # 2. 检测工具调用：按 Agent 角色执行最小权限白名单。
                    if any(name in msg_name for name in ["Coder", "Researcher"]):
                        import re
                        import io
                        import sys

                        all_tool_funcs = {
                            "get_golden_scripts": get_golden_scripts,
                            "local_search": local_search,
                            "get_knowledge_structure": get_knowledge_structure,
                            "online_search": online_search,
                            "get_current_fenics_script_path": get_current_fenics_script_path,
                            "reset_current_fenics_script": reset_current_fenics_script,
                            "append_current_fenics_script": append_current_fenics_script,
                            "get_current_fenics_script_status": get_current_fenics_script_status,
                            "run_current_fenics_script": run_current_fenics_script,
                            "get_run_statistics": get_run_statistics,
                            "record_physics_validation": record_physics_validation,
                        }
                        role_tools = {
                            "Coder": {
                                "get_golden_scripts", "local_search", "get_knowledge_structure", "online_search",
                                "get_current_fenics_script_path", "reset_current_fenics_script",
                                "append_current_fenics_script", "get_current_fenics_script_status",
                                "run_current_fenics_script", "get_run_statistics",
                            },
                            "Researcher": {
                                "get_golden_scripts", "local_search", "get_knowledge_structure", "online_search",
                                "get_current_fenics_script_status",
                                "get_run_statistics", "record_physics_validation",
                            },
                        }
                        role = next((name for name in role_tools if name in msg_name), None)
                        allowed_tools = role_tools.get(role, set())
                        rag_tools = {"get_golden_scripts", "local_search", "get_knowledge_structure", "online_search"}
                        script_tools = {"reset_current_fenics_script", "append_current_fenics_script", "get_current_fenics_script_status", "run_current_fenics_script"}

                        has_tool_marker = (
                            "```python" in content
                            or "<｜｜DSML" in content
                            or "tool_calls" in content
                            or "<function" in content
                            or "<function_name>" in content
                        )
                        if role == "Coder" and has_tool_marker and any(name in content for name in all_tool_funcs):
                            if not self._is_pure_tool_message(content):
                                logger.warning("[SmartUserProxy] 拒绝 Coder 混合自然语言和工具调用")
                                _record_rag_event("policy_block", "Coder", "Coder工具调用前后包含自然语言")
                                return "❌ Coder 输出格式违规：回复中只能包含一个工具调用块，不能在工具块前后添加说明文字。请按当前阶段只输出一个真实工具调用。"

                        dsml_calls = self._extract_dsml_tool_calls(content)
                        xml_calls = self._extract_xml_function_calls(content)
                        structured_calls = dsml_calls or xml_calls
                        if structured_calls:
                            sequence = [call["name"] for call in structured_calls if call.get("name") in all_tool_funcs]
                            called_tools = set(sequence)
                            if not called_tools:
                                return "ERROR: structured tool call found, but no authorized tool name was recognized."
                            forbidden = called_tools - allowed_tools
                            if forbidden:
                                logger.warning("[SmartUserProxy] blocked unauthorized structured tool call from %s: %s", msg_name, sorted(forbidden))
                                return f"ERROR: {msg_name} is not allowed to call {sorted(forbidden)}."
                            if role in {"Coder", "Researcher"} and (called_tools & rag_tools) and (called_tools & script_tools):
                                _record_rag_event("policy_block", role or "unknown", "blocked structured mixed RAG and script tools")
                                return f"ERROR: {role} cannot mix RAG and script/run tools in one structured tool message."
                            if role == "Coder" and (called_tools & script_tools) and not (called_tools & rag_tools):
                                phase_error = self._validate_coder_tool_phase(sequence, called_tools, rag_tools, script_tools)
                                if phase_error:
                                    _record_rag_event("guardrail_block", "Coder", phase_error)
                                    return phase_error

                            outputs = []
                            try:
                                for call in structured_calls:
                                    name = call.get("name")
                                    if name not in all_tool_funcs or name not in allowed_tools:
                                        continue
                                    kwargs = call.get("kwargs") or {}
                                    if "__raw_argument__" in kwargs:
                                        return f"ERROR: {name} arguments must be a JSON object."
                                    result = all_tool_funcs[name](**kwargs)
                                    outputs.append(f"--- {name} ---\n{result}")
                                output = "\n".join(outputs)
                                if role in {"Coder", "Researcher"} and (called_tools & rag_tools):
                                    _record_rag_event("tool_call", role or "unknown", ",".join(sorted(called_tools & rag_tools)))
                                if role == "Coder":
                                    self._update_coder_phase_after_success(called_tools, output, rag_tools)
                                logger.info("[SmartUserProxy] structured tool execution succeeded")
                                if "run_current_fenics_script" in called_tools:
                                    try:
                                        last = outputs[-1].split("\n", 1)[1] if outputs else ""
                                        run_payload = json.loads(last.strip())
                                        run_status = run_payload.get("status") or run_payload.get("data", {}).get("status")
                                        if run_status == "failed":
                                            return f"SIMULATION_FAILED\n\n{output}"
                                    except Exception:
                                        pass
                                    return f"SIMULATION_COMPLETED\n\n{output}"
                                return f"TOOL_EXECUTION_OK\n\nOutput:\n{output}"
                            except Exception as e:
                                logger.error("[SmartUserProxy] structured tool execution failed: %s", e)
                                return f"TOOL_EXECUTION_FAILED: {e}"

                        code_blocks = re.findall(r"```python\s*(.*?)\s*```", content, re.DOTALL)
                        for code in code_blocks:
                            sequence = self._extract_tool_sequence(code, set(all_tool_funcs))
                            called_tools = set(sequence)
                            if not called_tools:
                                continue

                            hallucination_markers = ["simulated", "Expected output", "expected output", "假设返回", "模拟", "工具不可用", "No relevant golden script found"]
                            if role in {"Coder", "Researcher"} and any(marker in code for marker in hallucination_markers):
                                logger.warning("[SmartUserProxy] 拒绝 %s 伪造/模拟 RAG 工具结果", role)
                                _record_rag_event("policy_block", role or "unknown", "拒绝伪造/模拟RAG工具结果")
                                return f"❌ 已拒绝执行：{role} 的工具块包含模拟/伪造 RAG 结果的文字。请只调用真实工具，例如 print(get_golden_scripts())，等待 User_Proxy 返回后再继续。"

                            rag_tools = {"get_golden_scripts", "local_search", "get_knowledge_structure", "online_search"}
                            script_tools = {"reset_current_fenics_script", "append_current_fenics_script", "get_current_fenics_script_status", "run_current_fenics_script"}
                            if role in {"Coder", "Researcher"} and (called_tools & rag_tools) and (called_tools & script_tools):
                                logger.warning("[SmartUserProxy] 拒绝 %s 在同一工具块混合 RAG 和脚本/状态工具", role)
                                _record_rag_event("policy_block", role or "unknown", "拒绝同一工具块混合RAG与脚本/状态工具")
                                return f"❌ 已拒绝执行：{role} 必须先单独调用 RAG 工具并等待真实返回，不能在同一个工具块里同时检索和写脚本/运行脚本。"

                            if role == "Coder" and (called_tools & script_tools) and not (called_tools & rag_tools):
                                phase_error = self._validate_coder_tool_phase(sequence, called_tools, rag_tools, script_tools)
                                if phase_error:
                                    logger.warning("[SmartUserProxy] blocked Coder out-of-order tool call: %s", phase_error)
                                    _record_rag_event("guardrail_block", "Coder", phase_error)
                                    return phase_error
                            forbidden = called_tools - allowed_tools
                            if forbidden:
                                logger.warning("[SmartUserProxy] 拒绝 %s 调用越权工具: %s", msg_name, sorted(forbidden))
                                return f"❌ 工具权限不足：{msg_name} 不允许调用 {sorted(forbidden)}。"

                            logger.info("[SmartUserProxy] 检测到%s工具调用: %s", msg_name, code[:80])
                            old_stdout = sys.stdout
                            redirected_output = sys.stdout = io.StringIO()
                            try:
                                safe_builtins = {"print": print, "str": str, "int": int, "float": float, "len": len, "range": range, "isinstance": isinstance, "dict": dict, "list": list, "tuple": tuple, "bool": bool, "min": min, "max": max, "abs": abs}
                                local_env = {name: all_tool_funcs[name] for name in allowed_tools}
                                local_env["print"] = print
                                compiled_code = self._compile_tool_code_with_printed_exprs(code, set(all_tool_funcs))
                                exec(compiled_code, {"__builtins__": safe_builtins}, local_env)
                                sys.stdout = old_stdout
                                output = redirected_output.getvalue()
                                if role in {"Coder", "Researcher"} and (called_tools & rag_tools):
                                    _record_rag_event("tool_call", role or "unknown", ",".join(sorted(called_tools & rag_tools)))
                                if role == "Coder":
                                    self._update_coder_phase_after_success(called_tools, output, rag_tools)
                                logger.info("[SmartUserProxy] tool execution succeeded")
                                if "run_current_fenics_script" in called_tools:
                                    try:
                                        run_payload = json.loads(output.strip())
                                        run_status = run_payload.get("status") or run_payload.get("data", {}).get("status")
                                        if run_status == "failed":
                                            return f"❌ 仿真执行失败\n\n{output}"
                                    except Exception:
                                        pass
                                    return f"✅ 仿真执行成功\n\n{output}"
                                return f"✅ 工具执行成功\n\n输出:\n{output}"
                            except Exception as e:
                                sys.stdout = old_stdout
                                logger.error("[SmartUserProxy] 工具执行失败: %s", e)
                                return f"❌ 工具执行失败: {e}"

                        manual_script_markers = [
                            "```python", "```bash", "from dolfin import", "#!/usr/bin/env python",
                            "@User_Proxy", "python3 ", "copy", "save", "manual run",
                            "\u8bf7\u5c06", "\u4fdd\u5b58\u4e3a", "\u590d\u5236\u5e76\u4fdd\u5b58",
                        ]
                        if any(marker in content for marker in manual_script_markers):
                            logger.warning("[SmartUserProxy] rejected %s chat-script/manual-run output", msg_name)
                            _record_rag_event("policy_block", role or "unknown", "blocked chat script/manual execution request")
                            return (
                                "ERROR: Coder/Researcher must not paste full scripts or ask for manual execution. "
                                "Coder must use fixed script tools in order: "
                                "reset_current_fenics_script(), append_current_fenics_script(...), "
                                "get_current_fenics_script_status(), run_current_fenics_script()."
                            )

                        if "script = '''" in content or "script = ''''" in content:
                            logger.warning("[SmartUserProxy] 检测到旧式脚本标记但已禁止执行")
                            return "❌ 已禁止旧式脚本标记。请使用固定脚本工具。"
            # 默认行为
            logger.info("[SmartUserProxy] 未检测到脚本,调用默认generate_reply")
            return super().generate_reply(messages, sender, **kwargs)
    
    user_proxy = SmartUserProxy(
        name='User_Proxy',
        human_input_mode='NEVER',
        max_consecutive_auto_reply=80,
        code_execution_config=False,  # 禁用代码执行,避免NameError
        system_message='用户代理,执行工具函数。',
        function_map={
            "get_golden_scripts": get_golden_scripts,
            "local_search": local_search,
            "get_knowledge_structure": get_knowledge_structure,
            "online_search": online_search,
            "get_current_fenics_script_path": get_current_fenics_script_path,
            "reset_current_fenics_script": reset_current_fenics_script,
            "append_current_fenics_script": append_current_fenics_script,
            "get_current_fenics_script_status": get_current_fenics_script_status,
            "run_current_fenics_script": run_current_fenics_script,
            "get_run_statistics": get_run_statistics,
            "record_physics_validation": record_physics_validation,
        }
    )
    
    # 创建各个 Agent
    researcher = AssistantAgent(
        name='Researcher_Agent',
        system_message=RESEARCHER_PROMPT,
        llm_config=llm_conf
    )
    
    coder = AssistantAgent(
        name='Coder_Agent',
        system_message=CODER_PROMPT,
        llm_config=llm_conf
    )
    
    
    manager_agent = AssistantAgent(
        name='Manager_Agent',
        system_message=MANAGER_PROMPT,
        llm_config=llm_conf
    )
    
    # 创建 GroupChat (移除了Executor)
    agents = [user_proxy, manager_agent, researcher, coder]
    
    initial_messages = []
    logger.info('[GroupChat] 初始化：无状态 RAG 模式，不加载历史对话')
    
    # 自定义speaker选择函数: 优先检测脚本,否则选择Manager
    def custom_speaker_selection(last_speaker, groupchat):
        """
        智能speaker选择:
        0. 检测TERMINATE → 立即结束
        1. Coder输出脚本或工具调用 → User_Proxy执行
        2. User_Proxy返回结果 → Manager分析
        3. Manager发出@指令 → 选择对应Agent
        4. 其他Agent完成 → 返回Manager
        """
        if groupchat.messages:
            last_msg = groupchat.messages[-1]
            content = last_msg.get('content', '')
            speaker_name = last_msg.get('name', '')

            def _latest_researcher_validation_passed() -> bool:
                for item in reversed(groupchat.messages):
                    item_name = item.get('name', '')
                    item_content = item.get('content', '') or ''
                    if "Researcher" not in item_name:
                        continue
                    if "验证通过" in item_content:
                        return _latest_simulation_succeeded()
                    if "验证失败" in item_content:
                        return False
                return False
            
            # 规则0: 只有 Manager 直接尝试终止时才检查 TERMINATE。
            # Manager 给 Researcher/Coder 的调度指令中可能提到 TERMINATE，不能因此抢先拦截。
            direct_terminate_attempt = (
                "Manager" in speaker_name
                and "TERMINATE" in content
                and "@Researcher_Agent" not in content
                and "@Coder_Agent" not in content
            )
            if direct_terminate_attempt:
                if _latest_researcher_validation_passed():
                    logger.info("[Speaker Selection] 检测到TERMINATE且Researcher已验证通过且最近仿真成功,结束对话")
                    return None
                logger.warning("[Speaker Selection] 拦截提前TERMINATE: Researcher尚未验证通过，转给Researcher做真实验证")
                return researcher            # 规则1: Coder/Researcher输出脚本或工具调用 → User_Proxy执行
            if ("Coder" in speaker_name or "Researcher" in speaker_name):
                # 1.1 检测脚本
                if "script = '''" in content:
                    logger.info("[Speaker Selection] 检测到%s脚本输出,强制选择User_Proxy", speaker_name)
                    return user_proxy

                
                # 1.2 检测工具调用 (Python代码块中包含特定关键词)
                tool_keywords = ["get_golden_scripts", "local_search", "online_search", "get_knowledge_structure",
                                 "get_current_fenics_script_path", "reset_current_fenics_script",
                                 "append_current_fenics_script", "get_current_fenics_script_status",
                                 "run_current_fenics_script", "get_run_statistics", "record_physics_validation"]
                if ("```python" in content or "<｜｜DSML" in content or "tool_calls" in content or "<function" in content or "<function_name>" in content) and any(kw in content for kw in tool_keywords):
                    logger.info("[Speaker Selection] 检测到%s工具调用,强制选择User_Proxy", speaker_name)
                    return user_proxy

                manual_script_markers = ["```python", "```bash", "from dolfin import", "#!/usr/bin/env python", "@User_Proxy", "python3 ", "copy", "save", "manual run", "\u8bf7\u5c06", "\u4fdd\u5b58\u4e3a", "\u590d\u5236\u5e76\u4fdd\u5b58"]
                if "Coder" in speaker_name and any(marker in content for marker in manual_script_markers):
                    logger.warning("[Speaker Selection] Coder posted script/manual-run instructions; route to User_Proxy rejection")
                    return user_proxy

                if "Coder" in speaker_name:
                    logger.warning("[Speaker Selection] Coder未输出可执行工具调用，返回Manager重新下达RAG/写入/运行指令")
                    return manager_agent
            
            # 规则2: User_Proxy返回结果
            if "User_Proxy" in speaker_name:
                # 区分是仿真结果还是工具结果
                if "仿真执行成功" in content or "仿真执行失败" in content or "SIMULATION_COMPLETED" in content or "SIMULATION_FAILED" in content:
                    logger.info("[Speaker Selection] 仿真结束, 选择Manager分析结果")
                    return manager_agent
                
                # 如果是工具执行结果 (包括成功或失败)
                if "\u5df2\u62d2\u7edd" in content or "ERROR:" in content or "TOOL_EXECUTION_FAILED" in content:
                    if len(groupchat.messages) >= 2:
                        prev_msg = groupchat.messages[-2]
                        prev_name = prev_msg.get('name', '')
                        if "Coder" in prev_name:
                            logger.info("[Speaker Selection] User_Proxy rejected Coder output; returning to Coder")
                            return coder
                        if "Researcher" in prev_name:
                            logger.info("[Speaker Selection] User_Proxy rejected Researcher output; returning to Researcher")
                            return researcher
                    logger.info("[Speaker Selection] User_Proxy rejection default; returning to Manager")
                    return manager_agent

                if "工具执行成功" in content or "工具执行失败" in content or "TOOL_EXECUTION_OK" in content or "TOOL_EXECUTION_FAILED" in content:
                    # 检查是谁调用的工具
                    if len(groupchat.messages) >= 2:
                        prev_msg = groupchat.messages[-2]
                        prev_name = prev_msg.get('name', '')
                        
                        if "Coder" in prev_name:
                            logger.info("[Speaker Selection] 工具返回, Coder继续工作")
                            return coder
                        elif "Researcher" in prev_name:
                            logger.info("[Speaker Selection] 工具返回, Researcher继续工作")
                            return researcher
                
                # 默认返回Manager
                logger.info("[Speaker Selection] User_Proxy默认返回Manager")
                return manager_agent
            
            # 规则3: Manager发出@指令 → 选择对应Agent
            if "Manager" in speaker_name:
                stripped_manager = (content or "").strip()
                normalized_manager = stripped_manager.lstrip("*").strip()
                is_manager_delegation = (
                    normalized_manager.startswith("@Coder_Agent")
                    or normalized_manager.startswith("@Researcher_Agent")
                    or normalized_manager.startswith("@Fixer_Agent")
                )
                is_manager_final = "TERMINATE" in stripped_manager and _latest_researcher_validation_passed()
                if not is_manager_delegation and not is_manager_final:
                    logger.warning("[Speaker Selection] Manager输出不符合调度协议，转User_Proxy拒绝")
                    return user_proxy
                if "验证通过" in content and "@Researcher_Agent" not in content and not _latest_researcher_validation_passed():
                    logger.warning("[Speaker Selection] Manager越权声明验证通过，转给User_Proxy拒绝")
                    return user_proxy
                premature_done_markers = ["任务已完成", "任务正式结束", "无需进一步操作", "对话结束", "流程合规", "正式结束"]
                if any(marker in content for marker in premature_done_markers) and not _latest_researcher_validation_passed():
                    logger.warning("[Speaker Selection] Manager提前宣布完成，转给User_Proxy拒绝")
                    return user_proxy
                manager_forbidden_markers = [
                    "```python", "```bash", "<function", "<function_name>",
                    "script =", "from dolfin import", "python3 ", "copy", "save", "manual run",
                    "--- FENICS JOB RESULT ---", "Linear solver", "Stage 0", "Stage 1", "Stage 2", "u_z", "common knowledge", "expected result", "simulation completed", "results saved",
                ]
                if any(marker in content for marker in manager_forbidden_markers):
                    logger.warning("[Speaker Selection] Manager越权输出代码/工具/伪结果，转给User_Proxy拒绝")
                    return user_proxy
                if "@Researcher_Agent" in content:
                    logger.info("[Speaker Selection] Manager调用Researcher")
                    return researcher
                elif "@Coder_Agent" in content:
                    logger.info("[Speaker Selection] Manager调用Coder")
                    return coder
                elif "@Fixer_Agent" in content:
                    logger.info("[Speaker Selection] 旧修复指令已转给Coder完整重写")
                    return coder
                # Manager没有@指令,检查是否在总结
                else:
                    # 如果Manager的消息很短且没有技术细节,可能是简短确认,继续给Manager
                    if len(content) < 100:
                        logger.info("[Speaker Selection] Manager短消息,继续Manager")
                        return manager_agent
                    else:
                        if "TERMINATE" in content and _latest_researcher_validation_passed():
                            logger.info("[Speaker Selection] Manager在验证通过后总结,允许结束")
                            return None
                        logger.info("[Speaker Selection] Manager未验证通过不得自然结束,继续Manager")
                        return manager_agent
            
            # 规则4: 其他Agent完成 → 返回Manager
            if "Researcher" in speaker_name:
                if "验证通过" in content:
                    if not _latest_simulation_succeeded():
                        logger.warning("[Speaker Selection] Researcher在最近仿真未成功时声明验证通过，转User_Proxy拒绝")
                        return user_proxy
                    record_physics_validation(True, content)
                elif "验证失败" in content:
                    record_physics_validation(False, content)
                # 如果Researcher在调用工具，则不返回Manager
                tool_keywords = ["get_golden_scripts", "local_search", "online_search", "get_knowledge_structure",
                                 "get_current_fenics_script_path", "reset_current_fenics_script",
                                 "append_current_fenics_script", "get_current_fenics_script_status",
                                 "run_current_fenics_script", "get_run_statistics", "record_physics_validation"]
                if ("```python" in content or "<｜｜DSML" in content or "tool_calls" in content or "<function" in content or "<function_name>" in content) and any(kw in content for kw in tool_keywords):
                    logger.info("[Speaker Selection] Researcher调用工具, 强制选择User_Proxy")
                    return user_proxy
                
                logger.info("[Speaker Selection] %s完成,返回Manager", speaker_name)
                return manager_agent
        
        # 默认: 第一轮选择Manager
        logger.info("[Speaker Selection] 默认选择Manager")
        return manager_agent
    
    groupchat = GroupChat(
        agents=agents,
        messages=initial_messages,
        max_round=300,
        speaker_selection_method=custom_speaker_selection,
        allow_repeat_speaker=True
    )
    
    # Manager LLM 配置（移除工具，只负责说话）
    manager_llm_conf = llm_conf.copy()
    manager_llm_conf.pop('tools', None)
    manager_llm_conf.pop('functions', None)
    
    # GroupChatManager
    manager = GroupChatManager(
        groupchat=groupchat,
        llm_config=manager_llm_conf,
        name='Manager',
        system_message=MANAGER_PROMPT
    )
    
    return manager, user_proxy, agents

# =============== 运行接口 =================

def run_demo(request: str):
    """
    运行单次演示（无状态 RAG 模式）。
    """
    logger.info("=" * 70)
    logger.info(f"启动多Agent协作请求: {request}")
    logger.info("=" * 70)

    manager, user_proxy, agents = build_multiagent_system()

    try:
        user_proxy.initiate_chat(manager, message=request)
    except Exception as e:
        logger.error(f"[Demo] 对话出错: {e}")
    messages = getattr(getattr(manager, "groupchat", None), "messages", []) or []
    latest_validation_passed = False
    for msg in reversed(messages):
        name = msg.get("name", "")
        content = msg.get("content", "") or ""
        if "Researcher" not in name:
            continue
        if "验证通过" in content and _latest_simulation_succeeded():
            latest_validation_passed = True
            break
        if "验证失败" in content:
            break
    if not latest_validation_passed:
        logger.warning("[Demo] 本轮未达到物理验证通过就结束。最后消息如下：")
        for msg in messages[-6:]:
            name = msg.get("name", msg.get("role", "unknown"))
            content = (msg.get("content", "") or "").replace("\n", " ")[:300]
            logger.warning("[Demo] last %s: %s", name, content)

    logger.info("\n" + "=" * 70)
    logger.info("对话完成")
    logger.info("=" * 70)


def run_interactive():
    """
    交互模式 - 无状态 RAG 模式。
    每轮不加载历史、不保存历史，只使用当前输入和本地 RAG。
    """
    print("=" * 70)
    print("多Agent交互模式（无状态 RAG 版）")
    print("=" * 70)
    print("支持：")
    print("  - 本地知识库 RAG 检索")
    print("  - 固定脚本文件生成/完整重写修复")
    print("  - 不加载、不保存历史对话")
    print("\n输入 'exit' 或 'quit' 退出\n")

    while True:
        try:
            user_input = input("\n请求> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n检测到中断信号...")
            break

        if not user_input:
            continue

        if user_input.lower() in ['exit', 'quit', 'q']:
            print("退出交互模式")
            break

        logger.info(f"[交互模式] 处理请求: {user_input[:100]}...")
        manager, user_proxy, agents = build_multiagent_system()

        try:
            user_proxy.initiate_chat(manager, message=user_input)
        except Exception as e:
            logger.error(f"[交互模式] 对话出错: {e}")
            print(f"\n错误: {e}")

        print("\n--- 一轮完成 ---")


def main():
    parser = argparse.ArgumentParser(description='FEniCS 多Agent协作系统（无状态 RAG 版）')
    parser.add_argument('--demo', type=str, help='演示请求')
    parser.add_argument('--interactive', action='store_true', help='交互模式（推荐）')
    parser.add_argument('--show-stats', action='store_true', help='显示固定脚本运行统计')
    args = parser.parse_args()

    if args.show_stats:
        print(get_run_statistics())
    elif args.demo:
        run_demo(args.demo)
    elif args.interactive:
        run_interactive()
    else:
        print("未指定模式，启动交互模式...")
        print("提示：使用 --help 查看所有选项\n")
        run_interactive()


if __name__ == '__main__':
    main()






























