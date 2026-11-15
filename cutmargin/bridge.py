"""收益桥分析。

在同一计量口径下闭合各批次的投入、产出、损耗与期末存货，沿批次与
混料关系精确转移成本（同一笔成本不重复计算），并列比较批准的共同
成本分摊口径，区分整批贡献、联产品分摊毛利与新增加工的可避免收益，
并按事先交代的约定分解毛利变动。分析供经营讨论，不直接给出停产或
继续加工的结论。
"""

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction

BASES = ("weight", "nrv")
COMPONENTS = ("joint_avoidable", "joint_committed", "separable_avoidable", "separable_committed")
_CENT = Decimal("0.01")
_FOUR = Decimal("0.0001")

CONVENTIONS = (
    "采购批次期初量按该批全部投入量确认；阶段批次期末存货 = 期初 + 产出 − 投入 − 净销售。",
    "工序损耗的成本由同工序合格产出吸收，损耗记录不二次扣减批次实物。",
    "批次单位成本在产出时确定后保持不变；领用、销售与退货均按该单位成本计价。",
    "退货冲减原销售批次的收入与已售成本，退回实物按原单位成本回到原阶段库存，不压到退货发生期的出成。",
    "收入与已售成本按批次归属期归集；按销售日确认的期间口径另列对照，差异单列表述。",
    "共同成本为采购成本与多产出工序（分切）的全部成本；单产出工序成本为可分离成本；可避免与已承诺属性来自成本记录，与分摊口径无关。",
    "批准的两种分摊口径并列展示：重量（产出公斤占比）与可变现价值（产出量 × 该批次已实现净售价占比）；无已实现售价的批次不参与可变现价值口径。",
    "全部金额以有理数精确计算，仅展示时保留两位小数；各工序分摊合计恒等于被分摊成本，混料只转移成本、不新增也不重复。",
    "毛利变动分解采用 Shapley 全排列平均，交互项均分；敏感性为单因素顺序区间，其余因素保持中值。",
    "新增加工的可避免收益中，投入机会价值按该批次同期直接售价估计，未售产出按已实现售价估值并单独标注；结果供讨论，不构成继续或停止加工的决定。",
)


def _frac(value):
    return Fraction(str(value))


def _decimal(value):
    return Decimal(value.numerator) / Decimal(value.denominator)


def _money(value):
    return str(_decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP))


def _unit(value):
    return str(_decimal(value).quantize(_FOUR, rounding=ROUND_HALF_UP))


def _qty(value):
    return round(float(value), 4)


def _share(value):
    return round(float(value), 6)


class _Model:
    """源表的只读整理：批次、工序、物料流、成本、销售及其派生数量。"""

    def __init__(self, catalog):
        self.lots = {row["id"]: row for row in catalog.rows("lots")}
        self.operations = {row["id"]: row for row in catalog.rows("operations")}
        self.flows = catalog.rows("flows")
        self.costs = catalog.rows("costs")
        self.sales = catalog.rows("sales")
        self.op_flows = defaultdict(list)
        self.lot_flows = defaultdict(list)
        for flow in self.flows:
            self.op_flows[flow["operation_id"]].append(flow)
            self.lot_flows[flow["lot_id"]].append(flow)
        self.lot_sales = defaultdict(list)
        for row in self.sales:
            self.lot_sales[row["lot_id"]].append(row)
        self.produced = defaultdict(Fraction)
        self.consumed = defaultdict(Fraction)
        self.lost = defaultdict(Fraction)
        for flow in self.flows:
            quantity = _frac(flow["quantity"])
            if flow["direction"] == "output":
                self.produced[flow["lot_id"]] += quantity
            elif flow["direction"] == "input":
                self.consumed[flow["lot_id"]] += quantity
            elif flow["direction"] == "loss":
                self.lost[flow["lot_id"]] += quantity
            else:
                raise ValueError("未知物料方向：" + str(flow["direction"]))
        self.sold_net = defaultdict(Fraction)
        self.revenue_net = defaultdict(Fraction)
        for row in self.sales:
            self.sold_net[row["lot_id"]] += _frac(row["quantity"])
            self.revenue_net[row["lot_id"]] += _frac(row["amount"])
        self.beginning = {lot_id: (self.consumed[lot_id] if self.produced[lot_id] == 0 else Fraction(0))
                          for lot_id in self.lots}
        self.ending = {lot_id: self.beginning[lot_id] + self.produced[lot_id]
                       - self.consumed[lot_id] - self.sold_net[lot_id]
                       for lot_id in self.lots}
        self.ordered_ops = sorted(self.operations.values(),
                                  key=lambda op: (op["day"], op["sequence"], op["id"]))


def _prices(model):
    """各批次已实现净售价（退货已冲减）；无销售批次为 None。"""
    prices = {}
    for lot_id in model.lots:
        qty = model.sold_net[lot_id]
        prices[lot_id] = model.revenue_net[lot_id] / qty if qty > 0 else None
    return prices


