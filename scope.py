import sys
import queue
import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QScrollArea, QGroupBox, QPushButton, QLabel,
    QDoubleSpinBox, QSpinBox, QComboBox, QLineEdit, QMessageBox,
    QFormLayout, QCheckBox, QTabWidget, QTextEdit
)
from PyQt6.QtCore import Qt, QObject, pyqtSignal, QThread
from PyQt6.QtGui import QFont

import chipwhisperer as cw

import threading

DARK_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #1a1a2e;
    color: #e0e0e0;
    font-family: "Monospace", "Consolas", "Courier New";
    font-size: 11px;
}
QGroupBox {
    border: 1px solid #3a3a5c;
    border-radius: 4px;
    margin-top: 8px;
    padding-top: 14px;
    font-weight: bold;
    color: #8888cc;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
}
QPushButton {
    background-color: #2a2a4a;
    border: 1px solid #4a4a6a;
    border-radius: 3px;
    padding: 5px 12px;
    color: #e0e0e0;
    min-height: 20px;
}
QPushButton:hover { background-color: #3a3a5a; }
QPushButton:pressed { background-color: #1a1a3a; }
QPushButton:disabled { color: #555; border-color: #333; }
QPushButton#btn_start {
    background-color: #1a3a1a;
    border-color: #2a5a2a;
}
QPushButton#btn_start:hover { background-color: #2a4a2a; }
QPushButton#btn_stop {
    background-color: #3a1a1a;
    border-color: #5a2a2a;
}
QPushButton#btn_stop:hover { background-color: #4a2a2a; }
QSpinBox, QDoubleSpinBox, QComboBox, QLineEdit {
    background-color: #0e0e1e;
    border: 1px solid #3a3a5c;
    border-radius: 2px;
    padding: 3px;
    color: #00ff88;
    selection-background-color: #3a3a5c;
}
QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {
    border-color: #6666aa;
}
QComboBox::drop-down {
    border: none;
    background: #2a2a4a;
}
QComboBox QAbstractItemView {
    background-color: #1a1a2e;
    color: #e0e0e0;
    selection-background-color: #3a3a5c;
}
QScrollArea { border: none; }
QScrollBar:vertical {
    background: #1a1a2e;
    width: 8px;
}
QScrollBar::handle:vertical {
    background: #3a3a5c;
    border-radius: 4px;
    min-height: 20px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QStatusBar {
    background-color: #0e0e1e;
    color: #00ff88;
    border-top: 1px solid #3a3a5c;
    font-family: "Monospace", "Consolas";
}
QTabWidget::pane {
    border: 1px solid #3a3a5c;
    background: #1a1a2e;
}
QTabBar::tab {
    background: #2a2a4a;
    border: 1px solid #3a3a5c;
    padding: 4px 10px;
    color: #8888cc;
    border-top-left-radius: 3px;
    border-top-right-radius: 3px;
}
QTabBar::tab:selected {
    background: #1a1a2e;
    color: #e0e0e0;
    border-bottom-color: #1a1a2e;
}
QCheckBox { color: #e0e0e0; spacing: 5px; }
QCheckBox::indicator {
    width: 14px; height: 14px;
    border: 1px solid #4a4a6a;
    border-radius: 2px;
    background: #0e0e1e;
}
QCheckBox::indicator:checked { background: #00ff88; border-color: #00ff88; }
QLabel#measurement_label { color: #00ff88; font-family: "Monospace"; }
QLabel#status_value { color: #ffaa00; }
QSplitter::handle { background: #3a3a5c; width: 2px; }
"""

class ScopeConnection:
    def __init__(self):
        self.scope = None
        self.device_type = None
        self.usb_lock = threading.RLock()
        self.is_armed = False

    def connect(self):
        self.scope = cw.scope()
        self.device_type = self.scope.get_name()

        # Bootstrap internal clocks manually to avoid default_setup() deadlocks
        # while preventing the ADC from sitting at 0Hz (which crashes capture APIs)
        try:
            self.scope.gain.mode = "low"
            self.scope.gain.db = -15.0
            self.scope.adc.samples = 35000
            self.scope.adc.offset = 0
            self.scope.adc.decimate = 10
            self.scope.adc.basic_mode = "rising_edge"
            self.scope.trigger.module = "ADC"
            self.scope.trigger.level = 0.25
            
            if hasattr(self.scope, "io"):
                self.scope.io.hs2 = "clkgen"

            if self.scope._is_husky:
                self.scope.clock.clkgen_src = "system"
                self.scope.clock.clkgen_freq = 7.37e6
                self.scope.clock.adc_mul = 1
                self.scope.clock.reset_adc()
            else:
                self.scope.clock.clkgen_src = "internal"
                self.scope.clock.clkgen_freq = 7.37e6
                self.scope.clock.adc_src = "clkgen_x1"
                self.scope.clock.reset_adc()
        except Exception as e:
            print(f"Non-fatal clock init warning: {e}")

        return self.device_type

    def default_setup(self):
        if self.scope:
            self.scope.default_setup()

    def disconnect(self):
        if self.scope:
            self.scope.dis()
        self.scope = None
        self.device_type = None

    @property
    def is_husky(self):
        if not self.scope:
            return False
        return self.scope._is_husky

    def apply_setting(self, attr_path, value):
        with self.usb_lock:
            if not self.scope:
                return
            parts = attr_path.split(".")
            obj = self.scope
            for part in parts[:-1]:
                obj = getattr(obj, part)
            setattr(obj, parts[-1], value)
            
            # ChipWhisperer requires ADC reset after clock parameters change
            if parts[0] == "clock" and hasattr(self.scope.clock, "reset_adc"):
                try:
                    self.scope.clock.reset_adc()
                except Exception:
                    pass

    def read_setting(self, attr_path):
        with self.usb_lock:
            if not self.scope:
                return None
            parts = attr_path.split(".")
            obj = self.scope
            try:
                for part in parts[:-1]:
                    obj = getattr(obj, part)
                return getattr(obj, parts[-1])
            except AttributeError:
                return None

    def read_status(self):
        with self.usb_lock:
            if not self.scope or self.is_armed:
                return {}
                
            res = {"device_name": self.device_type}
            
            def safe_read(obj, attr):
                try:
                    val = getattr(obj, attr)
                    return val
                except Exception:
                    return None

            if hasattr(self.scope, "clock"):
                res["adc_freq"] = safe_read(self.scope.clock, "adc_freq")
                res["adc_rate"] = safe_read(self.scope.clock, "adc_rate")
                res["clkgen_freq"] = safe_read(self.scope.clock, "clkgen_freq")
                
            if hasattr(self.scope, "adc"):
                res["trig_count"] = safe_read(self.scope.adc, "trig_count")
                
            return res


    def read_params(self):
        """Return list of (attr_path, value) for all configurable parameters."""
        with self.usb_lock:
            if not self.scope or self.is_armed:
                return []
            params = []
            settings = [
                "gain.db", "gain.mode",
                "adc.samples", "adc.offset", "adc.presamples", "adc.decimate",
                "adc.basic_mode", "adc.timeout",
                "trigger.module", "trigger.triggers", "trigger.level",
                "clock.clkgen_src", "clock.clkgen_freq",
            ]
            if self.is_husky:
                settings += ["clock.adc_mul", "adc.bits_per_sample"]
            else:
                settings += ["clock.adc_src"]
            for attr in settings:
                parts = attr.split(".")
                try:
                    obj = self.scope
                    for part in parts[:-1]:
                        obj = getattr(obj, part)
                    val = getattr(obj, parts[-1])
                    params.append((attr, val))
                except (AttributeError, Exception):
                    pass
            return params


class CaptureWorker(QObject):
    trace_ready = pyqtSignal(object, object)  # (trace_data, status_dict)
    error_occurred = pyqtSignal(str)
    capture_status = pyqtSignal(str)

    def __init__(self, scope_connection):
        super().__init__()
        self._scope = scope_connection
        self._running = False
        self._single_shot = False
        self._settings_queue = queue.Queue()

    def queue_setting(self, attr_path, value):
        self._settings_queue.put((attr_path, value))

    def _apply_pending_settings(self):
        while not self._settings_queue.empty():
            attr_path, value = self._settings_queue.get_nowait()
            self._scope.apply_setting(attr_path, value)

    def run_loop(self):
        while self._running or self._single_shot:
            self._single_shot = False
            self._apply_pending_settings()
            try:
                if not self._scope.scope:
                    self._running = False
                    break
                self.capture_status.emit("Armed...")
                try:
                    self._scope.is_armed = True
                    self._scope.scope.arm()
                    self.capture_status.emit("Waiting for trigger...")
                    
                    timed_out = self._scope.scope.capture()
                    data = self._scope.scope.get_last_trace()
                finally:
                    self._scope.is_armed = False
                
                if timed_out:
                    self.capture_status.emit("Auto-Forced (Timeout)")
                else:
                    self.capture_status.emit("Captured")
                    
                if data is not None:
                    status = self._scope.read_status()
                    status['_params'] = self._scope.read_params()
                    self.trace_ready.emit(data, status)
                    
            except Exception as e:
                self.error_occurred.emit(str(e))
                self._running = False
                break


class WaveformPlot(pg.PlotWidget):
    trigger_level_changed = pyqtSignal(float)
    cursor_moved = pyqtSignal(int, float)  # cursor_index, position

    def __init__(self):
        super().__init__()
        self.setBackground('#0a0a14')
        self.showGrid(x=True, y=True, alpha=0.15)
        self.setMouseEnabled(x=True, y=True)
        self.setYRange(-0.5, 0.5)
        self.getPlotItem().setLabel('left', 'Voltage', units='V')
        self.getPlotItem().setLabel('bottom', 'Sample')

        # Phosphor-green waveform
        self.curve = self.plot(pen=pg.mkPen('#00ff88', width=1))

        # Zero baseline
        self.zero_line = pg.InfiniteLine(
            pos=0, angle=0,
            pen=pg.mkPen('#444466', width=1, style=Qt.PenStyle.DashLine),
            movable=False
        )
        self.addItem(self.zero_line)

        # Zero label
        self.zero_label = pg.TextItem('0V', color='#666688', anchor=(0, 0.5))
        self.zero_label.setPos(0, 0)
        self.addItem(self.zero_label)

        # Trigger level line (draggable)
        self.trigger_line = pg.InfiniteLine(
            pos=0.25, angle=0,
            pen=pg.mkPen('#ff6600', width=1, style=Qt.PenStyle.DashDotLine),
            movable=True,
            label='Trig: {value:.3f}V',
            labelOpts={'color': '#ff6600', 'position': 0.95, 'fill': '#1a1a2e80'}
        )
        self.trigger_line.sigPositionChanged.connect(self._on_trigger_dragged)
        self.addItem(self.trigger_line)

        # Trigger flag (arrow marker on right edge)
        self.trigger_flag = pg.ArrowItem(
            angle=180, tipAngle=30, baseAngle=20, headLen=12, tailLen=0,
            pen=pg.mkPen('#ff6600'), brush='#ff6600'
        )
        self.addItem(self.trigger_flag)
        self._update_trigger_flag()

        # Measurement cursors (two vertical lines)
        cursor_pen_a = pg.mkPen('#00ccff', width=1, style=Qt.PenStyle.DashLine)
        cursor_pen_b = pg.mkPen('#ff44cc', width=1, style=Qt.PenStyle.DashLine)

        self.cursor_a = pg.InfiniteLine(
            pos=0, angle=90, pen=cursor_pen_a, movable=True,
            label='A: {value:.0f}',
            labelOpts={'color': '#00ccff', 'position': 0.95, 'fill': '#1a1a2e80'}
        )
        self.cursor_b = pg.InfiniteLine(
            pos=100, angle=90, pen=cursor_pen_b, movable=True,
            label='B: {value:.0f}',
            labelOpts={'color': '#ff44cc', 'position': 0.90, 'fill': '#1a1a2e80'}
        )

        # Cursor delta label
        self.cursor_delta_label = pg.TextItem('', color='#e0e0e0', anchor=(0.5, 0))
        self.addItem(self.cursor_delta_label)

        self.cursor_a.sigPositionChanged.connect(lambda: self._on_cursor_moved())
        self.cursor_b.sigPositionChanged.connect(lambda: self._on_cursor_moved())

        # Cursors hidden by default
        self._cursors_visible = False
        self.cursor_a.setVisible(False)
        self.cursor_b.setVisible(False)
        self.cursor_delta_label.setVisible(False)

        self._last_data = None

    def _on_trigger_dragged(self):
        self.trigger_level_changed.emit(self.trigger_line.value())
        self._update_trigger_flag()

    def _update_trigger_flag(self):
        vr = self.viewRange()
        x_max = vr[0][1]
        self.trigger_flag.setPos(x_max, self.trigger_line.value())

    def set_trigger_level(self, level):
        self.trigger_line.blockSignals(True)
        self.trigger_line.setValue(level)
        self.trigger_line.blockSignals(False)
        self._update_trigger_flag()

    def set_cursors_visible(self, visible):
        self._cursors_visible = visible
        self.cursor_a.setVisible(visible)
        self.cursor_b.setVisible(visible)
        self.cursor_delta_label.setVisible(visible)

    def _on_cursor_moved(self):
        a = self.cursor_a.value()
        b = self.cursor_b.value()
        delta = abs(b - a)
        mid = (a + b) / 2
        self.cursor_delta_label.setText(f'\u0394 = {delta:.0f} samples')
        self.cursor_delta_label.setPos(mid, self.viewRange()[1][1])

    def update_trace(self, data: np.ndarray):
        self._last_data = data
        self.curve.setData(data)
        # Keep zero label at left edge
        vr = self.viewRange()
        self.zero_label.setPos(vr[0][0] + 5, 0)
        self._update_trigger_flag()


class FFTPlot(pg.PlotWidget):
    def __init__(self):
        super().__init__()
        self.setBackground('#0a0a14')
        self.showGrid(x=True, y=True, alpha=0.15)
        self.setMouseEnabled(x=True, y=True)
        self.getPlotItem().setLabel('left', 'Magnitude', units='dB')
        self.getPlotItem().setLabel('bottom', 'Frequency', units='Hz')
        self.curve = self.plot(pen=pg.mkPen('#ff6600', width=1))
        self._sample_rate = 1.0

    def set_sample_rate(self, rate):
        self._sample_rate = rate if rate else 1.0

    def update_trace(self, data: np.ndarray):
        if data is None or len(data) < 2:
            return
        n = len(data)
        window = np.hanning(n)
        windowed = data * window
        fft_vals = np.fft.rfft(windowed)
        magnitude = np.abs(fft_vals)
        # Convert to dB, floor at -120dB
        magnitude[magnitude == 0] = 1e-12
        mag_db = 20 * np.log10(magnitude)
        freqs = np.fft.rfftfreq(n, d=1.0 / self._sample_rate)
        self.curve.setData(freqs, mag_db)


class ControlPanel(QWidget):
    setting_changed = pyqtSignal(str, object)
    connect_clicked = pyqtSignal()
    disconnect_clicked = pyqtSignal()
    default_setup_clicked = pyqtSignal()
    auto_setup_clicked = pyqtSignal()
    cursors_toggled = pyqtSignal(bool)
    fft_toggled = pyqtSignal(bool)
    volts_div_changed = pyqtSignal(float)
    time_div_changed = pyqtSignal(float)
    acq_mode_changed = pyqtSignal(str)  # "auto", "normal", "single", "hold"

    def __init__(self):
        super().__init__()
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)

        # Connection & Capture (always visible at top)
        self._setup_connection_group(main_layout)
        self._setup_capture_group(main_layout)

        # Tabbed scope sections
        self.tabs = QTabWidget()
        self._setup_vertical_tab()
        self._setup_horizontal_tab()
        self._setup_trigger_tab()
        self._setup_clock_tab()
        self._setup_display_tab()
        self._setup_script_tab()
        main_layout.addWidget(self.tabs)

        # Status & Measurements (always visible at bottom)
        self._setup_status_group(main_layout)
        self._setup_measurements_group(main_layout)
        main_layout.addStretch()

    def _setup_connection_group(self, parent_layout):
        gb = QGroupBox("Connection")
        l = QHBoxLayout()
        l.setSpacing(4)
        self.btn_connect = QPushButton("\u26a1 Connect")
        self.btn_disconnect = QPushButton("\u26d4 Disconnect")
        self.btn_default = QPushButton("\u21bb Default")
        self.btn_auto_setup = QPushButton("\u2699 Auto Setup")
        self.btn_auto_setup.setStyleSheet(
            "QPushButton { background-color: #1a2a4a; border-color: #2a4a7a; }"
            "QPushButton:hover { background-color: #2a3a5a; }"
        )

        self.btn_connect.clicked.connect(self.connect_clicked.emit)
        self.btn_disconnect.clicked.connect(self.disconnect_clicked.emit)
        self.btn_default.clicked.connect(self.default_setup_clicked.emit)
        self.btn_auto_setup.clicked.connect(self.auto_setup_clicked.emit)

        l.addWidget(self.btn_connect)
        l.addWidget(self.btn_disconnect)
        l.addWidget(self.btn_default)
        l.addWidget(self.btn_auto_setup)
        gb.setLayout(l)
        parent_layout.addWidget(gb)

    def _setup_capture_group(self, parent_layout):
        gb = QGroupBox("Acquisition")
        l = QHBoxLayout()
        l.setSpacing(4)

        self.btn_auto = QPushButton("\u25b6 Auto")
        self.btn_auto.setObjectName("btn_start")
        self.btn_auto.setCheckable(True)
        self.btn_normal = QPushButton("\u25b6 Normal")
        self.btn_normal.setObjectName("btn_start")
        self.btn_normal.setCheckable(True)
        self.btn_single = QPushButton("\u25ab Single")
        self.btn_hold = QPushButton("\u23f8 Hold")
        self.btn_hold.setObjectName("btn_stop")

        self.btn_auto.clicked.connect(lambda: self._set_acq_mode("auto"))
        self.btn_normal.clicked.connect(lambda: self._set_acq_mode("normal"))
        self.btn_single.clicked.connect(lambda: self._set_acq_mode("single"))
        self.btn_hold.clicked.connect(lambda: self._set_acq_mode("hold"))

        l.addWidget(self.btn_auto)
        l.addWidget(self.btn_normal)
        l.addWidget(self.btn_single)
        l.addWidget(self.btn_hold)
        gb.setLayout(l)
        parent_layout.addWidget(gb)

    def _set_acq_mode(self, mode):
        self.btn_auto.setChecked(mode == "auto")
        self.btn_normal.setChecked(mode == "normal")
        self.acq_mode_changed.emit(mode)

    def _setup_vertical_tab(self):
        w = QWidget()
        l = QFormLayout(w)
        l.setContentsMargins(6, 6, 6, 6)

        self.gain_db_spin = QDoubleSpinBox()
        self.gain_db_spin.setRange(-15.0, 65.0)
        self.gain_db_spin.setSingleStep(0.5)
        self.gain_db_spin.setSuffix(" dB")
        self.gain_db_spin.valueChanged.connect(lambda v: self.setting_changed.emit("gain.db", v))

        self.gain_mode_combo = QComboBox()
        self.gain_mode_combo.addItems(["low", "high"])
        self.gain_mode_combo.currentTextChanged.connect(lambda v: self.setting_changed.emit("gain.mode", v))

        self.volts_div_combo = QComboBox()
        self.volts_div_combo.addItems([
            '500 mV', '200 mV', '100 mV', '50 mV', '20 mV', '10 mV', '5 mV'
        ])
        self.volts_div_combo.setCurrentText('100 mV')
        self._volts_div_map = {
            '500 mV': 0.5, '200 mV': 0.2, '100 mV': 0.1,
            '50 mV': 0.05, '20 mV': 0.02, '10 mV': 0.01, '5 mV': 0.005
        }
        self.volts_div_combo.currentTextChanged.connect(
            lambda t: self.volts_div_changed.emit(self._volts_div_map[t])
        )

        l.addRow("V/Div", self.volts_div_combo)
        l.addRow("Gain", self.gain_db_spin)
        l.addRow("Mode", self.gain_mode_combo)

        self.tabs.addTab(w, "Vertical")

    def _setup_horizontal_tab(self):
        w = QWidget()
        l = QFormLayout(w)
        l.setContentsMargins(6, 6, 6, 6)

        self.samples_spin = QSpinBox()
        self.samples_spin.setRange(1, 131070)
        self.samples_spin.valueChanged.connect(lambda v: self.setting_changed.emit("adc.samples", v))

        self.offset_spin = QSpinBox()
        self.offset_spin.setRange(0, 2147483647)
        self.offset_spin.valueChanged.connect(lambda v: self.setting_changed.emit("adc.offset", v))

        self.presamples_spin = QSpinBox()
        self.presamples_spin.setRange(0, 131070)
        self.presamples_spin.valueChanged.connect(lambda v: self.setting_changed.emit("adc.presamples", v))

        self.decimate_spin = QSpinBox()
        self.decimate_spin.setRange(0, 65535)
        self.decimate_spin.valueChanged.connect(lambda v: self.setting_changed.emit("adc.decimate", v))

        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(0.1, 60.0)
        self.timeout_spin.setValue(2.0)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.valueChanged.connect(lambda v: self.setting_changed.emit("adc.timeout", v))

        self.time_div_combo = QComboBox()
        self.time_div_combo.addItems([
            '10k samples', '5k samples', '2k samples', '1k samples',
            '500 samples', '200 samples', '100 samples'
        ])
        self.time_div_combo.setCurrentText('5k samples')
        self._time_div_map = {
            '10k samples': 10000, '5k samples': 5000, '2k samples': 2000,
            '1k samples': 1000, '500 samples': 500, '200 samples': 200,
            '100 samples': 100
        }
        self.time_div_combo.currentTextChanged.connect(
            lambda t: self.time_div_changed.emit(self._time_div_map[t])
        )

        l.addRow("Time/Div", self.time_div_combo)
        l.addRow("Samples", self.samples_spin)
        l.addRow("Offset", self.offset_spin)
        l.addRow("Presamples", self.presamples_spin)
        l.addRow("Decimate", self.decimate_spin)
        l.addRow("Timeout", self.timeout_spin)

        self.tabs.addTab(w, "Horizontal")

    def _setup_trigger_tab(self):
        w = QWidget()
        l = QFormLayout(w)
        l.setContentsMargins(6, 6, 6, 6)

        self.trigger_module_combo = QComboBox()
        self.trigger_module_combo.addItems(['basic', 'ADC', 'SAD'])
        self.trigger_module_combo.currentTextChanged.connect(lambda v: self.setting_changed.emit("trigger.module", v))

        self.basic_mode_combo = QComboBox()
        self.basic_mode_combo.addItems(['rising_edge', 'falling_edge', 'high', 'low', 'always'])
        self.basic_mode_combo.currentTextChanged.connect(lambda v: self.setting_changed.emit("adc.basic_mode", v))

        self.triggers_edit = QLineEdit()
        self.triggers_edit.setText("tio4")
        self.triggers_edit.editingFinished.connect(lambda: self.setting_changed.emit("trigger.triggers", self.triggers_edit.text()))

        self.adc_trigger_level_spin = QDoubleSpinBox()
        self.adc_trigger_level_spin.setRange(-32768.0, 32767.0)
        self.adc_trigger_level_spin.setDecimals(4)
        self.adc_trigger_level_spin.setSingleStep(0.01)
        self.adc_trigger_level_spin.valueChanged.connect(lambda v: self.setting_changed.emit("trigger.level", v))

        l.addRow("Module", self.trigger_module_combo)
        l.addRow("Edge/Level", self.basic_mode_combo)
        l.addRow("Pin", self.triggers_edit)
        l.addRow("ADC Level", self.adc_trigger_level_spin)

        self.tabs.addTab(w, "Trigger")

    def _setup_clock_tab(self):
        w = QWidget()
        l = QFormLayout(w)
        l.setContentsMargins(6, 6, 6, 6)

        self.clkgen_src_combo = QComboBox()
        self.clkgen_src_combo.addItems(['system', 'extclk', 'internal'])
        self.clkgen_src_combo.currentTextChanged.connect(lambda v: self.setting_changed.emit("clock.clkgen_src", v))

        self.clkgen_freq_spin = QDoubleSpinBox()
        self.clkgen_freq_spin.setRange(0.1, 200.0)
        self.clkgen_freq_spin.setSuffix(" MHz")
        self.clkgen_freq_spin.valueChanged.connect(lambda v: self.setting_changed.emit("clock.clkgen_freq", v * 1e6))

        self.adc_mul_spin = QSpinBox()
        self.adc_mul_spin.setRange(1, 50)
        self.adc_mul_spin.valueChanged.connect(lambda v: self.setting_changed.emit("clock.adc_mul", v))

        self.adc_src_combo = QComboBox()
        self.adc_src_combo.addItems(['clkgen_x1', 'clkgen_x4', 'extclk_x1', 'extclk_x4'])
        self.adc_src_combo.currentTextChanged.connect(lambda v: self.setting_changed.emit("clock.adc_src", v))

        self.adc_bits_combo = QComboBox()
        self.adc_bits_combo.addItems(['8', '12'])
        self.adc_bits_combo.currentTextChanged.connect(lambda v: self.setting_changed.emit("adc.bits_per_sample", int(v)))

        l.addRow("Source", self.clkgen_src_combo)
        l.addRow("Frequency", self.clkgen_freq_spin)
        l.addRow("ADC mul", self.adc_mul_spin)
        l.addRow("ADC src", self.adc_src_combo)
        l.addRow("ADC bits", self.adc_bits_combo)

        self.adc_mul_widget = self.adc_mul_spin
        self.adc_src_widget = self.adc_src_combo
        self.bits_per_sample_widget = self.adc_bits_combo

        self.tabs.addTab(w, "Clock")

    def _setup_display_tab(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(6, 6, 6, 6)

        self.cursors_check = QCheckBox("Enable Cursors")
        self.cursors_check.toggled.connect(self.cursors_toggled.emit)
        l.addWidget(self.cursors_check)

        self.fft_check = QCheckBox("Show FFT")
        self.fft_check.toggled.connect(self.fft_toggled.emit)
        l.addWidget(self.fft_check)
        l.addStretch()

        self.tabs.addTab(w, "Display")

    def _setup_script_tab(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(6, 6, 6, 6)
        lbl = QLabel("Current device parameters (copy for scripting):")
        lbl.setObjectName("measurement_label")
        l.addWidget(lbl)
        self.script_params_text = QTextEdit()
        self.script_params_text.setReadOnly(True)
        self.script_params_text.setStyleSheet(
            "QTextEdit { background: #0e0e1e; color: #00ff88; "
            "font-family: 'Monospace', 'Consolas'; font-size: 11px; "
            "border: 1px solid #3a3a5c; }"
        )
        self.script_params_text.setPlainText("# Connect to device first")
        l.addWidget(self.script_params_text)
        self.tabs.addTab(w, "Script")

    def _setup_status_group(self, parent_layout):
        gb = QGroupBox("Status")
        l = QFormLayout()
        l.setSpacing(2)
        self.lbl_device = QLabel("-")
        self.lbl_device.setObjectName("status_value")
        self.lbl_adc_freq = QLabel("-")
        self.lbl_adc_freq.setObjectName("status_value")
        self.lbl_adc_rate = QLabel("-")
        self.lbl_adc_rate.setObjectName("status_value")
        self.lbl_clkgen_freq = QLabel("-")
        self.lbl_clkgen_freq.setObjectName("status_value")
        self.lbl_trig_count = QLabel("-")
        self.lbl_trig_count.setObjectName("status_value")

        l.addRow("Device:", self.lbl_device)
        l.addRow("ADC freq:", self.lbl_adc_freq)
        l.addRow("ADC rate:", self.lbl_adc_rate)
        l.addRow("Clkgen:", self.lbl_clkgen_freq)
        l.addRow("Trig cnt:", self.lbl_trig_count)
        gb.setLayout(l)
        parent_layout.addWidget(gb)

    def _setup_measurements_group(self, parent_layout):
        gb = QGroupBox("Measurements")
        l = QVBoxLayout()
        l.setSpacing(2)
        self.lbl_vstats = QLabel("Vmin: -  Vmax: -  Vpp: -")
        self.lbl_vstats.setObjectName("measurement_label")
        self.lbl_freq = QLabel("Freq: -")
        self.lbl_freq.setObjectName("measurement_label")
        l.addWidget(self.lbl_vstats)
        l.addWidget(self.lbl_freq)
        gb.setLayout(l)
        parent_layout.addWidget(gb)

    def update_status(self, stats):
        self.lbl_device.setText(str(stats.get("device_name", "-")))
        af = stats.get("adc_freq", 0)
        self.lbl_adc_freq.setText(f"{af/1e6:.2f} MHz" if af else "-")
        ar = stats.get("adc_rate", 0)
        self.lbl_adc_rate.setText(f"{ar/1e6:.2f} MHz" if ar else "-")
        cf = stats.get("clkgen_freq", 0)
        self.lbl_clkgen_freq.setText(f"{cf/1e6:.2f} MHz" if cf else "-")
        pc = stats.get("trig_count", "-")
        self.lbl_trig_count.setText(str(pc))

    def update_script_params(self, params):
        lines = ["import chipwhisperer as cw", "scope = cw.scope()", ""]
        for attr, val in params:
            if isinstance(val, str):
                lines.append(f'scope.{attr} = "{val}"')
            elif isinstance(val, float):
                lines.append(f'scope.{attr} = {val}')
            else:
                lines.append(f'scope.{attr} = {val}')
        self.script_params_text.setPlainText("\n".join(lines))

    def update_measurements(self, meas):
        self.lbl_vstats.setText(f"Vmin: {meas['vmin']:.3f}  Vmax: {meas['vmax']:.3f}  Vpp: {meas['vpp']:.3f}")
        if meas['freq']:
            self.lbl_freq.setText(f"Freq: ~{meas['freq']/1e6:.2f} MHz")
        else:
            self.lbl_freq.setText("Freq: -")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CW-Scope")
        self.resize(1000, 600)

        self.scope_connection = ScopeConnection()

        # UI Setup
        self.control_panel = ControlPanel()
        self.waveform_plot = WaveformPlot()
        self.fft_plot = FFTPlot()
        self.fft_plot.setVisible(False)

        # Right side: waveform + FFT stacked vertically
        plot_splitter = QSplitter(Qt.Orientation.Vertical)
        plot_splitter.addWidget(self.waveform_plot)
        plot_splitter.addWidget(self.fft_plot)
        plot_splitter.setSizes([400, 200])

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Wrap control panel in scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.control_panel)
        scroll.setMinimumWidth(320)

        splitter.addWidget(scroll)
        splitter.addWidget(plot_splitter)
        splitter.setSizes([320, 680])

        self.setCentralWidget(splitter)

        # Status Bar
        self.status_bar = self.statusBar()
        self.status_bar.showMessage("Ready")

        # Thread Setup
        self.capture_thread = QThread()
        self.capture_worker = CaptureWorker(self.scope_connection)
        self.capture_worker.moveToThread(self.capture_thread)
        self.capture_thread.started.connect(self.capture_worker.run_loop)
        
        self.capture_worker.trace_ready.connect(self.on_trace_ready)
        self.capture_worker.error_occurred.connect(self.on_error)
        self.capture_worker.capture_status.connect(self.status_bar.showMessage)

        # Wire ControlPanel signals directly pushing to the thread-safe python queue
        # to bypass the background QThread's blocked event-loop logic
        self.control_panel.setting_changed.connect(
            lambda attr, val: self.capture_worker._settings_queue.put_nowait((attr, val))
        )
        self.control_panel.connect_clicked.connect(self.on_connect)
        self.control_panel.disconnect_clicked.connect(self.on_disconnect)
        self.control_panel.default_setup_clicked.connect(self.on_default_setup)
        self.control_panel.auto_setup_clicked.connect(self.on_auto_setup)

        self.control_panel.acq_mode_changed.connect(self.on_acq_mode)

        # Cursor and FFT toggles
        self.control_panel.cursors_toggled.connect(self.waveform_plot.set_cursors_visible)
        self.control_panel.fft_toggled.connect(self.fft_plot.setVisible)

        # Trigger line drag on plot syncs to spinbox and device
        self.waveform_plot.trigger_level_changed.connect(self._on_plot_trigger_drag)
        # Spinbox trigger level syncs to plot line
        self.control_panel.adc_trigger_level_spin.valueChanged.connect(
            self.waveform_plot.set_trigger_level
        )

        # View division controls
        self.control_panel.volts_div_changed.connect(self._on_volts_div)
        self.control_panel.time_div_changed.connect(self._on_time_div)

        self._enable_controls(False)

    def _enable_controls(self, connected):
        self.control_panel.btn_connect.setEnabled(not connected)
        self.control_panel.btn_disconnect.setEnabled(connected)
        self.control_panel.btn_default.setEnabled(connected)
        self.control_panel.btn_auto_setup.setEnabled(connected)
        self.control_panel.btn_auto.setEnabled(connected)
        self.control_panel.btn_normal.setEnabled(connected)
        self.control_panel.btn_single.setEnabled(connected)
        self.control_panel.btn_hold.setEnabled(connected)

    def on_connect(self):
        self._enable_controls(False)
        self.control_panel.btn_connect.setEnabled(False)
        self.status_bar.showMessage("Connecting... Please wait.")
        QApplication.processEvents()  # Force GUI update before blocking USB calls

        try:
            device_type = self.scope_connection.connect()
            # Deliberately skipping default_setup() on connection 
            # to solve the hardware deadlock when no target CW-board is attached. 
            # (Users can manually trigger default setup via the designated button)
            self._update_ui_for_device()
            self._pull_settings_to_ui()
            self._enable_controls(True)
            self.status_bar.showMessage(f"Connected to {device_type}")
            self.update_status()
        except Exception as ex:
            self.on_error(f"Failed to connect: {ex}")
            self._enable_controls(False)
            self.control_panel.btn_connect.setEnabled(True)
            self.status_bar.showMessage("Ready")

    def on_disconnect(self):
        self.on_acq_mode("hold")
        self.scope_connection.disconnect()
        self._enable_controls(False)
        self.status_bar.showMessage("Disconnected")

    def on_default_setup(self):
        try:
            self.scope_connection.default_setup()
            self._pull_settings_to_ui()
            self.update_status()
            self.status_bar.showMessage("Default setup applied")
        except Exception as e:
            self.on_error(f"Failed default setup: {e}")

    def on_auto_setup(self):
        """Analyze signal and auto-set gain, trigger, and timebase like a real scope."""
        sc = self.scope_connection
        if not sc.scope:
            return

        # Stop any running capture
        self.on_acq_mode("hold")
        self.status_bar.showMessage("Auto Setup: analyzing signal...")
        QApplication.processEvents()

        try:
            self._auto_setup_iterate(sc)
        except Exception as e:
            self.on_error(f"Auto Setup failed: {e}")
            return

        self._pull_settings_to_ui()
        self.update_status()
        self.status_bar.showMessage("Auto Setup complete")
        # Start continuous capture in auto mode
        self.on_acq_mode("auto")
        self.control_panel._set_acq_mode("auto")

    def _auto_setup_iterate(self, sc, max_iterations=3):
        """Iteratively adjust parameters to get a stable display."""
        # Start with generous settings for initial capture
        sc.apply_setting("gain.db", 0.0)
        sc.apply_setting("adc.samples", 5000)
        sc.apply_setting("adc.decimate", 0)
        sc.apply_setting("adc.offset", 0)
        sc.apply_setting("adc.timeout", 2.0)

        for iteration in range(max_iterations):
            # Capture a trace
            sc.scope.arm()
            timed_out = sc.scope.capture()
            data = sc.scope.get_last_trace()

            if data is None or len(data) < 10:
                continue

            vmin = float(np.min(data))
            vmax = float(np.max(data))
            vpp = vmax - vmin
            vmid = (vmin + vmax) / 2.0

            # -- Auto trigger: set to signal midpoint --
            sc.apply_setting("trigger.level", vmid)

            # -- Auto gain: target Vpp ~60-80% of full range (±0.5V = 1.0V) --
            if vpp > 0.001:
                target_vpp = 0.7  # 70% of 1.0V range
                ratio = target_vpp / vpp
                current_gain = sc.read_setting("gain.db") or 0.0
                # gain in dB: 20*log10(ratio) adjustment
                gain_adjust = 20 * np.log10(ratio)
                new_gain = current_gain + gain_adjust

                # Clamp to device range
                if sc.is_husky:
                    new_gain = max(-15.0, min(65.0, new_gain))
                else:
                    new_gain = max(-6.5, min(56.0, new_gain))

                sc.apply_setting("gain.db", round(new_gain * 2) / 2)  # snap to 0.5 steps

            # -- Auto timebase: detect frequency, show ~2-3 periods --
            mean = np.mean(data)
            diff_sign = np.diff(np.sign(data - mean))
            crossings = np.where(diff_sign)[0]
            rising = crossings[::2]

            if len(rising) >= 2:
                avg_period = float(np.mean(np.diff(rising)))
                # Target: show ~3 periods
                desired_samples = int(avg_period * 3)
                desired_samples = max(100, min(131070 if sc.is_husky else 24400, desired_samples))
                sc.apply_setting("adc.samples", desired_samples)

                # Update view to match
                self.waveform_plot.setXRange(0, desired_samples)
            else:
                # No periodic signal detected, just use current samples
                pass

            # -- Auto vertical scale: set Y range to fit signal --
            if vpp > 0.001:
                margin = vpp * 0.2
                self.waveform_plot.setYRange(vmin - margin, vmax + margin)

            # Show intermediate result
            self.waveform_plot.update_trace(data)
            self.waveform_plot.set_trigger_level(vmid)
            QApplication.processEvents()

    def on_acq_mode(self, mode):
        if mode == "hold":
            self.capture_worker._running = False
            self.capture_thread.wait()
            self.status_bar.showMessage("Hold")
            return

        # Stop current capture first if running
        if self.capture_thread.isRunning():
            self.capture_worker._running = False
            self.capture_thread.wait()

        if mode == "auto":
            self.capture_worker._running = True
            self.capture_thread.start()
        elif mode == "normal":
            self.capture_worker._running = True
            self.capture_thread.start()
        elif mode == "single":
            self.capture_worker._single_shot = True
            self.capture_thread.start()

    def _update_ui_for_device(self):
        is_husky = self.scope_connection.is_husky
        cp = self.control_panel
        cp.adc_mul_widget.setVisible(is_husky)
        cp.bits_per_sample_widget.setVisible(is_husky)
        cp.adc_src_widget.setVisible(not is_husky)
        
        if is_husky:
            cp.gain_db_spin.setRange(-15.0, 65.0)
            cp.samples_spin.setMaximum(131070)
        else:
            cp.gain_db_spin.setRange(-6.5, 56.0)
            cp.samples_spin.setMaximum(24400)

    def _pull_settings_to_ui(self):
        cp = self.control_panel
        sc = self.scope_connection

        def safe_set_spin(spin, val):
            if val is not None:
                spin.blockSignals(True)
                if isinstance(spin, QDoubleSpinBox):
                    spin.setValue(float(val))
                else:
                    spin.setValue(int(val))
                spin.blockSignals(False)

        def safe_set_combo(combo, val):
            if val is not None:
                combo.blockSignals(True)
                combo.setCurrentText(str(val))
                combo.blockSignals(False)

        safe_set_spin(cp.gain_db_spin, sc.read_setting("gain.db"))
        safe_set_combo(cp.gain_mode_combo, sc.read_setting("gain.mode"))
        safe_set_spin(cp.samples_spin, sc.read_setting("adc.samples"))
        safe_set_spin(cp.offset_spin, sc.read_setting("adc.offset"))
        safe_set_spin(cp.presamples_spin, sc.read_setting("adc.presamples"))
        safe_set_spin(cp.decimate_spin, sc.read_setting("adc.decimate"))
        safe_set_combo(cp.basic_mode_combo, sc.read_setting("adc.basic_mode"))
        safe_set_combo(cp.trigger_module_combo, sc.read_setting("trigger.module"))
        safe_set_spin(cp.adc_trigger_level_spin, sc.read_setting("trigger.level"))
        
        trig = sc.read_setting("trigger.triggers")
        if trig is not None:
            cp.triggers_edit.blockSignals(True)
            cp.triggers_edit.setText(str(trig))
            cp.triggers_edit.blockSignals(False)

        safe_set_combo(cp.clkgen_src_combo, sc.read_setting("clock.clkgen_src"))
        
        clkgen_freq = sc.read_setting("clock.clkgen_freq")
        if clkgen_freq is not None:
            safe_set_spin(cp.clkgen_freq_spin, clkgen_freq / 1e6)

        if sc.is_husky:
            safe_set_spin(cp.adc_mul_spin, sc.read_setting("clock.adc_mul"))
            safe_set_combo(cp.adc_bits_combo, sc.read_setting("adc.bits_per_sample"))
        else:
            safe_set_combo(cp.adc_src_combo, sc.read_setting("clock.adc_src"))
            
        safe_set_spin(cp.timeout_spin, sc.read_setting("adc.timeout"))

    def on_trace_ready(self, data, status):
        self.waveform_plot.update_trace(data)
        self.control_panel.update_status(status)
        self.update_measurements(data, status)
        if self.fft_plot.isVisible():
            sample_rate = status.get("adc_rate", 1.0) or 1.0
            self.fft_plot.set_sample_rate(sample_rate)
            self.fft_plot.update_trace(data)
        params = status.get('_params', [])
        if params:
            self.control_panel.update_script_params(params)

    def on_error(self, err_msg):
        self.status_bar.showMessage(f"Error: {err_msg}")
        QMessageBox.warning(self, "Error", err_msg)

    def update_status(self):
        stats = self.scope_connection.read_status()
        self.control_panel.update_status(stats)

    def update_measurements(self, data, status=None):
        if data is None or len(data) == 0:
            return

        if status is None:
            status = self.scope_connection.read_status()
        sample_rate = status.get("adc_rate", 1.0) or 1.0
        
        vmin = float(np.min(data))
        vmax = float(np.max(data))
        vpp = vmax - vmin
        mean = np.mean(data)
        
        # Protective check for constant data where np.sign might return zeros 
        # that don't produce valid crossings
        diff_sign = np.diff(np.sign(data - mean))
        crossings = np.where(diff_sign)[0]
        rising = crossings[::2]
        
        freq = None
        if len(rising) >= 2:
            avg_period_samples = np.float64(np.mean(np.diff(rising)))
            if avg_period_samples > 0:
                freq = sample_rate / avg_period_samples
            
        meas = {"vmin": vmin, "vmax": vmax, "vpp": vpp, "freq": freq}
        self.control_panel.update_measurements(meas)

    def _on_plot_trigger_drag(self, level):
        cp = self.control_panel
        cp.adc_trigger_level_spin.blockSignals(True)
        cp.adc_trigger_level_spin.setValue(level)
        cp.adc_trigger_level_spin.blockSignals(False)
        self.capture_worker._settings_queue.put_nowait(("trigger.level", level))

    def _on_volts_div(self, volts_per_div):
        # 8 divisions on screen
        half_range = volts_per_div * 4
        self.waveform_plot.setYRange(-half_range, half_range)

    def _on_time_div(self, samples_per_div):
        # 10 divisions on screen
        total = samples_per_div * 10
        vr = self.waveform_plot.viewRange()
        center = (vr[0][0] + vr[0][1]) / 2
        self.waveform_plot.setXRange(center - total / 2, center + total / 2)

    def closeEvent(self, event):
        self.on_acq_mode("hold")
        self.scope_connection.disconnect()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_STYLESHEET)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
