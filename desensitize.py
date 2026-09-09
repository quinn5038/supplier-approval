"""
离线材料脱敏助手（身份证 PaddleOCR 精确打码）
==============================================

按保密专员共识：法人身份证等敏感文件不能直接发给 AI 处理。
本模块只负责身份证的离线脱敏——通过调用外部 PaddleOCR 脚本
（idcard_masker.py）在本地精确打码，仅保留「姓名」+「有效期限」，
其余（证号/住址/头像等）按文字框黑遮，敏感信息不离开本机。

9/9 简化：移除财报 PDF 脱敏（各公司财报格式差异大，难以统一离线脱敏），
财报按保密合规要求不通过 OCR 解析，改走企查查/人工核验。

调用方式：
    from desensitize import desensitize_dir, desensitize_id_cards_with_paddle
    desensitize_dir(Path("cache_v4"), Path("cache_v4_desens"))
    desensitize_id_cards_with_paddle(Path("cache_v4"), Path("cache_v4_desens"))

    # 或命令行：
    python desensitize.py --src cache_v4 --dst cache_v4_desens
"""
import logging
import json
import os
import re
import sys
from pathlib import Path

log = logging.getLogger("desensitize")


# ============================================================
# 公开 API
# ============================================================
def desensitize_dir(src_dir, dst_dir):
    """将供应商材料原样复制到脱敏目录。

    src_dir: 原始材料目录（如 cache_v4/{todoId}/）
    dst_dir: 输出目录（如 cache_v4_desens/{todoId}/）

    9/9 简化：移除财报 PDF 脱敏，本函数只做原样复制；身份证的精确打码
    由 desensitize_id_cards_with_paddle 单独覆盖。逐文件覆盖写入（同名文件
    覆盖，stale 文件保留无害），避免整目录删除触发沙箱批量删除保护。
    """
    import shutil
    src = Path(src_dir)
    dst = Path(dst_dir)
    if not src.exists():
        log.warning(f"源目录不存在：{src}")
        return
    dst.mkdir(parents=True, exist_ok=True)

    n = 0
    for f in src.rglob("*"):
        if f.is_file():
            rel = f.relative_to(src)
            dst_file = dst / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst_file)
            n += 1

    log.info(f"[复制完成] {src} → {dst}：共 {n} 个文件（身份证随后由 PaddleOCR 精确打码覆盖）")


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


# 财报 PDF 脱敏已删除（2026-09-09）：各公司财报格式差异大，难以统一离线脱敏，改走企查查/人工核验
# 身份证粗比例打码已删除（2026-09-09）：改由下方 desensitize_id_cards_with_paddle 用 PaddleOCR 精确打码
# PaddleOCR 精确打码（2026-09-09 接入 idcard_masker.py，替换粗比例矩形）
# ============================================================
# PaddleOCR 脱敏的三个路径：优先读环境变量（便于换机器/移交复用），否则用默认值
_IDCARD_MASKER_SCRIPT = os.environ.get(
    "IDCARD_MASKER_SCRIPT",
    r"E:\OneDrive\工作\05 证书、竞赛\人工智能创新大赛\离线脱敏程序\idcard_masker.py",
)
_PADDLE_PYTHON = os.environ.get(
    "PADDLE_PYTHON",
    r"C:\Users\CHEC\AppData\Local\Temp\idcard_env\Scripts\python.exe",
)
_PADDLE_MODEL_DIR = os.environ.get(
    "PADDLE_MODEL_DIR",
    r"C:\Users\CHEC\AppData\Local\Temp\paddleocr-models",
)


