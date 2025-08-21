#!/usr/bin/env python3
"""
py_6.0.py - Ubuntu-elegant ODS → Evolution sync (timezone-aware)

- Robust ODS read with header validation
- Deterministic UIDs and idempotent upsert
- Strong datetime parsing; localize tz-naive values to system zone
- UTC outputs for iCal (Z)
- CLI flags, --dry-run, structured logging

Usage example:
  python3 py_6.0.py --ods ~/Client_Tracking/Client_Session_Data_2025_08.ods --calendar "Work" --tasks "Personal" --latest-only
"""

import os
import sys
import uuid
import argparse
import logging
from typing import Optional, Tuple, List
from datetime import datetime, timedelta, timezone

import gi

gi.require_version('ECal', '2.0')
gi.require_version('EDataServer', '1.2')
gi.require_version('ICalGLib', '3.0')
gi.require_version('Gio', '2.0')
from gi.repository import ECal, EDataServer, ICalGLib, Gio  # type: ignore

import pandas as pd

try:
	from dateutil import parser as dateutil_parser  # type: ignore
	HAVE_DATEUTIL = True
except Exception:
	HAVE_DATEUTIL = False

HEADERS_REQUIRED = [
	"Client Name", "Date", "Session", "Hours Booked",
	"Session Time", "Total Hours", "Hours Remaining", "Next Session", "Homework"
]


def setup_logging(verbose: bool) -> None:
	level = logging.DEBUG if verbose else logging.INFO
	logging.basicConfig(level=level, format="%(levelname)s: %(message)s")


def ical_escape(text: str) -> str:
	return (
		text.replace("\\", "\\\\")
			.replace("\n", "\\n")
			.replace(",", "\\,")
			.replace(";", "\\;")
	)


def ensure_pydatetime_aware(dt):
	"""Convert pandas.Timestamp to datetime and attach local tz if naive."""
	# pandas.Timestamp -> datetime
	if hasattr(dt, "to_pydatetime"):
		dt = dt.to_pydatetime()
	if isinstance(dt, datetime) and dt.tzinfo is None:
		local_tz = datetime.now().astimezone().tzinfo
		dt = dt.replace(tzinfo=local_tz)
	return dt


def to_utc_ical(dt: datetime) -> str:
	dt = ensure_pydatetime_aware(dt)
	return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_datetime_any(value: str) -> Optional[datetime]:
	s = (value or "").strip()
	if not s or s == "?":
		return None
	if HAVE_DATEUTIL:
		try:
			return dateutil_parser.parse(s, dayfirst=False, yearfirst=True)
		except Exception:
			pass
	for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M",
	            "%d/%m/%Y %H:%M", "%Y-%m-%d", "%d/%m/%Y"):
		try:
			dt = datetime.strptime(s, fmt)
			if fmt in ("%Y-%m-%d", "%d/%m/%Y"):
				dt = dt.replace(hour=9, minute=0, second=0, microsecond=0)
			return dt
		except Exception:
			continue
	return None


def read_ods(ods_path: str, sheet: Optional[str]):
	if not os.path.exists(ods_path):
		raise FileNotFoundError(f"ODS not found: {ods_path}")
	df = pd.read_excel(ods_path, engine="odf", sheet_name=sheet or 0)
	missing = [h for h in HEADERS_REQUIRED if h not in df.columns]
	if missing:
		raise ValueError(f"ODS missing headers: {missing}")
	return df


def normalize_df(df: pd.DataFrame) -> pd.DataFrame:
	# Date as date
	df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date

	# Next Session to datetime/'?' or None, but let tz attach later
	def norm_next_session(x):
		if pd.isna(x):
			return None
		# convert pandas Timestamp to datetime early
		if hasattr(x, "to_pydatetime"):
			x = x.to_pydatetime()
		if isinstance(x, datetime):
			return x
		s = str(x).strip()
		if s == "?":
			return "?"
		dt = parse_datetime_any(s)
		return dt if dt else "?"

	df["Next Session"] = df["Next Session"].apply(norm_next_session)

	# Homework as string (empty if "0" or NaN)
	def norm_hw(x):
		if pd.isna(x):
			return ""
		s = str(x).strip()
		return "" if s == "0" else s

	df["Homework"] = df["Homework"].apply(norm_hw)
	return df


def newest_rows(df: pd.DataFrame) -> pd.DataFrame:
	max_date = df["Date"].max()
	return df[df["Date"] == max_date]


def get_source_registry() -> EDataServer.SourceRegistry:
	return EDataServer.SourceRegistry.new_sync(None)


def list_sources(reg: EDataServer.SourceRegistry, extension: str):
	return [(s.get_display_name(), s.get_uid()) for s in reg.list_sources(extension)]


