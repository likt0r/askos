# The Agent Box's token and settings for shells too (Orca's terminals)
[ -r /run/agentbox/agent-env ] && . /run/agentbox/agent-env
# docker and docker compose use the agent's rootless Podman, as every service does (systemd).
# Other users, root included, get podman-docker's socket for them (podman-docker.sh, after this).
if [ "$(id -u)" = 1000 ]; then
  export DOCKER_HOST=unix:///run/user/1000/podman/podman.sock
elif [ "${DOCKER_HOST-}" = unix:///run/user/1000/podman/podman.sock ]; then
  unset DOCKER_HOST
fi
# Playwright takes the browsers of the Image and downloads none; every service has these from
# systemd's DefaultEnvironment, this is for logins (SSH).
export PLAYWRIGHT_BROWSERS_PATH=/usr/lib/ms-playwright PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
# Claude Code fetches none of the owner's claude.ai connectors, a Safeguard. Every service
# has it from systemd's DefaultEnvironment; this is for shells whose environment was rebuilt.
export ENABLE_CLAUDEAI_MCP_SERVERS=false
