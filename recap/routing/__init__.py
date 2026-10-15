"""Routing: decide how much investigation an incident deserves, then run it.

- signals.py       observable facts: services affected, symptom kinds, memory, failures, budget
- router.py        RulesRouter (the first matching rule wins) and the LearnedRouter interface stub
- orchestrator.py  runs one investigator, or two specialists truly in parallel (asyncio.gather)

See README.md here for the rule table and how concurrency is proven in the trace.
"""
