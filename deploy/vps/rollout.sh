#!/usr/bin/env bash
set -euo pipefail

repo_dir="${FORGE_HUB_REPO_DIR:-/opt/lush-forge-hub/repo}"
service="${FORGE_HUB_SERVICE:-lush-forge-hub.service}"
health_url="${FORGE_HUB_HEALTH_URL:-http://172.17.0.1:8036/healthz}"
branch="${FORGE_HUB_BRANCH:-main}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this script as root so it can restart ${service}." >&2
  exit 2
fi
if [[ ! -d "${repo_dir}/.git" ]]; then
  echo "Git checkout not found: ${repo_dir}" >&2
  exit 2
fi
local_changes="$(runuser -u forgehub -- git -C "${repo_dir}" status --porcelain)"
if [[ -n "${local_changes}" ]]; then
  echo "Refusing rollout: ${repo_dir} has local changes." >&2
  exit 2
fi

previous_commit="$(runuser -u forgehub -- git -C "${repo_dir}" rev-parse HEAD)"
backup_dir="/root/backups/lush-forge-hub/rollout-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${backup_dir}"
printf '%s\n' "${previous_commit}" > "${backup_dir}/previous-commit.txt"

runuser -u forgehub -- git -C "${repo_dir}" fetch --prune origin "${branch}"
target_commit="$(runuser -u forgehub -- git -C "${repo_dir}" rev-parse "origin/${branch}")"
runuser -u forgehub -- git -C "${repo_dir}" checkout --detach "${target_commit}"
systemctl restart "${service}"

if ! systemctl is-active --quiet "${service}" || ! curl -fsS --max-time 8 "${health_url}" >/dev/null; then
  echo "Health check failed; rolling back to ${previous_commit}." >&2
  runuser -u forgehub -- git -C "${repo_dir}" checkout --detach "${previous_commit}"
  systemctl restart "${service}"
  curl -fsS --max-time 8 "${health_url}" >/dev/null
  exit 1
fi

printf 'Rolled out %s (%s); health check passed.\n' "${branch}" "${target_commit}"
