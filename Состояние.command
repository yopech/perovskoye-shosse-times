#!/bin/zsh
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  print 'Сначала открой Установить.command.'
else
  .venv/bin/python gazette.py status
fi
read '?Нажми Enter, чтобы закрыть окно.'
