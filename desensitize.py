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
import json
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

    9/6 修复：不再 shutil.rmtree(dst) 整目录删除——
    files_cache_desens/ 下累积 50+ 文件时触发 WorkBuddy 沙箱
    SAFE_DELETE_BULK_CONFIRM 批量删除保护直接杀子进程。
    改为逐文件覆盖写入（同名文件覆盖，stale 文件保留无害）。
    """
    src = Path(src_dir)
    dst = Path(dst_dir)
    if not src.exists():
        log.warning(f"源目录不存在：{src}")
        return
    dst.mkdir(parents=True, exist_ok=True)

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
        # 身份证判断（9/7 精确化：用 cache_v4 的 types 交叉验证，避免营业执照被误判）
        if _is_id_card_image(src):
            return _desensitize_id_card_image(src, dst)
        return False  # 其他图片原样保留

    return False


# cache_v4 的身份证/非身份证文件名集合（types 交叉验证，懒加载）
_ID_CARD_NAMES = None
_NON_ID_CARD_NAMES = None


def _load_type_name_sets():
    """从 cache_v4.json 读 materials_detail 的 types，构建：
    - _ID_CARD_NAMES：types 含 legal_person_id 的文件名（真身份证）
    - _NON_ID_CARD_NAMES：types 含其他已知类型但不含 legal_person_id 的文件名（营业执照等）
    """
    global _ID_CARD_NAMES, _NON_ID_CARD_NAMES
    if _ID_CARD_NAMES is not None:
        return
    _ID_CARD_NAMES = set()
    _NON_ID_CARD_NAMES = set()
    try:
        cache_file = Path(__file__).parent / "cache_v4.json"
        if cache_file.exists():
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
            for e in cache.values():
                for d in e.get("materials_detail", []):
                    types = set(d.get("types") or [])
                    fn = d.get("fileName", "")
                    if not fn:
                        continue
                    if "legal_person_id" in types:
                        _ID_CARD_NAMES.add(fn)
                    elif types:  # 有其他已知类型（营业执照/ISO/税务等）
                        _NON_ID_CARD_NAMES.add(fn)
    except Exception:
        pass


def _is_id_card_image(src):
    """判断图片是否为身份证（明确关键词 → cache_v4 types → 中文名保守兜底）"""
    name_lower = src.name.lower()
    # 1) 明确关键词
    if any(k in name_lower for k in ["身份证", "证件", "id_card", "id_"]):
        return True
    # 2) cache_v4 types 交叉验证
    _load_type_name_sets()
    bare = re.sub(r"^\d+_", "", src.name)  # 去 uploadId 前缀
    if bare in _ID_CARD_NAMES:
        return True
    if bare in _NON_ID_CARD_NAMES:
        return False  # cache_v4 明确分类为非身份证（营业执照等）
    # 3) 中文名保守兜底（cache_v4 无分类时的回退）
    return _looks_like_id_card_name(name_lower)


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
# 9/7 重写：仅保留「姓名」+「身份证有效期」两项，其余全部打码。
# 身份证为「上下拼版」扫描件：
#   - 上半部分为人像页正面（顶部约 45%），含姓名/性别/民族/出生/住址/公民身份号码+头像
#   - 下半部分为国徽页背面（底部约 55%），含国徽+"中华人民共和国居民身份证"+签发机关+有效期限
# 保留矩形（基于图像宽高比例）：
#   - 姓名：top 5%-13%, left 5%-58%（姓名行宽度，按标准排版估算）
#   - 有效期限：top 84%-94%, left 22%-72%（底部居中行）
# 全部其余区域：白色覆盖 + 「[DESENSITIZED]」水印 + 头像马赛克

# 保留矩形坐标（基于图像宽高比例）
_KEEP_NAME_BOX = (0.03, 0.58, 0.03, 0.24)          # left, right, top, bottom（正面姓名区）
_KEEP_EXPIRY_BOX = (0.18, 0.75, 0.76, 0.97)        # 背面有效期限区
_HEAD_PHOTO_BOX = (0.62, 0.98, 0.02, 0.42)        # 头像估算区域


def _desensitize_id_card_image(src, dst):
    """身份证图片脱敏：保留 姓名 + 有效期限，其余打码

    依据：9/7 与保密专员共识——身份证扫描件只暴露这两两个字段给 AI，
    其余敏感信息（性别/民族/出生/住址/身份证号/头像/签发机关）一律打码。
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

        # 解析保留矩形的像素坐标
        name_box = (
            int(w * _KEEP_NAME_BOX[0]), int(h * _KEEP_NAME_BOX[2]),
            int(w * _KEEP_NAME_BOX[1]), int(h * _KEEP_NAME_BOX[3]),
        )
        expiry_box = (
            int(w * _KEEP_EXPIRY_BOX[0]), int(h * _KEEP_EXPIRY_BOX[2]),
            int(w * _KEEP_EXPIRY_BOX[1]), int(h * _KEEP_EXPIRY_BOX[3]),
        )
        photo_box = (
            int(w * _HEAD_PHOTO_BOX[0]), int(h * _HEAD_PHOTO_BOX[2]),
            int(w * _HEAD_PHOTO_BOX[1]), int(h * _HEAD_PHOTO_BOX[3]),
        )

        # 1) 头像马赛克（先做，确保后续白色覆盖不影响视觉效果）
        if (photo_box[2] > photo_box[0] and photo_box[3] > photo_box[1]
                and photo_box[2] <= w and photo_box[3] <= h):
            face = img.crop(photo_box)
            small_size = (max(1, face.width // 12), max(1, face.height // 12))
            small = face.resize(small_size)
            face_mosaic = small.resize(face.size, Image.NEAREST)
            img.paste(face_mosaic, photo_box)
            draw.rectangle(photo_box, fill=(255, 255, 255))

        # 2) 整图打白色背景（保留姓名/有效期两个矩形）
        # 先整图覆盖
        draw.rectangle((0, 0, w, h), fill=(255, 255, 255))
        # 再还原姓名矩形和有效期矩形的原图内容
        img_org = Image.open(src).convert("RGB")
        if (name_box[2] > name_box[0] and name_box[3] > name_box[1]
                and name_box[2] <= w and name_box[3] <= h):
            name_crop = img_org.crop(name_box)
            img.paste(name_crop, name_box)
        if (expiry_box[2] > expiry_box[0] and expiry_box[3] > expiry_box[1]
                and expiry_box[2] <= w and expiry_box[3] <= h):
            expiry_crop = img_org.crop(expiry_box)
            img.paste(expiry_crop, expiry_box)

        # 3) 加 [DESENSITIZED] 水印（在姓名矩形下方，不遮挡关键字段）
        try:
            font = ImageFont.truetype("arial.ttf", max(18, h // 32))
        except OSError:
            font = ImageFont.load_default()
        text = "[DESENSITIZED]"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_x = (w - text_w) // 2
        # 水印 y 位置：姓名矩形下方到有效期限矩形上方之间的中间
        text_y = (name_box[3] + expiry_box[2]) // 2
        # 浅灰文字
        draw.text((text_x, text_y), text, fill=(170, 170, 170), font=font)

        img.save(dst, quality=85)
        log.info(f"[身份证脱敏] {src.name} → {dst.name}（{w}x{h}，"
                 f"姓名/有效期限保留，其余打码）")
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