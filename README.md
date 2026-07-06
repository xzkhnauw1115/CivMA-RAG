# CivMA-RAG
面向土木工程有限元仿真的物理信息增强多智能体 RAG 框架。/A physics-informed multi-agent RAG framework for civil engineering finite element simulation.
The system combines:

- a Manager agent for workflow control;
- a Coder agent for script generation and full-script repair;
- a Researcher agent for physical validation;
- a UserProxy/tool gate for controlled tool execution;
- a local RAG knowledge base for FEniCS patterns, error handling, and physics checks;
- a Flask-based FEniCS execution bridge that submits generated scripts to a WSL Ubuntu FEniCS runtime.

This repository is prepared for academic code release. Runtime caches, job outputs, vector databases, generated scripts, API keys, and copyrighted standards are intentionally excluded.

## 1. Repository Layout

```text
.
├── all_in_one.py                         # Main entry: FEniCS bridge + multi-agent workflow + optional Gradio UI
├── chat_bot_multiagent.py                # Multi-agent system, RAG tools, script gate, statistics hooks
├── config/
│   ├── config.json                       # Local runtime config, uses environment-variable placeholder for API key
│   └── config.example.json               # Public example config
├── data/
│   └── local_knowledge/                  # Active local RAG knowledge base
│       ├── KNOWLEDGE_INDEX.md
│       ├── physics_validation_rules.md
│       ├── fenics2019_reliable_patterns.md
│       ├── fenics2019_error_fix_matrix.md
│       ├── failure_case_review.md
│       ├── bridge_*_guardrails.md
│       └── golden_scripts/               # Reusable FEniCS/Python recipe snippets
├── src/
│   ├── __init__.py
│   └── fenics_mcp_server.py              # Flask execution server, submits scripts to WSL/FEniCS
├── temp_scripts/
│   └── fenics_drafts/.gitkeep            # Runtime script folder placeholder
├── environment.yml                       # Conda environment for the Python manager side
├── .env.example                          # Environment variable example
└── .gitignore
```

## 2. What Is Not Included

The following are generated locally and should not be committed:

- `state/`: runtime statistics, vector DB, embedding caches;
- `fenics_jobs/`: simulation job outputs;
- `temp_scripts/fenics_drafts/current_fenics_script.py`: current generated script;
- `__pycache__/`, `.cache/`: Python and model caches;
- raw standards/PDF documents with redistribution restrictions;
- API keys or other credentials.

## 3. Requirements

There are two runtime layers.

### 3.1 Python Manager Environment

The Conda environment in `environment.yml` runs:

- AutoGen-based agents;
- local RAG retrieval;
- Gradio UI;
- Flask bridge server;
- script validation/gating logic.

Create the environment:

```bash
conda env create -f environment.yml
conda activate fem-agent-rag
```

### 3.2 FEniCS Runtime

The Conda environment above is only for the Python manager, RAG, agents, Gradio, and Flask bridge. It does **not** install `dolfin`.

Generated simulation scripts target:

```text
FEniCS 2019.1.0 / old dolfin
```

On Windows, the default integration path is:

```text
Windows Python manager
  -> Flask bridge server
  -> WSL Ubuntu
  -> conda environment named fenics
  -> python script with `from dolfin import *`
```

The default runtime settings are in `config/config.json`:

```json
"fenics_runtime": {
  "backend": "wsl",
  "wsl_distro": "Ubuntu",
  "conda_env": "fenics",
  "python_command": "python",
  "fallback_python_command": "python3",
  "job_timeout_sec": 1800
}
```

The bridge server automatically searches these WSL conda startup files:

```text
$HOME/miniconda3/etc/profile.d/conda.sh
$HOME/anaconda3/etc/profile.d/conda.sh
/opt/conda/etc/profile.d/conda.sh
```

If one is found, it runs:

```bash
conda activate fenics
python generated_script.py
```

If no conda startup file is found, it falls back to:

```bash
python3 generated_script.py
```

#### Option A: Install FEniCS in a WSL conda environment

Inside WSL Ubuntu:

```bash
# Install Miniconda first if conda is not available.
# Then create the FEniCS runtime environment:
conda create -n fenics -c conda-forge python=3.8 fenics=2019.1.0 -y
conda activate fenics
python - <<'PY'
from dolfin import *
print('dolfin ok')
PY
```

If your environment name is not `fenics`, change `fenics_runtime.conda_env` in `config/config.json`.

#### Option B: Use system Python inside WSL

If `python3` in WSL already has `dolfin`:

```bash
python3 - <<'PY'
from dolfin import *
print('dolfin ok')
PY
```

Then you can leave conda unused; the bridge will fall back to `python3` when conda is not found.

#### Preflight check

Before spending LLM/API calls, `all_in_one.py` runs a real FEniCS preflight:

```bash
from dolfin import *
print('FENICS_OK')
```

If this fails, fix the WSL/FEniCS runtime first, or run with an existing compatible backend:

```bash
python all_in_one.py --no-server
```

## 4. API Key Configuration

The public config never stores a real API key. It uses an environment variable placeholder:

```json
"api_key": "${DEEPSEEK_API_KEY}"
```

Set the key before running.

PowerShell:

```powershell
$env:DEEPSEEK_API_KEY="your_api_key_here"
```

