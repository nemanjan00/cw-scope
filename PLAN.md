# ChipWhisperer Oscilloscope GUI — Implementation Plan

## Context
Build a Python GUI oscilloscope app for ChipWhisperer devices (Husky and Lite). The Husky has a sample signal connected to its POS pin. The app should provide real-time waveform visualization with full control over capture parameters.

## Files to Create/Modify
- **`scope.py`** (new) — single-file GUI application (~600-750 lines)
- **`requirements.txt`** (update) — add PyQt6, pyqtgraph, numpy

## requirements.txt
```
chipwhisperer @ git+https://github.com/newaetech/chipwhisperer.git
PyQt6
pyqtgraph
numpy
```

---

## Architecture

Single-file app (`scope.py`) with these classes:

### 1. `ScopeConnection`
Plain Python class wrapping `cw.scope()`.

- **`connect()`** — calls `cw.scope()`, detects device type via `scope._is_husky`, returns device type string
- **`default_setup()`** — calls `scope.default_setup()`
- **`disconnect()`** — calls `scope.dis()`
- **`is_husky`** property — `True` if Husky/Husky+
- **`apply_setting(attr_path, value)`** — generic setter resolving dotted paths like `"gain.db"`, e.g.:
  ```python
  def apply_setting(self, attr_path, value):
      parts = attr_path.split(".")
      obj = self.scope
      for part in parts[:-1]:
          obj = getattr(obj, part)
      setattr(obj, parts[-1], value)
  ```
- **`read_setting(attr_path)`** — same approach, but `getattr` on the last part
- **`read_status()`** — returns dict with read-only values: `adc_freq`, `adc_rate`, `clkgen_freq`, `trig_count`, device name

### 2. `CaptureWorker(QObject)`
Runs in a dedicated `QThread`. All USB device access happens exclusively in this thread.

**Signals:**
- `trace_ready = pyqtSignal(object)` — emits numpy ndarray
- `error_occurred = pyqtSignal(str)` — emits error messages
- `capture_status = pyqtSignal(str)` — "Armed", "Waiting for trigger", "Captured", "Timeout"

**State:**
- `self._running: bool` — continuous capture flag
- `self._single_shot: bool` — one-shot flag
- `self._settings_queue: queue.Queue` — thread-safe settings queue

**Core loop (slot connected to thread start):**
```python
def run_loop(self):
    while self._running or self._single_shot:
        self._single_shot = False
        self._apply_pending_settings()  # drain settings queue
        try:
            self.capture_status.emit("Armed...")
            self._scope.scope.arm()
            self.capture_status.emit("Waiting for trigger...")
            timed_out = self._scope.scope.capture()
            if timed_out:
                self.capture_status.emit("Timeout")
                if not self._running:
                    break
                continue
            data = self._scope.scope.get_last_trace()
            self.trace_ready.emit(data)
            self.capture_status.emit("Captured")
        except Exception as e:
            self.error_occurred.emit(str(e))
            self._running = False
            break
```

**Settings queue pattern (thread-safe):**
```python
def queue_setting(self, attr_path, value):
    """Called from GUI thread."""
    self._settings_queue.put((attr_path, value))

def _apply_pending_settings(self):
    """Called from worker thread between captures."""
    while not self._settings_queue.empty():
        attr_path, value = self._settings_queue.get_nowait()
        self._scope.apply_setting(attr_path, value)
```

### 3. `WaveformPlot(pg.PlotWidget)`
- Dark background, grid enabled, anti-aliasing off (performance)
- Single `PlotDataItem` for the waveform curve
- **`update_trace(data: np.ndarray)`** — X axis = sample index, Y axis = voltage [-0.5, 0.5]
- Auto-range toggle checkbox
- pyqtgraph handles 131k points at 30+ FPS easily

### 4. `ControlPanel(QWidget)`
Scrollable left sidebar (`QScrollArea`) with `QGroupBox` sections:

#### ConnectionGroup
- `QPushButton("Connect")` — connects, runs `default_setup()`, populates controls, shows/hides device-specific widgets
- `QPushButton("Disconnect")` — stops capture, disconnects
- `QPushButton("Default Setup")` — resets to defaults, refreshes controls

#### CaptureGroup
- `QPushButton("Start Continuous")` / `QPushButton("Stop")` — toggle
- `QPushButton("Single Shot")`

#### GainGroup
- `QDoubleSpinBox` for `gain.db` — Lite: [-6.5, 56], Husky: [-15, 65], step 0.5
- `QComboBox` for `gain.mode` — ['low', 'high']

#### SamplingGroup
- `QSpinBox` for `adc.samples` — Lite max: 24400, Husky max: 131070
- `QSpinBox` for `adc.offset` — range 0 to 2^32
- `QSpinBox` for `adc.presamples` — range 0 to samples
- `QSpinBox` for `adc.decimate` — range 0 to 65535