def resolve_source(reg: EDataServer.SourceRegistry, extension: str,
                   name: Optional[str], uid: Optional[str]):
	if uid:
		for s in reg.list_sources(extension):
			if s.get_uid() == uid:
				return s
	if name:
		for s in reg.list_sources(extension):
			if s.get_display_name() == name:
				return s
	return None


def connect_client(source: EDataServer.Source, kind: ECal.ClientSourceType):
	try:
		# Try several GI call signatures
		try:
			return ECal.Client.connect_sync(source, kind, 0)
		except TypeError:
			pass
		try:
			return ECal.Client.connect_sync(source, kind, 0, None)
		except TypeError:
			pass
		try:
			return ECal.Client.connect_sync(source, kind)
		except Exception as e:
			raise e
	except Exception as e:
		logging.error(f"Connect failed for '{source.get_display_name()}': {e}")
		return None


def safe_create(client: ECal.Client, comp: ICalGLib.Component) -> None:
	attempts = [
		(comp, 0, None),
		(comp, 0),
		(comp, None),
		(comp,)
	]
	last = None
	for args in attempts:
		try:
			client.create_object_sync(*args)
			return
		except TypeError as e:
			last = e
			continue
	if last:
		raise last


def safe_remove_by_uid(client: ECal.Client, uid: str) -> bool:
	attempts = [
		(uid, None, 0, None),
		(uid, None, 0),
		(uid, None),
		(uid,)
	]
	for args in attempts:
		try:
			client.remove_object_sync(*args)
			return True
		except TypeError:
			continue
		except Exception:
			return False
	return False


NAMESPACE = uuid.UUID("9b2c3e3a-8d49-4c2d-b9f5-7a5c3cb0f0b6")


def event_uid(client_name: str, dt: datetime) -> str:
	dt = ensure_pydatetime_aware(dt)
	key = f"event|{client_name}|{dt.astimezone(timezone.utc).isoformat()}"
	return str(uuid.uuid5(NAMESPACE, key))


def confirm_task_uid(client_name: str) -> str:
	key = f"task-confirm|{client_name}"
	return str(uuid.uuid5(NAMESPACE, key))


def homework_task_uid(client_name: str, homework: str) -> str:
	key = f"task-homework|{client_name}|{homework}"
	return str(uuid.uuid5(NAMESPACE, key))


def build_vevent(uid: str, summary: str, start_dt: datetime, duration_min: int,
                 description: Optional[str] = None) -> ICalGLib.Component:
	dtstamp = to_utc_ical(datetime.now())
	dtstart = to_utc_ical(start_dt)
	dtend = to_utc_ical(start_dt + timedelta(minutes=duration_min))
	lines = [
		"BEGIN:VEVENT",
		f"UID:{uid}",
		f"DTSTAMP:{dtstamp}",
		f"SUMMARY:{ical_escape(summary)}",
		f"DTSTART:{dtstart}",
		f"DTEND:{dtend}",
	]
	if description:
		lines.append(f"DESCRIPTION:{ical_escape(description)}")
	lines.append("END:VEVENT")
	ics = "\r\n".join(lines) + "\r\n"
	return ICalGLib.Component.new_from_string(ics)


def build_vtodo(uid: str, summary: str, description: Optional[str] = None) -> ICalGLib.Component:
	dtstamp = to_utc_ical(datetime.now())
	lines = [
		"BEGIN:VTODO",
		f"UID:{uid}",
		f"DTSTAMP:{dtstamp}",
		f"SUMMARY:{ical_escape(summary)}",
	]
	if description:
		lines.append(f"DESCRIPTION:{ical_escape(description)}")
	lines.append("END:VTODO")
	ics = "\r\n".join(lines) + "\r\n"
	return ICalGLib.Component.new_from_string(ics)


