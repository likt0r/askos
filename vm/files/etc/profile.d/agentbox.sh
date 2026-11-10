# The Agent Box's token and settings for shells too (Orca's terminals)
[ -r /run/agentbox/agent-env ] && . /run/agentbox/agent-env
# docker and docker compose use the agent's rootless Podman, as for every service
export DOCKER_HOST="unix:///run/user/$(id -u)/podman/podman.sock"
# Claude Code fetches none of the owner's claude.ai connectors, a Safeguard. Every service
# has it from systemd's DefaultEnvironment; this is for shells whose environment was rebuilt.
export ENABLE_CLAUDEAI_MCP_SERVERS=false
