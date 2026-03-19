import os
from datetime import date, datetime, time, timedelta
import re
import uuid

import requests
from dotenv import load_dotenv

load_dotenv()


def _env_required(name):
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _parse_time(value):
    return datetime.strptime(value, "%H:%M").time()


def _slot_minutes():
    return int(os.getenv("SLOT_MINUTES", "20"))


def _open_time():
    return _parse_time(os.getenv("HOSPITAL_OPEN_TIME", "10:00"))


def _close_time():
    return _parse_time(os.getenv("HOSPITAL_CLOSE_TIME", "18:00"))


def _closed_weekday():
    return int(os.getenv("HOSPITAL_CLOSED_WEEKDAY", "6"))


def _doctor_map():
    return {
        os.getenv("DOCTOR_1_NAME", "Dr First").strip().lower(): os.getenv("DOCTOR_1_NAME", "Dr First").strip(),
        os.getenv("DOCTOR_2_NAME", "Dr Second").strip().lower(): os.getenv("DOCTOR_2_NAME", "Dr Second").strip(),
    }


def _status_field_name():
    return os.getenv("AIRTABLE_STATUS_FIELD", "status copy")


def _normalize_doctor_name(doctor_name):
    if not doctor_name:
        return None
    doctors = _doctor_map()
    normalized = doctor_name.strip().lower().replace(".", "")
    normalized = re.sub(r"\s+", " ", normalized)
    direct = doctors.get(normalized)
    if direct:
        return direct

    # Accept natural variants from speech like "doctor first" and map to "Dr First".
    normalized = re.sub(r"\bdoctor\b", "dr", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return doctors.get(normalized)


def _parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def _parse_slot_time(value):
    return datetime.strptime(value, "%H:%M").time()


def _is_slot_boundary(slot):
    open_t = _open_time()
    open_minutes = open_t.hour * 60 + open_t.minute
    slot_minutes = slot.hour * 60 + slot.minute
    diff = slot_minutes - open_minutes
    return diff >= 0 and diff % _slot_minutes() == 0


def _is_hospital_open(appointment_date, appointment_time):
    if appointment_date.weekday() == _closed_weekday():
        return False, "Hospital is closed on this day."

    open_t = _open_time()
    close_t = _close_time()
    if appointment_time < open_t or appointment_time >= close_t:
        return False, f"Hospital timings are {open_t.strftime('%H:%M')} to {close_t.strftime('%H:%M')}."

    if not _is_slot_boundary(appointment_time):
        return False, f"Appointments are available every {_slot_minutes()} minutes only."

    return True, "Open"


def _airtable_headers():
    token = _env_required("AIRTABLE_API_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _airtable_url():
    base_id = _env_required("AIRTABLE_BASE_ID")
    table_name = _env_required("AIRTABLE_TABLE_NAME")
    return f"https://api.airtable.com/v0/{base_id}/{table_name}"


def _airtable_list_records(filter_formula=None, max_records=100):
    params = {"maxRecords": max_records}
    if filter_formula:
        params["filterByFormula"] = filter_formula

    response = requests.get(
        _airtable_url(),
        headers=_airtable_headers(),
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    return response.json().get("records", [])


def _airtable_create_record(fields):
    response = requests.post(
        _airtable_url(),
        headers=_airtable_headers(),
        json={"fields": fields},
        timeout=20,
    )
    if not response.ok:
        raise Exception(f"Airtable create failed: {response.status_code} - {response.text}")
    return response.json()


def _airtable_update_record(record_id, fields):
    response = requests.patch(
        f"{_airtable_url()}/{record_id}",
        headers=_airtable_headers(),
        json={"fields": fields},
        timeout=20,
    )
    if not response.ok:
        raise Exception(f"Airtable update failed: {response.status_code} - {response.text}")
    return response.json()


def _normalize_date_text(value):
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()

    text = str(value).strip()
    if not text:
        return None

    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        pass

    # Handles ISO datetime strings if Airtable returns date-time formatted values.
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text


def _normalize_time_text(value):
    if value is None:
        return None
    if isinstance(value, time):
        return value.strftime("%H:%M")

    text = str(value).strip()
    if not text:
        return None

    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(text, fmt).strftime("%H:%M")
        except ValueError:
            continue

    return text


def _booked_records_for_doctor_date(doctor_name, appointment_date):
    status_field = _status_field_name()
    records = _records_for_doctor_date(doctor_name, appointment_date)
    return [
        record
        for record in records
        if str(record.get("fields", {}).get(status_field) or "").strip().lower() == "booked"
    ]


def _records_for_doctor_date(doctor_name, appointment_date):
    records = _airtable_list_records(max_records=1000)
    target_date = appointment_date.isoformat()

    filtered = []
    for record in records:
        fields = record.get("fields", {})
        record_doctor = str(fields.get("doctor_name") or "").strip()
        record_date = _normalize_date_text(fields.get("appointment_date"))

        if record_doctor == doctor_name and record_date == target_date:
            filtered.append(record)

    return filtered


def _is_cancelled_status(status_value):
    return str(status_value or "").strip().lower() == "cancelled"


def _is_doctor_slot_available(doctor_name, appointment_date, appointment_time):
    records = _records_for_doctor_date(doctor_name, appointment_date)
    requested_time = appointment_time.strftime("%H:%M")
    status_field = _status_field_name()

    for record in records:
        fields = record.get("fields", {})
        existing_time = _normalize_time_text(fields.get("appointment_time"))
        status_value = fields.get(status_field)
        if existing_time == requested_time and not _is_cancelled_status(status_value):
            return False
    return True


def _other_doctors_available_same_slot(current_doctor, appointment_date, appointment_time):
    alternatives = []
    for doctor in _doctor_map().values():
        if doctor == current_doctor:
            continue
        if _is_doctor_slot_available(doctor, appointment_date, appointment_time):
            alternatives.append(doctor)
    return alternatives


def get_supported_doctors():
    doctors = list(_doctor_map().values())
    return {"doctors": doctors}


def get_available_slots(doctor_name, appointment_date):
    try:
        normalized_doctor = _normalize_doctor_name(doctor_name)
        if not normalized_doctor:
            return {"error": "Doctor not found", "supported_doctors": list(_doctor_map().values())}

        booking_date = _parse_date(appointment_date)
        open_result, reason = _is_hospital_open(booking_date, _open_time())
        if not open_result:
            return {"doctor": normalized_doctor, "date": appointment_date, "available_slots": [], "message": reason}

        records = _records_for_doctor_date(normalized_doctor, booking_date)
        status_field = _status_field_name()
        booked_times = {
            _normalize_time_text(record.get("fields", {}).get("appointment_time"))
            for record in records
            if record.get("fields", {}).get("appointment_time")
            and not _is_cancelled_status(record.get("fields", {}).get(status_field))
        }

        slots = []
        cursor = datetime.combine(booking_date, _open_time())
        end_dt = datetime.combine(booking_date, _close_time())

        while cursor < end_dt:
            slot = cursor.time().strftime("%H:%M")
            if slot not in booked_times:
                slots.append(slot)
            cursor += timedelta(minutes=_slot_minutes())

        return {
            "doctor": normalized_doctor,
            "date": appointment_date,
            "available_slots": slots,
            "total_available": len(slots),
        }
    except Exception as exc:
        return {"error": f"Failed to fetch available slots: {str(exc)}"}


def book_appointment(doctor_name, patient_name, phone, appointment_date, appointment_time):
    try:
        normalized_doctor = _normalize_doctor_name(doctor_name)
        if not normalized_doctor:
            return {"error": "Doctor not found", "supported_doctors": list(_doctor_map().values())}

        booking_date = _parse_date(appointment_date)
        booking_time = _parse_slot_time(appointment_time)

        is_open, reason = _is_hospital_open(booking_date, booking_time)
        if not is_open:
            return {"error": reason}

        requested_time = booking_time.strftime("%H:%M")
        generated_appointment_id = f"APT-{uuid.uuid4().hex[:8].upper()}"

        if not _is_doctor_slot_available(normalized_doctor, booking_date, booking_time):
            return {
                "error": (
                    f"Slot {requested_time} is already booked for {normalized_doctor}. "
                    "Please choose another slot."
                ),
                "doctor": normalized_doctor,
                "date": appointment_date,
                "time": requested_time,
            }

        created = _airtable_create_record(
            {
                "appointment_id": generated_appointment_id,
                "doctor_name": normalized_doctor,
                "patient_name": patient_name,
                "phone": phone,
                "appointment_date": booking_date.isoformat(),
                "appointment_time": requested_time,
                _status_field_name(): "Booked",
            }
        )

        appointment_record_id = created.get("id")

        return {
            "appointment_id": created.get("fields", {}).get("appointment_id", generated_appointment_id),
            "airtable_record_id": appointment_record_id,
            "doctor": normalized_doctor,
            "patient_name": patient_name,
            "phone": phone,
            "date": booking_date.isoformat(),
            "time": requested_time,
            "status": "Booked",
            "message": "Appointment booked successfully.",
        }
    except Exception as exc:
        return {"error": f"Failed to book appointment: {str(exc)}"}


def list_doctor_appointments(doctor_name, appointment_date):
    try:
        normalized_doctor = _normalize_doctor_name(doctor_name)
        if not normalized_doctor:
            return {"error": "Doctor not found", "supported_doctors": list(_doctor_map().values())}

        booking_date = _parse_date(appointment_date)
        records = _booked_records_for_doctor_date(normalized_doctor, booking_date)

        appointments = []
        for record in records:
            fields = record.get("fields", {})
            appointments.append(
                {
                    "appointment_id": record.get("id"),
                    "doctor": fields.get("doctor_name"),
                    "patient_name": fields.get("patient_name"),
                    "phone": fields.get("phone"),
                    "date": fields.get("appointment_date"),
                    "time": fields.get("appointment_time"),
                    "status": fields.get(_status_field_name()),
                }
            )

        appointments.sort(key=lambda item: item.get("time") or "")

        return {
            "doctor": normalized_doctor,
            "date": booking_date.isoformat(),
            "appointments": appointments,
            "total": len(appointments),
        }
    except Exception as exc:
        return {"error": f"Failed to list appointments: {str(exc)}"}


def cancel_appointment(appointment_id):
    try:
        updated = _airtable_update_record(appointment_id, {_status_field_name(): "Cancelled"})
        fields = updated.get("fields", {})
        return {
            "appointment_id": updated.get("id"),
            "doctor": fields.get("doctor_name"),
            "date": fields.get("appointment_date"),
            "time": fields.get("appointment_time"),
            "status": fields.get(_status_field_name()),
            "message": "Appointment cancelled successfully.",
        }
    except Exception as exc:
        return {"error": f"Failed to cancel appointment: {str(exc)}"}


FUNCTION_MAP = {
    "get_supported_doctors": get_supported_doctors,
    "get_available_slots": get_available_slots,
    "book_appointment": book_appointment,
    "list_doctor_appointments": list_doctor_appointments,
    "cancel_appointment": cancel_appointment,
}