def process_row(row, cal_client, tasks_client, duration_min: int, dry_run: bool) -> None:
	client_name = str(row["Client Name"]).strip()
	next_session = row["Next Session"]
	homework = str(row["Homework"]).strip()

	# Event for real datetime
	if isinstance(next_session, datetime) and cal_client:
		next_session_dt = ensure_pydatetime_aware(next_session)
		euid = event_uid(client_name, next_session_dt)
		comp = build_vevent(
			uid=euid,
			summary=client_name,
			start_dt=next_session_dt,
			duration_min=duration_min,
			description=f"From ODS row for {client_name}"
		)
		if dry_run:
			logging.info(f"[DRY-RUN] Event upsert {client_name} @ {next_session_dt} uid={euid}")
		else:
			try:
				try:
					safe_create(cal_client, comp)
				except Exception as e:
					if "exist" in str(e).lower() or "invalid" in str(e).lower():
						if safe_remove_by_uid(cal_client, euid):
							safe_create(cal_client, comp)
						else:
							raise
					else:
						raise
				logging.info(f"Event upserted for {client_name} @ {next_session_dt}")
			except Exception as e:
				logging.error(f"Event upsert failed ({client_name}): {e}")

	# Confirm task if '?' next session
	if next_session == "?" and tasks_client:
		tuid = confirm_task_uid(client_name)
		comp = build_vtodo(
			uid=tuid,
			summary=f"Confirm next booking – {client_name}",
			description="Next session missing in ODS"
		)
		if dry_run:
			logging.info(f"[DRY-RUN] Task upsert confirm uid={tuid}")
		else:
			try:
				try:
					safe_create(tasks_client, comp)
				except Exception as e:
					if "exist" in str(e).lower() or "invalid" in str(e).lower():
						if safe_remove_by_uid(tasks_client, tuid):
							safe_create(tasks_client, comp)
						else:
							raise
					else:
						raise
				logging.info(f"Confirm task upserted for {client_name}")
			except Exception as e:
				logging.error(f"Confirm task upsert failed ({client_name}): {e}")

	# Homework task if present
	if homework and tasks_client:
		huid = homework_task_uid(client_name, homework)
		comp = build_vtodo(
			uid=huid,
			summary=f"Send Homework – {client_name}",
			description=homework
		)
		if dry_run:
			logging.info(f"[DRY-RUN] Task upsert homework uid={huid}")
		else:
			try:
				try:
					safe_create(tasks_client, comp)
				except Exception as e:
					if "exist" in str(e).lower() or "invalid" in str(e).lower():
						if safe_remove_by_uid(tasks_client, huid):
							safe_create(tasks_client, comp)
						else:
							raise
					else:
						raise
				logging.info(f"Homework task upserted for {client_name}")
			except Exception as e:
				logging.error(f"Homework task upsert failed ({client_name}): {e}")


def main() -> int:
	parser = argparse.ArgumentParser(description="Sync ODS rows into Evolution calendar and tasks")
	parser.add_argument("--ods", required=True, help="Path to ODS file")
	parser.add_argument("--sheet", default="", help="Sheet name (default first sheet)")
	parser.add_argument("--calendar", default="", help="Calendar display name")
	parser.add_argument("--calendar-uid", default="", help="Calendar source UID")
	parser.add_argument("--tasks", default="", help="Tasks list display name")
	parser.add_argument("--tasks-uid", default="", help="Tasks list UID")
	parser.add_argument("--duration-min", type=int, default=60, help="Event duration minutes")
	parser.add_argument("--latest-only", action="store_true", help="Process only latest Date rows")
	parser.add_argument("--dry-run", action="store_true", help="Print actions, do not modify Evolution")
	parser.add_argument("--verbose", action="store_true", help="Verbose logging")
	args = parser.parse_args()

	setup_logging(args.verbose)

	try:
		df = read_ods(os.path.expanduser(args.ods), args.sheet or None)
	except Exception as e:
		logging.error(e)
		return 2

	df = normalize_df(df)
	if args.latest_only:
		df = newest_rows(df)

	reg = get_source_registry()

	cal_source = resolve_source(reg, EDataServer.SOURCE_EXTENSION_CALENDAR,
	                            args.calendar or None, args.calendar_uid or None)
	task_source = resolve_source(reg, EDataServer.SOURCE_EXTENSION_TASK_LIST,
	                             args.tasks or None, args.tasks_uid or None)

	if not cal_source and not task_source:
		logging.error("Neither calendar nor tasks source found. Provide --calendar/--calendar-uid and/or --tasks/--tasks-uid.")
		logging.info("Available calendars:")
		for n, u in list_sources(reg, EDataServer.SOURCE_EXTENSION_CALENDAR):
			logging.info(f"  - {n} (uid={u})")
		logging.info("Available task lists:")
		for n, u in list_sources(reg, EDataServer.SOURCE_EXTENSION_TASK_LIST):
			logging.info(f"  - {n} (uid={u})")
		return 3

	cal_client = connect_client(cal_source, ECal.ClientSourceType.EVENTS) if cal_source and not args.dry_run else None
	tasks_client = connect_client(task_source, ECal.ClientSourceType.TASKS) if task_source and not args.dry_run else None

	if (cal_source and not cal_client) or (task_source and not tasks_client):
		logging.error("Failed to connect to Evolution sources.")
		return 3

	for _, row in df.iterrows():
		try:
			process_row(row, cal_client, tasks_client, args.duration_min, args.dry_run)
		except Exception as e:
			logging.error(f"Row processing failed: {e}")

	logging.info("Done.")
	return 0


if __name__ == "__main__":
	sys.exit(main())