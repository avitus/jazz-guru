"""Test infrastructure for Tier-2 dynamic tools.

See ``docs/plans/tier2-tool-tests-and-improvement.md`` for the full design.
The submodules are:

- ``predicates``: deterministic JSON-tree predicate DSL used by test cases.
- ``runner`` (PR 4): subprocess-backed test case executor.
- ``failure_signals`` (PR 5): trace-mining for the reflexion-driven
  improver.
"""
