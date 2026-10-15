"""Harness: the incident controller, the agent turn loop, budgets and checkpoints.

- controller.py  owns one incident's lifecycle and is the only code that can execute a fix
- loop.py        runs one agent's observe -> reason -> act loop
- budget.py      counts steps, tool calls, tokens and time; stops runs cleanly at a limit
- store.py       persists RunRecord checkpoints and evidence to SQLite
"""
