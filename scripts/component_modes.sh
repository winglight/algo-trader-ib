#!/usr/bin/env bash
# Component metadata is parsed as data, never sourced as shell code.
# all keeps its original research + trading meaning; Agent is always opt-in.
ati_normalize_components() {
  local remaining="${1// /}" token research=0 trading=0 agent=0 result=''
  [ -n "$remaining" ] || return 2
  while :; do
    token="${remaining%%,*}"
    case "$token" in research) research=1 ;; trading) trading=1 ;; agent) agent=1 ;; all) research=1; trading=1 ;; *) echo 'Invalid components; select research, trading, agent, or a comma-separated combination.' >&2; return 2 ;; esac
    [ "$remaining" != "$token" ] || break
    remaining="${remaining#*,}"
  done
  if [ "$research" = 1 ] && [ "$trading" = 1 ]; then result=all
  elif [ "$research" = 1 ]; then result=research
  elif [ "$trading" = 1 ]; then result=trading
  fi
  if [ "$agent" = 1 ]; then result="${result:+$result,}agent"; fi
  printf '%s\n' "$result"
}
ati_component_has() {
  local normalized
  normalized="$(ati_normalize_components "$1")" || return $?
  case ",$normalized," in *",$2,"*) return 0 ;; esac
  [ "$2" = agent ] && return 1
  case ",$normalized," in *,all,*) return 0 ;; esac
  return 1
}

ati_detect_components() {
  local root="$1" state="${2:-}" recorded='' research=0 trading=0 agent=0 result=''
  if [ -f "$root/.components.env" ]; then
    recorded="$(sed -n 's/^ATI_INSTALLED_COMPONENTS=//p' "$root/.components.env")"
    ati_normalize_components "$recorded"; return $?
  fi
  if [ -f "$root/.research.env" ] || [ -f "$root/.local-tools.env" ]; then research=1; fi
  if [ -f "$root/.env" ] || { [ -n "$state" ] && [ -f "$state/trading-enabled" ]; }; then trading=1; fi
  if [ -f "$root/.agent.env" ]; then agent=1; fi
  [ "$research" = 0 ] || result=research
  [ "$trading" = 0 ] || result="${result:+$result,}trading"
  [ "$agent" = 0 ] || result="${result:+$result,}agent"
  [ -z "$result" ] || ati_normalize_components "$result"
}
ati_record_component() {
  local root="$1" component="$2" previous="$3" installed temp
  installed="$(ati_normalize_components "${previous:+$previous,}$component")" || return $?
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
  if ati_component_has "$components" trading; then bash "$root/scripts/install_trading_connector.sh" install || return $?; fi
  if ati_component_has "$components" research; then bash "$root/scripts/install_research.sh" install || return $?; fi
}
