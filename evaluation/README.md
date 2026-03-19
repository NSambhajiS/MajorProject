# Voice-Agent Evaluation (No Runtime Changes)

This folder contains an offline evaluator for your research paper.
It does not modify `main.py`, `config.json`, or booking logic.

## What You Can Measure

- Task-level booking success rate
- Conflict handling behavior (slot unavailable cases)
- Duplicate successful bookings detected
- Doctor name recognition issues ("Doctor not found")
- UX-quality proxy metrics:
  - slot dump messages (assistant reading long slot lists)
  - formatting artifact messages (`**`, bullets, numbered list style)
- Turn efficiency (avg user/assistant turns per call)
- Optional latency metrics (if logs include timestamps)

## 1) Capture Logs

Run your project exactly as usual and save terminal output to a file.

Example (Git Bash):

```bash
python main.py | tee evaluation/run.log
```

Optional timestamped logs (for response-time metrics):

```bash
python main.py 2>&1 | awk '{ print strftime("%Y-%m-%dT%H:%M:%S"), $0; fflush(); }' | tee evaluation/run_ts.log
```

Then perform your test calls.

## 2) Run Analyzer

```bash
python evaluation/analyze_calls.py --log evaluation/run.log --out evaluation/report.json
```

For timestamped logs:

```bash
python evaluation/analyze_calls.py --log evaluation/run_ts.log --out evaluation/report_ts.json
```

## 3) Use in Research Paper

Recommended metrics table:

- `booking_success_rate_percent`
- `conflict_block_rate_percent`
- `duplicate_successful_bookings_detected`
- `doctor_not_found_errors`
- `slot_dump_messages`
- `format_artifact_messages`
- `avg_user_turns_per_call`
- `avg_assistant_turns_per_call`
- `avg_book_request_to_response_seconds` (if timestamps available)
- `avg_user_to_booking_confirmation_seconds` (if timestamps available)

## Notes

- This is black-box evaluation from logs and does not impact production behavior.
- Latency fields are `null` when timestamps are not present.
- If needed, keep separate logs per experiment scenario (normal booking, conflict booking, noisy input).
