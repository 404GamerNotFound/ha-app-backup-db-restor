#!/usr/bin/with-contenv bashio
set -euo pipefail

readonly LOG_LEVEL="$(bashio::config 'log_level')"
readonly DATABASE_PATH="$(bashio::config 'database_path')"
readonly CACHE_PATH="$(bashio::config 'cache_path')"
readonly CONFIG_BACKUP_PATH="$(bashio::config 'config_backup_path')"
readonly MAX_UPLOAD_MB="$(bashio::config 'max_upload_mb')"

bashio::log.level "${LOG_LEVEL}"

bashio::log.info "Starting Backup DB Restore UI"
bashio::log.info "Current database path: ${DATABASE_PATH}"
bashio::log.info "Cache path: ${CACHE_PATH}"
bashio::log.info "Config backup path: ${CONFIG_BACKUP_PATH}"
bashio::log.info "Upload limit: ${MAX_UPLOAD_MB} MB"

exec python3 /app/app.py
