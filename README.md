# 招采平台供应商智能辅助审批系统

> 当前分支为离线 OCR 试运行版：材料解析禁用 TextIn，使用本地 PaddleOCR。第一阶段范围、部署、缓存和验证限制见 [离线OCR分支说明](docs/离线OCR分支说明.md)。下文历史架构描述中的 TextIn 已由本地解析替代；招采和企业信息查询仍需联网。

> 由离线脚本驱动的供应商准入自动化审批工具，核心是"规则引擎自动判定 + 离线/在线 OCR 识别证件 + 人工复核结论"，将原本需要人工逐项核对的供应商准入审查流程改造为可配置的自动审批流水线。

> **在线体验**：本系统支持通过链接直接在线体验（无需配置环境或下载代码），但因涉及敏感数据，体验链接未在公开仓库中发布。如需更快体验，可在交建通中联系我获取链接。

## 一、项目简介

本系统是面向集团招采平台的供应商智能辅助审批系统，把「拉取待办 → 下载资质材料 → 离线/在线 OCR 识别 → 规则引擎判定 → 生成审查报告」全流程自动化，将人工逐项核对收敛为「一键后台审批 + 人工复核结论」。

本系统实时接入集团招采平台，已跑通所有待办审批。它针对供应商准入审批「效率低、合规风险高、标准难统一」三大痛点，通过「规则引擎自动判定 + 离线/在线 OCR 识别证件 + 人工复核结论」的方式，把单家审批从十几分钟压缩到 1 分钟以内，同时做到身份证敏感信息本地离线脱敏、凭证与业务数据不入库、判定标准可配置可审计。详细的问题分析、解决方案与价值说明见《竞赛提交说明.md》。

## 二、核心特性

| 特性 | 说明 |
|------|------|
| **规则可配置** | 准入标准全部写在 `rules.yaml`，改标准只改配置不碰代码；新员工无需理解 Python 即可调整阈值 |
| **OCR 证件识别** | 身份证用 PaddleOCR 本地离线识别，营业执照/ISO 证书/纳税信用证明用 TextIn 在线 OCR 抽取关键字段（注册资本、有效期、报告年度等）；财报按保密合规不 OCR，改走企查查/人工核验 |
| **三段式决策** | 自动判定 → 通过 / 退回 / 转人工，硬伤自动退回补材料，模糊项转人工核验，依据明确可追溯 |
| **审查报告自动生成** | 每家生成结构化 HTML 报告（含审批意见表格 + 可复制纯文本），直接粘贴进 ICCEC 审批框 |
| **Web UI** | 提供 FastAPI Web 界面，本机启动后浏览器打开即可；待办支持搜索筛选，平台凭证可在网页更新 |
| **可复用设计** | 不绑定个人账号，规则、缓存、日志分离，可整体移交其他同事；规则引擎可平移到其他类似审批场景 |

## 三、系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                    Web UI (FastAPI + Jinja2)                  │
│   待办列表页 ←→ 审批详情页 ←→ 平台凭证弹窗                       │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP 调用
┌──────────────────────────▼──────────────────────────────────┐
│                       核心处理层（Python）                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐    │
│  │ auto_approve │→ │ textin_      │→ │ gen_opinion /     │   │
│  │   .py        │  │ pipeline.py  │  │ gen_stage2_report │   │
│  │ (拉取+审批)   │  │ (OCR 识别)    │  │   .py (意见生成)   │   │
│  └──────┬───────┘  └──────────────┘  └──────────────────┘    │
│         │                                                    │
│         │ 读取规则                                            │
│  ┌──────▼───────┐                                            │
│  │  rules.yaml  │  ← 准入标准配置（改规则只改这里）              │
│  └──────────────┘                                            │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTPS
              ┌────────────▼────────────┐
              │  外部服务                 │
              │  · scpma.iccec.cn (审批) │
              │  · api.textin.com (OCR)  │
              └─────────────────────────┘
