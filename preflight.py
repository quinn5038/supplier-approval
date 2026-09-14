"""Read-only configuration diagnostics; never display credential values."""
import os
from pathlib import Path

# “按需配置”不参与基础就绪计数：实际接口是否要求它们以平台响应为准。
OPTIONAL_CHECKS = {
    "平台 APP_TOKEN（asca 请求凭证；未验证有效性）",
    "平台 AGENT_ID（审批页参数）",
    "企业信息凭证（可选；缺少时相关项转人工）",
}
CAPABILITY_CHECKS = {
    "Paddle 本地解释器",
    "Paddle 三组模型文件",
    "TextIn 凭证配置（非在线有效性验证）",
}

CHECK_GUIDES = {
    "本机便捷模式或账号配置": (
        "双击 start_webui.bat 即使用本机便捷模式，无需另设账号。",
        "若单独部署账号模式，设置 WEB_AUTH_MODE=password 和非空 WEB_ADMIN_PASSWORD；值分别为 password 和自设密码。",
        "便捷模式只供本机访问；使用 BAT 时会固定为本机模式。",
    ),
    "平台 Cookie 和 Authorization（网页粘贴；未验证有效性）": (
        "先在浏览器登录招采平台，按 F12 打开 Network，选一条 scpma.iccec.cn/apis/ 请求。",
        "从 Request Headers 复制完整 Cookie 与 Authorization 值；在首页右上角“更新平台凭证”分别粘贴，不带字段名或引号。",
        "Cookie 通常是以分号分隔的 key=value；Authorization 若含 Bearer 前缀应一并复制。关闭服务后需重新粘贴，不要把凭证发给他人。",
    ),
    "平台 APP_TOKEN（asca 请求凭证；未验证有效性）": (
        "若 asca 待办读取失败，在 Network 中查看 asca.iccec.cn/apis/asca/todo/home/list 的 Request Headers 是否带 APP_TOKEN。",
        "若有 APP_TOKEN，复制其值到“更新平台凭证 → 高级配置 → APP_TOKEN”，不带字段名或引号；也可在 .env 中填写 APP_TOKEN=<值> 后重启。",
        "该值只用于 asca 请求头；部分平台会话可在未填写时正常读取待办，因此空白不表示整个系统不可用。",
    ),
    "平台 AGENT_ID（审批页参数）": (
        "若打开供应商详情时提示缺少 agentId，查看平台审批页地址栏中 a= 后面的参数值。",
        "在“更新平台凭证 → 高级配置 → AGENT_ID”填写参数值本身，不带 a=；也可在 .env 中填写 AGENT_ID=<值> 后重启。",
        "该值进入部分 scpma 请求体；接口接受空值时无需配置。地址中没有 a= 时不要猜测填写。",
    ),
    "规则文件": (
        "使用仓库或安装包自带的项目根目录 rules.yaml，无需从外部服务获取。",
        "若显示未就绪，确认文件仍在项目根目录，内容是有效 YAML 且包含 part_1_basic；修改规则前请备份。",
        "规则文件不得写入平台凭证；规则通过本机读取，不会由启动检查上传。",
    ),
    "工作目录可写": (
        "确认项目安装目录存在，当前 Windows 用户对该目录有写入权限。",
        "若显示未就绪，可将项目放到本人可写的文件夹，或由管理员授予该目录写权限；无需填写环境变量。",
        "审批过程会写入本机缓存、结果和审计文件；不要直接在只读目录中运行。",
    ),
    "Paddle 本地解释器": (
        "首次安装可运行 SupplierApproval-Setup.exe；手动安装可在项目根目录运行 powershell -ExecutionPolicy Bypass -File .\\setup_paddle.ps1。",
        "默认路径为 .paddle-venv/Scripts/python.exe；若放在别处，在 .env 中填写 PADDLE_PYTHON=<python.exe 的绝对路径> 并重启。",
        "这里只核对文件是否存在；识别能力请以安装脚本的本地自检为准。缺失时身份证核验转人工。",
    ),
    "Paddle 三组模型文件": (
        "运行安装器或 setup_paddle.ps1 下载官方 det、rec、cls 三组模型。",
        "默认目录为 models/paddleocr；自定义时在 .env 中填写 PADDLE_MODEL_DIR=<模型目录绝对路径> 并重启。",
        "目录下各 det/rec/cls 子目录均需有 inference.pdmodel 与 inference.pdiparams；这里只核对文件存在。",
    ),
    "TextIn 凭证配置（非在线有效性验证）": (
        "登录 TextIn 开发者工作台，在账号设置的开发者信息中获取 APP ID 和 SECRET CODE。",
        "在项目根目录 .env 中分别填写 TEXTIN_APP_ID=<值>、TEXTIN_SECRET_CODE=<值>，保存并重启服务；等号后不加引号。",
        "检查只判断非空，不验证账号权限；仅允许范围内的非敏感材料可发送该服务。",
    ),
    "企业信息凭证（可选；缺少时相关项转人工）": (
        "向公司 IT 或获授权的企查查 API 服务渠道申请 AppKey、SecretKey 与接口访问权限。",
        "取得后在 .env 中填写 QCC_APP_KEY=<值>、QCC_SECRET_KEY=<值>，并按获批通道配置 QCC_BASE_URL，重启服务。",
        "当前接入仍需核对服务连通性与接口权限；无法连接或无授权时保持空白，相关证据转人工。",
    ),
    "真实回写关闭": (
        "日常双击 start_webui.bat 即保持真实回写关闭，无需另行配置。",
        "若自行配置，在 .env 中保持 ENABLE_LIVE_APPROVAL=false；Web 流水线会强制关闭回写。",
        "此项显示就绪表示不会向平台提交审批决定；审查报告仍需由审核员人工复核。",
    ),
}


