from __future__ import annotations

import json
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .app_paths import database_path
from .database import Database
from .hashing import sha256_file
from .logging_utils import configure_detailed, essential, info, log_path
from .models import FileRecord
from .naming import apply_destination_names
from .processor import (
    ProcessingCancelled,
    ValidationError,
    copy_one,
    delete_destination_duplicates,
    discover_destination_duplicates,
    lightweight_destination_check,
    make_process_folder,
    post_validate,
    pre_validate,
    resume_required_bytes,
    validate_parent,
    verify_hashes,
    write_delete_reports,
    write_reports,
    write_review_csv,
    write_review_summary,
)
from .scanner import ScanCancelled, Scanner


class ScanThread(QThread):
    progress = Signal(int, int, str)
    log = Signal(str)
    finished_data = Signal(object, object, object, object)
    failed = Signal(str)

    def __init__(self, root: Path, ignore_metadata: bool, detailed_logging: bool):
        super().__init__()
        self.root = Path(root)
        self.ignore_metadata = ignore_metadata
        self.detailed_logging = detailed_logging
        self.cancel_requested = False

    def cancel(self):
        self.cancel_requested = True

    def _progress(self, current, total, path, phase):
        self.progress.emit(current, total, f"{phase} | {path}")

    def run(self):
        db = Database(database_path())
        scan_id = None
        try:
            logger = configure_detailed(self.detailed_logging)
            self.log.emit(f"SCAN START | source={self.root}")
            scan_id = db.create_scan(self.root)
            scanner = Scanner(
                self.root,
                ignore_system_metadata=self.ignore_metadata,
            )
            records = scanner.discover(
                lambda c, t, p: self._progress(c, t, p, "DISCOVERING"),
                lambda: self.cancel_requested,
                logger,
            )
            self.log.emit(
                f"DISCOVERY COMPLETE | files={len(records)} | "
                f"ignored={len(scanner.ignored_files)} | errors={len(scanner.errors)}"
            )
            groups = scanner.analyze(
                records,
                lambda c, t, p: self._progress(c, t, p, "ANALYZING"),
                lambda: self.cancel_requested,
                logger,
            )
            apply_destination_names(records, "FLAT")
            db.save_scan(scan_id, records, groups, len(scanner.errors), scanner.ignored_files)
            summary = {
                "scan_id": scan_id,
                "total_files": len(records),
                "total_size": sum(r.size_bytes for r in records),
                "group_count": len(groups),
                "ignored_files": scanner.ignored_files,
                "ignored_count": len(scanner.ignored_files),
                "ignored_size": sum(i.size_bytes for i in scanner.ignored_files),
            }
            self.log.emit(f"ANALYSIS COMPLETE | groups={len(groups)}")
            self.finished_data.emit(records, groups, scanner.errors, summary)
        except ScanCancelled:
            self.log.emit("SCAN CANCELLED")
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        finally:
            if scan_id is not None and self.cancel_requested:
                try:
                    db.mark_scan_cancelled(scan_id)
                except Exception:
                    pass
            db.close()


