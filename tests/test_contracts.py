import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from district_works.contracts import AccessCommitment, WorkTask


class WorkContractTests(unittest.TestCase):
    def test_dependency_is_retained(self):
        self.assertEqual(WorkTask("B", "S", ("A",), 1).depends_on, ("A",))

    def test_self_dependency_is_rejected(self):
        with self.assertRaises(ValueError):
            WorkTask("A", "S", ("A",), 1)

    def test_extended_fields_keep_original_signature(self):
        t = WorkTask("W-6", "SEG-2", ("W-3",), 2)
        self.assertEqual((t.crew_id, t.occupies_width_m, t.requires_closure),
                         (None, 0.0, False))

    def test_bad_occupancy_width_rejected(self):
        with self.assertRaises(ValueError):
            WorkTask("A", "S", (), 1, occupies_width_m=-1)


class AccessCommitmentTests(unittest.TestCase):
    def test_commitment_retains_width(self):
        self.assertEqual(AccessCommitment("SEG-2", 1.5, True).minimum_width_m, 1.5)

    def test_non_positive_width_rejected(self):
        with self.assertRaises(ValueError):
            AccessCommitment("S", 0, True)


if __name__ == "__main__":
    unittest.main()
