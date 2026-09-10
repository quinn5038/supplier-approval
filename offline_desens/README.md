# 中国大陆二代身份证最小保留式脱敏

本地批处理工具，读取图片与 PDF 后，逐张识别身份证正反面和方向：

- 正面仅保留蓝色“姓名”之后的姓名值；
- 背面仅保留“有效期限”之后的完整值，支持 `2017.08.23-2037.08.23` 和 `2026.09.08-长期`；
- 其他卡片内容全部用不可逆黑色遮盖。

它不会修改输入文件。无法可靠识别卡片、正反面或指定字段时，会整张卡片（或整页）遮盖，避免信息泄露。

## 安装

建议 Python 3.10–3.13。先按 PaddlePaddle 官方说明安装适合本机的 CPU 版 `paddlepaddle`，再执行：

```bash
python -m pip install -r requirements.txt
```

首次运行会下载 PaddleOCR 中文模型。若默认模型目录不可写，使用下文的 `--model-dir` 指向可写位置。

### Windows 10/11 x64

Windows 可直接运行本程序；脚本没有 Unix 专属命令或路径。推荐使用 **64 位 Python 3.10** 和 PowerShell。Windows ARM64 当前不能安装 PaddlePaddle，因此不受支持。

```powershell
# 在项目目录内执行
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install paddlepaddle==3.3.0 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
python -m pip install -r requirements.txt
python -c "import paddle; paddle.utils.run_check()"
```

如果 PowerShell 拒绝激活虚拟环境，仅对当前窗口执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

首次执行可把模型缓存放在项目目录，避免公司设备限制用户目录写入：

```powershell
python .\idcard_masker.py .\input .\output --output-mode both --model-dir .\paddleocr-models
```

## 运行

```bash
python idcard_masker.py /path/to/input /path/to/output --output-mode both
```

`--output-mode`：

- `images`（默认）：只写脱敏图片；
- `fields`：只写字段 JSON 与汇总 CSV；
- `both`：同时写入两者。

输出目录结构：

```text
output/
  images/
    applicant__page-001__masked.png
  fields/
    applicant__page-001__fields.json
    fields.csv
```

输入目录内的子目录会在 `images/`、`fields/` 下保留。每页一个图片文件和一个 JSON 文件；`fields.csv` 汇总每张检测到的卡片。

常用参数：

```bash
# 处理白底上下双面扫描、单张卡片、或正反面任意排列的默认模式
python idcard_masker.py ./input ./output --output-mode both

# 无法检测卡片边框时，按上下两半作为两张卡片尝试识别
python idcard_masker.py ./input ./output --layout stacked

# 指定 PaddleOCR 模型缓存目录
python idcard_masker.py ./input ./output --model-dir ./paddleocr-models

# PDF 以 400 DPI 渲染，提高低清扫描识别率
python idcard_masker.py ./input ./output --dpi 400
```

## 质量与隐私说明

- 图片/PDF 支持 `jpg/jpeg/png/bmp/tif/tiff/webp/pdf`，PDF 页输出为 PNG。
- 程序会尝试 0°、90°、180°、270° 四个方向，再根据“姓名”“有效期限”等字段判定正反面，因而不依赖上下顺序。
- 请抽检输出，尤其是严重反光、遮挡、透视变形、分辨率极低、卡片间重叠或文字已经被部分遮盖的样本。
- 本工具只在本地处理原始证件。首次下载 OCR 模型需要网络；模型下载完成后，证件图像不会上传。
