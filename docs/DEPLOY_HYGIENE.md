\# Deploy Hygiene



Short runbook for deploying the expense-reporting app to the production VPS, plus lessons from the April 24, 2026 recovery. Read this before making any production change.



\## Current deploy model



\- Production VPS: `46.225.103.156`

\- App path: `/opt/dcexpense/app` — a \*\*git checkout of `origin/main`\*\*, owned by `dcexpense:dcexpense`

\- Deploy script: `/opt/dcexpense/deploy.sh`

\- Service: `dcexpense.service` (systemd)

\- Env file: `/etc/dcexpense/env` (not in repo, never touched by deploy)

\- DB: `/var/lib/dcexpense/expense\_app.db` (not in repo, never touched by deploy)

\- Venv: `/opt/dcexpense/venv` (not in repo, picks up editable-install changes on each deploy)



\## Normal deploy



```bash