def _classify_costs(model):
    """把每笔成本归入共同/可分离 × 可避免/已承诺四个成分。"""
    multi_output = set()
    for op_id, flows in model.op_flows.items():
        outputs = {flow["lot_id"] for flow in flows if flow["direction"] == "output"}
        if len(outputs) > 1:
            multi_output.add(op_id)
    joint_lots = set()
    for flow in model.flows:
        if flow["direction"] == "input" and flow["operation_id"] in multi_output:
            joint_lots.add(flow["lot_id"])
    classified = []
    for cost in model.costs:
        if cost["lot_id"] is not None:
            scope = "joint" if cost["lot_id"] in joint_lots else "separable"
        else:
            scope = "joint" if cost["operation_id"] in multi_output else "separable"
        component = scope + ("_avoidable" if cost["avoidable"] else "_committed")
        classified.append((cost, component, _frac(cost["amount"])))
    return classified, multi_output


def _shares(basis, out_lots, prices, op):
    if len(out_lots) == 1:
        return [Fraction(1)]
    if basis == "weight":
        total = sum((quantity for _, quantity in out_lots), Fraction(0))
        return [quantity / total for _, quantity in out_lots]
    weights = []
    for lot_id, quantity in out_lots:
        price = prices[lot_id]
        if price is None:
            raise ValueError(f"可变现价值口径缺少批次 {lot_id} 的已实现售价（工序 {op['id']}）")
        weights.append(quantity * price)
    total = sum(weights, Fraction(0))
    return [weight / total for weight in weights]


def _cost_flow(model, basis, prices, classified):
    """按口径沿批次与混料关系转移成本；每个池子只减不增地流向领用、销售与存货。"""
    pool = {lot_id: dict.fromkeys(COMPONENTS, Fraction(0)) for lot_id in model.lots}
    unit = {lot_id: dict.fromkeys(COMPONENTS, Fraction(0)) for lot_id in model.lots}
    available = {lot_id: model.beginning[lot_id] for lot_id in model.lots}
    direct = dict.fromkeys(model.lots, Fraction(0))
    received = dict.fromkeys(model.lots, Fraction(0))
    own_costs = defaultdict(lambda: dict.fromkeys(COMPONENTS, Fraction(0)))
    for cost, component, amount in classified:
        if cost["lot_id"] is not None:
            pool[cost["lot_id"]][component] += amount
            direct[cost["lot_id"]] += amount
        else:
            own_costs[cost["operation_id"]][component] += amount
    for lot_id in model.lots:
        if available[lot_id] > 0:
            for component in COMPONENTS:
                unit[lot_id][component] = pool[lot_id][component] / available[lot_id]
    ops_exact = {}
    for op in model.ordered_ops:
        flows = model.op_flows[op["id"]]
        inputs = [flow for flow in flows if flow["direction"] == "input"]
        outputs = [flow for flow in flows if flow["direction"] == "output"]
        loss_qty = sum((_frac(flow["quantity"]) for flow in flows if flow["direction"] == "loss"), Fraction(0))
        drawn = dict.fromkeys(COMPONENTS, Fraction(0))
        drawn_rows = []
        for flow in inputs:
            lot_id = flow["lot_id"]
            quantity = _frac(flow["quantity"])
            amount = Fraction(0)
            for component in COMPONENTS:
                part = unit[lot_id][component] * quantity
                pool[lot_id][component] -= part
                drawn[component] += part
                amount += part
            available[lot_id] -= quantity
            drawn_rows.append((lot_id, quantity, amount))
        own = own_costs[op["id"]]
        total = {component: drawn[component] + own[component] for component in COMPONENTS}
        out_lots = [(flow["lot_id"], _frac(flow["quantity"])) for flow in outputs]
        shares = _shares(basis, out_lots, prices, op)
        distributed_rows = []
        for (lot_id, quantity), share in zip(out_lots, shares):
            amount = Fraction(0)
            for component in COMPONENTS:
                part = total[component] * share
                pool[lot_id][component] += part
                amount += part
            available[lot_id] += quantity
            received[lot_id] += amount
            for component in COMPONENTS:
                unit[lot_id][component] = pool[lot_id][component] / available[lot_id]
            distributed_rows.append((lot_id, quantity, share, amount))
        ops_exact[op["id"]] = {
            "drawn": drawn_rows, "loss": loss_qty,
            "own_total": sum(own.values()),
            "own_avoidable": own["joint_avoidable"] + own["separable_avoidable"],
            "own_committed": own["joint_committed"] + own["separable_committed"],
            "distributed": distributed_rows, "total": sum(total.values()),
        }
    cogs = {lot_id: dict.fromkeys(COMPONENTS, Fraction(0)) for lot_id in model.lots}
    for row in model.sales:
        lot_id = row["lot_id"]
        quantity = _frac(row["quantity"])
        for component in COMPONENTS:
            part = unit[lot_id][component] * quantity
            cogs[lot_id][component] += part
            pool[lot_id][component] -= part
        available[lot_id] -= quantity
    lots_exact = {}
    for lot_id in model.lots:
        comp = cogs[lot_id]
        total_cogs = sum(comp.values())
        lots_exact[lot_id] = {
            "direct": direct[lot_id], "received": received[lot_id],
            "unit": sum(unit[lot_id].values()), "cogs_total": total_cogs,
            "cogs_avoidable": comp["joint_avoidable"] + comp["separable_avoidable"],
            "cogs_joint": comp["joint_avoidable"] + comp["joint_committed"],
            "ending_value": sum(pool[lot_id].values()),
        }
    total_costs = sum((amount for _, _, amount in classified), Fraction(0))
    total_cogs = sum((lot["cogs_total"] for lot in lots_exact.values()), Fraction(0))
    total_inventory = sum((lot["ending_value"] for lot in lots_exact.values()), Fraction(0))
    return {
        "lots": lots_exact, "ops": ops_exact,
        "total_costs": total_costs, "total_cogs": total_cogs, "total_inventory": total_inventory,
        "conserved": total_costs == total_cogs + total_inventory,
    }