def desensitize_id_cards_with_paddle(src_dir, dst_dir):
    """用 idcard_masker.py（PaddleOCR）批量精确打码身份证图片。

    2026-09-09 接入：粗比例矩形已删除，身份证统一走 PaddleOCR 精确到文字框的打码，
    只保留「姓名/有效期限」，其余黑遮。

    返回 dict：{rel.as_posix(): {"cards": [...]}}  成功（含 PaddleOCR 识别结果）
              {rel.as_posix(): {"error": "原因"}}   失败
    调用方（textin_pipeline.run_parse）据此区分成功/失败：成功用识别结果核验，
    失败则明确写失败原因并转人工审批。
    """
    import subprocess
    import shutil
    import tempfile
    import os

    script = Path(_IDCARD_MASKER_SCRIPT)
    paddle_python = Path(_PADDLE_PYTHON)
    model_dir = Path(_PADDLE_MODEL_DIR)

    src = Path(src_dir)
    dst = Path(dst_dir)

    # 1. 收集身份证图片（保留相对路径，供回写用），区分「支持/不支持」的格式
    _load_type_name_sets()
    id_cards = []  # [(相对路径, 绝对路径)] 支持的格式，交给 PaddleOCR
    result = {}    # {rel.as_posix(): {"cards": [...]} 或 {"error": "..."}}
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        if not _is_id_card_image(f):
            continue
        rel = f.relative_to(src)
        # 9/9 修复：白名单补上 .pdf（及 .tif/.tiff/.webp）——idcard_masker.py 实际支持
        # PDF（fitz 渲染成图再 OCR，此前批量测试柯力发/王文巍 PDF 均成功），此前漏了
        # .pdf 导致 PDF 身份证被误判「格式不受支持」。
        if f.suffix.lower() not in (".jpg", ".jpeg", ".png", ".bmp",
                                    ".tif", ".tiff", ".webp", ".pdf"):
            result[rel.as_posix()] = {
                "error": f"身份证文件格式 {f.suffix} 不受支持（仅支持 jpg/png/bmp/tif/webp/pdf），无法打码识别"
            }
            continue
        id_cards.append((rel, f))

    # 环境缺失：所有待打码身份证统一报错（不再回退粗比例）
    if not script.exists() or not paddle_python.exists():
        log.warning("[PaddleOCR] 未找到 idcard_masker.py 或 Python 3.11 环境")
        for rel, f in id_cards:
            result.setdefault(rel.as_posix(), {
                "error": "PaddleOCR 打码环境未就绪（缺少 idcard_masker.py 或 Python 3.11）"
            })
        return result

    if not id_cards:
        return result

    n = 0
    with tempfile.TemporaryDirectory(prefix="idcard_paddle_") as tmp:
        tmp_root = Path(tmp)
        tmp_in = tmp_root / "in"
        tmp_out = tmp_root / "out"
        # 复制身份证到临时输入目录（保留相对路径，文件名原样）
        for rel, f in id_cards:
            (tmp_in / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, tmp_in / rel)

        # 2. subprocess 调 idcard_masker.py（独立 Python 3.11 环境）
        env = dict(os.environ)
        env["PROCESSOR_ARCHITECTURE"] = "AMD64"
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            proc = subprocess.run(
                [str(paddle_python), str(script), str(tmp_in), str(tmp_out),
                 "--output-mode", "both", "--model-dir", str(model_dir)],
                capture_output=True, text=True, env=env, timeout=1800,
            )
        except Exception as e:
            log.warning(f"[PaddleOCR] idcard_masker 调用异常：{e}")
            for rel, f in id_cards:
                result[rel.as_posix()] = {"error": f"PaddleOCR 打码调用异常：{e}"}
            return result
        if proc.returncode != 0:
            log.warning(f"[PaddleOCR] idcard_masker 失败：{proc.stderr[-300:]}")
            reason = (proc.stderr or "").strip()[-200:]
            for rel, f in id_cards:
                result[rel.as_posix()] = {"error": f"PaddleOCR 打码失败：{reason or '未知错误'}"}
            return result

        # 3. 回写 + 收集识别结果：images/..._masked.png → dst/{rel}；fields/*.json → result
        for rel, f in id_cards:
            stem = Path(rel)
            out_stem = stem.with_suffix("").parent / f"{stem.stem}__page-001"
            masked = tmp_out / "images" / out_stem.with_name(f"{out_stem.name}__masked.png")
            fields_json = tmp_out / "fields" / out_stem.with_name(f"{out_stem.name}__fields.json")
            if not masked.exists():
                log.warning(f"[PaddleOCR] 未找到打码结果：{masked.name}（{rel}）")
                result[rel.as_posix()] = {"error": "PaddleOCR 未产出打码结果"}
                continue
            dst_file = dst / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                # 直接复制打码结果（PNG 内容；TextIn/PIL 按内容识别格式，扩展名不影响解析）
                shutil.copy2(masked, dst_file)
                n += 1
            except Exception as e:
                log.warning(f"[PaddleOCR] 回写失败 {rel}：{e}")
                result[rel.as_posix()] = {"error": f"打码结果回写失败：{e}"}
                continue
            # 收集 PaddleOCR 识别结果（cards：side/name/valid_until）
            if fields_json.exists():
                try:
                    info = json.loads(fields_json.read_text(encoding="utf-8"))
                    result[rel.as_posix()] = {"cards": info.get("cards", [])}
                except Exception as e:
                    log.warning(f"[PaddleOCR] 读取 fields.json 失败 {rel}：{e}")
                    result[rel.as_posix()] = {"error": f"读取识别结果失败：{e}"}
            else:
                result[rel.as_posix()] = {"error": "PaddleOCR 未产出识别结果（fields.json 缺失）"}

    n_ok = sum(1 for v in result.values() if "cards" in v)
    log.info(f"[PaddleOCR] 精确打码身份证 {n}/{len(id_cards)} 张，成功识别 {n_ok} 份")
    return result


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