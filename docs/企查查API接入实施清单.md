# 企查查 API 接入实施清单 v2

> 基于「企查查API接入方案.md」+ Quinn 9/4 同意"按 8 维度先接第一档+第二档围标串标"编写。
> 这份文档是动手前的**可行性对齐与流程清单**，看完确认时机再写代码。

---

## 0. 接入前提（Quinn 必须先提供）

| 凭据 | 用途 | 申请渠道 |
|---|---|---|
| **QCC_APP_KEY** | 企查查 API 调用凭据（请求头 `apicode`） | 企查查开放平台或公司 IT 共享 |
| **QCC_SECRET_KEY** | 签名密钥（生成 timestamp+sign） | 同上 |

**关键问题**：Quinn 给的"企业信息接口清单.xlsx"只是接口路径清单，**没有 AppKey/SecretKey**。需要 Quinn 联系公司 IT 或企查查开放平台（`https://openapi.qcc.com`）申请。

**建议**：
- 公司有企业版企查查账号 → 让 IT 共享 AppKey+SecretKey
- 没有 → 个人在企查查开放平台注册（送免费配额），但**免费配额很小**（一般100次/天），演示和测试不够用
- 推荐：让公司 IT 申请企业版账号，借给 Quinn 用

---

## 1. 13 个接口清单（第一档10 + 第二档围标串标3）

### 第一档：核心合规拦截项（10个，规则引擎强依赖）

| # | 接口路径 | 用途 | 对应规则项 | 工作量 |
|---|---|---|---|---|
| 1 | `/webapi/ic/verify/2.0` | 工商三要素核验（企业名/统一社会信用代码/法人） | A01 工商执照核实 | 中 |
| 2 | `/webapi/ic/baseinfoV3/2.0` | 工商基本信息（含主要人员、注册资本、成立日期） | A01/A02 工商+法人 | 中 |
| 3 | `/webapi/ic/holder/2.0` | 股东信息 | A03 股东核实 | 中 |
| 4 | `/webapi/ic/inverst/2.0` | 对外投资 | A03 股东核实（扩展） | 小 |
| 5 | `/webapi/ic/staff/2.0` | 主要人员（法人、董事、监事） | A02 法人核实 | 小 |
| 6 | `/webapi/jr/dishonest/2.0` | **失信被执行人**（A10硬拦截项） | A10 商业信誉 | 中 |
| 7 | `/webapi/jr/zhixinginfo` | **被执行人**（A10硬拦截项） | A10 商业信誉 | 中 |
| 8 | `/webapi/jr/consumptionRestriction` | **限制消费令**（A10硬拦截项） | A10 商业信誉 | 小 |
| 9 | `/webapi/mr/baseinfo/normal` | **经营异常名录**（A10硬拦截项） | A10 商业信誉 | 中 |
| 10 | `/webapi/mr/illegalinfo` | **严重违法失信**（A10硬拦截项） | A10 商业信誉 | 小 |

**额外补充**（第一档扩展，强烈推荐加）：
- `/webapi/m/certificate/2.0` 资质证书（用于 A04/A05 资质核实）— 小
- `/webapi/m/taxCredit/2.0` 纳税信用等级（A10 商业信誉扩展）— 小
- `/webapi/jr/endCase` 终本案件（A10 扩展）— 小

**第一档小计**：10个核心 + 3个扩展 = **13个接口**

### 第二档：围标串标合规加固（3个）

| # | 接口路径 | 用途 | 应用场景 | 工作量 |
|---|---|---|---|---|
| 11 | `/webapi/rela/shortPath/2.0` | **两家公司最短路径**（查共同股东/法人） | 多家供应商关联排查 | 大 |
| 12 | `/webapi/v3/investtree/ten` | **股权穿透10层**（追溯实控人） | 关联交易识别 | 大 |
| 13 | `/webapi/ic/humanholding/2.0` | **最终受益人**（实际控制人） | 影子股东识别 | 大 |

**第二档小计**：3个接口，**计算量大**（每对供应商需要调1次 shortPath，13家供应商两两组合78对）