def _closure(model):
    op_rows = []
    all_closed = True
    for op in model.ordered_ops:
        flows = model.op_flows[op["id"]]
        ins = sum((_frac(f["quantity"]) for f in flows if f["direction"] == "input"), Fraction(0))
        outs = sum((_frac(f["quantity"]) for f in flows if f["direction"] == "output"), Fraction(0))
        loss = sum((_frac(f["quantity"]) for f in flows if f["direction"] == "loss"), Fraction(0))
        balance = ins - outs - loss
        closed = balance == 0
        all_closed = all_closed and closed
        op_rows.append({"operation_id": op["id"], "kind": op["kind"], "day": op["day"],
                        "input": _qty(ins), "output": _qty(outs), "loss": _qty(loss),
                        "balance": _qty(balance), "closed": closed})
    lot_rows = []
    for lot_id in sorted(model.lots):
        lot = model.lots[lot_id]
        ending = model.ending[lot_id]
        closed = ending >= 0
        all_closed = all_closed and closed
        lot_rows.append({"lot_id": lot_id, "product": lot["product"], "period": lot["period"],
                         "beginning": _qty(model.beginning[lot_id]), "produced": _qty(model.produced[lot_id]),
                         "consumed": _qty(model.consumed[lot_id]), "sold_net": _qty(model.sold_net[lot_id]),
                         "loss_in_operations": _qty(model.lost[lot_id]),
                         "ending": _qty(ending), "closed": closed})
    totals = {
        "input": _qty(sum((row["input"] for row in op_rows), Fraction(0))) if op_rows else 0.0,
        "ending_inventory": _qty(sum((model.ending[lot_id] for lot_id in model.lots), Fraction(0))),
    }
    return {"operations": op_rows, "lots": lot_rows, "all_closed": all_closed, "totals": totals,
            "note": "损耗为工序损失备忘，已含于工序平衡（投入 = 产出 + 损耗），不再扣减批次实物。"}


def _families(model):
    """从采购批次沿物料流追溯族系；被多个族系共享的批次（如混料产出）单独标记。"""
    children = defaultdict(set)
    for op_id, flows in model.op_flows.items():
        ins = {flow["lot_id"] for flow in flows if flow["direction"] == "input"}
        outs = {flow["lot_id"] for flow in flows if flow["direction"] == "output"}
        for source in ins:
            children[source] |= outs
    roots = sorted(lot_id for lot_id in model.lots if model.produced[lot_id] == 0)
    family_of = {}
    shared = set()
    for root in roots:
        family_of[root] = root
        stack = [root]
        while stack:
            current = stack.pop()
            for desc in sorted(children[current]):
                owner = family_of.get(desc)
                if owner is None:
                    family_of[desc] = root
                    stack.append(desc)
                elif owner != root:
                    shared.add(desc)
    op_family = {}
    for op_id, flows in model.op_flows.items():
        owners = {family_of.get(flow["lot_id"]) for flow in flows if flow["direction"] == "input"}
        owners.discard(None)
        op_family[op_id] = owners.pop() if len(owners) == 1 else None
    return family_of, shared, op_family, roots


