#!/bin/bash
# Install job into ~/.local/bin on this machine and on remote hosts, then check
# that every copy has the same sha256 as bin/job.
# Usage: ./install.sh [host ...]
#   With no arguments: this machine plus every host in ~/.config/agent-pueue/hosts.
#   A host is a name from that file, or "local".
set -uo pipefail
src="$(cd "$(dirname "$0")" && pwd)/bin/job"
want="$(sha256sum "$src" | cut -d' ' -f1)"
ver="$(python3 "$src" --version)"
conf="${JOB_HOSTS_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/agent-pueue/hosts}"
# The hosts file, read the way job reads it: "name [destination]", # comments,
# names of letters, digits, dot, dash and underscore, never "local".
valid_hosts() {
  [ -f "$conf" ] || return 0
  awk '{sub(/#.*/, "")} NF == 0 {next}
       NF > 2 || $1 == "local" || $1 ~ /^-/ || $1 !~ /^[A-Za-z0-9._-]+$/ || ($2 != "" && $2 ~ /^-/) {next}
       {print $1, ($2 != "" ? $2 : $1)}' "$conf"
}
dest_of() { valid_hosts | awk -v n="$1" '$1 == n {d = $2} END {print d}'; }  # the last line wins, as in job
hosts=("$@")
if [ ${#hosts[@]} -eq 0 ]; then
  hosts=(local)
  while read -r h _; do hosts+=("$h"); done < <(valid_hosts | awk '!seen[$1]++')
fi
failed=()
for h in "${hosts[@]}"; do
  if [ "$h" = local ]; then
    install -D -m 0755 "$src" "$HOME/.local/bin/job"
    got="$(sha256sum "$HOME/.local/bin/job" | cut -d' ' -f1)"
  else
    d="$(dest_of "$h")"
    if [ -z "$d" ]; then echo "$h: FAILED: not a host in $conf" >&2; failed+=("$h"); continue; fi
    # the file goes over stdin; nothing but fixed text is in the ssh command line
    ssh -o BatchMode=yes -- "$d" 'mkdir -p ~/.local/bin && cat > ~/.local/bin/job.new && chmod 0755 ~/.local/bin/job.new && mv ~/.local/bin/job.new ~/.local/bin/job' < "$src" \
      && got="$(ssh -o BatchMode=yes -- "$d" 'sha256sum ~/.local/bin/job' | cut -d' ' -f1)" || got="(install failed)"
  fi
  if [ "$got" = "$want" ]; then
    echo "$h: $ver at ~/.local/bin/job, sha256 ${want:0:16}... matches"
  else
    echo "$h: FAILED: sha256 $got, expected $want" >&2
    failed+=("$h")
  fi
done
if [ ${#failed[@]} -gt 0 ]; then
  echo "not installed on: ${failed[*]}; hosts now run different versions; rerun ./install.sh ${failed[*]}" >&2
  exit 1
fi