class ProcessThread(QThread):
    progress = Signal(int, int, str)
    metrics = Signal(object)
    status = Signal(str)
    log = Signal(str)
    finished_data = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        source: Path,
        parent: Path,
        records: list[FileRecord],
        summary: dict,
        organization: str,
        *,
        detailed_logging: bool,
        pre_validation_enabled: bool,
        post_validation_enabled: bool,
        resume_job=None,
        resume_destination=None,
    ):
        super().__init__()
        self.source = Path(source)
        self.parent = Path(parent)
        self.records = records
        self.summary = summary
        self.organization = organization
        self.detailed = detailed_logging
        self.pre_enabled = pre_validation_enabled
        self.post_enabled = post_validation_enabled
        self.resume_job = resume_job
        self.resume_destination = Path(resume_destination) if resume_destination else None
        self.cancel_requested = False

    def cancel(self):
        self.cancel_requested = True

    def emit_log(self, message, level="INFO"):
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {level} | {message}"
        self.log.emit(line)
        logger = configure_detailed(self.detailed)
        if level == "ERROR":
            essential(logger, 40, message)
        elif level == "WARNING":
            essential(logger, 30, message)
        else:
            info(logger, message)

    def run(self):
        db = Database(database_path())
        job_id = None
        started = time.monotonic()
        destination = None
        try:
            if self.resume_destination:
                destination = self.resume_destination
                if not destination.is_dir():
                    raise ValidationError("Existing destination is unavailable.")
                parent = destination.parent
                required = resume_required_bytes(self.records, destination)
            else:
                parent = self.parent
                required = sum(r.size_bytes for r in self.records)

            if self.pre_enabled:
                self.status.emit("Pre-copy validation running...")
                self.emit_log("PRE-VALIDATION START")
                pre = pre_validate(
                    self.source,
                    self.records,
                    parent,
                    required_bytes=required,
                )
                self.emit_log("PRE-VALIDATION PASS")
            else:
                validate_parent(self.source, parent, 0)
                pre = {"status": "SKIPPED BY USER"}
                self.emit_log("PRE-VALIDATION SKIPPED BY USER")

            if destination is None:
                destination = make_process_folder(parent)

            if self.resume_job:
                job_id = int(self.resume_job["job_id"])
            else:
                job_id = db.create_job(
                    int(self.summary["scan_id"]),
                    self.source,
                    destination,
                    self.records,
                    json.dumps({
                        "detailed_logging": self.detailed,
                        "pre_validation": self.pre_enabled,
                        "post_validation": self.post_enabled,
                        "organization": self.organization,
                    }),
                )

            db.set_job(job_id, "PROCESSING")
            total = len(self.records)
            success = 0
            failed = 0
            copied_bytes = 0
            self.emit_log(
                f"COPY START | files={total} | destination={destination} | "
                f"organization={self.organization}"
            )

            for idx, record in enumerate(self.records, 1):
                if self.cancel_requested:
                    db.set_job(job_id, "INTERRUPTED", "Cancelled by user")
                    raise ProcessingCancelled()

                try:
                    target = destination / record.planned_relative_destination
                    reused = False
                    if (
                        target.is_file()
                        and target.stat().st_size == record.size_bytes
                        and record.prevalidated_hash
                    ):
                        try:
                            if sha256_file(target) == record.prevalidated_hash:
                                record.processing_status = "VERIFIED"
                                record.destination_hash = record.prevalidated_hash
                                db.set_file(
                                    job_id,
                                    record.file_id,
                                    "VERIFIED",
                                    record.prevalidated_hash,
                                    record.destination_hash,
                                )
                                reused = True
                        except OSError:
                            pass

                    if not reused:
                        copy_one(
                            record,
                            destination,
                            db,
                            job_id,
                            lambda: self.cancel_requested,
                        )
                    success += 1
                    copied_bytes += record.size_bytes
                except ProcessingCancelled:
                    raise
                except Exception as exc:
                    failed += 1
                    record.processing_status = "FAILED"
                    record.error_message = f"{type(exc).__name__}: {exc}"
                    self.emit_log(
                        f"FILE FAILED; CONTINUING | file_id={record.file_id} | "
                        f"source={record.source_path} | error={record.error_message}",
                        "ERROR",
                    )

                elapsed = max(time.monotonic() - started, 0.001)
                speed = copied_bytes / elapsed
                remaining = sum(
                    r.size_bytes
                    for r in self.records[idx:]
                    if r.processing_status != "VERIFIED"
                )
                eta = remaining / speed if speed else None
                self.metrics.emit({
                    "phase": "COPY",
                    "current": idx,
                    "total": total,
                    "successes": success,
                    "failures": failed,
                    "bytes": copied_bytes,
                    "elapsed": elapsed,
                    "speed": speed,
                    "eta": eta,
                })
                self.progress.emit(idx, total, str(record.source_path))

            self.emit_log(f"COPY COMPLETE | successful={success} | failed={failed}")

            # Mandatory reconciliation is against the COMPLETE expected source
            # population, not just successful copies. A copy failure must fail
            # reconciliation rather than being hidden by a reduced expectation.
            light = lightweight_destination_check(self.records, destination)
            self.emit_log(
                f"LIGHTWEIGHT DESTINATION CHECK | status={light['status']} | "
                f"expected={light['expected_count']} | actual={light['actual_count']} | "
                f"bytes={light['actual_size']}"
            )

            if self.post_enabled:
                self.status.emit("Post-copy validation running...")
                post = post_validate(self.records, destination)
                self.emit_log(f"POST-VALIDATION {post['status']}")
                self.status.emit("SHA-256 verification running...")
                verified, hash_failures = verify_hashes(
                    self.records,
                    destination,
                    db,
                    job_id,
                    lambda: self.cancel_requested,
                    lambda c, t, p: self.progress.emit(c, t, f"SHA-256 | {p}"),
                )
            else:
                post = {"status": "SKIPPED BY USER"}
                verified = 0
                hash_failures = []
                self.emit_log("POST-VALIDATION AND SHA-256 SKIPPED BY USER")

            reports = write_reports(
                self.records,
                self.source,
                destination,
                pre,
                post,
                hash_failures,
                self.summary.get("ignored_files", []),
                validation_enabled=self.post_enabled,
                pre_validation_enabled=self.pre_enabled,
                detailed_logging=self.detailed,
                lightweight_check=light,
                organization=self.organization,
                started_at=started,
                completed_at=time.monotonic(),
            )
            final_status = (
                "COMPLETED"
                if failed == 0 and light["status"] == "PASS"
                else "COMPLETED_WITH_ERRORS"
            )
            db.set_job(
                job_id,
                final_status,
                None if failed == 0 else f"{failed} file(s) failed",
            )
            self.finished_data.emit({
                "destination": destination,
                "copy_failures": failed,
                "lightweight": light,
                "post": post,
                "reports": reports,
                "verified": verified,
            })
        except ProcessingCancelled:
            self.failed.emit("Processing interrupted. The job can be resumed.")
        except Exception as exc:
            if job_id:
                try:
                    db.set_job(job_id, "FAILED", str(exc))
                except Exception:
                    pass
            self.failed.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        finally:
            db.close()