def describe_checks(values):
    """Join diagnostics and static instructions without exposing configuration values."""
    return [{"label": label, "ready": ready,
             "kind": "optional" if label in OPTIONAL_CHECKS else
                     "capability" if label in CAPABILITY_CHECKS else "core",
             "steps": CHECK_GUIDES[label]}
            for label, ready in values.items()]


def checks(base):
    import auto_approve
    import desensitize
    base = Path(base)
    try:
        config = auto_approve.load_rules()
        rules_ok = bool(config.get("part_1_basic"))
    except (OSError, ValueError, TypeError):
        rules_ok = False
    return {
        "本机便捷模式或账号配置": os.getenv("WEB_AUTH_MODE", "local") == "local" or bool(os.getenv("WEB_ADMIN_PASSWORD")),
        "平台 Cookie 和 Authorization（网页粘贴；未验证有效性）": all(os.getenv(k) for k in ("SCPMA_COOKIE", "SCPMA_AUTH_TOKEN")),
        "平台 APP_TOKEN（asca 请求凭证；未验证有效性）": bool(os.getenv("APP_TOKEN")),
        "平台 AGENT_ID（审批页参数）": bool(os.getenv("AGENT_ID")),
        "规则文件": rules_ok,
        "工作目录可写": os.access(base, os.W_OK),
        "Paddle 本地解释器": Path(desensitize._PADDLE_PYTHON).is_file(),
        "Paddle 三组模型文件": all((Path(desensitize._PADDLE_MODEL_DIR) / part / name).is_file()
                                   for part in ("det", "rec", "cls")
                                   for name in ("inference.pdmodel", "inference.pdiparams")),
        "TextIn 凭证配置（非在线有效性验证）": all(os.getenv(k) for k in ("TEXTIN_APP_ID", "TEXTIN_SECRET_CODE")),
        "企业信息凭证（可选；缺少时相关项转人工）": all(os.getenv(k) for k in ("QCC_APP_KEY", "QCC_SECRET_KEY")),
        "真实回写关闭": os.getenv("ENABLE_LIVE_APPROVAL", "false").lower() != "true",
    }
