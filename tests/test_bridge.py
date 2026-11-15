import tempfile
import unittest
from decimal import Decimal

from cutmargin.bridge import bridge_analysis, lot_bridge
from cutmargin.catalog import Catalog
from cutmargin.service import Application


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog("data")
        self.analysis = bridge_analysis(self.catalog)

    def test_closure_all_closed(self):
        closure = self.analysis["closure"]
        self.assertTrue(closure["all_closed"])
        self.assertTrue(all(row["closed"] for row in closure["operations"]))
        lots = {row["lot_id"]: row for row in closure["lots"]}
        self.assertEqual(lots["B0-fillet"]["ending"], 9.0)
        self.assertEqual(lots["B1-fillet"]["ending"], 7.0)
        self.assertEqual(lots["B2-fillet"]["ending"], 12.0)
        self.assertEqual(lots["B0-rework"]["ending"], 1.0)
        self.assertEqual(lots["B0-raw"]["ending"], 0.0)
        self.assertEqual(lots["MIXED"]["ending"], 10.0)

    def test_conservation_both_bases(self):
        for basis in ("weight", "nrv"):
            conservation = self.analysis["cost_flow"][basis]["conservation"]
            self.assertTrue(conservation["conserved"])
            self.assertEqual(conservation["total_costs"], "61800.00")
            total = Decimal(conservation["cogs"]) + Decimal(conservation["ending_inventory"])
            self.assertEqual(total, Decimal(conservation["total_costs"]))

    def test_mixing_transfers_cost_without_duplication(self):
        lots = {row["lot_id"]: row for row in self.analysis["cost_flow"]["weight"]["lots"]}
        self.assertEqual(lots["MIXED"]["direct_costs"], "0.00")
        self.assertEqual(lots["MIXED"]["transferred_in"], "221.74")
        self.assertEqual(lots["MIXED"]["ending_value"], "221.74")
        # 混料只转移成本：来源批次的池子相应减少，全库守恒已在守恒检查中覆盖
        self.assertEqual(lots["B0-fillet"]["ending_value"], "199.57")
        self.assertEqual(lots["B1-fillet"]["ending_quantity"], 7.0)

    def test_weight_allocation_split(self):
        lots = {row["lot_id"]: row for row in self.analysis["cost_flow"]["weight"]["lots"]}
        self.assertEqual(lots["B0-fillet"]["transferred_in"], "1374.78")
        self.assertEqual(lots["B0-bones"]["transferred_in"], "665.22")
        self.assertEqual(lots["B0-fillet"]["unit_cost"], "22.1739")
        self.assertEqual(lots["B0-bones"]["unit_cost"], "22.1739")

    def test_batch_contribution(self):
        families = {row["batch"]: row
                    for row in self.analysis["views"]["batch_contribution"]["families"]}
        self.assertEqual(families["B0-raw"]["revenue_net"], "1956.00")
        self.assertEqual(families["B0-raw"]["contribution"], "-14.00")
        self.assertEqual(families["B0-raw"]["margin_after_committed"], "-104.00")
        self.assertEqual(families["B0-raw"]["cross_period_revenue"], "176.00")
        self.assertEqual(families["B0-raw"]["returns_amount"], "-80.00")
        self.assertEqual(families["B1-raw"]["contribution"], "66.00")
        self.assertEqual(families["B1-raw"]["margin_after_committed"], "-24.00")
        totals = {row["period"]: row
                  for row in self.analysis["views"]["batch_contribution"]["period_totals"]}
        self.assertEqual(totals["2026-09"]["margin_after_committed"], "-560.00")
        self.assertEqual(totals["2026-10"]["margin_after_committed"], "-240.00")

    def test_allocation_basis_flips_bones_margin(self):
        rows = self.analysis["allocation_choice"]["per_product"]
        bones = next(row for row in rows if row["product"] == "鱼骨" and row["period"] == "2026-09")
        self.assertLess(Decimal(bones["margin_weight"]), 0)
        self.assertGreater(Decimal(bones["margin_nrv"]), 0)
        self.assertEqual(bones["margin_weight"], "-9704.35")
        self.assertEqual(bones["margin_nrv"], "839.10")

    def test_allocation_swings_match_inventory_gap_overall(self):
        choice = self.analysis["allocation_choice"]
        swing = sum(Decimal(row["swing"]) for row in choice["per_product"])
        gap = sum(Decimal(values["nrv"]) - Decimal(values["weight"])
                  for values in choice["inventory_value"].values())
        self.assertLess(abs(swing - gap), Decimal("0.10"))

    def test_further_processing_rework(self):
        rows = self.analysis["views"]["further_processing"]["rework"]
        self.assertEqual(len(rows), 30)
        row = rows[0]
        self.assertEqual(row["input_opportunity_value"], "400.00")
        self.assertEqual(row["avoidable_benefit_realized"], "-164.00")
        self.assertEqual(row["avoidable_benefit_with_inventory"], "-132.00")
        self.assertEqual(row["breakeven_output_price"], "46.6667")
        self.assertEqual(row["incremental_avoidable_cost"], "20.00")
        drivers = {entry["driver"] for entry in row["sensitivity"]}
        self.assertEqual(drivers, {"产出售价", "投入机会价值"})

    def test_mixing_has_no_realized_benefit(self):
        mixing = self.analysis["views"]["further_processing"]["mixing"]
        self.assertEqual(len(mixing), 1)
        self.assertIsNone(mixing[0]["avoidable_benefit_realized"])
        self.assertEqual(mixing[0]["drawn_cost_by_basis"]["weight"], "221.74")
        self.assertEqual(mixing[0]["drawn_cost_by_basis"]["nrv"], "306.77")

    def test_margin_bridge_reconciles(self):
        bridge = self.analysis["margin_bridge"]
        self.assertTrue(bridge["reconciled"])
        self.assertEqual(bridge["effects_sum"], bridge["delta"])
        self.assertEqual(bridge["margin_by_period"]["2026-09"], "5343.29")
        self.assertEqual(bridge["margin_by_period"]["2026-10"], "2689.47")
        self.assertEqual(bridge["delta"], "-2653.82")
        rounded_sum = sum(Decimal(effect["amount"]) for effect in bridge["effects"])
        self.assertLess(abs(rounded_sum - Decimal(bridge["delta"])), Decimal("0.02"))

    def test_margin_bridge_factors(self):
        effects = {effect["factor"]: effect for effect in self.analysis["margin_bridge"]["effects"]}
        self.assertEqual(effects["售价"]["amount"], "0.00")
        self.assertEqual(effects["出成"]["amount"], "0.00")
        self.assertEqual(effects["单位成本"]["amount"], "0.00")
        self.assertLess(Decimal(effects["规模（批次数）"]["amount"]), 0)
        self.assertGreater(Decimal(effects["售出率（含退货）"]["amount"]), 0)
        for effect in effects.values():
            lo, hi = (Decimal(value) for value in effect["range"])
            self.assertLessEqual(lo, hi)
            self.assertLessEqual(lo, Decimal(effect["amount"]))
            self.assertLessEqual(Decimal(effect["amount"]), hi)

    def test_timing_separates_recognition_from_attribution(self):
        rows = {row["period"]: row for row in self.analysis["timing"]["periods"]}
        self.assertEqual(rows["2026-09"]["recognized_by_sale_date"], "35600.00")
        self.assertEqual(rows["2026-09"]["attributed_by_batch"], "40640.00")
        self.assertEqual(rows["2026-10"]["recognized_by_sale_date"], "25400.00")
        self.assertEqual(rows["2026-10"]["attributed_by_batch"], "20360.00")
        returns = self.analysis["timing"]["returns"]
        self.assertEqual(len(returns), 1)
        self.assertEqual(returns[0]["attributed_period"], "2026-09")
        cross = self.analysis["timing"]["cross_period_sales"]
        self.assertTrue(all(row["sale_period"] != row["lot_period"] for row in cross))

    def test_lot_bridge_traces_to_evidence(self):
        lot = lot_bridge(self.catalog, "B0-fillet")
        self.assertEqual(lot["family"], "B0-raw")
        self.assertEqual(lot["closure"]["ending"], 9.0)
        self.assertIn("FM0", lot["evidence"]["flows"])
        self.assertIn("RET-0", lot["evidence"]["sales"])
        self.assertEqual(lot["cost_flow"]["weight"]["unit_cost"], "22.1739")
        self.assertEqual(lot["cost_flow"]["nrv"]["unit_cost"], "30.6767")
        self.assertEqual(lot["produced_by"], ["P0-1"])
        self.assertEqual(set(lot["consumed_by"]), {"P0-2", "MIX-1"})
        mixed = lot_bridge(self.catalog, "MIXED")
        self.assertEqual(mixed["family"], "跨批共享")
        with self.assertRaises(KeyError):
            lot_bridge(self.catalog, "NOPE")

    def test_invalid_parameters(self):
        with self.assertRaises(ValueError):
            bridge_analysis(self.catalog, basis="gold")
        with self.assertRaises(ValueError):
            bridge_analysis(self.catalog, period_from="2026-10", period_to="2026-09")
        with self.assertRaises(ValueError):
            bridge_analysis(self.catalog, period_from="2026-09")
        with self.assertRaises(ValueError):
            bridge_analysis(self.catalog, period_from="2026-09", period_to="2030-01")


class BridgeServiceTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.application = Application("data", self.workspace.name)

    def test_bridge_endpoint(self):
        status, result = self.application.dispatch("GET", "/bridge")
        self.assertEqual(status, 200)
        self.assertEqual(result["source_fingerprint"], self.application.catalog.fingerprint)
        for key in ("closure", "cost_flow", "views", "timing", "margin_bridge", "allocation_choice"):
            self.assertIn(key, result["analysis"])

    def test_bridge_endpoint_with_parameters(self):
        status, result = self.application.dispatch("GET", "/bridge?basis=nrv&from=2026-09&to=2026-10")
        self.assertEqual(status, 200)
        self.assertEqual(result["analysis"]["parameters"]["reference_basis"], "nrv")
        self.assertEqual(self.application.dispatch("GET", "/bridge?basis=gold")[0], 400)
        self.assertEqual(self.application.dispatch("GET", "/bridge?from=2026-10&to=2026-09")[0], 400)

    def test_lot_endpoint(self):
        status, result = self.application.dispatch("GET", "/bridge/lots/B0-fillet")
        self.assertEqual(status, 200)
        self.assertEqual(result["lot"]["lot_id"], "B0-fillet")
        self.assertEqual(self.application.dispatch("GET", "/bridge/lots/NOPE")[0], 404)

    def test_case_can_embed_bridge_analysis(self):
        status, created = self.application.dispatch(
            "POST", "/cases", {"label": "收益桥讨论", "analysis": "bridge"})
        self.assertEqual(status, 201)
        self.assertEqual(created["analysis"]["kind"], "bridge")
        self.assertIn("margin_bridge", created["analysis"]["result"])
        status, restored = self.application.dispatch("GET", "/cases/" + created["id"])
        self.assertEqual(status, 200)
        self.assertEqual(restored, created)

    def test_case_rejects_unknown_analysis(self):
        status, _ = self.application.dispatch("POST", "/cases", {"label": "讨论", "analysis": "bogus"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
