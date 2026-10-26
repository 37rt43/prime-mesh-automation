# prime-mesh-automation

基于 [PyPrimeMesh](https://github.com/ansys/pyprimemesh) 的自动化网格划分脚本仓库，配合 ChatGPT Codex 云端环境使用。

## 版本对应（务必先看）

本机为 **Ansys 2024R1（Prime Server 24.1.0）**，客户端必须用 **0.5.x**，且只支持 **Python 3.8–3.11**。

## 1. 准备 Python 3.11

本机若只有 Python 3.12/3.14，先装 3.11（PowerShell）：

```powershell
py install 3.11
```

## 2. 用 3.11 创建虚拟环境并安装依赖

```powershell
cd 你的仓库路径\prime-mesh-automation
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version          # 应显示 3.11.x
pip install -r requirements.txt
```

## 3. 验证连接

```powershell
python main.py
```

应打印 0.5.x 客户端版本和 Prime Server 进程 PID。

## 4. 导入 CAD 划分网格

```powershell
python main.py --cad part.step --min-size 0.5 --max-size 5 --out part.msh
```

脚本用 Lucid 高层接口：读入 CAD → 面网格 → 多面体体网格 → 导出 `.msh`（可直接给 Fluent）。

## 5. 接入 Codex

1. 在 GitHub 新建空仓库 `prime-mesh-automation`（不勾自动 README），上传本目录文件并提交。
2. ChatGPT → Codex → 新建云端环境 → 选择 `prime-mesh-automation` 仓库。
3. Codex 会读取 `requirements.txt` 与 `AGENTS.md`（已锁定 0.5.x / 24.1 / Python 3.11）。

> 云端 Codex 没有 Prime Server 和许可证，只负责写代码；实际划网格在本机运行。
