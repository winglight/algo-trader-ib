#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

has_cmd() { command -v "$1" >/dev/null 2>&1; }
is_wsl() { [ -f /proc/version ] && grep -qi "microsoft" /proc/version; }
wait_for_docker() { local tries=60; while [ $tries -gt 0 ]; do if docker info >/dev/null 2>&1; then return 0; fi; sleep 2; tries=$((tries-1)); done; return 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if has_cmd sudo; then SUDO="sudo"; else echo "sudo is required" >&2; exit 1; fi
fi

install_brew() {
  if ! has_cmd brew; then
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    if [ -d /opt/homebrew/bin ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; fi
    if [ -d /usr/local/bin ]; then eval "$(/usr/local/bin/brew shellenv)"; fi
  fi
}

install_mac() {
  install_brew
  brew install --cask docker || true
  open -a Docker || true
  wait_for_docker || true
}

install_linux_debian() {
  $SUDO apt-get update -y
  $SUDO apt-get install -y ca-certificates curl gnupg
  $SUDO install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | $SUDO gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  $SUDO chmod a+r /etc/apt/keyrings/docker.gpg
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null
  $SUDO apt-get update -y
  $SUDO apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  $SUDO systemctl enable --now docker || $SUDO service docker start || true
  $SUDO usermod -aG docker "$USER" || true
}

install_linux_rhel() {
  $SUDO yum install -y yum-utils
  $SUDO yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
  $SUDO yum install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  $SUDO systemctl enable --now docker || $SUDO service docker start || true
  $SUDO usermod -aG docker "$USER" || true
}

install_linux_fedora() {
  $SUDO dnf -y install dnf-plugins-core
  $SUDO dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo
  $SUDO dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  $SUDO systemctl enable --now docker || $SUDO service docker start || true
  $SUDO usermod -aG docker "$USER" || true
}

install_wsl() {
  if has_cmd apt-get; then
    install_linux_debian
  else
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "winget install --id Docker.DockerDesktop -e --source winget" || true
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath 'C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe'" || true
  fi
}

main() {
  if has_cmd docker; then return 0; fi
  os="$(uname -s)"
  case "$os" in
    Darwin) install_mac ;;
    Linux)
      if is_wsl; then
        install_wsl
      else
        if has_cmd apt-get; then
          install_linux_debian
        elif has_cmd dnf; then
          install_linux_fedora
        elif has_cmd yum; then
          install_linux_rhel
        else
          echo "Unsupported Linux distribution" >&2
          exit 1
        fi
      fi
      ;;
    *) echo "Unsupported OS: $os" >&2; exit 1 ;;
  esac
  wait_for_docker || true
}

main "$@"

