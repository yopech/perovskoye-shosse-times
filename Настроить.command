#!/bin/zsh
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  print 'Сначала открой Установить.command.'
  read '?Нажми Enter для выхода.'
  exit 1
fi
.venv/bin/python gazette.py personalize
.venv/bin/python gazette.py setup
