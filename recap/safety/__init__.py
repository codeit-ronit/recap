"""Safety: the trusted rules that decide what may run, and the red-team tools that test them.

    policy.py     PolicyEngine: ALLOW / APPROVAL_REQUIRED / DENY with rule IDs (R0-R14)
    approvals.py  ApprovalStore + channels: exact-scope, expiring, single-use approvals
    injection.py  pattern-based prompt-injection *flagging* (not production-grade protection)
    redteam.py    PromptInjectedProvider: a deliberately compromised planner for red-team tests

The protection is architectural: the tool gateway (recap/tools/gateway.py) asks the policy
about every call, so safety never depends on a model or a document behaving well.
See README.md in this folder for the rule table and the limitations.
"""
