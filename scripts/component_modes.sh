#!/usr/bin/env bash
# Component metadata is parsed as data, never sourced as shell code.
ati_detect_components() {
  local root="$1" state="${2:-}" recorded='' research=0 trading=0
  if [ -f "$root/.components.env" ]; then
    recorded="$(sed -n 's/^ATI_INSTALLED_COMPONENTS=//p' "$root/.components.env")"
    case "$recorded" in research|trading|all) printf '%s\n' "$recorded"; return ;; *) echo 'Invalid component metadata; installation unchanged.' >&2; return 1 ;; esac
  fi
  if [ -f "$root/.research.env" ] || [ -f "$root/.local-tools.env" ]; then research=1; fi
  if [ -f "$root/.env" ] || { [ -n "$state" ] && [ -f "$state/trading-enabled" ]; }; then trading=1; fi
  if [ "$research" = 1 ] && [ "$trading" = 1 ]; then echo all
  elif [ "$research" = 1 ]; then echo research
  elif [ "$trading" = 1 ]; then echo trading
  fi
}
ati_component_has() { [ "$1" = all ] || [ "$1" = "$2" ]; }
ati_record_component() {
  local root="$1" component="$2" previous="$3" installed temp
  installed="$component"
  if [ -n "$previous" ] && [ "$previous" != "$component" ]; then installed=all; fi
  temp="$(mktemp "$root/.components.env.XXXXXX")"
  chmod 600 "$temp"
  printf 'ATI_INSTALLED_COMPONENTS=%s\n' "$installed" > "$temp"
  mv "$temp" "$root/.components.env"
}

# Saved connector configuration expresses installation intent, not readiness or
# authority. Existing connector installers still verify identity under their lock.
ati_install_studio_connectors() {
  local root="$1" components="$2" mode="$3" connect=0
  case "$mode" in
    1) connect=1 ;;
    0) connect=0 ;;
    auto) if [ -f "$root/.research.env" ] || [ -f "$root/.trading-connector.env" ]; then connect=1; fi ;;
    *) echo 'Invalid Studio connection mode; use auto, 0 or 1.' >&2; return 2 ;;
  esac
  if [ "$connect" = 0 ]; then
    echo 'Studio linking is optional. Eligible accounts receive a 14-day free membership trial; credits are not included.'
    echo '绑定 Studio 为可选操作；符合条件的账户首次绑定可获 14 天免费会员试用，不附赠 credits。本地功能可继续使用。'
    if [ "$components" = research ]; then
      echo 'To link later / 稍后连接：bash scripts/install_research.sh install'
    else
      echo 'Use Link Studio in the local console, or rerun with --studio-connect. Existing connectors are retained.'
    fi
    return 0
  fi
  if [ "$components" != research ]; then bash "$root/scripts/install_trading_connector.sh" install || return $?; fi
  if [ "$components" != trading ]; then bash "$root/scripts/install_research.sh" install || return $?; fi
}
