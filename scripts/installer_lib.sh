#!/usr/bin/env bash

read_env_value() {
  local file="$1" key="$2" value
  if [ -f "$file" ]; then
    value="$(
      awk -v key="$key" '
        index($0, key "=") == 1 {
          value=substr($0, length(key) + 2)
          found=1
        }
        END { if (found) printf "%s", value }
      ' "$file"
    )"
    decode_env_value "$value"
  fi
}

decode_env_value() {
  local value="${1-}" output="" char next
  if [ "${#value}" -ge 2 ] && [ "${value:0:1}" = '"' ] && [ "${value: -1}" = '"' ]; then
    value="${value:1:${#value}-2}"
    while [ -n "$value" ]; do
      char="${value:0:1}"
      value="${value:1}"
      if [ "$char" = '\' ] && [ -n "$value" ]; then
        next="${value:0:1}"
        case "$next" in
          '\'|'"')
            output+="$next"
            value="${value:1}"
            continue
            ;;
        esac
      elif [ "$char" = '$' ] && [ "${value:0:1}" = '$' ]; then
        output+='$'
        value="${value:1}"
        continue
      fi
      output+="$char"
    done
    printf '%s' "$output"
    return
  fi
  if [ "${#value}" -ge 2 ] && [ "${value:0:1}" = "'" ] && [ "${value: -1}" = "'" ]; then
    value="${value:1:${#value}-2}"
    value="${value//\\\'/\'}"
  fi
  printf '%s' "$value"
}

encode_env_value() {
  local value="${1-}"
  case "$value" in
    *$'\n'*|*$'\r'*)
      echo "Environment values must contain exactly one line." >&2
      return 1
      ;;
  esac
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//\$/\$\$}"
  printf '"%s"' "$value"
}

placeholder_or_empty() {
  case "${1:-}" in
    ""|change_me|change-this-even-stronger|ChangeThisUserPassword) return 0 ;;
    *) return 1 ;;
  esac
}

env_set() {
  local file="$1" key="$2" value="$3" tmp encoded
  encoded="$(encode_env_value "$value")"
  tmp="$(mktemp "${file}.write.XXXXXX")"
  awk -v key="$key" 'index($0, key "=") != 1 { print }' "$file" >"$tmp"
  printf '%s=%s\n' "$key" "$encoded" >>"$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$file"
}

contains_profile() {
  case ",${1}," in
    *",${2},"*) return 0 ;;
    *) return 1 ;;
  esac
}

validate_enabled_adapters() {
  case "$1" in
    sim|sim,ibkr_paper|sim,alpaca_paper|sim,ibkr_paper,alpaca_paper) ;;
    *)
      echo "Enabled adapters must be an ordered subset of sim,ibkr_paper,alpaca_paper and must include sim." >&2
      return 1
      ;;
  esac
}

validate_initial_adapter() {
  local enabled="$1" initial="$2"
  case "$initial" in
    sim|ibkr_paper|alpaca_paper) ;;
    *) echo "Unknown initial adapter: $initial" >&2; return 1 ;;
  esac
  if ! contains_profile "$enabled" "$initial"; then
    echo "Initial adapter must be present in enabled adapters." >&2
    return 1
  fi
}

secret_file_mode() {
  if stat -f '%Lp' "$1" >/dev/null 2>&1; then
    stat -f '%Lp' "$1"
  else
    stat -c '%a' "$1"
  fi
}

read_secret_file() {
  local file="$1" label="$2" mode lines value
  [ -f "$file" ] || { echo "$label file does not exist: $file" >&2; return 1; }
  [ ! -L "$file" ] || { echo "$label file must not be a symlink." >&2; return 1; }
  mode="$(secret_file_mode "$file")"
  case "$mode" in
    600|400) ;;
    *) echo "$label file permissions must be 0600 or 0400." >&2; return 1 ;;
  esac
  lines="$(awk 'END { print NR }' "$file")"
  [ "$lines" = "1" ] || { echo "$label file must contain exactly one line." >&2; return 1; }
  IFS= read -r value <"$file" || [ -n "$value" ]
  [ -n "$value" ] || { echo "$label file must not be empty." >&2; return 1; }
  printf '%s' "$value"
}

prompt_value() {
  local prompt="$1" default_value="${2:-}" secret="${3:-0}" value
  if [ "$secret" = "1" ]; then
    if [ -n "$default_value" ]; then
      read -r -s -p "$prompt [keep existing]: " value
    else
      read -r -s -p "$prompt: " value
    fi
    printf '\n' >&2
  else
    if [ -n "$default_value" ]; then
      read -r -p "$prompt [$default_value]: " value
    else
      read -r -p "$prompt: " value
    fi
  fi
  printf '%s' "${value:-$default_value}"
}

mask_prompt_value() {
  local value="${1-}" visible hidden_count index
  visible="${value:0:2}"
  hidden_count=$((${#value} - ${#visible}))
  printf '%s' "$visible"
  index=0
  while [ "$index" -lt "$hidden_count" ]; do
    printf '*'
    index=$((index + 1))
  done
}

prompt_masked_value() {
  local prompt="$1" default_value="${2:-}" value masked
  if [ -n "$default_value" ]; then
    masked="$(mask_prompt_value "$default_value")"
    read -r -p "$prompt [$masked]: " value
  else
    read -r -p "$prompt: " value
  fi
  printf '%s' "${value:-$default_value}"
}

prompt_yes_no() {
  local prompt="$1" default="$2" value suffix
  if [ "$default" = "yes" ]; then suffix="Y/n"; else suffix="y/N"; fi
  while true; do
    read -r -p "$prompt [$suffix]: " value
    value="${value:-$default}"
    case "$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')" in
      y|yes) return 0 ;;
      n|no) return 1 ;;
      *) echo "Please answer yes or no." >&2 ;;
    esac
  done
}

configured_existing_or_file() {
  local current="$1" file="$2" label="$3"
  if [ -n "$file" ]; then
    read_secret_file "$file" "$label"
  elif ! placeholder_or_empty "$current"; then
    printf '%s' "$current"
  else
    echo "$label is required." >&2
    return 1
  fi
}
