#!/usr/bin/env bash
# Shared release settings. Never enable tracing while credentials are loaded.

release_load_env() {
  set +x
  set +v
  local release_env_path="${NARUMI_RELEASE_ENV:-$HOME/.config/narumi/release.env}"
  if [[ -e "$release_env_path" ]]; then
    [[ -f "$release_env_path" && -r "$release_env_path" ]] || return 1
    set -a
    # This is a trusted local shell configuration, not release input.
    source "$release_env_path" >/dev/null 2>&1 || return 1
    set +a
    set +x
    set +v
  elif [[ -n "${NARUMI_RELEASE_ENV:-}" ]]; then
    return 1
  fi
}

release_sparkle_bin() {
  local release_root="$1" candidate tool found
  if [[ -n "${SPARKLE_BIN:-}" ]]; then
    candidate="$SPARKLE_BIN"
    for tool in generate_keys sign_update generate_appcast; do
      [[ -x "$candidate/$tool" ]] || return 1
    done
    printf '%s\n' "$candidate"
    return 0
  fi
  # SwiftPM already downloaded the pinned Sparkle artifact for the application.
  for candidate in "$release_root"/app/.build/artifacts/*/*/bin; do
    found=1
    for tool in generate_keys sign_update generate_appcast; do
      [[ -x "$candidate/$tool" ]] || found=0
    done
    if [[ $found -eq 1 ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}
