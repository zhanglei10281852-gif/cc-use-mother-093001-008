import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from district_works.contracts import AccessCommitment, WorkTask


class WorkContractTests(unittest.TestCase):
    def test_dependency_is_retained(self):
        self.assertEqual(WorkTask("B", "S", ("A",), 1).depends_on, ("A",))

    def test_self_dependency_is_rejected(self):
        with self.assertRaises(ValueError):
            WorkTask("A", "S", ("A",), 1)


if __name__ == "__main__": unittest.main()