#### TriggerGroup
- `QComboBox` for `adc.basic_mode` — `['rising_edge', 'falling_edge', 'high', 'low']`
- `QLineEdit` for `trigger.triggers` — default "tio4", accepts OR/AND/NAND combos (e.g. "tio1 OR tio2")

#### ClockGroup
- `QComboBox` for `clock.clkgen_src` — `['system', 'extclk']` (Lite uses `'internal'` instead of `'system'`)
- `QDoubleSpinBox` for `clock.clkgen_freq` — display in MHz, convert to Hz internally
- **Husky-only:** `QSpinBox` for `clock.adc_mul` — range 1 to 50
- **Lite-only:** `QComboBox` for `clock.adc_src` — `['clkgen_x1', 'clkgen_x4', 'extclk_x1', 'extclk_x4']`
- **Husky-only:** `QComboBox` for `adc.bits_per_sample` — `[8, 12]`

#### TimeoutGroup
- `QDoubleSpinBox` for `adc.timeout` — range 0.1 to 60 seconds

#### StatusGroup (read-only, updated after each capture)
- `QLabel` — device name/type
- `QLabel` — ADC frequency (formatted MHz)
- `QLabel` — ADC sample rate (formatted MHz)
- `QLabel` — clkgen frequency
- `QLabel` — trigger count

#### MeasurementsGroup (computed from trace)
- `QLabel` — Vmin, Vmax, Vpp
- `QLabel` — estimated frequency (zero-crossing method)

### 5. `MainWindow(QMainWindow)`
- **Layout:** `QSplitter` — ControlPanel (left, ~320px fixed) + WaveformPlot (right, expanding)
- **Status bar** for capture state messages
- **Signal wiring:**
  - `CaptureWorker.trace_ready` → `WaveformPlot.update_trace` + `update_measurements()`
  - `CaptureWorker.error_occurred` → `QMessageBox.warning` or status bar
  - `CaptureWorker.capture_status` → status bar
  - Each control widget's change signal → `worker.queue_setting(attr_path, value)`
- **`closeEvent`** — stop worker, disconnect scope, clean up thread

---

## Device-Specific Logic

After connection, detect device type and adjust UI:

```python
def on_connected(self, device_type):
    is_husky = device_type in ("cwhusky", "cwhuskyplus")
    self.control_panel.adc_mul_widget.setVisible(is_husky)
    self.control_panel.bits_per_sample_widget.setVisible(is_husky)
    self.control_panel.adc_src_widget.setVisible(not is_husky)
    if is_husky:
        self.control_panel.gain_db_spin.setRange(-15.0, 65.0)
        self.control_panel.samples_spin.setMaximum(131070)
    else:
        self.control_panel.gain_db_spin.setRange(-6.5, 56.0)
        self.control_panel.samples_spin.setMaximum(24400)
```

---

## Measurement Calculations

```python
def compute_measurements(data: np.ndarray, sample_rate: float) -> dict:
    vmin = float(np.min(data))
    vmax = float(np.max(data))
    vpp = vmax - vmin
    mean = np.mean(data)
    crossings = np.where(np.diff(np.sign(data - mean)))[0]
    rising = crossings[::2]
    if len(rising) >= 2:
        avg_period_samples = np.mean(np.diff(rising))
        freq = sample_rate / avg_period_samples
    else:
        freq = None
    return {"vmin": vmin, "vmax": vmax, "vpp": vpp, "freq": freq}
```

---

## Thread Lifecycle

```python
# In MainWindow.__init__:
self.capture_thread = QThread()
self.capture_worker = CaptureWorker(self.scope_connection)
self.capture_worker.moveToThread(self.capture_thread)
self.capture_thread.started.connect(self.capture_worker.run_loop)
self.capture_worker.trace_ready.connect(self.on_trace_ready)

# Start continuous:
self.capture_worker._running = True
self.capture_thread.start()

# Stop:
self.capture_worker._running = False
self.capture_thread.quit()
self.capture_thread.wait()
```

---

## GUI Layout

