#!/usr/bin/env python3
"""Offline call-log analyzer for the Voice-Agent project.

This script is intentionally standalone and read-only. It does not modify
project runtime behavior or Airtable data.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


ISO_PREFIX_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}[^\s]*)\s+(\{.*\})\s*$")
TIME_TOKEN_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")


def parse_ts(text: str) -> datetime | None:
    cleaned = text.strip()
    if not cleaned:
        return None

    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"

    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def parse_json_line(raw_line: str) -> tuple[datetime | None, dict[str, Any] | None]:
    line = raw_line.strip()
    if not line:
        return None, None

    # Case 1: pure JSON line.
    if line.startswith("{") and line.endswith("}"):
        try:
            return None, json.loads(line)
        except json.JSONDecodeError:
            return None, None

    # Case 2: timestamp-prefixed JSON line.
    match = ISO_PREFIX_RE.match(line)
    if match:
        ts = parse_ts(match.group(1))
        try:
            return ts, json.loads(match.group(2))
        except json.JSONDecodeError:
            return None, None

    return None, None


def parse_embedded_json(text: str) -> dict[str, Any] | None:
    try:
        return json.loads(text)
    except Exception:
        return None


def looks_like_slot_dump(text: str, threshold: int = 5) -> bool:
    return len(TIME_TOKEN_RE.findall(text or "")) >= threshold


def has_format_artifacts(text: str) -> bool:
    if not text:
        return False
    if "**" in text:
        return True
    if re.search(r"(^|\n)\s*[-*]\s+", text):
        return True
    if re.search(r"(^|\n)\s*\d+\.\s*", text):
        return True
    return False


def booking_confirmation_text(text: str) -> bool:
    normalized = (text or "").lower()
    return "appointment" in normalized and "book" in normalized and "success" in normalized


@dataclass
class CallStats:
    request_id: str
    user_turns: int = 0
    assistant_turns: int = 0
    booking_attempts: int = 0
    booking_successes: int = 0
    booking_conflicts: int = 0
    doctor_not_found_errors: int = 0
    slot_dump_messages: int = 0
    format_artifact_messages: int = 0
    first_user_ts: datetime | None = None
    first_book_req_ts: datetime | None = None
    first_book_resp_ts: datetime | None = None
    first_booking_confirm_ts: datetime | None = None


def latency_seconds(start: datetime | None, end: datetime | None) -> float | None:
    if not start or not end:
        return None
    return (end - start).total_seconds()


def run_analysis(log_path: Path) -> dict[str, Any]:
    calls: list[CallStats] = []
    current: CallStats | None = None

    # Track duplicates by successful doctor/date/time tuples.
    success_slots: Counter[tuple[str, str, str]] = Counter()

    # Temporary cache from FunctionCallRequest(id) -> requested args.
    request_args_by_id: dict[str, dict[str, Any]] = {}

    ignored_non_json = 0

    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        ts, event = parse_json_line(line)
        if event is None:
            ignored_non_json += 1
            continue

        event_type = event.get("type")

        if event_type == "Welcome":
            request_id = event.get("request_id") or f"call-{len(calls) + 1}"
            current = CallStats(request_id=request_id)
            calls.append(current)
            continue

        if current is None:
            # Ignore events before first Welcome.
            continue

        if event_type == "ConversationText":
            role = event.get("role")
            content = event.get("content") or ""
            if role == "user":
                current.user_turns += 1
                if current.first_user_ts is None:
                    current.first_user_ts = ts
            elif role == "assistant":
                current.assistant_turns += 1
                if looks_like_slot_dump(content):
                    current.slot_dump_messages += 1
                if has_format_artifacts(content):
                    current.format_artifact_messages += 1
                if booking_confirmation_text(content) and current.first_booking_confirm_ts is None:
                    current.first_booking_confirm_ts = ts
            continue

        if event_type == "FunctionCallRequest":
            for fn in event.get("functions", []):
                fn_name = fn.get("name")
                fn_id = fn.get("id")
                raw_args = fn.get("arguments")
                parsed_args = parse_embedded_json(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                if fn_id and isinstance(parsed_args, dict):
                    request_args_by_id[fn_id] = parsed_args

                if fn_name == "book_appointment":
                    current.booking_attempts += 1
                    if current.first_book_req_ts is None:
                        current.first_book_req_ts = ts
            continue

        if event_type == "FunctionCallResponse":
            fn_name = event.get("name")
            payload = event.get("content")
            parsed = parse_embedded_json(payload) if isinstance(payload, str) else (payload or {})
            if not isinstance(parsed, dict):
                continue

            if fn_name == "get_available_slots":
                if str(parsed.get("error", "")).lower().startswith("doctor not found"):
                    current.doctor_not_found_errors += 1
                continue

            if fn_name == "book_appointment":
                if current.first_book_resp_ts is None:
                    current.first_book_resp_ts = ts

                error_text = str(parsed.get("error", "")).lower()
                is_conflict = "already booked" in error_text or "not available" in error_text
                if is_conflict:
                    current.booking_conflicts += 1
                    continue

                status = str(parsed.get("status", "")).lower()
                is_success = status == "booked" or "appointment_id" in parsed
                if is_success:
                    current.booking_successes += 1

                    doctor = str(parsed.get("doctor") or "").strip()
                    date_text = str(parsed.get("date") or "").strip()
                    time_text = str(parsed.get("time") or "").strip()

                    if not (doctor and date_text and time_text):
                        req_id = event.get("id")
                        req_args = request_args_by_id.get(req_id or "", {})
                        doctor = doctor or str(req_args.get("doctor_name") or "").strip()
                        date_text = date_text or str(req_args.get("appointment_date") or "").strip()
                        time_text = time_text or str(req_args.get("appointment_time") or "").strip()

                    if doctor and date_text and time_text:
                        success_slots[(doctor, date_text, time_text)] += 1

    total_calls = len(calls)
    total_booking_attempts = sum(c.booking_attempts for c in calls)
    total_booking_successes = sum(c.booking_successes for c in calls)
    total_booking_conflicts = sum(c.booking_conflicts for c in calls)

    duplicate_successes = sum(count - 1 for count in success_slots.values() if count > 1)

    booking_success_rate = (
        (total_booking_successes / total_booking_attempts) * 100 if total_booking_attempts else 0.0
    )

    conflict_block_rate = (
        (total_booking_conflicts / total_booking_attempts) * 100 if total_booking_attempts else 0.0
    )

    avg_user_turns = (sum(c.user_turns for c in calls) / total_calls) if total_calls else 0.0
    avg_assistant_turns = (sum(c.assistant_turns for c in calls) / total_calls) if total_calls else 0.0

    # Latencies are only available when timestamp-prefixed logs are used.
    req_to_resp = [
        latency_seconds(c.first_book_req_ts, c.first_book_resp_ts)
        for c in calls
        if latency_seconds(c.first_book_req_ts, c.first_book_resp_ts) is not None
    ]
    user_to_confirm = [
        latency_seconds(c.first_user_ts, c.first_booking_confirm_ts)
        for c in calls
        if latency_seconds(c.first_user_ts, c.first_booking_confirm_ts) is not None
    ]

    def avg(values: list[float]) -> float | None:
        return (sum(values) / len(values)) if values else None

    return {
        "file": str(log_path),
        "calls": total_calls,
        "ignored_non_json_lines": ignored_non_json,
        "booking_attempts": total_booking_attempts,
        "booking_successes": total_booking_successes,
        "booking_conflicts": total_booking_conflicts,
        "booking_success_rate_percent": round(booking_success_rate, 2),
        "conflict_block_rate_percent": round(conflict_block_rate, 2),
        "duplicate_successful_bookings_detected": duplicate_successes,
        "doctor_not_found_errors": sum(c.doctor_not_found_errors for c in calls),
        "slot_dump_messages": sum(c.slot_dump_messages for c in calls),
        "format_artifact_messages": sum(c.format_artifact_messages for c in calls),
        "avg_user_turns_per_call": round(avg_user_turns, 2),
        "avg_assistant_turns_per_call": round(avg_assistant_turns, 2),
        "avg_book_request_to_response_seconds": (round(avg(req_to_resp), 3) if avg(req_to_resp) is not None else None),
        "avg_user_to_booking_confirmation_seconds": (
            round(avg(user_to_confirm), 3) if avg(user_to_confirm) is not None else None
        ),
        "notes": [
            "Latency metrics require timestamp-prefixed JSON log lines.",
            "No runtime project files were modified by this analyzer.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze Voice-Agent call logs without changing project behavior.")
    parser.add_argument("--log", required=True, help="Path to the merged runtime log file.")
    parser.add_argument("--out", help="Optional path to save analysis JSON output.")
    args = parser.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        raise SystemExit(f"Log file not found: {log_path}")

    report = run_analysis(log_path)
    rendered = json.dumps(report, indent=2)
    print(rendered)

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(rendered + "\n", encoding="utf-8")
        print(f"Saved report: {out_path}")


if __name__ == "__main__":
    main()
