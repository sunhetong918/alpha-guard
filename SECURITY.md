# Security Policy

## Supported versions

Alpha Guard is currently pre-release software. Security fixes are applied to
the latest commit on the default branch; older commits and forks are not
maintained.

## Reporting a vulnerability

Please do not disclose suspected vulnerabilities in a public issue. Use the
repository's [private vulnerability reporting
form](https://github.com/sunhetong918/alpha-guard/security/advisories/new) and
include:

- the affected commit and component;
- reproducible steps or a minimal proof of concept;
- the likely impact and any known mitigations; and
- whether the report contains secrets or personal data.

You should receive an acknowledgement within seven days and a status update
within fourteen days. Please allow time for a fix and coordinated disclosure
before publishing details.

Never include live Telegram, data-provider, or model credentials in a report.
Alpha Guard does not intentionally accept broker trading credentials or expose
an order-execution interface; any path that appears to do so should be treated
as a security issue.
