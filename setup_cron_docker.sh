#!/bin/bash
# Cron для LSE в Docker — обёртка: тот же эталон, что setup_cron.sh на VM с lse-bot.
# Запуск: ./setup_cron_docker.sh

set -e
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/setup_cron.sh"