def _batch_contribution(model, family_of, shared, op_family, roots):
    """整批贡献：不做任何共同成本分摊，收入与可避免、已承诺成本按批次归属期归集。"""
    rows = []
    totals = defaultdict(lambda: dict.fromkeys(("revenue", "avoidable", "committed"), Fraction(0)))
    for root in roots:
        members = sorted(lot_id for lot_id, owner in family_of.items()
                         if owner == root and lot_id not in shared)
        period = model.lots[root]["period"]
        revenue = sum((model.revenue_net[m] for m in members), Fraction(0))
        avoidable = committed = Fraction(0)
        for cost in model.costs:
            if cost["lot_id"] is not None:
                in_family = family_of.get(cost["lot_id"]) == root and cost["lot_id"] not in shared
            else:
                in_family = op_family.get(cost["operation_id"]) == root
            if not in_family:
                continue
            if cost["avoidable"]:
                avoidable += _frac(cost["amount"])
            else:
                committed += _frac(cost["amount"])
        cross = sum((_frac(s["amount"]) for m in members for s in model.lot_sales[m]
                     if s["day"][:7] != period), Fraction(0))
        returned = sum((_frac(s["amount"]) for m in members for s in model.lot_sales[m]
                        if s["return_of"]), Fraction(0))
        unsold = {model.lots[m]["product"]: _qty(model.ending[m])
                  for m in members if model.ending[m] > 0}
        contribution = revenue - avoidable
        rows.append({"batch": root, "period": period, "lots": members,
                     "revenue_net": _money(revenue),
                     "avoidable_costs": _money(avoidable), "committed_costs": _money(committed),
                     "contribution": _money(contribution),
                     "margin_after_committed": _money(contribution - committed),
                     "cross_period_revenue": _money(cross), "returns_amount": _money(returned),
                     "unsold_quantity": unsold})
        bucket = totals[period]
        bucket["revenue"] += revenue
        bucket["avoidable"] += avoidable
        bucket["committed"] += committed
    total_rows = []
    for period in sorted(totals):
        bucket = totals[period]
        contribution = bucket["revenue"] - bucket["avoidable"]
        total_rows.append({"period": period,
                           "revenue_net": _money(bucket["revenue"]),
                           "avoidable_costs": _money(bucket["avoidable"]),
                           "committed_costs": _money(bucket["committed"]),
                           "contribution": _money(contribution),
                           "margin_after_committed": _money(contribution - bucket["committed"])})
    shared_rows = [{"lot_id": lot_id, "product": model.lots[lot_id]["product"],
                    "period": model.lots[lot_id]["period"],
                    "ending_quantity": _qty(model.ending[lot_id])}
                   for lot_id in sorted(shared)]
    return {"families": rows, "period_totals": total_rows, "shared_lots": shared_rows,
            "note": "整批贡献不做共同成本分摊；跨批混料批次的成本来自多个原料批，不单独归属某一族系。"
                    "未售存货数量单列，不计入收入。"}


def _joint_rows(model, flow):
    """联产品分摊毛利：每批次在某口径下的分摊后毛利，保留分摊前后金额。"""
    rows = []
    for lot_id in sorted(model.lots):
        exact = flow["lots"][lot_id]
        revenue = model.revenue_net[lot_id]
        if revenue == 0 and exact["cogs_total"] == 0 and exact["ending_value"] == 0:
            continue
        lot = model.lots[lot_id]
        rows.append({"lot_id": lot_id, "product": lot["product"], "period": lot["period"],
                     "revenue_net": _money(revenue),
                     "direct_costs": _money(exact["direct"]),
                     "transferred_in": _money(exact["received"]),
                     "unit_cost": _unit(exact["unit"]),
                     "cogs": _money(exact["cogs_total"]),
                     "cogs_avoidable": _money(exact["cogs_avoidable"]),
                     "cogs_committed": _money(exact["cogs_total"] - exact["cogs_avoidable"]),
                     "cogs_joint": _money(exact["cogs_joint"]),
                     "cogs_separable": _money(exact["cogs_total"] - exact["cogs_joint"]),
                     "allocated_margin": _money(revenue - exact["cogs_total"]),
                     "avoidable_margin": _money(revenue - exact["cogs_avoidable"]),
                     "ending_quantity": _qty(model.ending[lot_id]),
                     "ending_value": _money(exact["ending_value"])})
    return rows


