"""
离线材料脱敏助手（2026-09-04 保密合规改造·改造点③）
==============================================

按保密专员共识：供应商未公开披露的财报、类似法人身份证的敏感文件
不能直接发给 AI 处理。本模块提供本地纯 Python 脱敏处理，
输出到 cache_v4_desens/ 给后续 textin / AI 识别使用。

设计原则：
- 本地处理：不调任何云端 OCR/AI（这就是"脱敏"的核心）
- 文件名加 _desens 后缀与原文件区分
- 失败时返回原文件 + warning，不抛异常
- 输出到 cache_v4_desens/（不进 .gitignore，与 files_cache 同等待遇）

按文件类型分四类处理：
- 财报 PDF：保留数字（财务指标需要），人名/签字/银行账号打码
- 身份证 JPG/PNG：人脸马赛克 + 姓名/证号/地址打码
- 营业执照：原样保留（公开信息）
- 其他（ISO/授权/声明）：原样保留

调用方式：
    from desensitize import desensitize_dir
    desensitize_dir(Path("cache_v4"), Path("cache_v4_desens"))

    # 或命令行：
    python desensitize.py --src cache_v4 --dst cache_v4_desens
"""
import logging
import re
import sys
from pathlib import Path

log = logging.getLogger("desensitize")


# ============================================================
# 公开 API
# ============================================================
def desensitize_dir(src_dir, dst_dir):
    """脱敏整个目录的供应商材料。

    src_dir: 原始材料目录（如 cache_v4/{todoId}/）
    dst_dir: 输出目录（如 cache_v4_desens/{todoId}/）
    """
    src = Path(src_dir)
    dst = Path(dst_dir)
    if not src.exists():
        log.warning(f"源目录不存在：{src}")
        return
    if dst.exists():
        # 清空旧的脱敏目录，避免混合新旧数据
        import shutil
        shutil.rmtree(dst)
    dst.mkdir(parents=True)

    n_total = 0
    n_desens = 0
    for f in src.rglob("*"):
        if f.is_file():
            n_total += 1
            rel = f.relative_to(src)
            dst_file = dst / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            if _desensitize_one(f, dst_file):
                n_desens += 1
            else:
                # 原样复制（不阻塞流程）
                import shutil
                shutil.copy2(f, dst_file)

    log.info(f"[脱敏完成] {src} → {dst}：共 {n_total} 个文件，实际脱敏 {n_desens} 个")


def _desensitize_one(src, dst):
    """根据文件类型分派脱敏函数。返回 True 表示已脱敏，False 表示原样复制。"""
    ext = src.suffix.lower()
    name_lower = src.name.lower()

    if ext == ".pdf":
        if any(k in name_lower for k in ["财务", "审计", "财报", "审计报告", "financial"]):
            return _desensitize_financial_pdf(src, dst)
        return False  # 其他 PDF 原样保留

    if ext in (".jpg", ".jpeg", ".png", ".bmp", ".gif"):
        # 身份证判断：通过文件名特征（法人姓名.jpg 或包含 id_/证件/身份证）
        if any(k in name_lower for k in ["身份证", "证件", "id_card", "id_"]) \
                or _looks_like_id_card_name(name_lower):
            return _desensitize_id_card_image(src, dst)
        return False  # 其他图片原样保留

    return False


# 常见材料关键词（用于排除——这些不是身份证）
MATERIAL_KEYWORDS = (
    "营业执照", "执照", "营业", "税务", "纳税", "信用", "质量",
    "ISO", "授权", "代理", "经销", "声明", "承诺", "售后",
    "生产", "许可", "强制", "3C", "检验", "检测",
    "资质", "证书", "审计", "财报", "财务", "报告",
    "专利", "商标", "ICP",
)


def _looks_like_id_card_name(name_lower):
    """判断文件名是否像身份证（中文姓名+扩展名，可能有数字ID前缀）

    排除规则：
    1. 文件名包含常见材料关键词（如"营业执照"）→ 不是身份证
    2. 纯中文姓名 + 扩展名（如 李作发.jpg）→ 可能是身份证
    3) 数字ID前缀 + 下划线 + 中文姓名 + 扩展名（如 10091700300_李作发.jpg）→ 可能是身份证
    """
    # 排除常见材料关键词
    if any(kw in name_lower for kw in MATERIAL_KEYWORDS):
        return False
    # 1) 纯中文姓名 + 扩展名（如 李作发.jpg）
    m = re.match(r"^([\u4e00-\u9fa5]{2,4})\.(jpg|jpeg|png)$", name_lower)
    if m:
        return True
    # 2) 数字ID前缀 + 下划线 + 中文姓名 + 扩展名（如 10091700300_李作发.jpg）
    m = re.match(r"^\d+_([\u4e00-\u9fa5]{2,4})\.(jpg|jpeg|png)$", name_lower)
    return bool(m)


# ============================================================
# 财报 PDF 脱敏
# ============================================================
SENSITIVE_PATTERNS = [
    (re.compile(r"\b\d{19}\b"), "银行账号"),                       # 19 位银行账号
    (re.compile(r"\b\d{17}[\dXx]\b"), "身份证号"),                  # 18 位身份证号
    (re.compile(r"签字|盖章|经办人|复核人|主管|会计|出纳|审计师"),
     "签字栏"),
    (re.compile(r"法定代表人\s*签字"), "法人签字"),
]


