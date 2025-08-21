#!/usr/bin/env python3
"""
python_5.7_fixed.py

Improves Evolution integration:
- Uses correct EDataServer source extensions for calendars and task lists
- Wraps VEVENT/VTODO inside a VCALENDAR for create_object_sync
- Robust datetime parsing for "Date" and "Next Session"
- Picks latest row by parsed Date
- Safer 1-hour duration calculation
- Lists available sources on not-found; clearer logging

Requirements (Ubuntu packages):
- gir1.2-ecal-2.0 gir1.2-edataserver-1.2 gir1.2-icalglib-3.0
- python3-gi, python3-gi-cairo

Optional (pip):
- python-dateutil (if installed, will be used for more robust parsing)
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple
import uuid

import gi

gi.require_version('ECal', '2.0')
gi.require_version('ICalGLib', '3.0')
gi.require_version('EDataServer', '1.2')
gi.require_version('Gio', '2.0')
from gi.repository import ECal, ICalGLib, EDataServer, Gio  # type: ignore

try:
	from dateutil import parser as dateutil_parser  # type: ignore
	HAVE_DATEUTIL = True
except Exception:
	HAVE_DATEUTIL = False

from odf.opendocument import load  # type: ignore
from odf.table import Table, TableRow, TableCell  # type: ignore
from odf.text import P  # type: ignore

ODS_PATH = os.path.expanduser("~/Client_Tracking/Client_Session_Data_2025_08.ods")
CALENDAR_NAME = "Work"      # Evolution calendar display name
TASKS_NAME    = "Personal"  # Evolution tasks list display name


# --- Logging helpers ---

def log_info(msg: str) -> None:
	print(f"[INFO] {msg}")

def log_error(msg: str) -> None:
	print(f"[ERROR] {msg}")

def log_warn(msg: str) -> None:
	print(f"[WARN] {msg}")


def ical_escape(text: str) -> str:
	"""Escape text for iCalendar (RFC 5545 minimal set)."""
	return (
		text.replace("\\", "\\\\")
			.replace("\n", "\\n")
			.replace(",", "\\,")
			.replace(";", "\\;")
	)


def format_dt_as_utc_ical(dt: datetime) -> str:
	"""Format a datetime as UTC in iCal DATE-TIME form: YYYYMMDDTHHMMSSZ.
	If naive, assume local time and convert to UTC.
	"""
	if dt.tzinfo is None:
		local_tz = datetime.now().astimezone().tzinfo
		dt = dt.replace(tzinfo=local_tz)
	dt_utc = dt.astimezone(timezone.utc)
	return dt_utc.strftime("%Y%m%dT%H%M%SZ")

# --- ODS reading ---

def read_ods_rows(path: str) -> List[List[str]]:
	doc = load(path)
	rows: List[List[str]] = []
	for table in doc.getElementsByType(Table):
		for row in table.getElementsByType(TableRow):
			cells = row.getElementsByType(TableCell)
			if not cells:
				continue
			values: List[str] = []
			for c in cells:
				txts = [p.firstChild.data for p in c.getElementsByType(P) if getattr(p, 'firstChild', None)]
				values.append(" ".join(txts) if txts else "")
			if values and values[0] != "Client Name":
				rows.append(values)
	return rows


def parse_rows(raw_rows: List[List[str]]) -> List[Dict[str, str]]:
	"""Convert row lists to dicts with fixed headers, ignore too-short rows."""
	headers = [
		"Client Name","Date","Session","Hours Booked","Session Time",
		"Total Hours","Hours Remaining","Next Session","Homework"
	]
	parsed: List[Dict[str, str]] = []
	for r in raw_rows:
		if len(r) >= len(headers):
			parsed.append(dict(zip(headers, r)))
	return parsed


# --- Datetime parsing ---

FALLBACK_PATTERNS = [
	"%Y-%m-%d %H:%M:%S",
	"%Y-%m-%d %H:%M",
	"%Y/%m/%d %H:%M",
	"%d/%m/%Y %H:%M",
	"%Y-%m-%d",
	"%d/%m/%Y",
]


def parse_datetime_any(value: str) -> Optional[datetime]:
	value = (value or "").strip()
	if not value:
		return None
	if HAVE_DATEUTIL:
		try:
			return dateutil_parser.parse(value, dayfirst=False, yearfirst=True)
		except Exception:
			pass
	for fmt in FALLBACK_PATTERNS:
		try:
			dt = datetime.strptime(value, fmt)
			# If only a date was provided, default to 09:00
			if fmt in ("%Y-%m-%d", "%d/%m/%Y"):
				dt = dt.replace(hour=9, minute=0, second=0, microsecond=0)
			return dt
		except Exception:
			continue
	return None


def parse_date_for_sort(value: str) -> Optional[datetime]:
	"""Parse the Date column for selecting the latest row."""
	return parse_datetime_any(value)


# --- Evolution helpers ---

def get_source_registry() -> EDataServer.SourceRegistry:
	return EDataServer.SourceRegistry.new_sync(None)


def list_sources_display(reg: EDataServer.SourceRegistry, extension: str) -> List[Tuple[str, str]]:
	items: List[Tuple[str, str]] = []
	for s in reg.list_sources(extension):
		name = s.get_display_name()
		uuid = s.get_uid()
		items.append((name, uuid))
	return items


def find_source_by_name(reg: EDataServer.SourceRegistry, name: str, extension: str) -> Optional[EDataServer.Source]:
	for source in reg.list_sources(extension):
		if source.get_display_name() == name:
			return source
	return None


def connect_client(source: EDataServer.Source, kind: ECal.ClientSourceType) -> Optional[ECal.Client]:
	try:
		# Try common GI signatures across EDS versions
		try:
			return ECal.Client.connect_sync(source, kind, 0)
		except TypeError:
			pass
		try:
			return ECal.Client.connect_sync(source, kind, 0, None)
		except TypeError:
			pass
		try:
			return ECal.Client.connect_sync(source, kind, 0, Gio.Cancellable.new())
		except TypeError:
			pass
		# Fallbacks
		try:
			return ECal.Client.connect_sync(source, kind)
		except Exception as e:
			raise e
	except Exception as e:
		log_error(f"Failed to connect client for '{source.get_display_name()}': {e}")
		return None


def _safe_create_object(client: ECal.Client, vcal: ICalGLib.Component) -> None:
	# Try multiple create_object_sync signatures
	attempts = [
		(vcal, 0, None),
		(vcal, 0, Gio.Cancellable.new()),
		(vcal, 0),
		(vcal, None),
		(vcal,),
	]
	last_err: Optional[Exception] = None
	for args in attempts:
		try:
			client.create_object_sync(*args)
			return
		except TypeError as e:
			last_err = e
			continue
		except Exception as e:
			# Real runtime error from EDS
			raise e
	# If we exhausted signatures, raise last TypeError
	if last_err:
		raise last_err


# --- iCalendar builders (bare components) ---

def build_vevent_component(summary: str, start_dt: datetime, duration: timedelta) -> ICalGLib.Component:
	uid = str(uuid.uuid4())
	dtstamp = format_dt_as_utc_ical(datetime.now())
	dtstart = format_dt_as_utc_ical(start_dt)
	dtend = format_dt_as_utc_ical(start_dt + duration)
	ics = (
		"BEGIN:VEVENT\r\n"
		f"UID:{uid}\r\n"
		f"DTSTAMP:{dtstamp}\r\n"
		f"SUMMARY:{ical_escape(summary)}\r\n"
		f"DTSTART:{dtstart}\r\n"
		f"DTEND:{dtend}\r\n"
		"END:VEVENT\r\n"
	)
	return ICalGLib.Component.new_from_string(ics)


def build_vtodo_component(summary: str) -> ICalGLib.Component:
	uid = str(uuid.uuid4())
	dtstamp = format_dt_as_utc_ical(datetime.now())
	ics = (
		"BEGIN:VTODO\r\n"
		f"UID:{uid}\r\n"
		f"DTSTAMP:{dtstamp}\r\n"
		f"SUMMARY:{ical_escape(summary)}\r\n"
		"END:VTODO\r\n"
	)
	return ICalGLib.Component.new_from_string(ics)


# --- Evolution create helpers ---

def create_calendar_event(source: EDataServer.Source, client_name: str, next_session_text: str) -> None:
	dt = parse_datetime_any(next_session_text)
	if not dt:
		log_warn(f"Skip event. Unparsable Next Session: '{next_session_text}'")
		return
	vevent = build_vevent_component(summary=client_name, start_dt=dt, duration=timedelta(hours=1))
	client = connect_client(source, ECal.ClientSourceType.EVENTS)
	if not client:
		return
	try:
		_safe_create_object(client, vevent)
		log_info(f"Event created for {client_name} at {dt}")
	except Exception as e:
		log_error(f"Failed to create event: {e}")


def create_task(source: EDataServer.Source, summary: str) -> None:
	vtodo = build_vtodo_component(summary)
	client = connect_client(source, ECal.ClientSourceType.TASKS)
	if not client:
		return
	try:
		_safe_create_object(client, vtodo)
		log_info(f"Task created: {summary}")
	except Exception as e:
		log_error(f"Failed to create task: {e}")


# --- Main sync logic ---

def process_latest() -> None:
	if not os.path.exists(ODS_PATH):
		log_error(f"ODS not found: {ODS_PATH}")
		return

	raw = read_ods_rows(ODS_PATH)
	rows = parse_rows(raw)
	if not rows:
		log_error("No rows found in ODS")
		return

	# Pick most recent row by Date
	def row_key(r: Dict[str, str]) -> Tuple[int, str]:
		parsed = parse_date_for_sort(r.get("Date", ""))
		# Use timestamp as int for comparison; None sorts lowest
		return (int(parsed.timestamp()) if parsed else -1, r.get("Date", ""))

	latest = max(rows, key=row_key)

	client_name = latest.get("Client Name", "").strip() or "Unknown Client"
	next_session = (latest.get("Next Session", "") or "").strip()
	homework = (latest.get("Homework", "") or "").strip()

	reg = get_source_registry()

	# Calendar event if Next Session has a datetime-looking value
	if next_session and next_session != "?":
		source = find_source_by_name(reg, CALENDAR_NAME, EDataServer.SOURCE_EXTENSION_CALENDAR)
		if source:
			create_calendar_event(source, client_name, next_session)
		else:
			log_error(f"Calendar '{CALENDAR_NAME}' not found.")
			available = list_sources_display(reg, EDataServer.SOURCE_EXTENSION_CALENDAR)
			if available:
				log_info("Available calendars:")
				for name, uid in available:
					print(f"  - {name} (uid={uid})")
			else:
				log_warn("No calendars available in SourceRegistry.")

	# Task for confirm next booking if Next Session = ?
	if next_session == "?":
		source = find_source_by_name(reg, TASKS_NAME, EDataServer.SOURCE_EXTENSION_TASK_LIST)
		if source:
			create_task(source, f"Confirm next booking – {client_name}")
		else:
			log_error(f"Tasks list '{TASKS_NAME}' not found.")
			available = list_sources_display(reg, EDataServer.SOURCE_EXTENSION_TASK_LIST)
			if available:
				log_info("Available task lists:")
				for name, uid in available:
					print(f"  - {name} (uid={uid})")
			else:
				log_warn("No task lists available in SourceRegistry.")

	# Task for Homework
	if homework and homework != "0":
		source = find_source_by_name(reg, TASKS_NAME, EDataServer.SOURCE_EXTENSION_TASK_LIST)
		if source:
			create_task(source, f"Send Homework – {client_name} ({homework})")
		else:
			log_error(f"Tasks list '{TASKS_NAME}' not found.")
			available = list_sources_display(reg, EDataServer.SOURCE_EXTENSION_TASK_LIST)
			if available:
				log_info("Available task lists:")
				for name, uid in available:
					print(f"  - {name} (uid={uid})")
			else:
				log_warn("No task lists available in SourceRegistry.")


# --- Entry point ---
if __name__ == "__main__":
	try:
		process_latest()
	except KeyboardInterrupt:
		log_warn("Interrupted by user")
	except Exception as e:
		log_error(f"Unhandled error: {e}")