def _further_processing(model, prices, flows_by_basis):
    """新增加工的可避免收益：继续加工相对直接出售的增量对比，供讨论。"""
    rework = []
    mixing = []
    for op in model.ordered_ops:
        flows = model.op_flows[op["id"]]
        inputs = [(f["lot_id"], _frac(f["quantity"])) for f in flows if f["direction"] == "input"]
        outputs = [(f["lot_id"], _frac(f["quantity"])) for f in flows if f["direction"] == "output"]
        if not inputs or not outputs:
            continue
        own_avoidable = sum((_frac(c["amount"]) for c in model.costs
                             if c["operation_id"] == op["id"] and c["avoidable"]), Fraction(0))
        own_committed = sum((_frac(c["amount"]) for c in model.costs
                             if c["operation_id"] == op["id"] and not c["avoidable"]), Fraction(0))
        if len(inputs) == 1 and len(outputs) == 1 and model.produced[inputs[0][0]] > 0:
            in_lot, q_in = inputs[0]
            out_lot, q_out = outputs[0]
            p_in = prices[in_lot]
            p_out = prices[out_lot]
            sold_qty = model.sold_net[out_lot]
            sold_amount = model.revenue_net[out_lot]
            unsold = q_out - sold_qty
            row = {"operation_id": op["id"], "kind": op["kind"],
                   "input_lot": in_lot, "input_quantity": _qty(q_in),
                   "output_lot": out_lot, "output_quantity": _qty(q_out),
                   "output_sold_quantity": _qty(sold_qty),
                   "output_revenue": _money(sold_amount),
                   "output_unsold_quantity": _qty(unsold),
                   "incremental_avoidable_cost": _money(own_avoidable),
                   "incremental_committed_cost": _money(own_committed)}
            opportunity = q_in * p_in if p_in is not None else None
            if opportunity is not None:
                realized = sold_amount - opportunity - own_avoidable
                row["input_opportunity_unit_price"] = _unit(p_in)
                row["input_opportunity_value"] = _money(opportunity)
                row["avoidable_benefit_realized"] = _money(realized)
                if p_out is not None:
                    estimate = unsold * p_out
                    row["output_unsold_estimate"] = _money(estimate)
                    row["avoidable_benefit_with_inventory"] = _money(realized + estimate)
                    row["breakeven_output_price"] = _unit((opportunity + own_avoidable) / q_out)
                    sensitivity = []
                    for driver in ("产出售价", "投入机会价值"):
                        values = []
                        for mult in (Fraction(9, 10), Fraction(1), Fraction(11, 10)):
                            if driver == "产出售价":
                                benefit = q_out * p_out * mult - opportunity - own_avoidable
                                level = _unit(p_out * mult)
                            else:
                                benefit = q_out * p_out - opportunity * mult - own_avoidable
                                level = _unit(p_in * mult)
                            values.append({"level": level, "benefit": _money(benefit)})
                        sensitivity.append({"driver": driver, "values": values})
                    row["sensitivity"] = sensitivity
            else:
                row["avoidable_benefit_realized"] = None
                row["note"] = "投入批次无直接售价，机会价值无法估计。"
            rework.append(row)
        elif len(inputs) > 1:
            out_lot, q_out = outputs[0]
            opportunity = sum((quantity * prices[lot_id] for lot_id, quantity in inputs
                               if prices[lot_id] is not None), Fraction(0))
            mixing.append({"operation_id": op["id"], "kind": op["kind"],
                           "inputs": [{"lot_id": lot_id, "quantity": _qty(quantity)}
                                      for lot_id, quantity in inputs],
                           "output_lot": out_lot, "output_quantity": _qty(q_out),
                           "input_opportunity_value": _money(opportunity),
                           "drawn_cost_by_basis": {basis: _money(flows_by_basis[basis]["ops"][op["id"]]["total"])
                                                   for basis in BASES},
                           "output_revenue": _money(model.revenue_net[out_lot]),
                           "output_unsold_quantity": _qty(model.ending[out_lot]),
                           "avoidable_benefit_realized": None,
                           "note": "混合产出尚无销售记录，可变现收益无法估计；存货按转入成本列示，供讨论。"})
    return {"rework": rework, "mixing": mixing,
            "note": "可避免收益为负仅表示按既定口径继续加工不抵直接出售；是否继续加工还需讨论品质分流、"
                    "客户承诺等资料外因素，本分析不直接给出停产结论。"}


def _timing(model):
    """退货与跨期销售：批次归属口径与销售日确认口径的对照。"""
    by_sale_date = defaultdict(Fraction)
    by_batch = defaultdict(Fraction)
    cross = []
    returns = []
    for row in model.sales:
        amount = _frac(row["amount"])
        sale_period = row["day"][:7]
        lot_period = model.lots[row["lot_id"]]["period"]
        by_sale_date[sale_period] += amount
        by_batch[lot_period] += amount
        if sale_period != lot_period:
            cross.append({"sale_id": row["id"], "lot_id": row["lot_id"], "day": row["day"],
                          "amount": _money(amount), "sale_period": sale_period, "lot_period": lot_period})
        if row["return_of"]:
            returns.append({"sale_id": row["id"], "return_of": row["return_of"], "lot_id": row["lot_id"],
                            "day": row["day"], "amount": _money(amount), "attributed_period": lot_period})
    periods = sorted(set(by_sale_date) | set(by_batch))
    rows = [{"period": period,
             "recognized_by_sale_date": _money(by_sale_date[period]),
             "attributed_by_batch": _money(by_batch[period]),
             "difference": _money(by_batch[period] - by_sale_date[period])}
            for period in periods]
    return {"periods": rows, "cross_period_sales": cross, "returns": returns,
            "note": "跨期销售与退货按批次归属期归集，不全部压给销售发生期的出成与毛利。"}


def _effect(factor, amount, lo=None, hi=None, note=None):
    lo = amount if lo is None else lo
    hi = amount if hi is None else hi
    lo, hi = min(lo, hi), max(lo, hi)
    row = {"factor": factor, "amount": _money(amount), "range": [_money(lo), _money(hi)]}
    if note:
        row["note"] = note
    return row


