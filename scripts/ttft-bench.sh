#!/usr/bin/env bash
set -u

# 用流式 chat/completions 测 TTFT，依赖 curl 和 python3。
# 默认低并发，避免误伤线上；通过环境变量调大 TOTAL/CONCURRENCY。

BASE_URL="${BASE_URL:-https://ai.iootx.com}"
API_KEY="${API_KEY:-${NEW_API_KEY:-}}"
MODEL="${MODEL:-deepseek-v4-flash-ga-260731}"
TOTAL="${TOTAL:-20}"
CONCURRENCY="${CONCURRENCY:-2}"
WARMUP="${WARMUP:-2}"
CACHE_LINES="${CACHE_LINES:-260}"
MAX_TIME="${MAX_TIME:-180}"
CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-10}"
PROMPT_CACHE_KEY="${PROMPT_CACHE_KEY:-ttft-bench-${MODEL}}"
OUT_DIR="${OUT_DIR:-/tmp/new-api-ttft-bench-$(date +%Y%m%d%H%M%S)}"

die() {
  echo "error: $*" >&2
  exit 1
}

need_uint() {
  case "${2}" in
    ''|*[!0-9]*) die "${1} must be a positive integer" ;;
  esac
  [ "${2}" -gt 0 ] || die "${1} must be greater than 0"
}

need_uint TOTAL "${TOTAL}"
need_uint CONCURRENCY "${CONCURRENCY}"
need_uint WARMUP "${WARMUP}"
need_uint CACHE_LINES "${CACHE_LINES}"

[ -n "${API_KEY}" ] || die "set API_KEY or NEW_API_KEY first"
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

mkdir -p "${OUT_DIR}" || die "cannot create OUT_DIR: ${OUT_DIR}"

REQUEST_FILE="${OUT_DIR}/request.json"
RESULT_FILE="${OUT_DIR}/results.tsv"
WARMUP_FILE="${OUT_DIR}/warmup.tsv"
TTFT_FILE="${OUT_DIR}/ttft.success.txt"
TOTAL_TIME_FILE="${OUT_DIR}/total.success.txt"

export MODEL PROMPT_CACHE_KEY CACHE_LINES
python3 - "${REQUEST_FILE}" <<'PY'
import json
import os
import sys

model = os.environ["MODEL"]
cache_key = os.environ["PROMPT_CACHE_KEY"]
lines = int(os.environ["CACHE_LINES"])
stable = "\n".join(
    f"stable prompt cache prefix line {i:04d}: keep this content unchanged for cache testing."
    for i in range(lines)
)

payload = {
    "model": model,
    "stream": True,
    "stream_options": {"include_usage": True},
    "prompt_cache_key": cache_key,
    "messages": [
        {
            "role": "system",
            "content": stable,
        },
        {
            "role": "user",
            "content": "Reply with exactly: ok",
        },
    ],
}

with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
PY

parse_usage() {
  python3 - "$1" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    text = open(path, "r", encoding="utf-8", errors="ignore").read()
except FileNotFoundError:
    print("0\t0")
    raise SystemExit

cached = 0
pcht = 0

def observe_usage(obj):
    global cached, pcht
    if not isinstance(obj, dict):
        return
    usage = obj.get("usage")
    if usage is None and isinstance(obj.get("response"), dict):
        usage = obj["response"].get("usage")
    if not isinstance(usage, dict):
        return
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = max(cached, int(details.get("cached_tokens") or 0))
    input_details = usage.get("input_tokens_details")
    if isinstance(input_details, dict):
        cached = max(cached, int(input_details.get("cached_tokens") or 0))
    pcht = max(pcht, int(usage.get("prompt_cache_hit_tokens") or 0))

for raw in text.splitlines():
    line = raw.strip()
    if line.startswith("data:"):
        line = line[5:].strip()
    if not line or line == "[DONE]":
        continue
    try:
        observe_usage(json.loads(line))
    except Exception:
        pass

if cached == 0 and pcht == 0:
    try:
        observe_usage(json.loads(text))
    except Exception:
        pass

print(f"{cached}\t{pcht}")
PY
}

