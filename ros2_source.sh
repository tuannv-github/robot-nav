#!/bin/sh
# Source this file so ros2 sees packages built in this repo:
#   source ros2_source.sh
# Running it as a program does not change the calling shell.

if [ -n "${BASH_SOURCE[0]:-}" ]; then
  _src="${BASH_SOURCE[0]}"
elif [ -n "${ZSH_VERSION:-}" ]; then
  eval '_src="${(%):-%x}"'
else
  _src="$0"
fi
ROOT="$(CDPATH= cd -- "$(dirname -- "$_src")" && pwd)"
unset _src

if [ ! -f "$ROOT/install/setup.bash" ] && [ ! -f "$ROOT/install/setup.zsh" ]; then
  echo "install/ is missing. From ${ROOT}: ./ros2_build.sh" >&2
  return 1 2>/dev/null || exit 1
fi

if [ -n "${ZSH_VERSION:-}" ]; then
  # shellcheck disable=SC1091
  . "$ROOT/install/setup.zsh"
elif [ -n "${BASH_VERSION:-}" ]; then
  # shellcheck disable=SC1091
  . "$ROOT/install/setup.bash"
else
  # shellcheck disable=SC1091
  . "$ROOT/install/setup.sh"
fi