def _margin_bridge(model, flow, basis, periods, products):
    """毛利变动分解：售价、单位成本、规模、出成、售出率、产品组合，Shapley 约定。"""
    p_from, p_to = periods
    stats = {}
    for product in products:
        for period in periods:
            members = [lot_id for lot_id, lot in model.lots.items()
                       if lot["product"] == product and lot["period"] == period]
            stats[product, period] = {
                "qty": sum((model.sold_net[m] for m in members), Fraction(0)),
                "revenue": sum((model.revenue_net[m] for m in members), Fraction(0)),
                "cogs": sum((flow["lots"][m]["cogs_total"] for m in members), Fraction(0)),
                "produced": sum((model.produced[m] for m in members), Fraction(0)),
            }
    batches = {period: sum(1 for lot_id, lot in model.lots.items()
                           if lot["period"] == period and model.produced[lot_id] == 0)
               for period in periods}
    margins = {period: sum((stats[product, period]["revenue"] - stats[product, period]["cogs"]
                            for product in products), Fraction(0))
               for period in periods}
    delta = margins[p_to] - margins[p_from]
    notes = []
    per_product = {}
    price_effect = price_lo = price_hi = Fraction(0)
    cost_effect = cost_lo = cost_hi = Fraction(0)
    for product in products:
        s0 = stats[product, p_from]
        s1 = stats[product, p_to]
        q0, q1 = s0["qty"], s1["qty"]
        if q0 == 0 and q1 == 0:
            continue
        if q0 > 0 and q1 > 0:
            price0, price1 = s0["revenue"] / q0, s1["revenue"] / q1
            uc0, uc1 = s0["cogs"] / q0, s1["cogs"] / q1
        else:
            known = s1 if q1 > 0 else s0
            known_qty = known["qty"]
            price0 = price1 = known["revenue"] / known_qty
            uc0 = uc1 = known["cogs"] / known_qty
            notes.append(f"产品 {product} 在其中一期无销售，其毛利变动全部计入数量因素。")
        m0, m1 = price0 - uc0, price1 - uc1
        entry = {"m_avg": (m0 + m1) / 2, "q_avg": (q0 + q1) / 2, "d_q": q1 - q0,
                 "y0": s0["produced"] / batches[p_from] if batches[p_from] else Fraction(0),
                 "y1": s1["produced"] / batches[p_to] if batches[p_to] else Fraction(0),
                 "sell0": q0 / s0["produced"] if s0["produced"] > 0 else Fraction(0),
                 "sell1": q1 / s1["produced"] if s1["produced"] > 0 else Fraction(0),
                 "price0": price0, "price1": price1, "uc0": uc0, "uc1": uc1}
        per_product[product] = entry
        d_price = price1 - price0
        d_uc = uc1 - uc0
        price_effect += d_price * entry["q_avg"]
        price_lo += d_price * q0
        price_hi += d_price * q1
        cost_effect += -d_uc * entry["q_avg"]
        cost_lo += -d_uc * q0
        cost_hi += -d_uc * q1
    q_tot = {period: sum((stats[product, period]["qty"] for product in per_product), Fraction(0))
             for period in periods}
    q_avg_tot = (q_tot[p_from] + q_tot[p_to]) / 2
    m_blend = (sum((entry["m_avg"] * entry["q_avg"] for entry in per_product.values()), Fraction(0)) / q_avg_tot
               if q_avg_tot > 0 else Fraction(0))
    mix_effect = sum((entry["d_q"] * (entry["m_avg"] - m_blend) for entry in per_product.values()), Fraction(0))
    g = {period: sum((entry[f"y{index}"] * entry[f"sell{index}"] for entry in per_product.values()), Fraction(0))
         for period, index in ((p_from, "0"), (p_to, "1"))}
    b0, b1 = Fraction(batches[p_from]), Fraction(batches[p_to])
    b_avg = (b0 + b1) / 2
    scale_effect = (b1 - b0) * (g[p_from] + g[p_to]) / 2 * m_blend
    scale_lo = (b1 - b0) * g[p_from] * m_blend
    scale_hi = (b1 - b0) * g[p_to] * m_blend
    yield_base = sum(((entry["y1"] - entry["y0"]) * (entry["sell0"] + entry["sell1"]) / 2
                      for entry in per_product.values()), Fraction(0))
    yield_lo_base = sum(((entry["y1"] - entry["y0"]) * entry["sell0"]
                         for entry in per_product.values()), Fraction(0))
    yield_hi_base = sum(((entry["y1"] - entry["y0"]) * entry["sell1"]
                         for entry in per_product.values()), Fraction(0))
    sell_base = sum(((entry["y0"] + entry["y1"]) / 2 * (entry["sell1"] - entry["sell0"])
                     for entry in per_product.values()), Fraction(0))
    sell_lo_base = sum((entry["y0"] * (entry["sell1"] - entry["sell0"])
                        for entry in per_product.values()), Fraction(0))
    sell_hi_base = sum((entry["y1"] * (entry["sell1"] - entry["sell0"])
                        for entry in per_product.values()), Fraction(0))
    effects = [
        _effect("售价", price_effect, price_lo, price_hi),
        _effect("单位成本", cost_effect, cost_lo, cost_hi),
        _effect("规模（批次数）", scale_effect, scale_lo, scale_hi,
                note=f"批次数 {batches[p_from]} → {batches[p_to]}"),
        _effect("出成", yield_base * b_avg * m_blend,
                yield_lo_base * b_avg * m_blend, yield_hi_base * b_avg * m_blend),
        _effect("售出率（含退货）", sell_base * b_avg * m_blend,
                sell_lo_base * b_avg * m_blend, sell_hi_base * b_avg * m_blend,
                note="退货冲减原批次售出率，不计入出成"),
        _effect("产品组合", mix_effect),
    ]
    exact_sum = price_effect + cost_effect + scale_effect + yield_base * b_avg * m_blend \
        + sell_base * b_avg * m_blend + mix_effect
    product_stats = []
    for product in per_product:
        entry = per_product[product]
        for period, index in ((p_from, "0"), (p_to, "1")):
            stat = stats[product, period]
            product_stats.append({
                "product": product, "period": period,
                "quantity_sold": _qty(stat["qty"]), "revenue": _money(stat["revenue"]),
                "cogs": _money(stat["cogs"]),
                "unit_price": _unit(entry[f"price{index}"]) if stat["qty"] > 0 else None,
                "unit_cogs": _unit(entry[f"uc{index}"]) if stat["qty"] > 0 else None,
                "yield_per_batch": _qty(entry[f"y{index}"]),
                "sell_through": _share(entry[f"sell{index}"])})
    return {"basis": basis, "periods": [p_from, p_to],
            "margin_by_period": {period: _money(margins[period]) for period in periods},
            "delta": _money(delta), "effects": effects,
            "effects_sum": _money(exact_sum), "reconciled": exact_sum == delta,
            "product_stats": product_stats,
            "notes": notes + ["收益桥分解已售口径毛利（批次归属收入 − 已售成本）；未售存货按成本列示于成本流转，不计入毛利。",
                              "交互项按 Shapley 全排列平均分摊；range 为单因素顺序区间，其余因素保持中值。"]}


