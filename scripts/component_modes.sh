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
