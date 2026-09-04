"""安全规则求值器:手写 AST 解释器,不使用 eval。

只允许以下操作:
  - 逻辑:and / or / not
  - 比较:== != < <= > >= in not_in is is_not
  - 常量:数字、字符串、布尔、None
  - 名称:仅 supplier
  - 下标:supplier['key'] 或对返回值继续取下标
  - 列表/元组字面量:[...] (...)
  - 属性:仅 supplier.get
  - 调用:仅 supplier.get(key) 或 supplier.get(key, default)

设计上彻底封死沙箱逃逸:`().__class__.__bases__[0].__subclasses__()` 这类
攻击需要属性访问或调用,本求值器对属性/调用的接收方严格限定为 supplier,
任何对其它对象取属性或调用都会抛错并返回 False。
"""

import ast


class RuleError(Exception):
    pass


def _eval(node, supplier):
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            for v in node.values:
                if not _eval(v, supplier):
                    return False
            return True
        else:
            for v in node.values:
                if _eval(v, supplier):
                    return True
            return False

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval(node.operand, supplier)

    if isinstance(node, ast.Compare):
        left = _eval(node.left, supplier)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, supplier)
            if isinstance(op, ast.Eq):
                ok = left == right
            elif isinstance(op, ast.NotEq):
                ok = left != right
            elif isinstance(op, ast.Lt):
                ok = left < right
            elif isinstance(op, ast.LtE):
                ok = left <= right
            elif isinstance(op, ast.Gt):
                ok = left > right
            elif isinstance(op, ast.GtE):
                ok = left >= right
            elif isinstance(op, ast.In):
                ok = left in right
            elif isinstance(op, ast.NotIn):
                ok = left not in right
            elif isinstance(op, ast.Is):
                ok = left is right
            elif isinstance(op, ast.IsNot):
                ok = left is not right
            else:
                raise RuleError("operator not allowed")
            if not ok:
                return False
            left = right
        return True

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        if node.id == "supplier":
            return supplier
        raise RuleError("only 'supplier' name allowed")

    if isinstance(node, ast.Subscript):
        obj = _eval(node.value, supplier)
        slc = node.slice
        if isinstance(slc, ast.Index):
            slc = slc.value
        key = _eval(slc, supplier)
        return obj[key]

    if isinstance(node, ast.List):
        return [_eval(e, supplier) for e in node.elts]

    if isinstance(node, ast.Tuple):
        return tuple(_eval(e, supplier) for e in node.elts)

    if isinstance(node, ast.Attribute):
        if (
            isinstance(node.value, ast.Name)
            and node.value.id == "supplier"
            and node.attr == "get"
        ):
            return supplier.get
        raise RuleError("attribute access not allowed")

    if isinstance(node, ast.Call):
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "supplier"
            and func.attr == "get"
        ):
            args = [_eval(a, supplier) for a in node.args]
            return supplier.get(*args)
        raise RuleError("only supplier.get(...) call allowed")

    raise RuleError(f"node {type(node).__name__} not allowed")


def safe_eval_rule(expr, supplier):
    """对 supplier 字典求值 expr,返回布尔值。非法/异常一律返回 False。"""
    try:
        tree = ast.parse(expr, mode="eval")
        return bool(_eval(tree.body, supplier))
    except Exception:
        return False
