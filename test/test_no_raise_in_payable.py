"""
Structural test: no @gl.public.write.payable method may contain a `raise`
statement anywhere in its body.

This is the load-bearing money-safety rule for GenVM contracts. A payable
method that raises rolls back contract *storage* -- but not the GEN value
that rode in with the call, which the ghost contract has already credited.
Raising from a payable rejection would strand the sender's money in the
contract, unaccounted for and unrecoverable through the contract's own
logic. Every rejection inside a payable method must instead refund the
sender and return a normal (non-raising) response.

This is checked here with Python's `ast` module directly against the
contract source, independent of the offline runtime tests, so a future edit
that reintroduces a `raise` inside a payable method fails a test even if
every functional test still happens to pass.

Run with:  python3 -m unittest discover -s test -v
"""
import ast
import unittest
from pathlib import Path

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"
CONTRACT_FILES = ["GigEscrow.py", "TalentGate.py"]


def _is_payable(func_node):
	for dec in func_node.decorator_list:
		try:
			src = ast.unparse(dec)
		except Exception:
			src = ""
		if "payable" in src:
			return True
	return False


def _payable_methods_with_raise(source, filename):
	tree = ast.parse(source, filename=filename)
	found = []
	for node in ast.walk(tree):
		if isinstance(node, ast.FunctionDef) and _is_payable(node):
			for sub in ast.walk(node):
				if isinstance(sub, ast.Raise):
					found.append((node.name, sub.lineno))
	return found


def _all_payable_methods(source, filename):
	tree = ast.parse(source, filename=filename)
	names = []
	for node in ast.walk(tree):
		if isinstance(node, ast.FunctionDef) and _is_payable(node):
			names.append(node.name)
	return names


class PayableNeverRaisesTestCase(unittest.TestCase):
	def test_no_payable_method_contains_a_raise(self):
		for filename in CONTRACT_FILES:
			path = CONTRACTS_DIR / filename
			source = path.read_text()
			with self.subTest(file=filename):
				violations = _payable_methods_with_raise(source, filename)
				self.assertEqual(violations, [],
					filename + " has payable method(s) containing a raise: "
					+ str(violations))

	def test_gigescrow_has_the_two_expected_payable_entry_points(self):
		path = CONTRACTS_DIR / "GigEscrow.py"
		names = set(_all_payable_methods(path.read_text(), "GigEscrow.py"))
		self.assertEqual(names, {"fund_milestone", "dispute_milestone"})

	def test_talentgate_has_no_payable_methods(self):
		"""TalentGate never holds funds -- it should have nothing payable
		to begin with, which makes the raise-safety question moot for it
		by construction rather than by convention."""
		path = CONTRACTS_DIR / "TalentGate.py"
		names = _all_payable_methods(path.read_text(), "TalentGate.py")
		self.assertEqual(names, [])


if __name__ == "__main__":
	unittest.main()
