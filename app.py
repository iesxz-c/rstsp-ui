import queue
import sys
import threading
from pathlib import Path
from dataclasses import dataclass

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

import gi


def _bootstrap_windows_gi() -> None:
    if sys.platform != "win32":
        return

    app_root = Path(__file__).resolve().parent
    candidates = [
        app_root / "gstreamer" / "1.0" / "msvc_x86_64",
        app_root / "gstreamer",
    ]
    env_root = Path("C:/gstreamer/1.0/msvc_x86_64")
    progfiles_root = Path("C:/Program Files/gstreamer/1.0/msvc_x86_64")
    candidates.extend([env_root, progfiles_root])

    for root in candidates:
        site_packages = root / "lib" / "site-packages"
        bin_dir = root / "bin"
        if site_packages.exists() and str(site_packages) not in sys.path:
            sys.path.insert(0, str(site_packages))
        if bin_dir.exists():
            try:
                import os

                os.add_dll_directory(str(bin_dir))
            except Exception:
                pass


_bootstrap_windows_gi()

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
gi.require_version("GstRtsp", "1.0")

from gi.repository import GLib, Gst, GstRtsp, GstVideo


Gst.init(None)


@dataclass
class StreamConfig:
    url: str
    latency_ms: int
    protocol: str


class VideoWidget(QWidget):
    zoom_requested = pyqtSignal(int)

    def __init__(self) -> None:
        super().__init__()
        self.setAttribute(Qt.WA_NativeWindow)
        self.setMinimumSize(640, 360)
        self.setStyleSheet("background-color: black;")

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta > 0:
            self.zoom_requested.emit(1)
            event.accept()
            return
        if delta < 0:
            self.zoom_requested.emit(-1)
            event.accept()
            return
        event.ignore()