def _allocation_choice(model, flows_by_basis, periods, products):
    """分摊选择的影响：各产品分摊毛利在两口径间的摆动，及期末存货估值差。"""
    rows = []
    margin_totals = {period: dict.fromkeys(BASES, Fraction(0)) for period in periods}
    for product in products:
        for period in periods:
            members = [lot_id for lot_id, lot in model.lots.items()
                       if lot["product"] == product and lot["period"] == period]
            revenue = sum((model.revenue_net[m] for m in members), Fraction(0))
            margins = {}
            for candidate in BASES:
                cogs = sum((flows_by_basis[candidate]["lots"][m]["cogs_total"] for m in members), Fraction(0))
                margins[candidate] = revenue - cogs
                margin_totals[period][candidate] += margins[candidate]
            rows.append({"product": product, "period": period, "revenue_net": _money(revenue),
                         "margin_weight": _money(margins["weight"]), "margin_nrv": _money(margins["nrv"]),
                         "swing": _money(margins["nrv"] - margins["weight"])})
    inventory_value = {}
    for period in periods:
        inventory_value[period] = {}
        for candidate in BASES:
            value = sum((flows_by_basis[candidate]["lots"][lot_id]["ending_value"]
                         for lot_id, lot in model.lots.items() if lot["period"] == period), Fraction(0))
            inventory_value[period][candidate] = _money(value)
    return {"per_product": rows,
            "margin_totals": {period: {candidate: _money(margin_totals[period][candidate])
                                       for candidate in BASES}
                              for period in periods},
            "inventory_value": inventory_value,
            "note": "分摊口径在产品间与期末存货间重新分配成本：各产品分摊毛利随之摆动，两口径的合计差异"
                    "等于期末存货估值差。跨期混料会把成本带入另一期间的存货（如 MIXED 计入 2026-10），"
                    "因此单期摆动与单期存货差可能相差该转移额，全期合计仍恒等。"
                    "分摊选择不改变整批贡献，不应单独作为停产依据。"}


def _format_cost_flow(model, flow):
    lot_rows = []
    for lot_id in sorted(model.lots):
        exact = flow["lots"][lot_id]
        lot = model.lots[lot_id]
        lot_rows.append({"lot_id": lot_id, "product": lot["product"], "period": lot["period"],
                         "direct_costs": _money(exact["direct"]),
                         "transferred_in": _money(exact["received"]),
                         "unit_cost": _unit(exact["unit"]),
                         "sold_quantity": _qty(model.sold_net[lot_id]),
                         "cogs": _money(exact["cogs_total"]),
                         "cogs_avoidable": _money(exact["cogs_avoidable"]),
                         "cogs_committed": _money(exact["cogs_total"] - exact["cogs_avoidable"]),
                         "cogs_joint": _money(exact["cogs_joint"]),
                         "cogs_separable": _money(exact["cogs_total"] - exact["cogs_joint"]),
                         "ending_quantity": _qty(model.ending[lot_id]),
                         "ending_value": _money(exact["ending_value"])})
    op_rows = []
    for op in model.ordered_ops:
        exact = flow["ops"][op["id"]]
        op_rows.append({"operation_id": op["id"], "kind": op["kind"], "day": op["day"],
                        "drawn_from_inputs": [{"lot_id": lot_id, "quantity": _qty(quantity),
                                               "amount": _money(amount)}
                                              for lot_id, quantity, amount in exact["drawn"]],
                        "own_costs": _money(exact["own_total"]),
                        "own_costs_avoidable": _money(exact["own_avoidable"]),
                        "own_costs_committed": _money(exact["own_committed"]),
                        "loss_quantity": _qty(exact["loss"]),
                        "distributed_to_outputs": [{"lot_id": lot_id, "quantity": _qty(quantity),
                                                    "share": _share(share), "amount": _money(amount)}
                                                   for lot_id, quantity, share, amount in exact["distributed"]],
                        "total_distributed": _money(exact["total"])})
    return {"lots": lot_rows, "operations": op_rows,
            "conservation": {"total_costs": _money(flow["total_costs"]),
                             "cogs": _money(flow["total_cogs"]),
                             "ending_inventory": _money(flow["total_inventory"]),
                             "conserved": flow["conserved"],
                             "note": "总成本恒等于已售成本加期末存货；混料只转移成本，同一笔成本不重复计算。"}}


