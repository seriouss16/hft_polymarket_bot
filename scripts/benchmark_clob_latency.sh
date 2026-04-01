#!/bin/bash
# Simple HTTPS latency benchmark to Polymarket CLOB using curl
# No Python dependencies required

CLOB_URL="https://clob.polymarket.com/"
RUNS=${1:-20}
OUTPUT_DIR="hft_bot/reports/banch_lag"

mkdir -p "$OUTPUT_DIR"

echo "🎯 Target: $CLOB_URL"
echo "📊 Runs: $RUNS"
echo "⏱️  Starting benchmark..."
echo "----------------------------------------"

# Array to store latencies
declare -a latencies

for i in $(seq 1 $RUNS); do
    start_time=$(date +%s%N)
    
    # Measure HTTPS latency
    latency=$(curl -o /dev/null -s -w '%{time_total}' --connect-timeout 5 --max-time 10 "$CLOB_URL" 2>/dev/null)
    
    end_time=$(date +%s%N)
    
    if [ -n "$latency" ] && [ "$latency" != "0.000000" ]; then
        # Convert to milliseconds
        latency_ms=$(echo "scale=1; $latency * 1000" | bc)
        latencies+=("$latency_ms")
        echo "✅ Run $i: ${latency_ms}ms"
    else
        echo "❌ Run $i: FAILED"
    fi
done

echo "----------------------------------------"

# Calculate statistics
count=${#latencies[@]}

if [ $count -eq 0 ]; then
    echo "❌ No successful measurements!"
    exit 1
fi

# Sort latencies
IFS=$'\n' sorted=($(sort -n <<<"${latencies[*]}")); unset IFS

min=${sorted[0]}
max=${sorted[$((count-1))]}

# Calculate sum for mean
sum=0
for lat in "${latencies[@]}"; do
    sum=$(echo "$sum + $lat" | bc)
done
mean=$(echo "scale=1; $sum / $count" | bc)

# Median
mid=$((count / 2))
if [ $((count % 2)) -eq 0 ]; then
    median=$(echo "scale=1; (${sorted[$mid-1]} + ${sorted[$mid]}) / 2" | bc)
else
    median=${sorted[$mid]}
fi

# P95 index
p95_idx=$(echo "scale=0; $count * 95 / 100" | bc)
p95=${sorted[$p95_idx]}

# P99 index
p99_idx=$(echo "scale=0; $count * 99 / 100" | bc)
p99=${sorted[$p99_idx]}

echo ""
echo "=================================================="
echo "📈 LATENCY RESULTS"
echo "=================================================="
echo "Samples:    $count"
echo "Min:        ${min} ms"
echo "Max:        ${max} ms"
echo "Mean:       ${mean} ms"
echo "Median:     ${median} ms"
echo "P95:        ${p95} ms"
echo "P99:        ${p99} ms"
echo "=================================================="

# Save to file
timestamp=$(date +%y%m%d_%H%M%S)
md_file="$OUTPUT_DIR/clob_latency_${timestamp}.md"
csv_file="$OUTPUT_DIR/clob_latency_${timestamp}.csv"

# Write markdown report
cat > "$md_file" << EOF
# CLOB HTTPS Latency Report

- Timestamp: $(date -Iseconds)
- Target: $CLOB_URL
- Samples: $count

## Statistics

| Metric | Value |
|--------|-------|
| Min | ${min} ms |
| Max | ${max} ms |
| Mean | ${mean} ms |
| Median | ${median} ms |
| P95 | ${p95} ms |
| P99 | ${p99} ms |

## Interpretation

EOF

# Add interpretation
if (( $(echo "$mean < 15" | bc -l) )); then
    echo "✅ **Excellent** - You're close to Polymarket servers. Ready for migration to Ireland/London." >> "$md_file"
elif (( $(echo "$mean < 30" | bc -l) )); then
    echo "✅ **Good** - Acceptable latency. Ireland/London migration would add ~5-10ms improvement." >> "$md_file"
elif (( $(echo "$mean < 50" | bc -l) )); then
    echo "⚠️ **Moderate** - Consider migration to Ireland/London for <15ms latency." >> "$md_file"
else
    echo "❌ **High** - Significant latency. Migration to Ireland/London strongly recommended." >> "$md_file"
fi

# Write CSV
echo "run,timestamp,latency_ms" > "$csv_file"
i=1
for lat in "${latencies[@]}"; do
    echo "$i,$(date -Iseconds),$lat" >> "$csv_file"
    i=$((i + 1))
done

echo ""
echo "📁 Reports saved to:"
echo "   $md_file"
echo "   $csv_file"
echo ""
echo "✅ Benchmark complete!"
