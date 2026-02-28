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
    def __init__(self) -> None:
        super().__init__()
        self.setAttribute(Qt.WA_NativeWindow)
        self.setMinimumSize(640, 360)
        self.setStyleSheet("background-color: black;")


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
        self._convert: Gst.Element | None = None
        self._queue_el: Gst.Element | None = None
        self._sink: Gst.Element | None = None

        self._video_window_id: int | None = None
        self._is_connected = False
        self._command_source: GLib.Source | None = None

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
        pipeline.add(convert)
        pipeline.add(queue_el)
        pipeline.add(sink)

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
        self._convert = convert
        self._queue_el = queue_el
        self._sink = sink

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
        if self._convert is None:
            return

        pad_caps = pad.get_current_caps() or pad.query_caps()
        if not pad_caps or pad_caps.get_size() == 0:
            return

        structure = pad_caps.get_structure(0)
        caps_name = structure.get_name()
        if not caps_name.startswith("video/"):
            return

        sink_pad = self._convert.get_static_pad("sink")
        if sink_pad is None:
            self.log.emit("ERROR: videoconvert sink pad unavailable")
            return
        if sink_pad.is_linked():
            return

        result = pad.link(sink_pad)
        if result == Gst.PadLinkReturn.OK:
            self.log.emit(f"Decode pad linked: {caps_name}")
        else:
            self.log.emit(f"ERROR: Failed linking decodebin pad: {result}")

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
        self._convert = None
        self._queue_el = None
        self._sink = None
        self._is_connected = False


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

        controls = QWidget()
        grid = QGridLayout(controls)

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
        self.disconnect_btn.setEnabled(False)

        self.connect_btn.clicked.connect(self._connect_clicked)
        self.disconnect_btn.clicked.connect(self._disconnect_clicked)

        grid.addWidget(QLabel("RTSP URL"), 0, 0)
        grid.addWidget(self.url_input, 0, 1, 1, 5)

        grid.addWidget(QLabel("Latency"), 1, 0)
        grid.addWidget(self.latency_input, 1, 1)

        grid.addWidget(QLabel("Protocol"), 1, 2)
        grid.addWidget(self.protocol_box, 1, 3)

        grid.addWidget(self.auto_reconnect_box, 1, 4)
        grid.addWidget(self.retry_interval_input, 1, 5)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.connect_btn)
        btn_row.addWidget(self.disconnect_btn)
        btn_row.addStretch(1)

        self.video_widget = VideoWidget()

        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(1500)

        main_layout.addWidget(controls)
        main_layout.addLayout(btn_row)
        main_layout.addWidget(self.video_widget, stretch=3)
        main_layout.addWidget(QLabel("Monitoring Logs"))
        main_layout.addWidget(self.logs, stretch=2)

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

    def _disconnect_clicked(self) -> None:
        self.reconnect_timer.stop()
        self.worker.disconnect_stream()
        self.connected = False
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)

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