run_one() {
  local idx="$1"
  local result_file="$2"
  local body_file="${OUT_DIR}/body_${idx}.txt"
  local err_file="${OUT_DIR}/curl_${idx}.err"
  local meta http_code connect_s ttft_s total_s rc usage cached pcht

  meta="$(
    curl --noproxy '*' -sS -N --no-buffer \
      --connect-timeout "${CONNECT_TIMEOUT}" \
      --max-time "${MAX_TIME}" \
      -o "${body_file}" \
      -w $'%{http_code}\t%{time_connect}\t%{time_starttransfer}\t%{time_total}' \
      -H "Authorization: Bearer ${API_KEY}" \
      -H "Content-Type: application/json" \
      -d @"${REQUEST_FILE}" \
      "${BASE_URL%/}/v1/chat/completions" \
      2>"${err_file}"
  )"
  rc=$?

  http_code="000"
  connect_s="0"
  ttft_s="0"
  total_s="0"
  IFS=$'\t' read -r http_code connect_s ttft_s total_s <<<"${meta}"
  usage="$(parse_usage "${body_file}")"
  IFS=$'\t' read -r cached pcht <<<"${usage}"

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "${idx}" "${http_code}" "${connect_s}" "${ttft_s}" "${total_s}" "${rc}" "${cached}" "${pcht}" >>"${result_file}"
}

summarize_series() {
  local name="$1"
  local file="$2"

  awk -v name="${name}" '
    { a[NR] = $1 + 0; sum += $1 }
    END {
      n = NR
      if (n == 0) {
        printf "%s: no successful samples\n", name
        exit
      }
      printf "%s: count=%d avg=%.3fs min=%.3fs p50=%.3fs p90=%.3fs p95=%.3fs p99=%.3fs max=%.3fs\n",
        name, n, sum / n, a[1], pct(50), pct(90), pct(95), pct(99), a[n]
    }
    function pct(p, i, x) {
      x = (p / 100) * n
      i = int(x)
      if (i < x) i++
      if (i < 1) i = 1
      if (i > n) i = n
      return a[i]
    }
  ' "${file}"
}

echo -e "idx\thttp_code\tconnect_s\tttft_s\ttotal_s\tcurl_rc\tcached_tokens\tprompt_cache_hit_tokens" >"${RESULT_FILE}"
echo -e "idx\thttp_code\tconnect_s\tttft_s\ttotal_s\tcurl_rc\tcached_tokens\tprompt_cache_hit_tokens" >"${WARMUP_FILE}"

echo "base_url=${BASE_URL}"
echo "model=${MODEL}"
echo "total=${TOTAL} concurrency=${CONCURRENCY} warmup=${WARMUP} out_dir=${OUT_DIR}"
echo "warming cache..."
for i in $(seq 1 "${WARMUP}"); do
  run_one "warmup_${i}" "${WARMUP_FILE}"
done

echo "running measured requests..."
for i in $(seq 1 "${TOTAL}"); do
  run_one "${i}" "${RESULT_FILE}" &
  while [ "$(jobs -rp | wc -l | tr -d ' ')" -ge "${CONCURRENCY}" ]; do
    wait -n 2>/dev/null || true
  done
done
wait

awk -F'\t' 'NR > 1 && $2 ~ /^2/ && $6 == 0 { print $4 }' "${RESULT_FILE}" | sort -n >"${TTFT_FILE}"
awk -F'\t' 'NR > 1 && $2 ~ /^2/ && $6 == 0 { print $5 }' "${RESULT_FILE}" | sort -n >"${TOTAL_TIME_FILE}"

echo
echo "summary"
awk -F'\t' '
  NR > 1 {
    total++
    if ($2 ~ /^2/ && $6 == 0) ok++
    else failed++
    if (($7 + 0) > 0 || ($8 + 0) > 0) cache_hit++
    if (($7 + 0) > max_cached) max_cached = $7 + 0
    if (($8 + 0) > max_pcht) max_pcht = $8 + 0
  }
  END {
    printf "requests: total=%d ok=%d failed=%d\n", total, ok, failed
    printf "cache: hit_samples=%d max_cached_tokens=%d max_prompt_cache_hit_tokens=%d\n", cache_hit, max_cached, max_pcht
  }
' "${RESULT_FILE}"
summarize_series "ttft" "${TTFT_FILE}"
summarize_series "total_time" "${TOTAL_TIME_FILE}"

echo
echo "raw_results=${RESULT_FILE}"
echo "nginx_log_hint=tail -f /data/wwwlog/ai.iootx.com.access.log"
