import sys
import queue
import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QScrollArea, QGroupBox, QPushButton, QLabel,
    QDoubleSpinBox, QSpinBox, QComboBox, QLineEdit, QMessageBox,
    QFormLayout
)
from PyQt6.QtCore import Qt, QObject, pyqtSignal, QThread

import chipwhisperer as cw


import threading

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
                    self.trace_ready.emit(data, status)
                    
            except Exception as e:
                self.error_occurred.emit(str(e))
                self._running = False
                break


class WaveformPlot(pg.PlotWidget):
    def __init__(self):
        super().__init__()
        self.setBackground('k')
        self.showGrid(x=True, y=True)
        self.setMouseEnabled(x=True, y=False)
        self.setYRange(-0.5, 0.5)
        self.curve = self.plot(pen='y')

    def update_trace(self, data: np.ndarray):
        self.curve.setData(data)


class ControlPanel(QWidget):
    setting_changed = pyqtSignal(str, object)
    connect_clicked = pyqtSignal()
    disconnect_clicked = pyqtSignal()
    default_setup_clicked = pyqtSignal()
    start_capture_clicked = pyqtSignal()
    stop_capture_clicked = pyqtSignal()
    single_capture_clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.layout = QVBoxLayout(self)
        self._setup_connection_group()
        self._setup_capture_group()
        self._setup_gain_group()
        self._setup_sampling_group()
        self._setup_trigger_group()
        self._setup_clock_group()
        self._setup_timeout_group()
        self._setup_status_group()
        self._setup_measurements_group()
        
        self.layout.addStretch()

    def _setup_connection_group(self):
        gb = QGroupBox("Connection")
        l = QVBoxLayout()
        self.btn_connect = QPushButton("Connect")
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_default = QPushButton("Default Setup")
        
        self.btn_connect.clicked.connect(self.connect_clicked.emit)
        self.btn_disconnect.clicked.connect(self.disconnect_clicked.emit)
        self.btn_default.clicked.connect(self.default_setup_clicked.emit)
        
        l.addWidget(self.btn_connect)
        l.addWidget(self.btn_disconnect)
        l.addWidget(self.btn_default)
        gb.setLayout(l)
        self.layout.addWidget(gb)

    def _setup_capture_group(self):
        gb = QGroupBox("Capture")
        l = QVBoxLayout()
        self.btn_start = QPushButton("Start Continuous")
        self.btn_stop = QPushButton("Stop")
        self.btn_single = QPushButton("Single Shot")
        
        self.btn_start.clicked.connect(self.start_capture_clicked.emit)
        self.btn_stop.clicked.connect(self.stop_capture_clicked.emit)
        self.btn_single.clicked.connect(self.single_capture_clicked.emit)
        
        l.addWidget(self.btn_start)
        l.addWidget(self.btn_stop)
        l.addWidget(self.btn_single)
        gb.setLayout(l)
        self.layout.addWidget(gb)

    def _setup_gain_group(self):
        gb = QGroupBox("Gain")
        l = QFormLayout()
        
        self.gain_db_spin = QDoubleSpinBox()
        self.gain_db_spin.setRange(-15.0, 65.0)
        self.gain_db_spin.setSingleStep(0.5)
        self.gain_db_spin.valueChanged.connect(lambda v: self.setting_changed.emit("gain.db", v))
        
        self.gain_mode_combo = QComboBox()
        self.gain_mode_combo.addItems(["low", "high"])
        self.gain_mode_combo.currentTextChanged.connect(lambda v: self.setting_changed.emit("gain.mode", v))
        
        l.addRow("dB", self.gain_db_spin)
        l.addRow("Mode", self.gain_mode_combo)
        gb.setLayout(l)
        self.layout.addWidget(gb)

    def _setup_sampling_group(self):
        gb = QGroupBox("Sampling")
        l = QFormLayout()
        
        self.samples_spin = QSpinBox()
        self.samples_spin.setRange(1, 131070)
        self.samples_spin.valueChanged.connect(lambda v: self.setting_changed.emit("adc.samples", v))
        
        self.offset_spin = QSpinBox()
        self.offset_spin.setRange(0, 2147483647) # arbitrary large
        self.offset_spin.valueChanged.connect(lambda v: self.setting_changed.emit("adc.offset", v))
        
        self.presamples_spin = QSpinBox()
        self.presamples_spin.setRange(0, 131070)
        self.presamples_spin.valueChanged.connect(lambda v: self.setting_changed.emit("adc.presamples", v))
        
        self.decimate_spin = QSpinBox()
        self.decimate_spin.setRange(0, 65535)
        self.decimate_spin.valueChanged.connect(lambda v: self.setting_changed.emit("adc.decimate", v))
        
        l.addRow("Samples", self.samples_spin)
        l.addRow("Offset", self.offset_spin)
        l.addRow("Presamples", self.presamples_spin)
        l.addRow("Decimate", self.decimate_spin)
        gb.setLayout(l)
        self.layout.addWidget(gb)

    def _setup_trigger_group(self):
        gb = QGroupBox("Trigger")
        l = QFormLayout()

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
        l.addRow("ADC Lvl", self.adc_trigger_level_spin)
        gb.setLayout(l)
        self.layout.addWidget(gb)

    def _setup_clock_group(self):
        gb = QGroupBox("Clock")
        l = QFormLayout()
        
        self.clkgen_src_combo = QComboBox()
        self.clkgen_src_combo.addItems(['system', 'extclk', 'internal'])
        self.clkgen_src_combo.currentTextChanged.connect(lambda v: self.setting_changed.emit("clock.clkgen_src", v))
        
        self.clkgen_freq_spin = QDoubleSpinBox()
        self.clkgen_freq_spin.setRange(0.1, 200.0)
        self.clkgen_freq_spin.setSuffix(" MHz")
        self.clkgen_freq_spin.valueChanged.connect(lambda v: self.setting_changed.emit("clock.clkgen_freq", v * 1e6))
        
        self.adc_mul_spin = QSpinBox() # Husky only
        self.adc_mul_spin.setRange(1, 50)
        self.adc_mul_spin.valueChanged.connect(lambda v: self.setting_changed.emit("clock.adc_mul", v))
        
        self.adc_src_combo = QComboBox() # Lite only
        self.adc_src_combo.addItems(['clkgen_x1', 'clkgen_x4', 'extclk_x1', 'extclk_x4'])
        self.adc_src_combo.currentTextChanged.connect(lambda v: self.setting_changed.emit("clock.adc_src", v))
        
        self.adc_bits_combo = QComboBox() # Husky only
        self.adc_bits_combo.addItems(['8', '12'])
        self.adc_bits_combo.currentTextChanged.connect(lambda v: self.setting_changed.emit("adc.bits_per_sample", int(v)))
        
        l.addRow("Src", self.clkgen_src_combo)
        l.addRow("Freq", self.clkgen_freq_spin)
        l.addRow("ADC mul", self.adc_mul_spin)
        l.addRow("ADC src", self.adc_src_combo)
        l.addRow("ADC bits", self.adc_bits_combo)
        
        self.adc_mul_widget = self.adc_mul_spin
        self.adc_src_widget = self.adc_src_combo
        self.bits_per_sample_widget = self.adc_bits_combo
        
        gb.setLayout(l)
        self.layout.addWidget(gb)

    def _setup_timeout_group(self):
        gb = QGroupBox("Timeout")
        l = QVBoxLayout()
        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(0.1, 60.0)
        self.timeout_spin.setValue(2.0)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.valueChanged.connect(lambda v: self.setting_changed.emit("adc.timeout", v))
        l.addWidget(self.timeout_spin)
        gb.setLayout(l)
        self.layout.addWidget(gb)

    def _setup_status_group(self):
        gb = QGroupBox("Status")
        l = QFormLayout()
        self.lbl_device = QLabel("-")
        self.lbl_adc_freq = QLabel("-")
        self.lbl_adc_rate = QLabel("-")
        self.lbl_clkgen_freq = QLabel("-")
        self.lbl_trig_count = QLabel("-")
        
        l.addRow("Device:", self.lbl_device)
        l.addRow("ADC freq:", self.lbl_adc_freq)
        l.addRow("ADC rate:", self.lbl_adc_rate)
        l.addRow("Clkgen freq:", self.lbl_clkgen_freq)
        l.addRow("Trig cnt:", self.lbl_trig_count)
        gb.setLayout(l)
        self.layout.addWidget(gb)

    def _setup_measurements_group(self):
        gb = QGroupBox("Measurements")
        l = QVBoxLayout()
        self.lbl_vstats = QLabel("Vmin: -  Vmax: -  Vpp: -")
        self.lbl_freq = QLabel("Freq: -")
        l.addWidget(self.lbl_vstats)
        l.addWidget(self.lbl_freq)
        gb.setLayout(l)
        self.layout.addWidget(gb)

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

        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Wrap control panel in scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.control_panel)
        scroll.setMinimumWidth(320)
        
        splitter.addWidget(scroll)
        splitter.addWidget(self.waveform_plot)
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
        
        self.control_panel.start_capture_clicked.connect(self.on_start_capture)
        self.control_panel.stop_capture_clicked.connect(self.on_stop_capture)
        self.control_panel.single_capture_clicked.connect(self.on_single_capture)

        self._enable_controls(False)

    def _enable_controls(self, connected):
        self.control_panel.btn_connect.setEnabled(not connected)
        self.control_panel.btn_disconnect.setEnabled(connected)
        self.control_panel.btn_default.setEnabled(connected)
        self.control_panel.btn_start.setEnabled(connected)
        self.control_panel.btn_stop.setEnabled(connected)
        self.control_panel.btn_single.setEnabled(connected)

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
        self.on_stop_capture()
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

    def on_start_capture(self):
        self.capture_worker._running = True
        self.capture_thread.start()
        
    def on_stop_capture(self):
        self.capture_worker._running = False
        self.capture_thread.wait()

    def on_single_capture(self):
        self.capture_worker._single_shot = True
        if not self.capture_thread.isRunning():
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

    def closeEvent(self, event):
        self.on_stop_capture()
        self.scope_connection.disconnect()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