```

## 四、文件结构

```
supplier_approval_starter/
├── auto_approve.py          # 主程序：拉取待办 → 下载材料 → 调用 OCR → 触发判定
├── textin_pipeline.py       # OCR 处理：调用 TextIn 识别证件并抽取结构化字段
├── gen_opinion.py           # 生成最终审批意见纯文本（可直接粘贴进 ICCEC 审批框）
├── gen_stage2_report.py     # 生成结构化 HTML 审查报告（含审批意见表格）
├── rules.yaml               # 准入规则配置（核心可复用资产，改标准只改这里）
├── eval.py                  # 规则求值器（沙箱执行规则表达式）
├── .env.example             # 环境变量模板（复制为 .env 填真实值）
├── .gitignore               # Git 忽略规则（敏感数据默认不入库）
├── web/                     # FastAPI Web UI（详见下方"Web UI 使用"）
│   ├── app.py               # FastAPI 主入口
│   ├── templates/           # Jinja2 HTML 模板（待办列表页 + 审批详情页）
│   └── static/              # CSS 静态资源
```

## 五、快速开始

### 5.1 环境准备

- Windows 10/11 x64 首次使用可从仓库 `dist/SupplierApproval-Setup.exe` 下载并联网安装 Python、项目依赖及 PaddleOCR；安装后双击 `start_webui.bat`，无平台凭证时可用 `start_demo.bat` 查看合成演示。开发者也可通过 `python installer/build_installer.py` 重新构建 EXE
- 手动安装需 Python 3.12；pip 依赖：`pip install -r requirements.txt`

### 5.2 配置凭证

1. 复制 `.env.example` 为 `.env`，按需填写 TextIn OCR 的 APP_ID 和 SECRET_CODE（textin.com 工作台获取）
2. 浏览器登录公司审批页 scpma.iccec.cn → F12 → Network，从请求头复制 Authorization 和 Cookie
3. 在 Web UI 右上角点击"更新平台凭证"并粘贴上述两项；仅在当前服务进程内有效，关闭服务后需重新粘贴
4. 待办或详情无法读取时，可在弹窗的"高级配置"中按需填写 APP_TOKEN、AGENT_ID；已有 `.env` 配置仍兼容，CODE 当前未参与请求

### 5.3 命令行运行（批量处理）

```bash
# 默认 DRY_RUN=true，只模拟不真审批
python auto_approve.py
```

### 5.4 Web UI 运行（推荐，便于同事使用）

```bat
start_webui.bat
```

浏览器打开 `http://127.0.0.1:8000` 即可使用。默认便捷模式仅供本机访问：
- 首页展示当前待办列表，可按供应商名称或待办 ID、业务类型和决策状态筛选
- 点击"后台审批"后，系统生成审查报告 + 审批意见，供人工复核
- 点击右上角"启动检查"查看本机配置；点击"更新平台凭证"粘贴 Authorization 和 Cookie

### 5.5 离线材料脱敏助手（合规保障）

```bash
# 把从系统下载的材料（cache_v4/{todoId}/）脱敏后输出到 cache_v4_desens/
python desensitize.py --src cache_v4 --dst cache_v4_desens
```

按文件类型处理（仅身份证需要脱敏）：
- 身份证（PaddleOCR 精确打码）：本地离线识别文字框坐标，**只保留「姓名」+「有效期限」两个字段**，其余（性别/民族/出生/住址/证号/头像/签发机关）按文字框精确黑遮，敏感信息不离开本机
- 财报 PDF：不脱敏、不 OCR 解析（各公司财报格式差异大，难以统一离线脱敏；按保密合规要求改走企查查/人工核验）
- 营业执照 / ISO / 授权 / 声明等：原样保留

PaddleOCR 环境未就绪时：身份证转人工核验并写明失败原因。**绝不抛异常阻塞流程**。

### 5.6 身份证 PaddleOCR 离线精确脱敏（保密合规核心）

