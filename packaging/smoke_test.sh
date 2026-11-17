#!/usr/bin/env bash
# Open a built disk image and check the app inside it works: it is signed, it
# starts, serves the page, parses a resume, exports a one-page PDF that passes
# its own self-check (which is what notices a font missing from the bundle),
# reads that PDF back in as a resume (which needs the whole PDF stack), and
# quits when asked. What someone double-clicking it gets, short of the
# browser window.
#
# usage: packaging/smoke_test.sh dist/Resumap-<version>-macos-<arch>.dmg
set -euo pipefail
dmg=${1:?usage: $0 <path to .dmg>}
port=${RESUMAP_SMOKE_PORT:-8799}
base="http://127.0.0.1:$port"
mnt=$(mktemp -d)
home=$(mktemp -d)
pid=

cleanup() {
  if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; fi
  hdiutil detach "$mnt" -force -quiet 2>/dev/null || true
  rm -rf "$home"
}
trap cleanup EXIT
fail() {
  echo "error: $*" >&2
  if [ -f "$home/server.log" ]; then tail -40 "$home/server.log" >&2; fi
  exit 1
}
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

hdiutil attach "$dmg" -nobrowse -readonly -noautoopen -mountpoint "$mnt" -quiet
app="$mnt/Resumap.app"
[ -L "$mnt/Applications" ] || fail "the image has no Applications link to drag onto"
codesign --verify --deep --strict "$app" || fail "the app's signature does not verify"
echo "ok   image mounts, app is signed"

# The frozen app keeps its data under ~/Library/Application Support/Resumap; a
# throwaway HOME keeps this run away from a real one.
HOME="$home" "$app/Contents/MacOS/Resumap" --serve --port "$port" >"$home/server.log" 2>&1 &
pid=$!
for _ in $(seq 1 120); do
  curl -fsS -o /dev/null "$base/api/status" 2>/dev/null && break
  kill -0 "$pid" 2>/dev/null || fail "the app exited before it answered"
  sleep 1
done
curl -fsS -o /dev/null "$base/api/status" || fail "the app did not answer within 120s"
curl -fsS "$base/" | grep -q "<title>Resumap</title>" || fail "the page is not served"
echo "ok   server answers and serves the page"

cat >"$home/resume.txt" <<'TXT'
Jane Doe
jane.doe@example.com | (555) 010-0000

EXPERIENCE
Data Engineer, Example Corp, 2021 - Present
- Built a streaming ingestion pipeline in Python and Kafka for 2 million events a day.
- Reduced batch runtime by 40% by tuning Spark jobs and their partitioning.
- Led four engineers through a migration from cron scripts to Airflow.

EDUCATION
B.S. Computer Science, Example University, 2021
TXT
resume=$(curl -fsS -F "file=@$home/resume.txt" -F "label=Smoke test" "$base/api/resumes" | json 'd["id"]') ||
  fail "uploading a resume failed"
job=$(curl -fsS -H 'Content-Type: application/json' "$base/api/jobs" -d "{\"resume_id\": $resume, \"text\": \"Data Engineer\\n- Build streaming pipelines with Kafka and Python.\\n- Tune Spark batch jobs for cost and speed.\\n- Orchestrate workflows with Airflow.\"}" | json 'd["id"]') ||
  fail "parsing a job description failed"
export=$(curl -fsS -X POST "$base/api/jobs/$job/export") || fail "exporting the PDF failed"
check=$(echo "$export" | json '"pages=%s ok=%s problems=%s" % (d["pages"], d["check"]["ok"], d["check"]["problems"])')
[ "$check" = "pages=1 ok=True problems=[]" ] || fail "the exported PDF failed its self-check: $check"
echo "ok   resume parsed, PDF exported, self-check clean ($check)"

name=$(echo "$export" | json 'd["filename"]')
curl -fsS -o "$home/exported.pdf" "$base/api/download/$name" || fail "downloading the export failed"
points=$(curl -fsS -F "file=@$home/exported.pdf" -F "label=Round trip" "$base/api/resumes" |
  json 'sum(1 for b in d["blocks"] if b["mappable"])') || fail "the app could not read a PDF resume"
[ "$points" -ge 3 ] || fail "the exported PDF read back with $points points, expected 3"
echo "ok   PDF resume read back ($points points)"

curl -fsS -o /dev/null -X POST "$base/api/quit" || fail "Quit was refused"
for _ in $(seq 1 20); do
  kill -0 "$pid" 2>/dev/null || { pid=; break; }
  sleep 0.5
done
[ -z "$pid" ] || fail "the app did not stop after Quit"
echo "ok   Quit stops it"
