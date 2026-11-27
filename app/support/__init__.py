"""AI Support & self-healing reporting engine.

Employees report discrepancies and portal problems as persistent issues, talk to the AI Reporting
Support Agent (built-in rules first, Claude for exceptions), and issues that need a code change are
handed to the AI Engineering Agent (a Claude Code cloud scheduled task, or GitHub Actions). Status is
synchronised back from GitHub and the deployed commit; a trigger being accepted never counts as fixed.
"""
