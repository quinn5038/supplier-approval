# OCR 双后端说明

当前系统在同一主分支中提供两个可切换的材料 OCR 后端：

- `online`：公开材料使用 TextIn 在线 OCR；
- `offline`：公开材料使用本机 PaddleOCR，不调用 TextIn；
- 法人身份证无论选择哪个后端，都只在本机完成识别与脱敏；
- 招采材料下载和企业信息查询仍需要网络，因此“离线 OCR”不等于整套系统可断网运行。

WebUI 首页可切换后端。存在正在运行的审批任务时禁止切换，防止任务状态和报告归属错位。程序不会在所选后端失败时静默回退到另一个后端；无法识别的材料按规则转人工。

## 结果与缓存隔离

原始下载材料在两个后端间共用，OCR 中间缓存、结构化结果和报告相互隔离：

| 内容 | 在线模式 | 离线模式 |
|---|---|---|
| OCR 中间缓存 | `*.online.md` / `*.online.detail.json` / `*.online.sha256` | `*.offline.md` / `*.offline.detail.json` / `*.offline.sha256` |
| OCR 结构化结果 | `textin_results.online.json` | `textin_results.offline.json` |
| 阶段二结果 | `stage2_results.online.json` | `stage2_results.offline.json` |
| HTML 报告 | `output/online/` | `output/offline/` |

旧版未带模式后缀的在线结果只用于迁移兼容；供应商重新审批后会写入上述隔离路径。不要手工复制一个后端的缓存文件充当另一个后端的识别结果。

## 离线后端范围

- 本地识别营业执照、纳税证明、ISO 9001/14001/45001、生产许可、运输许可、售后认证等已支持的公开材料；
- 授权书、售后承诺书和财报等不适合自动抽取的材料按现有规则转人工；
- 支持 PDF、PNG、JPG/JPEG、BMP；不支持的格式转人工；
- 空白页、超长文档、模型不可用或解析异常都会给出人工核验提示，不调用在线 OCR 兜底。

离线识别会保留页码、文字框坐标和置信度。低置信度提示按参与关键字段和核验结果的文字处理，不再因标题、页脚等无关文字的低分直接否定整份材料。

## 部署与验证

默认使用项目目录下 `.paddle-venv\Scripts\python.exe` 和 `models\paddleocr`；可通过 `PADDLE_PYTHON`、`PADDLE_MODEL_DIR` 覆盖。首次联网部署运行：

```powershell
powershell -ExecutionPolicy Bypass -File setup_paddle.ps1
```

安装过程可能联网下载依赖和公开模型；正式识别进程禁止自动下载模型。环境缺失时报告转人工，不调用外部服务。

真实模型自检：

```powershell
& <PADDLE_PYTHON> offline_desens/material_smoke_test.py --model-dir <PADDLE_MODEL_DIR>
```

自检使用合成 PNG 和 PDF，只验证模型可加载、识别结果含页码/坐标/置信度，不代表真实业务材料准确率。合并前的固定样本对比数据保存在 [离线 OCR 历史对比测试报告](离线OCR对比测试报告.md)，仅作为历史基线。
