"""Validated identities and exact payload confirmation for approval writes."""
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ApprovalIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    todo_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    bill_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    task_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    proc_inst_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    act_inst_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    business_bill_type: Literal["P0701", "P0702", "P0704"]
    oper_code: Literal[2, 3]


def approval_digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def verify_fresh_task(identity, fresh):
    expected = {"businessBillId": identity.bill_id, "taskId": identity.task_id,
                "procInstId": identity.proc_inst_id, "actInstId": identity.act_inst_id,
                "businessBillType": identity.business_bill_type}
    if str(fresh.get("id") or fresh.get("todoId") or "") != identity.todo_id:
        raise ValueError("待办身份不一致")
    if any(str(fresh.get(k, "")) != v for k, v in expected.items()):
        raise ValueError("待办、供应商业务单据或流程已变化，禁止回写")
    if fresh.get("workItemStatus") not in (0, "0"):
        raise ValueError("任务不处于待处理状态")
