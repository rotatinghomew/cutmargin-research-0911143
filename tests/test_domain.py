import unittest
from cutmargin.catalog import Catalog
from cutmargin.domain import operation_balances


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog("data")

    def test_stage_mass_balance(self):
        self.assertTrue(all(float(value) == 0 for value in operation_balances(self.catalog).values()))
        self.assertTrue(any(record["return_of"] for record in self.catalog.rows("sales")))


    def test_single_operation_hand_balance(self):
        records = [{"operation_id": "cut", "direction": direction, "quantity": quantity}
                   for direction, quantity in (("input", 10), ("output", 7), ("output", 2), ("loss", 1))]
        self.assertEqual(float(operation_balances(FakeCatalog({"flows": records}))["cut"]), 0)

    def test_nonzero_balance_is_visible(self):
        records = [{"operation_id": "cut", "direction": "input", "quantity": 10},
                   {"operation_id": "cut", "direction": "output", "quantity": 8}]
        self.assertEqual(float(operation_balances(FakeCatalog({"flows": records}))["cut"]), 2)

    def test_same_product_different_operation_not_combined(self):
        records = [{"operation_id": name, "direction": "input", "quantity": quantity}
                   for name, quantity in (("cut", 10), ("rework", 4))]
        result = operation_balances(FakeCatalog({"flows": records}))
        self.assertEqual(set(result), {"cut", "rework"})

    def test_no_flows(self):
        self.assertEqual(operation_balances(FakeCatalog({"flows": []})), {})


class FakeCatalog:
    def __init__(self, tables):
        self.tables = tables

    def rows(self, name):
        return self.tables[name]