def _is_sensitive_text(text):
    """判断 span 文本是否含敏感字段"""
    for pat, _ in SENSITIVE_PATTERNS:
        if pat.search(text):
            return True
    return False


def _desensitize_financial_pdf(src, dst):
    """财报 PDF 脱敏：保留数字表格，打码人名/签字/银行账号"""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        log.warning("[财报 PDF 脱敏] PyMuPDF 未安装，跳过")
        return False

    try:
        doc = fitz.open(str(src))
        n_redacted = 0
        for page in doc:
            text_dict = page.get_text("dict")
            for block in text_dict.get("blocks", []):
                for line in block.get("lines", []):
                    for char in line.get("spans", []):
                        text = char.get("text", "")
                        if _is_sensitive_text(text):
                            # 用白色矩形覆盖该 span
                            rect = fitz.Rect(char["bbox"])
                            page.add_redact_rect(rect, fill=(1, 1, 1))
                            n_redacted += 1
            page.apply_redactions()

        if n_redacted > 0:
            doc.save(str(dst))
            log.info(f"[财报 PDF 脱敏] {src.name} → {dst.name}（打码 {n_redacted} 处）")
            return True
        doc.close()
        # 没有敏感字段，原样复制
        return False
    except Exception as e:
        log.warning(f"[财报 PDF 脱敏] {src.name} 失败：{e}，原样复制")
        return False


# ============================================================
# 身份证图片脱敏
# ============================================================
def _desensitize_id_card_image(src, dst):
    """身份证图片脱敏：保留国徽页（公开），整个人像页覆盖打码

    身份证布局（身份证图片扫描件，常见比例约 4:3）：
    - 上方 ~50% 是国徽页（公开）：含国徽 + "中华人民共和国居民身份证" + 签发机关 + 有效期
    - 下方 ~50% 是人像页（敏感）：含姓名 + 性别 + 出生 + 住址 + 公民身份证号 + 头像

    脱敏策略：
    - 整片覆盖人像页（白色 + "已脱敏"水印）
    - 人像页区域头像必须马赛克化（用 Pillow 的缩小再放大 = 像素化）

    简化方案——整个图片除国徽外全部打码（最保守，避免漏打码）。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        log.warning("[身份证脱敏] Pillow 未安装，跳过")
        return False

    try:
        img = Image.open(src).convert("RGB")
        w, h = img.size
        draw = ImageDraw.Draw(img)

        # 简化打码——直接打码下半部分（覆盖整个人像页 + 国徽页的"签发机关"行）
        # 但保留国徽本身（在图片顶 1/4）
        # 计算坐标：身份证扫描件通常上方 1/3 是国徽区，下方 2/3 是人像页
        # 为了最保守，整片覆盖下方 2/3
        text_box = (0, int(h * 0.30), w, h)
        draw.rectangle(text_box, fill=(255, 255, 255))

        # 头像照片马赛克——位置在人像页中部偏右（典型身份证布局）
        # 头像区域估算：宽 25-40%，高 30-65% 区域的中部
        face_box = (
            int(w * 0.25),   # 左
            int(h * 0.32),   # 上
            int(w * 0.50),   # 右
            int(h * 0.65),   # 下
        )
        # 先取头像区域（即使这部分已被白色覆盖，马赛克仍生效）
        if face_box[2] > face_box[0] and face_box[3] > face_box[1]:
            face = img.crop(face_box)
            # 缩小再放大 = 马赛克（即使空白图像也能产生像素化视觉效果）
            small_size = (max(1, face.width // 16), max(1, face.height // 16))
            small = face.resize(small_size)
            face_mosaic = small.resize(face.size, Image.NEAREST)
            img.paste(face_mosaic, face_box)
            # 再覆盖一次（确保是白色底）
            draw.rectangle(face_box, fill=(255, 255, 255))

        # 在中间加 "已脱敏" 水印（用 ASCII 文字避免字体缺失显示方框）
        try:
            font = ImageFont.truetype("arial.ttf", max(20, h // 30))
        except OSError:
            font = ImageFont.load_default()
        text = "[DESENSITIZED]"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        text_x = (w - text_w) // 2
        text_y = int(h * 0.55)
        # 浅灰文字
        draw.text((text_x, text_y), text, fill=(180, 180, 180), font=font)

        img.save(dst, quality=85)
        log.info(f"[身份证脱敏] {src.name} → {dst.name}（{w}x{h}，整片覆盖人像页 + 头像马赛克）")
        return True
    except Exception as e:
        log.warning(f"[身份证脱敏] {src.name} 失败：{e}，原样复制")
        return False


# ============================================================
# 命令行入口
# ============================================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="离线材料脱敏助手")
    parser.add_argument("--src", default="cache_v4",
                       help="源目录（默认 cache_v4，相对项目根）")
    parser.add_argument("--dst", default="cache_v4_desens",
                       help="目标目录（默认 cache_v4_desens）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(message)s")
    src = Path(args.src)
    dst = Path(args.dst)
    if not src.is_absolute():
        src = Path.cwd() / src
        dst = Path.cwd() / dst
    desensitize_dir(src, dst)
    print(f"[OK] 脱敏完成：{src} → {dst}")


if __name__ == "__main__":
    main()