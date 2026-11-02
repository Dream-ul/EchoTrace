# Security policy

## Reporting a vulnerability

Please do not report security vulnerabilities in public issues. Use GitHub's private vulnerability reporting feature for this repository. Repository administrators should enable private vulnerability reporting before inviting public use.

Include the affected component, impact, reproduction steps, and any suggested mitigation. Do not include live credentials or private investigation data in the report.

## Credential handling

- Keep runtime credentials in an untracked `.env` file or a deployment secret store.
- Rotate any credential that may have been exposed; removing it from the latest commit does not remove it from Git history or clones.
- Use dedicated credentials with the least access needed, and do not expose database, Redis, or object-store ports to untrusted networks.
