#!/bin/bash
# Remove sandbox containers whose episode trace has not been written for 5+ minutes (orphans of stopped
# processes). Containers whose episode trace cannot be found are left alone.
cd .; now=$(date +%s)
for n in $(docker ps --format '{{.Names}}' | grep '^pf-'); do
  ep=$(echo $n | sed -E 's/^pf-(.*)-[0-9a-f]{8}$/\1/; s/-pristine$//'); f=traces/$ep.jsonl
  [ -f $f ] || continue
  age=$(( now - $(stat -c %Y $f) ))
  [ $age -gt 300 ] && docker rm -f $n >/dev/null && echo "reaped $n ($age s)"
done