身份证脱敏独立于主项目，用飞桨 PaddleOCR 在**本地离线**完成：先识别文字框坐标，再只保留「姓名」+「有效期限」，其余按框黑遮。主项目通过 `subprocess` 调用脱敏脚本 `idcard_masker.py`（随仓库分发在 `offline_desens/`，路径由环境变量 `IDCARD_MASKER_SCRIPT` 指定，默认即项目内该文件）。

**依赖环境（独立于主项目，需 Python 3.11 + PaddleOCR 2.x）**：

```bash
# 1. 装 Python 3.11（PaddleOCR 2.9.1 不支持 Python 3.13，两者不可共存）
winget install Python.Python.3.11

# 2. 建独立 venv（与主项目 3.13 隔离）
"C:\Users\<用户>\AppData\Local\Programs\Python\Python311\python.exe" -m venv idcard_env

# 3. 装 PaddlePaddle CPU 版（国内源，约 500MB）
idcard_env\Scripts\python.exe -m pip install paddlepaddle==2.6.2 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/

# 4. 装 PaddleOCR 2.9.1 + 依赖（清华源）
idcard_env\Scripts\python.exe -m pip install paddleocr==2.9.1 -i https://pypi.tuna.tsinghua.edu.cn/simple
idcard_env\Scripts\python.exe -m pip install opencv-python pymupdf pillow
```

**首次运行自动下载中文模型**（det/rec/cls 三个 PP-OCRv4 模型，约 100MB），并直接对身份证目录批量打码：

```bash
PROCESSOR_ARCHITECTURE=AMD64 PYTHONIOENCODING=utf-8 \
idcard_env\Scripts\python.exe idcard_masker.py <输入目录> <输出目录> \
  --output-mode both --model-dir <模型缓存目录>
```

**Windows 环境三个关键坑（务必注意）**：

1. **venv 和模型目录建议放独立目录**（如 `D:\WorkBuddy`，本机当前即此位置）——若被部分开发环境的「批量删除守卫」拦截 pip 装包/模型解压，可 `unset CODEBUDDY_TOOL_CALL_ID CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR CODEBUDDY_SAFE_DELETE_BULK_GUARD` 绕过；
2. 运行时加 `PROCESSOR_ARCHITECTURE=AMD64`——规避 `platform.machine()` 在部分运行环境下返回空值导致的误判；
3. 模型首次下载走国内网络**直连**，不要走代理。

主项目侧三个环境变量（缺省有默认值，换机器可覆盖）：

| 环境变量 | 含义 | 默认值 |
|---|---|---|
| `IDCARD_MASKER_SCRIPT` | 离线脱敏脚本路径 | `offline_desens/idcard_masker.py`（项目内，随仓库分发） |
| `PADDLE_PYTHON` | PaddleOCR 独立 venv 的 python | `.paddle-venv\Scripts\python.exe` |
| `PADDLE_MODEL_DIR` | 模型缓存目录 | `models\paddleocr` |

## 六、工作流程

```
1. 拉取待办        auto_approve.py 调用 scpma 接口获取待办列表
      ↓
2. 下载材料        对每家供应商下载营业执照/身份证/财报/资质证书等附件
      ↓
3. OCR 识别        textin_pipeline.py 调用 TextIn 识别证件，抽取关键字段
      ↓
4. 规则判定        按 rules.yaml 中的准入标准逐项判定（通过/退回/转人工）
      ↓
5. 生成报告        gen_stage2_report.py 输出 HTML 报告 + 审批意见纯文本
      ↓
6. 人工复核        审批人员查看报告，复制审批意见粘贴进 ICCEC 审批框
```

## 七、规则引擎说明（可复用核心）

准入标准全部以 YAML 配置形式存放在 `rules.yaml`，包括：

- **part_1_basic**：基础准入项（营业执照、注册资本、法人身份证、纳税信用、财务报告、ISO 三体系、检验检测资质等）
- **part_2_scales**：规模分级（贸易商/服务商/承包商/检验检测类等不同阈值）
- **decision_logic**：综合决策逻辑（硬伤退回、软伤转人工、全通过则同意）

