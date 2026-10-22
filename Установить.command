#!/bin/zsh
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ "$(uname -s)" != Darwin ]]; then
  print 'Этот установщик рассчитан на macOS.'
  exit 1
fi
GAZETTE_PYTHON=''
for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3,11))'; then
    GAZETTE_PYTHON="$(command -v "$candidate")"
    break
  fi
done
if [[ -z "$GAZETTE_PYTHON" ]]; then
  print 'Сначала установи Python 3.11 или новее с https://www.python.org/downloads/macos/'
  print 'После установки снова открой этот файл.'
  read '?Нажми Enter для выхода.'
  exit 1
fi
"$GAZETTE_PYTHON" -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
if [[ ! -f config.json ]]; then
  cp config.example.json config.json
fi
.venv/bin/python gazette.py demo
print '\nГотово. Это только пример: принтер ещё не включён.'
print 'Дальше открой Настроить.command и README.md.'
/usr/bin/open output/газета-пример.pdf
read '?Нажми Enter, чтобы закрыть окно.'