---

## 2. 模块设计：`qcc_api_client.py`（新建零侵入）

```python
# supplier_approval_starter/qcc_api_client.py（新建）
"""
企查查 API 独立客户端，不依赖 WorkBuddy 连接器。
被 auto_approve.py 的 enhance_checklist_with_qcc 调用。
"""
import os, hashlib, time, requests, logging
from pathlib import Path

log = logging.getLogger(__name__)

QCC_APP_KEY = os.getenv("QCC_APP_KEY", "")
QCC_SECRET  = os.getenv("QCC_SECRET_KEY", "")
QCC_BASE    = "https://api.qcc.com"  # 实际域名看申请到的通道

def _sign(ts: str) -> str:
    """企查查签名：md5(ts + appkey + secretkey)"""
    return hashlib.md5(f"{ts}{QCC_APP_KEY}{QCC_SECRET}".encode()).hexdigest()

def _headers() -> dict:
    ts = str(int(time.time()))
    return {
        "apicode": QCC_APP_KEY,
        "timestamp": ts,
        "sign": _sign(ts),
        "Content-Type": "application/json",
    }

def _get(path: str, params: dict) -> dict:
    """统一调用入口，失败返回空dict不抛异常"""
    if not QCC_APP_KEY:
        log.warning("[QCC] AppKey 未配置，跳过 %s", path)
        return {}
    try:
        r = requests.get(QCC_BASE + path, headers=_headers(), params=params, timeout=15)
        return r.json() if r.ok else {}
    except Exception as e:
        log.warning(f"[QCC] {path} 调用失败：{e}")
        return {}

# 13个独立函数，每个对应一个接口
def verify_ic(name, creditcode, legalrep):
    """1. 工商三要素核验"""
    return _get("/webapi/ic/verify/2.0", {"name": name, "creditCode": creditcode, "legalRep": legalrep})

def baseinfo(name):
    """2. 工商基本信息"""
    return _get("/webapi/ic/baseinfoV3/2.0", {"keyword": name})

def holders(name):
    """3. 股东信息"""
    return _get("/webapi/ic/holder/2.0", {"keyword": name})

# ... 共13个函数，每个~10行
```

---

## 3. 现有代码修改点（4个文件，向后兼容）

### ① 新建 `qcc_api_client.py`（约300行，全新文件）
- 不影响现有代码，独立模块
- 被 auto_approve.py 调用

### ② 扩展 `qcc_results.json` 数据结构（向后兼容）

现有结构（WorkBuddy 连接器跑出来的缓存）：
```json
{
  "贝滨": {
    "工商": {...},
    "股东": [...],
    "实控人": {...},
    "财务": {...}
  }
}
```

扩展为：
```json
{
  "贝滨": {
    "工商": {...},      // 既有
    "股东": [...],      // 既有
    "实控人": {...},    // 既有
    "财务": {...},      // 既有
    "失信": [...],      // 新增 6块
    "被执行": [...],
    "限消令": [...],
    "经营异常": [...],
    "严重违法": [...],
    "资质证书": [...],
    "纳税信用": {...},
    "终本案件": [...]
  }
}
```

向后兼容：旧数据没有的新字段，enhance_checklist_with_qcc 用 `.get("失信", [])` 取空 list，规则引擎走"数据缺失→人工复核"路径，**不会误判**。

### ③ 修改 `auto_approve.py:1598 enhance_checklist_with_qcc`

当前只对 A01/A02/A10 三项增强，A10 只看登记状态。