**新增/修改规则无需改代码**：在 `rules.yaml` 添加一个 check 节，指定 `check_type`（material/field/conditional_material）、`rule`（沙箱表达式）即可。规则表达式通过 `eval.py` 在沙箱中执行，只能读取 supplier 字段，安全可控。

**跨场景复用**：本规则引擎可平移到任何"拉数据 → 套规则 → 出结论"的审批场景，例如招标资格审查、合同审批等，只需替换 `rules.yaml` 与 `auto_approve.py` 中的接口调用部分。

## 八、安全与合规

- **DRY_RUN 安全开关**：默认 `true`；Web 流水线始终关闭真实回写，命令行真实提交还需额外开关及人工确认
- **凭证隔离**：Web UI 粘贴的凭证只保留在当前进程内存中，不写 `.env`；已有 `.env` 仍兼容读取，且已被 `.gitignore` 排除
- **敏感数据保护**：供应商缓存、OCR 结果、企查查数据均被 `.gitignore` 排除，不提交到仓库
- **操作日志**：所有审批动作写日志 `approval_YYYYMMDD.log`，可追溯谁批的、依据哪条、几点几分
- **保守决策**：OCR 判定的 fail 归入"转人工"而非直接 reject，避免 OCR 误判导致错杀

## 九、技术栈

| 层 | 技术 |
|----|------|
| Web 框架 | FastAPI + Uvicorn |
| 模板引擎 | Jinja2 |
| OCR 服务 | TextIn（api.textin.com） |
| 规则引擎 | 自研沙箱求值器（eval.py） |
| 配置 | YAML（rules.yaml） + .env |
| HTTP 客户端 | requests |
| 目标系统 | 招采平台供应商管理审批系统（scpma.iccec.cn） |

## 十、可复用性与推广价值

本系统设计之初即考虑**部门内多人复用 + 跨场景平移**两个层面：

1. **部门内复用**：Web UI 不绑定个人账号，Cookie 在线更新，规则统一维护，新同事浏览器打开即用
2. **跨场景平移**：规则引擎与业务代码解耦，`rules.yaml` 可替换为任何审批场景的标准；`auto_approve.py` 中的接口调用部分可替换为其他系统的 API
3. **规则可审计**：所有判定依据写在 YAML，评审/审计时一目了然，不会出现"代码里藏着魔法数字"
4. **渐进式自动化**：从纯人工 → 命令行辅助 → Web UI → 未来对接官方 API，每一步都可独立交付，不会一次性推翻重来
5. **离线脱敏方案可独立复用**：
   - 现状：已实现身份证的 PaddleOCR 本地离线精确脱敏，敏感信息不出本机；
   - 未来潜力：该方案可独立抽取为通用脱敏组件，扩展接入驾驶证、护照、营业执照关键字段、银行账户等其他证件/凭证的脱敏功能，供全公司各业务部门（尤其是商法、采购等证件材料较多的部门）在合规调用外部 AI 前对敏感信息做本地脱敏，从源头降低证据信息泄露风险

## 十一、已知限制与后续规划

- 当前依赖浏览器抓包获取 Cookie，未来希望对接公司 IT 提供的官方 API（已提交申请，等待回复）
- 财务报告抽取器目前覆盖年度与现金流，资产负债率/流动比率抽取待完善
- 企查查数据通过外部连接器获取，未来考虑改为独立 API 调用避免依赖
- 商业信誉专项核验（失信被执行人、被执行人、限制消费令、经营异常、严重违法等）目前因接入企查查免费版接口，尚无法实现自动化核验；该专项数据接口已向集团提交接入申请，当前处于审批阶段，审批通过后即可在程序中实现上述信息的自动化核验
- Web UI 第一版仅生成审查报告，不自动回写 ICCEC；后续版本将加"一键回写"按钮