class DeleteAnalysisThread(QThread):
    progress = Signal(int, int, str)
    log = Signal(str)
    finished_data = Signal(object)
    failed = Signal(str)

    def __init__(self, destination: Path, detailed: bool):
        super().__init__()
        self.destination = Path(destination)
        self.detailed = detailed
        self.cancel_requested = False

    def cancel(self):
        self.cancel_requested = True

    def run(self):
        try:
            self.log.emit(f"DELETE ANALYSIS START | destination={self.destination}")
            groups = discover_destination_duplicates(
                self.destination,
                lambda c, t, p: self.progress.emit(c, t, str(p)),
                lambda: self.cancel_requested,
            )
            self.log.emit(f"DUPLICATE ANALYSIS COMPLETE | groups={len(groups)}")
            self.finished_data.emit({
                "groups": groups,
                "destination": str(self.destination),
            })
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")


class DeleteExecutionThread(QThread):
    progress = Signal(int, int, str)
    log = Signal(str)
    finished_data = Signal(object)
    failed = Signal(str)

    def __init__(self, destination: Path, groups, detailed: bool):
        super().__init__()
        self.destination = Path(destination)
        self.groups = groups
        self.detailed = detailed
        self.cancel_requested = False

    def cancel(self):
        self.cancel_requested = True

    def run(self):
        try:
            # Capture the review snapshot BEFORE deletion. This avoids stat'ing
            # files after they have been removed.
            review_rows = []
            for g in self.groups:
                g = sorted(g, key=lambda p: (str(p).casefold(), str(p)))
                keep = g[0]
                keep_hash = sha256_file(keep)
                for seq, path in enumerate(g, 1):
                    review_rows.append([
                        seq,
                        "KEEP" if path == keep else "DELETE_CANDIDATE",
                        str(path.relative_to(self.destination)),
                        path.stat().st_size,
                        keep_hash,
                    ])

            self.log.emit("DELETE START | destination-only deletion")
            result = delete_destination_duplicates(
                self.destination,
                self.groups,
                cancelled=lambda: self.cancel_requested,
            )
            paths = write_delete_reports(
                self.destination,
                self.groups,
                result,
                review_rows=review_rows,
            )
            self.log.emit(
                f"DELETE COMPLETE | deleted={result['deleted']} | "
                f"failed={result['failed']} | bytes_recovered={result['bytes_deleted']}"
            )
            self.finished_data.emit({"result": result, "reports": paths})
        except ProcessingCancelled:
            self.failed.emit("Deletion interrupted. No further files were deleted.")
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")


