# Security Policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/Cascapera/trade-forge/security/advisories/new)
rather than opening a public issue.

## Scope worth knowing about

This project can place real orders on a real brokerage account. Two consequences follow.

**Credentials.** MT5 login details and API keys are read from environment variables and
never committed. `gitleaks` scans the entire git history on every CI run, not just the
diff — a secret is compromised the moment it exists in any commit, and rewriting history
afterwards does not un-leak it.

**Execution safeguards.** The kill switch and the risk limits (max daily loss, max open
positions, max order volume, trading window) live *inside* the execution service, not in
the core. That placement is deliberate: they must hold even when the core is unreachable.
Any change that lets an order reach the broker without passing them is a critical bug, not
a feature request.

## Supported versions

Pre-1.0: only `main` receives fixes.
