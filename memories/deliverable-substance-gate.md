# Deliverable substance gate

Probe completion now runs a deterministic AST check over delivered Python files after both semantic review stages. A syntax error or a file containing only documentation, imports, `pass`, ellipses, or explicitly unimplemented definitions forces a bounded tool-enabled repair pass before publication. Empty `__init__.py` files are exempt, and substantive Python plus non-code answers remain unaffected. The public-path proof is `tests/test_deliverable_substance_gate.py`.