```
+------------------------------------------------------------+
| CW-Scope                                        [_][O][X]  |
+------------------------------------------------------------+
| +----------+ +--------------------------------------------+|
| | Connection| |                                            ||
| | [Connect] | |     Waveform Plot (pyqtgraph)             ||
| | [Disconn] | |                                            ||
| | [Default] | |     Y: -0.5 to 0.5                        ||
| +----------+ |     X: 0 to N samples                      ||
| | Capture   | |                                            ||
| | [Start]   | |     ~~~~~~~~~~~~~~~~~~~~~~~~~             ||
| | [Single]  | |     ~~  waveform curve   ~~~~             ||
| +----------+ |     ~~~~~~~~~~~~~~~~~~~~~~~~~             ||
| | Gain      | |                                            ||
| | dB [===]  | +--------------------------------------------+|
| | Mode [v]  | |  Vmin: -0.23  Vmax: 0.31  Vpp: 0.54      ||
| +----------+ |  Freq: ~7.37 MHz                           ||
| | Sampling  | +--------------------------------------------+|
| | Samples   |                                               |
| | Offset    |  Status: Armed... | ADC: 29.53 MHz           |
| | Presamp   | +--------------------------------------------+|
| | Decimate  |                                               |
| +----------+                                               |
| | Trigger   |                                               |
| | Mode [v]  |                                               |
| | Pin [___] |                                               |
| +----------+                                               |
| | Clock     |                                               |
| | Src  [v]  |                                               |
| | Freq [  ] |                                               |
| | ADC mul   |  <- Husky only                                |
| | ADC src   |  <- Lite only                                 |
| | ADC bits  |  <- Husky only                                |
| +----------+                                               |
| | Timeout   |                                               |
| | [    ] s  |                                               |
| +----------+                                               |
| | Status    |                                               |
| | Device:.. |                                               |
| | ADC freq: |                                               |
| | ADC rate: |                                               |
| | Trig cnt: |                                               |
| +----------+                                               |
+------------------------------------------------------------+
```

---

## ChipWhisperer API Reference

### Connection
```python
import chipwhisperer as cw
scope = cw.scope()           # auto-detect device
scope.default_setup()        # sane defaults
scope.dis()                  # disconnect
scope._is_husky              # True for Husky/Husky+
scope.get_name()             # device name string
```

### Capture
```python
scope.arm()
timed_out = scope.capture()  # True=timeout, False=success
data = scope.get_last_trace()  # numpy array, float [-0.5, 0.5]
data = scope.get_last_trace(as_int=True)  # raw ADC counts
```

### All Configurable Parameters
| Parameter | API | Type | Range |
|-----------|-----|------|-------|
| Gain dB | `scope.gain.db` | float | Lite: -6.5–56, Husky: -15–65 |
| Gain mode | `scope.gain.mode` | str | 'low', 'high' |
| Samples | `scope.adc.samples` | int | Lite: max 24400, Husky: max 131070 |
| Offset | `scope.adc.offset` | int | 0 to 2^32 |
| Presamples | `scope.adc.presamples` | int | 0 to samples |
| Decimate | `scope.adc.decimate` | int | 0 to 65535 |
| Trigger mode | `scope.adc.basic_mode` | str | 'rising_edge', 'falling_edge', 'high', 'low' |
| Trigger pins | `scope.trigger.triggers` | str | 'tio1'–'tio4', 'nrst', 'sma', OR/AND/NAND combos |
| Clock source | `scope.clock.clkgen_src` | str | 'system'/'internal', 'extclk' |
| Clock freq | `scope.clock.clkgen_freq` | float | Hz |
| ADC multiplier | `scope.clock.adc_mul` | int | Husky only, 1–50 |
| ADC source | `scope.clock.adc_src` | str | Lite only: 'clkgen_x1', 'clkgen_x4', 'extclk_x1', 'extclk_x4' |
| ADC bits | `scope.adc.bits_per_sample` | int | Husky only: 8 or 12 |
| Timeout | `scope.adc.timeout` | float | seconds |

### Read-Only Status
| Value | API |
|-------|-----|
| ADC frequency | `scope.clock.adc_freq` |
| ADC sample rate | `scope.clock.adc_rate` |
| Clkgen frequency | `scope.clock.clkgen_freq` |
| Trigger count | `scope.adc.trig_count` |

---

## Implementation Steps

1. Update `requirements.txt`
2. Scaffold `scope.py` with imports, `main()`, empty `MainWindow`
3. Implement `ScopeConnection` (connect/disconnect/apply_setting/read_status)
4. Implement `ControlPanel` with all widget groups, wire ConnectionGroup to `ScopeConnection`
5. Implement `WaveformPlot` with `update_trace()` method
6. Implement `CaptureWorker` with continuous and single-shot modes, wire signals to plot
7. Wire all control widgets to settings application via settings queue
8. Implement device-specific show/hide logic based on `_is_husky`
9. Implement measurement calculations and status display updates
10. Implement clean shutdown in `closeEvent`

## Verification
1. `pip install -r requirements.txt`
2. `python scope.py` — app window opens
3. Click Connect — device detected, controls populated, device-specific widgets shown/hidden
4. Click Start Continuous — waveform updates in real-time
5. Adjust gain/samples/trigger — settings applied between captures
6. Single Shot — captures one trace and stops
7. Close window — clean disconnect, no errors