class GstWorker(QObject):
    log = pyqtSignal(str)
    state_changed = pyqtSignal(str, str)
    stream_disconnected = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self._cmd_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._context: GLib.MainContext | None = None
        self._loop: GLib.MainLoop | None = None

        self._pipeline: Gst.Pipeline | None = None
        self._source: Gst.Element | None = None
        self._decodebin: Gst.Element | None = None
        self._crop: Gst.Element | None = None
        self._convert: Gst.Element | None = None
        self._queue_el: Gst.Element | None = None
        self._sink: Gst.Element | None = None

        self._video_window_id: int | None = None
        self._is_connected = False
        self._command_source: GLib.Source | None = None
        self._zoom_factor = 1.0
        self._zoom_step = 0.25
        self._max_zoom = 4.0
        self._video_width = 0
        self._video_height = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._cmd_queue.put(("shutdown", None))
        if self._thread:
            self._thread.join(timeout=2)

    def connect_stream(self, config: StreamConfig, video_window_id: int) -> None:
        self._cmd_queue.put(("connect", (config, video_window_id)))

    def disconnect_stream(self) -> None:
        self._cmd_queue.put(("disconnect", None))

    def zoom_in(self) -> None:
        self._cmd_queue.put(("zoom_in", None))

    def zoom_out(self) -> None:
        self._cmd_queue.put(("zoom_out", None))

    def reset_zoom(self) -> None:
        self._cmd_queue.put(("reset_zoom", None))

    def _thread_main(self) -> None:
        self._context = GLib.MainContext.new()
        self._loop = GLib.MainLoop.new(self._context, False)
        self._context.push_thread_default()
        self.log.emit("Worker GLib loop started")
        self._command_source = GLib.timeout_source_new(50)
        self._command_source.set_callback(lambda *args: self._process_commands())
        self._command_source.attach(self._context)
        try:
            self._loop.run()
        except Exception as exc:
            self.log.emit(f"ERROR: Worker loop crashed: {exc}")
        finally:
            if self._command_source is not None:
                self._command_source.destroy()
                self._command_source = None
            self._teardown_pipeline("Worker shutdown")
            self._context.pop_thread_default()

    def _process_commands(self) -> bool:
        while True:
            try:
                cmd, payload = self._cmd_queue.get_nowait()
            except queue.Empty:
                break

            if cmd == "connect":
                config, window_id = payload
                self._video_window_id = window_id
                self._connect_pipeline(config)
            elif cmd == "disconnect":
                self._teardown_pipeline("Disconnected by user")
            elif cmd == "shutdown":
                self._teardown_pipeline("Worker stopping")
                if self._loop and self._loop.is_running():
                    self._loop.quit()
                return False
            elif cmd == "zoom_in":
                self._change_zoom(self._zoom_step)
            elif cmd == "zoom_out":
                self._change_zoom(-self._zoom_step)
            elif cmd == "reset_zoom":
                self._reset_zoom()
            else:
                self.log.emit(f"WARN: Unknown command received: {cmd}")

        return True

    def _connect_pipeline(self, config: StreamConfig) -> None:
        self._teardown_pipeline("Rebuilding pipeline")
        self.log.emit(f"Connecting to {config.url}")

        pipeline = Gst.Pipeline.new("rtsp-client-pipeline")
        if not pipeline:
            self.log.emit("ERROR: Unable to create pipeline")
            self.stream_disconnected.emit("pipeline_create_failed")
            return

        source = Gst.ElementFactory.make("rtspsrc", "source")
        decodebin = Gst.ElementFactory.make("decodebin", "decodebin")
        crop = Gst.ElementFactory.make("videocrop", "crop")
        convert = Gst.ElementFactory.make("videoconvert", "convert")
        queue_el = Gst.ElementFactory.make("queue", "queue")

        sink = (
            Gst.ElementFactory.make("d3d11videosink", "sink")
            or Gst.ElementFactory.make("glimagesink", "sink")
            or Gst.ElementFactory.make("autovideosink", "sink")
        )

        elements = [source, decodebin, convert, queue_el, sink]
        if any(el is None for el in elements):
            self.log.emit("ERROR: Missing required GStreamer plugin(s)")
            self.stream_disconnected.emit("missing_plugins")
            return

        if crop is None:
            self.log.emit("WARN: videocrop plugin missing. Zoom controls disabled.")

        source.set_property("location", config.url)
        source.set_property("latency", config.latency_ms)

        try:
            source.set_property("drop-on-latency", True)
        except Exception:
            self.log.emit("WARN: drop-on-latency not supported by this rtspsrc build")

        protocols = self._protocol_flags(config.protocol)
        source.set_property("protocols", protocols)

        sink.set_property("sync", False)

        queue_el.set_property("max-size-buffers", 2)
        queue_el.set_property("max-size-time", 0)
        queue_el.set_property("max-size-bytes", 0)
        queue_el.set_property("leaky", 2)

        pipeline.add(source)
        pipeline.add(decodebin)
        if crop is not None:
            pipeline.add(crop)
        pipeline.add(convert)
        pipeline.add(queue_el)
        pipeline.add(sink)

        if crop is not None:
            if not crop.link(convert):
                self.log.emit("ERROR: Failed to link videocrop -> videoconvert")
                self.stream_disconnected.emit("link_failed")
                return

        if not convert.link(queue_el):
            self.log.emit("ERROR: Failed to link videoconvert -> queue")
            self.stream_disconnected.emit("link_failed")
            return

        if not queue_el.link(sink):
            self.log.emit("ERROR: Failed to link queue -> sink")
            self.log.emit("ERROR: Failed to link video output chain")
            self.stream_disconnected.emit("link_failed")
            return

        source.connect("pad-added", self._on_rtsp_pad_added, decodebin)
        decodebin.connect("pad-added", self._on_decodebin_pad_added)

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)
        bus.enable_sync_message_emission()
        bus.connect("sync-message::element", self._on_sync_message)

        self._pipeline = pipeline
        self._source = source
        self._decodebin = decodebin
        self._crop = crop
        self._convert = convert
        self._queue_el = queue_el
        self._sink = sink
        self._zoom_factor = 1.0
        self._video_width = 0
        self._video_height = 0

        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self.log.emit("ERROR: Failed to set pipeline to PLAYING")
            self._teardown_pipeline("Pipeline start failure")
            self.stream_disconnected.emit("start_failed")
            return

        self._is_connected = True

    def _protocol_flags(self, protocol: str):
        if protocol == "TCP":
            return GstRtsp.RTSPLowerTrans.TCP
        if protocol == "UDP":
            return GstRtsp.RTSPLowerTrans.UDP | GstRtsp.RTSPLowerTrans.UDP_MCAST
        return (
            GstRtsp.RTSPLowerTrans.UDP
            | GstRtsp.RTSPLowerTrans.UDP_MCAST
            | GstRtsp.RTSPLowerTrans.TCP
        )

    def _on_rtsp_pad_added(self, src: Gst.Element, pad: Gst.Pad, decodebin: Gst.Element) -> None:
        if self._pipeline is None:
            return

        pad_caps = pad.get_current_caps() or pad.query_caps()
        if not pad_caps or pad_caps.get_size() == 0:
            self.log.emit("WARN: RTSP pad has no caps yet")
            return

        structure = pad_caps.get_structure(0)
        caps_name = structure.get_name()
        if "application/x-rtp" not in caps_name:
            return

        media = (structure.get_string("media") or "").lower()
        encoding = (structure.get_string("encoding-name") or "").upper()
        self.log.emit(f"Incoming RTP stream: media={media or 'unknown'} encoding={encoding or 'unknown'}")

        if media and media != "video":
            self.log.emit(f"Ignoring non-video RTP pad (media={media})")
            return

        sink_pad = decodebin.get_static_pad("sink")
        if sink_pad is None:
            self.log.emit("ERROR: decodebin sink pad not available")
            return
        if sink_pad.is_linked():
            return

        result = pad.link(sink_pad)
        if result != Gst.PadLinkReturn.OK:
            self.log.emit(f"ERROR: Failed linking RTSP pad: {result}")

    def _on_decodebin_pad_added(self, decodebin: Gst.Element, pad: Gst.Pad) -> None:
        target = self._crop or self._convert
        if target is None:
            return

        pad_caps = pad.get_current_caps() or pad.query_caps()
        if not pad_caps or pad_caps.get_size() == 0:
            return

        structure = pad_caps.get_structure(0)
        caps_name = structure.get_name()
        if not caps_name.startswith("video/"):
            return

        self._video_width = structure.get_value("width") if structure.has_field("width") else 0
        self._video_height = structure.get_value("height") if structure.has_field("height") else 0

        sink_pad = target.get_static_pad("sink")
        if sink_pad is None:
            self.log.emit("ERROR: decoder target sink pad unavailable")
            return
        if sink_pad.is_linked():
            return

        result = pad.link(sink_pad)
        if result == Gst.PadLinkReturn.OK:
            self.log.emit(f"Decode pad linked: {caps_name}")
            self._apply_zoom_crop()
        else:
            self.log.emit(f"ERROR: Failed linking decodebin pad: {result}")

    def _change_zoom(self, delta: float) -> None:
        if self._crop is None:
            self.log.emit("WARN: Zoom unavailable (videocrop plugin missing)")
            return

        new_zoom = max(1.0, min(self._max_zoom, self._zoom_factor + delta))
        if abs(new_zoom - self._zoom_factor) < 1e-6:
            return

        self._zoom_factor = new_zoom
        self._apply_zoom_crop()
        self.log.emit(f"Zoom: {self._zoom_factor:.2f}x")

    def _reset_zoom(self) -> None:
        if self._crop is None:
            self.log.emit("WARN: Zoom unavailable (videocrop plugin missing)")
            return
        if self._zoom_factor == 1.0:
            return
        self._zoom_factor = 1.0
        self._apply_zoom_crop()
        self.log.emit("Zoom reset: 1.00x")

    def _apply_zoom_crop(self) -> None:
        if self._crop is None:
            return

        if self._zoom_factor <= 1.0:
            self._crop.set_property("left", 0)
            self._crop.set_property("right", 0)
            self._crop.set_property("top", 0)
            self._crop.set_property("bottom", 0)
            return

        width = int(self._video_width or 0)
        height = int(self._video_height or 0)
        if width <= 0 or height <= 0:
            return

        visible_width = max(2, int(width / self._zoom_factor))
        visible_height = max(2, int(height / self._zoom_factor))

        left = max(0, (width - visible_width) // 2)
        right = max(0, width - visible_width - left)
        top = max(0, (height - visible_height) // 2)
        bottom = max(0, height - visible_height - top)

        self._crop.set_property("left", left)
        self._crop.set_property("right", right)
        self._crop.set_property("top", top)
        self._crop.set_property("bottom", bottom)

    def _on_sync_message(self, bus: Gst.Bus, message: Gst.Message) -> None:
        if self._video_window_id is None:
            return

        if message.type != Gst.MessageType.ELEMENT:
            return

        structure = message.get_structure()
        if structure is None:
            return

        if structure.get_name() != "prepare-window-handle":
            return

        sink = message.src
        if sink and hasattr(sink, "set_window_handle"):
            sink.set_window_handle(int(self._video_window_id))

    def _on_bus_message(self, bus: Gst.Bus, message: Gst.Message) -> None:
        msg_type = message.type

        if msg_type == Gst.MessageType.STATE_CHANGED and message.src == self._pipeline:
            old, new, _ = message.parse_state_changed()
            self.state_changed.emit(Gst.Element.state_get_name(old), Gst.Element.state_get_name(new))

        elif msg_type == Gst.MessageType.WARNING:
            err, debug = message.parse_warning()
            self.log.emit(f"WARNING: {err.message}")
            if debug:
                self.log.emit(f"WARNING-DEBUG: {debug}")

        elif msg_type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            self.log.emit(f"ERROR: {err.message}")
            if debug:
                self.log.emit(f"ERROR-DEBUG: {debug}")
            self._teardown_pipeline("Pipeline error")
            self.stream_disconnected.emit("error")

        elif msg_type == Gst.MessageType.EOS:
            self.log.emit("EOS: stream ended")
            self._teardown_pipeline("Received EOS")
            self.stream_disconnected.emit("eos")

    def _teardown_pipeline(self, reason: str) -> None:
        if self._pipeline is None:
            return

        self.log.emit(f"Teardown pipeline: {reason}")

        bus = self._pipeline.get_bus()
        if bus:
            bus.remove_signal_watch()
            bus.disable_sync_message_emission()

        self._pipeline.set_state(Gst.State.NULL)
        self._pipeline = None
        self._source = None
        self._decodebin = None
        self._crop = None
        self._convert = None
        self._queue_el = None
        self._sink = None
        self._is_connected = False
        self._zoom_factor = 1.0
        self._video_width = 0
        self._video_height = 0


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Universal RTSP IP Camera Client")
        self.resize(1100, 750)

        self.worker = GstWorker()
        self.worker.log.connect(self._append_log)
        self.worker.state_changed.connect(self._on_state_changed)
        self.worker.stream_disconnected.connect(self._on_stream_disconnected)
        self.worker.start()

        self.connected = False
        self.reconnect_attempts = 0

        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setSingleShot(True)
        self.reconnect_timer.timeout.connect(self._attempt_reconnect)

        self._build_ui()

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)

        main_layout = QVBoxLayout(root)

        content_layout = QHBoxLayout()

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("rtsp://user:password@camera-ip:554/stream")

        self.latency_input = QSpinBoxNoWheel()
        self.latency_input.setRange(0, 3000)
        self.latency_input.setValue(100)
        self.latency_input.setSuffix(" ms")

        self.protocol_box = QComboBox()
        self.protocol_box.addItems(["AUTO", "TCP", "UDP"])

        self.auto_reconnect_box = QCheckBox("Auto reconnect")
        self.auto_reconnect_box.setChecked(True)

        self.retry_interval_input = QDoubleSpinBoxNoWheel()
        self.retry_interval_input.setRange(0.5, 30.0)
        self.retry_interval_input.setValue(3.0)
        self.retry_interval_input.setSingleStep(0.5)
        self.retry_interval_input.setSuffix(" s")

        self.connect_btn = QPushButton("Connect")
        self.disconnect_btn = QPushButton("Disconnect")
        self.zoom_in_btn = QPushButton("Zoom In")
        self.zoom_out_btn = QPushButton("Zoom Out")
        self.reset_zoom_btn = QPushButton("Reset Zoom")
        self.disconnect_btn.setEnabled(False)
        self.zoom_in_btn.setEnabled(False)
        self.zoom_out_btn.setEnabled(False)
        self.reset_zoom_btn.setEnabled(False)

        self.connect_btn.clicked.connect(self._connect_clicked)
        self.disconnect_btn.clicked.connect(self._disconnect_clicked)
        self.zoom_in_btn.clicked.connect(self._zoom_in_clicked)
        self.zoom_out_btn.clicked.connect(self._zoom_out_clicked)
        self.reset_zoom_btn.clicked.connect(self._reset_zoom_clicked)

        self.video_widget = VideoWidget()
        self.video_widget.zoom_requested.connect(self._on_video_zoom_requested)
        left_layout.addWidget(self.video_widget)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)

        controls = QWidget()
        grid = QGridLayout(controls)

        grid.addWidget(QLabel("RTSP URL"), 0, 0)
        grid.addWidget(self.url_input, 1, 0, 1, 2)

        grid.addWidget(QLabel("Latency"), 2, 0)
        grid.addWidget(self.latency_input, 3, 0)

        grid.addWidget(QLabel("Protocol"), 2, 1)
        grid.addWidget(self.protocol_box, 3, 1)

        grid.addWidget(self.auto_reconnect_box, 4, 0)
        grid.addWidget(self.retry_interval_input, 4, 1)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.connect_btn)
        btn_row.addWidget(self.disconnect_btn)
        grid.addLayout(btn_row, 5, 0, 1, 2)

        zoom_row = QHBoxLayout()
        zoom_row.addWidget(self.zoom_in_btn)
        zoom_row.addWidget(self.zoom_out_btn)
        zoom_row.addWidget(self.reset_zoom_btn)
        grid.addLayout(zoom_row, 6, 0, 1, 2)

        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(1500)

        right_layout.addWidget(controls)
        right_layout.addWidget(QLabel("Monitoring Logs"))
        right_layout.addWidget(self.logs, stretch=1)

        content_layout.addWidget(left_panel, stretch=3)
        content_layout.addWidget(right_panel, stretch=1)

        main_layout.addLayout(content_layout)

    def _connect_clicked(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            self._append_log("ERROR: RTSP URL is required")
            return

        self.reconnect_timer.stop()
        self.reconnect_attempts = 0

        config = StreamConfig(
            url=url,
            latency_ms=self.latency_input.value(),
            protocol=self.protocol_box.currentText(),
        )

        self.worker.connect_stream(config, int(self.video_widget.winId()))
        self.connected = True
        self.connect_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(True)
        self.zoom_in_btn.setEnabled(True)
        self.zoom_out_btn.setEnabled(True)
        self.reset_zoom_btn.setEnabled(True)

    def _disconnect_clicked(self) -> None:
        self.reconnect_timer.stop()
        self.worker.disconnect_stream()
        self.connected = False
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self.zoom_in_btn.setEnabled(False)
        self.zoom_out_btn.setEnabled(False)
        self.reset_zoom_btn.setEnabled(False)

    def _zoom_in_clicked(self) -> None:
        self.worker.zoom_in()

    def _zoom_out_clicked(self) -> None:
        self.worker.zoom_out()

    def _reset_zoom_clicked(self) -> None:
        self.worker.reset_zoom()

    def _on_video_zoom_requested(self, direction: int) -> None:
        if not self.connected:
            return
        if direction > 0:
            self.worker.zoom_in()
        elif direction < 0:
            self.worker.zoom_out()

    def _on_state_changed(self, old_state: str, new_state: str) -> None:
        self._append_log(f"State transition: {old_state} → {new_state}")

    def _on_stream_disconnected(self, reason: str) -> None:
        if not self.connected:
            return

        if not self.auto_reconnect_box.isChecked():
            self._append_log(f"Disconnected ({reason}). Auto reconnect disabled.")
            self.connected = False
            self.connect_btn.setEnabled(True)
            self.disconnect_btn.setEnabled(False)
            return

        interval_ms = int(self.retry_interval_input.value() * 1000)
        self.reconnect_attempts += 1
        self._append_log(
            f"Reconnect attempt #{self.reconnect_attempts} scheduled in {self.retry_interval_input.value():.1f}s"
        )
        self.reconnect_timer.start(interval_ms)

    def _attempt_reconnect(self) -> None:
        if not self.connected:
            return

        url = self.url_input.text().strip()
        if not url:
            self._append_log("ERROR: Reconnect canceled, URL is empty")
            self.connected = False
            self.connect_btn.setEnabled(True)
            self.disconnect_btn.setEnabled(False)
            return

        config = StreamConfig(
            url=url,
            latency_ms=self.latency_input.value(),
            protocol=self.protocol_box.currentText(),
        )
        self.worker.connect_stream(config, int(self.video_widget.winId()))

    def _append_log(self, message: str) -> None:
        self.logs.appendPlainText(message)

    def closeEvent(self, event) -> None:
        self.reconnect_timer.stop()
        self.worker.stop()
        super().closeEvent(event)


class QDoubleSpinBoxNoWheel(QDoubleSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class QSpinBoxNoWheel(QSpinBox):
    def wheelEvent(self, event):
        event.ignore()


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
