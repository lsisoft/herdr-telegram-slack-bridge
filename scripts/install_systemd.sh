#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${HOME}/.local/bin"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
WRAPPER="${BIN_DIR}/agent_notify.py"
TELEGRAM_SERVICE_NAME="agent-telegram-tmux-bridge.service"
SLACK_SERVICE_NAME="agent-slack-tmux-bridge.service"

mkdir -p "${BIN_DIR}" "${SYSTEMD_USER_DIR}"

if [[ -f "${WRAPPER}" && ! -L "${WRAPPER}" ]]; then
  cp "${WRAPPER}" "${WRAPPER}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
fi

python3 - "${PROJECT_ROOT}" "${WRAPPER}" <<'PY'
from pathlib import Path
import sys

project_root = Path(sys.argv[1])
wrapper = Path(sys.argv[2])
wrapper.write_text(
    "#!/usr/bin/env python3\n"
    "import sys\n"
    f"sys.path.insert(0, {str(project_root)!r})\n"
    "from agent_telegram_bridge.cli import main\n"
    "raise SystemExit(main(['notify', *sys.argv[1:]]))\n",
    encoding="utf-8",
)
wrapper.chmod(0o755)
PY

for service_name in "${TELEGRAM_SERVICE_NAME}" "${SLACK_SERVICE_NAME}"; do
  sed "s|@PROJECT_ROOT@|${PROJECT_ROOT}|g" \
    "${PROJECT_ROOT}/systemd/${service_name}" > "${SYSTEMD_USER_DIR}/${service_name}"
  chmod 0644 "${SYSTEMD_USER_DIR}/${service_name}"
done

systemctl --user daemon-reload
systemctl --user enable --now "${TELEGRAM_SERVICE_NAME}"
systemctl --user restart "${TELEGRAM_SERVICE_NAME}"
systemctl --user --no-pager --full status "${TELEGRAM_SERVICE_NAME}" || true

echo "Installed ${TELEGRAM_SERVICE_NAME} and ${SLACK_SERVICE_NAME}"
echo "Enable Slack after configuring it: systemctl --user enable --now ${SLACK_SERVICE_NAME}"
echo "Hook wrapper: ${WRAPPER}"
