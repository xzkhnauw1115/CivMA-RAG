# CivMA-RAG
面向土木工程有限元仿真的物理信息增强多智能体 RAG 框架。/A physics-informed multi-agent RAG framework for civil engineering finite element simulation.

## Directory Structure

```text
.
├── all_in_one.py
├── chat_bot_multiagent.py
├── config/
│   ├── config.example.json
│   └── config.json                 # local file; ignored by Git
├── data/local_knowledge/           # default active generic RAG scope
│   ├── KNOWLEDGE_INDEX.md
│   ├── fenics2019_error_fix_matrix.md
│   ├── fenics2019_reliable_patterns.md
│   ├── physics_validation_rules.md
│   └── golden_scripts/
│       └── recipe_fenics2019_3d_elastic_dg0_initial_stress.py
├── examples/reference_bridge/      # optional paper/reference reproduction materials; excluded from default RAG
│   ├── README.md
│   ├── guardrails/
│   └── scripts/
├── src/
│   ├── __init__.py
│   ├── agent_prompts.py
│   ├── agent_workflow.py
│   ├── config_loader.py
│   ├── fenics_mcp_server.py
│   ├── rag_retriever.py
│   ├── result_parser.py
│   ├── script_workspace.py
│   ├── statistics_store.py
│   └── workflow_gate.py
├── tests/
├── temp_scripts/fenics_drafts/.gitkeep
├── environment.yml
├── .env.example
└── .gitignore
```

## Two-Layer Runtime

There are two environments.

### 1. Windows/Python Manager Environment

This environment runs:

- AutoGen agents;
- local deterministic keyword RAG;
- Gradio UI;
- Flask bridge server;
- unit tests.

Install it with:

```bash
conda env create -f environment.yml
conda activate fem-agent-rag
```

### 2. WSL/FEniCS Runtime

FEniCS/dolfin is not installed in the Windows manager environment. Generated simulation scripts target:

```text
FEniCS 2019.1.0 / old dolfin
```

Default execution path:

```text
Windows Python manager
  -> local Flask bridge
  -> WSL Ubuntu
  -> conda environment named fenics
  -> generated Python script using `from dolfin import *`
```

Default runtime config:

```json
"fenics_runtime": {
  "backend": "wsl",
  "wsl_distro": "Ubuntu",
  "conda_env": "fenics",
  "python_command": "python",
  "fallback_python_command": "python3",
  "job_timeout_sec": 1800,
  "debug_include_absolute_paths": false,
  "allow_remote_bind": false,
  "access_token": ""
}
```

Inside WSL Ubuntu, one reproducible installation path is:

```bash
conda create -n fenics -c conda-forge python=3.8 fenics=2019.1.0 -y
conda activate fenics
python - <<'PY'
from dolfin import *
print('dolfin ok')
PY
```

If your WSL environment already has `dolfin` in `python3`, the bridge can fall back to `python3`.

## Configuration

Create local config from the example.

PowerShell:

```powershell
Copy-Item config/config.example.json config/config.json
```

Bash:

```bash
cp config/config.example.json config/config.json
```

Set the LLM key in the process environment. The code does not auto-load `.env` files.

PowerShell:

```powershell
$env:DEEPSEEK_API_KEY="your_api_key_here"
```

Bash:

```bash
export DEEPSEEK_API_KEY="your_api_key_here"
```

If the key is missing, the workflow fails before an LLM request is made.

## CLI Usage

Show help:

```bash
python all_in_one.py --help
```

Default UI mode:

```bash
python all_in_one.py
```

Server-only mode:

```bash
python all_in_one.py --server-only
```

Compatibility server-only mode without Gradio:

```bash
python all_in_one.py --no-gradio
```

Single prompt:

```bash
python all_in_one.py --demo "Generate and run a simple FEniCS beam script."
```

Prompt from file:

```bash
python all_in_one.py --demo-file request.txt
```

Interactive CLI:

```bash
python all_in_one.py --interactive
```

Use an already running bridge server:

```bash
python all_in_one.py --no-server
```

Skip WSL/FEniCS preflight only when you know the backend is valid:

```bash
python all_in_one.py --skip-fenics-preflight
```

Choose ports:

```bash
python all_in_one.py --fenics-port 5001 --port 7861
```

Print statistics:

```bash
python all_in_one.py --show-stats
```

Unsupported local-only mode:

```bash
python all_in_one.py --disable-remote
```

This exits early because this release has no local LLM fallback.

## Strict Workflow State

The Coder tool workflow is gated as:

```text
RAG -> reset -> append -> status -> run -> validation
```

The concrete phases are:

```text
need_rag -> need_reset -> need_append -> need_status -> need_run
```

After every append, an explicit status check is required. A failed status check returns to RAG and requires a full rewrite. The default maximum full rewrite count is `5` and is configured by `workflow.max_rewrites`.

## RAG Scope

Default RAG mode is deterministic local keyword retrieval from:

```text
data/local_knowledge/
```

The default active knowledge is generic only. It excludes:

- `examples/`
- `state/`
- `fenics_jobs/`
- archive/generated-output directories

`examples/reference_bridge/` contains optional reference reproduction material. It includes case parameters and scripts, so it is not default active RAG knowledge and must not be described as generic rules.

Semantic RAG is not enabled in this release. ChromaDB and sentence-transformers are intentionally not included in `environment.yml`.

## Output Directories

Runtime outputs are generated under ignored paths:

```text
state/
fenics_jobs/
temp_scripts/fenics_drafts/current_fenics_script.py
```

Do not commit runtime jobs, generated scripts, logs, or vector databases.

## Safety Notes

- The bridge executes generated Python code. Keep it on `127.0.0.1`.
- Binding the bridge to non-loopback addresses is refused unless explicitly enabled with a token.
- Do not commit API keys.
- Do not publish copyrighted standards or private input data.
- Reference examples are for reproducibility only and do not establish field or experimental validation.

## Tests

Run offline tests that do not require WSL, FEniCS, or a remote LLM:

```bash
pytest -q
```

Compile check:

```bash
python -m compileall .
```

Inspect CLI:

```bash
python all_in_one.py --help
```

Suggested secret scan before release:

```bash
rg 'sk-[A-Za-z0-9]+' .
rg 'api_key|secret|password|token|Bearer|Authorization' .
```

## Common Problems

### FEniCS preflight fails

Check WSL and dolfin:

```bash
wsl -d Ubuntu -e bash -lc "source ~/miniconda3/etc/profile.d/conda.sh && conda activate fenics && python - <<'PY'
from dolfin import *
print('dolfin ok')
PY"
```

If your distribution or conda environment has a different name, update `config/config.json`.

### Missing API key

Set `DEEPSEEK_API_KEY` in the process environment before starting the workflow.

### The bridge refuses non-localhost binding

This is intentional. The service executes generated code and must not be exposed to untrusted networks.

### RAG cannot find a reference example

Default RAG intentionally excludes `examples/reference_bridge/`. Use those files manually only for explicit reference reproduction tasks.

## Academic Citation

Paper citation information will be added after publication. Do not invent a DOI, author list, title, or result claim before the paper metadata is final.

