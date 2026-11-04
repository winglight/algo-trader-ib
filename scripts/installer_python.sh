#!/usr/bin/env bash
# Bootstrap without Python, Docker, Git or a system package-manager install.
ati_ensure_installer_python() {
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
    return 0
  fi
  local tools="${XDG_DATA_HOME:-$HOME/.local/share}/ati/installer-tools" uv download
  mkdir -p "$tools"
  uv="$(command -v uv || true)"
  if [ -z "$uv" ]; then
    uv="$tools/uv"
    if [ ! -x "$uv" ]; then
      echo 'Preparing installer Python / 正在自动准备安装器的 Python 环境…'
      download="$(mktemp)"
      if ! curl -fsSL https://astral.sh/uv/install.sh -o "$download"; then rm -f "$download"; return 1; fi
      if ! UV_UNMANAGED_INSTALL="$tools" /bin/sh "$download"; then rm -f "$download"; return 1; fi
      rm -f "$download"
    fi
  fi
  "$uv" venv --python 3.11 --seed "$tools/python"
  export PATH="$tools/python/bin:$tools:$PATH"
  python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'
}