class ReviewDialog(QDialog):
    def __init__(self, parent, records, source):
        super().__init__(parent)
        self.setWindowTitle("Review & Confirm Processing")
        self.resize(1300, 750)
        self.destination_parent = None

        self.organization = QComboBox()
        self.organization.addItem(
            "Flat — all files directly in destination", "FLAT"
        )
        self.organization.addItem(
            "Year → Location — use media creation metadata", "YEAR_LOCATION"
        )

        choose = QPushButton("Choose Destination Parent")
        choose.clicked.connect(self.choose_destination)
        self.dest_label = QLabel("No destination selected")
        check = QCheckBox(
            "I reviewed the files and destination names. Source files remain unchanged."
        )
        self.check = check

        box = QGroupBox("Processing Summary")
        grid = QGridLayout(box)
        values = [
            ("Files to copy", len(records)),
            ("Duplicate groups", len({r.group_id for r in records if r.group_id})),
            ("Original/representative", sum(r.classification == "ORIGINAL" for r in records)),
            ("Duplicates", sum(r.classification == "DUPLICATE" for r in records)),
            ("Unique", sum(r.classification == "UNIQUE" for r in records)),
            ("Total size", f"{sum(r.size_bytes for r in records):,} bytes"),
        ]
        for i, (label, value) in enumerate(values):
            row = (i // 2) * 2
            col = (i % 2) * 2
            grid.addWidget(QLabel(label), row, col)
            grid.addWidget(QLabel(str(value)), row, col + 1)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "Classification", "Group", "Seq", "Size", "Original File",
            "Source Path", "Year", "Location",
        ])
        for record in records:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = [
                record.classification or "",
                f"G{record.group_id:06d}" if record.group_id else "",
                str(record.sequence or ""),
                str(record.size_bytes),
                record.original_filename,
                str(record.source_path),
                record.creation_year or "",
                record.creation_location or "",
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText("Confirm and Start")
        self.ok.setEnabled(False)
        buttons.accepted.connect(self.accept_plan)
        buttons.rejected.connect(self.reject)
        check.stateChanged.connect(self.update_buttons)

        top = QHBoxLayout()
        top.addWidget(QLabel("Source:"))
        top.addWidget(QLabel(str(source)), 1)
        dest = QHBoxLayout()
        dest.addWidget(QLabel("Destination parent:"))
        dest.addWidget(self.dest_label, 1)
        dest.addWidget(choose)
        org = QHBoxLayout()
        org.addWidget(QLabel("Destination organization:"))
        org.addWidget(self.organization, 1)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(dest)
        layout.addLayout(org)
        layout.addWidget(box)
        layout.addWidget(self.table)
        layout.addWidget(check)
        layout.addWidget(buttons)

    def choose_destination(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose destination parent folder"
        )
        if folder:
            self.destination_parent = Path(folder)
            self.dest_label.setText(str(self.destination_parent))
            self.update_buttons()

    def update_buttons(self):
        self.ok.setEnabled(bool(self.destination_parent and self.check.isChecked()))

    def accept_plan(self):
        self.accept()


class DeleteDialog(QDialog):
    def __init__(self, parent, destination, groups):
        super().__init__(parent)
        self.setWindowTitle("Delete Duplicates — Destination Only")
        self.resize(1100, 650)
        self.destination = Path(destination)
        self.groups = groups

        duplicate_count = sum(len(g) - 1 for g in groups)
        recoverable = sum(
            p.stat().st_size
            for g in groups
            for p in sorted(g, key=lambda x: (str(x).casefold(), str(x)))[1:]
        )
        self.check = QCheckBox(
            "I understand that this will permanently delete duplicate files from "
            "the selected DESTINATION only. The source folder will not be touched."
        )

        box = QGroupBox("Deletion Summary")
        grid = QGridLayout(box)
        values = [
            ("Destination", str(destination)),
            ("Duplicate groups", str(len(groups))),
            ("Files eligible for deletion", str(duplicate_count)),
            ("Potential space recovered", f"{recoverable:,} bytes"),
        ]
        for row, (label, value) in enumerate(values):
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(QLabel(value), row, 1)

        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels([
            "Action", "File", "Keep representative", "Size"
        ])
        for group in groups:
            group = sorted(group, key=lambda x: (str(x).casefold(), str(x)))
            keep = group[0]
            for path in group:
                row = table.rowCount()
                table.insertRow(row)
                values = [
                    "KEEP" if path == keep else "DELETE",
                    str(path.relative_to(destination)),
                    str(keep.relative_to(destination)),
                    str(path.stat().st_size),
                ]
                for col, value in enumerate(values):
                    table.setItem(row, col, QTableWidgetItem(value))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText("Delete Duplicates")
        self.ok.setEnabled(False)
        self.check.stateChanged.connect(lambda: self.ok.setEnabled(self.check.isChecked()))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(box)
        layout.addWidget(table)
        layout.addWidget(self.check)
        layout.addWidget(buttons)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Duplicate File Organizer — Phase 1.0.1")
        self.resize(1550, 950)
        self.source = None
        self.records = []
        self.groups = []
        self.ignored_files = []
        self.summary = None
        self.active_thread = None
        self.active_kind = None

        self.select = QPushButton("Select Source Folder")
        self.scan = QPushButton("Scan")
        self.review = QPushButton("Review & Process")
        self.resume = QPushButton("Resume Last Job")
        self.cancel = QPushButton("Cancel")
        self.ignored = QPushButton("View Ignored System Files")
        self.delete_btn = QPushButton("Delete Duplicates from Destination")

        # Default is OFF because the application's core requirement is to copy
        # every regular file discovered. Users may explicitly opt into ignoring
        # OS metadata files.
        self.ignore_metadata = QCheckBox("Ignore OS-generated system/metadata files")
        self.ignore_metadata.setChecked(False)
        self.detailed = QCheckBox("Detailed execution logging")
        self.detailed.setChecked(True)
        self.pre = QCheckBox("Pre-copy validation")
        self.pre.setChecked(True)
        self.post = QCheckBox("Post-copy validation + SHA-256 verification")
        self.post.setChecked(True)

        self.review_dl = QPushButton("Download Review CSV")
        self.copy_dl = QPushButton("Download Copy Status CSV")
        self.summary_dl = QPushButton("Download Summary CSV")
        self.validation_dl = QPushButton("Download Validation Report")
        self.log_dl = QPushButton("Download Log")

        for button in [
            self.review, self.resume, self.cancel, self.ignored,
            self.copy_dl, self.summary_dl, self.validation_dl, self.log_dl,
        ]:
            button.setEnabled(False)

        self.folder = QLabel("No source folder selected")
        self.status = QLabel("Ready")
        self.eta = QLabel("ETA: —")
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([
            "Classification", "Group", "Seq", "Size", "Original File",
            "Source Path", "Planned Destination",
        ])
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(8000)

        top = QHBoxLayout()
        for button in [
            self.select, self.scan, self.review, self.resume,
            self.delete_btn, self.ignored, self.cancel,
        ]:
            top.addWidget(button)
        options = QHBoxLayout()
        for widget in [self.ignore_metadata, self.detailed, self.pre, self.post]:
            options.addWidget(widget)
        options.addStretch()
        reports = QHBoxLayout()
        for button in [
            self.review_dl, self.copy_dl, self.summary_dl,
            self.validation_dl, self.log_dl,
        ]:
            reports.addWidget(button)

        log_box = QGroupBox("Execution Log (live)")
        log_layout = QVBoxLayout(log_box)
        log_layout.addWidget(self.log_view)

        layout = QVBoxLayout()
        layout.addLayout(top)
        layout.addLayout(options)
        layout.addWidget(self.folder)
        layout.addWidget(self.status)
        layout.addWidget(self.eta)
        layout.addWidget(self.progress)
        layout.addWidget(self.table, 1)
        layout.addWidget(log_box, 1)
        layout.addLayout(reports)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

        self.select.clicked.connect(self.select_folder)
        self.scan.clicked.connect(self.start_scan)
        self.review.clicked.connect(self.review_process)
        self.resume.clicked.connect(self.resume_last)
        self.cancel.clicked.connect(self.cancel_current)
        self.ignored.clicked.connect(self.show_ignored)
        self.delete_btn.clicked.connect(self.delete_destination)
        self.review_dl.clicked.connect(lambda: self.download(self.review_dl.property("path")))
        self.copy_dl.clicked.connect(lambda: self.download(self.copy_dl.property("path")))
        self.summary_dl.clicked.connect(lambda: self.download(self.summary_dl.property("path")))
        self.validation_dl.clicked.connect(lambda: self.download(self.validation_dl.property("path")))
        self.log_dl.clicked.connect(lambda: self.download(log_path()))

        self.refresh_resume()

    def append_log(self, message):
        self.log_view.appendPlainText(message)
        self.log_view.ensureCursorVisible()

    def download(self, path):
        if not path or not Path(path).exists():
            QMessageBox.information(
                self, "Report unavailable", "The requested report is not available yet."
            )
            return
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Save report",
            Path(path).name,
            "CSV (*.csv);;JSON (*.json);;Log (*.log);;All files (*)",
        )
        if target:
            try:
                shutil.copy2(path, target)
                QMessageBox.information(self, "Saved", f"Saved to:\n{target}")
            except Exception as exc:
                QMessageBox.critical(self, "Save failed", str(exc))

    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select source folder")
        if not folder:
            return
        self.source = Path(folder)
        self.folder.setText(str(self.source))
        self.records = []
        self.groups = []
        self.ignored_files = []
        self.review.setEnabled(False)
        self.review_dl.setEnabled(False)
        self.copy_dl.setEnabled(False)
        self.summary_dl.setEnabled(False)
        self.validation_dl.setEnabled(False)
        self.append_log(f"SOURCE SELECTED | {self.source}")

    def start_scan(self):
        if not self.source:
            QMessageBox.warning(self, "Source required", "Select a source folder first.")
            return
        self.log_view.clear()
        self.start_thread(
            ScanThread(self.source, self.ignore_metadata.isChecked(), self.detailed.isChecked()),
            "scan",
        )

    def start_thread(self, thread, kind):
        self.active_thread = thread
        self.active_kind = kind
        self.progress.setVisible(True)
        self.cancel.setEnabled(True)
        self.select.setEnabled(False)
        self.scan.setEnabled(False)
        self.review.setEnabled(False)
        self.resume.setEnabled(False)
        self.delete_btn.setEnabled(False)
        self.ignore_metadata.setEnabled(False)
        self.detailed.setEnabled(False)
        self.pre.setEnabled(False)
        self.post.setEnabled(False)

        thread.progress.connect(self.progress_update)
        thread.log.connect(self.append_log)
        thread.failed.connect(self.operation_failed)
        thread.finished.connect(self.thread_finished)

        if kind == "scan":
            thread.finished_data.connect(self.scan_done)
        elif kind == "process":
            thread.metrics.connect(self.metrics_update)
            thread.status.connect(self.status.setText)
            thread.finished_data.connect(self.process_done)
        elif kind == "delete_analysis":
            thread.finished_data.connect(self.delete_analyzed)
        elif kind == "delete_execute":
            thread.finished_data.connect(self.delete_finished)

        thread.start()

    def progress_update(self, current, total, message):
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(current)
        else:
            self.progress.setRange(0, 0)
        self.status.setText(message)

    def metrics_update(self, metrics):
        self.progress_update(
            metrics["current"], metrics["total"],
            f"COPYING {metrics['current']:,}/{metrics['total']:,}",
        )
        self.eta.setText(
            f"Successful: {metrics['successes']:,} | "
            f"Failed: {metrics['failures']:,} | "
            f"Speed: {self.fmt(metrics['speed'])}/s | "
            f"ETA: {self.duration(metrics['eta'])}"
        )

    @staticmethod
    def duration(seconds):
        if seconds is None:
            return "Calculating..."
        seconds = max(0, int(seconds))
        return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"

    @staticmethod
    def fmt(number):
        units = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        while number >= 1024 and i < 4:
            number /= 1024
            i += 1
        return f"{number:.1f} {units[i]}"

    @Slot(object, object, object, object)
    def scan_done(self, records, groups, errors, summary):
        self.records = list(records)
        self.groups = list(groups)
        self.ignored_files = list(summary["ignored_files"])
        self.summary = summary
        self.populate(records)
        original = sum(r.classification == "ORIGINAL" for r in records)
        duplicate = sum(r.classification == "DUPLICATE" for r in records)
        unique = sum(r.classification == "UNIQUE" for r in records)
        self.status.setText(
            f"Files: {len(records):,} | Ignored: {len(self.ignored_files):,} | "
            f"Groups: {len(groups):,} | Original: {original:,} | "
            f"Duplicate: {duplicate:,} | Unique: {unique:,} | "
            f"Size: {sum(r.size_bytes for r in records):,} bytes"
        )
        self.review.setEnabled(bool(records) and not errors)
        self.ignored.setEnabled(bool(self.ignored_files))
        review_path = write_review_csv(
            records, self.source, self.ignored_files, scan_id=summary["scan_id"]
        )
        self.review_dl.setProperty("path", str(review_path))
        self.review_dl.setEnabled(True)
        summary_path = write_review_summary(
            records, self.ignored_files, scan_id=summary["scan_id"]
        )
        self.summary_dl.setProperty("path", str(summary_path))
        self.summary_dl.setEnabled(True)

    def populate(self, records):
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            values = [
                record.classification or "",
                f"G{record.group_id:06d}" if record.group_id else "",
                str(record.sequence or ""),
                str(record.size_bytes),
                record.original_filename,
                str(record.source_path),
                record.planned_relative_destination or "",
            ]
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))

    def show_ignored(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Ignored System/Metadata Files")
        dialog.resize(1000, 500)
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["File", "Relative Path", "Size", "Reason"])
        for item in self.ignored_files:
            row = table.rowCount()
            table.insertRow(row)
            for col, value in enumerate([
                item.filename, item.relative_path, str(item.size_bytes), item.reason
            ]):
                table.setItem(row, col, QTableWidgetItem(value))
        layout = QVBoxLayout(dialog)
        layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def review_process(self):
        dialog = ReviewDialog(self, self.records, self.source)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.start_processing(
                dialog.destination_parent,
                dialog.organization.currentData(),
            )

    def start_processing(self, parent, organization="FLAT", resume_job=None, resume_dest=None):
        apply_destination_names(self.records, organization)
        thread = ProcessThread(
            self.source,
            parent,
            self.records,
            self.summary or {"scan_id": resume_job["scan_id"]},
            organization,
            detailed_logging=self.detailed.isChecked(),
            pre_validation_enabled=self.pre.isChecked(),
            post_validation_enabled=self.post.isChecked(),
            resume_job=resume_job,
            resume_destination=resume_dest,
        )
        self.start_thread(thread, "process")

    def process_done(self, result):
        paths = result["reports"]
        self.copy_dl.setProperty("path", str(paths[0]))
        self.copy_dl.setEnabled(True)
        self.summary_dl.setProperty("path", str(paths[2]))
        self.summary_dl.setEnabled(True)
        self.validation_dl.setEnabled(bool(paths[3]))
        if paths[3]:
            self.validation_dl.setProperty("path", str(paths[3]))
        self.log_dl.setEnabled(True)
        light = result["lightweight"]
        failed = result["copy_failures"]
        successful = len(self.records) - failed
        self.status.setText(
            f"COMPLETED | Successful: {successful:,} | Failed: {failed:,} | "
            f"Destination check: {light['status']}"
        )
        QMessageBox.information(
            self,
            "Processing completed",
            f"Destination:\n{result['destination']}\n\n"
            f"Successful: {successful:,}\n"
            f"Failed: {failed:,}\n"
            f"Destination count/size check: {light['status']}\n\n"
            "Reports are available below.",
        )
        self.refresh_resume()

    def delete_destination(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select DESTINATION folder only"
        )
        if not folder:
            return
        destination = Path(folder)
        if self.source and destination.resolve() == self.source.resolve():
            QMessageBox.warning(
                self, "Unsafe destination", "The selected destination is the source folder."
            )
            return
        self.log_view.clear()
        self.start_thread(
            DeleteAnalysisThread(destination, self.detailed.isChecked()),
            "delete_analysis",
        )

    def delete_analyzed(self, data):
        destination = Path(data["destination"])
        groups = data["groups"]
        if not groups:
            QMessageBox.information(
                self,
                "No duplicates",
                "No exact duplicate files were found in the selected destination.",
            )
            return
        dialog = DeleteDialog(self, destination, groups)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.start_thread(
                DeleteExecutionThread(destination, groups, self.detailed.isChecked()),
                "delete_execute",
            )

    def delete_finished(self, data):
        result = data["result"]
        paths = data["reports"]
        QMessageBox.information(
            self,
            "Deletion completed",
            f"Deleted: {result['deleted']}\n"
            f"Failed: {result['failed']}\n"
            f"Bytes recovered: {result['bytes_deleted']:,}\n\n"
            f"Reports:\n{paths[0]}\n{paths[1]}\n{paths[2]}",
        )

    def cancel_current(self):
        if self.active_thread and self.active_thread.isRunning():
            self.active_thread.cancel()
            self.append_log("USER REQUESTED CANCELLATION")

    def resume_last(self):
        try:
            db = Database(database_path())
            job = db.resume_job()
            db.close()
            if not job:
                QMessageBox.information(self, "No resumable job", "No resumable job found.")
                return

            self.source = Path(job["source_path"])
            self.folder.setText(str(self.source))
            self.records = []
            for row in job["files"]:
                (
                    file_id, source_path, relative_path, filename, extension,
                    size, modified_ns, partial_hash, sha256, classification,
                    group_id, sequence, metadata_reason, planned_destination,
                    status, source_hash, destination_hash, error,
                    year, location, location_source, date_source, date_confidence,
                ) = row
                self.records.append(FileRecord(
                    file_id,
                    Path(source_path),
                    relative_path,
                    filename,
                    extension,
                    size,
                    modified_ns,
                    partial_hash,
                    sha256,
                    classification,
                    group_id,
                    sequence,
                    metadata_reason,
                    planned_destination,
                    source_hash,
                    destination_hash,
                    status,
                    error,
                    creation_year=year,
                    creation_location=location,
                    location_source=location_source,
                    creation_date_source=date_source,
                    creation_date_confidence=date_confidence,
                ))

            options = json.loads(job.get("options_json") or "{}")
            self.detailed.setChecked(bool(options.get("detailed_logging", True)))
            self.pre.setChecked(bool(options.get("pre_validation", True)))
            self.post.setChecked(bool(options.get("post_validation", True)))
            organization = options.get("organization", "FLAT")
            self.summary = {
                "scan_id": job["scan_id"],
                "total_files": job["expected_count"],
                "total_size": job["expected_size"],
                "ignored_files": [],
            }
            self.start_processing(
                Path(job["destination_path"]).parent,
                organization,
                job,
                Path(job["destination_path"]),
            )
        except Exception as exc:
            QMessageBox.critical(
                self, "Resume unavailable", f"{type(exc).__name__}: {exc}"
            )

    def refresh_resume(self):
        try:
            db = Database(database_path())
            self.resume.setEnabled(db.resume_job() is not None)
            db.close()
        except Exception:
            self.resume.setEnabled(False)

    def operation_failed(self, message):
        self.append_log("FATAL | " + message)
        self.status.setText("Operation failed")
        self.refresh_resume()
        QMessageBox.critical(self, "Operation failed", message)

    @Slot()
    def thread_finished(self):
        thread = self.sender()
        if thread is not self.active_thread:
            return
        # QThread has completed its run() method. At this point it is safe for
        # the GUI to release its Python reference; there is no worker QObject
        # living in a stopped thread to destroy.
        self.active_thread = None
        self.active_kind = None
        self.progress.setVisible(False)
        self.cancel.setEnabled(False)
        self.select.setEnabled(True)
        self.scan.setEnabled(True)
        self.delete_btn.setEnabled(True)
        self.ignore_metadata.setEnabled(True)
        self.detailed.setEnabled(True)
        self.pre.setEnabled(True)
        self.post.setEnabled(True)
        if thread is not None:
            thread.deleteLater()
        self.refresh_resume()

    def closeEvent(self, event):
        if self.active_thread and self.active_thread.isRunning():
            self.active_thread.cancel()
            self.active_thread.wait(3000)
        event.accept()


def install_exception_hook():
    def handle_exception(exc_type, exc_value, exc_traceback):
        if exc_type is KeyboardInterrupt:
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        try:
            logger = configure_detailed(True)
            essential(
                logger,
                40,
                "UNHANDLED GUI EXCEPTION\n" + "".join(
                    traceback.format_exception(exc_type, exc_value, exc_traceback)
                ),
            )
        finally:
            sys.__excepthook__(exc_type, exc_value, exc_traceback)

    sys.excepthook = handle_exception


def run_app():
    install_exception_hook()
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    return app.exec()


def run():
    return run_app()
