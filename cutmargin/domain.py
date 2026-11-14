from collections import defaultdict
from decimal import Decimal


def operation_balances(catalog):
    balances = defaultdict(Decimal)
    for record in catalog.rows("flows"):
        sign = 1 if record["direction"] == "input" else -1
        balances[record["operation_id"]] += sign * Decimal(str(record["quantity"]))
    return {operation: str(value) for operation, value in balances.items()}


def overview(catalog):
    return {"阶段批次数": len(catalog.rows("lots")), "工序物料差": operation_balances(catalog), "销售净额": str(sum((Decimal(record["amount"]) for record in catalog.rows("sales")), Decimal(0)))}