扩展为：
```python
def enhance_checklist_with_qcc(checklist, qcc_data, todo_id):
    name = checklist.get("name", "")
    qcc = qcc_data.get(name, {})
    
    # A01 工商执照核实（既有）
    # A02 法人核实（既有）
    
    # A10 商业信誉扩展（关键新增）
    a10 = next((c for c in checklist["items"] if c["code"] == "A10"), None)
    if a10:
        # 失信被执行人
        dishonest = qcc.get("失信", [])
        if dishonest:
            a10["status"] = "FAIL"
            a10["evidence"] = f"失信被执行人 {len(dishonest)} 条"
        # 被执行人
        executed = qcc.get("被执行", [])
        if executed and a10["status"] != "FAIL":
            a10["status"] = "WARN"
            a10["evidence"] = f"被执行案件 {len(executed)} 条，金额合计 ..."
        # 限消令
        xiaofei = qcc.get("限消令", [])
        if xiaofei:
            a10["status"] = "FAIL"
            a10["evidence"] = f"限制消费令 {len(xiaofei)} 条"
        # 经营异常
        yichang = qcc.get("经营异常", [])
        if yichang and a10["status"] != "FAIL":
            a10["status"] = "WARN"
            a10["evidence"] = f"经营异常 {len(yichang)} 条"
        # 严重违法
        weifa = qcc.get("严重违法", [])
        if weifa:
            a10["status"] = "FAIL"
            a10["evidence"] = f"严重违法 {len(weifa)} 条"
    
    # A03 股东核实扩展（用 qcc_api_client 的 holders 数据补充）
    # ...
    
    # A11 围标串标（新增规则项，可选，需要多家供应商数据）
    # 这块用第二档的 shortPath 接口
    
    return checklist
```

### ④ 更新 `.env.example` 增加3行

```env
# 企查查 API 凭据（向公司IT申请或企查查开放平台注册）
QCC_APP_KEY=<your_qcc_app_key>
QCC_SECRET_KEY=<your_qcc_secret_key>
```

---

## 4. 工作量评估

| 模块 | 行数 | 工时 |
|---|---|---|
| qcc_api_client.py 新建（13个函数） | ~300 | 4-6小时 |
| qcc_results.json 结构扩展 + 兼容性测试 | - | 1小时 |
| enhance_checklist_with_qcc 扩展（A10 5块新增） | ~100 | 2-3小时 |
| .env.example 更新 | 3行 | 5分钟 |
| rules.yaml 加 A11 围标串标规则项 | ~20 | 1小时 |
| 端到端测试（用湖北科规或新供应商跑通） | - | 2小时 |
| **总计** | ~420行 | **10-13小时** |

---

## 5. 推荐动手时机（参赛倒计时 7 天）

参赛提交截止日（按 Quinn 9/4 启动7天倒计时算）：约 **9/10**

| 日期 | 推荐工作 |
|---|---|
| 9/4-9/5 | Quinn 申请 QCC AppKey/SecretKey（前提） |
| 9/5-9/6 | 我开始写 qcc_api_client.py（13个接口函数） |
| 9/6-9/7 | 扩展 enhance_checklist_with_qcc + 端到端测试 |
| 9/7-9/8 | cpolar 配置 + 演示视频录制 |
| 9/8-9/9 | 代码仓库整理 + 远程推送 |
| 9/9-9/10 | 最终检查 + 提交 |

**风险提示**：
1. 如果 QCC AppKey 9/5 申请不到，**降级方案**：用现有 qcc_results.json 缓存数据演示（WorkBuddy 连接器跑出来的旧数据），但 A10 商业信誉只能"工商登记状态"演示，失信/被执行等硬拦截项**演示不了**——评委可能扣分
2. **围标串标第二档（3个接口）建议放到参赛后续版本**——每对供应商需调1次 shortPath，13家78对调用配额压力很大，且需要"多家供应商同时分析"的架构改动，时间不够。第一档10个接口能确保 A10 完整跑通

---

## 6. Quinn 需要确认的5件事

1. **QCC AppKey/SecretKey 来源**：公司IT共享 还是 企查查开放平台个人注册？
2. **是否同意降级方案**：如果9/5拿不到 AppKey，用旧缓存数据演示？
3. **第二档围标串标放参赛后续版本OK吗**：第一档10个接口足以覆盖A10硬拦截项
4. **qcc_api_client.py 写完后是否立刻 commit**：还是等 Quinn 验证再 commit？
5. **演示用哪一家供应商**：湖北科规（已有 qcc_results 缓存）还是新供应商？
