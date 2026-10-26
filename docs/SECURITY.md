# Security notes

- Bind API to loopback. Do not expose it to a LAN without authentication and security review.
- Do not commit secrets; .env.example contains only blank configuration variables.
- A public URL is not reuse permission. Unknown rights block media import and analysis.
- Media paths must resolve inside configured media root; subprocesses use argument arrays with shell=False and finite timeouts.
- Global pause prevents new queueing/claiming; it does not kill a running subprocess.
- No public publishing route exists in this release. Back up SQLite and media separately.