Bash:

```bash
export DEEPSEEK_API_KEY="your_api_key_here"
```

Do not commit `.env`, local config files, or real keys.

## 5. Configuration

The active config is intentionally minimal:

```json
{
  "fenics_server": {
    "host": "127.0.0.1",
    "port": 5000,
    "url": "http://127.0.0.1:5000"
  },
  "fenics_runtime": {
    "backend": "wsl",
    "wsl_distro": "Ubuntu",
    "conda_env": "fenics",
    "python_command": "python",
    "fallback_python_command": "python3",
    "job_timeout_sec": 1800
  },
  "autogen": {
    "llm_config": {
      "config_list": [
        {
          "model": "deepseek-chat",
          "base_url": "https://api.deepseek.com/v1",
          "api_key": "${DEEPSEEK_API_KEY}",
          "max_new_tokens": 12000
        }
      ],
      "temperature": 0.6,
      "timeout": 300
    }
  }
}
```

Only these keys are used by the current startup path:

- `fenics_server.host`
- `fenics_server.port`
- `fenics_server.url`
- `fenics_runtime.backend`
- `fenics_runtime.wsl_distro`
- `fenics_runtime.conda_env`
- `fenics_runtime.python_command`
- `fenics_runtime.fallback_python_command`
- `fenics_runtime.job_timeout_sec`
- `autogen.llm_config.config_list`
- `autogen.llm_config.temperature`
- `autogen.llm_config.timeout`

Old training, memory, fine-tuning, and curriculum fields were removed because they are not required to start or reproduce the released workflow.

## 6. Running the System

### 6.1 Launch UI and Backend

```bash
python all_in_one.py
```

This starts:

- the FEniCS execution server;
- the multi-agent workflow;
- a local Gradio interface.

Default URLs:

```text
FEniCS bridge: http://127.0.0.1:5000
Gradio UI:     http://127.0.0.1:7860
```

If a port is occupied, the program attempts to use the next available port.

### 6.2 Run a Single Request from CLI

```bash
python all_in_one.py --demo "Generate a FEniCS 2019 script for a simple beam and run it."
```

For long prompts:

```bash
python all_in_one.py --demo-file request.txt
```

### 6.3 Interactive CLI Mode

```bash
python all_in_one.py --interactive
```

### 6.4 Use an Existing Backend

If the FEniCS server is already running:

```bash
python all_in_one.py --no-server
```

If preflight should be skipped:

```bash
python all_in_one.py --skip-fenics-preflight
```

## 7. Workflow Summary

The intended workflow is:

```text
User request
  -> Manager assigns task
  -> Coder performs local RAG retrieval
  -> Coder writes a complete script to temp_scripts/fenics_drafts/current_fenics_script.py
  -> Tool gate checks script completeness and execution order
  -> FEniCS bridge runs the script
  -> Researcher validates physical plausibility using RAG rules
  -> If failed, Coder performs RAG again and fully rewrites the script
  -> If passed, Manager summarizes outputs and statistics
```

The system intentionally avoids line-by-line partial fixing for generated simulation scripts. Failed scripts are repaired by full rewrite after another RAG lookup, which reduces accumulated corruption and tool-call disorder.

## 8. Local RAG Knowledge Base

The active knowledge base is under:

```text
data/local_knowledge/
```

It contains:

- stable FEniCS 2019 API patterns;
- known error-to-rewrite rules;
- physical validation rules;
- task-specific modeling guardrails;
- golden recipe snippets.

The knowledge base is sanitized for open release. It does not include raw copyrighted standards, project-private bridge names, or runtime job histories.

## 9. Output Locations

During runtime, generated files are created locally:

```text
temp_scripts/fenics_drafts/current_fenics_script.py
fenics_jobs/
state/run_statistics.json
```

These paths are ignored by Git. They are useful for debugging but should not be treated as source files.

## 10. Safety and Reproducibility Notes

Before publishing or submitting a release, check:

```bash
rg 'sk-[A-Za-z0-9]+' .
rg 'api_key|secret|password|token|Bearer|Authorization' .
```

The expected public config should contain only:

```text
${DEEPSEEK_API_KEY}
```

If a real key was ever committed to Git history, revoke it from the provider dashboard and publish from a clean repository or scrub history before release.

## 11. Common Problems

### Gradio port is occupied

Use another port:

```bash
python all_in_one.py --port 7861
```

### FEniCS preflight fails

Check WSL and dolfin:

```bash
wsl -d Ubuntu -e bash -lc "python3 - <<'PY'
from dolfin import *
print('dolfin ok')
PY"
```

### The generated script is not executed

Check the tool gate state in the console. The expected order is:

```text
RAG -> reset_current_fenics_script -> append_current_fenics_script -> status -> run_current_fenics_script
```

### RAG returns irrelevant knowledge

Check `data/local_knowledge/KNOWLEDGE_INDEX.md` and the direct-priority routing in `chat_bot_multiagent.py`.

## 12. Citation / Academic Use

If this repository accompanies a paper, cite the paper and describe the system as a multi-agent RAG workflow for automated finite-element script generation and physical validation. The released code contains the framework, open local knowledge cards, and reproducible templates, but excludes private runtime data and restricted documents.