def bridge_analysis(catalog, basis="weight", period_from=None, period_to=None):
    """完整收益桥分析：闭合、成本流转、三种收益视图、毛利变动分解与分摊选择敏感性。"""
    if basis not in BASES:
        raise ValueError("未知分摊口径：" + str(basis))
    model = _Model(catalog)
    periods = sorted({lot["period"] for lot in model.lots.values()})
    if (period_from is None) != (period_to is None):
        raise ValueError("期间需同时给出起止")
    if period_from is None:
        if len(periods) < 2:
            raise ValueError("不足两个期间，无法比较")
        period_from, period_to = periods[0], periods[1]
    if period_from not in periods or period_to not in periods:
        raise ValueError("期间不存在于资料中")
    if period_from >= period_to:
        raise ValueError("期间顺序无效")
    prices = _prices(model)
    classified, _ = _classify_costs(model)
    flows_by_basis = {candidate: _cost_flow(model, candidate, prices, classified)
                      for candidate in BASES}
    family_of, shared, op_family, roots = _families(model)
    products = sorted({lot["product"] for lot in model.lots.values()
                       if model.sold_net[lot["id"]] > 0})
    compare_periods = (period_from, period_to)
    return {
        "conventions": list(CONVENTIONS),
        "parameters": {"reference_basis": basis, "bases_compared": list(BASES),
                       "periods": [period_from, period_to]},
        "closure": _closure(model),
        "cost_flow": {candidate: _format_cost_flow(model, flows_by_basis[candidate])
                      for candidate in BASES},
        "views": {
            "batch_contribution": _batch_contribution(model, family_of, shared, op_family, roots),
            "joint_product_margin": {
                "rows_by_basis": {candidate: _joint_rows(model, flows_by_basis[candidate])
                                  for candidate in BASES},
                "note": "分摊毛利随口径变动；可避免毛利不依赖口径。原料批次成本已全额转入分切工序，零值行不列示。"},
            "further_processing": _further_processing(model, prices, flows_by_basis),
        },
        "timing": _timing(model),
        "margin_bridge": _margin_bridge(model, flows_by_basis[basis], basis, compare_periods, products),
        "allocation_choice": _allocation_choice(model, flows_by_basis, compare_periods, products),
    }


def lot_bridge(catalog, lot_id):
    """单批次核对视图：闭合、成本流转、分摊毛利与物料证据，可核对回原料批次。"""
    model = _Model(catalog)
    if lot_id not in model.lots:
        raise KeyError(lot_id)
    prices = _prices(model)
    classified, _ = _classify_costs(model)
    flows_by_basis = {candidate: _cost_flow(model, candidate, prices, classified)
                      for candidate in BASES}
    family_of, shared, _, _ = _families(model)
    lot = model.lots[lot_id]
    closure_row = next(row for row in _closure(model)["lots"] if row["lot_id"] == lot_id)
    cost_rows = {}
    margin_rows = {}
    for candidate in BASES:
        exact = flows_by_basis[candidate]["lots"][lot_id]
        revenue = model.revenue_net[lot_id]
        cost_rows[candidate] = {
            "direct_costs": _money(exact["direct"]), "transferred_in": _money(exact["received"]),
            "unit_cost": _unit(exact["unit"]),
            "cogs": _money(exact["cogs_total"]),
            "cogs_avoidable": _money(exact["cogs_avoidable"]),
            "cogs_committed": _money(exact["cogs_total"] - exact["cogs_avoidable"]),
            "cogs_joint": _money(exact["cogs_joint"]),
            "cogs_separable": _money(exact["cogs_total"] - exact["cogs_joint"]),
            "ending_quantity": _qty(model.ending[lot_id]),
            "ending_value": _money(exact["ending_value"])}
        margin_rows[candidate] = {
            "revenue_net": _money(revenue),
            "allocated_margin": _money(revenue - exact["cogs_total"]),
            "avoidable_margin": _money(revenue - exact["cogs_avoidable"])}
    produced_by = [flow["operation_id"] for flow in model.lot_flows[lot_id]
                   if flow["direction"] == "output"]
    consumed_by = [flow["operation_id"] for flow in model.lot_flows[lot_id]
                   if flow["direction"] == "input"]
    family = "跨批共享" if lot_id in shared else family_of.get(lot_id)
    return {"lot_id": lot_id, "product": lot["product"], "period": lot["period"], "unit": lot["unit"],
            "family": family,
            "closure": closure_row,
            "evidence": {"flows": [flow["id"] for flow in model.lot_flows[lot_id]],
                         "sales": [row["id"] for row in model.lot_sales[lot_id]],
                         "costs": [cost["id"] for cost in model.costs if cost["lot_id"] == lot_id]},
            "produced_by": produced_by, "consumed_by": consumed_by,
            "cost_flow": cost_rows, "margins": margin_rows}
